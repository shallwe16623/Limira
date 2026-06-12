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
    load_auth_options_block = app[app.index("async function loadAuthOptions()") : app.index("function setAuthScope(scope)")]
    assert "Promise.allSettled([" in load_auth_options_block
    assert "api('/api/limira/auth/organizations')" in load_auth_options_block
    assert "state.organizations = [];" not in load_auth_options_block
    assert "renderAuthMode();" in load_auth_options_block
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
    styles = _read(LIMIRA_STANDALONE_STYLES)

    assert 'id="historyList"' in index
    assert 'id="conversationNavigator"' in index
    assert 'id="conversationNavigatorList"' in index
    assert 'id="conversationNavigatorPreview"' in index
    assert 'id="historyMessage"' in index
    assert 'id="newChatButton"' in index
    assert 'id="refreshHistoryButton"' in index
    assert 'id="historyExpandToggleButton"' in index
    assert 'id="historyExpandIcon"' in index
    assert 'id="historySearchButton"' in index
    assert 'id="historySearchModal"' in index
    assert 'id="historySearchResults"' in index
    assert 'id="historySearchInput"' in index
    assert 'id="clearHistorySearchButton"' in index
    assert index.index('id="historySearchButton"') < index.index('id="historySearchModal"')
    assert index.index('id="historySearchModal"') < index.index('id="historySearchInput"')
    assert 'id="sidebarCollapseButton"' in index
    assert 'id="sidebarOverlayBackdrop"' in index
    assert 'id="mainSidebarOpenButton"' in index
    assert "sidebarOverlayOpen: false" in app
    assert "dom.mainSidebarOpenButton.addEventListener('click', () => {\n\t\tsetSidebarOverlayOpen(true);" in app
    assert "dom.sidebarOverlayBackdrop.addEventListener('click', () => {\n\t\tsetSidebarOverlayOpen(false);" in app
    assert "function setSidebarOverlayOpen(open)" in app

    assert "dom.workspace.classList.toggle('sidebar-overlay-open', Boolean(state.sidebarOverlayOpen));" in app
    assert "dom.sidebarOverlayBackdrop?.classList.toggle('hidden', !state.sidebarOverlayOpen);" in app
    assert "dom.mainSidebarOpenButton?.classList.toggle('hidden', !state.sidebarCollapsed || state.sidebarOverlayOpen);" in app
    assert ".workspace.sidebar-collapsed .main-sidebar-open-button" in styles
    assert ".workspace.sidebar-collapsed .sidebar {\n\tdisplay: none;" in styles
    assert ".workspace.sidebar-overlay-open .sidebar" in styles
    assert ".sidebar-overlay-backdrop" in styles
    assert "function scrollThinkingToLatest()" in app
    assert "scrollThinkingToLatest();" in app
    assert "function scrollThinkingListToBottom()" not in app
    assert "function keepThinkingListAboveInput()" not in app
    assert "dom.thinkingList.lastElementChild?.scrollIntoView({" not in app
    assert "dom.thinkingList.scrollTop = dom.thinkingList.scrollHeight;" not in app
    assert "dom.thinkingToggleIcon.textContent = state.thinkingCollapsed ? '>' : '⌄';" in app
    assert index.index('id="messageList"') < index.index('id="thinkingPanel"')
    assert index.index('id="thinkingPanel"') < index.index('id="reportList"')
    assert index.index('id="reportList"') < index.index('id="artifactTabs"')
    assert ".conversation-panel {\n\twidth: 100%;" in styles
    assert ".conversation-panel {\n\twidth: 100%;\n\tmax-width: 880px;\n\tmargin: 0 auto;\n\tdisplay: flex;\n\tflex-direction: column;\n\tgap: 0.9rem;\n}" in styles
    assert ".thinking-list {\n\tdisplay: grid;\n\tgap: 0.85rem;\n\toverflow: visible;\n\tpadding: 1rem;\n}" in styles
    assert ".task-thinking-panel" in styles
    assert ".conversation-navigator" in styles
    assert ".conversation-navigator-line" in styles
    assert ".conversation-navigator-title" in styles
    assert ".conversation-navigator-item.active .conversation-navigator-line" in styles
    assert ".conversation-navigator:hover .conversation-navigator-list" in styles
    assert ".conversation-navigator:hover .conversation-navigator-line" in styles
    assert ".conversation-navigator:hover .conversation-navigator-title" in styles
    assert ".conversation-navigator-preview" in styles
    assert "max-height: min(52vh, 560px);" not in styles
    assert "overscroll-behavior-y: auto;" not in styles
    assert "scrollbar-gutter: stable;" not in styles
    assert "overscroll-behavior: contain;" not in styles
    render_messages_block = app[app.index("function renderMessages(options = {})") : app.index("function latestUserMessageIndex()")]
    assert "message-meta" not in render_messages_block
    assert "roleLabel(message.role)" not in render_messages_block
    assert "message.time" not in render_messages_block
    assert 'class="message-bubble"' in render_messages_block
    assert "function latestUserMessageIndex()" in app
    assert "function renderMessageActions(message, index, latestUserIndex)" in app
    assert "function copyMessageContent(index, button)" in app
    assert "navigator.clipboard?.writeText" in app
    assert "document.execCommand('copy')" in app
    assert "function editMessageForResend(index)" in app
    assert "setQueryInputValue(String(message.content || ''));" in app
    assert "title=\"修改后再次发送\"" in app
    assert ".message-actions {\n\tdisplay: flex;" in styles
    assert ".message-list-bottom .message:hover .message-actions" in styles
    assert ".message-action-button.copied" in styles
    assert "SIDEBAR_COLLAPSED_STORAGE_KEY" in app
    assert "function setSidebarCollapsed(collapsed)" in app
    assert "dom.workspace.classList.toggle('sidebar-collapsed'" in app
    assert "state.historyExpanded = !state.historyExpanded;" in app
    assert "dom.historyList.classList.toggle('hidden', !state.historyExpanded);" in app
    assert "function openHistorySearchModal()" in app
    assert "function closeHistorySearchModal()" in app
    assert "function renderHistorySearchModal()" in app
    assert "function searchTaskHistory()" in app
    assert "function loadTaskHistory()" in app
    assert "const tasks = Array.isArray(data.tasks) ? data.tasks : [];" in app
    assert "observeTaskLifecycles(tasks);" in app
    assert "state.taskHistory = collapseCurrentConversationHistory(tasks);" in app
    assert "async function loadTaskProgressRecords()" in app
    assert "function rebuildThinkingFromProgressRecords(records)" in app
    assert "function thinkingStepsFromProgressRecords(records, taskId)" in app
    assert "function appendProgressRecordThinkingStep(record)" in app
    assert "thinkingStepsByTaskId: {}" in app
    assert "thinkingCollapsedByTaskId: {}" in app
    assert "function renderTaskThinkingPanel(message)" in app
    assert "function ensureReportThinkingSnapshots()" in app
    assert "async function loadTaskThinkingSnapshot(taskId)" in app
    assert "state.thinkingStepsByTaskId = {" in app
    assert "rememberTaskThinkingSteps(state.taskId);" in app
    assert "thinkingStepsByTaskId: savedThinkingStepsByTaskId()" in app
    assert "function renderMessages(options = {})" in app
    assert "dom.queryInput.addEventListener('input', () => {" in app
    assert "function resizeQueryInput()" in app
    assert "dom.queryInput.scrollHeight" in app
    assert "preserveScroll" in app
    assert "function handleWorkspaceScroll()" in app
    assert "function renderConversationNavigator()" in app
    assert "function conversationNavigationItems()" in app
    assert "function navigatorPromptTitle(prompt)" in app
    assert "dom.workspaceContent.scrollHeight - dom.workspaceContent.clientHeight > 24" in app
    assert "conversation-navigator-title" in app
    assert "conversationNavigatorHideTimer" not in app
    assert "function scrollToConversationMessage(index)" in app
    assert "data-navigator-message-index" in app
    assert "dom.workspaceContent.addEventListener('scroll', handleWorkspaceScroll);" in app
    assert "conversationNavigatorVisible" in app
    assert "function selectHistoryTask(taskId)" in app
    assert "function startNewChat()" in app
    assert "function switchToWorkspaceRoute()" in app
    assert "window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);" in app
    select_history_block = app[app.index("async function selectHistoryTask(taskId)") : app.index("function startNewChat()")]
    start_new_chat_block = app[app.index("function startNewChat()") : app.index("function resetCurrentTaskView()")]
    assert "switchToWorkspaceRoute();" in select_history_block
    assert "renderShell();" in select_history_block
    assert "await loadTaskProgressRecords();" in select_history_block
    assert "setQueryInputValue('', { syncState: false });" in select_history_block
    assert "const members = conversationMembersForTask(cached, normalizedTaskId);" in select_history_block
    assert "state.conversationRootTaskId = rootTaskId;" in select_history_block
    assert "state.conversationTaskIds = uniqueTaskIds(members.map((task) => task.task_id));" in select_history_block
    assert "state.messages = conversationHistoryMessages(members);" in select_history_block
    assert "await hydrateConversationHistory(members);" in select_history_block
    assert "dom.queryInput.value = cached.query" not in select_history_block
    assert "switchToWorkspaceRoute();" in start_new_chat_block
    assert "renderShell();" in start_new_chat_block
    assert "const params = new URLSearchParams({" in app
    assert "api(`/api/limira/tasks?${activeParams.toString()}`)" in app
    assert "api(`/api/limira/tasks?${archivedParams.toString()}`)" in app
    assert "Promise.all([" in app
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
    assert "api(`/api/limira/tasks/${encodeURIComponent(state.taskId)}/event-logs`)" in app
    assert "state.eventSource = new EventSource(`/api/limira/tasks/${state.taskId}/events`)" in app
    assert "message.kind === 'report' && String(message.taskId || '') === taskId" in app
    assert "function conversationMembersForTask(task, fallbackTaskId)" in app
    assert "function hydrateConversationHistory(members)" in app
    assert "conversation_members" in app
    assert "insertAfterConversationIndex: memberIndex" in app
    assert "reportKey: conversationReportKey(taskId, memberIndex)" in app
    assert "function insertReportMessage(messages, message, options = {})" in app
    assert "function shouldInlineConversationReports()" in app
    assert "function orderedConversationIndexedMessages(indexedMessages)" in app
    assert "function messageAnchorKeys(message)" in app
    assert "const inlineConversationReports = shouldInlineConversationReports();" in app
    assert "taskId" in app[app.index("function upsertReportMessage(content, options = {})") : app.index("function appendThinkingStep")]
    render_messages_block = app[app.index("function renderMessages(options = {})") : app.index("function latestUserMessageIndex()")]
    assert "function messageBelongsToCurrentTask(message)" in app
    assert "function renderReportTaskControls(message)" in app
    assert "data-report-task-id" in app
    assert "data-report-tab" in app
    assert "data-report-archive" in app
    assert "function currentArtifactTaskId()" in app
    assert "conversationRootTaskId: ''" in app
    assert "conversationTaskIds: []" in app
    assert "const wasContinuingConversation = hasConversationActivity() && Boolean(previousConversationRootTaskId);" in app
    assert "researchBody.conversation_id = previousConversationRootTaskId;" in app
    assert "const responseConversationId = String(task.conversation_id || '').trim();" in app
    assert "const restoredMessageTaskIds = taskIdsFromMessages(state.messages);" in app
    assert "state.conversationRootTaskId = restoredMessageTaskIds[0];" in app
    assert "function taskIdsFromMessages(messages)" in app
    assert "function collapseCurrentConversationHistory(tasks)" in app
    assert "function rememberCurrentConversationTask(taskId, rootTaskId = '')" in app
    assert "mergeTaskHistory(task, { suppressIfCurrentConversation: wasContinuingConversation });" in app
    assert "state.historySearchResults = collapseCurrentConversationHistory([...byId.values()]);" in app
    assert "function artifactsForTask(taskId)" in app
    assert "function archiveStatusForTask(taskId)" in app
    assert "state.artifactTaskId" in app
    assert "state.artifactsByTaskId" in app
    assert "const shouldSelect =" in app
    assert "options.select === true" in app
    assert "options.select !== false" in app
    assert "await loadArtifacts(normalizedTaskId, { updateReport: false, select: true });" in app
    assert "void loadArtifacts(taskId, { updateReport: false, silent: true, select: false })" in app
    assert "state.artifactsByTaskId = {" in app
    assert "function ensureReportArtifactSnapshots()" in app
    assert "function rememberReportArtifactCounts(taskId, artifacts)" in app
    assert "artifactCountsSnapshot" in app
    assert "message.artifactCountsSnapshot || {}" in app
    assert "upsertReportMessage(reportMarkdown(artifacts));" in app
    assert "upsertArtifactThinkingStep(artifacts);" in app
    assert "return artifacts ? artifactCounts(artifacts) : {};" in app
    assert "function artifactCounts(artifacts = state.artifacts)" in app
    artifact_content_block = app[app.index("function renderEvidence()") : app.index("function renderReport()")]
    assert "function artifactReadableText(item, options = {})" in artifact_content_block
    assert "function artifactDetailsText(details)" in artifact_content_block
    assert "ARTIFACT_TEXT_FIELDS" in artifact_content_block
    assert "stringifyCompact(" not in artifact_content_block
    render_evidence_block = app[app.index("function renderEvidence()") : app.index("function openEvidenceSource")]
    assert "artifactReadableText(item, {" in render_evidence_block
    assert "renderMarkdown(summary)" in render_evidence_block
    render_entities_block = app[app.index("function renderEntities()") : app.index("function renderGraph()")]
    assert "artifactReadableText(item)" in render_entities_block
    assert "renderMarkdown(body)" in render_entities_block
    relation_card_block = app[app.index("function relationCard(relation, index)") : app.index("function renderTimeline()")]
    assert "artifactReadableText(relation)" in relation_card_block
    assert "renderMarkdown(body)" in relation_card_block
    render_timeline_block = app[app.index("function renderTimeline()") : app.index("function renderMap()")]
    assert "function timelineEventBody(item)" in render_timeline_block
    assert "function timelineEventDate(item)" in render_timeline_block
    assert "artifactReadableText(item, {" in render_timeline_block
    assert "'event'" in render_timeline_block
    assert "'key_findings'" in render_timeline_block
    assert "stringifyCompact(item)" not in render_timeline_block
    map_features_block = app[app.index("function mapFeatures(artifacts = state.artifacts)") : app.index("function normalizeGeometry(raw)")]
    assert "summary: artifactReadableText(item)" in map_features_block
    report_text_block = app[app.index("function reportText(section)") : app.index("const REPORT_TEXT_FIELDS")]
    assert "artifactReadableText(section)" in report_text_block
    assert "stringifyCompact(section)" not in report_text_block
    assert "inlineConversationReports ||" in render_messages_block
    assert "!messageBelongsToCurrentTask(item.message)" in render_messages_block
    assert "item.message.kind === 'report' && messageBelongsToCurrentTask(item.message)" in render_messages_block
    restore_workspace_block = app[app.index("function restoreWorkspace()") : app.index("function saveWorkspace()")]
    assert "dom.queryInput.value = state.query" not in restore_workspace_block
    assert LEGACY_AUTH_PREFIX not in app


