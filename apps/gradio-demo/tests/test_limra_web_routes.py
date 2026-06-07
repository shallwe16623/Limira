import io
import json
import sys
import zipfile
import asyncio
from pathlib import Path

import pytest
import httpx
from fastapi import FastAPI, HTTPException


ROOT = Path(__file__).resolve().parents[3]
LIMRA_BACKEND = ROOT / "apps/limra-web/backend"
sys.path.insert(0, str(LIMRA_BACKEND))

from open_webui.routers import limra  # noqa: E402


class FakeArchiveClient:
    def __init__(self, archive_bytes: bytes | None = None):
        self.archive_bytes = archive_bytes or _archive_zip()
        self.calls = []

    async def download_archive(self, task, user):
        self.calls.append({"task": task, "user": user})
        return self.archive_bytes


class FakeResearchClient:
    def __init__(
        self,
        *,
        runner_task_id="runner-task-a",
        events=None,
        status_payload=None,
        stream_exception=None,
    ):
        self.runner_task_id = runner_task_id
        self.events = events or []
        self.status_payload = status_payload or {
            "task_id": runner_task_id,
            "status": "completed",
            "archive_status": "ready",
        }
        self.stream_exception = stream_exception
        self.create_calls = []
        self.stream_calls = []
        self.status_calls = []

    async def create_research_task(self, *, query, scenario, user):
        self.create_calls.append(
            {
                "query": query,
                "scenario": scenario,
                "user": user,
            }
        )
        return {
            "task_id": self.runner_task_id,
            "status": "queued",
            "stream_url": f"/mirothinker/tasks/{self.runner_task_id}/events",
            "task_url": f"/mirothinker/tasks/{self.runner_task_id}",
        }

    async def stream_events(self, *, task, user):
        self.stream_calls.append({"task": task, "user": user})
        if self.stream_exception:
            raise self.stream_exception
        for event in self.events:
            yield event

    async def get_task_status(self, *, task, user):
        self.status_calls.append({"task": task, "user": user})
        return dict(self.status_payload)


