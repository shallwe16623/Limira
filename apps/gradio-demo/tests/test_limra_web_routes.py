import io
import sys
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


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


def test_create_research_uses_limra_namespace_and_rejects_body_user_id():
    client, _repo, _archive = _client_for(limra.LimraUser("user-a"))

    response = client.post("/api/limra/research", json={"query": "track sanctions"})
    assert response.status_code == 202
    payload = response.json()
    assert payload["task_url"].startswith("/api/limra/tasks/")
    assert payload["events_url"].startswith("/api/limra/tasks/")
    assert payload["artifacts_url"].startswith("/api/limra/tasks/")
    _assert_no_browser_leak(payload)

    rejected = client.post(
        "/api/limra/research",
        json={"query": "track sanctions", "user_id": "attacker"},
    )
    assert rejected.status_code == 400


def test_user_isolation_for_task_status_and_archive_download():
    user_a_client, repo, archive = _client_for(limra.LimraUser("user-a"))
    task_id = user_a_client.post(
        "/api/limra/research",
        json={"query": "red sea shipping risk"},
    ).json()["task_id"]
    repo.tasks[task_id].archive_status = "ready"
    repo.tasks[task_id].runner_task_id = "runner-task-a"

    user_b_client, _repo, _archive = _client_for(
        limra.LimraUser("user-b"),
        repo=repo,
        archive=archive,
    )

    assert user_b_client.get(f"/api/limra/tasks/{task_id}").status_code == 404
    assert (
        user_b_client.get(f"/api/limra/tasks/{task_id}/archive.zip").status_code
        == 404
    )

    response = user_a_client.get(f"/api/limra/tasks/{task_id}/archive.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(response.content)).namelist() == [
        "metadata.json",
        "report.html",
        "report.md",
        "trace.json",
    ]
    assert archive.calls[0]["task"].runner_task_id == "runner-task-a"
    assert archive.calls[0]["user"].id == "user-a"
    _assert_no_browser_leak(response.text)


def test_admin_access_requires_explicit_admin_route():
    user_client, repo, archive = _client_for(limra.LimraUser("user-a"))
    task_id = user_client.post(
        "/api/limra/research",
        json={"query": "critical minerals policy"},
    ).json()["task_id"]
    repo.tasks[task_id].archive_status = "ready"

    admin_client, _repo, _archive = _client_for(
        limra.LimraUser("admin-user", role="admin"),
        repo=repo,
        archive=archive,
    )

    assert admin_client.get(f"/api/limra/tasks/{task_id}").status_code == 404
    assert admin_client.get(f"/api/limra/admin/tasks/{task_id}").status_code == 200
    assert (
        admin_client.get(f"/api/limra/admin/tasks/{task_id}/archive.zip").status_code
        == 200
    )


def test_archive_proxy_rejects_not_ready_and_invalid_zip_members():
    client, repo, _archive = _client_for(limra.LimraUser("user-a"))
    task_id = client.post("/api/limra/research", json={"query": "query"}).json()[
        "task_id"
    ]

    assert client.get(f"/api/limra/tasks/{task_id}/archive.zip").status_code == 409

    repo.tasks[task_id].archive_status = "ready"
    bad_client, _repo, _archive = _client_for(
        limra.LimraUser("user-a"),
        repo=repo,
        archive=FakeArchiveClient(_archive_zip(extra_member=True)),
    )
    assert bad_client.get(f"/api/limra/tasks/{task_id}/archive.zip").status_code == 502


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


def _client_for(
    user: limra.LimraUser,
    *,
    repo: limra.InMemoryLimraTaskRepository | None = None,
    archive: FakeArchiveClient | None = None,
):
    app = FastAPI()
    app.include_router(limra.router, prefix="/api/limra")
    repo = repo or limra.InMemoryLimraTaskRepository()
    archive = archive or FakeArchiveClient()
    app.state.limra_task_repository = repo
    app.state.limra_archive_client = archive
    app.dependency_overrides[limra.get_current_limra_user] = lambda: user
    app.dependency_overrides[limra.get_current_limra_admin] = lambda: user
    return TestClient(app), repo, archive


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
