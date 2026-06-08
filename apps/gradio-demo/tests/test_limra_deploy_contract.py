import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = ROOT / "docker-compose.limra-aggressive.yml"
ENV_EXAMPLE = ROOT / ".env.example"
LIMRA_WEB_ENV = ROOT / "apps/limra-web/backend/open_webui/env.py"
LIMRA_WEB_MAIN = ROOT / "apps/limra-web/backend/open_webui/main.py"
MIGRATION_FILE = (
    ROOT / "deploy/limra/postgres/migrations/001_limra_osint_schema.sql"
)


def test_limra_compose_defines_aggressive_stack_contract():
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]

    expected_services = {
        "limra-web",
        "limra-runner",
        "postgres",
        "redis",
        "minio",
        "minio-init",
        "reverse-proxy",
    }
    assert expected_services.issubset(services)

    assert _published_port(services["limra-web"]) == "3001"
    assert _published_port(services["limra-runner"]) == "8091"
    assert _published_port(services["postgres"]) == "5433"
    assert _published_port(services["redis"]) == "6380"
    assert _published_ports(services["minio"]) == {"9002", "9003"}

    assert "healthcheck" in services["postgres"]
    assert "healthcheck" in services["redis"]
    assert "healthcheck" in services["minio"]
    assert services["minio-init"]["depends_on"]["minio"]["condition"] == (
        "service_healthy"
    )

    postgres = services["postgres"]
    assert postgres["build"]["dockerfile"] == "deploy/limra/postgres.Dockerfile"
    assert any(
        "deploy/limra/postgres/migrations" in volume
        for volume in postgres["volumes"]
    )

    runner = services["limra-runner"]
    assert runner["build"]["dockerfile"] == "deploy/limra/runner.Dockerfile"
    assert runner["environment"]["MIROTHINKER_RUNNER_PORT"] == "8091"
    assert "${RUNNER_SERVICE_TOKEN" in runner["environment"]["RUNNER_SERVICE_TOKEN"]
    assert runner["environment"]["RUNNER_TASK_STORE_BACKEND"] == "postgres"
    assert runner["environment"]["RUNNER_DATABASE_URL"].startswith("postgresql://")
    assert "RUNNER_ALLOW_SQLITE_TASK_STORE" not in runner["environment"]

    web = services["limra-web"]
    assert web["build"]["context"] == "apps/limra-web"
    assert web["build"]["dockerfile"] == "Dockerfile"
    assert "image" not in web
    assert web["environment"]["PORT"] == "8080"
    assert web["environment"]["WEBUI_NAME"] == "limra"
    assert web["environment"]["LIMRA_API_BASE_URL"] == "/api/limra"
    assert web["environment"]["LIMRA_RUNNER_INTERNAL_URL"] == (
        "http://limra-runner:8091"
    )
    assert "${RUNNER_SERVICE_TOKEN" in web["environment"][
        "LIMRA_RUNNER_SERVICE_TOKEN"
    ]
    assert web["environment"]["LIMRA_REPOSITORY_BACKEND"] == "postgres"
    assert web["environment"]["LIMRA_DATABASE_URL"].startswith("postgresql://")
    assert "LIMRA_ALLOW_IN_MEMORY_REPOSITORY" not in web["environment"]
    assert web["environment"]["LIMRA_RUNTIME_STATE_BACKEND"] == "redis"
    assert "LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE" not in web["environment"]
    assert web["environment"]["LIMRA_OBJECT_STORAGE_BACKEND"] == "s3"
    assert web["environment"]["LIMRA_OBJECT_BUCKET"] == "${MINIO_BUCKET:-limra-artifacts}"
    assert "LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE" not in web["environment"]
    assert web["environment"]["S3_ENDPOINT_URL"] == "http://minio:9000"
    assert web["environment"]["AWS_ACCESS_KEY_ID"] == "${MINIO_ROOT_USER:-limra_minio}"
    assert "${MINIO_ROOT_PASSWORD" in web["environment"]["AWS_SECRET_ACCESS_KEY"]


