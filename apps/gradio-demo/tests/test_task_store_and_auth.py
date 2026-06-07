import sqlite3

import pytest

from auth_adapter import AuthError, authenticate_headers, reject_body_user_id
from runner_api import SERVICE_TOKEN_KEY, create_app
from task_store import PostgresTaskStore, TaskStore, create_task_store_from_env


def test_task_store_creates_updates_and_lists_by_user(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
        model_summary={"provider": "deepseek"},
    )
    store.create_task(
        task_id="task-b",
        user_id="user-a",
        query="second",
        created_at="2026-06-06T12:01:00+00:00",
    )
    store.create_task(
        task_id="task-c",
        user_id="user-b",
        query="third",
        created_at="2026-06-06T12:02:00+00:00",
    )

    updated = store.update_task(
        "task-a",
        status="completed",
        archive_status="ready",
        archive_dir="/archives/task-a",
        archive_zip_path="/archives/task-a/archive.zip",
        completed_at="2026-06-06T12:10:00+00:00",
        warnings=["warning"],
    )

    assert updated.status == "completed"
    assert updated.archive_status == "ready"
    assert updated.warnings == ["warning"]
    assert store.user_owns_task("task-a", "user-a") is True
    assert store.user_owns_task("task-a", "user-b") is False
    assert [record.task_id for record in store.list_user_tasks("user-a")] == [
        "task-b",
        "task-a",
    ]


def test_task_store_enforces_unique_task_id(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.create_task(
            task_id="task-a",
            user_id="user-b",
            query="duplicate",
            created_at="2026-06-06T12:01:00+00:00",
        )


def test_task_store_claims_queued_task_once(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
    )

    claimed = store.claim_queued_task(
        "task-a",
        started_at="2026-06-06T12:01:00+00:00",
    )
    duplicate_claim = store.claim_queued_task(
        "task-a",
        started_at="2026-06-06T12:02:00+00:00",
    )

    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.started_at == "2026-06-06T12:01:00+00:00"
    assert duplicate_claim is None
    current = store.get_task("task-a")
    assert current.status == "running"
    assert current.started_at == "2026-06-06T12:01:00+00:00"


def test_task_store_cancels_only_still_queued_tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
    )
    store.create_task(
        task_id="task-b",
        user_id="user-a",
        query="second",
        created_at="2026-06-06T12:00:00+00:00",
    )

    cancelled = store.cancel_queued_task(
        "task-a",
        started_at="2026-06-06T12:01:00+00:00",
        completed_at="2026-06-06T12:01:01+00:00",
        error="task cancelled before stream started",
    )
    stream_claim_after_cancel = store.claim_queued_task(
        "task-a",
        started_at="2026-06-06T12:02:00+00:00",
    )

    stream_claim = store.claim_queued_task(
        "task-b",
        started_at="2026-06-06T12:03:00+00:00",
    )
    cancel_after_stream_claim = store.cancel_queued_task(
        "task-b",
        started_at="2026-06-06T12:04:00+00:00",
        completed_at="2026-06-06T12:04:01+00:00",
        error="late cancel",
    )

    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.error == "task cancelled before stream started"
    assert stream_claim_after_cancel is None
    assert stream_claim is not None
    assert stream_claim.status == "running"
    assert cancel_after_stream_claim is None
    current = store.get_task("task-b")
    assert current.status == "running"
    assert current.started_at == "2026-06-06T12:03:00+00:00"


def test_task_store_rejects_invalid_status_updates(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
    )

    with pytest.raises(ValueError):
        store.update_task("task-a", status="done")

    with pytest.raises(ValueError):
        store.update_task("task-a", archive_status="missing")


def test_runner_task_store_factory_requires_explicit_sqlite_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNNER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RUNNER_TASK_STORE_BACKEND", "postgres")

    with pytest.raises(RuntimeError, match="runner_postgres_database_url_missing"):
        create_task_store_from_env(sqlite_path=tmp_path / "tasks.sqlite3")

    monkeypatch.setenv(
        "RUNNER_DATABASE_URL",
        "postgresql://limra:test@postgres:5432/limra",
    )
    store = create_task_store_from_env(sqlite_path=tmp_path / "tasks.sqlite3")
    assert isinstance(store, PostgresTaskStore)
    assert store.database_url == "postgresql://limra:test@postgres:5432/limra"

    monkeypatch.setenv("RUNNER_TASK_STORE_BACKEND", "sqlite")
    monkeypatch.delenv("RUNNER_ALLOW_SQLITE_TASK_STORE", raising=False)
    with pytest.raises(
        RuntimeError,
        match="runner_sqlite_task_store_requires_explicit_fallback",
    ):
        create_task_store_from_env(sqlite_path=tmp_path / "tasks.sqlite3")

    monkeypatch.setenv("RUNNER_ALLOW_SQLITE_TASK_STORE", "true")
    store = create_task_store_from_env(sqlite_path=tmp_path / "tasks.sqlite3")
    assert isinstance(store, TaskStore)
    assert store.db_path == str(tmp_path / "tasks.sqlite3")


