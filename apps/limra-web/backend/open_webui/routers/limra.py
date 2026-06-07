from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

try:
    from open_webui.utils.auth import (
        get_admin_user as _open_webui_admin_user,
        get_verified_user as _open_webui_verified_user,
    )
except Exception:  # pragma: no cover - exercised only when imported without deps
    _open_webui_admin_user = None
    _open_webui_verified_user = None


ARCHIVE_MEMBERS = {"trace.json", "report.md", "metadata.json", "report.html"}
FORBIDDEN_BROWSER_SUBSTRINGS = {
    "/mirothinker/",
    "limra-runner:8091",
    "localhost:8091",
    "RUNNER_SERVICE_TOKEN",
}
FINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}
ARTIFACT_BUCKETS = {
    "evidence": "evidence",
    "entity": "entities",
    "relation": "relations",
    "timeline_event": "timeline_events",
    "map_feature": "map_features",
    "verification": "verifications",
    "report_section": "report_sections",
}
ARTIFACT_EVENT_TYPES = {
    "evidence_collected": "evidence",
    "entity_extracted": "entity",
    "relation_extracted": "relation",
    "timeline_event_added": "timeline_event",
    "map_feature_added": "map_feature",
    "verification_result": "verification",
    "report_section_generated": "report_section",
}
LIMRA_REPOSITORY_BACKEND_ENV = "LIMRA_REPOSITORY_BACKEND"
LIMRA_DATABASE_URL_ENV = "LIMRA_DATABASE_URL"
LIMRA_ALLOW_IN_MEMORY_REPOSITORY_ENV = "LIMRA_ALLOW_IN_MEMORY_REPOSITORY"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}

router = APIRouter()
log = logging.getLogger(__name__)


class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    scenario: str | None = None


@dataclass(frozen=True)
class LimraUser:
    id: str
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class LimraTask:
    task_id: str
    owner_user_id: str
    query: str
    status: str = "queued"
    archive_status: str = "pending"
    runner_task_id: str | None = None
    archive_object_key: str | None = None
    archive_zip_sha256: str | None = None
    scenario: str | None = None
    error: str | None = None
    model_summary: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "query": self.query,
            "status": self.status,
            "archive_status": self.archive_status,
            "scenario": self.scenario,
            "error": self.error,
            "model_summary": self.model_summary or {},
            "download_url": f"/api/limra/tasks/{self.task_id}/archive.zip"
            if self.archive_status == "ready"
            else None,
            "events_url": f"/api/limra/tasks/{self.task_id}/events",
            "artifacts_url": f"/api/limra/tasks/{self.task_id}/artifacts",
        }


