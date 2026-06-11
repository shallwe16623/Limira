import json
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
        context={
            "query": "first",
            "scenario": "sanctions",
            "conversation_id": "conversation-a",
            "document_ids": ["doc-a"],
            "upload_scope": {"document_count": 1},
            "source_policy": {"prefer_uploaded_documents": True},
        },
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
    assert updated.context["scenario"] == "sanctions"
    assert updated.context["document_ids"] == ["doc-a"]
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


def test_task_store_persists_lease_heartbeat_checkpoint_and_events(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-durable",
        user_id="user-a",
        query="durable",
        created_at="2026-06-06T12:00:00+00:00",
    )

    claimed = store.claim_queued_task(
        "task-durable",
        started_at="2026-06-06T12:01:00+00:00",
        worker_id="worker-a",
        lease_expires_at="2026-06-06T12:06:00+00:00",
    )
    duplicate = store.claim_queued_task(
        "task-durable",
        started_at="2026-06-06T12:02:00+00:00",
        worker_id="worker-b",
        lease_expires_at="2026-06-06T12:07:00+00:00",
    )
    wrong_worker_renewal = store.renew_task_lease(
        "task-durable",
        worker_id="worker-b",
        heartbeat_at="2026-06-06T12:03:00+00:00",
        lease_expires_at="2026-06-06T12:08:00+00:00",
    )
    renewed = store.renew_task_lease(
        "task-durable",
        worker_id="worker-a",
        heartbeat_at="2026-06-06T12:04:00+00:00",
        lease_expires_at="2026-06-06T12:09:00+00:00",
    )
    checkpointed = store.write_task_checkpoint(
        "task-durable",
        checkpoint={
            "phase": "research",
            "current_unit": "unit-1",
            "evidence_ids": ["EVID-abcdef123456"],
        },
        updated_at="2026-06-06T12:04:01+00:00",
    )
    store.append_task_event(
        "task-durable",
        {"task_id": "task-durable", "type": "message", "timestamp": "t1"},
        created_at="2026-06-06T12:04:02+00:00",
    )
    store.append_task_event(
        "task-durable",
        {"task_id": "task-durable", "type": "complete", "timestamp": "t2"},
        created_at="2026-06-06T12:04:03+00:00",
    )
    cleared = store.clear_task_lease("task-durable", worker_id="worker-a")

    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.worker_id == "worker-a"
    assert claimed.lease_expires_at == "2026-06-06T12:06:00+00:00"
    assert claimed.heartbeat_at == "2026-06-06T12:01:00+00:00"
    assert claimed.attempt == 1
    assert duplicate is None
    assert wrong_worker_renewal is None
    assert renewed is not None
    assert renewed.worker_id == "worker-a"
    assert renewed.heartbeat_at == "2026-06-06T12:04:00+00:00"
    assert renewed.lease_expires_at == "2026-06-06T12:09:00+00:00"
    assert checkpointed is not None
    assert checkpointed.checkpoint["phase"] == "research"
    assert checkpointed.checkpoint["evidence_ids"] == ["EVID-abcdef123456"]
    assert store.get_task_checkpoint("task-durable")["current_unit"] == "unit-1"
    assert [event["type"] for event in store.list_task_events("task-durable")] == [
        "message",
        "complete",
    ]
    assert cleared is not None
    assert cleared.worker_id is None
    assert cleared.lease_expires_at is None
    assert cleared.heartbeat_at is None


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


def test_task_store_preserves_terminal_status_from_late_updates(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="terminal protection",
        created_at="2026-06-06T12:00:00+00:00",
    )
    failed = store.update_task(
        "task-a",
        status="failed",
        archive_status="failed",
        completed_at="2026-06-06T12:02:00+00:00",
        error="pipeline failed",
    )

    late_completed = store.update_task(
        "task-a",
        status="completed",
        archive_status="ready",
        completed_at="2026-06-06T12:03:00+00:00",
        error=None,
    )

    assert failed.status == "failed"
    assert late_completed.status == "failed"
    assert late_completed.archive_status == "failed"
    assert late_completed.completed_at == "2026-06-06T12:02:00+00:00"
    assert late_completed.error == "pipeline failed"