def test_runner_postgres_task_store_normalizes_supported_sqlalchemy_urls(monkeypatch):
    from psycopg.conninfo import conninfo_to_dict

    raw_postgresql = PostgresTaskStore("postgresql://limra:test@postgres:5432/limra")
    legacy_postgres = PostgresTaskStore("postgres://limra:test@postgres:5432/limra")
    psycopg_url = PostgresTaskStore("postgresql+psycopg://limra:test@postgres:5432/limra")
    psycopg2_url = PostgresTaskStore("postgresql+psycopg2://limra:test@postgres:5432/limra")

    assert raw_postgresql.database_url == "postgresql://limra:test@postgres:5432/limra"
    assert legacy_postgres.database_url == "postgres://limra:test@postgres:5432/limra"
    assert psycopg_url.database_url == "postgresql://limra:test@postgres:5432/limra"
    assert psycopg2_url.database_url == "postgresql://limra:test@postgres:5432/limra"
    assert conninfo_to_dict(psycopg_url.database_url)["dbname"] == "limra"
    assert conninfo_to_dict(psycopg2_url.database_url)["dbname"] == "limra"

    monkeypatch.setenv("RUNNER_TASK_STORE_BACKEND", "postgres")
    monkeypatch.setenv(
        "RUNNER_DATABASE_URL",
        "postgresql+psycopg://limra:test@postgres:5432/limra",
    )
    store = create_task_store_from_env()
    assert isinstance(store, PostgresTaskStore)
    assert store.database_url == "postgresql://limra:test@postgres:5432/limra"


def test_runner_postgres_task_store_rejects_unsupported_postgresql_plus_urls():
    with pytest.raises(RuntimeError, match="runner_postgres_database_url_required"):
        PostgresTaskStore("postgresql+asyncpg://limra:test@postgres:5432/limra")


def test_postgres_task_store_sql_targets_limra_research_tasks():
    sql = PostgresTaskStore.sql_contract().lower()

    assert "limra_research_tasks" in sql
    assert "owner_user_id" in sql
    assert "archive_status" in sql
    assert "archive_object_key" in sql
    assert "archive_zip_sha256" in sql
    assert "runner_task_id" in sql
    assert "model_summary" in sql
    assert "metadata" in sql
    assert "where task_id = %s" in sql
    assert "and status = 'queued'" in sql
    assert "returning" in sql
    assert "mirothinker_research_tasks" not in sql


def test_postgres_task_store_matches_runner_task_store_contract():
    fake_db = FakeRunnerPostgresDatabase()
    store = PostgresTaskStore(
        "postgresql://limra:test@postgres:5432/limra",
        connection_factory=fake_db.connect,
    )

    first = store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
        model_summary={"provider": "deepseek"},
    )
    store.create_task(
        task_id="task-b",
        user_id="user-a",
        query="second",
        created_at="2026-06-06T12:01:00+00:00",
    )
    store.create_task(
        task_id="task-c",
        user_id="user-b",
        query="third",
        created_at="2026-06-06T12:02:00+00:00",
    )

    assert first.status == "queued"
    assert first.model_summary == {"provider": "deepseek"}
    assert [record.task_id for record in store.list_user_tasks("user-a")] == [
        "task-b",
        "task-a",
    ]
    assert store.user_owns_task("task-a", "user-a") is True
    assert store.user_owns_task("task-a", "user-b") is False

    claimed = store.claim_queued_task(
        "task-a",
        started_at="2026-06-06T12:03:00+00:00",
    )
    duplicate_claim = store.claim_queued_task(
        "task-a",
        started_at="2026-06-06T12:04:00+00:00",
    )
    assert claimed is not None
    assert claimed.status == "running"
    assert duplicate_claim is None

    updated = store.update_task(
        "task-a",
        status="completed",
        archive_status="ready",
        archive_dir="/archives/task-a",
        archive_zip_path="/archives/task-a/archive.zip",
        completed_at="2026-06-06T12:10:00+00:00",
        warnings=["warning"],
    )
    assert updated.status == "completed"
    assert updated.archive_status == "ready"
    assert updated.archive_dir == "/archives/task-a"
    assert updated.archive_zip_path == "/archives/task-a/archive.zip"
    assert updated.warnings == ["warning"]

    cancelled = store.cancel_queued_task(
        "task-b",
        started_at="2026-06-06T12:11:00+00:00",
        completed_at="2026-06-06T12:12:00+00:00",
        error="cancelled",
    )
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.archive_status == "failed"
    assert cancelled.error == "cancelled"


def test_auth_adapter_accepts_trusted_headers_only():
    auth = authenticate_headers(
        {
            "X-MiroThinker-Service-Token": "shared",
            "X-OpenWebUI-User-Id": "user-a",
            "X-OpenWebUI-User-Role": "admin",
        },
        service_token="shared",
    )

    assert auth.user_id == "user-a"
    assert auth.is_admin is True