class RunnerStreamConflict(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class LimraTaskRepository(Protocol):
    def create_task(
        self,
        *,
        task_id: str,
        owner_user_id: str,
        query: str,
        scenario: str | None,
        runner_task_id: str | None,
    ) -> LimraTask: ...

    def get_task(self, task_id: str) -> LimraTask | None: ...

    def get_user_task(self, task_id: str, owner_user_id: str) -> LimraTask | None: ...

    def update_task(self, task_id: str, **updates: Any) -> LimraTask: ...

    def record_artifact(
        self,
        task_id: str,
        artifact_type: str,
        artifact: dict[str, Any],
    ) -> None: ...

    def get_artifacts(self, task_id: str) -> dict[str, list[dict[str, Any]]]: ...


class RunnerResearchClientProtocol(Protocol):
    async def create_research_task(
        self,
        *,
        query: str,
        scenario: str | None,
        user: LimraUser,
    ) -> dict[str, Any]: ...

    def stream_events(
        self,
        *,
        task: LimraTask,
        user: LimraUser,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def get_task_status(
        self,
        *,
        task: LimraTask,
        user: LimraUser,
    ) -> dict[str, Any]: ...


class InMemoryLimraTaskRepository:
    def __init__(self) -> None:
        self.tasks: dict[str, LimraTask] = {}
        self.artifacts: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def create_task(
        self,
        *,
        task_id: str,
        owner_user_id: str,
        query: str,
        scenario: str | None,
        runner_task_id: str | None,
    ) -> LimraTask:
        task = LimraTask(
            task_id=task_id,
            owner_user_id=owner_user_id,
            query=query,
            scenario=scenario,
            runner_task_id=runner_task_id,
        )
        self.tasks[task_id] = task
        self.artifacts[task_id] = _empty_artifact_buckets()
        return task

    def get_task(self, task_id: str) -> LimraTask | None:
        return self.tasks.get(task_id)

    def get_user_task(self, task_id: str, owner_user_id: str) -> LimraTask | None:
        task = self.get_task(task_id)
        if not task or task.owner_user_id != owner_user_id:
            return None
        return task

    def update_task(self, task_id: str, **updates: Any) -> LimraTask:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        for field_name, value in updates.items():
            if hasattr(task, field_name):
                setattr(task, field_name, value)
        return task

    def record_artifact(
        self,
        task_id: str,
        artifact_type: str,
        artifact: dict[str, Any],
    ) -> None:
        task_artifacts = self.artifacts.setdefault(task_id, _empty_artifact_buckets())
        bucket = ARTIFACT_BUCKETS[artifact_type]
        task_artifacts[bucket].append(artifact)

    def get_artifacts(self, task_id: str) -> dict[str, list[dict[str, Any]]]:
        task_artifacts = self.artifacts.setdefault(task_id, _empty_artifact_buckets())
        return {bucket: list(items) for bucket, items in task_artifacts.items()}


class PostgresLimraTaskRepository:
    POSTGRES_ARTIFACT_TABLES = {
        "limra_research_tasks",
        "limra_artifact_events",
        "limra_evidence_items",
        "limra_entities",
        "limra_entity_relations",
        "limra_timeline_events",
        "limra_generated_reports",
        "limra_uploaded_documents",
        "limra_media_assets",
    }
    TASK_COLUMNS = """
        task_id,
        owner_user_id,
        query,
        status,
        archive_status,
        runner_task_id,
        archive_object_key,
        archive_zip_sha256,
        scenario,
        error,
        model_summary
    """
    INSERT_TASK_SQL = f"""
        INSERT INTO limra_research_tasks (
            task_id,
            owner_user_id,
            query,
            status,
            archive_status,
            runner_task_id,
            scenario,
            model_summary,
            metadata
        )
        VALUES (
            :task_id,
            :owner_user_id,
            :query,
            'queued',
            'pending',
            :runner_task_id,
            :scenario,
            CAST(:model_summary AS jsonb),
            CAST(:metadata AS jsonb)
        )
        RETURNING {TASK_COLUMNS}
    """
    SELECT_TASK_SQL = f"""
        SELECT {TASK_COLUMNS}
        FROM limra_research_tasks
        WHERE task_id = :task_id
    """
    SELECT_USER_TASK_SQL = f"""
        SELECT {TASK_COLUMNS}
        FROM limra_research_tasks
        WHERE task_id = :task_id
          AND owner_user_id = :owner_user_id
    """
    INSERT_ARTIFACT_EVENT_SQL = """
        INSERT INTO limra_artifact_events (
            artifact_id,
            task_id,
            artifact_type,
            bucket,
            payload,
            evidence_refs,
            confidence,
            notes,
            source_event_type
        )
        VALUES (
            :artifact_id,
            :task_id,
            :artifact_type,
            :bucket,
            CAST(:payload AS jsonb),
            :evidence_refs,
            :confidence,
            :notes,
            :source_event_type
        )
        ON CONFLICT (artifact_id) DO UPDATE SET
            payload = EXCLUDED.payload,
            evidence_refs = EXCLUDED.evidence_refs,
            confidence = EXCLUDED.confidence,
            notes = EXCLUDED.notes,
            source_event_type = EXCLUDED.source_event_type
    """
    SELECT_ARTIFACT_EVENTS_SQL = """
        SELECT artifact_type, payload
        FROM limra_artifact_events
        WHERE task_id = :task_id
        ORDER BY created_at ASC, artifact_id ASC
    """
    INSERT_EVIDENCE_SQL = """
        INSERT INTO limra_evidence_items (
            evidence_id,
            task_id,
            source_url,
            source_title,
            publisher,
            original_text,
            translated_text,
            summary,
            language,
            credibility,
            confidence,
            cross_verification,
            conflict_notes,
            tool_name,
            model_name,
            human_confirmed,
            metadata
        )
        VALUES (
            :evidence_id,
            :task_id,
            :source_url,
            :source_title,
            :publisher,
            :original_text,
            :translated_text,
            :summary,
            :language,
            :credibility,
            :confidence,
            CAST(:cross_verification AS jsonb),
            :conflict_notes,
            :tool_name,
            :model_name,
            :human_confirmed,
            CAST(:metadata AS jsonb)
        )
        ON CONFLICT (evidence_id) DO UPDATE SET
            source_url = EXCLUDED.source_url,
            source_title = EXCLUDED.source_title,
            summary = EXCLUDED.summary,
            confidence = EXCLUDED.confidence,
            metadata = EXCLUDED.metadata
    """
    INSERT_ENTITY_SQL = """
        INSERT INTO limra_entities (
            entity_id,
            task_id,
            entity_type,
            display_name,
            canonical_name,
            country_code,
            confidence,
            metadata
        )
        VALUES (
            :entity_id,
            :task_id,
            :entity_type,
            :display_name,
            :canonical_name,
            :country_code,
            :confidence,
            CAST(:metadata AS jsonb)
        )
        ON CONFLICT (entity_id) DO UPDATE SET
            entity_type = EXCLUDED.entity_type,
            display_name = EXCLUDED.display_name,
            canonical_name = EXCLUDED.canonical_name,
            country_code = EXCLUDED.country_code,
            confidence = EXCLUDED.confidence,
            metadata = EXCLUDED.metadata
    """
    INSERT_RELATION_SQL = """
        INSERT INTO limra_entity_relations (
            relation_id,
            task_id,
            source_entity_id,
            target_entity_id,
            relation_type,
            evidence_refs,
            confidence,
            metadata
        )
        VALUES (
            :relation_id,
            :task_id,
            :source_entity_id,
            :target_entity_id,
            :relation_type,
            :evidence_refs,
            :confidence,
            CAST(:metadata AS jsonb)
        )
        ON CONFLICT (relation_id) DO UPDATE SET
            source_entity_id = EXCLUDED.source_entity_id,
            target_entity_id = EXCLUDED.target_entity_id,
            relation_type = EXCLUDED.relation_type,
            evidence_refs = EXCLUDED.evidence_refs,
            confidence = EXCLUDED.confidence,
            metadata = EXCLUDED.metadata
    """
    INSERT_TIMELINE_SQL = """
        INSERT INTO limra_timeline_events (
            timeline_event_id,
            task_id,
            event_title,
            event_type,
            location_name,
            risk_level,
            confidence,
            evidence_refs,
            metadata
        )
        VALUES (
            :timeline_event_id,
            :task_id,
            :event_title,
            :event_type,
            :location_name,
            :risk_level,
            :confidence,
            :evidence_refs,
            CAST(:metadata AS jsonb)
        )
        ON CONFLICT (timeline_event_id) DO UPDATE SET
            event_title = EXCLUDED.event_title,
            event_type = EXCLUDED.event_type,
            location_name = EXCLUDED.location_name,
            risk_level = EXCLUDED.risk_level,
            confidence = EXCLUDED.confidence,
            evidence_refs = EXCLUDED.evidence_refs,
            metadata = EXCLUDED.metadata
    """
    INSERT_REPORT_SECTION_SQL = """
        INSERT INTO limra_generated_reports (
            report_id,
            task_id,
            report_type,
            markdown,
            html,
            evidence_refs,
            creator_user_id,
            metadata
        )
        VALUES (
            :report_id,
            :task_id,
            'section',
            :markdown,
            :html,
            :evidence_refs,
            :creator_user_id,
            CAST(:metadata AS jsonb)
        )
        ON CONFLICT (report_id) DO UPDATE SET
            markdown = EXCLUDED.markdown,
            html = EXCLUDED.html,
            evidence_refs = EXCLUDED.evidence_refs,
            creator_user_id = EXCLUDED.creator_user_id,
            metadata = EXCLUDED.metadata,
            updated_at = now()
    """

    def __init__(self, database_url: str, *, engine_factory: Any | None = None) -> None:
        if not _is_postgres_database_url(database_url):
            raise RuntimeError("limra_postgres_database_url_required")
        self.database_url = database_url
        self._engine_factory = engine_factory
        self._engine: Any | None = None

    @classmethod
    def sql_contract(cls) -> str:
        return "\n".join(
            value
            for name, value in cls.__dict__.items()
            if name.endswith("_SQL") and isinstance(value, str)
        )

    @property
    def engine(self) -> Any:
        if self._engine is None:
            if self._engine_factory is not None:
                self._engine = self._engine_factory(self.database_url)
            else:
                from sqlalchemy import create_engine

                self._engine = create_engine(self.database_url, pool_pre_ping=True)
        return self._engine

    def create_task(
        self,
        *,
        task_id: str,
        owner_user_id: str,
        query: str,
        scenario: str | None,
        runner_task_id: str | None,
    ) -> LimraTask:
        row = self._fetch_one(
            self.INSERT_TASK_SQL,
            {
                "task_id": task_id,
                "owner_user_id": owner_user_id,
                "query": query,
                "runner_task_id": runner_task_id,
                "scenario": scenario,
                "model_summary": _json_dumps({}),
                "metadata": _json_dumps({"repository": "postgres"}),
            },
        )
        if not row:
            raise RuntimeError("limra_task_insert_failed")
        return _task_from_row(row)

    def get_task(self, task_id: str) -> LimraTask | None:
        row = self._fetch_one(self.SELECT_TASK_SQL, {"task_id": task_id})
        return _task_from_row(row) if row else None

    def get_user_task(self, task_id: str, owner_user_id: str) -> LimraTask | None:
        row = self._fetch_one(
            self.SELECT_USER_TASK_SQL,
            {"task_id": task_id, "owner_user_id": owner_user_id},
        )
        return _task_from_row(row) if row else None

    def update_task(self, task_id: str, **updates: Any) -> LimraTask:
        allowed = {
            "status",
            "archive_status",
            "runner_task_id",
            "archive_object_key",
            "archive_zip_sha256",
            "scenario",
            "error",
            "model_summary",
        }
        values = {key: value for key, value in updates.items() if key in allowed}
        if not values:
            task = self.get_task(task_id)
            if not task:
                raise KeyError(task_id)
            return task

        assignments: list[str] = []
        params: dict[str, Any] = {"task_id": task_id}
        for key, value in values.items():
            if key == "model_summary":
                assignments.append(f"{key} = CAST(:{key} AS jsonb)")
                params[key] = _json_dumps(value or {})
            else:
                assignments.append(f"{key} = :{key}")
                params[key] = value

        status = values.get("status")
        if status == "running":
            assignments.append("started_at = COALESCE(started_at, now())")
        elif status in FINAL_TASK_STATUSES:
            assignments.append("completed_at = COALESCE(completed_at, now())")

        sql = f"""
            UPDATE limra_research_tasks
            SET {", ".join(assignments)}
            WHERE task_id = :task_id
            RETURNING {self.TASK_COLUMNS}
        """
        row = self._fetch_one(sql, params)
        if not row:
            raise KeyError(task_id)
        return _task_from_row(row)

    def record_artifact(
        self,
        task_id: str,
        artifact_type: str,
        artifact: dict[str, Any],
    ) -> None:
        artifact_id = _artifact_primary_id(artifact_type, artifact)
        bucket = ARTIFACT_BUCKETS[artifact_type]
        event_params = {
            "artifact_id": artifact_id,
            "task_id": task_id,
            "artifact_type": artifact_type,
            "bucket": bucket,
            "payload": _json_dumps(artifact),
            "evidence_refs": _list_of_strings(artifact.get("evidence_refs")),
            "confidence": _optional_float(artifact.get("confidence")),
            "notes": _optional_string(artifact.get("notes")),
            "source_event_type": _optional_string(artifact.get("source_event_type")),
        }
        self._execute(self.INSERT_ARTIFACT_EVENT_SQL, event_params)
        try:
            self._record_typed_artifact(task_id, artifact_type, artifact, artifact_id)
        except Exception:
            log.exception("Failed to persist typed limra artifact %s", artifact_id)

    def get_artifacts(self, task_id: str) -> dict[str, list[dict[str, Any]]]:
        artifacts = _empty_artifact_buckets()
        rows = self._fetch_all(self.SELECT_ARTIFACT_EVENTS_SQL, {"task_id": task_id})
        for row in rows:
            artifact_type = row.get("artifact_type")
            if artifact_type not in ARTIFACT_BUCKETS:
                continue
            payload = _json_loads(row.get("payload"))
            if isinstance(payload, dict):
                artifacts[ARTIFACT_BUCKETS[artifact_type]].append(payload)
        return artifacts

    def _record_typed_artifact(
        self,
        task_id: str,
        artifact_type: str,
        artifact: dict[str, Any],
        artifact_id: str,
    ) -> None:
        if artifact_type == "evidence":
            self._execute(self.INSERT_EVIDENCE_SQL, self._evidence_params(task_id, artifact))
        elif artifact_type == "entity":
            self._execute(self.INSERT_ENTITY_SQL, self._entity_params(task_id, artifact))
        elif artifact_type == "relation":
            self._execute(self.INSERT_RELATION_SQL, self._relation_params(task_id, artifact))
        elif artifact_type in {"timeline_event", "map_feature"}:
            self._execute(
                self.INSERT_TIMELINE_SQL,
                self._timeline_params(task_id, artifact_type, artifact, artifact_id),
            )
        elif artifact_type == "report_section":
            self._execute(
                self.INSERT_REPORT_SECTION_SQL,
                self._report_section_params(task_id, artifact),
            )

    def _evidence_params(self, task_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "evidence_id": str(artifact["evidence_id"]),
            "task_id": task_id,
            "source_url": artifact.get("source_url") or artifact.get("url"),
            "source_title": artifact.get("source_title") or artifact.get("title"),
            "publisher": artifact.get("publisher"),
            "original_text": artifact.get("original_text") or artifact.get("text"),
            "translated_text": artifact.get("translated_text"),
            "summary": artifact.get("summary"),
            "language": artifact.get("language"),
            "credibility": _optional_float(artifact.get("credibility")),
            "confidence": _optional_float(artifact.get("confidence")),
            "cross_verification": _json_dumps(artifact.get("cross_verification") or {}),
            "conflict_notes": artifact.get("conflict_notes"),
            "tool_name": artifact.get("tool_name"),
            "model_name": artifact.get("model_name"),
            "human_confirmed": bool(artifact.get("human_confirmed", False)),
            "metadata": _json_dumps(artifact),
        }

    def _entity_params(self, task_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        entity_type = str(artifact.get("entity_type") or artifact.get("type") or "event")
        if entity_type not in _allowed_entity_types():
            entity_type = "event"
        display_name = (
            artifact.get("display_name")
            or artifact.get("name")
            or artifact.get("title")
            or artifact["entity_id"]
        )
        return {
            "entity_id": str(artifact["entity_id"]),
            "task_id": task_id,
            "entity_type": entity_type,
            "display_name": str(display_name),
            "canonical_name": artifact.get("canonical_name"),
            "country_code": artifact.get("country_code"),
            "confidence": _optional_float(artifact.get("confidence")),
            "metadata": _json_dumps(artifact),
        }

    def _relation_params(self, task_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        relation_type = str(artifact.get("relation_type") or artifact.get("type") or "mentions")
        if relation_type not in _allowed_relation_types():
            relation_type = "mentions"
        return {
            "relation_id": str(artifact["relation_id"]),
            "task_id": task_id,
            "source_entity_id": artifact.get("source_entity_id"),
            "target_entity_id": artifact.get("target_entity_id"),
            "relation_type": relation_type,
            "evidence_refs": _list_of_strings(artifact.get("evidence_refs")),
            "confidence": _optional_float(artifact.get("confidence")),
            "metadata": _json_dumps(artifact),
        }

    def _timeline_params(
        self,
        task_id: str,
        artifact_type: str,
        artifact: dict[str, Any],
        artifact_id: str,
    ) -> dict[str, Any]:
        risk_level = str(artifact.get("risk_level") or "unknown")
        if risk_level not in {"unknown", "low", "medium", "high", "critical"}:
            risk_level = "unknown"
        return {
            "timeline_event_id": artifact_id,
            "task_id": task_id,
            "event_title": str(
                artifact.get("event_title")
                or artifact.get("title")
                or artifact.get("name")
                or artifact_id
            ),
            "event_type": artifact.get("event_type") or artifact_type,
            "location_name": artifact.get("location_name") or artifact.get("location"),
            "risk_level": risk_level,
            "confidence": _optional_float(artifact.get("confidence")),
            "evidence_refs": _list_of_strings(artifact.get("evidence_refs")),
            "metadata": _json_dumps({**artifact, "artifact_type": artifact_type}),
        }

    def _report_section_params(self, task_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        owner = self._task_owner_user_id(task_id) or "limra"
        return {
            "report_id": str(artifact["section_id"]),
            "task_id": task_id,
            "markdown": artifact.get("markdown") or artifact.get("content") or "",
            "html": artifact.get("html"),
            "evidence_refs": _list_of_strings(artifact.get("evidence_refs")),
            "creator_user_id": owner,
            "metadata": _json_dumps(artifact),
        }

    def _task_owner_user_id(self, task_id: str) -> str | None:
        row = self._fetch_one(
            "SELECT owner_user_id FROM limra_research_tasks WHERE task_id = :task_id",
            {"task_id": task_id},
        )
        return str(row["owner_user_id"]) if row and row.get("owner_user_id") else None

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            row = connection.execute(_sql_text(sql), params).mappings().first()
        return dict(row) if row else None

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self.engine.begin() as connection:
            rows = connection.execute(_sql_text(sql), params).mappings().all()
        return [dict(row) for row in rows]

    def _execute(self, sql: str, params: dict[str, Any]) -> None:
        with self.engine.begin() as connection:
            connection.execute(_sql_text(sql), params)


def create_limra_task_repository_from_env(env: Any = os.environ) -> LimraTaskRepository:
    backend = str(env.get(LIMRA_REPOSITORY_BACKEND_ENV, "postgres")).strip().lower()
    if backend in {"postgres", "postgresql"}:
        database_url = str(env.get(LIMRA_DATABASE_URL_ENV) or env.get("DATABASE_URL") or "")
        if not database_url:
            raise RuntimeError("limra_postgres_database_url_missing")
        return PostgresLimraTaskRepository(database_url)

    if backend in {"memory", "in-memory", "in_memory"}:
        if str(env.get(LIMRA_ALLOW_IN_MEMORY_REPOSITORY_ENV, "")).strip().lower() not in TRUTHY_ENV_VALUES:
            raise RuntimeError("limra_in_memory_repository_requires_explicit_fallback")
        return InMemoryLimraTaskRepository()

    raise RuntimeError(f"unsupported_limra_repository_backend:{backend}")


class RunnerResearchClient:
    def __init__(
        self,
        *,
        runner_url: str | None = None,
        service_token: str | None = None,
    ) -> None:
        self.runner_url = (runner_url or os.getenv("LIMRA_RUNNER_INTERNAL_URL") or "").rstrip(
            "/"
        )
        self.service_token = service_token or os.getenv("LIMRA_RUNNER_SERVICE_TOKEN")

    async def create_research_task(
        self,
        *,
        query: str,
        scenario: str | None,
        user: LimraUser,
    ) -> dict[str, Any]:
        if not self.runner_url:
            raise HTTPException(status_code=503, detail="runner_url_not_configured")

        payload: dict[str, Any] = {"query": query}
        if scenario:
            payload["scenario"] = scenario

        url = f"{self.runner_url}/mirothinker/research"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                json=payload,
                headers=runner_service_headers(user, self.service_token),
            )

        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="runner_research_start_failed")
        return response.json()

    async def stream_events(
        self,
        *,
        task: LimraTask,
        user: LimraUser,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.runner_url:
            raise HTTPException(status_code=503, detail="runner_url_not_configured")
        if not task.runner_task_id:
            raise HTTPException(status_code=500, detail="runner_task_id_missing")

        url = f"{self.runner_url}/mirothinker/tasks/{task.runner_task_id}/events"
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET",
                url,
                headers=runner_service_headers(user, self.service_token),
            ) as response:
                if response.status_code >= 400:
                    detail = await _runner_error_detail(response)
                    if response.status_code == 409:
                        raise RunnerStreamConflict(detail)
                    raise HTTPException(status_code=502, detail="runner_event_stream_failed")
                async for event in _iter_sse_json(response):
                    yield event

    async def get_task_status(
        self,
        *,
        task: LimraTask,
        user: LimraUser,
    ) -> dict[str, Any]:
        if not self.runner_url:
            raise HTTPException(status_code=503, detail="runner_url_not_configured")
        if not task.runner_task_id:
            raise HTTPException(status_code=500, detail="runner_task_id_missing")

        url = f"{self.runner_url}/mirothinker/tasks/{task.runner_task_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url,
                headers=runner_service_headers(user, self.service_token),
            )
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="runner_task_not_found")
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="runner_task_status_failed")
        return response.json()