def test_limra_env_example_has_required_placeholders_without_real_secrets():
    env = _read_env_example()
    required = {
        "COMPOSE_PROJECT_NAME",
        "LIMRA_WEB_PORT",
        "LIMRA_RUNNER_PORT",
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
        "LIMRA_REPOSITORY_BACKEND",
        "LIMRA_DATABASE_URL",
        "LIMRA_ALLOW_IN_MEMORY_REPOSITORY",
        "LIMRA_RUNTIME_STATE_BACKEND",
        "LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE",
        "LIMRA_OBJECT_STORAGE_BACKEND",
        "LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE",
        "LIMRA_OBJECT_BUCKET",
        "LIMRA_OBJECT_KEY_PREFIX",
        "DATABASE_URL",
        "REDIS_URL",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "MINIO_BUCKET",
        "S3_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "BASE_URL",
        "DEFAULT_MODEL_NAME",
        "SERPER_API_KEY",
        "JINA_API_KEY",
        "E2B_API_KEY",
        "SUMMARY_API_KEY",
        "EMBEDDING_API_KEY",
    }
    assert required.issubset(env)

    assert env["COMPOSE_PROJECT_NAME"] == "limra_aggressive"
    assert env["LIMRA_WEB_PORT"] == "3001"
    assert env["LIMRA_RUNNER_PORT"] == "8091"
    assert env["POSTGRES_PORT"] == "5433"
    assert env["REDIS_PORT"] == "6380"
    assert env["MINIO_API_PORT"] == "9002"
    assert env["MINIO_CONSOLE_PORT"] == "9003"
    assert env["LIMRA_REPOSITORY_BACKEND"] == "postgres"
    assert env["LIMRA_DATABASE_URL"].startswith("postgresql://")
    assert env["LIMRA_ALLOW_IN_MEMORY_REPOSITORY"] == "false"
    assert env["LIMRA_RUNTIME_STATE_BACKEND"] == "redis"
    assert env["LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE"] == "false"
    assert env["LIMRA_OBJECT_STORAGE_BACKEND"] == "s3"
    assert env["LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE"] == "false"
    assert env["LIMRA_OBJECT_BUCKET"] == "limra-artifacts"
    assert env["LIMRA_OBJECT_KEY_PREFIX"] == "limra"
    assert env["RUNNER_TASK_STORE_BACKEND"] == "postgres"
    assert env["RUNNER_DATABASE_URL"].startswith("postgresql://")
    assert env["RUNNER_ALLOW_SQLITE_TASK_STORE"] == "false"

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


def test_limra_runtime_product_name_is_not_rewritten_to_open_webui():
    env_py = LIMRA_WEB_ENV.read_text(encoding="utf-8")
    main_py = LIMRA_WEB_MAIN.read_text(encoding="utf-8")

    assert "WEBUI_NAME = os.getenv('WEBUI_NAME', 'limra')" in env_py
    assert "WEBUI_NAME += ' (Open WebUI)'" not in env_py
    assert "if WEBUI_NAME != 'Open WebUI'" not in env_py
    assert "WEBUI_FAVICON_URL = '/static/favicon.png'" in env_py

    assert "title='limra'" in main_py
    assert "'name': app.state.WEBUI_NAME" in main_py
    assert "'short_name': app.state.WEBUI_NAME" in main_py
    assert "<ShortName>{app.state.WEBUI_NAME}</ShortName>" in main_py
    assert "<Description>Search {app.state.WEBUI_NAME}</Description>" in main_py
    assert "limra (Open WebUI)" not in main_py