def test_limira_standalone_frontend_exposes_async_task_stop_and_unread_completion_ui():
    app = _read(LIMIRA_STANDALONE_APP)
    index = _read(LIMIRA_STANDALONE_INDEX)
    styles = _read(LIMIRA_STANDALONE_STYLES)

    assert 'id="submitResearchButton"' in index
    assert 'aria-label="发送"' in index
    assert "const STOP_BUTTON_ICON" in app
    assert "send-stop-icon" in app
    assert "function cancelCurrentTask()" in app
    assert "/cancel" in app
    assert "function isTaskExecutionActive()" in app
    assert "ACTIVE_TASK_REFRESH_INTERVAL_MS" in app
    assert "function ensureActiveTaskRefresh()" in app
    assert "function refreshActiveTaskSnapshot()" in app
    assert "unreadCompletedTaskIds" in app
    assert "historyTaskHasUnreadCompletion(task)" in app
    assert "history-unread-dot" in app
    assert ".send-button.running" in styles
    assert ".history-unread-dot" in styles


def test_limira_standalone_frontend_exposes_real_voice_input_paths():
    app = _read(LIMIRA_STANDALONE_APP)
    index = _read(LIMIRA_STANDALONE_INDEX)
    styles = _read(LIMIRA_STANDALONE_STYLES)

    assert 'id="voiceInputButton"' in index
    assert 'id="voiceAudioInput"' not in index
    assert 'id="voiceInputMessage"' in index
    assert "function toggleVoiceInput()" in app
    assert "function browserLiveVoiceAllowed()" in app
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in app
    assert "navigator.mediaDevices?.getUserMedia && window.MediaRecorder" in app
    assert "new MediaRecorder(stream" in app
    assert "api('/api/limira/speech/transcribe'" in app
    assert "form.append('file', blob" in app
    assert "function transcribeVoiceBlob(blob, filename)" in app
    assert "function transcribeSelectedVoiceAudio()" not in app
    assert ".click();" not in app[app.index("async function toggleVoiceInput()"):app.index("function transcribeVoiceBlob")]
    assert ".voice-input-button.recording" in styles
    assert "unsupported_audio_upload" in app
    assert "speech_transcription_unavailable" in app


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
    assert 'id="downloadArchiveButton"' not in app
    assert "data-archive-download" not in app
    assert "const CONVERSATION_VIEW = '对话';" in app
    assert "const BACK_TO_CHAT_LABEL = '回到对话';" in app
    assert "const tabs = ['证据', '实体', '图谱', '时间线', '地图'];" in app
    assert "state.activeTab = CONVERSATION_VIEW;" in app
    assert "state.activeTab = tabs.includes(button.dataset.tab) ? button.dataset.tab : CONVERSATION_VIEW;" in app
    assert "upsertReportMessage(state.finalReportText);" in app
    assert "upsertReportMessage(reportMarkdown(artifacts));" in app
    assert "function upsertReportMessage(content, options = {})" in app
    assert "function upsertArtifactThinkingStep(artifacts = state.artifacts)" in app
    assert "kind: 'artifact-summary'" in app
    assert "state.activeTab = '报告'" not in app
    assert "dom.artifactTabs.classList.toggle('hidden', conversationView || !surfaceVisible);" in app
    assert "dom.artifactTabs.innerHTML = `<button type=\"button\" class=\"back-tab\" data-tab=\"${CONVERSATION_VIEW}\">${BACK_TO_CHAT_LABEL}</button>`;" in app
    assert "function initialMessages()" in app
    assert "return [];" in app
    assert "function hasConversationActivity()" in app
    assert "function hasCurrentReportMessage()" in app
    assert "!conversationView || !hasConversationActivity() || hasCurrentReportMessage()" in app
    assert "dom.inputContainer?.classList.toggle('hidden', state.route !== 'workspace');" in app
    assert index.index('id="conversationPanel"') < index.index('id="thinkingPanel"')
    assert index.index('id="thinkingPanel"') < index.index('id="reportList"')
    assert index.index('id="reportList"') < index.index('id="artifactTabs"')
    assert index.index('id="artifactTabs"') < index.index('id="artifactContent"')
    assert index.index('id="workspaceContent"') < index.index('id="inputContainer"')
    assert index.index('id="artifactContent"') < index.index('id="inputContainer"')
    assert ".tabs {\n\tz-index: 8;" in styles
    assert ".report-artifact-controls {" in styles
    assert "bottom: 7.15rem;" not in styles
    assert ".input-container {\n\tposition: absolute;" in styles
    assert ".input-container .tabs" not in styles
    assert "function scrollConversationToBottom()" in app
    assert "dom.workspaceContent.scrollTo({\n\t\t\ttop: dom.workspaceContent.scrollHeight," in app
    assert "const top = Math.max(0, dom.conversationPanel.offsetTop - 16);" in app
    assert "dom.workspaceContent.scrollTo({ top, behavior: 'smooth' });" in app
    assert ".conversation-panel.compact .message-list-bottom {\n\tmax-height: none;" in styles
    assert "max-height: 104px;" not in styles
    assert "async function downloadArchive(taskId = currentArtifactTaskId())" in app
    assert "downloadPdfButton" not in app
    assert "downloadPdfButton" not in index