class RunnerArchiveClient:
    def __init__(
        self,
        *,
        runner_url: str | None = None,
        service_token: str | None = None,
    ) -> None:
        self.runner_url = (runner_url or os.getenv("LIMRA_RUNNER_INTERNAL_URL") or "").rstrip(
            "/"
        )
        self.service_token = service_token or os.getenv("LIMRA_RUNNER_SERVICE_TOKEN")

    async def download_archive(self, task: LimraTask, user: LimraUser) -> bytes:
        if not self.runner_url:
            raise HTTPException(status_code=503, detail="runner_url_not_configured")
        runner_task_id = task.runner_task_id or task.task_id
        url = f"{self.runner_url}/mirothinker/tasks/{runner_task_id}/archive.zip"
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, headers=runner_service_headers(user, self.service_token))
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="archive_not_found")
        if response.status_code == 409:
            raise HTTPException(status_code=409, detail="archive_not_ready")
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="runner_archive_proxy_failed")
        return response.content


def runner_service_headers(user: LimraUser, service_token: str | None) -> dict[str, str]:
    headers = {
        "X-OpenWebUI-User-Id": user.id,
        "X-OpenWebUI-User-Role": user.role,
    }
    if service_token:
        headers["X-MiroThinker-Service-Token"] = service_token
    return headers