def test_limra_migration_creates_required_extensions_tables_and_indexes():
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    lowered = sql.lower()
    tables = _parse_create_tables(sql)

    for extension in ("postgis", "vector"):
        assert f"create extension if not exists {extension}" in lowered

    required_tables = {
        "limra_research_tasks",
        "limra_artifact_events",
        "limra_artifact_trace_events",
        "limra_evidence_items",
        "limra_entities",
        "limra_entity_relations",
        "limra_timeline_events",
        "limra_generated_reports",
        "limra_uploaded_documents",
        "limra_media_assets",
    }
    for table in required_tables:
        assert re.search(rf"create table if not exists\s+{table}\b", lowered)
        assert table in tables

    assert "geometry(geometry, 4326)" in lowered
    assert "embedding vector(1536)" in lowered
    assert "using gist (geometry)" in lowered
    assert "using ivfflat (embedding vector_cosine_ops)" in lowered
    assert (
        "constraint uq_limra_entities_task_entity unique (task_id, entity_id)"
        in tables["limra_entities"]["body"]
    )
    assert "artifact_event_id text primary key" in tables["limra_artifact_events"]["body"]
    assert (
        "constraint uq_limra_artifact_events_task_local unique "
        "(task_id, artifact_type, local_artifact_id)"
        in tables["limra_artifact_events"]["body"]
    )
    assert (
        "trace_event_id text primary key"
        in tables["limra_artifact_trace_events"]["body"]
    )
    assert (
        "payload jsonb not null default '{}'::jsonb"
        in tables["limra_artifact_trace_events"]["body"]
    )
    assert "evidence_storage_id text primary key" in tables["limra_evidence_items"]["body"]
    assert (
        "constraint uq_limra_evidence_items_task_evidence unique "
        "(task_id, evidence_id)"
        in tables["limra_evidence_items"]["body"]
    )
    assert "entity_storage_id text primary key" in tables["limra_entities"]["body"]
    assert (
        "constraint uq_limra_entity_relations_task_relation unique "
        "(task_id, relation_id)"
        in tables["limra_entity_relations"]["body"]
    )
    assert (
        "constraint uq_limra_timeline_events_task_event unique "
        "(task_id, timeline_event_id)"
        in tables["limra_timeline_events"]["body"]
    )
    assert (
        "constraint uq_limra_generated_reports_task_report unique "
        "(task_id, report_id)"
        in tables["limra_generated_reports"]["body"]
    )
    assert (
        "constraint uq_limra_entities_task_entity"
        not in tables["limra_evidence_items"]["body"]
    )
    assert (
        "constraint fk_limra_entity_relations_source_same_task"
        in tables["limra_entity_relations"]["body"]
    )
    assert (
        "foreign key (task_id, source_entity_id)"
        in tables["limra_entity_relations"]["body"]
    )
    assert (
        "constraint fk_limra_entity_relations_target_same_task"
        in tables["limra_entity_relations"]["body"]
    )
    assert (
        "foreign key (task_id, target_entity_id)"
        in tables["limra_entity_relations"]["body"]
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


def test_limra_migration_constraints_reference_existing_table_columns():
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

    relations = tables["limra_entity_relations"]
    assert {"task_id", "source_entity_id", "target_entity_id"} <= relations["columns"]
    assert any(
        "foreign key (task_id, source_entity_id)" in constraint
        and "references limra_entities (task_id, entity_id)" in constraint
        for constraint in relations["constraints"]
    )
    assert any(
        "foreign key (task_id, target_entity_id)" in constraint
        and "references limra_entities (task_id, entity_id)" in constraint
        for constraint in relations["constraints"]
    )


def test_limra_runtime_artifacts_are_ignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for ignored in (
        "*.sqlite3",
        "apps/gradio-demo/archives/",
        "apps/gradio-demo/runner_tasks.sqlite3",
        "apps/gradio-demo/runtime/",
        "limra-runtime/",
        "postgres-data/",
        "redis-data/",
        "minio-data/",
    ):
        assert ignored in gitignore


def test_limra_deploy_helper_files_exist_and_are_executable_where_needed():
    required_files = [
        ROOT / "deploy/limra/postgres.Dockerfile",
        ROOT / "deploy/limra/runner.Dockerfile",
        ROOT / "deploy/limra/nginx.conf",
        ROOT / "deploy/limra/minio/init-bucket.sh",
        ROOT / "apps/limra-web/Dockerfile",
        ROOT / "apps/limra-web/package.json",
        ROOT / "apps/limra-web/backend/open_webui/main.py",
        ROOT / "apps/limra-web/backend/open_webui/routers/limra.py",
    ]
    for path in required_files:
        assert path.exists(), path

    init_script = ROOT / "deploy/limra/minio/init-bucket.sh"
    assert init_script.stat().st_mode & 0o111


def test_limra_web_is_real_open_webui_vendor_path_not_placeholder():
    limra_web = ROOT / "apps/limra-web"
    assert (limra_web / "src/routes").is_dir()
    assert (limra_web / "backend/open_webui/routers").is_dir()
    assert not (ROOT / "deploy/limra/web-placeholder/index.html").exists()
    assert "open-webui" in (limra_web / "package.json").read_text(encoding="utf-8")
    main_py = (limra_web / "backend/open_webui/main.py").read_text(encoding="utf-8")
    assert "limra.router" in main_py
    assert "prefix='/api/limra'" in main_py


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


def _extract_host_port(port: str | dict) -> str:
    if isinstance(port, dict):
        return str(port["published"])
    default_match = re.search(r"\$\{[^:}]+:-([0-9]+)\}", port)
    if default_match:
        return default_match.group(1)
    return port.split(":", 1)[0]


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
