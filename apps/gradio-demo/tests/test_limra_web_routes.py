import io
import json
import sys
import zipfile
import asyncio
import types
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
async def test_demo_scenarios_are_browser_safe_and_artifact_oriented():
    payload = await limra.list_demo_scenarios(user=limra.LimraUser("user-a"))

    scenario_ids = {scenario["id"] for scenario in payload["scenarios"]}
    assert scenario_ids == {
        "sanctions_export_controls",
        "geopolitical_risk_assessment",
        "critical_minerals_competition",
    }
    assert payload["count"] == 3
    _assert_no_browser_leak(payload)

    payload_text = json.dumps(payload)
    assert "Artifact requirements" not in payload_text
    assert "record_research_artifact" not in payload_text
    assert "runner" not in payload_text.lower()

    for scenario_id in scenario_ids:
        runner_query = limra._runner_query_for_scenario("base query", scenario_id)
        assert "base query" in runner_query
        assert "record_research_artifact" in runner_query
        assert "EVID-001" in runner_query
        assert "map_feature" in runner_query
        assert "[EVID-001]" in runner_query
        assert "report_section" in runner_query

    assert limra._runner_query_for_scenario("base query", "legacy-scenario") == "base query"


@pytest.mark.asyncio
async def test_create_research_with_known_demo_scenario_enriches_runner_query_only():
    repo = limra.InMemoryLimraTaskRepository()
    research = FakeResearchClient()
    user = limra.LimraUser("user-a")
    scenario_id = "critical_minerals_competition"

    payload = await limra.create_research_task(
        {"query": "Analyze nickel supply chain risk", "scenario": scenario_id},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
    )

    assert payload["scenario"] == scenario_id
    assert payload["query"] == "Analyze nickel supply chain risk"
    _assert_no_browser_leak(payload)
    assert "record_research_artifact" not in json.dumps(payload)

    assert len(research.create_calls) == 1
    runner_call = research.create_calls[0]
    assert runner_call["scenario"] == scenario_id
    assert runner_call["user"] == user
    assert runner_call["query"] != payload["query"]
    assert "Critical minerals competition" in runner_call["query"]
    assert "Analyze nickel supply chain risk" in runner_call["query"]
    assert "record_research_artifact" in runner_call["query"]
    assert "EVID-001" in runner_call["query"]
    assert "map_feature" in runner_call["query"]
    assert "[EVID-001]" in runner_call["query"]

    task = repo.get_task(payload["task_id"])
    assert task.query == "Analyze nickel supply chain risk"
    assert task.scenario == scenario_id


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
    storage = limra.InMemoryLimraObjectStorage()
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
            object_storage=storage,
        )
    assert forbidden_archive.value.status_code == 404

    response = await limra.download_task_archive(
        task_id,
        user=user_a,
        repo=repo,
        object_storage=storage,
    )

    assert response.media_type == "application/zip"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert zipfile.ZipFile(io.BytesIO(response.body)).namelist() == [
        "metadata.json",
        "report.html",
        "report.md",
        "trace.json",
    ]
    archive_key = repo.tasks[task_id].archive_object_key
    assert archive_key in storage.objects
    assert "/tasks/" in archive_key
    assert "/archives/" in archive_key
    assert (
        repo.tasks[task_id].archive_zip_sha256
        == storage.objects[archive_key]["sha256"]
    )
    assert storage.objects[archive_key]["content_type"] == "application/zip"
    assert storage.objects[archive_key]["metadata"]["task_id"] == task_id
    assert storage.objects[archive_key]["metadata"]["owner_user_id"] == "user-a"
    second_response = await limra.download_task_archive(
        task_id,
        user=user_a,
        repo=repo,
        object_storage=storage,
    )
    assert second_response.body == response.body
    assert len(storage.objects) == 1
    _assert_no_browser_leak(response.body.decode("latin1"))


@pytest.mark.asyncio
async def test_archive_proxy_scrubs_allowed_text_members_before_download():
    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
    user = limra.LimraUser("user-a")
    task = repo.create_task(
        task_id="task-secret-archive",
        owner_user_id=user.id,
        query="archive secrets",
        scenario=None,
        runner_task_id="runner-secret-archive",
    )
    task.archive_status = "ready"
    repo.record_artifact(
        task.task_id,
        "evidence",
        {
            "evidence_id": "EVID-001",
            "summary": "Authorization: Bearer runner-token-123456",
            "url": "https://search.test?q=x&token=archive-token-123456",
        },
    )
    repo.record_generated_report(
        report_id="report-secret",
        task_id=task.task_id,
        report_type="final",
        markdown=(
            "# report\n"
            "OPENAI_API_KEY=sk-archiveopenai123456\n"
            "RUNNER_SERVICE_TOKEN=archive-runner-token-123456\n"
            "https://api.test/resource?api_key=archive-query-secret-123456"
        ),
        html=None,
        pdf_object_key=None,
        evidence_refs=["EVID-001"],
        creator_user_id=user.id,
        metadata={
            "cookie": "open_webui_session=session-secret-123456",
            "deepseek": "DEEPSEEK_API_KEY=sk-tracedeepseek123456",
        },
    )

    response = await limra.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )

    assert response.media_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.body)) as scrubbed_archive:
        assert scrubbed_archive.namelist() == [
            "metadata.json",
            "report.html",
            "report.md",
            "trace.json",
        ]
        combined = "\n".join(
            scrubbed_archive.read(member).decode("utf-8")
            for member in scrubbed_archive.namelist()
        )
        metadata = json.loads(scrubbed_archive.read("metadata.json"))
        trace = json.loads(scrubbed_archive.read("trace.json"))

    assert limra.LIMRA_SECRET_REDACTION in combined
    _assert_no_raw_secret(combined)
    assert metadata["reports"][0]["report_id"] == "report-secret"
    assert trace["artifacts"]["evidence"][0]["evidence_id"] == "EVID-001"
    assert task.archive_object_key in storage.objects
    _assert_no_raw_secret(storage.objects[task.archive_object_key]["metadata"])


@pytest.mark.asyncio
async def test_archive_download_regenerates_after_task_scoped_writes():
    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
    user = limra.LimraUser("user-a")
    task = repo.create_task(
        task_id="task-fresh-archive",
        owner_user_id=user.id,
        query="fresh archive",
        scenario=None,
        runner_task_id="runner-fresh-archive",
    )
    task.archive_status = "ready"

    first_response = await limra.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    first_sha = task.archive_zip_sha256
    first_members = _archive_member_texts(first_response.body)
    assert "Fresh report" not in first_members["report.md"]

    repo.record_generated_report(
        report_id="report-fresh",
        task_id=task.task_id,
        report_type="final",
        markdown="Fresh report [EVID-FRESH]",
        html=None,
        pdf_object_key=None,
        evidence_refs=["EVID-FRESH"],
        creator_user_id=user.id,
        metadata={},
    )
    assert task.archive_object_key is None
    assert task.archive_zip_sha256 is None

    second_response = await limra.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    second_sha = task.archive_zip_sha256
    second_members = _archive_member_texts(second_response.body)
    assert second_sha != first_sha
    assert "Fresh report [EVID-FRESH]" in second_members["report.md"]

    repo.record_uploaded_document(
        document_id="doc-fresh",
        owner_user_id=user.id,
        task_id=task.task_id,
        original_filename="brief.txt",
        content_type="text/plain",
        byte_size=5,
        minio_bucket="limra-artifacts",
        object_key="limra/users/hash/tasks/task-fresh-archive/uploads/doc-fresh.txt",
        extracted_text="brief",
        language=None,
        metadata={},
    )
    assert task.archive_object_key is None
    assert task.archive_zip_sha256 is None

    third_response = await limra.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    third_sha = task.archive_zip_sha256
    third_trace = json.loads(_archive_member_texts(third_response.body)["trace.json"])
    assert third_sha != second_sha
    assert third_trace["uploaded_documents"][0]["document_id"] == "doc-fresh"

    repo.record_artifact(
        task.task_id,
        "evidence",
        {
            "evidence_id": "EVID-NEW",
            "summary": "new evidence",
            "source_url": "https://example.test/new",
        },
    )
    assert task.archive_object_key is None
    assert task.archive_zip_sha256 is None

    fourth_response = await limra.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    fourth_trace = json.loads(_archive_member_texts(fourth_response.body)["trace.json"])
    assert fourth_trace["artifacts"]["evidence"][0]["evidence_id"] == "EVID-NEW"
    assert task.archive_object_key in storage.objects
    assert storage.objects[task.archive_object_key]["content_type"] == "application/zip"