def _open_webui_verified_dependency():
    if _open_webui_verified_user is None:
        async def _missing_verified_user():
            raise HTTPException(status_code=401, detail="open_webui_auth_unavailable")

        return _missing_verified_user
    return _open_webui_verified_user


def _open_webui_admin_dependency():
    if _open_webui_admin_user is None:
        async def _missing_admin_user():
            raise HTTPException(status_code=401, detail="open_webui_auth_unavailable")

        return _missing_admin_user
    return _open_webui_admin_user


async def get_current_limra_user(user=Depends(_open_webui_verified_dependency())) -> LimraUser:
    return _limra_user_from_open_webui_user(user)


async def get_current_limra_admin(user=Depends(_open_webui_admin_dependency())) -> LimraUser:
    return _limra_user_from_open_webui_user(user)


def get_task_repository(request: Request) -> LimraTaskRepository:
    repo = getattr(request.app.state, "limra_task_repository", None)
    if repo is None:
        repo = create_limra_task_repository_from_env()
        request.app.state.limra_task_repository = repo
    return repo


def get_archive_client(request: Request) -> RunnerArchiveClient:
    client = getattr(request.app.state, "limra_archive_client", None)
    if client is None:
        client = RunnerArchiveClient()
        request.app.state.limra_archive_client = client
    return client


