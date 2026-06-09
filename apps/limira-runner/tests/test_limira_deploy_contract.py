import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = ROOT / "docker-compose.limira.yml"
ENV_EXAMPLE = ROOT / ".env.example"
LIMIRA_NATIVE_APP = ROOT / "apps/limira-web/backend/limira_native.py"
LIMIRA_BACKEND_ROUTER = ROOT / "apps/limira-web/backend/limira_backend/routers/limira.py"
RUNNER_API = ROOT / "apps/limira-runner/runner_api.py"
MIGRATION_FILE = (
    ROOT / "deploy/limira/postgres/migrations/001_limira_osint_schema.sql"
)
LEGACY_PY_PACKAGE = "open" + "_" + "web" + "ui"
LEGACY_APP_DIR = "open-" + "web" + "ui-limira-runner"
LEGACY_WEB_NAME_KEY = "WEB" + "UI_NAME"


def test_limira_compose_defines_stack_contract():
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]

    expected_services = {
        "limira-web",
        "limira-runner",
        "postgres",
        "redis",
        "minio",
        "minio-init",
        "reverse-proxy",
    }
    assert expected_services.issubset(services)

    assert _published_port(services["limira-web"]) == "3001"
    assert _published_port(services["limira-runner"]) == "8091"
    assert _published_port(services["postgres"]) == "5433"
    assert _published_port(services["redis"]) == "6380"
    assert _published_ports(services["minio"]) == {"9002", "9003"}
    for service_name in ("limira-web", "limira-runner", "postgres", "redis", "minio"):
        assert _published_bind_hosts(services[service_name]) == {"127.0.0.1"}

    assert "healthcheck" in services["postgres"]
    assert "healthcheck" in services["redis"]
    assert "healthcheck" in services["minio"]
    assert "healthcheck" in services["limira-runner"]
    assert services["minio-init"]["depends_on"]["minio"]["condition"] == (
        "service_healthy"
    )
    assert services["limira-web"]["depends_on"]["limira-runner"]["condition"] == (
        "service_healthy"
    )

    postgres = services["postgres"]
    assert postgres["build"]["dockerfile"] == "deploy/limira/postgres.Dockerfile"
    assert any(
        "deploy/limira/postgres/migrations" in volume
        for volume in postgres["volumes"]
    )

    runner = services["limira-runner"]
    assert runner["build"]["dockerfile"] == "deploy/limira/runner.Dockerfile"
    assert runner["environment"]["LIMIRA_RUNNER_INTERNAL_PORT"] == "8091"
    assert "${RUNNER_SERVICE_TOKEN" in runner["environment"]["RUNNER_SERVICE_TOKEN"]
    assert runner["environment"]["RUNNER_TASK_STORE_BACKEND"] == "postgres"
    assert runner["environment"]["RUNNER_DATABASE_URL"].startswith("postgresql://")
    assert "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}" in runner["environment"][
        "RUNNER_DATABASE_URL"
    ]
    assert runner["environment"]["AWS_SECRET_ACCESS_KEY"] == (
        "${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD}"
    )
    assert runner["environment"]["BASE_URL"] == (
        "${BASE_URL:-https://api.deepseek.com}"
    )
    assert runner["environment"]["DEFAULT_LLM_PROVIDER"] == (
        "${DEFAULT_LLM_PROVIDER:-openai}"
    )
    assert runner["environment"]["DEFAULT_MODEL_NAME"] == (
        "${DEFAULT_MODEL_NAME:-deepseek-v4-pro}"
    )
    assert runner["environment"]["SUMMARY_LLM_BASE_URL"] == (
        "${SUMMARY_LLM_BASE_URL:-https://api.deepseek.com/chat/completions}"
    )
    assert runner["environment"]["SUMMARY_LLM_MODEL_NAME"] == (
        "${SUMMARY_LLM_MODEL_NAME:-deepseek-v4-pro}"
    )
    assert "RUNNER_ALLOW_SQLITE_TASK_STORE" not in runner["environment"]

    web = services["limira-web"]
    assert web["build"]["context"] == "apps/limira-web"
    assert web["build"]["dockerfile"] == "Dockerfile"
    assert web["command"] == [
        "python",
        "-m",
        "uvicorn",
        "limira_native:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
    ]
    assert "image" not in web
    assert web["environment"]["PORT"] == "8080"
    assert LEGACY_WEB_NAME_KEY not in web["environment"]
    assert web["environment"]["DATA_DIR"] == "/app/backend/data"
    assert web["environment"]["LIMIRA_API_BASE_URL"] == "/api/limira"
    assert web["environment"]["LIMIRA_AUTH_SQLITE_PATH"] == (
        "/app/backend/data/limira_auth.sqlite3"
    )
    assert web["environment"]["LIMIRA_LEGACY_AUTH_SQLITE_PATH"] == (
        "/app/backend/data/legacy_auth.sqlite3"
    )
    assert web["environment"]["LIMIRA_AUTH_SECRET"] == (
        "${LIMIRA_AUTH_SECRET:?set LIMIRA_AUTH_SECRET}"
    )
    assert web["environment"]["LIMIRA_RUNNER_INTERNAL_URL"] == (
        "http://limira-runner:8091"
    )
    assert "${RUNNER_SERVICE_TOKEN" in web["environment"][
        "LIMIRA_RUNNER_SERVICE_TOKEN"
    ]
    assert web["environment"]["LIMIRA_REPOSITORY_BACKEND"] == "postgres"
    assert web["environment"]["LIMIRA_DATABASE_URL"].startswith("postgresql://")
    assert "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}" in web["environment"][
        "LIMIRA_DATABASE_URL"
    ]
    assert "LIMIRA_ALLOW_IN_MEMORY_REPOSITORY" not in web["environment"]
    assert web["environment"]["LIMIRA_RUNTIME_STATE_BACKEND"] == "redis"
    assert "LIMIRA_ALLOW_IN_MEMORY_RUNTIME_STATE" not in web["environment"]
    assert web["environment"]["LIMIRA_OBJECT_STORAGE_BACKEND"] == "s3"
    assert web["environment"]["LIMIRA_OBJECT_BUCKET"] == "${MINIO_BUCKET:-limira-artifacts}"
    assert "LIMIRA_ALLOW_IN_MEMORY_OBJECT_STORAGE" not in web["environment"]
    assert web["environment"]["LIMIRA_UPLOAD_EMBEDDINGS_ENABLED"] == (
        "${LIMIRA_UPLOAD_EMBEDDINGS_ENABLED:-false}"
    )
    assert web["environment"]["LIMIRA_EMBEDDING_PROVIDER"] == (
        "${LIMIRA_EMBEDDING_PROVIDER:-disabled}"
    )
    assert web["environment"]["LIMIRA_EMBEDDING_MODEL"] == (
        "${LIMIRA_EMBEDDING_MODEL:-}"
    )
    assert web["environment"]["LIMIRA_EMBEDDING_DIMENSIONS"] == (
        "${LIMIRA_EMBEDDING_DIMENSIONS:-1536}"
    )
    assert web["environment"]["S3_ENDPOINT_URL"] == "http://minio:9000"
    assert web["environment"]["AWS_ACCESS_KEY_ID"] == "${MINIO_ROOT_USER:-limira_minio}"
    assert web["environment"]["AWS_SECRET_ACCESS_KEY"] == (
        "${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD}"
    )
    assert "limira_web_data:/app/backend/data" in web["volumes"]

    assert postgres["environment"]["POSTGRES_PASSWORD"] == (
        "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}"
    )
    assert services["minio"]["environment"]["MINIO_ROOT_PASSWORD"] == (
        "${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD}"
    )
    assert services["minio-init"]["environment"]["MINIO_ROOT_PASSWORD"] == (
        "${MINIO_ROOT_PASSWORD:?set MINIO_ROOT_PASSWORD}"
    )
    _assert_no_sensitive_compose_defaults(compose)


