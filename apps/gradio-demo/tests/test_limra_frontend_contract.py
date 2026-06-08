import json
import os
import re
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
BACKEND_ROOT = LIMRA_WEB_ROOT / "backend" / "open_webui"
LIMRA_PAGE = LIMRA_WEB_ROOT / "src" / "routes" / "(app)" / "limra" / "+page.svelte"
SIDEBAR = LIMRA_WEB_ROOT / "src" / "lib" / "components" / "layout" / "Sidebar.svelte"
LIB_ROOT = LIMRA_WEB_ROOT / "src" / "lib"
CONSTANTS = LIB_ROOT / "constants.ts"
APP_HTML = LIMRA_WEB_ROOT / "src" / "app.html"
AUTH_PAGE = LIMRA_WEB_ROOT / "src" / "routes" / "auth" / "+page.svelte"
LAYOUT = LIMRA_WEB_ROOT / "src" / "routes" / "+layout.svelte"
MANIFEST = LIMRA_WEB_ROOT / "backend" / "open_webui" / "static" / "site.webmanifest"
STATIC_MANIFEST = LIMRA_WEB_ROOT / "static" / "static" / "site.webmanifest"
OPENSEARCH = LIMRA_WEB_ROOT / "static" / "opensearch.xml"
BACKEND_MAIN = BACKEND_ROOT / "main.py"
BACKEND_INIT = BACKEND_ROOT / "__init__.py"
BACKEND_OAUTH = BACKEND_ROOT / "utils" / "oauth.py"
BACKEND_AUTOMATIONS = BACKEND_ROOT / "utils" / "automations.py"
BACKEND_AUTH_ROUTER = BACKEND_ROOT / "routers" / "auths.py"
BACKEND_EXTERNAL_WEB_LOADER = BACKEND_ROOT / "retrieval" / "loaders" / "external_web.py"
BACKEND_MISTRAL_LOADER = BACKEND_ROOT / "retrieval" / "loaders" / "mistral.py"
BACKEND_PGVECTOR = BACKEND_ROOT / "retrieval" / "vector" / "dbs" / "pgvector.py"
BACKEND_WEB_RETRIEVAL_FILES = [
    BACKEND_ROOT / "retrieval" / "web" / "external.py",
    BACKEND_ROOT / "retrieval" / "web" / "searxng.py",
    BACKEND_ROOT / "retrieval" / "web" / "yacy.py",
    BACKEND_ROOT / "retrieval" / "web" / "yandex.py",
]
PACKAGE_JSON = LIMRA_WEB_ROOT / "package.json"
PACKAGE_LOCK = LIMRA_WEB_ROOT / "package-lock.json"
LIMRA_SMOKE_SPEC = LIMRA_WEB_ROOT / "tests" / "limra-research-smoke.spec.ts"
USER_VISIBLE_BRAND_SCAN_PATHS = [
    LIMRA_WEB_ROOT / "src" / "routes",
    LIB_ROOT,
    APP_HTML,
    BACKEND_MAIN,
    BACKEND_INIT,
    LIMRA_WEB_ROOT / "backend" / "open_webui" / "constants.py",
    LIMRA_WEB_ROOT / "backend" / "open_webui" / "routers" / "audio.py",
    LIMRA_WEB_ROOT / "backend" / "open_webui" / "routers" / "openai.py",
    BACKEND_OAUTH,
    BACKEND_AUTOMATIONS,
    BACKEND_AUTH_ROUTER,
    BACKEND_EXTERNAL_WEB_LOADER,
    BACKEND_MISTRAL_LOADER,
    BACKEND_PGVECTOR,
    *BACKEND_WEB_RETRIEVAL_FILES,
    MANIFEST,
    STATIC_MANIFEST,
    OPENSEARCH,
]
TEXT_SUFFIXES = {".html", ".json", ".py", ".svelte", ".ts", ".xml"}
VISIBLE_WEBUI_PATTERN = re.compile(r"(?<![A-Z_])\bWebUI\b(?![_A-Z])")
BACKEND_BRAND_PATTERN = re.compile(r"Open WebUI|Open-WebUI|Open_WebUI|OpenWebUI|\bWebUI\b")
BACKEND_COMPAT_BRAND_ALLOWLIST = (
    "X-OpenWebUI-",
    "OpenWebUI-User-",
    "OpenWebUI-File-",
)
LIB_IMPORT_PATTERN = re.compile(r"['\"](\$lib/[^'\"]+)['\"]")
FRONTEND_SOURCE_SUFFIXES = {".svelte", ".ts", ".js"}
LIB_IMPORT_RESOLUTION_SUFFIXES = (".ts", ".js", ".svelte", ".json")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _brand_scan_files():
    files = []
    for path in USER_VISIBLE_BRAND_SCAN_PATHS:
        if path.is_dir():
            files.extend(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file() and candidate.suffix in TEXT_SUFFIXES
            )
        elif path.suffix in TEXT_SUFFIXES:
            files.append(path)
    return sorted(set(files))


def _tracked_files_under(*paths: Path) -> set[Path]:
    relative_paths = [str(path.relative_to(REPO_ROOT)) for path in paths]
    result = subprocess.run(
        ["git", "ls-files", *relative_paths],
        cwd=REPO_ROOT,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
    )
    return {REPO_ROOT / line for line in result.stdout.splitlines() if line}


def _resolve_lib_import(import_path: str) -> Path | None:
    clean_import = import_path.split("?", 1)[0].split("#", 1)[0]
    relative = clean_import.removeprefix("$lib/")
    candidate = LIB_ROOT / relative

    candidates = [candidate]
    candidates.extend(Path(f"{candidate}{suffix}") for suffix in LIB_IMPORT_RESOLUTION_SUFFIXES)
    candidates.extend(candidate / f"index{suffix}" for suffix in LIB_IMPORT_RESOLUTION_SUFFIXES)

    for resolved in candidates:
        if resolved.is_file():
            return resolved
    return None


def _node_executable() -> str | None:
    path_node = shutil.which("node")
    if path_node:
        return path_node
    bundled_node = Path("/tmp/codex-node/node/bin/node")
    if bundled_node.exists():
        return str(bundled_node)
    return None


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_url(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class _RecordingBackendHandler(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_GET(self):
        self.requests.append(self.path)
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"proxied_path": self.path}).encode("utf-8"))

    def log_message(self, *_args):
        return


def test_limra_research_page_exists_inside_authenticated_app_shell():
    assert LIMRA_PAGE.exists()
    page = _read(LIMRA_PAGE)

    assert "<svelte:head>" in page
    assert "<title>limra research</title>" in page
    assert "limra OSINT" in page
    assert "Research workspace" in page
    assert "EventSource" in page
    assert "Download archive" in page