def get_research_client(request: Request) -> RunnerResearchClientProtocol:
    client = getattr(request.app.state, "limra_research_client", None)
    if client is None:
        client = RunnerResearchClient()
        request.app.state.limra_research_client = client
    return client


@router.post("/research", status_code=202)
async def create_research_task(
    form_data: dict[str, Any],
    request: Request,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
    research_client: RunnerResearchClientProtocol = Depends(get_research_client),
) -> dict[str, Any]:
    if "user_id" in form_data or "owner_user_id" in form_data:
        raise HTTPException(status_code=400, detail="user_id_not_allowed")
    request_data = ResearchRequest.model_validate(form_data)
    query = request_data.query.strip()
    task_id = str(uuid.uuid4())
    task = repo.create_task(
        task_id=task_id,
        owner_user_id=user.id,
        query=query,
        scenario=request_data.scenario,
        runner_task_id=None,
    )
    try:
        runner_payload = await research_client.create_research_task(
            query=query,
            scenario=request_data.scenario,
            user=user,
        )
        runner_task_id = _runner_task_id_from_payload(runner_payload)
        task = repo.update_task(
            task.task_id,
            runner_task_id=runner_task_id,
            status=str(runner_payload.get("status") or "queued"),
        )
    except HTTPException as exc:
        repo.update_task(
            task.task_id,
            status="failed",
            archive_status="failed",
            error=str(exc.detail),
        )
        raise
    except Exception as exc:
        repo.update_task(
            task.task_id,
            status="failed",
            archive_status="failed",
            error="runner_research_start_failed",
        )
        raise HTTPException(status_code=502, detail="runner_research_start_failed") from exc
    return _assert_browser_safe(
        {
            "task_id": task.task_id,
            "status": task.status,
            "scenario": task.scenario,
            "query": task.query,
            "task_url": f"/api/limra/tasks/{task.task_id}",
            "events_url": f"/api/limra/tasks/{task.task_id}/events",
            "artifacts_url": f"/api/limra/tasks/{task.task_id}/artifacts",
        }
    )


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    task = _get_owned_task(repo, task_id, user)
    return _assert_browser_safe(task.public_dict())