def test_limira_env_example_has_required_placeholders_without_real_secrets():
    env = _read_env_example()
    required = {
        "COMPOSE_PROJECT_NAME",
        "LIMIRA_BIND_ADDRESS",
        "LIMIRA_WEB_PORT",
        "LIMIRA_RUNNER_PORT",
        "LIMIRA_AUTH_SECRET",
        "POSTGRES_PORT",
        "REDIS_PORT",
        "MINIO_API_PORT",
        "MINIO_CONSOLE_PORT",
        "RUNNER_SERVICE_TOKEN",
        "RUNNER_TASK_STORE_BACKEND",
        "RUNNER_DATABASE_URL",
        "RUNNER_ALLOW_SQLITE_TASK_STORE",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "LIMIRA_REPOSITORY_BACKEND",
        "LIMIRA_DATABASE_URL",
        "LIMIRA_ALLOW_IN_MEMORY_REPOSITORY",
        "LIMIRA_RUNTIME_STATE_BACKEND",
        "LIMIRA_ALLOW_IN_MEMORY_RUNTIME_STATE",
        "LIMIRA_OBJECT_STORAGE_BACKEND",
        "LIMIRA_ALLOW_IN_MEMORY_OBJECT_STORAGE",
        "LIMIRA_OBJECT_BUCKET",
        "LIMIRA_OBJECT_KEY_PREFIX",
        "LIMIRA_UPLOAD_EMBEDDINGS_ENABLED",
        "LIMIRA_EMBEDDING_PROVIDER",
        "LIMIRA_EMBEDDING_MODEL",
        "LIMIRA_EMBEDDING_DIMENSIONS",
        "DATABASE_URL",
        "REDIS_URL",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "MINIO_BUCKET",
        "S3_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "BASE_URL",
        "DEFAULT_LLM_PROVIDER",
        "DEFAULT_MODEL_NAME",
        "SERPER_API_KEY",
        "JINA_API_KEY",
        "E2B_API_KEY",
        "SUMMARY_LLM_API_KEY",
        "SUMMARY_LLM_BASE_URL",
        "SUMMARY_LLM_MODEL_NAME",
        "EMBEDDING_API_KEY",
    }
    assert required.issubset(env)

    assert env["COMPOSE_PROJECT_NAME"] == "limira"
    assert env["LIMIRA_BIND_ADDRESS"] == "127.0.0.1"
    assert env["LIMIRA_WEB_PORT"] == "3001"
    assert env["LIMIRA_RUNNER_PORT"] == "8091"
    assert env["POSTGRES_PORT"] == "5433"
    assert env["REDIS_PORT"] == "6380"
    assert env["MINIO_API_PORT"] == "9002"
    assert env["MINIO_CONSOLE_PORT"] == "9003"
    assert env["LIMIRA_REPOSITORY_BACKEND"] == "postgres"
    assert env["LIMIRA_DATABASE_URL"] == ""
    assert env["LIMIRA_ALLOW_IN_MEMORY_REPOSITORY"] == "false"
    assert env["LIMIRA_RUNTIME_STATE_BACKEND"] == "redis"
    assert env["LIMIRA_ALLOW_IN_MEMORY_RUNTIME_STATE"] == "false"
    assert env["LIMIRA_OBJECT_STORAGE_BACKEND"] == "s3"
    assert env["LIMIRA_ALLOW_IN_MEMORY_OBJECT_STORAGE"] == "false"
    assert env["LIMIRA_OBJECT_BUCKET"] == "limira-artifacts"
    assert env["LIMIRA_OBJECT_KEY_PREFIX"] == "limira"
    assert env["LIMIRA_UPLOAD_EMBEDDINGS_ENABLED"] == "false"
    assert env["LIMIRA_EMBEDDING_PROVIDER"] == "disabled"
    assert env["LIMIRA_EMBEDDING_MODEL"] == ""
    assert env["LIMIRA_EMBEDDING_DIMENSIONS"] == "1536"
    assert env["RUNNER_TASK_STORE_BACKEND"] == "postgres"
    assert env["RUNNER_DATABASE_URL"] == ""
    assert env["RUNNER_ALLOW_SQLITE_TASK_STORE"] == "false"
    assert env["BASE_URL"] == "https://api.deepseek.com"
    assert env["DEFAULT_LLM_PROVIDER"] == "openai"
    assert env["DEFAULT_MODEL_NAME"] == "deepseek-v4-pro"
    assert env["SUMMARY_LLM_BASE_URL"] == "https://api.deepseek.com/chat/completions"
    assert env["SUMMARY_LLM_MODEL_NAME"] == "deepseek-v4-pro"

    secret_keys = [
        key
        for key in env
        if any(marker in key for marker in ("PASSWORD", "TOKEN", "API_KEY", "SECRET"))
    ]
    for key in secret_keys:
        value = env[key]
        assert value == "" or "replace-with" in value
        assert "Bearer " not in value
        assert not value.startswith("sk-")
        assert not value.startswith("eyJ")


