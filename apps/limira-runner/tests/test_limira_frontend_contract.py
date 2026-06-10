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
LIMIRA_WEB_ROOT = REPO_ROOT / "apps" / "limira-web"
LIMIRA_STANDALONE_ROOT = REPO_ROOT / "apps" / "limira-standalone"
LIMIRA_STANDALONE_SERVER = LIMIRA_STANDALONE_ROOT / "server.mjs"
LIMIRA_STANDALONE_INDEX = LIMIRA_STANDALONE_ROOT / "public" / "index.html"
LIMIRA_STANDALONE_APP = LIMIRA_STANDALONE_ROOT / "public" / "app.js"
LIMIRA_STANDALONE_STYLES = LIMIRA_STANDALONE_ROOT / "public" / "styles.css"
LIMIRA_BACKEND_ROOT = LIMIRA_WEB_ROOT / "backend" / "limira_backend"
LIMIRA_BACKEND_ROUTER = LIMIRA_BACKEND_ROOT / "routers" / "limira.py"
LIMIRA_BACKEND_ROUTER_PARTS = LIMIRA_BACKEND_ROOT / "routers" / "limira_parts"
LIMIRA_NATIVE_APP = LIMIRA_WEB_ROOT / "backend" / "limira_native.py"
LEGACY_PY_PACKAGE = "open" + "_" + "web" + "ui"
LEGACY_APP_DIR = "open-" + "web" + "ui-limira-runner"
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


def _read_url(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class _RecordingBackendHandler(BaseHTTPRequestHandler):
    requests: list[str] = []
    headers_seen: list[dict[str, str]] = []

    def log_message(self, *_args):
        return

    def do_GET(self):
        self.__class__.requests.append(self.path)
        self.__class__.headers_seen.append(
            {str(key).lower(): str(value) for key, value in self.headers.items()}
        )
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"proxied_path": self.path}).encode("utf-8"))

    def do_POST(self):
        self.do_GET()


def _backend_artifact_event_types() -> set[str]:
    if LIMIRA_BACKEND_ROUTER_PARTS.exists():
        source = "\n".join(
            part.read_text(encoding="utf-8")
            for part in sorted(LIMIRA_BACKEND_ROUTER_PARTS.glob("limira_part_*.pyfrag"))
        )
    else:
        source = _read(LIMIRA_BACKEND_ROUTER)
    module = ast.parse(source)
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


def test_only_native_limira_frontend_and_backend_paths_remain():
    assert LIMIRA_STANDALONE_APP.exists()
    assert LIMIRA_STANDALONE_SERVER.exists()
    assert LIMIRA_BACKEND_ROUTER.exists()
    assert LIMIRA_NATIVE_APP.exists()

    removed_paths = [
        LIMIRA_WEB_ROOT / "src",
        LIMIRA_WEB_ROOT / "static",
        LIMIRA_WEB_ROOT / "node_modules",
        LIMIRA_WEB_ROOT / ".svelte-kit",
        LIMIRA_WEB_ROOT / "package.json",
        LIMIRA_WEB_ROOT / "package-lock.json",
        LIMIRA_WEB_ROOT / "svelte.config.js",
        LIMIRA_WEB_ROOT / "vite.config.ts",
        LIMIRA_WEB_ROOT / "backend" / LEGACY_PY_PACKAGE,
        REPO_ROOT / "apps" / LEGACY_APP_DIR,
    ]
    for path in removed_paths:
        assert not path.exists(), f"legacy frontend path still exists: {path}"


def test_limira_native_backend_imports_native_router_package_only():
    native_app = _read(LIMIRA_NATIVE_APP)
    router = _read(LIMIRA_BACKEND_ROUTER)

    assert "from limira_backend.routers import limira" in native_app
    assert f"from {LEGACY_PY_PACKAGE}" not in native_app
    assert f"import {LEGACY_PY_PACKAGE}" not in native_app
    assert f"from {LEGACY_PY_PACKAGE}" not in router
    assert f"import {LEGACY_PY_PACKAGE}" not in router


