import io
import json
import sys
import zipfile
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException


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
    assert "archive_object_key" in sql
    assert "archive_zip_sha256" in sql


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
    )
    first_events = _parse_sse_chunks([chunk async for chunk in first_response.body_iterator])

    second_response = await limra.get_task_events(
        task_id,
        user=user,
        repo=repo,
        research_client=research,
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