def test_limira_native_backend_entrypoint_has_no_legacy_app_dependency():
    native_app = LIMIRA_NATIVE_APP.read_text(encoding="utf-8")
    router = LIMIRA_BACKEND_ROUTER.read_text(encoding="utf-8")

    assert "from limira_backend.routers import limira" in native_app
    assert "app.include_router(limira.router, prefix=\"/api/limira\")" in native_app
    assert LEGACY_PY_PACKAGE not in native_app
    assert LEGACY_PY_PACKAGE not in router


def test_limira_migration_creates_required_extensions_tables_and_indexes():
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    lowered = sql.lower()
    tables = _parse_create_tables(sql)

    for extension in ("postgis", "vector"):
        assert f"create extension if not exists {extension}" in lowered

    required_tables = {
        "limira_research_tasks",
        "limira_artifact_events",
        "limira_artifact_trace_events",
        "limira_task_event_logs",
        "limira_evidence_items",
        "limira_entities",
        "limira_entity_relations",
        "limira_timeline_events",
        "limira_generated_reports",
        "limira_uploaded_documents",
        "limira_media_assets",
    }
    for table in required_tables:
        assert re.search(rf"create table if not exists\s+{table}\b", lowered)
        assert table in tables

    assert "geometry(geometry, 4326)" in lowered
    assert "embedding vector(1536)" in lowered
    assert "using gist (geometry)" in lowered
    assert "using ivfflat (embedding vector_cosine_ops)" in lowered
    assert (
        "constraint uq_limira_entities_task_entity unique (task_id, entity_id)"
        in tables["limira_entities"]["body"]
    )
    assert "artifact_event_id text primary key" in tables["limira_artifact_events"]["body"]
    assert (
        "constraint uq_limira_artifact_events_task_local unique "
        "(task_id, artifact_type, local_artifact_id)"
        in tables["limira_artifact_events"]["body"]
    )
    assert (
        "trace_event_id text primary key"
        in tables["limira_artifact_trace_events"]["body"]
    )
    assert (
        "payload jsonb not null default '{}'::jsonb"
        in tables["limira_artifact_trace_events"]["body"]
    )
    assert "event_log_id text primary key" in tables["limira_task_event_logs"]["body"]
    assert (
        "payload jsonb not null default '{}'::jsonb"
        in tables["limira_task_event_logs"]["body"]
    )
    assert "evidence_storage_id text primary key" in tables["limira_evidence_items"]["body"]
    assert (
        "constraint uq_limira_evidence_items_task_evidence unique "
        "(task_id, evidence_id)"
        in tables["limira_evidence_items"]["body"]
    )
    assert "entity_storage_id text primary key" in tables["limira_entities"]["body"]
    assert (
        "constraint uq_limira_entity_relations_task_relation unique "
        "(task_id, relation_id)"
        in tables["limira_entity_relations"]["body"]
    )
    assert (
        "constraint uq_limira_timeline_events_task_event unique "
        "(task_id, timeline_event_id)"
        in tables["limira_timeline_events"]["body"]
    )
    assert (
        "constraint uq_limira_generated_reports_task_report unique "
        "(task_id, report_id)"
        in tables["limira_generated_reports"]["body"]
    )
    assert (
        "constraint uq_limira_entities_task_entity"
        not in tables["limira_evidence_items"]["body"]
    )
    assert (
        "constraint fk_limira_entity_relations_source_same_task"
        in tables["limira_entity_relations"]["body"]
    )
    assert (
        "foreign key (task_id, source_entity_id)"
        in tables["limira_entity_relations"]["body"]
    )
    assert (
        "constraint fk_limira_entity_relations_target_same_task"
        in tables["limira_entity_relations"]["body"]
    )
    assert (
        "foreign key (task_id, target_entity_id)"
        in tables["limira_entity_relations"]["body"]
    )

    for status in ("queued", "running", "completed", "failed", "cancelled"):
        assert f"'{status}'" in lowered
    for archive_status in ("pending", "ready", "failed"):
        assert f"'{archive_status}'" in lowered

    for entity_type in (
        "country",
        "agency",
        "company",
        "person",
        "policy",
        "bill",
        "sanction_target",
        "technology",
        "project",
        "location",
        "event",
    ):
        assert f"'{entity_type}'" in lowered

    for relation_type in (
        "sanctions",
        "regulates",
        "affects_industry",
        "owns",
        "partners_with",
        "located_in",
        "supply_chain_dependency",
        "mentions",
        "conflicts_with",
    ):
        assert f"'{relation_type}'" in lowered


