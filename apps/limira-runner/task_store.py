import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


TASK_STATUSES = {"queued", "running", "completed", "failed", "cancelled"}
FINAL_TASK_STATUSES = ("completed", "failed", "cancelled")
ARCHIVE_STATUSES = {"pending", "ready", "failed"}
RUNNER_TASK_STORE_BACKEND_ENV = "RUNNER_TASK_STORE_BACKEND"
RUNNER_DATABASE_URL_ENV = "RUNNER_DATABASE_URL"
RUNNER_ALLOW_SQLITE_TASK_STORE_ENV = "RUNNER_ALLOW_SQLITE_TASK_STORE"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    user_id: str
    query: str
    status: str
    archive_status: str
    archive_dir: str | None
    archive_zip_path: str | None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    model_summary: dict[str, Any] | None = None
    warnings: list[str] | None = None
    context: dict[str, Any] = field(default_factory=dict)
    worker_id: str | None = None
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None
    attempt: int = 0
    checkpoint: dict[str, Any] | None = None
    checkpoint_updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskStore:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._init_db()

    def create_task(
        self,
        *,
        task_id: str,
        user_id: str,
        query: str,
        created_at: str,
        model_summary: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> TaskRecord:
        record = TaskRecord(
            task_id=task_id,
            user_id=user_id,
            query=query,
            status="queued",
            archive_status="pending",
            archive_dir=None,
            archive_zip_path=None,
            created_at=created_at,
            model_summary=model_summary or {},
            warnings=[],
            context=context or {},
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO limira_runner_research_tasks (
                    task_id, user_id, query, status, archive_status,
                    archive_dir, archive_zip_path, created_at, started_at,
                    completed_at, error, model_summary, warnings, context,
                    worker_id, lease_expires_at, heartbeat_at, attempt,
                    checkpoint, checkpoint_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._record_values(record),
            )
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM limira_runner_research_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_user_tasks(self, user_id: str, limit: int = 100) -> list[TaskRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM limira_runner_research_tasks
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_running_tasks(self, limit: int = 100) -> list[TaskRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM limira_runner_research_tasks
                WHERE status = ?
                ORDER BY COALESCE(started_at, created_at) ASC
                LIMIT ?
                """,
                ("running", max(1, int(limit))),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def user_owns_task(self, task_id: str, user_id: str) -> bool:
        record = self.get_task(task_id)
        return bool(record and record.user_id == user_id)

    def update_task(self, task_id: str, **updates: Any) -> TaskRecord:
        allowed = {
            "status",
            "archive_status",
            "archive_dir",
            "archive_zip_path",
            "started_at",
            "completed_at",
            "error",
            "model_summary",
            "warnings",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported task update fields: {sorted(unknown)}")
        if "status" in updates and updates["status"] not in TASK_STATUSES:
            raise ValueError(f"unsupported task status: {updates['status']}")
        if (
            "archive_status" in updates
            and updates["archive_status"] not in ARCHIVE_STATUSES
        ):
            raise ValueError(f"unsupported archive status: {updates['archive_status']}")
        if not updates:
            record = self.get_task(task_id)
            if not record:
                raise KeyError(task_id)
            return record

        incoming_status = updates.get("status")
        assignments = ", ".join(f"{field} = ?" for field in updates)
        values = [
            self._serialize_value(value)
            if field in {"model_summary", "warnings"}
            else value
            for field, value in updates.items()
        ]
        where_clause = "WHERE task_id = ?"
        values.append(task_id)
        if incoming_status is not None:
            placeholders = ", ".join("?" for _status in FINAL_TASK_STATUSES)
            where_clause += f" AND (status NOT IN ({placeholders}) OR status = ?)"
            values.extend([*FINAL_TASK_STATUSES, incoming_status])
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE limira_runner_research_tasks SET {assignments} {where_clause}",
                values,
            )
            if cursor.rowcount == 0:
                current = self.get_task(task_id)
                if (
                    current
                    and incoming_status is not None
                    and current.status in FINAL_TASK_STATUSES
                    and current.status != incoming_status
                ):
                    return current
                raise KeyError(task_id)
        record = self.get_task(task_id)
        if not record:
            raise KeyError(task_id)
        return record

    def claim_queued_task(
        self,
        task_id: str,
        *,
        started_at: str,
        worker_id: str | None = None,
        lease_expires_at: str | None = None,
    ) -> TaskRecord | None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE limira_runner_research_tasks
                SET status = ?,
                    started_at = ?,
                    worker_id = ?,
                    lease_expires_at = ?,
                    heartbeat_at = ?,
                    attempt = attempt + 1
                WHERE task_id = ? AND status = ?
                """,
                (
                    "running",
                    started_at,
                    worker_id,
                    lease_expires_at,
                    started_at if worker_id else None,
                    task_id,
                    "queued",
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)

    def renew_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> TaskRecord | None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE limira_runner_research_tasks
                SET heartbeat_at = ?,
                    lease_expires_at = ?
                WHERE task_id = ? AND status = ? AND worker_id = ?
                """,
                (heartbeat_at, lease_expires_at, task_id, "running", worker_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)

    def clear_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
    ) -> TaskRecord | None:
        values: list[Any] = [None, None, None, task_id]
        where_clause = "WHERE task_id = ?"
        if worker_id is not None:
            where_clause += " AND worker_id = ?"
            values.append(worker_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE limira_runner_research_tasks
                SET worker_id = ?,
                    lease_expires_at = ?,
                    heartbeat_at = ?
                {where_clause}
                """,
                values,
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)

    def write_task_checkpoint(
        self,
        task_id: str,
        *,
        checkpoint: dict[str, Any],
        updated_at: str,
    ) -> TaskRecord | None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE limira_runner_research_tasks
                SET checkpoint = ?,
                    checkpoint_updated_at = ?
                WHERE task_id = ?
                """,
                (self._serialize_value(checkpoint), updated_at, task_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)

    def get_task_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        record = self.get_task(task_id)
        return record.checkpoint if record else None

    def append_task_event(
        self,
        task_id: str,
        event: dict[str, Any],
        *,
        created_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO limira_runner_task_events (task_id, event_json, created_at)
                VALUES (?, ?, ?)
                """,
                (task_id, self._serialize_value(event), created_at),
            )

    def list_task_events(
        self,
        task_id: str,
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_json
                FROM (
                    SELECT event_id, event_json
                    FROM limira_runner_task_events
                    WHERE task_id = ?
                    ORDER BY event_id DESC
                    LIMIT ?
                ) replay
                ORDER BY event_id ASC
                """,
                (task_id, max(1, int(limit))),
            ).fetchall()
        return [
            self._deserialize_json(row["event_json"], default={})
            for row in rows
            if row["event_json"] is not None
        ]

    def finalize_stale_running_task(
        self,
        task_id: str,
        *,
        completed_at: str,
        error: str,
        warnings: list[str] | None = None,
        lease_checked_at: str | None = None,
    ) -> TaskRecord | None:
        checked_at = lease_checked_at or completed_at
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE limira_runner_research_tasks
                SET status = ?,
                    archive_status = ?,
                    archive_dir = ?,
                    archive_zip_path = ?,
                    completed_at = ?,
                    error = ?,
                    warnings = ?
                WHERE task_id = ? AND status = ?
                  AND (
                      worker_id IS NULL
                      OR lease_expires_at IS NULL
                      OR lease_expires_at <= ?
                  )
                """,
                (
                    "failed",
                    "failed",
                    None,
                    None,
                    completed_at,
                    error,
                    self._serialize_value(warnings or []),
                    task_id,
                    "running",
                    checked_at,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)

    def resume_stale_running_task(
        self,
        task_id: str,
        *,
        resumed_at: str,
        warnings: list[str] | None = None,
        lease_checked_at: str | None = None,
    ) -> TaskRecord | None:
        checked_at = lease_checked_at or resumed_at
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE limira_runner_research_tasks
                SET status = ?,
                    archive_status = ?,
                    archive_dir = ?,
                    archive_zip_path = ?,
                    completed_at = ?,
                    error = ?,
                    warnings = ?,
                    worker_id = ?,
                    lease_expires_at = ?,
                    heartbeat_at = ?
                WHERE task_id = ? AND status = ?
                  AND (
                      worker_id IS NULL
                      OR lease_expires_at IS NULL
                      OR lease_expires_at <= ?
                  )
                """,
                (
                    "queued",
                    "pending",
                    None,
                    None,
                    None,
                    None,
                    self._serialize_value(warnings or []),
                    None,
                    None,
                    None,
                    task_id,
                    "running",
                    checked_at,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)

    def cancel_stale_running_task(
        self,
        task_id: str,
        *,
        completed_at: str,
        error: str,
        archive_status: str,
        archive_dir: str | None = None,
        archive_zip_path: str | None = None,
        warnings: list[str] | None = None,
        lease_checked_at: str | None = None,
    ) -> TaskRecord | None:
        if archive_status not in ARCHIVE_STATUSES:
            raise ValueError(f"unsupported archive status: {archive_status}")
        checked_at = lease_checked_at or completed_at
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE limira_runner_research_tasks
                SET status = ?,
                    archive_status = ?,
                    archive_dir = ?,
                    archive_zip_path = ?,
                    completed_at = ?,
                    error = ?,
                    warnings = ?
                WHERE task_id = ? AND status = ?
                  AND (
                      worker_id IS NULL
                      OR lease_expires_at IS NULL
                      OR lease_expires_at <= ?
                  )
                """,
                (
                    "cancelled",
                    archive_status,
                    archive_dir,
                    archive_zip_path,
                    completed_at,
                    error,
                    self._serialize_value(warnings or []),
                    task_id,
                    "running",
                    checked_at,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)

    def cancel_queued_task(
        self,
        task_id: str,
        *,
        started_at: str,
        completed_at: str,
        error: str,
    ) -> TaskRecord | None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE limira_runner_research_tasks
                SET status = ?, started_at = ?, completed_at = ?, error = ?
                WHERE task_id = ? AND status = ?
                """,
                ("cancelled", started_at, completed_at, error, task_id, "queued"),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_task(task_id)

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS limira_runner_research_tasks (
                    task_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    archive_status TEXT NOT NULL,
                    archive_dir TEXT,
                    archive_zip_path TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT,
                    model_summary TEXT,
                    warnings TEXT,
                    context TEXT,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    heartbeat_at TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    checkpoint TEXT,
                    checkpoint_updated_at TEXT
                )
                """
            )
            self._ensure_sqlite_column(conn, "context", "TEXT")
            self._ensure_sqlite_column(conn, "worker_id", "TEXT")
            self._ensure_sqlite_column(conn, "lease_expires_at", "TEXT")
            self._ensure_sqlite_column(conn, "heartbeat_at", "TEXT")
            self._ensure_sqlite_column(conn, "attempt", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_sqlite_column(conn, "checkpoint", "TEXT")
            self._ensure_sqlite_column(conn, "checkpoint_updated_at", "TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_limira_runner_tasks_user_created
                ON limira_runner_research_tasks (user_id, created_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS limira_runner_task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_limira_runner_task_events_task_event
                ON limira_runner_task_events (task_id, event_id)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _record_values(self, record: TaskRecord) -> tuple[Any, ...]:
        return (
            record.task_id,
            record.user_id,
            record.query,
            record.status,
            record.archive_status,
            record.archive_dir,
            record.archive_zip_path,
            record.created_at,
            record.started_at,
            record.completed_at,
            record.error,
            self._serialize_value(record.model_summary or {}),
            self._serialize_value(record.warnings or []),
            self._serialize_value(record.context or {}),
            record.worker_id,
            record.lease_expires_at,
            record.heartbeat_at,
            record.attempt,
            self._serialize_value(record.checkpoint or {}),
            record.checkpoint_updated_at,
        )

    def _row_to_record(self, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            user_id=row["user_id"],
            query=row["query"],
            status=row["status"],
            archive_status=row["archive_status"],
            archive_dir=row["archive_dir"],
            archive_zip_path=row["archive_zip_path"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
            model_summary=self._deserialize_json(row["model_summary"], default={}),
            warnings=self._deserialize_json(row["warnings"], default=[]),
            context=self._deserialize_json(row["context"], default={}),
            worker_id=row["worker_id"],
            lease_expires_at=row["lease_expires_at"],
            heartbeat_at=row["heartbeat_at"],
            attempt=int(row["attempt"] or 0),
            checkpoint=self._deserialize_json(row["checkpoint"], default={}),
            checkpoint_updated_at=row["checkpoint_updated_at"],
        )

    def _ensure_sqlite_column(
        self,
        conn: sqlite3.Connection,
        column_name: str,
        column_sql: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(limira_runner_research_tasks)")
        }
        if column_name not in columns:
            conn.execute(
                f"ALTER TABLE limira_runner_research_tasks ADD COLUMN {column_name} {column_sql}"
            )

    def _serialize_value(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _deserialize_json(self, value: str | None, default: Any) -> Any:
        if value is None:
            return default
        return json.loads(value)


class PostgresTaskStore:
    TASK_COLUMNS = """
        task_id,
        owner_user_id,
        query,
        status,
        archive_status,
        runner_task_id,
        archive_object_key,
        archive_zip_sha256,
        created_at,
        started_at,
        completed_at,
        error,
        model_summary,
        metadata
    """
    CREATE_TASK_SQL = f"""
        INSERT INTO limira_research_tasks (
            task_id,
            owner_user_id,
            query,
            status,
            archive_status,
            runner_task_id,
            created_at,
            model_summary,
            metadata
        )
        VALUES (
            %s,
            %s,
            %s,
            'queued',
            'pending',
            %s,
            %s,
            CAST(%s AS jsonb),
            CAST(%s AS jsonb)
        )
        RETURNING {TASK_COLUMNS}
    """
    GET_TASK_SQL = f"""
        SELECT {TASK_COLUMNS}
        FROM limira_research_tasks
        WHERE task_id = %s
    """
    LIST_USER_TASKS_SQL = f"""
        SELECT {TASK_COLUMNS}
        FROM limira_research_tasks
        WHERE owner_user_id = %s
        ORDER BY created_at DESC
        LIMIT %s
    """
    LIST_RUNNING_TASKS_SQL = f"""
        SELECT {TASK_COLUMNS}
        FROM limira_research_tasks
        WHERE status = 'running'
          AND runner_task_id = task_id
        ORDER BY COALESCE(started_at, created_at) ASC
        LIMIT %s
    """
    CLAIM_QUEUED_TASK_SQL = f"""
        UPDATE limira_research_tasks
        SET status = 'running',
            started_at = %s,
            metadata = metadata
                || CAST(%s AS jsonb)
                || jsonb_build_object(
                    'runner_attempt',
                    COALESCE((metadata->>'runner_attempt')::int, 0) + 1
                )
        WHERE task_id = %s
          AND status = 'queued'
        RETURNING {TASK_COLUMNS}
    """
    RENEW_TASK_LEASE_SQL = f"""
        UPDATE limira_research_tasks
        SET metadata = metadata || CAST(%s AS jsonb)
        WHERE task_id = %s
          AND status = 'running'
          AND metadata->'runner_lease'->>'worker_id' = %s
        RETURNING {TASK_COLUMNS}
    """
    CLEAR_TASK_LEASE_SQL = f"""
        UPDATE limira_research_tasks
        SET metadata = metadata || CAST(%s AS jsonb)
        WHERE task_id = %s
          AND (%s IS NULL OR metadata->'runner_lease'->>'worker_id' = %s)
        RETURNING {TASK_COLUMNS}
    """
    WRITE_TASK_CHECKPOINT_SQL = f"""
        UPDATE limira_research_tasks
        SET metadata = metadata || CAST(%s AS jsonb)
        WHERE task_id = %s
        RETURNING {TASK_COLUMNS}
    """
    APPEND_TASK_EVENT_SQL = """
        INSERT INTO limira_task_event_logs (
            event_log_id,
            task_id,
            event_type,
            source,
            payload,
            created_at
        )
        VALUES (%s, %s, %s, 'runner', CAST(%s AS jsonb), %s)
    """
    LIST_TASK_EVENTS_SQL = """
        SELECT payload
        FROM (
            SELECT event_log_id, created_at, payload
            FROM limira_task_event_logs
            WHERE task_id = %s
              AND source = 'runner'
            ORDER BY created_at DESC, event_log_id DESC
            LIMIT %s
        ) replay
        ORDER BY created_at ASC, event_log_id ASC
    """
    FINALIZE_STALE_RUNNING_TASK_SQL = f"""
        UPDATE limira_research_tasks
        SET status = 'failed',
            archive_status = 'failed',
            completed_at = %s,
            error = %s,
            metadata = metadata || CAST(%s AS jsonb)
        WHERE task_id = %s
          AND status = 'running'
          AND runner_task_id = task_id
          AND (
              metadata->'runner_lease' IS NULL
              OR metadata->'runner_lease' = 'null'::jsonb
              OR NULLIF(metadata->'runner_lease'->>'lease_expires_at', '') IS NULL
              OR NULLIF(metadata->'runner_lease'->>'lease_expires_at', '')::timestamptz <= %s::timestamptz
          )
        RETURNING {TASK_COLUMNS}
    """
    RESUME_STALE_RUNNING_TASK_SQL = f"""
        UPDATE limira_research_tasks
        SET status = 'queued',
            archive_status = 'pending',
            completed_at = NULL,
            error = NULL,
            metadata = metadata || CAST(%s AS jsonb)
        WHERE task_id = %s
          AND status = 'running'
          AND runner_task_id = task_id
          AND (
              metadata->'runner_lease' IS NULL
              OR metadata->'runner_lease' = 'null'::jsonb
              OR NULLIF(metadata->'runner_lease'->>'lease_expires_at', '') IS NULL
              OR NULLIF(metadata->'runner_lease'->>'lease_expires_at', '')::timestamptz <= %s::timestamptz
          )
        RETURNING {TASK_COLUMNS}
    """
    CANCEL_STALE_RUNNING_TASK_SQL = f"""
        UPDATE limira_research_tasks
        SET status = 'cancelled',
            archive_status = %s,
            completed_at = %s,
            error = %s,
            metadata = metadata || CAST(%s AS jsonb)
        WHERE task_id = %s
          AND status = 'running'
          AND runner_task_id = task_id
          AND (
              metadata->'runner_lease' IS NULL
              OR metadata->'runner_lease' = 'null'::jsonb
              OR NULLIF(metadata->'runner_lease'->>'lease_expires_at', '') IS NULL
              OR NULLIF(metadata->'runner_lease'->>'lease_expires_at', '')::timestamptz <= %s::timestamptz
          )
        RETURNING {TASK_COLUMNS}
    """
    CANCEL_QUEUED_TASK_SQL = f"""
        UPDATE limira_research_tasks
        SET status = 'cancelled',
            started_at = %s,
            completed_at = %s,
            error = %s,
            archive_status = 'failed'
        WHERE task_id = %s
          AND status = 'queued'
        RETURNING {TASK_COLUMNS}
    """

    def __init__(self, database_url: str, *, connection_factory: Any | None = None):
        normalized_database_url = _normalize_postgres_database_url(database_url)
        if normalized_database_url is None:
            raise RuntimeError("runner_postgres_database_url_required")
        self.database_url = normalized_database_url
        self._connection_factory = connection_factory

    @classmethod
    def sql_contract(cls) -> str:
        return "\n".join(
            value
            for name, value in cls.__dict__.items()
            if name.endswith("_SQL") and isinstance(value, str)
        )

    def create_task(
        self,
        *,
        task_id: str,
        user_id: str,
        query: str,
        created_at: str,
        model_summary: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> TaskRecord:
        row = self._fetch_one(
            self.CREATE_TASK_SQL,
            (
                task_id,
                user_id,
                query,
                task_id,
                created_at,
                _serialize_json(model_summary or {}),
                _serialize_json(_runner_metadata(task_context=context or {})),
            ),
        )
        if not row:
            raise RuntimeError("runner_task_insert_failed")
        return self._row_to_record(row)

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._fetch_one(self.GET_TASK_SQL, (task_id,))
        return self._row_to_record(row) if row else None

    def list_user_tasks(self, user_id: str, limit: int = 100) -> list[TaskRecord]:
        rows = self._fetch_all(self.LIST_USER_TASKS_SQL, (user_id, limit))
        return [self._row_to_record(row) for row in rows]

    def list_running_tasks(self, limit: int = 100) -> list[TaskRecord]:
        rows = self._fetch_all(self.LIST_RUNNING_TASKS_SQL, (max(1, int(limit)),))
        return [self._row_to_record(row) for row in rows]

    def user_owns_task(self, task_id: str, user_id: str) -> bool:
        record = self.get_task(task_id)
        return bool(record and record.user_id == user_id)

    def update_task(self, task_id: str, **updates: Any) -> TaskRecord:
        allowed = {
            "status",
            "archive_status",
            "archive_dir",
            "archive_zip_path",
            "started_at",
            "completed_at",
            "error",
            "model_summary",
            "warnings",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported task update fields: {sorted(unknown)}")
        if "status" in updates and updates["status"] not in TASK_STATUSES:
            raise ValueError(f"unsupported task status: {updates['status']}")
        if (
            "archive_status" in updates
            and updates["archive_status"] not in ARCHIVE_STATUSES
        ):
            raise ValueError(f"unsupported archive status: {updates['archive_status']}")
        if not updates:
            record = self.get_task(task_id)
            if not record:
                raise KeyError(task_id)
            return record

        assignments = []
        values = []
        metadata_updates = {}
        for field, value in updates.items():
            if field == "model_summary":
                assignments.append("model_summary = CAST(%s AS jsonb)")
                values.append(_serialize_json(value or {}))
            elif field in {"archive_dir", "archive_zip_path", "warnings"}:
                metadata_updates[field] = value
            else:
                assignments.append(f"{field} = %s")
                values.append(value)
        if metadata_updates:
            assignments.append("metadata = metadata || CAST(%s AS jsonb)")
            values.append(_serialize_json(metadata_updates))

        incoming_status = updates.get("status")
        where_clause = "task_id = %s"
        if incoming_status is not None:
            where_clause += (
                " AND (status NOT IN ('completed', 'failed', 'cancelled') OR status = %s)"
            )
            where_params = (task_id, incoming_status)
        else:
            where_params = (task_id,)

        sql = f"""
            UPDATE limira_research_tasks
            SET {", ".join(assignments)}
            WHERE {where_clause}
            RETURNING {self.TASK_COLUMNS}
        """
        row = self._fetch_one(sql, (*values, *where_params))
        if not row:
            current = self.get_task(task_id)
            if (
                current
                and incoming_status is not None
                and current.status in FINAL_TASK_STATUSES
                and current.status != incoming_status
            ):
                return current
            raise KeyError(task_id)
        return self._row_to_record(row)

    def claim_queued_task(
        self,
        task_id: str,
        *,
        started_at: str,
        worker_id: str | None = None,
        lease_expires_at: str | None = None,
    ) -> TaskRecord | None:
        metadata_update = {}
        if worker_id:
            metadata_update["runner_lease"] = {
                "worker_id": worker_id,
                "lease_expires_at": lease_expires_at,
                "heartbeat_at": started_at,
            }
        row = self._fetch_one(
            self.CLAIM_QUEUED_TASK_SQL,
            (started_at, _serialize_json(metadata_update), task_id),
        )
        return self._row_to_record(row) if row else None

    def renew_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> TaskRecord | None:
        metadata_update = {
            "runner_lease": {
                "worker_id": worker_id,
                "heartbeat_at": heartbeat_at,
                "lease_expires_at": lease_expires_at,
            }
        }
        row = self._fetch_one(
            self.RENEW_TASK_LEASE_SQL,
            (_serialize_json(metadata_update), task_id, worker_id),
        )
        return self._row_to_record(row) if row else None

    def clear_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
    ) -> TaskRecord | None:
        row = self._fetch_one(
            self.CLEAR_TASK_LEASE_SQL,
            (_serialize_json({"runner_lease": None}), task_id, worker_id, worker_id),
        )
        return self._row_to_record(row) if row else None

    def write_task_checkpoint(
        self,
        task_id: str,
        *,
        checkpoint: dict[str, Any],
        updated_at: str,
    ) -> TaskRecord | None:
        metadata_update = {
            "graph_checkpoint": {
                "updated_at": updated_at,
                "state": checkpoint,
            }
        }
        row = self._fetch_one(
            self.WRITE_TASK_CHECKPOINT_SQL,
            (_serialize_json(metadata_update), task_id),
        )
        return self._row_to_record(row) if row else None

    def get_task_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        record = self.get_task(task_id)
        return record.checkpoint if record else None

    def append_task_event(
        self,
        task_id: str,
        event: dict[str, Any],
        *,
        created_at: str,
    ) -> None:
        self._execute(
            self.APPEND_TASK_EVENT_SQL,
            (
                _runner_event_log_id(),
                task_id,
                str(event.get("type") or event.get("event") or "unknown"),
                _serialize_json(event),
                created_at,
            ),
        )

    def list_task_events(
        self,
        task_id: str,
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        rows = self._fetch_all(self.LIST_TASK_EVENTS_SQL, (task_id, max(1, int(limit))))
        events = []
        for row in rows:
            payload = _deserialize_json(row.get("payload"), default={})
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def finalize_stale_running_task(
        self,
        task_id: str,
        *,
        completed_at: str,
        error: str,
        warnings: list[str] | None = None,
        lease_checked_at: str | None = None,
    ) -> TaskRecord | None:
        metadata_updates = {
            "archive_dir": None,
            "archive_zip_path": None,
            "warnings": warnings or [],
        }
        checked_at = lease_checked_at or completed_at
        row = self._fetch_one(
            self.FINALIZE_STALE_RUNNING_TASK_SQL,
            (
                completed_at,
                error,
                _serialize_json(metadata_updates),
                task_id,
                checked_at,
            ),
        )
        return self._row_to_record(row) if row else None

    def resume_stale_running_task(
        self,
        task_id: str,
        *,
        resumed_at: str,
        warnings: list[str] | None = None,
        lease_checked_at: str | None = None,
    ) -> TaskRecord | None:
        metadata_updates = {
            "archive_dir": None,
            "archive_zip_path": None,
            "warnings": warnings or [],
            "runner_lease": None,
        }
        checked_at = lease_checked_at or resumed_at
        row = self._fetch_one(
            self.RESUME_STALE_RUNNING_TASK_SQL,
            (
                _serialize_json(metadata_updates),
                task_id,
                checked_at,
            ),
        )
        return self._row_to_record(row) if row else None

    def cancel_stale_running_task(
        self,
        task_id: str,
        *,
        completed_at: str,
        error: str,
        archive_status: str,
        archive_dir: str | None = None,
        archive_zip_path: str | None = None,
        warnings: list[str] | None = None,
        lease_checked_at: str | None = None,
    ) -> TaskRecord | None:
        if archive_status not in ARCHIVE_STATUSES:
            raise ValueError(f"unsupported archive status: {archive_status}")
        metadata_updates = {
            "archive_dir": archive_dir,
            "archive_zip_path": archive_zip_path,
            "warnings": warnings or [],
        }
        checked_at = lease_checked_at or completed_at
        row = self._fetch_one(
            self.CANCEL_STALE_RUNNING_TASK_SQL,
            (
                archive_status,
                completed_at,
                error,
                _serialize_json(metadata_updates),
                task_id,
                checked_at,
            ),
        )
        return self._row_to_record(row) if row else None

    def cancel_queued_task(
        self,
        task_id: str,
        *,
        started_at: str,
        completed_at: str,
        error: str,
    ) -> TaskRecord | None:
        row = self._fetch_one(
            self.CANCEL_QUEUED_TASK_SQL,
            (started_at, completed_at, error, task_id),
        )
        return self._row_to_record(row) if row else None

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()

        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg_required_for_runner_postgres_task_store") from exc
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            row = cursor.fetchone()
        return dict(row) if row else None

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._connect() as conn:
            conn.execute(sql, params)

    def _row_to_record(self, row: dict[str, Any]) -> TaskRecord:
        metadata = _deserialize_json(row.get("metadata"), default={})
        warnings = metadata.get("warnings", [])
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        context = metadata.get("task_context", {})
        if not isinstance(context, dict):
            context = {}
        lease = metadata.get("runner_lease")
        if not isinstance(lease, dict):
            lease = {}
        checkpoint_record = metadata.get("graph_checkpoint")
        if not isinstance(checkpoint_record, dict):
            checkpoint_record = {}
        checkpoint = checkpoint_record.get("state")
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        return TaskRecord(
            task_id=str(row["task_id"]),
            user_id=str(row["owner_user_id"]),
            query=str(row["query"]),
            status=str(row["status"]),
            archive_status=str(row["archive_status"]),
            archive_dir=_optional_string(metadata.get("archive_dir")),
            archive_zip_path=_optional_string(metadata.get("archive_zip_path")),
            created_at=_iso_value(row["created_at"]),
            started_at=_iso_value(row.get("started_at")),
            completed_at=_iso_value(row.get("completed_at")),
            error=_optional_string(row.get("error")),
            model_summary=_deserialize_json(row.get("model_summary"), default={}),
            warnings=warnings,
            context=context,
            worker_id=_optional_string(lease.get("worker_id")),
            lease_expires_at=_optional_string(lease.get("lease_expires_at")),
            heartbeat_at=_optional_string(lease.get("heartbeat_at")),
            attempt=int(metadata.get("runner_attempt", 0) or 0),
            checkpoint=checkpoint,
            checkpoint_updated_at=_optional_string(checkpoint_record.get("updated_at")),
        )


def create_task_store_from_env(
    env: Any = os.environ,
    *,
    sqlite_path: Path | str | None = None,
) -> TaskStore | PostgresTaskStore:
    backend = str(env.get(RUNNER_TASK_STORE_BACKEND_ENV, "postgres")).strip().lower()
    if backend in {"postgres", "postgresql"}:
        database_url = str(
            env.get(RUNNER_DATABASE_URL_ENV) or env.get("DATABASE_URL") or ""
        )
        if not database_url:
            raise RuntimeError("runner_postgres_database_url_missing")
        return PostgresTaskStore(database_url)

    if backend in {"sqlite", "local-sqlite", "local_sqlite"}:
        allow_sqlite = str(env.get(RUNNER_ALLOW_SQLITE_TASK_STORE_ENV, ""))
        if allow_sqlite.strip().lower() not in TRUTHY_ENV_VALUES:
            raise RuntimeError("runner_sqlite_task_store_requires_explicit_fallback")
        return TaskStore(sqlite_path or Path(__file__).parent / "runner_tasks.sqlite3")

    raise RuntimeError(f"unsupported_runner_task_store_backend:{backend}")


def _runner_metadata(*, task_context: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "runner_archive_dir": None,
        "archive_dir": None,
        "archive_zip_path": None,
        "warnings": [],
        "task_context": task_context or {},
        "runner_lease": None,
        "runner_attempt": 0,
        "graph_checkpoint": None,
    }


def _runner_event_log_id() -> str:
    return f"runner-{time.time_ns():020d}-{uuid.uuid4().hex}"


def _serialize_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _deserialize_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(str(value))


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _iso_value(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _is_postgres_database_url(database_url: str) -> bool:
    return _normalize_postgres_database_url(database_url) is not None


def _normalize_postgres_database_url(database_url: str) -> str | None:
    if database_url.startswith(("postgresql://", "postgres://")):
        return database_url
    for scheme in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if database_url.startswith(scheme):
            return f"postgresql://{database_url.removeprefix(scheme)}"
    return None