@pytest.mark.asyncio
async def test_create_research_uses_limra_namespace_and_rejects_body_user_id():
    repo = limra.InMemoryLimraTaskRepository()
    research = FakeResearchClient()
    user = limra.LimraUser("user-a")

    payload = await limra.create_research_task(
        {"query": "track sanctions", "scenario": "sanctions"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )

    assert payload["task_url"].startswith("/api/limra/tasks/")
    assert payload["events_url"].startswith("/api/limra/tasks/")
    assert payload["artifacts_url"].startswith("/api/limra/tasks/")
    assert payload["scenario"] == "sanctions"
    assert payload["query"] == "track sanctions"
    assert research.create_calls == [
        {
            "query": "track sanctions",
            "scenario": "sanctions",
            "user": user,
        }
    ]
    task = repo.get_task(payload["task_id"])
    assert task.runner_task_id == "runner-task-a"
    _assert_no_browser_leak(payload)

    with pytest.raises(HTTPException) as rejected:
        await limra.create_research_task(
            {"query": "track sanctions", "user_id": "attacker"},
            request=None,
            user=user,
            repo=repo,
            research_client=research,
        )
    assert rejected.value.status_code == 400


@pytest.mark.asyncio
async def test_create_research_records_failed_web_task_when_runner_start_fails():
    repo = limra.InMemoryLimraTaskRepository()
    research = FakeResearchClient()
    user = limra.LimraUser("user-a")

    async def fail_create_research_task(*_args, **_kwargs):
        raise HTTPException(status_code=503, detail="runner_unavailable")

    research.create_research_task = fail_create_research_task

    with pytest.raises(HTTPException) as failed:
        await limra.create_research_task(
            {"query": "runner fails after web task is allocated"},
            request=None,
            user=user,
            repo=repo,
            research_client=research,
        )

    assert failed.value.status_code == 503
    assert len(repo.tasks) == 1
    task = next(iter(repo.tasks.values()))
    assert task.owner_user_id == "user-a"
    assert task.status == "failed"
    assert task.archive_status == "failed"
    assert task.error == "runner_unavailable"
    assert task.runner_task_id is None


@pytest.mark.asyncio
async def test_user_isolation_for_task_status_and_archive_download():
    repo = limra.InMemoryLimraTaskRepository()
    archive = FakeArchiveClient()
    research = FakeResearchClient()
    user_a = limra.LimraUser("user-a")
    user_b = limra.LimraUser("user-b")

    created = await limra.create_research_task(
        {"query": "red sea shipping risk"},
        request=None,
        user=user_a,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    repo.tasks[task_id].archive_status = "ready"
    repo.tasks[task_id].runner_task_id = "runner-task-a"

    with pytest.raises(HTTPException) as forbidden_status:
        await limra.get_task(task_id, user=user_b, repo=repo)
    assert forbidden_status.value.status_code == 404

    with pytest.raises(HTTPException) as forbidden_archive:
        await limra.download_task_archive(
            task_id,
            user=user_b,
            repo=repo,
            archive_client=archive,
        )
    assert forbidden_archive.value.status_code == 404

    response = await limra.download_task_archive(
        task_id,
        user=user_a,
        repo=repo,
        archive_client=archive,
    )

    assert response.media_type == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(response.body)).namelist() == [
        "metadata.json",
        "report.html",
        "report.md",
        "trace.json",
    ]
    assert archive.calls[0]["task"].runner_task_id == "runner-task-a"
    assert archive.calls[0]["user"].id == "user-a"
    _assert_no_browser_leak(response.body.decode("latin1"))


@pytest.mark.asyncio
async def test_admin_access_requires_explicit_admin_route():
    repo = limra.InMemoryLimraTaskRepository()
    archive = FakeArchiveClient()
    research = FakeResearchClient()
    user = limra.LimraUser("user-a")
    admin = limra.LimraUser("admin-user", role="admin")

    created = await limra.create_research_task(
        {"query": "critical minerals policy"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    repo.tasks[task_id].archive_status = "ready"

    with pytest.raises(HTTPException) as normal_admin_route:
        await limra.get_task(task_id, user=admin, repo=repo)
    assert normal_admin_route.value.status_code == 404

    payload = await limra.admin_get_task(task_id, user=admin, repo=repo)
    assert payload["owner_user_id"] == "user-a"
    assert payload["admin"] == "admin-user"

    response = await limra.admin_download_task_archive(
        task_id,
        user=admin,
        repo=repo,
        archive_client=archive,
    )
    assert response.media_type == "application/zip"


@pytest.mark.asyncio
async def test_archive_proxy_rejects_not_ready_and_invalid_zip_members():
    repo = limra.InMemoryLimraTaskRepository()
    research = FakeResearchClient()
    user = limra.LimraUser("user-a")
    created = await limra.create_research_task(
        {"query": "query"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    with pytest.raises(HTTPException) as not_ready:
        await limra.download_task_archive(
            task_id,
            user=user,
            repo=repo,
            archive_client=FakeArchiveClient(),
        )
    assert not_ready.value.status_code == 409

    repo.tasks[task_id].archive_status = "ready"
    with pytest.raises(HTTPException) as invalid_zip:
        await limra.download_task_archive(
            task_id,
            user=user,
            repo=repo,
            archive_client=FakeArchiveClient(_archive_zip(extra_member=True)),
        )
    assert invalid_zip.value.status_code == 502


def test_runner_service_headers_are_server_side_only():
    headers = limra.runner_service_headers(
        limra.LimraUser("user-a", role="admin"),
        "server-only-token",
    )

    assert headers == {
        "X-OpenWebUI-User-Id": "user-a",
        "X-OpenWebUI-User-Role": "admin",
        "X-MiroThinker-Service-Token": "server-only-token",
    }


def test_limra_repository_factory_requires_explicit_memory_fallback(monkeypatch):
    monkeypatch.delenv("LIMRA_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LIMRA_REPOSITORY_BACKEND", "postgres")

    with pytest.raises(RuntimeError, match="limra_postgres_database_url_missing"):
        limra.create_limra_task_repository_from_env()

    monkeypatch.setenv("LIMRA_DATABASE_URL", "postgresql://limra:test@postgres:5432/limra")
    repo = limra.create_limra_task_repository_from_env()
    assert isinstance(repo, limra.PostgresLimraTaskRepository)
    assert repo.database_url == "postgresql://limra:test@postgres:5432/limra"
    assert repo._engine is None

    monkeypatch.setenv("LIMRA_REPOSITORY_BACKEND", "memory")
    monkeypatch.delenv("LIMRA_ALLOW_IN_MEMORY_REPOSITORY", raising=False)
    with pytest.raises(RuntimeError, match="limra_in_memory_repository_requires_explicit_fallback"):
        limra.create_limra_task_repository_from_env()

    monkeypatch.setenv("LIMRA_ALLOW_IN_MEMORY_REPOSITORY", "true")
    repo = limra.create_limra_task_repository_from_env()
    assert isinstance(repo, limra.InMemoryLimraTaskRepository)


def test_limra_runtime_state_factory_requires_redis_or_explicit_memory(monkeypatch):
    monkeypatch.setenv("LIMRA_RUNTIME_STATE_BACKEND", "redis")
    with pytest.raises(RuntimeError, match="limra_redis_runtime_state_missing"):
        limra.create_limra_runtime_state_from_env(redis_client=None)

    redis = FakeRedisClient()
    runtime_state = limra.create_limra_runtime_state_from_env(redis_client=redis)
    assert isinstance(runtime_state, limra.RedisLimraRuntimeState)
    assert runtime_state.redis_client is redis

    monkeypatch.setenv("LIMRA_RUNTIME_STATE_BACKEND", "memory")
    monkeypatch.delenv("LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE", raising=False)
    with pytest.raises(
        RuntimeError,
        match="limra_in_memory_runtime_state_requires_explicit_fallback",
    ):
        limra.create_limra_runtime_state_from_env(redis_client=None)

    monkeypatch.setenv("LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE", "true")
    runtime_state = limra.create_limra_runtime_state_from_env(redis_client=None)
    assert isinstance(runtime_state, limra.InMemoryLimraRuntimeState)


def test_limra_object_storage_factory_requires_s3_or_explicit_memory(monkeypatch):
    for key in (
        "LIMRA_OBJECT_BUCKET",
        "S3_BUCKET",
        "MINIO_BUCKET",
        "S3_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LIMRA_OBJECT_STORAGE_BACKEND", "s3")

    with pytest.raises(RuntimeError, match="limra_object_bucket_missing"):
        limra.create_limra_object_storage_from_env(s3_client=FakeS3Client())

    monkeypatch.setenv("LIMRA_OBJECT_BUCKET", "limra-artifacts")
    with pytest.raises(RuntimeError, match="limra_s3_endpoint_url_missing"):
        limra.create_limra_object_storage_from_env(s3_client=FakeS3Client())

    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    with pytest.raises(RuntimeError, match="limra_s3_credentials_missing"):
        limra.create_limra_object_storage_from_env(s3_client=FakeS3Client())

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "limra_minio")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "replace-with-local-minio-password")
    storage = limra.create_limra_object_storage_from_env(s3_client=FakeS3Client())
    assert isinstance(storage, limra.S3LimraObjectStorage)
    assert storage.bucket == "limra-artifacts"
    assert storage.endpoint_url == "http://minio:9000"

    monkeypatch.setenv("LIMRA_OBJECT_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE", raising=False)
    with pytest.raises(
        RuntimeError,
        match="limra_in_memory_object_storage_requires_explicit_fallback",
    ):
        limra.create_limra_object_storage_from_env()

    monkeypatch.setenv("LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE", "true")
    storage = limra.create_limra_object_storage_from_env()
    assert isinstance(storage, limra.InMemoryLimraObjectStorage)


def test_limra_object_keys_are_server_generated_owner_scoped_and_safe():
    key = limra.build_limra_object_key(
        owner_user_id="analyst@example.com",
        category="uploads",
        task_id="../task/alpha",
        filename="../../secret.env",
        object_id="../browser-supplied-key",
        key_prefix="../limra",
    )

    assert key.startswith("limra/users/")
    assert "/tasks/task-alpha/uploads/" in key
    assert key.endswith("browser-supplied-key.bin")
    assert "analyst@example.com" not in key
    assert "secret.env" not in key
    assert ".." not in key
    assert not key.startswith("/")

    report_key = limra.build_limra_object_key(
        owner_user_id="analyst@example.com",
        category="reports",
        task_id="task-a",
        extension="html",
        object_id="report-a",
    )
    assert report_key.endswith("/reports/report-a.html")

    with pytest.raises(ValueError, match="unsupported_limra_object_category"):
        limra.build_limra_object_key(
            owner_user_id="analyst@example.com",
            category="secrets",
        )


@pytest.mark.asyncio
async def test_s3_object_storage_put_uses_server_key_bucket_and_metadata():
    s3 = FakeS3Client()
    storage = limra.S3LimraObjectStorage(
        bucket="limra-artifacts",
        endpoint_url="http://minio:9000",
        access_key_id="limra_minio",
        secret_access_key="replace-with-local-minio-password",
        s3_client=s3,
    )
    object_key = limra.build_limra_object_key(
        owner_user_id="user-a",
        category="archives",
        task_id="task-a",
        filename="archive.zip",
        object_id="archive-a",
    )

    stored = await storage.put_object(
        object_key=object_key,
        data=b"archive-bytes",
        content_type="application/zip",
        metadata={"task_id": "task-a", "none": None, "unsafe key": "value"},
    )

    assert stored.object_key == object_key
    assert stored.bucket == "limra-artifacts"
    assert stored.size_bytes == len(b"archive-bytes")
    assert stored.sha256
    assert stored.metadata == {"task_id": "task-a", "unsafe-key": "value"}
    assert s3.put_calls == [
        {
            "Bucket": "limra-artifacts",
            "Key": object_key,
            "Body": b"archive-bytes",
            "ContentType": "application/zip",
            "Metadata": {"task_id": "task-a", "unsafe-key": "value"},
        }
    ]


@pytest.mark.asyncio
async def test_upload_route_rejects_object_key_aliases_on_actual_http_surface():
    app, _repo = _limra_asgi_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        for alias in sorted(limra.OBJECT_KEY_FORBIDDEN_FIELDS):
            query_response = await client.post(
                "/api/limra/uploads",
                params={alias: "users/user-a/uploads/evil.pdf"},
                files={"file": ("evidence.txt", b"evidence", "text/plain")},
            )
            assert query_response.status_code == 400, alias
            assert query_response.json()["detail"] == "object_key_server_generated"

            form_response = await client.post(
                "/api/limra/uploads",
                data={alias: "users/user-a/uploads/evil.pdf"},
                files={"file": ("evidence.txt", b"evidence", "text/plain")},
            )
            assert form_response.status_code == 400, alias
            assert form_response.json()["detail"] == "object_key_server_generated"

        empty_query_response = await client.post(
            "/api/limra/uploads",
            params={"object_key": ""},
            files={"file": ("evidence.txt", b"evidence", "text/plain")},
        )
        assert empty_query_response.status_code == 400
        assert empty_query_response.json()["detail"] == "object_key_server_generated"

        duplicate_query_response = await client.post(
            "/api/limra/uploads?object_key=users/user-a/uploads/evil.pdf&object_key=",
            files={"file": ("evidence.txt", b"evidence", "text/plain")},
        )
        assert duplicate_query_response.status_code == 400
        assert duplicate_query_response.json()["detail"] == "object_key_server_generated"

        empty_form_response = await client.post(
            "/api/limra/uploads",
            data={"object_key": ""},
            files={"file": ("evidence.txt", b"evidence", "text/plain")},
        )
        assert empty_form_response.status_code == 400
        assert empty_form_response.json()["detail"] == "object_key_server_generated"


@pytest.mark.asyncio
async def test_pdf_route_rejects_object_key_aliases_on_actual_http_surface():
    app, repo = _limra_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="report",
        scenario=None,
        runner_task_id="runner-task-a",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        for alias in sorted(limra.OBJECT_KEY_FORBIDDEN_FIELDS):
            query_response = await client.post(
                "/api/limra/tasks/task-a/reports/pdf",
                params={alias: "users/user-a/reports/evil.pdf"},
                json={},
            )
            assert query_response.status_code == 400, alias
            assert query_response.json()["detail"] == "object_key_server_generated"

            json_response = await client.post(
                "/api/limra/tasks/task-a/reports/pdf",
                json={alias: "users/user-a/reports/evil.pdf"},
            )
            assert json_response.status_code == 400, alias
            assert json_response.json()["detail"] == "object_key_server_generated"

        empty_query_response = await client.post(
            "/api/limra/tasks/task-a/reports/pdf",
            params={"object_key": ""},
            json={},
        )
        assert empty_query_response.status_code == 400
        assert empty_query_response.json()["detail"] == "object_key_server_generated"

        duplicate_query_response = await client.post(
            "/api/limra/tasks/task-a/reports/pdf?object_key=users/user-a/reports/evil.pdf&object_key=",
            json={},
        )
        assert duplicate_query_response.status_code == 400
        assert duplicate_query_response.json()["detail"] == "object_key_server_generated"

        empty_json_response = await client.post(
            "/api/limra/tasks/task-a/reports/pdf",
            json={"object_key": ""},
        )
        assert empty_json_response.status_code == 400
        assert empty_json_response.json()["detail"] == "object_key_server_generated"


def test_postgres_repository_sql_targets_limra_task_and_artifact_tables():
    sql = limra.PostgresLimraTaskRepository.sql_contract().lower()

    for table in (
        "limra_research_tasks",
        "limra_artifact_events",
        "limra_evidence_items",
        "limra_entities",
        "limra_entity_relations",
        "limra_timeline_events",
        "limra_generated_reports",
    ):
        assert table in sql

    for artifact_type in (
        "evidence",
        "entity",
        "relation",
        "timeline_event",
        "map_feature",
        "verification",
        "report_section",
    ):
        assert artifact_type in limra.ARTIFACT_BUCKETS

    assert "select artifact_type, payload" in sql
    assert "on conflict" in sql
    assert "on conflict (task_id, artifact_type, local_artifact_id)" in sql
    assert "on conflict (task_id, evidence_id)" in sql
    assert "on conflict (task_id, entity_id)" in sql
    assert "on conflict (task_id, relation_id)" in sql
    assert "on conflict (task_id, timeline_event_id)" in sql
    assert "on conflict (task_id, report_id)" in sql
    assert "on conflict (artifact_id)" not in sql
    assert "on conflict (evidence_id)" not in sql
    assert "cast(:published_at as timestamptz)" in sql
    assert "cast(:event_time as timestamptz)" in sql
    assert "cast(:event_time_end as timestamptz)" in sql
    assert "st_geomfromgeojson(:geometry_geojson)" in sql
    assert "st_geomfromtext(:geometry_wkt)" in sql
    assert "archive_object_key" in sql
    assert "archive_zip_sha256" in sql


def test_postgres_repository_preserves_task_local_artifact_refs_by_task():
    engine = FakeLimraPostgresEngine()
    repo = limra.PostgresLimraTaskRepository(
        "postgresql://limra:test@postgres:5432/limra",
        engine_factory=lambda _url: engine,
    )
    for task_id, owner in (("task-a", "user-a"), ("task-b", "user-b")):
        repo.create_task(
            task_id=task_id,
            owner_user_id=owner,
            query=f"{task_id} query",
            scenario=None,
            runner_task_id=f"runner-{task_id}",
        )

    for task_id, suffix in (("task-a", "A"), ("task-b", "B")):
        repo.record_artifact(
            task_id,
            "evidence",
            {"evidence_id": "EVID-001", "title": f"Evidence {suffix}"},
        )
        repo.record_artifact(
            task_id,
            "entity",
            {
                "entity_id": "ENT-001",
                "entity_type": "country",
                "display_name": f"Entity {suffix}",
            },
        )
        repo.record_artifact(
            task_id,
            "relation",
            {"relation_id": "REL-001", "relation_type": "mentions"},
        )
        repo.record_artifact(
            task_id,
            "report_section",
            {"section_id": "REPORT-001", "markdown": f"Report {suffix}"},
        )

    task_a_artifacts = repo.get_artifacts("task-a")
    task_b_artifacts = repo.get_artifacts("task-b")

    assert task_a_artifacts["evidence"][0]["title"] == "Evidence A"
    assert task_b_artifacts["evidence"][0]["title"] == "Evidence B"
    assert task_a_artifacts["entities"][0]["display_name"] == "Entity A"
    assert task_b_artifacts["entities"][0]["display_name"] == "Entity B"
    assert task_a_artifacts["report_sections"][0]["markdown"] == "Report A"
    assert task_b_artifacts["report_sections"][0]["markdown"] == "Report B"
    assert ("task-a", "evidence", "EVID-001") in engine.artifact_events
    assert ("task-b", "evidence", "EVID-001") in engine.artifact_events


def test_postgres_repository_artifact_params_include_temporal_and_geometry_fields():
    engine = FakeLimraPostgresEngine()
    repo = limra.PostgresLimraTaskRepository(
        "postgresql://limra:test@postgres:5432/limra",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-geo",
        owner_user_id="user-a",
        query="geo query",
        scenario=None,
        runner_task_id="runner-geo",
    )
    repo.record_artifact(
        "task-geo",
        "evidence",
        {
            "evidence_id": "EVID-001",
            "title": "dated source",
            "published_at": "2026-01-02T03:04:05Z",
        },
    )
    repo.record_artifact(
        "task-geo",
        "timeline_event",
        {
            "event_id": "TIME-001",
            "title": "port disruption",
            "event_time": "2026-02-01T00:00:00Z",
            "event_time_end": "2026-02-02T00:00:00Z",
            "geometry": {"type": "Point", "coordinates": [32.55, 29.97]},
        },
    )
    repo.record_artifact(
        "task-geo",
        "map_feature",
        {
            "feature_id": "MAP-001",
            "title": "shipping lane",
            "geometry": "LINESTRING(32.5 29.9, 33.0 30.1)",
        },
    )
    repo.record_artifact(
        "task-geo",
        "entity",
        {
            "entity_id": "ENT-001",
            "entity_type": "location",
            "display_name": "Suez Canal",
            "lat": 29.97,
            "lon": 32.55,
        },
    )

    evidence_params = engine.typed_inserts["limra_evidence_items"][0]
    timeline_params = engine.typed_inserts["limra_timeline_events"][0]
    map_params = engine.typed_inserts["limra_timeline_events"][1]
    entity_params = engine.typed_inserts["limra_entities"][0]

    assert evidence_params["published_at"] == "2026-01-02T03:04:05Z"
    assert timeline_params["event_time"] == "2026-02-01T00:00:00Z"
    assert timeline_params["event_time_end"] == "2026-02-02T00:00:00Z"
    assert json.loads(timeline_params["geometry_geojson"]) == {
        "type": "Point",
        "coordinates": [32.55, 29.97],
    }
    assert map_params["geometry_wkt"] == "LINESTRING(32.5 29.9, 33.0 30.1)"
    assert json.loads(entity_params["geometry_geojson"]) == {
        "type": "Point",
        "coordinates": [32.55, 29.97],
    }


@pytest.mark.asyncio
async def test_event_proxy_streams_runner_events_and_populates_artifacts():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    research = FakeResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "status",
                "payload": {"status": "running"},
            },
            {
                "task_id": "runner-task-a",
                "type": "evidence_collected",
                "payload": {
                    "title": "Export control notice",
                    "summary": "Policy update",
                    "source_url": "https://example.test/source",
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "entity_extracted",
                "payload": {
                    "entity_id": "ENT-001",
                    "name": "United States",
                    "entity_type": "country",
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "record_research_artifact",
                "payload": {
                    "artifact_type": "report_section",
                    "payload": {
                        "title": "Assessment",
                        "markdown": "Finding references [EVID-001]",
                    },
                    "evidence_refs": ["EVID-001"],
                    "confidence": 0.8,
                    "notes": "draft",
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "relation_extracted",
                "payload": "not-a-dict",
            },
        ]
    )

    created = await limra.create_research_task(
        {"query": "semiconductor export controls"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limra.InMemoryLimraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert [event["type"] for event in events] == [
        "status",
        "evidence_collected",
        "entity_extracted",
        "record_research_artifact",
        "relation_extracted",
        "artifact_warning",
        "status",
    ]
    assert all(event["task_id"] == task_id for event in events)
    assert events[-1]["payload"]["status"] == "completed"
    assert events[-1]["payload"]["status_source"] == "runner"
    _assert_no_browser_leak(events)

    task = repo.get_task(task_id)
    assert task.status == "completed"
    assert task.archive_status == "ready"
    assert research.stream_calls[0]["task"].runner_task_id == "runner-task-a"

    artifacts = await limra.get_task_artifacts(task_id, user=user, repo=repo)
    assert artifacts["evidence"][0]["evidence_id"] == "EVID-001"
    assert artifacts["evidence"][0]["title"] == "Export control notice"
    assert artifacts["entities"][0]["entity_id"] == "ENT-001"
    assert artifacts["report_sections"][0]["evidence_refs"] == ["EVID-001"]
    assert artifacts["report_sections"][0]["confidence"] == 0.8
    assert artifacts["relations"] == []
    _assert_no_browser_leak(artifacts)


@pytest.mark.asyncio
async def test_event_proxy_records_runtime_state_to_redis():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limra.RedisLimraRuntimeState(
        redis,
        key_prefix="test:limra:runtime",
        ttl_seconds=120,
    )
    research = FakeResearchClient(
        events=[
            {
                "type": "status",
                "payload": {"status": "running", "archive_status": "pending"},
            },
            {
                "type": "evidence_collected",
                "payload": {"title": "Runtime source"},
            },
            {
                "type": "status",
                "payload": {"status": "completed", "archive_status": "ready"},
            },
        ]
    )

    created = await limra.create_research_task(
        {"query": "redis runtime state"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    key = runtime_state.task_key(task_id)
    runtime_hash = redis.hashes[key]
    assert events[-1]["payload"]["status"] == "completed"
    assert json.loads(runtime_hash["owner_user_id"]) == "user-a"
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_completed"
    assert json.loads(runtime_hash["last_event_type"]) == "status"
    assert key in {call["key"] for call in redis.hset_calls}
    assert redis.expire_calls[-1] == {"key": key, "seconds": 120}


@pytest.mark.asyncio
async def test_event_proxy_records_nested_terminal_status_to_redis():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limra.RedisLimraRuntimeState(redis, key_prefix="test:limra")
    research = FakeResearchClient(
        events=[
            {
                "type": "status",
                "payload": {
                    "data": {"status": "completed", "archive_status": "ready"}
                },
            }
        ]
    )

    created = await limra.create_research_task(
        {"query": "nested terminal status"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_completed"


@pytest.mark.asyncio
async def test_event_proxy_records_authoritative_terminal_status_to_redis():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limra.RedisLimraRuntimeState(redis, key_prefix="test:limra")
    research = FakeResearchClient(
        events=[],
        status_payload={
            "task_id": "runner-task-a",
            "status": "completed",
            "archive_status": "ready",
        },
    )

    created = await limra.create_research_task(
        {"query": "authoritative terminal status"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["payload"]["status_source"] == "runner"
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_completed"
    assert len(research.status_calls) == 1


@pytest.mark.asyncio
async def test_terminal_task_reattach_records_terminal_runtime_state_to_redis():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limra.RedisLimraRuntimeState(redis, key_prefix="test:limra")
    research = FakeResearchClient(
        stream_exception=AssertionError("terminal reattach must not stream")
    )
    created = await limra.create_research_task(
        {"query": "already terminal"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    repo.update_task(task_id, status="completed", archive_status="ready")

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["payload"]["terminal"] is True
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "terminal_reattach"
    assert research.stream_calls == []


@pytest.mark.asyncio
async def test_terminal_reattach_does_not_close_foreign_active_runtime_lease():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limra.RedisLimraRuntimeState(redis, key_prefix="test:limra")
    research = FakeResearchClient(
        stream_exception=AssertionError("terminal reattach must not stream")
    )
    created = await limra.create_research_task(
        {"query": "terminal while active stream still owns lease"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    assert await runtime_state.try_open_stream(
        task_id,
        owner_user_id="user-a",
        stream_id="active-stream",
        fields={"status": "running", "archive_status": "pending"},
    )
    repo.update_task(task_id, status="completed", archive_status="ready")

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["payload"]["terminal"] is True
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["status"]) == "completed"
    assert json.loads(runtime_hash["archive_status"]) == "ready"
    assert json.loads(runtime_hash["stream_state"]) == "open"
    assert json.loads(runtime_hash["stream_id"]) == "active-stream"
    assert not await runtime_state.try_open_stream(
        task_id,
        owner_user_id="user-a",
        stream_id="new-stream",
        fields={"status": "running", "archive_status": "pending"},
    )
    assert research.stream_calls == []


@pytest.mark.asyncio
async def test_event_proxy_duplicate_active_stream_uses_runtime_lease_without_runner_call():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limra.RedisLimraRuntimeState(
        redis,
        key_prefix="test:limra",
        ttl_seconds=240,
    )
    research = FakeResearchClient(
        stream_exception=AssertionError("duplicate stream must not call runner")
    )
    created = await limra.create_research_task(
        {"query": "duplicate stream"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    assert await runtime_state.try_open_stream(
        task_id,
        owner_user_id="user-a",
        stream_id="active-stream",
        fields={"status": "running", "archive_status": "pending"},
    )

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "running",
                "archive_status": "pending",
                "stream_state": "open",
                "status_source": "limra_runtime_state",
                "reason": "stream_already_open",
            },
        }
    ]
    assert research.stream_calls == []
    assert json.loads(runtime_hash["stream_state"]) == "open"
    assert json.loads(runtime_hash["stream_id"]) == "active-stream"
    assert {"key": runtime_state.task_key(task_id), "seconds": 240} in redis.expire_calls


@pytest.mark.asyncio
async def test_event_proxy_cancellation_closes_matching_runtime_stream():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limra.RedisLimraRuntimeState(redis, key_prefix="test:limra")
    research = FakeResearchClient(stream_exception=asyncio.CancelledError())
    created = await limra.create_research_task(
        {"query": "cancelled redis stream"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events == []
    assert json.loads(runtime_hash["status"]) == "cancelled"
    assert json.loads(runtime_hash["archive_status"]) == "failed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "event_stream_cancelled"


@pytest.mark.asyncio
async def test_event_proxy_http_exception_records_failed_terminal_runtime_state():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limra.RedisLimraRuntimeState(redis, key_prefix="test:limra")
    research = FakeResearchClient(
        stream_exception=HTTPException(status_code=503, detail="runner_unavailable")
    )
    created = await limra.create_research_task(
        {"query": "http exception redis state"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["error"] == "runner_unavailable"
    assert json.loads(runtime_hash["status"]) == "failed"
    assert json.loads(runtime_hash["archive_status"]) == "failed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["error"]) == "runner_unavailable"
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "http_exception"


@pytest.mark.asyncio
async def test_event_proxy_generic_exception_records_failed_terminal_runtime_state():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    redis = FakeRedisClient()
    runtime_state = limra.RedisLimraRuntimeState(redis, key_prefix="test:limra")
    research = FakeResearchClient(stream_exception=RuntimeError("private failure"))
    created = await limra.create_research_task(
        {"query": "generic exception redis state"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    runtime_hash = redis.hashes[runtime_state.task_key(task_id)]
    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["error"] == "limra_event_proxy_failed"
    assert json.loads(runtime_hash["status"]) == "failed"
    assert json.loads(runtime_hash["archive_status"]) == "failed"
    assert json.loads(runtime_hash["terminal"]) is True
    assert json.loads(runtime_hash["error"]) == "limra_event_proxy_failed"
    assert json.loads(runtime_hash["stream_state"]) == "closed"
    assert json.loads(runtime_hash["stream_close_reason"]) == "limra_event_proxy_failed"


@pytest.mark.asyncio
async def test_completed_task_event_reattach_is_terminal_and_does_not_call_runner():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    research = FakeResearchClient(
        stream_exception=AssertionError("final reattach must not call runner stream")
    )
    created = await limra.create_research_task(
        {"query": "completed query"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    repo.update_task(task_id, status="completed", archive_status="ready")

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limra.InMemoryLimraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events == [
        {
            "task_id": task_id,
            "type": "status",
            "payload": {
                "status": "completed",
                "archive_status": "ready",
                "terminal": True,
            },
        }
    ]
    assert research.stream_calls == []
    assert research.status_calls == []
    assert repo.get_task(task_id).status == "completed"
    assert repo.get_task(task_id).archive_status == "ready"


@pytest.mark.asyncio
async def test_eventsource_reconnect_after_completion_does_not_regress_task():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    research = FakeResearchClient(
        events=[
            {
                "type": "status",
                "payload": {"status": "completed", "archive_status": "ready"},
            }
        ],
        stream_exception=None,
    )
    created = await limra.create_research_task(
        {"query": "finish once"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    first_response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limra.InMemoryLimraRuntimeState(),
    )
    first_events = _parse_sse_chunks([chunk async for chunk in first_response.body_iterator])

    second_response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limra.InMemoryLimraRuntimeState(),
    )
    second_events = _parse_sse_chunks([chunk async for chunk in second_response.body_iterator])

    assert first_events[-1]["payload"]["status"] == "completed"
    assert second_events[-1]["payload"]["status"] == "completed"
    assert second_events[-1]["payload"]["terminal"] is True
    assert len(research.stream_calls) == 1
    assert repo.get_task(task_id).status == "completed"
    assert repo.get_task(task_id).archive_status == "ready"


@pytest.mark.asyncio
async def test_runner_stream_conflict_uses_authoritative_status_instead_of_failing():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    research = FakeResearchClient(
        stream_exception=limra.RunnerStreamConflict("task_already_finished"),
        status_payload={
            "task_id": "runner-task-a",
            "status": "completed",
            "archive_status": "ready",
        },
    )
    created = await limra.create_research_task(
        {"query": "reattach via runner conflict"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limra.InMemoryLimraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events[-1]["type"] == "status"
    assert events[-1]["payload"]["status"] == "completed"
    assert events[-1]["payload"]["archive_status"] == "ready"
    assert events[-1]["payload"]["reason"] == "task_already_finished"
    assert repo.get_task(task_id).status == "completed"
    assert repo.get_task(task_id).archive_status == "ready"
    assert repo.get_task(task_id).error is None


@pytest.mark.asyncio
async def test_runner_stream_running_conflict_stays_running_without_failed_regression():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    research = FakeResearchClient(
        stream_exception=limra.RunnerStreamConflict("task_already_running"),
        status_payload={
            "task_id": "runner-task-a",
            "status": "running",
            "archive_status": "pending",
        },
    )
    created = await limra.create_research_task(
        {"query": "active duplicate stream"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limra.InMemoryLimraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events[-1]["payload"]["status"] == "running"
    assert events[-1]["payload"]["archive_status"] == "pending"
    assert events[-1]["payload"]["reason"] == "task_already_running"
    assert repo.get_task(task_id).status == "running"
    assert repo.get_task(task_id).archive_status == "pending"
    assert repo.get_task(task_id).error is None


@pytest.mark.asyncio
async def test_event_proxy_cancellation_does_not_mark_task_completed():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")
    research = FakeResearchClient(stream_exception=asyncio.CancelledError())
    created = await limra.create_research_task(
        {"query": "cancelled stream"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]

    response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=limra.InMemoryLimraRuntimeState(),
    )
    events = _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    assert events == []
    assert repo.get_task(task_id).status == "cancelled"
    assert repo.get_task(task_id).archive_status == "failed"
    assert repo.get_task(task_id).error == "event_stream_cancelled"


@pytest.mark.asyncio
async def test_event_proxy_keeps_artifacts_user_scoped():
    repo = limra.InMemoryLimraTaskRepository()
    user_a = limra.LimraUser("user-a")
    user_b = limra.LimraUser("user-b")
    research = FakeResearchClient(
        events=[
            {
                "type": "evidence_collected",
                "payload": {"evidence_id": "EVID-777", "title": "private"},
            }
        ]
    )

    created = await limra.create_research_task(
        {"query": "private query"},
        request=None,
        user=user_a,
        repo=repo,
        research_client=research,
    )
    task_id = created["task_id"]
    response = await limra.get_task_events(
        task_id,
        user=user_a,
        repo=repo,
        research_client=research,
        runtime_state=limra.InMemoryLimraRuntimeState(),
    )
    _parse_sse_chunks([chunk async for chunk in response.body_iterator])

    with pytest.raises(HTTPException) as forbidden:
        await limra.get_task_artifacts(task_id, user=user_b, repo=repo)
    assert forbidden.value.status_code == 404


def test_runner_research_client_uses_server_side_headers(monkeypatch):
    monkeypatch.setenv("LIMRA_RUNNER_INTERNAL_URL", "http://internal-runner")
    monkeypatch.setenv("LIMRA_RUNNER_SERVICE_TOKEN", "server-only-token")

    client = limra.RunnerResearchClient()
    assert client.runner_url == "http://internal-runner"
    assert client.service_token == "server-only-token"


def test_limra_router_defines_required_browser_facing_paths():
    route_contract = {
        ("/research", "POST"),
        ("/tasks/{task_id}", "GET"),
        ("/tasks/{task_id}/events", "GET"),
        ("/tasks/{task_id}/artifacts", "GET"),
        ("/tasks/{task_id}/archive.zip", "GET"),
        ("/uploads", "POST"),
        ("/tasks/{task_id}/reports/pdf", "POST"),
        ("/admin/tasks/{task_id}", "GET"),
        ("/admin/tasks/{task_id}/archive.zip", "GET"),
    }
    actual = {
        (route.path, method)
        for route in limra.router.routes
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST"}
    }

    assert route_contract <= actual
    for path, _method in actual:
        assert "/mirothinker/" not in path


def _archive_zip(*, extra_member: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("metadata.json", "{}")
        archive.writestr("report.html", "<!doctype html><main></main>")
        archive.writestr("report.md", "# report")
        archive.writestr("trace.json", "{}")
        if extra_member:
            archive.writestr(".env", "RUNNER_SERVICE_TOKEN=secret")
    return buffer.getvalue()


def _parse_sse_chunks(chunks):
    text = b"".join(
        chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
        for chunk in chunks
    ).decode("utf-8")
    return [
        json.loads(line.removeprefix("data: "))
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


def _assert_no_browser_leak(payload):
    text = str(payload)
    for forbidden in limra.FORBIDDEN_BROWSER_SUBSTRINGS:
        assert forbidden not in text


def _limra_asgi_app():
    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
    app = FastAPI()
    app.include_router(limra.router, prefix="/api/limra")

    async def current_user_override():
        return limra.LimraUser("user-a")

    async def task_repository_override():
        return repo

    async def object_storage_override():
        return storage

    app.dependency_overrides[limra.get_current_limra_user] = current_user_override
    app.dependency_overrides[limra.get_task_repository] = task_repository_override
    app.dependency_overrides[limra.get_object_storage] = object_storage_override
    return app, repo


class FakeRedisClient:
    def __init__(self):
        self.hashes = {}
        self.hset_calls = []
        self.expire_calls = []
        self.eval_calls = []

    async def hset(self, key, *, mapping):
        self.hset_calls.append({"key": key, "mapping": dict(mapping)})
        self.hashes.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def expire(self, key, seconds):
        self.expire_calls.append({"key": key, "seconds": seconds})
        return True

    async def eval(self, script, numkeys, *keys_and_args):
        self.eval_calls.append(
            {
                "script": script,
                "numkeys": numkeys,
                "keys_and_args": keys_and_args,
            }
        )
        assert numkeys == 1
        key = keys_and_args[0]
        ttl_seconds = int(keys_and_args[1])
        if "limra_try_open_stream" in script:
            runtime_hash = self.hashes.setdefault(key, {})
            if runtime_hash.get("stream_state") == json.dumps("open"):
                return 0
            mapping = _pairs_to_mapping(keys_and_args[2:])
            runtime_hash.update(mapping)
            await self.expire(key, ttl_seconds)
            return 1
        if "limra_close_stream" in script:
            runtime_hash = self.hashes.setdefault(key, {})
            expected_stream_id = keys_and_args[2]
            current_stream_id = runtime_hash.get("stream_id")
            if current_stream_id and current_stream_id != expected_stream_id:
                return 0
            mapping = _pairs_to_mapping(keys_and_args[3:])
            runtime_hash.update(mapping)
            await self.expire(key, ttl_seconds)
            return 1
        raise AssertionError(f"unexpected redis eval script: {script}")


class FakeS3Client:
    def __init__(self):
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(dict(kwargs))
        return {"ETag": '"fake"'}


def _pairs_to_mapping(values):
    assert len(values) % 2 == 0
    return {values[index]: values[index + 1] for index in range(0, len(values), 2)}


class FakeLimraPostgresEngine:
    def __init__(self):
        self.tasks = {}
        self.artifact_events = {}
        self.typed_inserts = {
            "limra_evidence_items": [],
            "limra_entities": [],
            "limra_entity_relations": [],
            "limra_timeline_events": [],
            "limra_generated_reports": [],
        }

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params):
        sql = str(statement).lower()
        if "insert into limra_research_tasks" in sql:
            row = {
                "task_id": params["task_id"],
                "owner_user_id": params["owner_user_id"],
                "query": params["query"],
                "status": "queued",
                "archive_status": "pending",
                "runner_task_id": params["runner_task_id"],
                "archive_object_key": None,
                "archive_zip_sha256": None,
                "scenario": params["scenario"],
                "error": None,
                "model_summary": {},
            }
            self.tasks[params["task_id"]] = row
            return FakeLimraPostgresResult([row])

        if "select owner_user_id from limra_research_tasks" in sql:
            row = self.tasks.get(params["task_id"])
            return FakeLimraPostgresResult(
                [{"owner_user_id": row["owner_user_id"]}] if row else []
            )

        if "from limra_research_tasks" in sql:
            row = self.tasks.get(params["task_id"])
            if row and params.get("owner_user_id") and row["owner_user_id"] != params["owner_user_id"]:
                row = None
            return FakeLimraPostgresResult([row] if row else [])

        if "insert into limra_artifact_events" in sql:
            key = (
                params["task_id"],
                params["artifact_type"],
                params["local_artifact_id"],
            )
            self.artifact_events[key] = {
                "artifact_type": params["artifact_type"],
                "payload": params["payload"],
            }
            return FakeLimraPostgresResult([])

        if "from limra_artifact_events" in sql:
            rows = [
                row
                for (task_id, _artifact_type, _local_id), row in self.artifact_events.items()
                if task_id == params["task_id"]
            ]
            return FakeLimraPostgresResult(rows)

        for table in self.typed_inserts:
            if f"insert into {table}" in sql:
                self.typed_inserts[table].append(dict(params))
                return FakeLimraPostgresResult([])

        return FakeLimraPostgresResult([])


class FakeLimraPostgresResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)