@pytest.mark.asyncio
async def test_untasked_upload_does_not_invalidate_task_archive():
    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
    user = limra.LimraUser("user-a")
    task = repo.create_task(
        task_id="task-unlinked-upload",
        owner_user_id=user.id,
        query="unlinked upload",
        scenario=None,
        runner_task_id="runner-unlinked-upload",
    )
    task.archive_status = "ready"

    response = await limra.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    archive_key = task.archive_object_key
    archive_sha = task.archive_zip_sha256

    repo.record_uploaded_document(
        document_id="doc-unlinked",
        owner_user_id=user.id,
        task_id=None,
        original_filename="unlinked.txt",
        content_type="text/plain",
        byte_size=8,
        minio_bucket="limra-artifacts",
        object_key="limra/users/hash/uploads/doc-unlinked.txt",
        extracted_text="unlinked",
        language=None,
        metadata={},
    )

    assert task.archive_object_key == archive_key
    assert task.archive_zip_sha256 == archive_sha
    second_response = await limra.download_task_archive(
        task.task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    assert second_response.body == response.body


@pytest.mark.asyncio
async def test_admin_access_requires_explicit_admin_route():
    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
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
        object_storage=storage,
    )
    assert response.media_type == "application/zip"
    assert repo.tasks[task_id].archive_object_key in storage.objects


@pytest.mark.asyncio
async def test_archive_proxy_rejects_not_ready_and_invalid_zip_members():
    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
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
            object_storage=storage,
        )
    assert not_ready.value.status_code == 409

    repo.tasks[task_id].archive_status = "ready"
    with pytest.raises(HTTPException) as invalid_zip:
        limra.validate_archive_zip(_archive_zip(extra_member=True))
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


def test_sqlite_limra_repository_persists_task_artifacts_reports_and_uploads(tmp_path):
    database_path = tmp_path / "limra.sqlite3"
    repo = limra.SQLiteLimraTaskRepository(str(database_path))
    task = repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="query",
        scenario="scenario-a",
        runner_task_id="runner-a",
    )
    repo.update_task(task.task_id, status="completed", archive_status="ready")
    repo.record_artifact(
        task.task_id,
        "evidence",
        {
            "evidence_id": "EVID-001",
            "title": "Source",
            "summary": "Persistent evidence",
        },
    )
    repo.record_artifact_trace_event(
        task.task_id,
        {
            "type": "artifact_warning",
            "payload": {"warning": "nonfatal"},
        },
    )
    repo.record_uploaded_document(
        document_id="doc-a",
        owner_user_id="user-a",
        task_id=task.task_id,
        original_filename="memo.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket="bucket",
        object_key="limra/users/u/tasks/task-a/uploads/doc-a.txt",
        extracted_text="Persistent memo text",
        language="en",
        metadata={"sha256": "abc"},
        embedding=[1.0, 0.0],
    )
    repo.record_generated_report(
        report_id="report-a",
        task_id=task.task_id,
        report_type="final",
        markdown="## Final\n\nPersistent report",
        html=None,
        pdf_object_key="limra/users/u/tasks/task-a/reports/report-a.pdf",
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={"pdf_size_bytes": 123},
    )

    restored = limra.SQLiteLimraTaskRepository(str(database_path))
    restored_task = restored.get_user_task("task-a", "user-a")
    assert restored_task is not None
    assert restored_task.status == "completed"
    assert restored.get_artifacts("task-a")["evidence"][0]["evidence_id"] == "EVID-001"
    assert restored.get_artifact_trace_events("task-a")[-1]["type"] == "artifact_warning"
    assert restored.list_user_documents(owner_user_id="user-a", task_id="task-a")[0].document_id == "doc-a"
    assert restored.search_user_documents(
        owner_user_id="user-a",
        task_id="task-a",
        query="memo",
        limit=5,
    )[0].document.document_id == "doc-a"
    assert restored.get_user_report(
        task_id="task-a",
        report_id="report-a",
        owner_user_id="user-a",
    ).pdf_object_key.endswith("report-a.pdf")


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


@pytest.mark.asyncio
async def test_filesystem_object_storage_persists_objects_across_instances(tmp_path):
    storage = limra.FileSystemLimraObjectStorage(root_path=str(tmp_path), bucket="local")
    stored = await storage.put_object(
        object_key="limra/users/u/tasks/task-a/reports/report-a.pdf",
        data=b"%PDF-test",
        content_type="application/pdf",
        metadata={"report_id": "report-a"},
    )

    restored = limra.FileSystemLimraObjectStorage(root_path=str(tmp_path), bucket="local")
    assert stored.bucket == "local"
    assert await restored.get_object(object_key=stored.object_key) == b"%PDF-test"


@pytest.mark.asyncio
async def test_filesystem_object_storage_rejects_unsafe_object_keys(tmp_path):
    storage = limra.FileSystemLimraObjectStorage(root_path=str(tmp_path), bucket="local")
    unsafe_keys = [
        "",
        "/absolute/object.txt",
        "../outside.txt",
        "limra/users/u/../secret.txt",
        "limra/users/u/a/../../secret.txt",
        "limra/users/u/./object.txt",
        "limra/users/u//object.txt",
        "limra\\users\\u\\object.txt",
        "limra/users/u/object.metadata.json",
        "limra/users/u/object.metadata.json/nested.txt",
    ]

    for object_key in unsafe_keys:
        with pytest.raises(ValueError, match="invalid_limra_object_key"):
            await storage.put_object(
                object_key=object_key,
                data=b"unsafe",
                content_type="text/plain",
                metadata={"document_id": "doc-a"},
            )
        with pytest.raises(ValueError, match="invalid_limra_object_key"):
            await storage.get_object(object_key=object_key)

    assert [path.name for path in tmp_path.iterdir()] == []


def test_limra_upload_embedding_config_defaults_disabled_and_validates_enabled():
    config = limra.create_limra_upload_embedding_config_from_env({})
    assert config == limra.LimraUploadEmbeddingConfig(
        enabled=False,
        provider="disabled",
        model="",
        dimensions=1536,
    )

    enabled_env = {
        "LIMRA_UPLOAD_EMBEDDINGS_ENABLED": "true",
        "LIMRA_EMBEDDING_PROVIDER": "fake",
        "LIMRA_EMBEDDING_MODEL": "fake-model",
        "LIMRA_EMBEDDING_DIMENSIONS": "1536",
    }
    enabled = limra.create_limra_upload_embedding_config_from_env(enabled_env)
    assert enabled.enabled is True
    assert enabled.provider == "fake"
    assert enabled.model == "fake-model"
    assert enabled.dimensions == 1536

    with pytest.raises(RuntimeError, match="limra_upload_embedding_provider_required"):
        limra.create_limra_upload_embedding_config_from_env(
            {"LIMRA_UPLOAD_EMBEDDINGS_ENABLED": "true"}
        )
    with pytest.raises(RuntimeError, match="limra_upload_embedding_model_required"):
        limra.create_limra_upload_embedding_config_from_env(
            {
                "LIMRA_UPLOAD_EMBEDDINGS_ENABLED": "true",
                "LIMRA_EMBEDDING_PROVIDER": "fake",
            }
        )
    with pytest.raises(RuntimeError, match="limra_upload_embedding_dimensions_invalid"):
        limra.create_limra_upload_embedding_config_from_env(
            {"LIMRA_EMBEDDING_DIMENSIONS": "0"}
        )
    with pytest.raises(
        RuntimeError,
        match="limra_upload_embedding_dimensions_schema_mismatch",
    ):
        limra.create_limra_upload_embedding_config_from_env(
            {
                "LIMRA_UPLOAD_EMBEDDINGS_ENABLED": "true",
                "LIMRA_EMBEDDING_PROVIDER": "fake",
                "LIMRA_EMBEDDING_MODEL": "fake-model",
                "LIMRA_EMBEDDING_DIMENSIONS": "3",
            }
        )