def test_limira_standalone_proxy_only_forwards_limira_api_namespace():
    node = _node_executable()
    if not node:
        pytest.skip("node executable is unavailable")

    backend_port = _free_local_port()
    proxy_port = _free_local_port()
    _RecordingBackendHandler.requests = []
    _RecordingBackendHandler.headers_seen = []
    backend = ThreadingHTTPServer(("127.0.0.1", backend_port), _RecordingBackendHandler)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    process = subprocess.Popen(
        [node, str(LIMIRA_STANDALONE_SERVER)],
        cwd=LIMIRA_STANDALONE_ROOT,
        env={
            **os.environ,
            "LIMIRA_STANDALONE_HOST": "127.0.0.1",
            "LIMIRA_STANDALONE_PORT": str(proxy_port),
            "LIMIRA_BACKEND_URL": f"http://127.0.0.1:{backend_port}",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base_url = f"http://127.0.0.1:{proxy_port}"
        for _ in range(50):
            try:
                status, body = _read_url(f"{base_url}/api/limira/scenarios")
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
        assert json.loads(body)["proxied_path"] == "/api/limira/scenarios"

        auth_status, auth_body = _read_url(f"{base_url}/api/limira/auth/session")
        assert auth_status == 200
        assert json.loads(auth_body)["proxied_path"] == "/api/limira/auth/session"

        spoof_status, spoof_body = _read_url(
            f"{base_url}/api/limira/scenarios",
            headers={
                "Authorization": "Bearer browser-session-token",
                "Cookie": "limira_session=user-cookie",
                "X-Limira-User-Id": "attacker",
                "X-Limira-User-Role": "admin",
                "X-Limira-Runner-Service-Token": "browser-forged-service-token",
            },
        )
        assert spoof_status == 200
        assert json.loads(spoof_body)["proxied_path"] == "/api/limira/scenarios"
        proxied_headers = _RecordingBackendHandler.headers_seen[-1]
        assert proxied_headers["authorization"] == "Bearer browser-session-token"
        assert proxied_headers["cookie"] == "limira_session=user-cookie"
        for private_header in (
            "x-limira-user-id",
            "x-limira-user-role",
            "x-limira-runner-service-token",
        ):
            assert private_header not in proxied_headers

        for blocked_path in [
            f"{LEGACY_AUTH_PREFIX}/",
            f"{LEGACY_AUTH_PREFIX}/update/timezone",
            "/api/config",
            "/limira-runner/tasks/task-a/events",
        ]:
            blocked_status, blocked_body = _read_url(f"{base_url}{blocked_path}")
            assert blocked_status == 404
            assert json.loads(blocked_body)["detail"] == "not_found"

        assert _RecordingBackendHandler.requests == [
            "/api/limira/scenarios",
            "/api/limira/auth/session",
            "/api/limira/scenarios",
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


def test_limira_standalone_frontend_uses_native_auth_namespace_only():
    app = _read(LIMIRA_STANDALONE_APP)
    index = _read(LIMIRA_STANDALONE_INDEX)
    server = _read(LIMIRA_STANDALONE_SERVER)

    assert "/api/limira/auth/signup" in app
    assert "/api/limira/auth/signin" in app
    assert "/api/limira/auth/session" in app
    assert "/api/limira/auth/signout" in app
    assert "/api/limira/auth/verify-email" in app
    assert "/api/limira/auth/resend-verification" in app
    assert "/api/limira/auth/password-reset/request" in app
    assert "/api/limira/auth/password-reset/confirm" in app
    assert "/api/limira/auth/organizations" in app
    assert "/api/limira/auth/enterprise/signin" in app
    assert "/api/limira/enterprise/members" in app
    assert "/api/limira/enterprise/usage" in app
    assert "archive_generated" in app
    assert "completion_asset_warning" in app
    assert 'id="historyArchiveToggleButton"' in index
    assert 'id="personalScopeButton"' in index
    assert 'id="enterpriseScopeButton"' in index
    assert 'id="organizationCategorySelect"' in index
    assert 'id="organizationSelect"' in index
    assert 'id="usernameInput"' in index
    assert 'id="usernameLabel"' in index
    assert 'id="enterpriseMemberUsernameInput"' in index
    assert 'id="enterpriseMemberEmailInput"' not in index
    assert 'id="userSettingsButton"' in index
    assert 'id="cloudDriveManageButton"' in index
    assert 'id="cloudDrivePage"' in index
    assert 'id="cloudDriveFileList"' in index
    assert 'id="userSettingsPanel"' in index
    assert 'id="archivedHistoryManageButton"' in index
    assert 'id="archivedHistoryPage"' in index
    assert 'id="archivedHistoryBackButton"' in index
    assert 'id="archivedHistoryList"' in index
    assert 'class="settings-menu-item danger"' in index
    assert "此页面直接连接 limira 后端" not in index
    assert 'id="enterpriseContactPrompt"' in index
    assert "如需开通单位账号，请通过以下方式联系团队。" in index
    assert 'id="enterpriseContactActions"' in index
    assert 'href="tel:+8617267052536"' in index
    assert 'href="mailto:admin@limira-inc.com"' in index
    assert "dom.enterpriseContactPrompt.classList.toggle('hidden', personalScope)" in app
    assert "dom.enterpriseContactActions.classList.toggle('hidden', personalScope)" in app
    assert "DEFAULT_ENTERPRISE_ORGANIZATION_CATEGORY = 'enterprise'" in app
    assert "DEFAULT_ENTERPRISE_ORGANIZATION_SLUG = 'limira'" in app
    assert "renderOrganizationCategoryOptions()" in app
    assert "organizationsForSelectedCategory()" in app
    assert "const username = dom.usernameInput.value.trim();" in app
    assert "{ username, password }" in app
    assert "organization_id: state.selectedOrganizationId,\n\t\t\t\t\tusername," in app
    assert "dom.usernameLabel.classList.toggle('hidden', !usernameVisible)" in app
    assert "dom.emailLabel.classList.toggle('hidden', !emailVisible)" in app
    assert "enterpriseMemberUsernameInput" in app
    assert "enterpriseMemberEmailInput" not in app
    assert "state.userSettingsOpen = !state.userSettingsOpen" in app
    assert "enterpriseMemberResearchCount(member)" in app
    for category_label in ("企业", "事业单位", "高校", "智库", "国家部委", "地方政府"):
        assert category_label in app
    assert 'id="enterpriseAdminManageButton"' in index
    assert 'id="enterpriseAdminPage"' in index
    assert 'id="enterpriseAdminBackButton"' in index
    assert 'id="enterpriseMemberForm"' in index
    assert '<option value="admin">管理员</option>' not in index
    assert "window.location.hash = 'enterprise-admin';" in app
    assert "state.route === 'enterprise-admin'" in app
    assert "dom.enterpriseAdminManageButton.classList.toggle('hidden', !admin)" in app
    assert "dom.enterpriseAdminManageButton.classList.toggle('active', pageVisible)" in app
    assert 'id="scenarioSelect"' not in index
    assert 'id="useScenarioButton"' not in index
    assert "填入模板" not in index
    assert "关键矿产竞争" not in app
    assert "scenario: state.selectedScenario" not in app
    assert 'id="uploadMenuButton"' in index
    assert 'id="uploadFileButton"' in index
    assert 'id="historyFileButton"' in index
    assert 'id="historyFilePanel"' in index
    assert 'id="cloudStoragePanel"' in index
    assert 'id="cloudStorageMeter"' in index
    assert 'id="uploadList" class="attachment-list"' in index
    assert 'id="uploadMessage" class="upload-message"' in index
    assert "上传文件" in index
    assert "历史文件" in index
    assert "upload-trigger" not in index
    assert "dom.uploadFileButton.addEventListener" in app
    assert "dom.historyFileButton.addEventListener" in app
    assert "selectedUploadDocumentIds()" in app
    assert "document_ids: documentIds" in app
    assert "/api/limira/uploads/history" in app
    assert "/api/limira/uploads/storage" in app
    assert "xhr.upload.addEventListener('progress'" in app
    assert "function renderUploadCard(document, options = {})" in app
    assert 'data-remove-upload-id="${escapeAttr(id)}"' in app
    assert '<div class="attachment-actions">${remove}</div>' in app
    assert '<div class="attachment-actions">${download}</div>' in app
    assert 'href="/api/limira/uploads/${encodeURIComponent(id)}/download"' not in app
    assert "/api/limira/auth/google/config" in app
    assert "/api/limira/auth/google/start" in app
    assert "/api/limira/auth/wechat/config" in app
    assert "/api/limira/auth/wechat/start" in app
    assert 'id="googleSigninButton"' in index
    assert 'id="wechatSigninButton"' in index
    assert "googleAuthEnabled" in app
    assert "wechatAuthEnabled" in app
    assert "authScope: 'personal'" in app
    assert "accountLabel(user)" in app
    assert "personal_daily_quota_exceeded" in app
    assert "verify_email_token" in app
    assert "reset_password_token" in app
    assert "auth_error" in app
    assert "google_auth" in app
    assert "wechat_auth" in app
    assert "x-forwarded-host" in server
    assert "x-forwarded-proto" in server
    assert LEGACY_AUTH_PREFIX not in app
    assert LEGACY_AUTH_PREFIX not in server
    assert "isAuthApiPath" not in server
    assert "pathname.startsWith('/api/limira/')" in server


def test_limira_standalone_frontend_exposes_native_task_history_controls():
    app = _read(LIMIRA_STANDALONE_APP)
    index = _read(LIMIRA_STANDALONE_INDEX)

    assert 'id="historyList"' in index
    assert 'id="historyMessage"' in index
    assert 'id="newChatButton"' in index
    assert 'id="refreshHistoryButton"' in index
    assert "function loadTaskHistory()" in app
    assert "function selectHistoryTask(taskId)" in app
    assert "function startNewChat()" in app
    assert "`/api/limira/tasks?limit=${MAX_HISTORY_TASKS}&archived=${archived}`" in app
    assert 'id="historyArchiveToggleButton"' in index
    assert 'id="archivedHistoryManageButton"' in index
    assert "window.location.hash = 'archived-chats';" in app
    assert "state.route === 'archived-chats'" in app
    assert "function routeFromHash()" in app
    assert "function loadArchivedTaskHistory()" in app
    assert "function renderArchivedHistory()" in app
    assert "function archiveHistoryTask(taskId)" in app
    assert "function restoreHistoryTask(taskId)" in app
    assert "function deleteHistoryTask(taskId)" in app
    assert "function restoreArchivedHistoryTask(taskId)" in app
    assert "function deleteArchivedHistoryTask(taskId)" in app
    assert "`/api/limira/tasks?limit=${MAX_HISTORY_TASKS}&archived=true`" in app
    assert "/history/archive" in app
    assert "/history/restore" in app
    assert "method: 'DELETE'" in app
    assert "/api/limira/tasks/${encodeURIComponent(state.taskId)}" in app
    assert "state.eventSource = new EventSource(`/api/limira/tasks/${state.taskId}/events`)" in app
    assert LEGACY_AUTH_PREFIX not in app


def test_limira_standalone_frontend_uses_archive_only_export_surface():
    app = _read(LIMIRA_STANDALONE_APP)
    index = _read(LIMIRA_STANDALONE_INDEX)
    styles = _read(LIMIRA_STANDALONE_STYLES)

    assert "exportPdfButton" not in app
    assert "exportPdfButton" not in index
    assert "reports/pdf" not in app
    assert "report_pdf_generated" not in app
    assert "async function downloadGeneratedPdf" not in app
    assert "accept: 'application/pdf'" not in app
    assert "导出并下载 PDF" not in index
    assert 'class="header-actions"' not in index
    assert 'id="downloadArchiveButton"' not in index
    assert 'id="downloadArchiveButton"' in app
    assert "data-archive-download" in app
    assert "const CONVERSATION_VIEW = '对话';" in app
    assert "const BACK_TO_CHAT_LABEL = '回到对话';" in app
    assert "const tabs = ['证据', '实体', '图谱', '时间线', '地图'];" in app
    assert "state.activeTab = CONVERSATION_VIEW;" in app
    assert "state.activeTab = tabs.includes(button.dataset.tab) ? button.dataset.tab : CONVERSATION_VIEW;" in app
    assert "addMessage('assistant', state.finalReportText, { format: 'markdown', kind: 'report' });" in app
    assert "state.activeTab = '报告'" not in app
    assert "function initialMessages()" in app
    assert "return [];" in app
    assert "function hasConversationActivity()" in app
    assert "dom.thinkingPanel?.classList.toggle('hidden', !conversationView || !hasConversationActivity());" in app
    assert "dom.inputContainer?.classList.toggle('hidden', state.route !== 'workspace' || !conversationView);" in app
    assert index.index('id="thinkingPanel"') < index.index('id="artifactTabs"')
    assert index.index('id="artifactTabs"') < index.index('id="artifactContent"')
    assert ".tabs {\n\tz-index: 8;" in styles
    assert "bottom: 7.15rem;" not in styles
    assert "dom.workspaceContent.scrollTo({ top: 0, behavior: 'smooth' });" in app
    assert "async function downloadArchive()" in app
    assert "downloadPdfButton" not in app
    assert "downloadPdfButton" not in index


def test_limira_standalone_report_sections_take_precedence_over_cached_final_text():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "const finalCard = !cards && state.finalReportText" in app
    assert "if (sectionText) {" in app
    assert "return sectionText;" in app
    assert "return reportTextFromValue(state.finalReportText);" in app


def test_limira_standalone_archive_download_uses_authenticated_fetch():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "dom.downloadArchiveButton = dom.artifactTabs.querySelector('[data-archive-download]');" in app
    assert "dom.downloadArchiveButton?.addEventListener('click', () => void downloadArchive());" in app
    assert "async function downloadArchive()" in app
    assert "async function downloadGeneratedArchive(url, filename)" in app
    assert "accept: 'application/zip'" in app
    assert "headers.set('authorization', `Bearer ${state.token}`);" in app
    assert "credentials: 'include'" in app
    assert "empty_archive_download" in app
    assert "dom.downloadArchiveButton.disabled = state.restoreBlocked || !state.taskId;" in app


def test_limira_standalone_evidence_preview_uses_local_srcdoc_fallback():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "function evidencePreviewHtml({ title, url, summary, notice })" in app
    assert "function evidencePreviewMode(url)" in app
    assert "function showEvidenceIframePreview({ url, title, summary })" in app
    assert "function redirectBlockedEvidenceSource({ url, title, summary })" in app
    assert "DIRECT_EXTERNAL_EVIDENCE_HOSTS" in app
    assert "dom.sandboxIframe.src = url;" in app
    assert "openExternalEvidenceUrl(safeUrl);" in app
    assert "link.rel = 'noopener noreferrer';" in app
    assert "link.referrerPolicy = 'no-referrer';" in app
    assert "dom.sandboxIframe.removeAttribute('src');" in app
    assert "部分网站禁止被第三方页面嵌入预览" in app
    assert "已自动尝试在新标签页打开" in app
    assert 'data-summary="${escapeAttr(summary)}"' in app
    assert '<div class="artifact-body markdown-body compact-markdown">${renderMarkdown(summary)}</div>' in app
    assert "const safeSummary = renderMarkdown(summary || '该来源没有可用的本地摘要。');" in app


def test_limira_standalone_hides_raw_event_log_and_keeps_progress_panel():
    app = _read(LIMIRA_STANDALONE_APP)
    index = _read(LIMIRA_STANDALONE_INDEX)

    assert "事件日志" not in index
    assert "eventLog" not in index
    assert "eventLog" not in app
    assert "state.events" not in app
    assert "recordEvent(" not in app
    assert "renderEvents(" not in app
    assert 'id="conversationPanel"' in index
    assert 'id="thinkingPanel"' in index
    assert 'id="thinkingToggleButton"' in index
    assert 'id="thinkingList"' in index
    assert 'aria-label="工作过程"' in index
    assert 'id="messageList"' in index
    assert 'id="clearStreamButton"' in index
    assert "function renderThinking()" in app
    assert "function addThinkingStep" in app
    assert "state.thinkingCollapsed = false;" in app


def test_limira_standalone_localizes_runner_task_failures():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "runner_task_failed: '研究任务失败，请检查运行配置或稍后重试。'" in app
    assert "if (typeof error === 'string') return localizedErrorDetail(error);" in app


def test_limira_standalone_persists_uploaded_documents_across_workspace_restore():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "state.uploads = Array.isArray(saved.uploads) ? saved.uploads : [];" in app
    assert "state.uploadResults = [];" in app
    assert "uploads: state.uploads" in app
    assert "state.uploads = mergeUploadedDocument(state.uploads, uploaded);" in app
    assert "function mergeUploadedDocument(documents, uploaded)" in app
    assert "state.cloudFiles = Array.isArray(historyData.documents) ? historyData.documents : [];" in app
    assert "state.uploads = Array.isArray(taskData.documents) ? taskData.documents : [];" not in app
    assert "state.uploads = [];" in app
    assert "state.uploads = reconcileSelectedUploads(state.uploads, state.cloudFiles);" in app
    assert "function reconcileSelectedUploads(selectedUploads, cloudFiles)" in app


def test_limira_standalone_stream_handler_refreshes_all_artifact_events():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "const artifactEvents = new Set([" in app
    assert "artifactEvents.has(eventType)" in app
    assert "void loadArtifacts();" in app
    for event_type in _required_frontend_artifact_event_types():
        assert f"'{event_type}'" in app


def test_limira_standalone_stream_handler_unwraps_nested_runner_events():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "const nested = data.data && typeof data.data === 'object' ? data.data : {};" in app
    assert "const eventData = data.event === eventType" in app
    assert "handleToolCall(eventData);" in app
    assert "thinkingStepForStartEvent(eventType, eventData)" in app


def test_limira_standalone_graph_and_map_render_without_external_frontend_stack():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "function renderGraph()" in app
    assert "function renderMap()" in app
    assert "function geometrySvg(" in app
    assert "map_features" in app
    assert "cytoscape" not in app.lower()
    assert "maplibre" not in app.lower()


def test_runtime_sources_do_not_reference_legacy_ui_or_auth_paths():
    runtime_files = [
        LIMIRA_STANDALONE_APP,
        LIMIRA_STANDALONE_SERVER,
        LIMIRA_NATIVE_APP,
        LIMIRA_BACKEND_ROUTER,
        REPO_ROOT / "apps" / "limira-runner" / "auth_adapter.py",
        REPO_ROOT / "apps" / "limira-runner" / "runner_api.py",
        REPO_ROOT / "docker-compose.limira.yml",
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