def test_limra_research_page_uses_only_browser_facing_limra_api_paths():
    page = _read(LIMRA_PAGE)

    required_paths = [
        "'/api/limra/scenarios'",
        "'/api/limra/research'",
        "`/api/limra/tasks/${id}/events`",
        "`/api/limra/tasks/${id}/artifacts`",
        "`/api/limra/tasks/${taskId}/archive.zip`",
        "`/api/limra/tasks/${taskId}/reports/pdf`",
        "`/api/limra/tasks/${taskId}/reports/${latestGeneratedReport.report_id}/pdf`",
        "'/api/limra/uploads'",
        "`/api/limra/uploads?task_id=${encodeURIComponent(id)}`",
        "`/api/limra/uploads/search?query=${encodeURIComponent(trimmed)}${taskFilter}`",
        "`/api/limra/uploads/${uploadedDocument.document_id}/download`",
    ]
    for path in required_paths:
        assert path in page

    forbidden_browser_strings = [
        "RUNNER_SERVICE_TOKEN",
        "/mirothinker/",
        "limra-runner:8091",
        "localhost:8091",
    ]
    for forbidden in forbidden_browser_strings:
        assert forbidden not in page


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

        for blocked_path in [
            "/api/v1/auths",
            "/api/config",
            "/mirothinker/tasks/task-a/events",
        ]:
            blocked_status, blocked_body = _read_url(f"{base_url}{blocked_path}")
            assert blocked_status == 404
            assert json.loads(blocked_body)["detail"] == "not_found"

        assert _RecordingBackendHandler.requests == ["/api/limra/scenarios"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()


def test_limra_standalone_frontend_sanitizes_external_links():
    node = _node_executable()
    if not node:
        pytest.skip("node executable is unavailable")

    app = LIMRA_STANDALONE_ROOT / "public" / "app.js"
    script = f"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync({str(app)!r}, 'utf8');
        const storage = new Map();
        const context = {{
            console,
            URL,
            FormData: class FormData {{}},
            Headers: class Headers {{
                set() {{}}
            }},
            EventSource: class EventSource {{}},
            fetch: async () => ({{ ok: true, headers: new Map(), text: async () => '{{}}' }}),
            localStorage: {{
                getItem: (key) => storage.get(key) || '',
                setItem: (key, value) => storage.set(key, String(value)),
                removeItem: (key) => storage.delete(key)
            }},
            window: {{
                location: {{ href: 'http://127.0.0.1/' }},
                setTimeout: () => 0
            }},
            document: {{
                addEventListener: () => {{}},
                querySelectorAll: () => []
            }}
        }};
        vm.createContext(context);
        vm.runInContext(source, context);
        vm.runInContext(`
            this.__limraLinkChecks = {{
                javascriptEvidence: evidenceCard({{
                    evidence_id: 'EVID-JS',
                    title: 'Bad',
                    url: 'javascript:alert(1)'
                }}, 0),
                dataEvidence: evidenceCard({{
                    evidence_id: 'EVID-DATA',
                    title: 'Bad',
                    source_url: 'data:text/html,<script>alert(1)</script>'
                }}, 1),
                httpsEvidence: evidenceCard({{
                    evidence_id: 'EVID-HTTPS',
                    title: 'Good',
                    source_url: 'https://example.test/source?q=1'
                }}, 2),
                markdownLink: renderInlineMarkdown('See https://example.test/report?q=1')
            }};
        `, context);
        process.stdout.write(JSON.stringify(context.__limraLinkChecks));
    """
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    checks = json.loads(result.stdout)

    assert "href=" not in checks["javascriptEvidence"]
    assert "javascript:" not in checks["javascriptEvidence"].lower()
    assert "href=" not in checks["dataEvidence"]
    assert "data:" not in checks["dataEvidence"].lower()
    assert 'href="https://example.test/source?q=1"' in checks["httpsEvidence"]
    assert 'rel="noopener noreferrer"' in checks["httpsEvidence"]
    assert 'href="https://example.test/report?q=1"' in checks["markdownLink"]
    assert 'rel="noopener noreferrer"' in checks["markdownLink"]


def test_limra_standalone_frontend_respects_archive_and_pdf_readiness():
    node = _node_executable()
    if not node:
        pytest.skip("node executable is unavailable")

    app = LIMRA_STANDALONE_ROOT / "public" / "app.js"
    script = f"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync({str(app)!r}, 'utf8');
        const storage = new Map();
        function element() {{
            return {{
                textContent: '',
                disabled: false,
                innerHTML: '',
                scrollTop: 0,
                scrollHeight: 0,
                classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }},
                parentElement: {{ classList: {{ toggle() {{}} }} }},
                addEventListener() {{}},
                querySelectorAll: () => []
            }};
        }}
        const context = {{
            console,
            URL,
            element,
            FormData: class FormData {{}},
            Headers: class Headers {{
                set() {{}}
            }},
            EventSource: class EventSource {{}},
            fetch: async () => ({{ ok: true, headers: new Map(), text: async () => '{{}}' }}),
            localStorage: {{
                getItem: (key) => storage.get(key) || '',
                setItem: (key, value) => storage.set(key, String(value)),
                removeItem: (key) => storage.delete(key)
            }},
            window: {{
                location: {{ href: 'http://127.0.0.1/' }},
                setTimeout: () => 0
            }},
            document: {{
                addEventListener: () => {{}},
                querySelectorAll: () => []
            }}
        }};
        vm.createContext(context);
        vm.runInContext(source, context);
        vm.runInContext(`
            Object.assign(dom, {{
                statusLabel: element(),
                taskLabel: element(),
                submitResearchButton: element(),
                downloadArchiveButton: element(),
                reportMessage: element(),
                exportPdfButton: element(),
                downloadPdfButton: element(),
                messageList: element(),
                eventLog: element(),
                artifactContent: element()
            }});

            state.taskId = 'task-pending';
            state.status = 'running';
            state.archiveStatus = 'pending';
            state.archiveDownloadUrl = '';
            renderStatus();
            const pendingDisabled = dom.downloadArchiveButton.disabled;
            const beforePendingHref = window.location.href;
            downloadArchive();
            const pendingHrefUnchanged = window.location.href === beforePendingHref;
            const pendingMessage = dom.reportMessage.textContent;

            state.archiveStatus = 'failed';
            state.archiveDownloadUrl = '';
            renderStatus();
            const failedDisabled = dom.downloadArchiveButton.disabled;
            const beforeFailedHref = window.location.href;
            downloadArchive();
            const failedHrefUnchanged = window.location.href === beforeFailedHref;
            const failedMessage = dom.reportMessage.textContent;

            handleStreamEvent({{
                event: 'status',
                data: {{
                    status: 'completed',
                    archive_status: 'ready',
                    download_url: '/api/limra/tasks/task-pending/archive.zip'
                }}
            }});
            const readyDisabled = dom.downloadArchiveButton.disabled;
            downloadArchive();
            const readyHref = window.location.href;

            state.taskId = 'task-unsafe';
            state.archiveStatus = 'pending';
            state.archiveDownloadUrl = '';
            handleStreamEvent({{
                event: 'status',
                data: {{
                    archive_status: 'ready',
                    download_url: 'https://evil.test/archive.zip'
                }}
            }});
            const unsafeArchiveUrl = state.archiveDownloadUrl;

            localStorage.setItem(
                STORAGE_KEY,
                JSON.stringify({{
                    taskId: 'task-restore',
                    status: 'completed',
                    archiveStatus: 'ready',
                    archiveDownloadUrl: '/api/limra/tasks/task-restore/archive.zip',
                    latestReport: {{
                        task_id: 'task-other',
                        report_id: 'report-old',
                        pdf_url: '/api/limra/tasks/task-other/reports/report-old/pdf'
                    }}
                }})
            );
            restoreWorkspace();
            const restoredArchiveUrl = state.archiveDownloadUrl;
            const restoredLatestReport = state.latestReport;

            state.taskId = 'task-pdf';
            state.latestReport = normalizeGeneratedReport({{
                task_id: 'task-pdf',
                report_id: 'report-1',
                pdf_url: 'https://evil.test/report.pdf'
            }});
            const pdfFallbackUrl = reportPdfUrl(state.latestReport);
            downloadPdf();
            const pdfHref = window.location.href;

            state.latestReport = normalizeGeneratedReport({{
                task_id: 'task-other',
                report_id: 'report-stale',
                pdf_url: '/api/limra/tasks/task-other/reports/report-stale/pdf'
            }});
            renderReportControls();
            const stalePdfDisabled = dom.downloadPdfButton.disabled;

            this.__limraReadinessChecks = {{
                pendingDisabled,
                pendingHrefUnchanged,
                pendingMessage,
                failedDisabled,
                failedHrefUnchanged,
                failedMessage,
                readyDisabled,
                readyHref,
                unsafeArchiveUrl,
                restoredArchiveUrl,
                restoredLatestReport,
                pdfFallbackUrl,
                pdfHref,
                stalePdfDisabled
            }};
        `, context);
        process.stdout.write(JSON.stringify(context.__limraReadinessChecks));
    """
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    checks = json.loads(result.stdout)

    assert checks["pendingDisabled"] is True
    assert checks["pendingHrefUnchanged"] is True
    assert "尚未" in checks["pendingMessage"]
    assert checks["failedDisabled"] is True
    assert checks["failedHrefUnchanged"] is True
    assert "失败" in checks["failedMessage"]
    assert checks["readyDisabled"] is False
    assert checks["readyHref"] == "/api/limra/tasks/task-pending/archive.zip"
    assert checks["unsafeArchiveUrl"] == "/api/limra/tasks/task-unsafe/archive.zip"
    assert checks["restoredArchiveUrl"] == "/api/limra/tasks/task-restore/archive.zip"
    assert checks["restoredLatestReport"] is None
    assert checks["pdfFallbackUrl"] == "/api/limra/tasks/task-pdf/reports/report-1/pdf"
    assert checks["pdfHref"] == "/api/limra/tasks/task-pdf/reports/report-1/pdf"
    assert checks["stalePdfDisabled"] is True


def test_limra_standalone_frontend_clears_report_downloads_after_failed_restore():
    node = _node_executable()
    if not node:
        pytest.skip("node executable is unavailable")

    app = LIMRA_STANDALONE_ROOT / "public" / "app.js"
    script = f"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync({str(app)!r}, 'utf8');
        const storage = new Map();
        function element() {{
            return {{
                textContent: '',
                disabled: false,
                innerHTML: '',
                scrollTop: 0,
                scrollHeight: 0,
                classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }},
                parentElement: {{ classList: {{ toggle() {{}} }} }},
                addEventListener() {{}},
                querySelectorAll: () => []
            }};
        }}
        const context = {{
            console,
            URL,
            element,
            FormData: class FormData {{}},
            Headers: class Headers {{
                set() {{}}
            }},
            EventSource: class EventSource {{}},
            fetch: async (path) => {{
                if (String(path).startsWith('/api/limra/tasks/deleted-task')) {{
                    return {{
                        ok: false,
                        status: 404,
                        statusText: 'Not Found',
                        headers: {{ get: () => 'application/json' }},
                        text: async () => JSON.stringify({{ detail: 'task_not_found' }})
                    }};
                }}
                return {{
                    ok: true,
                    status: 200,
                    headers: {{ get: () => 'application/json' }},
                    text: async () => JSON.stringify({{ documents: [] }})
                }};
            }},
            localStorage: {{
                getItem: (key) => storage.get(key) || '',
                setItem: (key, value) => storage.set(key, String(value)),
                removeItem: (key) => storage.delete(key)
            }},
            window: {{
                location: {{ href: 'http://127.0.0.1/' }},
                setTimeout: () => 0
            }},
            document: {{
                addEventListener: () => {{}},
                querySelectorAll: () => []
            }}
        }};
        (async () => {{
            vm.createContext(context);
            vm.runInContext(source, context);
            await vm.runInContext(`(async () => {{
                Object.assign(dom, {{
                    statusLabel: element(),
                    taskLabel: element(),
                    submitResearchButton: element(),
                    downloadArchiveButton: element(),
                    reportMessage: element(),
                    exportPdfButton: element(),
                    downloadPdfButton: element(),
                    messageList: element(),
                    eventLog: element(),
                    artifactTabs: element(),
                    artifactContent: element(),
                    uploadList: element(),
                    uploadMessage: element()
                }});

                localStorage.setItem(
                    STORAGE_KEY,
                    JSON.stringify({{
                        taskId: 'deleted-task',
                        status: 'completed',
                        archiveStatus: 'ready',
                        archiveDownloadUrl: '/api/limra/tasks/deleted-task/archive.zip',
                        latestReport: {{
                            task_id: 'deleted-task',
                            report_id: 'old-report',
                            pdf_url: '/api/limra/tasks/deleted-task/reports/old-report/pdf'
                        }},
                        finalReportText: 'stale report body',
                        artifacts: {{
                            report_sections: [{{ title: 'stale', markdown: 'stale markdown' }}]
                        }}
                    }})
                );
                restoreWorkspace();
                renderReportControls();
                const beforeDisabled = dom.downloadPdfButton.disabled;
                const beforePdfUrl = reportPdfUrl(state.latestReport);
                const beforeHref = window.location.href;

                await resumeWorkspace();
                renderReportControls();
                downloadPdf();
                const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));

                this.__limraFailedRestoreChecks = {{
                    beforeDisabled,
                    beforePdfUrl,
                    afterTaskId: state.taskId,
                    afterStatus: state.status,
                    afterLatestReport: state.latestReport,
                    afterFinalReportText: state.finalReportText,
                    afterReportSections: state.artifacts.report_sections.length,
                    afterDisabled: dom.downloadPdfButton.disabled,
                    afterHrefUnchanged: window.location.href === beforeHref,
                    savedTaskId: saved.taskId,
                    savedLatestReport: saved.latestReport,
                    savedFinalReportText: saved.finalReportText,
                    errorMessage: state.messages[state.messages.length - 1].content
                }};
            }})()`, context);
            process.stdout.write(JSON.stringify(context.__limraFailedRestoreChecks));
        }})().catch((error) => {{
            console.error(error);
            process.exit(1);
        }});
    """
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    checks = json.loads(result.stdout)

    assert checks["beforeDisabled"] is False
    assert checks["beforePdfUrl"] == "/api/limra/tasks/deleted-task/reports/old-report/pdf"
    assert checks["afterTaskId"] == ""
    assert checks["afterStatus"] == "ready"
    assert checks["afterLatestReport"] is None
    assert checks["afterFinalReportText"] == ""
    assert checks["afterReportSections"] == 0
    assert checks["afterDisabled"] is True
    assert checks["afterHrefUnchanged"] is True
    assert checks["savedTaskId"] == ""
    assert checks["savedLatestReport"] is None
    assert checks["savedFinalReportText"] == ""
    assert "无法从后端恢复上次任务" in checks["errorMessage"]


def test_limra_standalone_frontend_preserves_cache_after_transient_restore_failure():
    node = _node_executable()
    if not node:
        pytest.skip("node executable is unavailable")

    app = LIMRA_STANDALONE_ROOT / "public" / "app.js"
    script = f"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync({str(app)!r}, 'utf8');
        const storage = new Map();
        function element() {{
            return {{
                textContent: '',
                disabled: false,
                innerHTML: '',
                scrollTop: 0,
                scrollHeight: 0,
                classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }},
                parentElement: {{ classList: {{ toggle() {{}} }} }},
                addEventListener() {{}},
                querySelectorAll: () => []
            }};
        }}
        const context = {{
            console,
            URL,
            element,
            FormData: class FormData {{}},
            Headers: class Headers {{
                set() {{}}
            }},
            EventSource: class EventSource {{}},
            fetch: async (path) => {{
                if (String(path).startsWith('/api/limra/tasks/transient-task')) {{
                    return {{
                        ok: false,
                        status: 500,
                        statusText: 'Server Error',
                        headers: {{ get: () => 'application/json' }},
                        text: async () => JSON.stringify({{ detail: 'temporary' }})
                    }};
                }}
                return {{
                    ok: true,
                    status: 200,
                    headers: {{ get: () => 'application/json' }},
                    text: async () => JSON.stringify({{ documents: [] }})
                }};
            }},
            localStorage: {{
                getItem: (key) => storage.get(key) || '',
                setItem: (key, value) => storage.set(key, String(value)),
                removeItem: (key) => storage.delete(key)
            }},
            window: {{
                location: {{ href: 'http://127.0.0.1/' }},
                setTimeout: () => 0
            }},
            document: {{
                addEventListener: () => {{}},
                querySelectorAll: () => []
            }}
        }};
        (async () => {{
            vm.createContext(context);
            vm.runInContext(source, context);
            await vm.runInContext(`(async () => {{
                Object.assign(dom, {{
                    statusLabel: element(),
                    taskLabel: element(),
                    submitResearchButton: element(),
                    downloadArchiveButton: element(),
                    reportMessage: element(),
                    exportPdfButton: element(),
                    downloadPdfButton: element(),
                    messageList: element(),
                    eventLog: element(),
                    artifactTabs: element(),
                    artifactContent: element(),
                    uploadList: element(),
                    uploadMessage: element()
                }});

                localStorage.setItem(
                    STORAGE_KEY,
                    JSON.stringify({{
                        taskId: 'transient-task',
                        status: 'completed',
                        archiveStatus: 'ready',
                        archiveDownloadUrl: '/api/limra/tasks/transient-task/archive.zip',
                        latestReport: {{
                            task_id: 'transient-task',
                            report_id: 'cached-report',
                            pdf_url: '/api/limra/tasks/transient-task/reports/cached-report/pdf'
                        }},
                        finalReportText: 'recoverable report body',
                        artifacts: {{
                            report_sections: [{{ title: 'cached', markdown: 'cached markdown' }}]
                        }}
                    }})
                );
                restoreWorkspace();
                const beforeHref = window.location.href;

                await resumeWorkspace();
                renderStatus();
                renderReportControls();
                downloadPdf();
                downloadArchive();
                await exportPdf();
                const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));

                this.__limraTransientRestoreChecks = {{
                    restoreBlocked: state.restoreBlocked,
                    taskId: state.taskId,
                    status: state.status,
                    latestReportId: state.latestReport && state.latestReport.report_id,
                    finalReportText: state.finalReportText,
                    reportSections: state.artifacts.report_sections.length,
                    pdfDisabled: dom.downloadPdfButton.disabled,
                    archiveDisabled: dom.downloadArchiveButton.disabled,
                    exportDisabled: dom.exportPdfButton.disabled,
                    hrefUnchanged: window.location.href === beforeHref,
                    savedTaskId: saved.taskId,
                    savedLatestReportId: saved.latestReport && saved.latestReport.report_id,
                    savedFinalReportText: saved.finalReportText,
                    savedReportSections: saved.artifacts.report_sections.length,
                    reportMessage: dom.reportMessage.textContent
                }};
            }})()`, context);
            process.stdout.write(JSON.stringify(context.__limraTransientRestoreChecks));
        }})().catch((error) => {{
            console.error(error);
            process.exit(1);
        }});
    """
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    checks = json.loads(result.stdout)

    assert checks["restoreBlocked"] is True
    assert checks["taskId"] == "transient-task"
    assert checks["status"] == "completed"
    assert checks["latestReportId"] == "cached-report"
    assert checks["finalReportText"] == "recoverable report body"
    assert checks["reportSections"] == 1
    assert checks["pdfDisabled"] is True
    assert checks["archiveDisabled"] is True
    assert checks["exportDisabled"] is True
    assert checks["hrefUnchanged"] is True
    assert checks["savedTaskId"] == "transient-task"
    assert checks["savedLatestReportId"] == "cached-report"
    assert checks["savedFinalReportText"] == "recoverable report body"
    assert checks["savedReportSections"] == 1
    assert "暂未从后端确认" in checks["reportMessage"]


