import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
LIMRA_WEB_ROOT = REPO_ROOT / "apps" / "limra-web"
LIMRA_PAGE = LIMRA_WEB_ROOT / "src" / "routes" / "(app)" / "limra" / "+page.svelte"
PACKAGE_JSON = LIMRA_WEB_ROOT / "package.json"
PACKAGE_LOCK = LIMRA_WEB_ROOT / "package-lock.json"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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
        "'/api/limra/research'",
        "`/api/limra/tasks/${id}/events`",
        "`/api/limra/tasks/${id}/artifacts`",
        "`/api/limra/tasks/${taskId}/archive.zip`",
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


def test_limra_artifact_drawer_tabs_and_reference_controls_are_present():
    page = _read(LIMRA_PAGE)

    assert "const artifactTabs: ArtifactTab[] = ['Evidence', 'Entities', 'Graph', 'Timeline', 'Map', 'Report'];" in page
    assert 'role="tablist"' in page
    assert 'role="tab"' in page
    assert "scrollToEvidence" in page
    assert "[{evidenceId(item, index)}]" in page
    assert "[{String(ref)}]" in page


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


def test_limra_graph_and_map_use_required_frontend_libraries_with_empty_states():
    page = _read(LIMRA_PAGE)

    assert "import('cytoscape')" in page
    assert "import('maplibre-gl')" in page
    assert "maplibre-gl/dist/maplibre-gl.css" in page
    assert "Cytoscape.js will render entity and relation artifacts after extraction." in page
    assert "MapLibre GL JS will render timeline and map features once geometry artifacts exist." in page
    assert "bind:this={graphContainer}" in page
    assert "bind:this={mapContainer}" in page


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