def test_auth_adapter_rejects_invalid_or_body_user_id():
    with pytest.raises(AuthError) as invalid_token:
        authenticate_headers(
            {
                "X-MiroThinker-Service-Token": "wrong",
                "X-OpenWebUI-User-Id": "user-a",
            },
            service_token="shared",
        )
    assert invalid_token.value.code == "invalid_service_token"

    with pytest.raises(AuthError) as missing_user:
        authenticate_headers(
            {"X-MiroThinker-Service-Token": "shared"},
            service_token="shared",
        )
    assert missing_user.value.code == "missing_user_id"

    with pytest.raises(AuthError) as body_user:
        reject_body_user_id({"query": "x", "user_id": "attacker"})
    assert body_user.value.status == 400


def test_runner_app_uses_runner_service_token_env_fallback(tmp_path, monkeypatch):
    async def stream_events(*_args, **_kwargs):
        if False:
            yield {}

    monkeypatch.setenv("RUNNER_SERVICE_TOKEN", "shared-from-compose-env")
    app = create_app(
        task_store=TaskStore(tmp_path / "tasks.sqlite3"),
        archive_root=tmp_path / "archives",
        stream_events=stream_events,
        init_render_state=lambda: {},
        update_state_with_event=lambda state, _message: state,
        render_markdown=lambda _state: "# report",
    )

    assert app[SERVICE_TOKEN_KEY] == "shared-from-compose-env"


def test_runner_app_requires_explicit_task_store_or_postgres_config(monkeypatch):
    monkeypatch.setenv("RUNNER_TASK_STORE_BACKEND", "postgres")
    monkeypatch.delenv("RUNNER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="runner_postgres_database_url_missing"):
        create_app()


class FakeRunnerPostgresDatabase:
    def __init__(self):
        self.rows = {}

    def connect(self):
        return FakeRunnerPostgresConnection(self)


class FakeRunnerPostgresConnection:
    def __init__(self, database):
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        lowered = sql.lower()
        if "insert into limra_research_tasks" in lowered:
            row = {
                "task_id": params[0],
                "owner_user_id": params[1],
                "query": params[2],
                "status": "queued",
                "archive_status": "pending",
                "runner_task_id": params[3],
                "archive_object_key": None,
                "archive_zip_sha256": None,
                "created_at": params[4],
                "started_at": None,
                "completed_at": None,
                "error": None,
                "model_summary": params[5],
                "metadata": params[6],
            }
            if row["task_id"] in self.database.rows:
                raise AssertionError("duplicate task_id")
            self.database.rows[row["task_id"]] = row
            return FakeRunnerPostgresCursor([row])

        if "select" in lowered and "where task_id = %s" in lowered:
            row = self.database.rows.get(params[0])
            return FakeRunnerPostgresCursor([row] if row else [])

        if "select" in lowered and "where owner_user_id = %s" in lowered:
            owner_user_id, limit = params
            rows = [
                row
                for row in self.database.rows.values()
                if row["owner_user_id"] == owner_user_id
            ]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return FakeRunnerPostgresCursor(rows[:limit])

        if "set status = 'running'" in lowered:
            started_at, task_id = params
            row = self.database.rows.get(task_id)
            if not row or row["status"] != "queued":
                return FakeRunnerPostgresCursor([])
            row["status"] = "running"
            row["started_at"] = started_at
            return FakeRunnerPostgresCursor([row])

        if "set status = 'cancelled'" in lowered:
            started_at, completed_at, error, task_id = params
            row = self.database.rows.get(task_id)
            if not row or row["status"] != "queued":
                return FakeRunnerPostgresCursor([])
            row["status"] = "cancelled"
            row["archive_status"] = "failed"
            row["started_at"] = started_at
            row["completed_at"] = completed_at
            row["error"] = error
            return FakeRunnerPostgresCursor([row])

        if "update limra_research_tasks" in lowered:
            task_id = params[-1]
            row = self.database.rows.get(task_id)
            if not row:
                return FakeRunnerPostgresCursor([])
            value_index = 0
            for field in (
                "status",
                "archive_status",
                "archive_object_key",
                "archive_zip_sha256",
                "started_at",
                "completed_at",
                "error",
            ):
                if f"{field} = %s" in lowered:
                    row[field] = params[value_index]
                    value_index += 1
            if "model_summary = cast(%s as jsonb)" in lowered:
                row["model_summary"] = params[value_index]
                value_index += 1
            if "metadata = metadata || cast(%s as jsonb)" in lowered:
                metadata = row.get("metadata")
                if isinstance(metadata, str):
                    import json

                    metadata = json.loads(metadata)
                import json

                metadata.update(json.loads(params[value_index]))
                row["metadata"] = metadata
            return FakeRunnerPostgresCursor([row])

        raise AssertionError(f"unhandled SQL: {sql}")


class FakeRunnerPostgresCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)
