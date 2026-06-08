import json
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
LIMRA_WEB_ROOT = REPO_ROOT / "apps" / "limra-web"
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
