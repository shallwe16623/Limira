import ast
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
LIMRA_WEB_ROOT = REPO_ROOT / "apps" / "limra-web"
LIMRA_STANDALONE_ROOT = REPO_ROOT / "apps" / "limra-standalone"
LIMRA_STANDALONE_SERVER = LIMRA_STANDALONE_ROOT / "server.mjs"
LIMRA_STANDALONE_INDEX = LIMRA_STANDALONE_ROOT / "public" / "index.html"
LIMRA_STANDALONE_APP = LIMRA_STANDALONE_ROOT / "public" / "app.js"
LIMRA_BACKEND_ROOT = LIMRA_WEB_ROOT / "backend" / "limra_backend"
LIMRA_BACKEND_ROUTER = LIMRA_BACKEND_ROOT / "routers" / "limra.py"
LIMRA_NATIVE_APP = LIMRA_WEB_ROOT / "backend" / "limra_native.py"
LEGACY_PY_PACKAGE = "open" + "_" + "web" + "ui"
LEGACY_APP_DIR = "open-" + "web" + "ui-mirothinker"
LEGACY_AUTH_PREFIX = "/" + "api" + "/" + "v1" + "/" + "auths"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _node_executable() -> str | None:
    return shutil.which("node") or (
        "/tmp/codex-node/node/bin/node"
        if Path("/tmp/codex-node/node/bin/node").exists()
        else None
    )


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_url(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class _RecordingBackendHandler(BaseHTTPRequestHandler):
    requests: list[str] = []

    def log_message(self, *_args):
        return

    def do_GET(self):
        self.__class__.requests.append(self.path)
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"proxied_path": self.path}).encode("utf-8"))

    def do_POST(self):
        self.do_GET()


def _backend_artifact_event_types() -> set[str]:
    module = ast.parse(_read(LIMRA_BACKEND_ROUTER))
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "ARTIFACT_EVENT_TYPES"
            for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        assert isinstance(value, dict)
        return {str(event_type) for event_type in value}
    raise AssertionError("ARTIFACT_EVENT_TYPES not found")


def _required_frontend_artifact_event_types() -> list[str]:
    return sorted(_backend_artifact_event_types() | {"record_research_artifact"})


def test_only_native_limra_frontend_and_backend_paths_remain():
    assert LIMRA_STANDALONE_APP.exists()
    assert LIMRA_STANDALONE_SERVER.exists()
    assert LIMRA_BACKEND_ROUTER.exists()
    assert LIMRA_NATIVE_APP.exists()

    removed_paths = [
        LIMRA_WEB_ROOT / "src",
        LIMRA_WEB_ROOT / "static",
        LIMRA_WEB_ROOT / "node_modules",
        LIMRA_WEB_ROOT / ".svelte-kit",
        LIMRA_WEB_ROOT / "package.json",
        LIMRA_WEB_ROOT / "package-lock.json",
        LIMRA_WEB_ROOT / "svelte.config.js",
        LIMRA_WEB_ROOT / "vite.config.ts",
        LIMRA_WEB_ROOT / "backend" / LEGACY_PY_PACKAGE,
        REPO_ROOT / "apps" / LEGACY_APP_DIR,
    ]
    for path in removed_paths:
        assert not path.exists(), f"legacy frontend path still exists: {path}"


def test_limra_native_backend_imports_native_router_package_only():
    native_app = _read(LIMRA_NATIVE_APP)
    router = _read(LIMRA_BACKEND_ROUTER)

    assert "from limra_backend.routers import limra" in native_app
    assert f"from {LEGACY_PY_PACKAGE}" not in native_app
    assert f"import {LEGACY_PY_PACKAGE}" not in native_app
    assert f"from {LEGACY_PY_PACKAGE}" not in router
    assert f"import {LEGACY_PY_PACKAGE}" not in router