def test_limra_standalone_frontend_clears_task_state_on_sign_out():
    node = _node_executable()
    if not node:
        pytest.skip("node executable is unavailable")

    app = LIMRA_STANDALONE_ROOT / "public" / "app.js"
    script = f"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync({str(app)!r}, 'utf8');
        const storage = new Map();
        function element() {{
            return {{
                textContent: '',
                disabled: false,
                innerHTML: '',
                value: '',
                autocomplete: '',
                scrollTop: 0,
                scrollHeight: 0,
                classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }},
                parentElement: {{ classList: {{ toggle() {{}} }} }},
                addEventListener() {{}},
                querySelectorAll: () => []
            }};
        }}
        const context = {{
            console,
            URL,
            element,
            FormData: class FormData {{}},
            Headers: class Headers {{
                set() {{}}
            }},
            EventSource: class EventSource {{}},
            fetch: async () => ({{
                ok: true,
                status: 200,
                headers: {{ get: () => 'application/json' }},
                text: async () => JSON.stringify({{ ok: true }})
            }}),
            localStorage: {{
                getItem: (key) => storage.get(key) || '',
                setItem: (key, value) => storage.set(key, String(value)),
                removeItem: (key) => storage.delete(key)
            }},
            window: {{
                location: {{ href: 'http://127.0.0.1/' }},
                setTimeout: () => 0
            }},
            document: {{
                addEventListener: () => {{}},
                querySelectorAll: () => []
            }}
        }};
        (async () => {{
            vm.createContext(context);
            vm.runInContext(source, context);
            await vm.runInContext(`(async () => {{
                Object.assign(dom, {{
                    authPanel: element(),
                    workspace: element(),
                    signOutButton: element(),
                    sessionLabel: element(),
                    signinModeButton: element(),
                    signupModeButton: element(),
                    nameInput: element(),
                    authSubmitButton: element(),
                    passwordInput: element(),
                    scenarioSelect: element(),
                    scenarioDetails: element(),
                    statusLabel: element(),
                    taskLabel: element(),
                    submitResearchButton: element(),
                    downloadArchiveButton: element(),
                    reportMessage: element(),
                    exportPdfButton: element(),
                    downloadPdfButton: element(),
                    messageList: element(),
                    eventLog: element(),
                    artifactTabs: element(),
                    artifactContent: element(),
                    uploadList: element(),
                    uploadMessage: element()
                }});

                localStorage.setItem('limraToken', 'old-token');
                localStorage.setItem('token', 'old-token');
                localStorage.setItem(
                    STORAGE_KEY,
                    JSON.stringify({{
                        taskId: 'old-task',
                        latestReport: {{
                            task_id: 'old-task',
                            report_id: 'old-report',
                            pdf_url: '/api/limra/tasks/old-task/reports/old-report/pdf'
                        }}
                    }})
                );
                state.user = {{ id: 'old-user', email: 'old@example.test', role: 'user' }};
                state.token = 'old-token';
                state.savedUserId = 'old-user';
                state.taskId = 'old-task';
                state.status = 'completed';
                state.archiveStatus = 'ready';
                state.archiveDownloadUrl = '/api/limra/tasks/old-task/archive.zip';
                state.latestReport = {{
                    task_id: 'old-task',
                    report_id: 'old-report',
                    pdf_url: '/api/limra/tasks/old-task/reports/old-report/pdf'
                }};
                state.finalReportText = 'old report';
                state.artifacts = {{
                    evidence: [{{ title: 'old evidence' }}],
                    entities: [],
                    relations: [],
                    timeline_events: [],
                    map_features: [],
                    report_sections: [{{ title: 'old', markdown: 'old markdown' }}]
                }};
                state.uploads = [{{ document_id: 'old-document', filename: 'old.txt' }}];
                state.uploadResults = [{{ document_id: 'old-result', filename: 'old-result.txt' }}];
                state.restoreBlocked = true;

                await signOut();

                this.__limraSignOutChecks = {{
                    user: state.user,
                    token: state.token,
                    savedUserId: state.savedUserId,
                    taskId: state.taskId,
                    status: state.status,
                    archiveStatus: state.archiveStatus,
                    archiveDownloadUrl: state.archiveDownloadUrl,
                    restoreBlocked: state.restoreBlocked,
                    latestReport: state.latestReport,
                    finalReportText: state.finalReportText,
                    reportSections: state.artifacts.report_sections.length,
                    uploads: state.uploads.length,
                    uploadResults: state.uploadResults.length,
                    storageWorkspace: localStorage.getItem(STORAGE_KEY),
                    storageLimraToken: localStorage.getItem('limraToken'),
                    storageToken: localStorage.getItem('token')
                }};
            }})()`, context);
            process.stdout.write(JSON.stringify(context.__limraSignOutChecks));
        }})().catch((error) => {{
            console.error(error);
            process.exit(1);
        }});
    """
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    checks = json.loads(result.stdout)

    assert checks["user"] is None
    assert checks["token"] == ""
    assert checks["savedUserId"] == ""
    assert checks["taskId"] == ""
    assert checks["status"] == "ready"
    assert checks["archiveStatus"] == "pending"
    assert checks["archiveDownloadUrl"] == ""
    assert checks["restoreBlocked"] is False
    assert checks["latestReport"] is None
    assert checks["finalReportText"] == ""
    assert checks["reportSections"] == 0
    assert checks["uploads"] == 0
    assert checks["uploadResults"] == 0
    assert checks["storageWorkspace"] == ""
    assert checks["storageLimraToken"] == ""
    assert checks["storageToken"] == ""


def test_limra_standalone_frontend_ignores_inflight_upload_results_after_sign_out():
    node = _node_executable()
    if not node:
        pytest.skip("node executable is unavailable")

    app = LIMRA_STANDALONE_ROOT / "public" / "app.js"
    script = f"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync({str(app)!r}, 'utf8');
        const storage = new Map();
        let resolveOldSearch;
        let resolveOldUploads;
        const oldSearchPromise = new Promise((resolve) => {{
            resolveOldSearch = resolve;
        }});
        const oldUploadsPromise = new Promise((resolve) => {{
            resolveOldUploads = resolve;
        }});
        function jsonResponse(body) {{
            return {{
                ok: true,
                status: 200,
                headers: {{ get: () => 'application/json' }},
                text: async () => JSON.stringify(body)
            }};
        }}
        function element() {{
            return {{
                textContent: '',
                disabled: false,
                innerHTML: '',
                value: '',
                autocomplete: '',
                scrollTop: 0,
                scrollHeight: 0,
                classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }},
                parentElement: {{ classList: {{ toggle() {{}} }} }},
                addEventListener() {{}},
                querySelectorAll: () => []
            }};
        }}
        const context = {{
            console,
            URL,
            element,
            FormData: class FormData {{}},
            Headers: class Headers {{
                set() {{}}
            }},
            EventSource: class EventSource {{}},
            fetch: async (path) => {{
                const text = String(path);
                if (text.startsWith('/api/limra/uploads/search')) {{
                    return oldSearchPromise.then(jsonResponse);
                }}
                if (text.startsWith('/api/limra/uploads?task_id=old-task')) {{
                    return oldUploadsPromise.then(jsonResponse);
                }}
                if (text.startsWith('/api/limra/uploads')) {{
                    return jsonResponse({{
                        documents: [
                            {{
                                document_id: 'new-doc',
                                filename: 'new-user.txt',
                                content_type: 'text/plain',
                                byte_size: 7
                            }}
                        ]
                    }});
                }}
                return jsonResponse({{ ok: true }});
            }},
            resolveOldSearch,
            resolveOldUploads,
            localStorage: {{
                getItem: (key) => storage.get(key) || '',
                setItem: (key, value) => storage.set(key, String(value)),
                removeItem: (key) => storage.delete(key)
            }},
            window: {{
                location: {{ href: 'http://127.0.0.1/' }},
                setTimeout: () => 0
            }},
            document: {{
                addEventListener: () => {{}},
                querySelectorAll: () => []
            }}
        }};
        (async () => {{
            vm.createContext(context);
            vm.runInContext(source, context);
            await vm.runInContext(`(async () => {{
                Object.assign(dom, {{
                    authPanel: element(),
                    workspace: element(),
                    signOutButton: element(),
                    sessionLabel: element(),
                    signinModeButton: element(),
                    signupModeButton: element(),
                    nameInput: element(),
                    authSubmitButton: element(),
                    passwordInput: element(),
                    scenarioSelect: element(),
                    scenarioDetails: element(),
                    statusLabel: element(),
                    taskLabel: element(),
                    submitResearchButton: element(),
                    downloadArchiveButton: element(),
                    reportMessage: element(),
                    exportPdfButton: element(),
                    downloadPdfButton: element(),
                    messageList: element(),
                    eventLog: element(),
                    artifactTabs: element(),
                    artifactContent: element(),
                    uploadSearchInput: element(),
                    uploadInput: element(),
                    uploadList: element(),
                    uploadMessage: element()
                }});

                setUser({{ id: 'old-user', email: 'old@example.test', role: 'user', token: 'old-token' }});
                state.savedUserId = 'old-user';
                state.taskId = 'old-task';
                dom.uploadSearchInput.value = 'old';

                const searchRun = searchUploads();
                const uploadsRun = loadUploads();
                await Promise.resolve();
                await signOut();

                const afterSignOutUser = state.user;
                const afterSignOutResults = state.uploadResults.length;
                const afterSignOutUploads = state.uploads.length;

                resolveOldSearch({{
                    documents: [
                        {{
                            document_id: 'old-search-doc',
                            filename: 'old-user-search.txt',
                            content_type: 'text/plain',
                            byte_size: 5
                        }}
                    ]
                }});
                resolveOldUploads({{
                    documents: [
                        {{
                            document_id: 'old-list-doc',
                            filename: 'old-user-list.txt',
                            content_type: 'text/plain',
                            byte_size: 6
                        }}
                    ]
                }});
                await searchRun;
                await uploadsRun;

                const staleHtml = dom.uploadList.innerHTML;
                const afterLateUser = state.user;
                const afterLateResults = state.uploadResults.length;
                const afterLateUploads = state.uploads.length;

                setUser({{ id: 'new-user', email: 'new@example.test', role: 'user', token: 'new-token' }});
                await loadUploads();
                const newHtml = dom.uploadList.innerHTML;

                this.__limraInflightUploadChecks = {{
                    afterSignOutUser,
                    afterSignOutResults,
                    afterSignOutUploads,
                    afterLateUser,
                    afterLateResults,
                    afterLateUploads,
                    staleHtml,
                    newUploadResults: state.uploadResults.length,
                    newUploads: state.uploads.length,
                    newHtml
                }};
            }})()`, context);
            process.stdout.write(JSON.stringify(context.__limraInflightUploadChecks));
        }})().catch((error) => {{
            console.error(error);
            process.exit(1);
        }});
    """
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    checks = json.loads(result.stdout)

    assert checks["afterSignOutUser"] is None
    assert checks["afterSignOutResults"] == 0
    assert checks["afterSignOutUploads"] == 0
    assert checks["afterLateUser"] is None
    assert checks["afterLateResults"] == 0
    assert checks["afterLateUploads"] == 0
    assert "old-user-search.txt" not in checks["staleHtml"]
    assert "old-user-list.txt" not in checks["staleHtml"]
    assert "/api/limra/uploads/old-search-doc/download" not in checks["staleHtml"]
    assert "/api/limra/uploads/old-list-doc/download" not in checks["staleHtml"]
    assert checks["newUploadResults"] == 0
    assert checks["newUploads"] == 1
    assert "new-user.txt" in checks["newHtml"]
    assert "/api/limra/uploads/new-doc/download" in checks["newHtml"]
    assert "old-user-search.txt" not in checks["newHtml"]


