import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


TASK_STATUSES = {"queued", "running", "completed", "failed", "cancelled"}
ARCHIVE_STATUSES = {"pending", "ready", "failed"}


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
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mirothinker_research_tasks (
                    task_id, user_id, query, status, archive_status,
                    archive_dir, archive_zip_path, created_at, started_at,
                    completed_at, error, model_summary, warnings
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._record_values(record),
            )
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mirothinker_research_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_user_tasks(self, user_id: str, limit: int = 100) -> list[TaskRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM mirothinker_research_tasks
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
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

        assignments = ", ".join(f"{field} = ?" for field in updates)
        values = [
            self._serialize_value(value)
            if field in {"model_summary", "warnings"}
            else value
            for field, value in updates.items()
        ]
        values.append(task_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE mirothinker_research_tasks SET {assignments} WHERE task_id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise KeyError(task_id)
        record = self.get_task(task_id)
        if not record:
            raise KeyError(task_id)
        return record

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mirothinker_research_tasks (
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
                    warnings TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mirothinker_tasks_user_created
                ON mirothinker_research_tasks (user_id, created_at)
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
        )

    def _serialize_value(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _deserialize_json(self, value: str | None, default: Any) -> Any:
        if value is None:
            return default
        return json.loads(value)