def test_limira_migration_constraints_reference_existing_table_columns():
    tables = _parse_create_tables(MIGRATION_FILE.read_text(encoding="utf-8"))

    for table_name, table in tables.items():
        columns = table["columns"]
        for constraint in table["constraints"]:
            for pattern in (r"\bunique\s*\(([^)]+)\)", r"\bforeign key\s*\(([^)]+)\)"):
                for match in re.finditer(pattern, constraint):
                    referenced = _column_list(match.group(1))
                    assert referenced <= columns, (
                        f"{table_name} constraint references missing local columns: "
                        f"{sorted(referenced - columns)} in {constraint}"
                    )

    relations = tables["limira_entity_relations"]
    assert {"task_id", "source_entity_id", "target_entity_id"} <= relations["columns"]
    assert any(
        "foreign key (task_id, source_entity_id)" in constraint
        and "references limira_entities (task_id, entity_id)" in constraint
        for constraint in relations["constraints"]
    )
    assert any(
        "foreign key (task_id, target_entity_id)" in constraint
        and "references limira_entities (task_id, entity_id)" in constraint
        for constraint in relations["constraints"]
    )


def test_limira_runtime_artifacts_are_ignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for ignored in (
        "*.sqlite3",
        "apps/limira-runner/archives/",
        "apps/limira-runner/runner_tasks.sqlite3",
        "apps/limira-runner/runtime/",
        "limira-runtime/",
        "postgres-data/",
        "redis-data/",
        "minio-data/",
    ):
        assert ignored in gitignore