def test_limra_standalone_proxy_only_forwards_limra_api_namespace():
    node = _node_executable()
    if not node:
        pytest.skip("node executable is unavailable")

    backend_port = _free_local_port()
    proxy_port = _free_local_port()
    _RecordingBackendHandler.requests = []
    backend = ThreadingHTTPServer(("127.0.0.1", backend_port), _RecordingBackendHandler)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    process = subprocess.Popen(
        [node, str(LIMRA_STANDALONE_SERVER)],
        cwd=LIMRA_STANDALONE_ROOT,
        env={
            **os.environ,
            "LIMRA_STANDALONE_HOST": "127.0.0.1",
            "LIMRA_STANDALONE_PORT": str(proxy_port),
            "LIMRA_BACKEND_URL": f"http://127.0.0.1:{backend_port}",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base_url = f"http://127.0.0.1:{proxy_port}"
        for _ in range(50):
            try:
                status, body = _read_url(f"{base_url}/api/limra/scenarios")
                if status == 200:
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            stdout, stderr = process.communicate(timeout=2)
            raise AssertionError(
                f"standalone proxy did not start; stdout={stdout!r}; stderr={stderr!r}"
            )

        assert status == 200
        assert json.loads(body)["proxied_path"] == "/api/limra/scenarios"

        auth_status, auth_body = _read_url(f"{base_url}/api/limra/auth/session")
        assert auth_status == 200
        assert json.loads(auth_body)["proxied_path"] == "/api/limra/auth/session"

        for blocked_path in [
            f"{LEGACY_AUTH_PREFIX}/",
            f"{LEGACY_AUTH_PREFIX}/update/timezone",
            "/api/config",
            "/mirothinker/tasks/task-a/events",
        ]:
            blocked_status, blocked_body = _read_url(f"{base_url}{blocked_path}")
            assert blocked_status == 404
            assert json.loads(blocked_body)["detail"] == "not_found"

        assert _RecordingBackendHandler.requests == [
            "/api/limra/scenarios",
            "/api/limra/auth/session",
        ]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()


def test_limra_standalone_frontend_uses_native_auth_namespace_only():
    app = _read(LIMRA_STANDALONE_APP)
    server = _read(LIMRA_STANDALONE_SERVER)

    assert "/api/limra/auth/signup" in app
    assert "/api/limra/auth/signin" in app
    assert "/api/limra/auth/session" in app
    assert "/api/limra/auth/signout" in app
    assert LEGACY_AUTH_PREFIX not in app
    assert LEGACY_AUTH_PREFIX not in server
    assert "isAuthApiPath" not in server
    assert "pathname.startsWith('/api/limra/')" in server


def test_limra_standalone_frontend_exposes_native_task_history_controls():
    app = _read(LIMRA_STANDALONE_APP)
    index = _read(LIMRA_STANDALONE_INDEX)

    assert 'id="historyList"' in index
    assert 'id="historyMessage"' in index
    assert 'id="newChatButton"' in index
    assert 'id="refreshHistoryButton"' in index
    assert "function loadTaskHistory()" in app
    assert "function selectHistoryTask(taskId)" in app
    assert "function startNewChat()" in app
    assert "`/api/limra/tasks?limit=${MAX_HISTORY_TASKS}`" in app
    assert "/api/limra/tasks/${encodeURIComponent(state.taskId)}" in app
    assert "state.eventSource = new EventSource(`/api/limra/tasks/${state.taskId}/events`)" in app
    assert LEGACY_AUTH_PREFIX not in app


def test_limra_standalone_frontend_keeps_pdf_download_bound_to_current_report():
    app = _read(LIMRA_STANDALONE_APP)
    index = _read(LIMRA_STANDALONE_INDEX)

    assert "const markdown = reportMarkdown().trim();" in app
    assert "if (!state.taskId || !markdown || state.isExporting)" in app
    assert "latestReportMatchesCurrentMarkdown()" in app
    assert "state.latestReportMarkdown = markdown;" in app
    assert "/api/limra/tasks/${encodeURIComponent(state.taskId)}/reports/pdf" in app
    assert "/api/limra/tasks/${encodeURIComponent(normalized.task_id)}/reports/${encodeURIComponent(" in app
    assert "normalized.report_id" in app
    assert "async function downloadGeneratedPdf(url, filename)" in app
    assert "accept: 'application/pdf'" in app
    assert "URL.createObjectURL(blob)" in app
    assert "state.latestReport = null;" in app
    assert "请重新导出 PDF" in app
    assert "downloadPdfButton" not in app
    assert "downloadPdfButton" not in index
    assert "导出并下载 PDF" in index


def test_limra_standalone_stream_handler_refreshes_all_artifact_events():
    app = _read(LIMRA_STANDALONE_APP)

    assert "const artifactEvents = new Set([" in app
    assert "artifactEvents.has(eventType)" in app
    assert "void loadArtifacts();" in app
    for event_type in _required_frontend_artifact_event_types():
        assert f"'{event_type}'" in app


def test_limra_standalone_graph_and_map_render_without_external_frontend_stack():
    app = _read(LIMRA_STANDALONE_APP)

    assert "function renderGraph()" in app
    assert "function renderMap()" in app
    assert "function geometrySvg(" in app
    assert "map_features" in app
    assert "cytoscape" not in app.lower()
    assert "maplibre" not in app.lower()


def test_runtime_sources_do_not_reference_legacy_ui_or_auth_paths():
    runtime_files = [
        LIMRA_STANDALONE_APP,
        LIMRA_STANDALONE_SERVER,
        LIMRA_NATIVE_APP,
        LIMRA_BACKEND_ROUTER,
        REPO_ROOT / "apps" / "gradio-demo" / "auth_adapter.py",
        REPO_ROOT / "apps" / "gradio-demo" / "runner_api.py",
        REPO_ROOT / "docker-compose.limra-aggressive.yml",
    ]
    forbidden = [
        LEGACY_PY_PACKAGE,
        "Open" + "Web" + "UI",
        "Open" + " Web" + "UI",
        LEGACY_AUTH_PREFIX,
        "X-Open" + "Web" + "UI",
        "WEB" + "UI_SECRET_KEY",
    ]
    for path in runtime_files:
        source = _read(path)
        for needle in forbidden:
            assert needle not in source, f"{needle!r} found in {path}"