def test_limira_standalone_report_sections_take_precedence_over_cached_final_text():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "const finalCard = !cards && state.finalReportText" in app
    assert "if (sectionText) {" in app
    assert "return sectionText;" in app
    assert "return reportTextFromValue(state.finalReportText, { includeTitle: true });" in app
    assert "const REPORT_SECTION_FIELDS = ['sections', 'report_sections'];" in app
    assert "function embeddedJsonObjectText(text)" in app
    assert "function structuredReportMarkdown(wrapped, options = {})" in app
    assert "parts.push(`# ${title}`);" in app
    assert "parts.push(`## ${heading}\\n\\n${text}`);" in app
    assert "reportTextFromValue(input.text, { includeTitle: true })" in app
    assert (
        "reportTextFromValue(section.markdown || section.content || "
        "section.text || section.summary || section, { includeTitle: false })"
    ) in app


def test_limira_standalone_archive_download_uses_authenticated_fetch():
    app = _read(LIMIRA_STANDALONE_APP)

    assert "dom.downloadArchiveButton = dom.artifactTabs.querySelector('[data-archive-download]');" not in app
    assert "data-archive-download" not in app
    assert "async function downloadArchive(taskId = currentArtifactTaskId())" in app
    assert "void downloadArchive(button.dataset.reportTaskId || '');" in app
    assert "void openReportArtifacts(button.dataset.reportTaskId || '', button.dataset.reportTab || '');" in app
    assert "async function downloadGeneratedArchive(url, filename)" in app
    assert "accept: 'application/zip'" in app
    assert "headers.set('authorization', `Bearer ${state.token}`);" in app
    assert "credentials: 'include'" in app
    assert "empty_archive_download" in app


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
    assert 'id="reportList"' in index
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