def test_limira_deploy_helper_files_exist_and_are_executable_where_needed():
    required_files = [
        ROOT / "deploy/limira/postgres.Dockerfile",
        ROOT / "deploy/limira/runner.Dockerfile",
        ROOT / "deploy/limira/nginx.conf",
        ROOT / "deploy/limira/minio/init-bucket.sh",
        ROOT / "apps/limira-web/Dockerfile",
        ROOT / "apps/limira-web/backend/limira_native.py",
        ROOT / "apps/limira-web/backend/limira_backend/routers/limira.py",
        ROOT / "apps/limira-standalone/server.mjs",
        ROOT / "apps/limira-standalone/public/app.js",
    ]
    for path in required_files:
        assert path.exists(), path

    init_script = ROOT / "deploy/limira/minio/init-bucket.sh"
    assert init_script.stat().st_mode & 0o111

    runner_api = RUNNER_API.read_text(encoding="utf-8")
    assert 'app.router.add_get("/health", healthcheck)' in runner_api
    assert 'return web.json_response({"status": True})' in runner_api


def test_limira_web_uses_native_backend_and_standalone_frontend_only():
    limira_web = ROOT / "apps/limira-web"
    assert (limira_web / "backend/limira_backend/routers").is_dir()
    assert not (limira_web / "src").exists()
    assert not (limira_web / "backend" / LEGACY_PY_PACKAGE).exists()
    assert not (limira_web / "package.json").exists()
    assert not (ROOT / "apps" / LEGACY_APP_DIR).exists()
    assert not (ROOT / "deploy/limira/web-placeholder/index.html").exists()
    native_app = (limira_web / "backend/limira_native.py").read_text(encoding="utf-8")
    assert "limira.router" in native_app
    assert 'prefix="/api/limira"' in native_app