def test_limra_research_page_has_demo_scenario_selector():
    page = _read(LIMRA_PAGE)

    assert "type LimraScenario" in page
    assert "const loadScenarios = async () =>" in page
    assert "'/api/limra/scenarios'" in page
    assert 'id="limra-scenario"' in page
    assert "bind:value={selectedScenario}" in page
    assert "selectedScenarioDetail" in page
    assert "Use scenario query" in page
    assert "scenario: selectedScenario || undefined" in page
    assert "osint-mvp" not in page


def test_limra_research_page_has_report_pdf_export_controls():
    page = _read(LIMRA_PAGE)

    assert "type GeneratedReport" in page
    assert "let latestGeneratedReport: GeneratedReport | null = null;" in page
    assert "const exportReportPdf = async () =>" in page
    assert "`/api/limra/tasks/${taskId}/reports/pdf`" in page
    assert "report_id: `ui-${Date.now()}`" in page
    assert "report_type: 'final'" in page
    assert "markdown: buildReportMarkdown()" in page
    assert "evidence_refs: reportEvidenceRefs()" in page
    assert "html:" not in page
    assert "object_key" not in page
    assert "pdf_object_key" not in page
    assert "const downloadGeneratedReportPdf = () =>" in page
    assert "`/api/limra/tasks/${taskId}/reports/${latestGeneratedReport.report_id}/pdf`" in page
    assert "Export PDF" in page
    assert "Download PDF" in page
    assert "disabled={!taskId || isExportingReport || artifacts.report_sections.length === 0}" in page
    assert "disabled={!latestGeneratedReport?.report_id}" in page