def test_upload_embedding_dependencies_avoid_threadpool_for_default_route_path():
    assert asyncio.iscoroutinefunction(limra.get_upload_embedding_config)
    assert asyncio.iscoroutinefunction(limra.get_upload_embedding_provider)


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
    app, _repo, _storage = _limra_asgi_app()

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
async def test_upload_route_stores_text_original_and_document_record():
    app, repo, storage = _limra_asgi_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.post(
            "/api/limra/uploads",
            files={"file": ("evidence.txt", b"hello limra", "text/plain")},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["filename"] == "evidence.txt"
    assert payload["content_type"] == "text/plain"
    assert payload["byte_size"] == len(b"hello limra")
    assert payload["extracted_text_chars"] == len("hello limra")
    assert "object_key" not in payload
    assert "minio_object_key" not in payload

    document = repo.get_user_document(payload["document_id"], "user-a")
    assert document is not None
    assert document.owner_user_id == "user-a"
    assert document.task_id is None
    assert document.extracted_text == "hello limra"
    assert document.embedding is None
    assert document.metadata["embedding"] == {
        "enabled": False,
        "provider": "disabled",
        "model": "",
        "dimensions": 1536,
        "status": "disabled",
    }
    assert document.minio_bucket == storage.bucket
    assert document.object_key in storage.objects
    stored = storage.objects[document.object_key]
    assert stored["data"] == b"hello limra"
    assert stored["content_type"] == "text/plain"
    assert stored["metadata"]["document_id"] == document.document_id
    assert stored["metadata"]["owner_user_id"] == "user-a"
    _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_upload_route_rejects_active_or_unsupported_content_types():
    app, repo, storage = _limra_asgi_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        for filename, content_type in [
            ("active.html", "text/html; charset=utf-8"),
            ("active.svg", "image/svg+xml"),
            ("active.xml", "application/xml"),
            ("active.js", "application/javascript"),
            ("unknown.bin", "application/octet-stream"),
        ]:
            response = await client.post(
                "/api/limra/uploads",
                files={
                    "file": (
                        filename,
                        b"<script>alert(1)</script>",
                        content_type,
                    )
                },
            )
            assert response.status_code == 415, (filename, content_type)
            assert response.json()["detail"] == "unsupported_upload_type"

        markdown_response = await client.post(
            "/api/limra/uploads",
            files={
                "file": (
                    "source.md",
                    b"# inert markdown",
                    "application/octet-stream",
                )
            },
        )

    assert markdown_response.status_code == 201
    assert markdown_response.json()["content_type"] == "text/markdown"
    assert len(repo.uploaded_documents) == 1
    assert len(storage.objects) == 1
    document = next(iter(repo.uploaded_documents.values()))
    assert document.original_filename == "source.md"
    assert document.content_type == "text/markdown"


@pytest.mark.asyncio
async def test_upload_route_records_configured_embedding_with_fake_provider():
    app, repo, _storage = _limra_asgi_app()
    provider = FakeUploadEmbeddingProvider([0.25, 0.5, 0.75])

    async def embedding_config_override():
        return limra.LimraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=3,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limra.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limra.get_upload_embedding_provider] = (
        embedding_provider_override
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.post(
            "/api/limra/uploads",
            files={"file": ("embedding.txt", b"lithium graphite source", "text/plain")},
        )

    assert response.status_code == 201
    payload = response.json()
    assert "embedding" not in payload
    document = repo.get_user_document(payload["document_id"], "user-a")
    assert document is not None
    assert document.embedding == [0.25, 0.5, 0.75]
    assert provider.calls == [
        {
            "text": "lithium graphite source",
            "config": limra.LimraUploadEmbeddingConfig(
                enabled=True,
                provider="fake",
                model="fake-model",
                dimensions=3,
            ),
        }
    ]
    assert document.metadata["embedding"] == {
        "enabled": True,
        "provider": "fake",
        "model": "fake-model",
        "dimensions": 3,
        "status": "stored",
    }
    _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_upload_route_rejects_embedding_dimension_mismatch_without_writes():
    app, repo, storage = _limra_asgi_app()
    provider = FakeUploadEmbeddingProvider([0.25, 0.5])

    async def embedding_config_override():
        return limra.LimraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=3,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limra.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limra.get_upload_embedding_provider] = (
        embedding_provider_override
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.post(
            "/api/limra/uploads",
            files={"file": ("bad-vector.txt", b"dimension mismatch", "text/plain")},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "upload_embedding_dimension_mismatch"
    assert storage.objects == {}
    assert repo.list_user_documents(owner_user_id="user-a") == []


@pytest.mark.asyncio
async def test_upload_route_links_only_owned_tasks():
    app, repo, storage = _limra_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="owned task",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    repo.create_task(
        task_id="task-b",
        owner_user_id="user-b",
        query="foreign task",
        scenario=None,
        runner_task_id="runner-task-b",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        owned_response = await client.post(
            "/api/limra/uploads",
            data={"task_id": "task-a"},
            files={"file": ("owned.md", b"# owned", "text/markdown")},
        )
        forbidden_response = await client.post(
            "/api/limra/uploads",
            data={"task_id": "task-b"},
            files={"file": ("foreign.md", b"# foreign", "text/markdown")},
        )
        owned_list_response = await client.get(
            "/api/limra/uploads",
            params={"task_id": "task-a"},
        )
        foreign_list_response = await client.get(
            "/api/limra/uploads",
            params={"task_id": "task-b"},
        )

    assert owned_response.status_code == 201
    payload = owned_response.json()
    assert payload["task_id"] == "task-a"
    document = repo.get_user_document(payload["document_id"], "user-a")
    assert document is not None
    assert document.task_id == "task-a"
    assert "/tasks/task-a/uploads/" in document.object_key
    assert storage.objects[document.object_key]["metadata"]["task_id"] == "task-a"
    assert owned_list_response.status_code == 200
    assert [doc["document_id"] for doc in owned_list_response.json()["documents"]] == [
        document.document_id
    ]
    assert foreign_list_response.status_code == 404
    assert forbidden_response.status_code == 404
    assert len(storage.objects) == 1


@pytest.mark.asyncio
async def test_upload_route_rejects_conflicting_form_and_query_task_ids():
    app, repo, storage = _limra_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="owned task",
        scenario=None,
        runner_task_id="runner-task-a",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.post(
            "/api/limra/uploads",
            params={"task_id": "task-a"},
            data={"task_id": "task-other"},
            files={"file": ("conflict.txt", b"conflict", "text/plain")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "conflicting_task_id"
    assert storage.objects == {}
    assert repo.list_user_documents(owner_user_id="user-a") == []


@pytest.mark.asyncio
async def test_upload_route_stores_pdf_with_extracted_text(monkeypatch):
    app, repo, storage = _limra_asgi_app()
    monkeypatch.setattr(limra, "_extract_pdf_text", lambda data: "pdf extracted text")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.post(
            "/api/limra/uploads",
            files={"file": ("brief.pdf", b"%PDF-1.7", "application/pdf")},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["filename"] == "brief.pdf"
    assert payload["content_type"] == "application/pdf"
    assert payload["extracted_text_chars"] == len("pdf extracted text")
    document = repo.get_user_document(payload["document_id"], "user-a")
    assert document is not None
    assert document.extracted_text == "pdf extracted text"
    assert document.object_key.endswith(f"/uploads/{document.document_id}.pdf")
    assert storage.objects[document.object_key]["data"] == b"%PDF-1.7"


@pytest.mark.asyncio
async def test_upload_document_list_read_and_download_are_owner_scoped():
    app, repo, storage = _limra_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="owned task",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    repo.create_task(
        task_id="task-b",
        owner_user_id="user-b",
        query="foreign task",
        scenario=None,
        runner_task_id="runner-task-b",
    )
    foreign = repo.record_uploaded_document(
        document_id="foreign-doc",
        owner_user_id="user-b",
        task_id="task-b",
        original_filename="foreign.txt",
        content_type="text/plain",
        byte_size=7,
        minio_bucket=storage.bucket,
        object_key="limra/users/foreign/tasks/task-b/uploads/foreign.txt",
        extracted_text="foreign",
        language=None,
        metadata={},
    )
    storage.objects[foreign.object_key] = {
        "data": b"foreign",
        "content_type": "text/plain",
        "metadata": {},
        "sha256": "foreign",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        unlinked_response = await client.post(
            "/api/limra/uploads",
            files={"file": ("unlinked.txt", b"unlinked", "text/plain")},
        )
        linked_response = await client.post(
            "/api/limra/uploads",
            params={"task_id": "task-a"},
            files={"file": ("linked.txt", b"linked", "text/plain")},
        )

        list_response = await client.get("/api/limra/uploads")
        task_list_response = await client.get(
            "/api/limra/uploads",
            params={"task_id": "task-a"},
        )
        foreign_task_list_response = await client.get(
            "/api/limra/uploads",
            params={"task_id": "task-b"},
        )
        detail_response = await client.get(
            f"/api/limra/uploads/{linked_response.json()['document_id']}"
        )
        download_response = await client.get(
            f"/api/limra/uploads/{linked_response.json()['document_id']}/download"
        )
        foreign_detail_response = await client.get("/api/limra/uploads/foreign-doc")
        foreign_download_response = await client.get(
            "/api/limra/uploads/foreign-doc/download"
        )

    assert unlinked_response.status_code == 201
    assert linked_response.status_code == 201
    assert list_response.status_code == 200
    listed = list_response.json()["documents"]
    assert {document["filename"] for document in listed} == {
        "linked.txt",
        "unlinked.txt",
    }
    assert all("object_key" not in document for document in listed)
    assert all("minio_object_key" not in document for document in listed)
    assert all(document["download_url"].startswith("/api/limra/uploads/") for document in listed)
    _assert_no_browser_leak(listed)

    assert task_list_response.status_code == 200
    assert [document["filename"] for document in task_list_response.json()["documents"]] == [
        "linked.txt"
    ]
    assert foreign_task_list_response.status_code == 404

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["filename"] == "linked.txt"
    assert detail["task_id"] == "task-a"
    assert "object_key" not in detail
    assert "minio_object_key" not in detail
    _assert_no_browser_leak(detail)

    assert download_response.status_code == 200
    assert download_response.content == b"linked"
    assert download_response.headers["content-type"].startswith("text/plain")
    assert 'filename="linked.txt"' in download_response.headers["content-disposition"]
    assert download_response.headers["x-content-type-options"] == "nosniff"
    assert foreign_detail_response.status_code == 404
    assert foreign_download_response.status_code == 404


@pytest.mark.asyncio
async def test_upload_search_is_owner_scoped_task_filterable_and_browser_safe():
    app, repo, storage = _limra_asgi_app()
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="owned lithium task",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    repo.create_task(
        task_id="task-b",
        owner_user_id="user-b",
        query="foreign lithium task",
        scenario=None,
        runner_task_id="runner-task-b",
    )
    repo.record_uploaded_document(
        document_id="doc-lithium",
        owner_user_id="user-a",
        task_id="task-a",
        original_filename="lithium-brief.txt",
        content_type="text/plain",
        byte_size=41,
        minio_bucket=storage.bucket,
        object_key="limra/users/user-a/tasks/task-a/uploads/doc-lithium.txt",
        extracted_text=(
            "Critical minerals update: lithium export controls and graphite risks."
        ),
        language=None,
        metadata={},
    )
    repo.record_uploaded_document(
        document_id="doc-copper",
        owner_user_id="user-a",
        task_id=None,
        original_filename="copper.txt",
        content_type="text/plain",
        byte_size=20,
        minio_bucket=storage.bucket,
        object_key="limra/users/user-a/uploads/doc-copper.txt",
        extracted_text="Copper inventory report.",
        language=None,
        metadata={},
    )
    repo.record_uploaded_document(
        document_id="doc-foreign",
        owner_user_id="user-b",
        task_id="task-b",
        original_filename="foreign-lithium.txt",
        content_type="text/plain",
        byte_size=18,
        minio_bucket=storage.bucket,
        object_key="limra/users/user-b/tasks/task-b/uploads/doc-foreign.txt",
        extracted_text="Lithium sanctions.",
        language=None,
        metadata={},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        search_response = await client.get(
            "/api/limra/uploads/search",
            params={"query": "lithium graphite"},
        )
        task_search_response = await client.get(
            "/api/limra/uploads/search",
            params={"query": "lithium", "task_id": "task-a"},
        )
        foreign_task_response = await client.get(
            "/api/limra/uploads/search",
            params={"query": "lithium", "task_id": "task-b"},
        )
        blank_response = await client.get(
            "/api/limra/uploads/search",
            params={"query": "   "},
        )

    assert search_response.status_code == 200
    payload = search_response.json()
    assert [document["document_id"] for document in payload["documents"]] == [
        "doc-lithium"
    ]
    result = payload["documents"][0]
    assert result["score"] > 0
    assert result["matched_terms"] == ["lithium", "graphite"]
    assert "graphite risks" in result["snippet"]
    assert "object_key" not in json.dumps(payload)
    assert "minio_object_key" not in json.dumps(payload)
    _assert_no_browser_leak(payload)

    assert task_search_response.status_code == 200
    assert [
        document["document_id"]
        for document in task_search_response.json()["documents"]
    ] == ["doc-lithium"]
    assert foreign_task_response.status_code == 404
    assert blank_response.status_code == 400
    assert blank_response.json()["detail"] == "search_query_required"


@pytest.mark.asyncio
async def test_upload_search_uses_configured_embedding_ranking_when_enabled():
    app, repo, storage = _limra_asgi_app()
    provider = FakeUploadEmbeddingProvider([1.0, 0.0])

    async def embedding_config_override():
        return limra.LimraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=2,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limra.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limra.get_upload_embedding_provider] = (
        embedding_provider_override
    )
    repo.record_uploaded_document(
        document_id="doc-vector",
        owner_user_id="user-a",
        task_id=None,
        original_filename="near-vector.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket=storage.bucket,
        object_key="limra/users/user-a/uploads/doc-vector.txt",
        extracted_text="Nickel supply memorandum.",
        language=None,
        metadata={},
        embedding=[1.0, 0.0],
    )
    repo.record_uploaded_document(
        document_id="doc-lexical",
        owner_user_id="user-a",
        task_id=None,
        original_filename="lexical-lithium.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket=storage.bucket,
        object_key="limra/users/user-a/uploads/doc-lexical.txt",
        extracted_text="Lithium lithium lithium.",
        language=None,
        metadata={},
        embedding=[0.0, 1.0],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.get(
            "/api/limra/uploads/search",
            params={"query": "lithium", "limit": 2},
        )

    assert response.status_code == 200
    payload = response.json()
    assert [document["document_id"] for document in payload["documents"]] == [
        "doc-vector",
        "doc-lexical",
    ]
    assert payload["documents"][0]["score"] > payload["documents"][1]["score"]
    assert "embedding" not in json.dumps(payload)
    assert "object_key" not in json.dumps(payload)
    assert provider.calls == [
        {
            "text": "lithium",
            "config": limra.LimraUploadEmbeddingConfig(
                enabled=True,
                provider="fake",
                model="fake-model",
                dimensions=2,
            ),
        }
    ]
    _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_upload_search_falls_back_to_lexical_when_embedding_provider_fails():
    app, repo, storage = _limra_asgi_app()
    provider = FakeFailingUploadEmbeddingProvider(RuntimeError("provider unavailable"))

    async def embedding_config_override():
        return limra.LimraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=2,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limra.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limra.get_upload_embedding_provider] = (
        embedding_provider_override
    )
    repo.record_uploaded_document(
        document_id="doc-lexical",
        owner_user_id="user-a",
        task_id=None,
        original_filename="lithium-fallback.txt",
        content_type="text/plain",
        byte_size=20,
        minio_bucket=storage.bucket,
        object_key="limra/users/user-a/uploads/doc-lexical.txt",
        extracted_text="Lithium supply memo.",
        language=None,
        metadata={},
        embedding=[0.0, 1.0],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.get(
            "/api/limra/uploads/search",
            params={"query": "lithium"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert [document["document_id"] for document in payload["documents"]] == [
        "doc-lexical"
    ]
    assert payload["documents"][0]["matched_terms"] == ["lithium"]
    assert "Lithium supply memo" in payload["documents"][0]["snippet"]
    assert "embedding" not in json.dumps(payload)
    assert "object_key" not in json.dumps(payload)
    assert provider.calls == [
        {
            "text": "lithium",
            "config": limra.LimraUploadEmbeddingConfig(
                enabled=True,
                provider="fake",
                model="fake-model",
                dimensions=2,
            ),
        }
    ]
    _assert_no_browser_leak(payload)


@pytest.mark.asyncio
async def test_upload_search_rejects_embedding_dimension_mismatch():
    app, repo, storage = _limra_asgi_app()
    provider = FakeUploadEmbeddingProvider([1.0])

    async def embedding_config_override():
        return limra.LimraUploadEmbeddingConfig(
            enabled=True,
            provider="fake",
            model="fake-model",
            dimensions=2,
        )

    async def embedding_provider_override():
        return provider

    app.dependency_overrides[limra.get_upload_embedding_config] = (
        embedding_config_override
    )
    app.dependency_overrides[limra.get_upload_embedding_provider] = (
        embedding_provider_override
    )
    repo.record_uploaded_document(
        document_id="doc-vector",
        owner_user_id="user-a",
        task_id=None,
        original_filename="near-vector.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket=storage.bucket,
        object_key="limra/users/user-a/uploads/doc-vector.txt",
        extracted_text="Nickel supply memorandum.",
        language=None,
        metadata={},
        embedding=[1.0, 0.0],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.get(
            "/api/limra/uploads/search",
            params={"query": "nickel"},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "upload_search_embedding_dimension_mismatch"


@pytest.mark.asyncio
async def test_pdf_route_rejects_object_key_aliases_on_actual_http_surface():
    app, repo, _storage = _limra_asgi_app()
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


@pytest.mark.asyncio
async def test_pdf_route_exports_report_to_storage_and_persists_metadata():
    app, repo, storage = _limra_asgi_app()
    pdf_exporter = app.state.test_pdf_exporter
    repo.create_task(
        task_id="task-a",
        owner_user_id="user-a",
        query="report",
        scenario=None,
        runner_task_id="runner-task-a",
    )
    repo.create_task(
        task_id="task-b",
        owner_user_id="user-b",
        query="foreign report",
        scenario=None,
        runner_task_id="runner-task-b",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.post(
            "/api/limra/tasks/task-a/reports/pdf",
            json={
                "report_id": "report-a",
                "report_type": "final",
                "markdown": "Finding references [EVID-001]",
                "evidence_refs": ["EVID-001"],
            },
        )
        unsafe_html_response = await client.post(
            "/api/limra/tasks/task-a/reports/pdf",
            json={
                "report_id": "report-html",
                "markdown": "Finding references [EVID-001]",
                "html": (
                    '<img src=x onerror=alert(1)>'
                    '<svg onload=alert(2)></svg>'
                    '<iframe srcdoc="<script>bad()</script>"></iframe>'
                    '<meta http-equiv="refresh" content="0;url=https://example.test">'
                ),
                "evidence_refs": ["EVID-001"],
            },
        )
        foreign_response = await client.post(
            "/api/limra/tasks/task-b/reports/pdf",
            json={
                "report_id": "report-b",
                "markdown": "foreign",
                "evidence_refs": [],
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["report_id"] == "report-a"
    assert payload["task_id"] == "task-a"
    assert payload["report_type"] == "final"
    assert payload["evidence_refs"] == ["EVID-001"]
    assert payload["pdf_size_bytes"] == len(pdf_exporter.pdf_bytes)
    assert payload["pdf_sha256"]
    assert payload["pdf_url"] == "/api/limra/tasks/task-a/reports/report-a/pdf"
    assert "object_key" not in payload
    assert "pdf_object_key" not in payload
    _assert_no_browser_leak(payload)

    assert len(pdf_exporter.html_inputs) == 1
    rendered_html = pdf_exporter.html_inputs[0]
    assert 'data-evidence-ref="EVID-001"' in rendered_html
    assert "<script" not in rendered_html.lower()
    assert "javascript:" not in rendered_html.lower()
    assert "onclick" not in rendered_html.lower()
    assert "content-security-policy" in rendered_html.lower()
    assert unsafe_html_response.status_code == 400
    assert unsafe_html_response.json()["detail"] == "browser_report_html_not_allowed"

    report = repo.get_user_report(
        task_id="task-a",
        report_id="report-a",
        owner_user_id="user-a",
    )
    assert report is not None
    assert report.markdown == "Finding references [EVID-001]"
    assert report.html == rendered_html
    assert report.evidence_refs == ["EVID-001"]
    assert report.pdf_object_key in storage.objects
    stored = storage.objects[report.pdf_object_key]
    assert stored["data"] == pdf_exporter.pdf_bytes
    assert stored["content_type"] == "application/pdf"
    assert stored["metadata"]["report_id"] == "report-a"
    assert stored["metadata"]["task_id"] == "task-a"
    assert stored["metadata"]["owner_user_id"] == "user-a"
    assert "/tasks/task-a/reports/" in report.pdf_object_key
    assert foreign_response.status_code == 404

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        download_response = await client.get(payload["pdf_url"])
        foreign_download_response = await client.get(
            "/api/limra/tasks/task-b/reports/report-a/pdf"
        )

    assert download_response.status_code == 200
    assert download_response.content == pdf_exporter.pdf_bytes
    assert download_response.headers["content-type"].startswith("application/pdf")
    assert 'filename="report-a.pdf"' in download_response.headers["content-disposition"]
    assert download_response.headers["x-content-type-options"] == "nosniff"
    assert foreign_download_response.status_code == 404


def test_report_html_renderer_strips_active_markup_from_markdown():
    rendered_html = limra._render_report_html(
        markdown=(
            "# Final Report\n\n"
            "Finding **important** [EVID-001]\n\n"
            "| Item | Status |\n"
            "| --- | --- |\n"
            "| BYD | not listed |\n\n"
            "- Verify official list\n"
            "- Monitor updates\n\n"
            '<img src="https://example.test/leak?token=RUNNER_SERVICE_TOKEN" onerror=alert(1)>\n'
            "<svg onload=alert(2)><text>bad</text></svg>\n"
            '<iframe srcdoc="<script>bad()</script>"></iframe>\n'
            '<meta http-equiv="refresh" content="0;url=https://example.test">\n'
            '<a href="javascript:alert(3)">bad link</a>\n'
            '<a href="data:text/html,bad">data link</a>\n'
            '<a href="blob:https://example.test/bad">blob link</a>\n'
            '<a href="file:///etc/passwd">file link</a>'
        ),
        evidence_refs=["EVID-001"],
    )

    lower_html = rendered_html.lower()
    assert "<h1>Final Report</h1>" in rendered_html
    assert "<strong>important</strong>" in rendered_html
    assert "<table>" in rendered_html
    assert "<th>Item</th>" in rendered_html
    assert "<td>BYD</td>" in rendered_html
    assert "<ul>" in rendered_html
    assert "<li>Verify official list</li>" in rendered_html
    assert 'data-evidence-ref="EVID-001"' in rendered_html
    assert "content-security-policy" in lower_html
    for forbidden in [
        "<img",
        "onerror",
        "<svg",
        "onload",
        "<iframe",
        "srcdoc",
        "refresh",
        "<script",
        "javascript:",
        "data:",
        "blob:",
        "file:",
        "https://example.test",
        "runner_service_token",
    ]:
        assert forbidden not in lower_html


@pytest.mark.asyncio
async def test_pdf_route_scrubs_report_secrets_before_persistence_and_export():
    app, repo, storage = _limra_asgi_app()
    pdf_exporter = app.state.test_pdf_exporter
    repo.create_task(
        task_id="task-secret-report",
        owner_user_id="user-a",
        query="report secrets",
        scenario=None,
        runner_task_id="runner-secret-report",
    )

    markdown = (
        "Finding [EVID-001]\n\n"
        "Authorization: Bearer report-bearer-secret-123456\n"
        "OPENAI_API_KEY=sk-reportopenai123456\n"
        "SERPER_API_KEY=serper-secret-123456\n"
        "Cookie: session=report-cookie-secret-123456\n"
        "https://example.test/path?token=report-url-token-123456"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limra.test",
    ) as client:
        response = await client.post(
            "/api/limra/tasks/task-secret-report/reports/pdf",
            json={
                "report_id": "report-secret",
                "report_type": "OPENAI_API_KEY=sk-reportopenai123456",
                "markdown": markdown,
                "evidence_refs": ["EVID-001", "JINA_API_KEY=nested-jina-secret-123456"],
            },
        )

    assert response.status_code == 201
    payload = response.json()
    _assert_no_raw_secret(payload)
    report = repo.get_user_report(
        task_id="task-secret-report",
        report_id="report-secret",
        owner_user_id="user-a",
    )
    assert report is not None
    assert limra.LIMRA_SECRET_REDACTION in report.markdown
    assert limra.LIMRA_SECRET_REDACTION in report.html
    assert "REDACTED" in report.report_type
    assert limra.LIMRA_SECRET_REDACTION in report.evidence_refs
    assert report.pdf_object_key in storage.objects
    stored = storage.objects[report.pdf_object_key]
    _assert_no_raw_secret(report.markdown)
    _assert_no_raw_secret(report.html)
    _assert_no_raw_secret(report.metadata)
    _assert_no_raw_secret(report.evidence_refs)
    _assert_no_raw_secret(stored["metadata"])
    assert len(pdf_exporter.html_inputs) == 1
    _assert_no_raw_secret(pdf_exporter.html_inputs[0])


def test_limra_secret_scrubber_redacts_nested_payloads_and_urls():
    payload = {
        "headers": {
            "Authorization": "Bearer report-bearer-secret-123456",
            "Cookie": "session=report-cookie-secret-123456",
        },
        "nested": [
            "OPENAI_API_KEY=sk-reportopenai123456",
            {
                "safe": "https://example.test/path?token=report-url-token-123456",
                "jina_api_key": "nested-jina-secret-123456",
            },
        ],
        "jwt": "eyJtrace.secret.payload",
    }

    scrubbed = limra.scrub_limra_secrets(payload)

    _assert_no_raw_secret(scrubbed)
    text = json.dumps(scrubbed, ensure_ascii=False)
    assert text.count(limra.LIMRA_SECRET_REDACTION) >= 5
    assert "Authorization" in scrubbed["headers"]
    assert scrubbed["headers"]["Authorization"] == limra.LIMRA_SECRET_REDACTION


@pytest.mark.asyncio
async def test_playwright_pdf_exporter_blocks_browser_resource_requests(monkeypatch):
    calls = {"launch_args": None, "closed": False}

    class FakeRoute:
        def __init__(self):
            self.aborted = False

        async def abort(self):
            self.aborted = True

    class FakePage:
        def __init__(self):
            self.routes = []
            self.set_content_calls = []

        async def route(self, pattern, handler):
            self.routes.append((pattern, handler))

        async def set_content(self, html_content, wait_until):
            self.set_content_calls.append(
                {"html_content": html_content, "wait_until": wait_until}
            )

        async def pdf(self, **kwargs):
            return b"%PDF-1.7\nfake\n%%EOF"

    class FakeBrowser:
        def __init__(self, page):
            self.page = page

        async def new_page(self):
            return self.page

        async def close(self):
            calls["closed"] = True

    class FakeChromium:
        def __init__(self, browser):
            self.browser = browser

        async def launch(self, args):
            calls["launch_args"] = args
            return self.browser

    class FakePlaywrightContext:
        def __init__(self, chromium):
            self.chromium = chromium

        async def __aenter__(self):
            return types.SimpleNamespace(chromium=self.chromium)

        async def __aexit__(self, exc_type, exc, tb):
            return False

    page = FakePage()
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)
    async_api_module = types.ModuleType("playwright.async_api")
    async_api_module.async_playwright = lambda: FakePlaywrightContext(chromium)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)

    pdf_bytes = await limra.PlaywrightLimraPdfExporter().render_pdf(
        "<!doctype html><html><body>report</body></html>"
    )

    assert pdf_bytes.startswith(b"%PDF")
    assert calls["launch_args"] == ["--no-sandbox"]
    assert calls["closed"] is True
    assert page.set_content_calls == [
        {
            "html_content": "<!doctype html><html><body>report</body></html>",
            "wait_until": "load",
        }
    ]
    assert len(page.routes) == 1
    pattern, handler = page.routes[0]
    assert pattern == "**/*"
    route = FakeRoute()
    await handler(route)
    assert route.aborted is True


def test_postgres_repository_sql_targets_limra_task_and_artifact_tables():
    sql = limra.PostgresLimraTaskRepository.sql_contract().lower()

    for table in (
        "limra_research_tasks",
        "limra_artifact_events",
        "limra_artifact_trace_events",
        "limra_evidence_items",
        "limra_entities",
        "limra_entity_relations",
        "limra_timeline_events",
        "limra_generated_reports",
        "limra_uploaded_documents",
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
    assert "insert into limra_artifact_trace_events" in sql
    assert "from limra_artifact_trace_events" in sql
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
    assert "pdf_object_key" in sql
    assert "from limra_generated_reports" in sql
    assert "order by created_at desc" in sql
    assert "returning" in sql
    assert "insert into limra_uploaded_documents" in sql
    assert "object_key" in sql
    assert "extracted_text" in sql
    assert "embedding" in sql
    assert "cast(:embedding as vector)" in sql
    assert "embedding <=> cast(:query_embedding as vector)" in sql
    assert "embedding is not null" in sql
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


def test_postgres_repository_records_artifact_trace_events_and_warnings():
    engine = FakeLimraPostgresEngine()
    repo = limra.PostgresLimraTaskRepository(
        "postgresql://limra:test@postgres:5432/limra",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-trace",
        owner_user_id="user-a",
        query="trace artifact events",
        scenario=None,
        runner_task_id="runner-trace",
    )

    repo.record_artifact(
        "task-trace",
        "evidence",
        {
            "evidence_id": "EVID-001",
            "title": "Evidence A",
            "source_event_type": "record_research_artifact",
        },
    )
    repo.record_artifact_trace_event(
        "task-trace",
        {
            "type": "artifact_warning",
            "artifact_type": "relation",
            "payload": {
                "warning": "invalid_artifact_payload",
                "artifact_type": "relation",
                "source_event_type": "record_research_artifact",
            },
            "source_event_type": "record_research_artifact",
        },
    )

    trace_events = repo.get_artifact_trace_events("task-trace")

    assert [event["type"] for event in trace_events] == [
        "evidence_collected",
        "artifact_warning",
    ]
    assert trace_events[0]["artifact_type"] == "evidence"
    assert trace_events[0]["bucket"] == "evidence"
    assert trace_events[0]["local_artifact_id"] == "EVID-001"
    assert trace_events[0]["source_event_type"] == "record_research_artifact"
    assert trace_events[0]["payload"]["title"] == "Evidence A"
    assert trace_events[1]["artifact_type"] == "relation"
    assert trace_events[1]["payload"]["warning"] == "invalid_artifact_payload"
    assert engine.artifact_trace_events[0]["task_id"] == "task-trace"


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


def test_postgres_repository_records_uploaded_documents_to_task_scoped_table():
    engine = FakeLimraPostgresEngine()
    repo = limra.PostgresLimraTaskRepository(
        "postgresql://limra:test@postgres:5432/limra",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-doc",
        owner_user_id="user-a",
        query="document query",
        scenario=None,
        runner_task_id="runner-doc",
    )

    document = repo.record_uploaded_document(
        document_id="doc-001",
        owner_user_id="user-a",
        task_id="task-doc",
        original_filename="brief.txt",
        content_type="text/plain",
        byte_size=12,
        minio_bucket="limra-artifacts",
        object_key="limra/users/hash/tasks/task-doc/uploads/doc-001.txt",
        extracted_text="brief text",
        language=None,
        metadata={"sha256": "abc123"},
        embedding=[0.1, 0.2, 0.3],
    )

    assert document.document_id == "doc-001"
    assert document.task_id == "task-doc"
    assert document.owner_user_id == "user-a"
    assert document.object_key.endswith("/uploads/doc-001.txt")
    assert document.extracted_text == "brief text"
    assert document.embedding == [0.1, 0.2, 0.3]
    assert document.metadata == {"sha256": "abc123"}
    assert engine.uploaded_documents["doc-001"]["minio_bucket"] == "limra-artifacts"
    assert engine.uploaded_documents["doc-001"]["embedding"] == "[0.1,0.2,0.3]"

    owned = repo.get_user_document("doc-001", "user-a")
    assert owned is not None
    assert owned.original_filename == "brief.txt"
    assert owned.embedding == [0.1, 0.2, 0.3]
    assert repo.get_user_document("doc-001", "user-b") is None
    assert [document.document_id for document in repo.list_user_documents(owner_user_id="user-a")] == [
        "doc-001"
    ]
    assert [
        document.document_id
        for document in repo.list_user_documents(owner_user_id="user-a", task_id="task-doc")
    ] == ["doc-001"]
    assert repo.list_user_documents(owner_user_id="user-a", task_id="task-other") == []
    assert repo.list_user_documents(owner_user_id="user-b") == []


def test_postgres_repository_searches_uploaded_documents_by_vector():
    engine = FakeLimraPostgresEngine()
    repo = limra.PostgresLimraTaskRepository(
        "postgresql://limra:test@postgres:5432/limra",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-doc",
        owner_user_id="user-a",
        query="document query",
        scenario=None,
        runner_task_id="runner-doc",
    )
    for document_id, owner, task_id, filename, text, embedding in (
        (
            "doc-far",
            "user-a",
            "task-doc",
            "lexical-lithium.txt",
            "Lithium lithium.",
            [0.0, 1.0],
        ),
        ("doc-near", "user-a", "task-doc", "nickel.txt", "Nickel memo.", [1.0, 0.0]),
        ("doc-other-task", "user-a", None, "other.txt", "Nickel other.", [1.0, 0.0]),
        (
            "doc-foreign",
            "user-b",
            "task-doc",
            "foreign.txt",
            "Nickel foreign.",
            [1.0, 0.0],
        ),
    ):
        repo.record_uploaded_document(
            document_id=document_id,
            owner_user_id=owner,
            task_id=task_id,
            original_filename=filename,
            content_type="text/plain",
            byte_size=len(text),
            minio_bucket="limra-artifacts",
            object_key=f"limra/users/hash/uploads/{document_id}.txt",
            extracted_text=text,
            language=None,
            metadata={},
            embedding=embedding,
        )

    results = repo.search_user_documents(
        owner_user_id="user-a",
        task_id="task-doc",
        query="lithium",
        limit=2,
        query_embedding=[1.0, 0.0],
    )

    assert [result.document.document_id for result in results] == [
        "doc-near",
        "doc-far",
    ]
    assert results[0].score > results[1].score
    assert engine.vector_search_calls == [
        {
            "owner_user_id": "user-a",
            "task_id": "task-doc",
            "query_embedding": "[1,0]",
            "limit": 2,
        }
    ]


def test_postgres_repository_records_generated_report_pdf_metadata():
    engine = FakeLimraPostgresEngine()
    repo = limra.PostgresLimraTaskRepository(
        "postgresql://limra:test@postgres:5432/limra",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-report",
        owner_user_id="user-a",
        query="report query",
        scenario=None,
        runner_task_id="runner-report",
    )

    report = repo.record_generated_report(
        report_id="report-001",
        task_id="task-report",
        report_type="final",
        markdown="Final report [EVID-001]",
        html="<p>Final report [EVID-001]</p>",
        pdf_object_key="limra/users/hash/tasks/task-report/reports/report-001.pdf",
        evidence_refs=["EVID-001"],
        creator_user_id="user-a",
        metadata={"pdf_sha256": "abc123", "pdf_size_bytes": 123},
    )

    assert report.report_id == "report-001"
    assert report.task_id == "task-report"
    assert report.pdf_object_key.endswith("/reports/report-001.pdf")
    assert report.evidence_refs == ["EVID-001"]
    assert report.metadata == {"pdf_sha256": "abc123", "pdf_size_bytes": 123}
    assert engine.generated_reports[("task-report", "report-001")]["pdf_object_key"].endswith(
        "/reports/report-001.pdf"
    )

    owned = repo.get_user_report(
        task_id="task-report",
        report_id="report-001",
        owner_user_id="user-a",
    )
    assert owned is not None
    assert owned.markdown == "Final report [EVID-001]"
    assert repo.get_user_report(
        task_id="task-report",
        report_id="report-001",
        owner_user_id="user-b",
    ) is None
    reports = repo.list_task_reports(task_id="task-report")
    assert [item.report_id for item in reports] == ["report-001"]
    assert reports[0].markdown == "Final report [EVID-001]"


def test_postgres_repository_invalidates_archive_metadata_on_task_scoped_writes():
    engine = FakeLimraPostgresEngine()
    repo = limra.PostgresLimraTaskRepository(
        "postgresql://limra:test@postgres:5432/limra",
        engine_factory=lambda _url: engine,
    )
    repo.create_task(
        task_id="task-archive-invalidated",
        owner_user_id="user-a",
        query="archive invalidation",
        scenario=None,
        runner_task_id="runner-archive-invalidated",
    )

    repo.update_task(
        "task-archive-invalidated",
        archive_status="ready",
        archive_object_key="limra/users/hash/tasks/task-archive-invalidated/archives/a.zip",
        archive_zip_sha256="sha-a",
    )
    repo.record_generated_report(
        report_id="report-invalidates",
        task_id="task-archive-invalidated",
        report_type="final",
        markdown="new report",
        html=None,
        pdf_object_key=None,
        evidence_refs=[],
        creator_user_id="user-a",
        metadata={},
    )
    assert engine.tasks["task-archive-invalidated"]["archive_object_key"] is None
    assert engine.tasks["task-archive-invalidated"]["archive_zip_sha256"] is None

    repo.update_task(
        "task-archive-invalidated",
        archive_object_key="limra/users/hash/tasks/task-archive-invalidated/archives/b.zip",
        archive_zip_sha256="sha-b",
    )
    repo.record_uploaded_document(
        document_id="doc-invalidates",
        owner_user_id="user-a",
        task_id="task-archive-invalidated",
        original_filename="brief.txt",
        content_type="text/plain",
        byte_size=5,
        minio_bucket="limra-artifacts",
        object_key="limra/users/hash/tasks/task-archive-invalidated/uploads/doc.txt",
        extracted_text="brief",
        language=None,
        metadata={},
    )
    assert engine.tasks["task-archive-invalidated"]["archive_object_key"] is None
    assert engine.tasks["task-archive-invalidated"]["archive_zip_sha256"] is None

    repo.update_task(
        "task-archive-invalidated",
        archive_object_key="limra/users/hash/tasks/task-archive-invalidated/archives/c.zip",
        archive_zip_sha256="sha-c",
    )
    repo.record_artifact(
        "task-archive-invalidated",
        "evidence",
        {
            "evidence_id": "EVID-INVALIDATES",
            "summary": "new evidence",
            "source_url": "https://example.test/source",
        },
    )
    assert engine.tasks["task-archive-invalidated"]["archive_object_key"] is None
    assert engine.tasks["task-archive-invalidated"]["archive_zip_sha256"] is None

    repo.update_task(
        "task-archive-invalidated",
        archive_object_key="limra/users/hash/tasks/task-archive-invalidated/archives/d.zip",
        archive_zip_sha256="sha-d",
    )
    repo.record_uploaded_document(
        document_id="doc-unlinked-does-not-invalidate",
        owner_user_id="user-a",
        task_id=None,
        original_filename="unlinked.txt",
        content_type="text/plain",
        byte_size=8,
        minio_bucket="limra-artifacts",
        object_key="limra/users/hash/uploads/doc-unlinked.txt",
        extracted_text="unlinked",
        language=None,
        metadata={},
    )
    assert (
        engine.tasks["task-archive-invalidated"]["archive_object_key"]
        == "limra/users/hash/tasks/task-archive-invalidated/archives/d.zip"
    )
    assert engine.tasks["task-archive-invalidated"]["archive_zip_sha256"] == "sha-d"


@pytest.mark.asyncio
async def test_event_proxy_streams_runner_events_and_populates_artifacts():
    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
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

    archive_response = await limra.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    trace = json.loads(_archive_member_texts(archive_response.body)["trace.json"])
    assert [event["type"] for event in trace["artifact_events"]] == [
        "evidence_collected",
        "entity_extracted",
        "report_section_generated",
        "artifact_warning",
    ]
    assert trace["artifact_events"][2]["source_event_type"] == (
        "record_research_artifact"
    )
    assert trace["artifact_events"][2]["local_artifact_id"] == "REPORT-001"
    assert trace["artifact_warnings"][0]["payload"]["warning"] == (
        "invalid_artifact_payload"
    )
    assert trace["artifact_warnings"][0]["payload"]["source_event_type"] == (
        "relation_extracted"
    )


@pytest.mark.asyncio
async def test_event_proxy_persists_final_summary_show_text_as_report_section():
    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
    user = limra.LimraUser("user-a")
    raw_secret_values = [
        "OPENAI_API_KEY=sk-finalopenai123456",
        "Bearer final-bearer-token-123456",
        "RUNNER_SERVICE_TOKEN=final-runner-token-123456",
    ]
    research = FakeResearchClient(
        events=[
            {
                "task_id": "runner-task-a",
                "type": "start_of_agent",
                "payload": {"agent_name": "Final Summary", "agent_id": "agent-final"},
            },
            {
                "task_id": "runner-task-a",
                "type": "tool_call",
                "payload": {
                    "tool_name": "show_text",
                    "tool_input": {
                        "text": (
                            "# Final answer\n\n"
                            "BYD is not on the active list. [EVID-001]\n\n"
                            + "\n".join(raw_secret_values)
                        )
                    },
                },
            },
            {
                "task_id": "runner-task-a",
                "type": "end_of_agent",
                "payload": {"agent_name": "Final Summary", "agent_id": "agent-final"},
            },
        ]
    )

    created = await limra.create_research_task(
        {"query": "BYD 1260H"},
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
        "start_of_agent",
        "tool_call",
        "report_section_generated",
        "end_of_agent",
        "status",
    ]
    serialized_events = json.dumps(events, ensure_ascii=False)
    for secret in raw_secret_values:
        assert secret not in serialized_events
    assert limra.LIMRA_SECRET_REDACTION in serialized_events
    assert limra.LIMRA_SECRET_REDACTION in json.dumps(
        events[1]["payload"]["tool_input"],
        ensure_ascii=False,
    )
    report_event = events[2]
    assert report_event["payload"]["title"] == "最终回答"
    assert report_event["payload"]["evidence_refs"] == ["EVID-001"]
    for secret in raw_secret_values:
        assert secret not in json.dumps(report_event, ensure_ascii=False)

    artifacts = await limra.get_task_artifacts(task_id, user=user, repo=repo)
    assert len(artifacts["report_sections"]) == 1
    assert artifacts["report_sections"][0]["markdown"].startswith("# Final answer")
    assert artifacts["report_sections"][0]["source_event_type"] == "final_summary_show_text"
    assert limra.LIMRA_SECRET_REDACTION in artifacts["report_sections"][0]["markdown"]
    for secret in raw_secret_values:
        assert secret not in json.dumps(artifacts, ensure_ascii=False)

    archive_response = await limra.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    members = _archive_member_texts(archive_response.body)
    assert "# Final answer" in members["report.md"]
    assert limra.LIMRA_SECRET_REDACTION in members["report.md"]
    assert limra.LIMRA_SECRET_REDACTION in members["trace.json"]
    for secret in raw_secret_values:
        assert secret not in json.dumps(members, ensure_ascii=False)
    trace = json.loads(members["trace.json"])
    assert trace["artifact_events"][0]["type"] == "report_section_generated"
    assert trace["artifact_events"][0]["source_event_type"] == "final_summary_show_text"


@pytest.mark.asyncio
async def test_record_research_artifact_tool_call_reaches_artifacts_and_archive_trace():
    from main import filter_message

    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
    user = limra.LimraUser("user-a")
    artifact_event = filter_message(
        {
            "event": "tool_call",
            "data": {
                "tool_name": "record_research_artifact",
                "tool_input": {
                    "artifact_type": "evidence",
                    "payload": {
                        "title": "Port authority bulletin",
                        "source_url": "https://example.test/port",
                        "summary": "New inspection rule published",
                    },
                    "evidence_refs": ["EVID-001"],
                    "confidence": 0.9,
                    "notes": "MCP tool path",
                },
            },
        }
    )
    research = FakeResearchClient(events=[artifact_event])

    created = await limra.create_research_task(
        {"query": "port inspection OSINT"},
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

    assert events[0]["type"] == "evidence_collected"
    artifacts = await limra.get_task_artifacts(task_id, user=user, repo=repo)
    assert artifacts["evidence"][0]["evidence_id"] == "EVID-001"
    assert artifacts["evidence"][0]["title"] == "Port authority bulletin"
    assert artifacts["evidence"][0]["notes"] == "MCP tool path"
    assert repo.get_task(task_id).archive_status == "ready"

    archive_response = await limra.download_task_archive(
        task_id,
        user=user,
        repo=repo,
        object_storage=storage,
    )
    trace = json.loads(_archive_member_texts(archive_response.body)["trace.json"])

    assert trace["artifacts"]["evidence"][0]["evidence_id"] == "EVID-001"
    assert trace["artifacts"]["evidence"][0]["title"] == "Port authority bulletin"
    assert trace["artifacts"]["evidence"][0]["source_event_type"] == (
        "record_research_artifact"
    )
    assert trace["artifact_events"][0]["type"] == "evidence_collected"
    assert trace["artifact_events"][0]["artifact_type"] == "evidence"
    assert trace["artifact_events"][0]["bucket"] == "evidence"
    assert trace["artifact_events"][0]["local_artifact_id"] == "EVID-001"
    assert trace["artifact_events"][0]["source_event_type"] == (
        "record_research_artifact"
    )
    assert trace["artifact_events"][0]["payload"]["title"] == (
        "Port authority bulletin"
    )
    assert trace["artifact_warnings"] == []
    assert repo.get_task(task_id).archive_object_key in storage.objects
    _assert_no_browser_leak(trace)


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
        ("/scenarios", "GET"),
        ("/research", "POST"),
        ("/tasks/{task_id}", "GET"),
        ("/tasks/{task_id}/events", "GET"),
        ("/tasks/{task_id}/artifacts", "GET"),
        ("/tasks/{task_id}/archive.zip", "GET"),
        ("/uploads", "GET"),
        ("/uploads", "POST"),
        ("/uploads/search", "GET"),
        ("/uploads/{document_id}", "GET"),
        ("/uploads/{document_id}/download", "GET"),
        ("/tasks/{task_id}/reports/pdf", "POST"),
        ("/tasks/{task_id}/reports/{report_id}/pdf", "GET"),
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


def _archive_zip(*, extra_member: bool = False, secret_members: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if secret_members:
            archive.writestr(
                "metadata.json",
                json.dumps(
                    {
                        "Authorization": "Bearer runner-token-123456",
                        "cookie": "open_webui_session=session-secret-123456",
                        "url": "https://search.test?q=x&token=archive-token-123456",
                    }
                ),
            )
            archive.writestr(
                "report.html",
                "<!doctype html><main>OPENAI_API_KEY=sk-archiveopenai123456</main>",
            )
            archive.writestr(
                "report.md",
                "# report\nRUNNER_SERVICE_TOKEN=archive-runner-token-123456\n"
                "https://api.test/resource?api_key=archive-query-secret-123456",
            )
            archive.writestr(
                "trace.json",
                json.dumps(
                    {
                        "headers": {"Cookie": "trace-cookie-secret-123456"},
                        "deepseek": "DEEPSEEK_API_KEY=sk-tracedeepseek123456",
                        "jwt": "eyJtrace.secret.payload",
                    }
                ),
            )
        else:
            archive.writestr("metadata.json", "{}")
            archive.writestr("report.html", "<!doctype html><main></main>")
            archive.writestr("report.md", "# report")
            archive.writestr("trace.json", "{}")
        if extra_member:
            archive.writestr(".env", "RUNNER_SERVICE_TOKEN=secret")
    return buffer.getvalue()


def _archive_member_texts(archive_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        return {
            member_name: archive.read(member_name).decode("utf-8")
            for member_name in archive.namelist()
        }


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


def _assert_no_raw_secret(payload):
    text = (
        payload.decode("utf-8", errors="ignore")
        if isinstance(payload, bytes)
        else json.dumps(payload, ensure_ascii=False)
        if isinstance(payload, (dict, list))
        else str(payload)
    )
    for forbidden in [
        "runner-token-123456",
        "session-secret-123456",
        "archive-token-123456",
        "sk-archiveopenai123456",
        "archive-runner-token-123456",
        "archive-query-secret-123456",
        "trace-cookie-secret-123456",
        "sk-tracedeepseek123456",
        "eyJtrace.secret.payload",
        "report-bearer-secret-123456",
        "sk-reportopenai123456",
        "serper-secret-123456",
        "report-cookie-secret-123456",
        "report-url-token-123456",
        "nested-jina-secret-123456",
    ]:
        assert forbidden not in text


def _limra_asgi_app():
    repo = limra.InMemoryLimraTaskRepository()
    storage = limra.InMemoryLimraObjectStorage()
    pdf_exporter = FakePdfExporter()
    app = FastAPI()
    app.include_router(limra.router, prefix="/api/limra")
    app.state.test_pdf_exporter = pdf_exporter

    async def current_user_override():
        return limra.LimraUser("user-a")

    async def task_repository_override():
        return repo

    async def object_storage_override():
        return storage

    async def pdf_exporter_override():
        return pdf_exporter

    app.dependency_overrides[limra.get_current_limra_user] = current_user_override
    app.dependency_overrides[limra.get_task_repository] = task_repository_override
    app.dependency_overrides[limra.get_object_storage] = object_storage_override
    app.dependency_overrides[limra.get_pdf_exporter] = pdf_exporter_override
    return app, repo, storage


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


class FakePdfExporter:
    def __init__(self):
        self.pdf_bytes = b"%PDF-1.7\nfake limra report\n%%EOF"
        self.html_inputs = []

    async def render_pdf(self, html_content):
        self.html_inputs.append(html_content)
        return self.pdf_bytes


class FakeUploadEmbeddingProvider:
    def __init__(self, embedding):
        self.embedding = list(embedding)
        self.calls = []

    async def embed_upload_text(self, text, *, config):
        self.calls.append({"text": text, "config": config})
        return list(self.embedding)


class FakeFailingUploadEmbeddingProvider:
    def __init__(self, error):
        self.error = error
        self.calls = []

    async def embed_upload_text(self, text, *, config):
        self.calls.append({"text": text, "config": config})
        raise self.error


def _pairs_to_mapping(values):
    assert len(values) % 2 == 0
    return {values[index]: values[index + 1] for index in range(0, len(values), 2)}


class FakeLimraPostgresEngine:
    def __init__(self):
        self.tasks = {}
        self.artifact_events = {}
        self.artifact_trace_events = []
        self.uploaded_documents = {}
        self.vector_search_calls = []
        self.generated_reports = {}
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

        if "update limra_research_tasks" in sql:
            row = self.tasks.get(params["task_id"])
            if not row:
                return FakeLimraPostgresResult([])
            for key, value in params.items():
                if key != "task_id":
                    row[key] = value
            return FakeLimraPostgresResult([row])

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

        if "insert into limra_artifact_trace_events" in sql:
            self.artifact_trace_events.append(
                {
                    "task_id": params["task_id"],
                    "event_type": params["event_type"],
                    "artifact_type": params["artifact_type"],
                    "bucket": params["bucket"],
                    "local_artifact_id": params["local_artifact_id"],
                    "payload": params["payload"],
                    "source_event_type": params["source_event_type"],
                }
            )
            return FakeLimraPostgresResult([])

        if "from limra_artifact_trace_events" in sql:
            rows = [
                {
                    "event_type": row["event_type"],
                    "artifact_type": row["artifact_type"],
                    "bucket": row["bucket"],
                    "local_artifact_id": row["local_artifact_id"],
                    "payload": row["payload"],
                    "source_event_type": row["source_event_type"],
                }
                for row in self.artifact_trace_events
                if row["task_id"] == params["task_id"]
            ]
            return FakeLimraPostgresResult(rows)

        if "from limra_artifact_events" in sql:
            rows = [
                row
                for (task_id, _artifact_type, _local_id), row in self.artifact_events.items()
                if task_id == params["task_id"]
            ]
            return FakeLimraPostgresResult(rows)

        if "insert into limra_uploaded_documents" in sql:
            row = {
                "document_id": params["document_id"],
                "task_id": params["task_id"],
                "owner_user_id": params["owner_user_id"],
                "original_filename": params["original_filename"],
                "content_type": params["content_type"],
                "byte_size": params["byte_size"],
                "minio_bucket": params["minio_bucket"],
                "object_key": params["object_key"],
                "extracted_text": params["extracted_text"],
                "language": params["language"],
                "embedding": params["embedding"],
                "metadata": params["metadata"],
            }
            self.uploaded_documents[params["document_id"]] = row
            return FakeLimraPostgresResult([row])

        if "from limra_uploaded_documents" in sql:
            if "embedding <=>" in sql:
                self.vector_search_calls.append(dict(params))
                query_embedding = limra._embedding_from_value(params["query_embedding"])
                rows = []
                for row in self.uploaded_documents.values():
                    if row["owner_user_id"] != params["owner_user_id"]:
                        continue
                    if (
                        params.get("task_id") is not None
                        and row["task_id"] != params["task_id"]
                    ):
                        continue
                    row_embedding = limra._embedding_from_value(row.get("embedding"))
                    score = limra._cosine_similarity(
                        query_embedding or [],
                        row_embedding,
                    )
                    if score is None:
                        continue
                    result_row = dict(row)
                    result_row["limra_search_score"] = score
                    rows.append(result_row)
                rows.sort(
                    key=lambda row: (
                        -row["limra_search_score"],
                        row["original_filename"].lower(),
                        row["document_id"],
                    )
                )
                return FakeLimraPostgresResult(rows[: params["limit"]])

            if "document_id = :document_id" in sql:
                row = self.uploaded_documents.get(params["document_id"])
                if row and row["owner_user_id"] != params["owner_user_id"]:
                    row = None
                return FakeLimraPostgresResult([row] if row else [])

            rows = [
                row
                for row in self.uploaded_documents.values()
                if row["owner_user_id"] == params["owner_user_id"]
                and (params.get("task_id") is None or row["task_id"] == params["task_id"])
            ]
            return FakeLimraPostgresResult(rows)

        if "insert into limra_generated_reports" in sql and "returning" in sql:
            row = {
                "report_id": params["report_id"],
                "task_id": params["task_id"],
                "report_type": params["report_type"],
                "markdown": params["markdown"],
                "html": params["html"],
                "pdf_object_key": params["pdf_object_key"],
                "evidence_refs": params["evidence_refs"],
                "creator_user_id": params["creator_user_id"],
                "metadata": params["metadata"],
            }
            self.generated_reports[(params["task_id"], params["report_id"])] = row
            return FakeLimraPostgresResult([row])

        if "from limra_generated_reports" in sql and "report_id = :report_id" in sql:
            task = self.tasks.get(params["task_id"])
            row = self.generated_reports.get((params["task_id"], params["report_id"]))
            if not task or task["owner_user_id"] != params["owner_user_id"]:
                row = None
            return FakeLimraPostgresResult([row] if row else [])

        if "from limra_generated_reports" in sql:
            rows = [
                row
                for (task_id, _report_id), row in self.generated_reports.items()
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