def _read_env_example() -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _published_port(service: dict) -> str:
    ports = _published_ports(service)
    assert len(ports) == 1
    return next(iter(ports))


def _published_ports(service: dict) -> set[str]:
    return {_extract_host_port(port) for port in service.get("ports", [])}


def _published_bind_hosts(service: dict) -> set[str]:
    return {_extract_host_ip(port) for port in service.get("ports", [])}


def _extract_host_port(port: str | dict) -> str:
    if isinstance(port, dict):
        published = str(port["published"])
        default_match = re.search(r"\$\{[^:}]+:-([0-9]+)\}", published)
        return default_match.group(1) if default_match else published
    default_match = re.search(r"\$\{[^:}]+:-([0-9]+)\}", port)
    if default_match:
        return default_match.group(1)
    return port.split(":", 1)[0]


def _extract_host_ip(port: str | dict) -> str:
    if isinstance(port, dict):
        host_ip = str(port.get("host_ip") or "")
        default_match = re.search(r"\$\{[^:}]+:-([^}]+)\}", host_ip)
        return default_match.group(1) if default_match else host_ip
    parts = port.split(":")
    return parts[0] if len(parts) == 3 else ""


def _assert_no_sensitive_compose_defaults(compose: dict) -> None:
    serialized = yaml.safe_dump(compose)
    forbidden = (
        "${POSTGRES_PASSWORD:-",
        "${MINIO_ROOT_PASSWORD:-",
        "${RUNNER_SERVICE_TOKEN:-",
        "limira_dev_password",
        "limira_minio_dev_password",
        "replace-with-long-random-service-token",
    )
    for value in forbidden:
        assert value not in serialized


def _parse_create_tables(sql: str) -> dict[str, dict]:
    lowered = sql.lower()
    tables: dict[str, dict] = {}
    for match in re.finditer(r"create table if not exists\s+([a-z0-9_]+)\s*\(", lowered):
        table_name = match.group(1)
        body_start = match.end()
        body_end = _matching_paren_index(lowered, body_start - 1)
        body = lowered[body_start:body_end]
        elements = _split_top_level(body)
        columns = set()
        constraints = []
        for element in elements:
            stripped = element.strip()
            if not stripped:
                continue
            if stripped.startswith("constraint "):
                constraints.append(_normalize_sql(stripped))
                continue
            columns.add(stripped.split(None, 1)[0].strip('"'))
        tables[table_name] = {
            "body": _normalize_sql(body),
            "columns": columns,
            "constraints": constraints,
        }
    return tables


def _matching_paren_index(text: str, open_index: int) -> int:
    depth = 0
    in_quote = False
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "'":
            in_quote = not in_quote
        if in_quote:
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("unmatched parenthesis in SQL")


def _split_top_level(body: str) -> list[str]:
    parts = []
    start = 0
    depth = 0
    in_quote = False
    for index, char in enumerate(body):
        if char == "'":
            in_quote = not in_quote
        if in_quote:
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:index])
            start = index + 1
    parts.append(body[start:])
    return parts


def _column_list(raw: str) -> set[str]:
    return {column.strip().strip('"') for column in raw.split(",")}


def _normalize_sql(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip())