def test_limra_research_page_has_uploaded_document_controls():
    page = _read(LIMRA_PAGE)

    assert "type UploadedDocument" in page
    assert "type UploadedDocumentSearchResult" in page
    assert "let uploadedDocuments: UploadedDocument[] = [];" in page
    assert "let uploadSearchResults: UploadedDocumentSearchResult[] = [];" in page
    assert "let uploadSearchQuery = '';" in page
    assert "let selectedUploadFile: File | null = null;" in page
    assert "const loadUploadedDocuments = async (id = taskId) =>" in page
    assert "`/api/limra/uploads?task_id=${encodeURIComponent(id)}`" in page
    assert "const searchUploadedDocuments = async () =>" in page
    assert "`/api/limra/uploads/search?query=${encodeURIComponent(trimmed)}${taskFilter}`" in page
    assert "const selectUploadFile = (event: Event) =>" in page
    assert "const uploadDocument = async () =>" in page
    assert "const formData = new FormData();" in page
    assert "formData.append('file', selectedUploadFile);" in page
    assert "formData.append('task_id', taskId);" in page
    assert "fetch('/api/limra/uploads'" in page
    assert "headers: {" not in page.split("const uploadDocument = async () =>", 1)[1].split(
        "const uploadedDocumentDownloadUrl", 1
    )[0]
    assert "const uploadedDocumentDownloadUrl = (uploadedDocument: UploadedDocument) =>" in page
    assert "`/api/limra/uploads/${uploadedDocument.document_id}/download`" in page
    assert 'id="limra-upload"' in page
    assert "Upload document" in page
    assert "Refresh uploads" in page
    assert "selectedUploadFile || isUploadingDocument" in page
    assert "uploadedDocument.download_url?.startsWith('/api/limra/uploads/')" in page
    assert 'id="limra-upload-search"' in page
    assert "Search uploads" in page
    assert "uploadSearchResults.length > 0" in page
    assert "user_id" not in page
    assert "owner_user_id" not in page