def test_task_store_lists_and_finalizes_only_running_tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="task-running",
        user_id="user-a",
        query="running",
        created_at="2026-06-06T12:00:00+00:00",
    )
    store.create_task(
        task_id="task-terminal",
        user_id="user-a",
        query="terminal",
        created_at="2026-06-06T12:01:00+00:00",
    )
    claimed = store.claim_queued_task(
        "task-running",
        started_at="2026-06-06T12:02:00+00:00",
    )
    terminal = store.update_task(
        "task-terminal",
        status="completed",
        archive_status="ready",
        completed_at="2026-06-06T12:03:00+00:00",
    )

    running = store.list_running_tasks()
    recovered = store.finalize_stale_running_task(
        "task-running",
        completed_at="2026-06-06T12:04:00+00:00",
        error="stale_running_task_recovered:no_active_worker",
        warnings=["stale running task recovered: no_active_worker"],
    )
    terminal_recovery = store.finalize_stale_running_task(
        "task-terminal",
        completed_at="2026-06-06T12:05:00+00:00",
        error="stale_running_task_recovered:no_active_worker",
        warnings=["should not apply"],
    )

    assert claimed is not None
    assert terminal.status == "completed"
    assert [record.task_id for record in running] == ["task-running"]
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.archive_status == "failed"
    assert recovered.archive_dir is None
    assert recovered.archive_zip_path is None
    assert recovered.completed_at == "2026-06-06T12:04:00+00:00"
    assert recovered.error == "stale_running_task_recovered:no_active_worker"
    assert recovered.warnings == ["stale running task recovered: no_active_worker"]
    assert terminal_recovery is None
    unchanged = store.get_task("task-terminal")
    assert unchanged.status == "completed"
    assert unchanged.completed_at == "2026-06-06T12:03:00+00:00"
    assert unchanged.warnings == []


def test_runner_task_store_factory_requires_explicit_sqlite_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNNER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RUNNER_TASK_STORE_BACKEND", "postgres")

    with pytest.raises(RuntimeError, match="runner_postgres_database_url_missing"):
        create_task_store_from_env(sqlite_path=tmp_path / "tasks.sqlite3")

    monkeypatch.setenv(
        "RUNNER_DATABASE_URL",
        "postgresql://limira:test@postgres:5432/limira",
    )
    store = create_task_store_from_env(sqlite_path=tmp_path / "tasks.sqlite3")
    assert isinstance(store, PostgresTaskStore)
    assert store.database_url == "postgresql://limira:test@postgres:5432/limira"

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

    raw_postgresql = PostgresTaskStore("postgresql://limira:test@postgres:5432/limira")
    legacy_postgres = PostgresTaskStore("postgres://limira:test@postgres:5432/limira")
    psycopg_url = PostgresTaskStore("postgresql+psycopg://limira:test@postgres:5432/limira")
    psycopg2_url = PostgresTaskStore("postgresql+psycopg2://limira:test@postgres:5432/limira")

    assert raw_postgresql.database_url == "postgresql://limira:test@postgres:5432/limira"
    assert legacy_postgres.database_url == "postgres://limira:test@postgres:5432/limira"
    assert psycopg_url.database_url == "postgresql://limira:test@postgres:5432/limira"
    assert psycopg2_url.database_url == "postgresql://limira:test@postgres:5432/limira"
    assert conninfo_to_dict(psycopg_url.database_url)["dbname"] == "limira"
    assert conninfo_to_dict(psycopg2_url.database_url)["dbname"] == "limira"

    monkeypatch.setenv("RUNNER_TASK_STORE_BACKEND", "postgres")
    monkeypatch.setenv(
        "RUNNER_DATABASE_URL",
        "postgresql+psycopg://limira:test@postgres:5432/limira",
    )
    store = create_task_store_from_env()
    assert isinstance(store, PostgresTaskStore)
    assert store.database_url == "postgresql://limira:test@postgres:5432/limira"


def test_runner_postgres_task_store_rejects_unsupported_postgresql_plus_urls():
    with pytest.raises(RuntimeError, match="runner_postgres_database_url_required"):
        PostgresTaskStore("postgresql+asyncpg://limira:test@postgres:5432/limira")