@router.get("/tasks/{task_id}/events")
async def get_task_events(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
    research_client: RunnerResearchClientProtocol = Depends(get_research_client),
) -> StreamingResponse:
    task = _get_owned_task(repo, task_id, user)
    return StreamingResponse(
        _limra_event_stream(task, user, repo, research_client),
        media_type="text/event-stream",
    )


@router.get("/tasks/{task_id}/artifacts")
async def get_task_artifacts(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, list[Any]]:
    _get_owned_task(repo, task_id, user)
    artifacts = repo.get_artifacts(task_id)
    artifacts["timeline"] = artifacts["timeline_events"]
    return _assert_browser_safe(artifacts)


@router.get("/tasks/{task_id}/archive.zip")
async def download_task_archive(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
    archive_client: RunnerArchiveClient = Depends(get_archive_client),
) -> Response:
    task = _get_owned_task(repo, task_id, user)
    return await _download_archive(task, user, archive_client)


@router.get("/admin/tasks/{task_id}")
async def admin_get_task(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_admin),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    payload = task.public_dict()
    payload["owner_user_id"] = task.owner_user_id
    payload["admin"] = user.id
    return _assert_browser_safe(payload)


@router.get("/admin/tasks/{task_id}/archive.zip")
async def admin_download_task_archive(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_admin),
    repo: LimraTaskRepository = Depends(get_task_repository),
    archive_client: RunnerArchiveClient = Depends(get_archive_client),
) -> Response:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    return await _download_archive(task, user, archive_client)


@router.post("/uploads", status_code=501)
async def upload_document(
    file: UploadFile,
    user: LimraUser = Depends(get_current_limra_user),
) -> dict[str, str]:
    return {"error": "upload_not_implemented", "user_id": user.id, "filename": file.filename or ""}


@router.post("/tasks/{task_id}/reports/pdf", status_code=501)
async def export_task_pdf(
    task_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, str]:
    _get_owned_task(repo, task_id, user)
    return {"error": "pdf_export_not_implemented"}


async def _download_archive(
    task: LimraTask,
    user: LimraUser,
    archive_client: RunnerArchiveClient,
) -> Response:
    if task.archive_status != "ready":
        raise HTTPException(status_code=409, detail="archive_not_ready")
    archive_bytes = await archive_client.download_archive(task, user)
    validate_archive_zip(archive_bytes)
    return Response(
        archive_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="archive.zip"'},
    )


def validate_archive_zip(archive_bytes: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=502, detail="invalid_archive_zip") from exc
    if names != ARCHIVE_MEMBERS:
        raise HTTPException(status_code=502, detail="invalid_archive_members")
    if any(name.startswith("/") or ".." in name.split("/") for name in names):
        raise HTTPException(status_code=502, detail="unsafe_archive_member")


def _get_owned_task(
    repo: LimraTaskRepository,
    task_id: str,
    user: LimraUser,
) -> LimraTask:
    task = repo.get_user_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    return task


async def _limra_event_stream(
    task: LimraTask,
    user: LimraUser,
    repo: LimraTaskRepository,
    research_client: RunnerResearchClientProtocol,
) -> AsyncIterator[bytes]:
    current = repo.get_task(task.task_id) or task
    if current.status in FINAL_TASK_STATUSES:
        yield _sse_bytes(_assert_browser_safe(_terminal_status_event(current)))
        return

    repo.update_task(task.task_id, status="running")
    saw_terminal_status = False

    try:
        async for runner_event in research_client.stream_events(task=task, user=user):
            event = _normalize_runner_event(task, runner_event)
            applied_status = _apply_task_status_from_event(repo, task.task_id, event)
            saw_terminal_status = saw_terminal_status or applied_status in FINAL_TASK_STATUSES
            warning = _record_artifact_from_event(repo, task, event)

            yield _sse_bytes(_assert_browser_safe(event))
            if warning:
                yield _sse_bytes(_assert_browser_safe(warning))

        if not saw_terminal_status:
            status_event = await _authoritative_runner_status_event(
                repo,
                task,
                user,
                research_client,
            )
            if status_event:
                yield _sse_bytes(_assert_browser_safe(status_event))
    except RunnerStreamConflict as exc:
        status_event = await _authoritative_runner_status_event(
            repo,
            task,
            user,
            research_client,
            reason=exc.reason,
        )
        if status_event:
            yield _sse_bytes(_assert_browser_safe(status_event))
    except asyncio.CancelledError:
        repo.update_task(
            task.task_id,
            status="cancelled",
            archive_status="failed",
            error="event_stream_cancelled",
        )
        return
    except HTTPException as exc:
        current = repo.get_task(task.task_id)
        if current and current.status in FINAL_TASK_STATUSES:
            yield _sse_bytes(_assert_browser_safe(_terminal_status_event(current)))
            return
        repo.update_task(
            task.task_id,
            status="failed",
            archive_status="failed",
            error=str(exc.detail),
        )
        yield _sse_bytes(
            _assert_browser_safe(
                {
                    "task_id": task.task_id,
                    "type": "error",
                    "payload": {"error": exc.detail},
                }
            )
        )
    except Exception as exc:
        current = repo.get_task(task.task_id)
        if current and current.status in FINAL_TASK_STATUSES:
            yield _sse_bytes(_assert_browser_safe(_terminal_status_event(current)))
            return
        repo.update_task(
            task.task_id,
            status="failed",
            archive_status="failed",
            error=str(exc),
        )
        yield _sse_bytes(
            _assert_browser_safe(
                {
                    "task_id": task.task_id,
                    "type": "error",
                    "payload": {"error": "limra_event_proxy_failed"},
                }
            )
        )