def test_limra_playwright_smoke_harness_covers_streamed_artifact_refresh_and_map_geometry():
    package = json.loads(_read(PACKAGE_JSON))
    spec = _read(LIMRA_SMOKE_SPEC)

    assert (
        package["scripts"]["test:limra-smoke"]
        == "npx -y @playwright/test@1.58.0 test tests/limra-research-smoke.spec.ts"
    )
    assert "import { expect, test } from '@playwright/test';" in spec
    assert "process.env.LIMRA_WEB_BASE_URL ?? 'http://127.0.0.1:5173'" in spec
    assert "const smokeAuthToken = 'limra-smoke-token';" in spec
    assert "const smokeBackendConfig = {" in spec
    assert "const smokeSessionUser = {" in spec
    assert "localStorage.setItem('token', 'limra-smoke-token');" in spec
    assert "(localStorage as Storage & { token: string }).token = 'limra-smoke-token';" in spec
    assert "class FakeEventSource" in spec
    assert "window.__limraFakeEventSource" in spec

    required_open_webui_bootstrap_routes = [
        "'**/api/config'",
        "'**/api/v1/auths/'",
        "'**/api/v1/auths/update/timezone'",
        "'**/api/v1/users/user/settings'",
        "'**/api/models**'",
        "'**/api/v1/configs/banners'",
        "'**/api/v1/tools/'",
        "'**/api/v1/terminals/'",
    ]
    for route in required_open_webui_bootstrap_routes:
        assert route in spec

    assert "enable_websocket: false" in spec
    assert "enable_direct_connections: false" in spec
    assert "role: 'user'" in spec

    required_limra_routes = [
        "'**/api/limra/scenarios'",
        "'**/api/limra/uploads**'",
        "'**/api/limra/research'",
        "'**/api/limra/tasks/task-smoke'",
        "'**/api/limra/tasks/task-smoke/artifacts'",
        "'/api/limra/research'",
        "'/api/limra/tasks/task-smoke/events'",
        "'/api/limra/tasks/task-smoke/artifacts'",
    ]
    for route in required_limra_routes:
        assert route in spec

    for event_type in [
        "'relation_extracted'",
        "'map_feature_added'",
        "'verification_result'",
    ]:
        assert event_type in spec

    for geometry_type in ["type: 'Point'", "type: 'LineString'", "type: 'Polygon'"]:
        assert geometry_type in spec

    assert "artifactLoadCount" in spec
    assert "toBeGreaterThanOrEqual(1 + streamedArtifactEvents.length)" in spec
    assert "privateRunnerUrlFragments" in spec
    assert "requestedUrls.some((url) => url.includes(forbidden))" in spec
    assert "limra-runner:8091" in spec
    assert "localhost:8091" in spec
    assert "RUNNER_SERVICE_TOKEN" in spec
    assert "/mirothinker/" in spec
    assert "'**/mirothinker/" not in spec
    assert "'**/api/runner" not in spec