def test_postgres_task_store_sql_targets_limira_research_tasks():
    sql = PostgresTaskStore.sql_contract().lower()

    assert "limira_research_tasks" in sql
    assert "owner_user_id" in sql
    assert "archive_status" in sql
    assert "archive_object_key" in sql
    assert "archive_zip_sha256" in sql
    assert "runner_task_id" in sql
    assert "model_summary" in sql
    assert "metadata" in sql
    assert "where task_id = %s" in sql
    assert "and status = 'queued'" in sql
    assert "where status = 'running'" in sql
    assert "and status = 'running'" in sql
    assert "runner_task_id = task_id" in sql
    assert "metadata = metadata || cast(%s as jsonb)" in sql
    assert "runner_lease" in sql
    assert "jsonb_build_object" in sql
    assert "limira_task_event_logs" in sql
    assert "source = 'runner'" in sql
    assert "returning" in sql
    assert "limira_runner_research_tasks" not in sql


def test_postgres_task_store_matches_runner_task_store_contract():
    fake_db = FakeRunnerPostgresDatabase()
    store = PostgresTaskStore(
        "postgresql://limira:test@postgres:5432/limira",
        connection_factory=fake_db.connect,
    )

    first = store.create_task(
        task_id="task-a",
        user_id="user-a",
        query="first",
        created_at="2026-06-06T12:00:00+00:00",
        model_summary={"provider": "deepseek"},
        context={
            "query": "first",
            "scenario": "sanctions",
            "conversation_id": "conversation-a",
            "document_ids": ["doc-a"],
            "upload_scope": {"document_count": 1},
            "source_policy": {"prefer_uploaded_documents": True},
        },
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
    assert first.context["scenario"] == "sanctions"
    assert first.context["document_ids"] == ["doc-a"]
    assert [record.task_id for record in store.list_user_tasks("user-a")] == [
        "task-b",
        "task-a",
    ]
    assert store.user_owns_task("task-a", "user-a") is True
    assert store.user_owns_task("task-a", "user-b") is False

    claimed = store.claim_queued_task(
        "task-a",
        started_at="2026-06-06T12:03:00+00:00",
        worker_id="worker-a",
        lease_expires_at="2026-06-06T12:08:00+00:00",
    )
    duplicate_claim = store.claim_queued_task(
        "task-a",
        started_at="2026-06-06T12:04:00+00:00",
    )
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.context["upload_scope"]["document_count"] == 1
    assert claimed.worker_id == "worker-a"
    assert claimed.heartbeat_at == "2026-06-06T12:03:00+00:00"
    assert claimed.lease_expires_at == "2026-06-06T12:08:00+00:00"
    assert claimed.attempt == 1
    assert duplicate_claim is None

    assert (
        store.renew_task_lease(
            "task-a",
            worker_id="worker-b",
            heartbeat_at="2026-06-06T12:04:30+00:00",
            lease_expires_at="2026-06-06T12:09:30+00:00",
        )
        is None
    )
    renewed = store.renew_task_lease(
        "task-a",
        worker_id="worker-a",
        heartbeat_at="2026-06-06T12:05:00+00:00",
        lease_expires_at="2026-06-06T12:10:00+00:00",
    )
    assert renewed.heartbeat_at == "2026-06-06T12:05:00+00:00"
    assert renewed.lease_expires_at == "2026-06-06T12:10:00+00:00"

    checkpointed = store.write_task_checkpoint(
        "task-a",
        checkpoint={"phase": "research", "unit": "unit-1"},
        updated_at="2026-06-06T12:05:30+00:00",
    )
    assert checkpointed.checkpoint == {"phase": "research", "unit": "unit-1"}
    assert store.get_task_checkpoint("task-a")["unit"] == "unit-1"
    store.append_task_event(
        "task-a",
        {"task_id": "task-a", "type": "message", "timestamp": "t1"},
        created_at="2026-06-06T12:05:31+00:00",
    )
    store.append_task_event(
        "task-a",
        {"task_id": "task-a", "type": "complete", "timestamp": "t2"},
        created_at="2026-06-06T12:05:32+00:00",
    )
    assert [event["type"] for event in store.list_task_events("task-a")] == [
        "message",
        "complete",
    ]

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
    assert updated.context["source_policy"]["prefer_uploaded_documents"] is True

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

    late_failed = store.update_task(
        "task-a",
        status="failed",
        archive_status="failed",
        completed_at="2026-06-06T12:13:00+00:00",
        error="late failure",
    )
    assert late_failed.status == "completed"
    assert late_failed.archive_status == "ready"
    assert late_failed.completed_at == "2026-06-06T12:10:00+00:00"
    assert late_failed.error is None

    store.claim_queued_task(
        "task-c",
        started_at="2026-06-06T12:14:00+00:00",
    )
    fake_db.rows["web-task-running"] = {
        "task_id": "web-task-running",
        "owner_user_id": "user-a",
        "query": "web-owned running task",
        "status": "running",
        "archive_status": "pending",
        "runner_task_id": "task-c",
        "archive_object_key": None,
        "archive_zip_sha256": None,
        "created_at": "2026-06-06T12:00:30+00:00",
        "started_at": "2026-06-06T12:00:45+00:00",
        "completed_at": None,
        "error": None,
        "model_summary": {},
        "metadata": {},
    }
    running = store.list_running_tasks()
    assert [record.task_id for record in running] == ["task-c"]
    web_recovery = store.finalize_stale_running_task(
        "web-task-running",
        completed_at="2026-06-06T12:14:30+00:00",
        error="should not overwrite web-owned row",
        warnings=["should not overwrite web-owned row"],
    )
    assert web_recovery is None
    assert store.get_task("web-task-running").status == "running"
    recovered = store.finalize_stale_running_task(
        "task-c",
        completed_at="2026-06-06T12:15:00+00:00",
        error="stale_running_task_recovered:no_active_worker",
        warnings=["stale running task recovered: no_active_worker"],
    )
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.archive_status == "failed"
    assert recovered.completed_at == "2026-06-06T12:15:00+00:00"
    assert recovered.error == "stale_running_task_recovered:no_active_worker"
    assert recovered.warnings == ["stale running task recovered: no_active_worker"]
    assert recovered.archive_dir is None
    assert recovered.archive_zip_path is None

    protected = store.finalize_stale_running_task(
        "task-a",
        completed_at="2026-06-06T12:16:00+00:00",
        error="should not overwrite terminal",
        warnings=["should not overwrite terminal"],
    )
    assert protected is None
    assert store.get_task("task-a").status == "completed"


def test_auth_adapter_accepts_trusted_headers_only():
    auth = authenticate_headers(
        {
            "X-Limira-Runner-Service-Token": "shared",
            "X-Limira-User-Id": "user-a",
            "X-Limira-User-Role": "admin",
        },
        service_token="shared",
    )

    assert auth.user_id == "user-a"
    assert auth.is_admin is True


def test_auth_adapter_rejects_invalid_or_body_user_id():
    with pytest.raises(AuthError) as invalid_token:
        authenticate_headers(
            {
                "X-Limira-Runner-Service-Token": "wrong",
                "X-Limira-User-Id": "user-a",
            },
            service_token="shared",
        )
    assert invalid_token.value.code == "invalid_service_token"

    with pytest.raises(AuthError) as missing_user:
        authenticate_headers(
            {"X-Limira-Runner-Service-Token": "shared"},
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
        self.event_logs = []

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
        if "insert into limira_research_tasks" in lowered:
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

        if "from limira_task_event_logs" in lowered:
            task_id, limit = params
            rows = [
                {"payload": row["payload"]}
                for row in self.database.event_logs
                if row["task_id"] == task_id
            ]
            return FakeRunnerPostgresCursor(rows[:limit])

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

        if "select" in lowered and "where status = 'running'" in lowered:
            (limit,) = params
            rows = [
                row
                for row in self.database.rows.values()
                if row["status"] == "running"
                and (
                    "runner_task_id = task_id" not in lowered
                    or row["runner_task_id"] == row["task_id"]
                )
            ]
            rows.sort(key=lambda row: row["started_at"] or row["created_at"])
            return FakeRunnerPostgresCursor(rows[:limit])

        if "set status = 'running'" in lowered:
            started_at, metadata_update, task_id = params
            row = self.database.rows.get(task_id)
            if not row or row["status"] != "queued":
                return FakeRunnerPostgresCursor([])
            row["status"] = "running"
            row["started_at"] = started_at
            metadata = _metadata_dict(row)
            metadata.update(json.loads(metadata_update))
            metadata["runner_attempt"] = int(metadata.get("runner_attempt") or 0) + 1
            row["metadata"] = metadata
            return FakeRunnerPostgresCursor([row])

        if (
            "metadata->'runner_lease'->>'worker_id'" in lowered
            and "set metadata = metadata || cast(%s as jsonb)" in lowered
            and "and status = 'running'" in lowered
        ):
            metadata_update, task_id, worker_id = params
            row = self.database.rows.get(task_id)
            if not row or row["status"] != "running":
                return FakeRunnerPostgresCursor([])
            metadata = _metadata_dict(row)
            lease = metadata.get("runner_lease")
            if not isinstance(lease, dict) or lease.get("worker_id") != worker_id:
                return FakeRunnerPostgresCursor([])
            metadata.update(json.loads(metadata_update))
            row["metadata"] = metadata
            return FakeRunnerPostgresCursor([row])

        if "where task_id = %s" in lowered and "(%s is null" in lowered:
            metadata_update, task_id, worker_id, _worker_id_again = params
            row = self.database.rows.get(task_id)
            if not row:
                return FakeRunnerPostgresCursor([])
            metadata = _metadata_dict(row)
            lease = metadata.get("runner_lease")
            if worker_id is not None and (
                not isinstance(lease, dict) or lease.get("worker_id") != worker_id
            ):
                return FakeRunnerPostgresCursor([])
            metadata.update(json.loads(metadata_update))
            row["metadata"] = metadata
            return FakeRunnerPostgresCursor([row])

        if "graph_checkpoint" in lowered and "update limira_research_tasks" in lowered:
            metadata_update, task_id = params
            row = self.database.rows.get(task_id)
            if not row:
                return FakeRunnerPostgresCursor([])
            metadata = _metadata_dict(row)
            metadata.update(json.loads(metadata_update))
            row["metadata"] = metadata
            return FakeRunnerPostgresCursor([row])

        if "insert into limira_task_event_logs" in lowered:
            task_id, event_type, payload, created_at = params
            self.database.event_logs.append(
                {
                    "task_id": task_id,
                    "event_type": event_type,
                    "payload": json.loads(payload),
                    "created_at": created_at,
                    "event_log_id": f"event-{len(self.database.event_logs) + 1}",
                }
            )
            return FakeRunnerPostgresCursor([])

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

        if "set status = 'failed'" in lowered and "and status = 'running'" in lowered:
            completed_at, error, metadata_update, task_id = params
            row = self.database.rows.get(task_id)
            if not row or row["status"] != "running":
                return FakeRunnerPostgresCursor([])
            if (
                "runner_task_id = task_id" in lowered
                and row["runner_task_id"] != row["task_id"]
            ):
                return FakeRunnerPostgresCursor([])
            row["status"] = "failed"
            row["archive_status"] = "failed"
            row["completed_at"] = completed_at
            row["error"] = error
            metadata = _metadata_dict(row)
            metadata.update(json.loads(metadata_update))
            row["metadata"] = metadata
            return FakeRunnerPostgresCursor([row])

        if "update limira_research_tasks" in lowered:
            has_terminal_guard = "status not in ('completed', 'failed', 'cancelled')" in lowered
            task_id = params[-2] if has_terminal_guard else params[-1]
            incoming_status = params[-1] if has_terminal_guard else None
            row = self.database.rows.get(task_id)
            if not row:
                return FakeRunnerPostgresCursor([])
            if (
                has_terminal_guard
                and row["status"] in {"completed", "failed", "cancelled"}
                and row["status"] != incoming_status
            ):
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
                metadata = _metadata_dict(row)
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


def _metadata_dict(row):
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        return json.loads(metadata)
    if isinstance(metadata, dict):
        return dict(metadata)
    return {}