async def _authoritative_runner_status_event(
    repo: LimraTaskRepository,
    task: LimraTask,
    user: LimraUser,
    research_client: RunnerResearchClientProtocol,
    *,
    reason: str | None = None,
) -> dict[str, Any] | None:
    try:
        runner_status = await research_client.get_task_status(task=task, user=user)
    except HTTPException as exc:
        current = repo.get_task(task.task_id)
        if current and current.status in FINAL_TASK_STATUSES:
            return _terminal_status_event(current, reason=reason)
        return {
            "task_id": task.task_id,
            "type": "status",
            "payload": {
                "status": current.status if current else task.status,
                "archive_status": current.archive_status if current else task.archive_status,
                "status_source": "limra",
                "warning": exc.detail,
                **({"reason": reason} if reason else {}),
            },
        }

    current = _apply_authoritative_runner_status(repo, task.task_id, runner_status)
    payload: dict[str, Any] = {
        "status": current.status,
        "archive_status": current.archive_status,
        "terminal": current.status in FINAL_TASK_STATUSES,
        "status_source": "runner",
    }
    if reason:
        payload["reason"] = reason
    return {
        "task_id": task.task_id,
        "type": "status",
        "payload": payload,
    }


def _apply_authoritative_runner_status(
    repo: LimraTaskRepository,
    task_id: str,
    runner_status: dict[str, Any],
) -> LimraTask:
    current = repo.get_task(task_id)
    if not current:
        raise KeyError(task_id)

    status = runner_status.get("status")
    if status not in {"queued", "running", "completed", "failed", "cancelled"}:
        return current

    updates: dict[str, Any] = {"status": status}
    archive_status = runner_status.get("archive_status")
    if archive_status in {"pending", "ready", "failed"}:
        updates["archive_status"] = archive_status
    elif status == "completed":
        updates["archive_status"] = "ready"
    elif status in {"failed", "cancelled"}:
        updates["archive_status"] = "failed"
    if runner_status.get("error"):
        updates["error"] = str(runner_status["error"])
    return repo.update_task(task_id, **updates)


def _terminal_status_event(task: LimraTask, *, reason: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": task.status,
        "archive_status": task.archive_status,
        "terminal": True,
    }
    if task.error:
        payload["error"] = task.error
    if reason:
        payload["reason"] = reason
    return {
        "task_id": task.task_id,
        "type": "status",
        "payload": payload,
    }


def _normalize_runner_event(task: LimraTask, event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or event.get("event") or "runner_event")
    payload = event.get("payload") if "payload" in event else dict(event)
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.pop("task_id", None)
        payload.pop("stream_url", None)
        payload.pop("task_url", None)
    normalized: dict[str, Any] = {
        "task_id": task.task_id,
        "type": event_type,
        "payload": payload,
    }
    if event.get("timestamp"):
        normalized["timestamp"] = event["timestamp"]
    return normalized


def _apply_task_status_from_event(
    repo: LimraTaskRepository,
    task_id: str,
    event: dict[str, Any],
) -> str | None:
    event_type = event.get("type")
    payload = event.get("payload")
    status = None
    archive_status = None
    if event_type == "error":
        status = "failed"
    elif isinstance(payload, dict):
        status = payload.get("status")
        archive_status = payload.get("archive_status")
        data = payload.get("data")
        if not status and isinstance(data, dict):
            status = data.get("status")
        if not archive_status and isinstance(data, dict):
            archive_status = data.get("archive_status")

    if status in {"queued", "running", "completed", "failed", "cancelled"}:
        updates: dict[str, Any] = {"status": status}
        if archive_status in {"pending", "ready", "failed"}:
            updates["archive_status"] = archive_status
        elif status == "completed":
            updates["archive_status"] = "ready"
        elif status in {"failed", "cancelled"}:
            updates["archive_status"] = "failed"
        repo.update_task(task_id, **updates)
        return str(status)
    return None