def test_limra_artifact_drawer_tabs_and_reference_controls_are_present():
    page = _read(LIMRA_PAGE)

    assert "const artifactTabs: ArtifactTab[] = ['Evidence', 'Entities', 'Graph', 'Timeline', 'Map', 'Report'];" in page
    assert 'role="tablist"' in page
    assert 'role="tab"' in page
    assert "scrollToEvidence" in page
    assert "[{evidenceId(item, index)}]" in page
    assert "[{String(ref)}]" in page


def test_limra_sidebar_navigation_is_first_class_authenticated_entry():
    sidebar = _read(SIDEBAR)

    assert "const DEFAULT_PINNED_ITEMS = ['limra', 'notes', 'workspace'];" in sidebar
    assert "ensureLimraPinned($settings?.pinnedMenuItems ?? DEFAULT_PINNED_ITEMS)" in sidebar
    assert "const ensureLimraPinned = (items) => (items.includes('limra') ? items : ['limra', ...items]);" in sidebar
    assert "case 'limra':" in sidebar
    assert "limra: { label: 'limra', href: '/limra', iconType: 'limra' }" in sidebar
    assert 'id="sidebar-{itemId}-button"' in sidebar
    assert "href={meta.href}" in sidebar
    assert "goto(meta.href);" in sidebar


def test_reviewed_user_visible_brand_surfaces_use_limra():
    app_html = _read(APP_HTML)
    auth = _read(AUTH_PAGE)
    layout = _read(LAYOUT)
    constants = _read(CONSTANTS)
    manifest_text = _read(MANIFEST)
    manifest = json.loads(manifest_text)
    static_manifest_text = _read(STATIC_MANIFEST)
    static_manifest = json.loads(static_manifest_text)
    opensearch = _read(OPENSEARCH)
    sidebar = _read(SIDEBAR)

    assert "<title>limra</title>" in app_html
    assert manifest["name"] == "limra"
    assert manifest["short_name"] == "limra"
    assert static_manifest["name"] == "limra"
    assert static_manifest["short_name"] == "limra"
    assert "<ShortName>limra</ShortName>" in opensearch
    assert "<Description>Search limra</Description>" in opensearch
    assert "export const APP_NAME = 'limra';" in constants
    assert " • limra" in layout

    assert "<title>{$WEBUI_NAME}</title>" in layout
    assert 'title={$WEBUI_NAME}' in layout
    assert 'content={$WEBUI_NAME}' in layout
    assert "<title>\n\t\t{`${$WEBUI_NAME}`}\n\t</title>" in auth
    assert "Signing in to {{WEBUI_NAME}}" in auth
    assert "Sign in to {{WEBUI_NAME}}" in auth
    assert "Sign up to {{WEBUI_NAME}}" in auth
    assert 'alt="{$WEBUI_NAME} logo"' in auth
    assert 'id="sidebar-webui-name"' in sidebar
    assert "{$WEBUI_NAME}" in sidebar

    for source in [app_html, auth, layout, manifest_text, static_manifest_text, opensearch, sidebar]:
        assert "Open WebUI" not in source
        assert "limra (Open WebUI)" not in source
    for served_asset in [manifest_text, static_manifest_text, opensearch]:
        assert "WebUI" not in served_asset


def test_user_visible_runtime_brand_sources_do_not_expose_open_webui():
    violations = []
    for path in _brand_scan_files():
        source = _read(path)
        if (
            "Open WebUI" in source
            or "Open-WebUI" in source
            or "Open_WebUI" in source
            or "OpenWebUI" in source
            or VISIBLE_WEBUI_PATTERN.search(source)
        ):
            violations.append(str(path.relative_to(REPO_ROOT)))

    assert violations == []


