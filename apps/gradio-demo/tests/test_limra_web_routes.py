import io
import sys
import zipfile
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


@pytest.mark.asyncio
async def test_create_research_uses_limra_namespace_and_rejects_body_user_id():
    repo = limra.InMemoryLimraTaskRepository()
    user = limra.LimraUser("user-a")

    payload = await limra.create_research_task(
        {"query": "track sanctions"},
        request=None,
        user=user,
        repo=repo,
    )

    assert payload["task_url"].startswith("/api/limra/tasks/")
    assert payload["events_url"].startswith("/api/limra/tasks/")
    assert payload["artifacts_url"].startswith("/api/limra/tasks/")
    _assert_no_browser_leak(payload)

    with pytest.raises(HTTPException) as rejected:
        await limra.create_research_task(
            {"query": "track sanctions", "user_id": "attacker"},
            request=None,
            user=user,
            repo=repo,
        )
    assert rejected.value.status_code == 400


@pytest.mark.asyncio
async def test_user_isolation_for_task_status_and_archive_download():
    repo = limra.InMemoryLimraTaskRepository()
    archive = FakeArchiveClient()
    user_a = limra.LimraUser("user-a")
    user_b = limra.LimraUser("user-b")

    created = await limra.create_research_task(
        {"query": "red sea shipping risk"},
        request=None,
        user=user_a,
        repo=repo,
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
    user = limra.LimraUser("user-a")
    admin = limra.LimraUser("admin-user", role="admin")

    created = await limra.create_research_task(
        {"query": "critical minerals policy"},
        request=None,
        user=user,
        repo=repo,
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
    user = limra.LimraUser("user-a")
    created = await limra.create_research_task(
        {"query": "query"},
        request=None,
        user=user,
        repo=repo,
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


def _assert_no_browser_leak(payload):
    text = str(payload)
    for forbidden in limra.FORBIDDEN_BROWSER_SUBSTRINGS:
        assert forbidden not in text