def _record_artifact_from_event(
    repo: LimraTaskRepository,
    task: LimraTask,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    artifact_type, artifact_payload, metadata = _artifact_parts_from_event(event)
    if not artifact_type:
        return None

    if artifact_type not in ARTIFACT_BUCKETS:
        return _artifact_warning(task.task_id, event, "unsupported_artifact_type")
    if not isinstance(artifact_payload, dict):
        return _artifact_warning(task.task_id, event, "invalid_artifact_payload")

    artifact = dict(artifact_payload)
    artifact.setdefault("artifact_type", artifact_type)
    artifact.setdefault("source_event_type", event.get("type"))
    if metadata.get("evidence_refs") is not None:
        artifact.setdefault("evidence_refs", metadata["evidence_refs"])
    if metadata.get("confidence") is not None:
        artifact.setdefault("confidence", metadata["confidence"])
    if metadata.get("notes") is not None:
        artifact.setdefault("notes", metadata["notes"])
    _ensure_artifact_id(repo, task.task_id, artifact_type, artifact)
    repo.record_artifact(task.task_id, artifact_type, artifact)
    return None


def _artifact_parts_from_event(
    event: dict[str, Any],
) -> tuple[str | None, Any, dict[str, Any]]:
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    if event_type in ARTIFACT_EVENT_TYPES:
        return ARTIFACT_EVENT_TYPES[event_type], payload, {}

    if event_type not in {"artifact", "artifact_recorded", "record_research_artifact"}:
        return None, None, {}
    if not isinstance(payload, dict):
        return "unknown", payload, {}

    artifact_type = payload.get("artifact_type")
    return (
        str(artifact_type) if artifact_type else "unknown",
        payload.get("payload"),
        {
            "evidence_refs": payload.get("evidence_refs"),
            "confidence": payload.get("confidence"),
            "notes": payload.get("notes"),
        },
    )


def _ensure_artifact_id(
    repo: LimraTaskRepository,
    task_id: str,
    artifact_type: str,
    artifact: dict[str, Any],
) -> None:
    artifacts = repo.get_artifacts(task_id)
    bucket = ARTIFACT_BUCKETS[artifact_type]
    index = len(artifacts[bucket]) + 1
    if artifact_type == "evidence":
        artifact.setdefault("evidence_id", f"EVID-{index:03d}")
    elif artifact_type == "entity":
        artifact.setdefault("entity_id", f"ENT-{index:03d}")
    elif artifact_type == "relation":
        artifact.setdefault("relation_id", f"REL-{index:03d}")
    elif artifact_type == "timeline_event":
        artifact.setdefault("event_id", f"TIME-{index:03d}")
    elif artifact_type == "map_feature":
        artifact.setdefault("feature_id", f"MAP-{index:03d}")
    elif artifact_type == "report_section":
        artifact.setdefault("section_id", f"REPORT-{index:03d}")
    elif artifact_type == "verification":
        artifact.setdefault("verification_id", f"VERIFY-{index:03d}")


def _artifact_warning(
    task_id: str,
    event: dict[str, Any],
    warning: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "type": "artifact_warning",
        "payload": {
            "warning": warning,
            "source_event_type": event.get("type"),
        },
    }


def _sse_bytes(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


async def _iter_sse_json(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield _parse_sse_data("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    if data_lines:
        yield _parse_sse_data("\n".join(data_lines))


async def _runner_error_detail(response: httpx.Response) -> str:
    try:
        body = await response.aread()
        parsed = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        return f"runner_http_{response.status_code}"
    if isinstance(parsed, dict):
        return str(parsed.get("error") or parsed.get("detail") or f"runner_http_{response.status_code}")
    return f"runner_http_{response.status_code}"


def _parse_sse_data(data: str) -> dict[str, Any]:
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return {"type": "message", "payload": {"message": data}}
    if isinstance(parsed, dict):
        return parsed
    return {"type": "message", "payload": {"message": parsed}}


def _runner_task_id_from_payload(payload: dict[str, Any]) -> str:
    runner_task_id = payload.get("task_id") or payload.get("id")
    if not runner_task_id:
        raise HTTPException(status_code=502, detail="runner_task_id_missing")
    return str(runner_task_id)


def _empty_artifact_buckets() -> dict[str, list[dict[str, Any]]]:
    return {
        "evidence": [],
        "entities": [],
        "relations": [],
        "timeline_events": [],
        "map_features": [],
        "verifications": [],
        "report_sections": [],
    }


def _task_from_row(row: dict[str, Any]) -> LimraTask:
    return LimraTask(
        task_id=str(row["task_id"]),
        owner_user_id=str(row["owner_user_id"]),
        query=str(row["query"]),
        status=str(row.get("status") or "queued"),
        archive_status=str(row.get("archive_status") or "pending"),
        runner_task_id=_optional_string(row.get("runner_task_id")),
        archive_object_key=_optional_string(row.get("archive_object_key")),
        archive_zip_sha256=_optional_string(row.get("archive_zip_sha256")),
        scenario=_optional_string(row.get("scenario")),
        error=_optional_string(row.get("error")),
        model_summary=_json_loads(row.get("model_summary")) or {},
    )


def _artifact_primary_id(artifact_type: str, artifact: dict[str, Any]) -> str:
    key_by_type = {
        "evidence": "evidence_id",
        "entity": "entity_id",
        "relation": "relation_id",
        "timeline_event": "event_id",
        "map_feature": "feature_id",
        "verification": "verification_id",
        "report_section": "section_id",
    }
    key = key_by_type.get(artifact_type)
    value = artifact.get(key) if key else None
    if value:
        return str(value)
    return f"{artifact_type}-{uuid.uuid4()}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _list_of_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _sql_text(sql: str) -> Any:
    from sqlalchemy import text

    return text(sql)


def _is_postgres_database_url(database_url: str) -> bool:
    return database_url.startswith(("postgresql://", "postgresql+", "postgres://"))


def _allowed_entity_types() -> set[str]:
    return {
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
    }


def _allowed_relation_types() -> set[str]:
    return {
        "sanctions",
        "regulates",
        "affects_industry",
        "owns",
        "partners_with",
        "located_in",
        "supply_chain_dependency",
        "mentions",
        "conflicts_with",
    }


def _assert_browser_safe(payload: Any) -> Any:
    encoded = json.dumps(payload, ensure_ascii=False)
    leaked = [needle for needle in FORBIDDEN_BROWSER_SUBSTRINGS if needle in encoded]
    if leaked:
        raise HTTPException(status_code=500, detail="browser_payload_leak")
    return payload


def _limra_user_from_open_webui_user(user: Any) -> LimraUser:
    user_id = getattr(user, "id", None) or getattr(user, "email", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="missing_user_id")
    return LimraUser(id=str(user_id), role=str(getattr(user, "role", "user")))