def test_tracked_limra_routes_only_import_tracked_lib_sources():
    tracked_files = _tracked_files_under(LIMRA_WEB_ROOT / "src" / "routes", LIB_ROOT)
    scanned_files = sorted(path for path in tracked_files if path.suffix in FRONTEND_SOURCE_SUFFIXES)

    assert LIB_ROOT / "stores" / "index.ts" in tracked_files
    assert LIB_ROOT / "utils" / "index.ts" in tracked_files
    assert LIB_ROOT / "i18n" / "index.ts" in tracked_files
    assert LIB_ROOT / "components" / "common" / "Spinner.svelte" in tracked_files
    assert LIB_ROOT / "components" / "icons" / "Plus.svelte" in tracked_files

    unresolved = []
    untracked = []
    for path in scanned_files:
        for match in LIB_IMPORT_PATTERN.finditer(_read(path)):
            import_path = match.group(1)
            resolved = _resolve_lib_import(import_path)
            if resolved is None:
                unresolved.append(
                    f"{path.relative_to(REPO_ROOT)} imports {import_path}, which does not resolve"
                )
            elif resolved not in tracked_files:
                untracked.append(
                    f"{path.relative_to(REPO_ROOT)} imports {import_path} -> "
                    f"{resolved.relative_to(REPO_ROOT)}, which is not tracked"
                )

    assert unresolved == []
    assert untracked == []


def test_runtime_backend_metadata_uses_limra_brand():
    backend_init = _read(BACKEND_INIT)
    backend_main = _read(BACKEND_MAIN)
    backend_oauth = _read(BACKEND_OAUTH)
    backend_automations = _read(BACKEND_AUTOMATIONS)
    backend_auth = _read(BACKEND_AUTH_ROUTER)
    backend_external_web_loader = _read(BACKEND_EXTERNAL_WEB_LOADER)
    backend_mistral_loader = _read(BACKEND_MISTRAL_LOADER)
    backend_pgvector = _read(BACKEND_PGVECTOR)
    backend_web_retrieval = "\n".join(_read(path) for path in BACKEND_WEB_RETRIEVAL_FILES)

    assert "typer.echo(f'limra version: {VERSION}')" in backend_init
    assert "print(f'limra v{VERSION} - building the best AI user interface." in backend_main
    assert "All models configured in limra are accessible via this endpoint." in backend_main
    assert "Get current usage statistics for limra." in backend_main
    assert "client_name='limra'" in backend_oauth
    assert "getattr(app.state, 'WEBUI_NAME', 'limra')" in backend_automations
    assert "before restarting limra." in backend_pgvector
    assert "limra (https://github.com/open-webui/open-webui) External Web Loader" in backend_external_web_loader
    assert "limra-MistralLoader/2.0" in backend_mistral_loader
    assert "limra (https://github.com/open-webui/open-webui) RAG Bot" in backend_web_retrieval
    assert "Exchange an external OAuth provider token for a limra JWT." in backend_auth


def test_backend_brand_references_are_only_internal_compatibility_identifiers():
    violations = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            if not BACKEND_BRAND_PATTERN.search(line):
                continue
            if any(allowed in line for allowed in BACKEND_COMPAT_BRAND_ALLOWLIST):
                continue
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}")

    assert violations == []


def test_limra_stream_handler_reads_nested_status_and_closes_terminal_events():
    page = _read(LIMRA_PAGE)

    assert "const eventPayload = taskEvent.payload && typeof taskEvent.payload === 'object' ? taskEvent.payload : {};" in page
    assert "const nextStatus = taskEvent.status ?? eventPayload.status;" in page
    assert "eventPayload.message" in page
    assert "eventPayload.summary" in page
    assert "const terminalStatuses = new Set(['completed', 'failed', 'cancelled']);" in page
    assert "if (isTerminalStatus(nextStatus))" in page
    assert "eventSource?.close();" in page
    assert "eventSource = null;" in page
    assert "void refreshTask(id);" in page


def test_limra_stream_handler_refreshes_all_first_class_artifact_events():
    page = _read(LIMRA_PAGE)

    assert "const artifactRefreshEventTypes = new Set([" in page
    for event_type in [
        "'evidence_collected'",
        "'entity_extracted'",
        "'relation_extracted'",
        "'timeline_event_added'",
        "'map_feature_added'",
        "'verification_result'",
        "'report_section_generated'",
    ]:
        assert event_type in page

    assert "const isArtifactEvent = (eventType: string) => artifactRefreshEventTypes.has(eventType);" in page
    assert "if (isArtifactEvent(eventType))" in page
    assert page.count("void loadArtifacts(id);") >= 2
    assert "eventType.includes('evidence')" not in page
    assert "eventType.includes('entity')" not in page
    assert "eventType.includes('timeline')" not in page
    assert "eventType.includes('report')" not in page


def test_limra_graph_and_map_use_required_frontend_libraries_with_empty_states():
    page = _read(LIMRA_PAGE)

    assert "import('cytoscape')" in page
    assert "import('maplibre-gl')" in page
    assert "maplibre-gl/dist/maplibre-gl.css" in page
    assert "Cytoscape.js will render entity and relation artifacts after extraction." in page
    assert "MapLibre GL JS will render timeline and map features once geometry artifacts exist." in page
    assert "bind:this={graphContainer}" in page
    assert "bind:this={mapContainer}" in page


def test_limra_map_panel_renders_point_line_and_polygon_geometry_layers():
    page = _read(LIMRA_PAGE)

    assert "const supportedMapGeometryTypes = new Set([" in page
    for geometry_type in [
        "'Point'",
        "'MultiPoint'",
        "'LineString'",
        "'MultiLineString'",
        "'Polygon'",
        "'MultiPolygon'",
    ]:
        assert geometry_type in page

    assert "const normalizeMapGeometry = (rawGeometry: unknown) =>" in page
    assert "const collectCoordinatePairs = (coordinates: unknown): [number, number][] =>" in page
    assert "item.geometry ?? item.payload?.geometry ?? item.geojson ?? item.payload?.geojson" in page
    assert "id: 'limra-polygons'" in page
    assert "type: 'fill'" in page
    assert "id: 'limra-lines'" in page
    assert "type: 'line'" in page
    assert "id: 'limra-points'" in page
    assert "type: 'circle'" in page
    assert "['==', ['geometry-type'], 'LineString']" in page
    assert "['==', ['geometry-type'], 'MultiLineString']" in page
    assert "['==', ['geometry-type'], 'Polygon']" in page
    assert "['==', ['geometry-type'], 'MultiPolygon']" in page
    assert "['==', ['geometry-type'], 'Point']" in page
    assert "['==', ['geometry-type'], 'MultiPoint']" in page


def test_limra_web_declares_graph_and_map_dependencies_for_docker_build():
    package = json.loads(_read(PACKAGE_JSON))
    lock = json.loads(_read(PACKAGE_LOCK))

    deps = package["dependencies"]
    root_lock_deps = lock["packages"][""]["dependencies"]
    packages = lock["packages"]

    assert deps["cytoscape"].startswith("^3.")
    assert deps["maplibre-gl"].startswith("^5.")
    assert root_lock_deps["cytoscape"] == deps["cytoscape"]
    assert root_lock_deps["maplibre-gl"] == deps["maplibre-gl"]
    assert "node_modules/cytoscape" in packages
    assert "node_modules/maplibre-gl" in packages

    for dependency_name in packages["node_modules/maplibre-gl"]["dependencies"]:
        assert f"node_modules/{dependency_name}" in packages

    assert "node_modules/@maplibre/vt-pbf/node_modules/pbf" in packages
    assert "node_modules/resolve-protobuf-schema" in packages
    assert "node_modules/protocol-buffers-schema" in packages
