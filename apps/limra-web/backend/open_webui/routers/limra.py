from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
import re
import uuid
import zipfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

try:
    from open_webui.utils.auth import (
        get_admin_user as _open_webui_admin_user,
        get_verified_user as _open_webui_verified_user,
    )
except Exception:  # pragma: no cover - exercised only when imported without deps
    _open_webui_admin_user = None
    _open_webui_verified_user = None


ARCHIVE_MEMBER_ORDER = ("metadata.json", "report.html", "report.md", "trace.json")
ARCHIVE_MEMBERS = set(ARCHIVE_MEMBER_ORDER)
LIMRA_SECRET_REDACTION = "[REDACTED]"
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
ARTIFACT_TYPE_EVENTS = {
    artifact_type: event_type
    for event_type, artifact_type in ARTIFACT_EVENT_TYPES.items()
}
LIMRA_REPOSITORY_BACKEND_ENV = "LIMRA_REPOSITORY_BACKEND"
LIMRA_DATABASE_URL_ENV = "LIMRA_DATABASE_URL"
LIMRA_ALLOW_IN_MEMORY_REPOSITORY_ENV = "LIMRA_ALLOW_IN_MEMORY_REPOSITORY"
LIMRA_RUNTIME_STATE_BACKEND_ENV = "LIMRA_RUNTIME_STATE_BACKEND"
LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE_ENV = "LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE"
LIMRA_RUNTIME_STATE_KEY_PREFIX_ENV = "LIMRA_RUNTIME_STATE_KEY_PREFIX"
LIMRA_RUNTIME_STATE_TTL_SECONDS_ENV = "LIMRA_RUNTIME_STATE_TTL_SECONDS"
LIMRA_OBJECT_STORAGE_BACKEND_ENV = "LIMRA_OBJECT_STORAGE_BACKEND"
LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE_ENV = "LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE"
LIMRA_OBJECT_BUCKET_ENV = "LIMRA_OBJECT_BUCKET"
LIMRA_OBJECT_KEY_PREFIX_ENV = "LIMRA_OBJECT_KEY_PREFIX"
LIMRA_OBJECT_STORAGE_ENDPOINT_ENV = "S3_ENDPOINT_URL"
LIMRA_OBJECT_ACCESS_KEY_ENV = "AWS_ACCESS_KEY_ID"
LIMRA_OBJECT_SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"
LIMRA_OBJECT_REGION_ENV = "AWS_REGION"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
OBJECT_KEY_FORBIDDEN_FIELDS = {
    "object_key",
    "objectKey",
    "s3_key",
    "s3Key",
    "minio_object_key",
    "minioObjectKey",
}
OBJECT_KEY_CATEGORIES = {"uploads", "reports", "archives", "media"}
OBJECT_KEY_ALLOWED_EXTENSIONS = {
    ".bin",
    ".csv",
    ".html",
    ".json",
    ".md",
    ".pdf",
    ".txt",
    ".zip",
}
REPORT_BLOCKED_HTML_TAGS = (
    "base",
    "button",
    "canvas",
    "embed",
    "form",
    "iframe",
    "img",
    "input",
    "link",
    "math",
    "meta",
    "object",
    "script",
    "select",
    "source",
    "style",
    "svg",
    "textarea",
    "track",
    "video",
)
REPORT_CSP = (
    "default-src 'none'; "
    "script-src 'none'; "
    "connect-src 'none'; "
    "img-src 'none'; "
    "font-src 'none'; "
    "media-src 'none'; "
    "object-src 'none'; "
    "frame-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)
SECRET_FIELD_PATTERN = re.compile(
    r"(?i)(authorization|cookie|set-cookie|api[_-]?key|secret|token|password|"
    r"runner_service_token|serper|jina|e2b|openai|deepseek)"
)
SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-=]+"),
    re.compile(r"(?i)\b(?:Authorization|Cookie|Set-Cookie)\s*[:=]\s*[^\n\r<>{}\[\]]+"),
    re.compile(
        r"(?i)\b(?:RUNNER_SERVICE_TOKEN|SERPER_API_KEY|JINA_API_KEY|E2B_API_KEY|"
        r"OPENAI_API_KEY|DEEPSEEK_API_KEY|[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD))"
        r"\s*[:=]\s*['\"]?[^'\"\s,;&<>]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){1,2}\b"),
    re.compile(
        r"(?i)([?&](?:api[_-]?key|apikey|key|token|access_token|id_token|"
        r"refresh_token|auth|authorization|cookie|secret|password|runner_service_token|"
        r"serper_api_key|jina_api_key|e2b_api_key|openai_api_key|deepseek_api_key)=)"
        r"[^&#\s<>'\"]+"
    ),
)

router = APIRouter()
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LimraDemoScenario:
    scenario_id: str
    title: str
    description: str
    default_query: str
    focus_areas: tuple[str, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.scenario_id,
            "title": self.title,
            "description": self.description,
            "default_query": self.default_query,
            "focus_areas": list(self.focus_areas),
        }

    def runner_query(self, query: str) -> str:
        focus = "\n".join(f"- {item}" for item in self.focus_areas)
        return (
            f"limra built-in demo scenario: {self.title} ({self.scenario_id}).\n"
            f"Scenario objective: {self.description}\n\n"
            f"Focus areas:\n{focus}\n\n"
            "Artifact requirements:\n"
            "- Use record_research_artifact for source-backed evidence records, "
            "entities, relations, timeline events, map features, verification "
            "notes, and report sections.\n"
            "- Use task-local references such as EVID-001, ENT-001, TL-001, "
            "MAP-001, and REPORT-001 when creating artifacts.\n"
            "- Each evidence artifact should include a title, URL, source, "
            "summary, confidence, and published_at when available.\n"
            "- If a trustworthy location exists, create map_feature artifacts "
            "with GeoJSON Point, LineString, or Polygon geometry; otherwise add "
            "a report_section that states why the map remains empty.\n"
            "- Report sections must cite evidence_refs such as [EVID-001] so "
            "the limra report and archive can link back to evidence.\n"
            "- Treat unsupported or low-confidence claims as verification "
            "artifacts instead of silently omitting them.\n\n"
            f"User research question:\n{query}"
        )


LIMRA_DEMO_SCENARIOS: dict[str, LimraDemoScenario] = {
    scenario.scenario_id: scenario
    for scenario in (
        LimraDemoScenario(
            scenario_id="sanctions_export_controls",
            title="Sanctions and export controls",
            description=(
                "Track sanctions, export-control, entity-list, and licensing "
                "changes affecting a company, sector, or supply chain."
            ),
            default_query=(
                "Track recent export control changes affecting semiconductor "
                "supply chains and identify entities, jurisdictions, and dates."
            ),
            focus_areas=(
                "Official sanctions, export-control, entity-list, and licensing notices",
                "Affected companies, intermediaries, jurisdictions, and supply-chain links",
                "Effective dates, enforcement milestones, and compliance deadlines",
                "Source-backed confidence grading for conflicting or unclear claims",
            ),
        ),
        LimraDemoScenario(
            scenario_id="geopolitical_risk_assessment",
            title="Geopolitical risk assessment",
            description=(
                "Assess current political, security, regulatory, and trade risks "
                "for a target country, corridor, investment, or operation."
            ),
            default_query=(
                "Assess geopolitical risk for a logistics route crossing the Red "
                "Sea and identify current incidents, actors, and risk indicators."
            ),
            focus_areas=(
                "Recent official advisories, incidents, sanctions, and regulatory actions",
                "State and non-state actors, alliances, chokepoints, and exposed assets",
                "Timeline of risk escalation, de-escalation, and announced mitigations",
                "Geographic hotspots or a clear map-empty rationale when coordinates are unreliable",
            ),
        ),
        LimraDemoScenario(
            scenario_id="critical_minerals_competition",
            title="Critical minerals competition",
            description=(
                "Map international competition over critical minerals, including "
                "projects, offtake deals, processing capacity, policy moves, and "
                "strategic chokepoints."
            ),
            default_query=(
                "Analyze recent international competition over lithium and nickel "
                "supply chains, including projects, policy moves, and chokepoints."
            ),
            focus_areas=(
                "Mine, refinery, processing, and transport assets with verifiable locations",
                "Government policy moves, investment screening, subsidies, and trade restrictions",
                "Companies, state-backed investors, offtake agreements, and supply-chain dependencies",
                "Evidence-backed timeline and map artifacts for projects and chokepoints",
            ),
        ),
    )
}


class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    scenario: str | None = Field(default=None, max_length=120)


class ReportPdfRequest(BaseModel):
    report_id: str | None = Field(default=None, max_length=120)
    report_type: str = Field(default="final", max_length=80)
    markdown: str = Field(min_length=1, max_length=2_000_000)
    html: str | None = Field(default=None, max_length=2_000_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=2_000)


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


@dataclass
class LimraUploadedDocument:
    document_id: str
    owner_user_id: str
    task_id: str | None
    original_filename: str
    content_type: str | None
    byte_size: int
    minio_bucket: str
    object_key: str
    extracted_text: str | None = None
    language: str | None = None
    metadata: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "task_id": self.task_id,
            "filename": self.original_filename,
            "content_type": self.content_type,
            "byte_size": self.byte_size,
            "language": self.language,
            "extracted_text_chars": len(self.extracted_text or ""),
            "download_url": f"/api/limra/uploads/{self.document_id}/download",
        }


@dataclass
class LimraGeneratedReport:
    report_id: str
    task_id: str
    report_type: str
    markdown: str
    html: str | None
    pdf_object_key: str | None
    evidence_refs: list[str]
    creator_user_id: str
    metadata: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        metadata = self.metadata or {}
        return {
            "report_id": self.report_id,
            "task_id": self.task_id,
            "report_type": self.report_type,
            "evidence_refs": list(self.evidence_refs),
            "markdown_chars": len(self.markdown or ""),
            "html_chars": len(self.html or ""),
            "pdf_size_bytes": metadata.get("pdf_size_bytes"),
            "pdf_sha256": metadata.get("pdf_sha256"),
            "pdf_url": f"/api/limra/tasks/{self.task_id}/reports/{self.report_id}/pdf"
            if self.pdf_object_key
            else None,
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

    def record_artifact_trace_event(
        self,
        task_id: str,
        event: dict[str, Any],
    ) -> None: ...

    def get_artifact_trace_events(self, task_id: str) -> list[dict[str, Any]]: ...

    def record_uploaded_document(
        self,
        *,
        document_id: str,
        owner_user_id: str,
        task_id: str | None,
        original_filename: str,
        content_type: str | None,
        byte_size: int,
        minio_bucket: str,
        object_key: str,
        extracted_text: str | None,
        language: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> LimraUploadedDocument: ...

    def get_user_document(
        self,
        document_id: str,
        owner_user_id: str,
    ) -> LimraUploadedDocument | None: ...

    def list_user_documents(
        self,
        *,
        owner_user_id: str,
        task_id: str | None = None,
    ) -> list[LimraUploadedDocument]: ...

    def record_generated_report(
        self,
        *,
        report_id: str,
        task_id: str,
        report_type: str,
        markdown: str,
        html: str | None,
        pdf_object_key: str | None,
        evidence_refs: list[str],
        creator_user_id: str,
        metadata: Mapping[str, Any] | None,
    ) -> LimraGeneratedReport: ...

    def get_user_report(
        self,
        *,
        task_id: str,
        report_id: str,
        owner_user_id: str,
    ) -> LimraGeneratedReport | None: ...

    def list_task_reports(
        self,
        *,
        task_id: str,
    ) -> list[LimraGeneratedReport]: ...


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


class LimraRuntimeState(Protocol):
    async def get_task_runtime(
        self,
        task_id: str,
    ) -> dict[str, Any]: ...

    async def try_open_stream(
        self,
        task_id: str,
        *,
        owner_user_id: str,
        stream_id: str,
        fields: dict[str, Any],
    ) -> bool: ...

    async def update_task_runtime(
        self,
        task_id: str,
        fields: dict[str, Any],
    ) -> None: ...

    async def close_stream(
        self,
        task_id: str,
        *,
        stream_id: str,
        fields: dict[str, Any],
    ) -> bool: ...


@dataclass(frozen=True)
class LimraStoredObject:
    object_key: str
    bucket: str
    content_type: str
    size_bytes: int
    sha256: str
    metadata: dict[str, str]


class LimraObjectStorage(Protocol):
    async def put_object(
        self,
        *,
        object_key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> LimraStoredObject: ...

    async def get_object(
        self,
        *,
        object_key: str,
    ) -> bytes: ...


class LimraPdfExporter(Protocol):
    async def render_pdf(self, html_content: str) -> bytes: ...


class InMemoryLimraTaskRepository:
    def __init__(self) -> None:
        self.tasks: dict[str, LimraTask] = {}
        self.artifacts: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.artifact_trace_events: dict[str, list[dict[str, Any]]] = {}
        self.uploaded_documents: dict[str, LimraUploadedDocument] = {}
        self.generated_reports: dict[tuple[str, str], LimraGeneratedReport] = {}

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
        self.artifact_trace_events[task_id] = []
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

    def _invalidate_archive_metadata(self, task_id: str | None) -> None:
        if not task_id:
            return
        task = self.get_task(task_id)
        if not task or (task.archive_object_key is None and task.archive_zip_sha256 is None):
            return
        task.archive_object_key = None
        task.archive_zip_sha256 = None

    def record_artifact(
        self,
        task_id: str,
        artifact_type: str,
        artifact: dict[str, Any],
    ) -> None:
        task_artifacts = self.artifacts.setdefault(task_id, _empty_artifact_buckets())
        bucket = ARTIFACT_BUCKETS[artifact_type]
        task_artifacts[bucket].append(artifact)
        self.record_artifact_trace_event(
            task_id,
            _artifact_trace_event_from_artifact(artifact_type, bucket, artifact),
        )
        self._invalidate_archive_metadata(task_id)

    def get_artifacts(self, task_id: str) -> dict[str, list[dict[str, Any]]]:
        task_artifacts = self.artifacts.setdefault(task_id, _empty_artifact_buckets())
        return {bucket: list(items) for bucket, items in task_artifacts.items()}

    def record_artifact_trace_event(
        self,
        task_id: str,
        event: dict[str, Any],
    ) -> None:
        self.artifact_trace_events.setdefault(task_id, []).append(dict(event))
        self._invalidate_archive_metadata(task_id)

    def get_artifact_trace_events(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(event) for event in self.artifact_trace_events.get(task_id, [])]

    def record_uploaded_document(
        self,
        *,
        document_id: str,
        owner_user_id: str,
        task_id: str | None,
        original_filename: str,
        content_type: str | None,
        byte_size: int,
        minio_bucket: str,
        object_key: str,
        extracted_text: str | None,
        language: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> LimraUploadedDocument:
        document = LimraUploadedDocument(
            document_id=document_id,
            owner_user_id=owner_user_id,
            task_id=task_id,
            original_filename=original_filename,
            content_type=content_type,
            byte_size=byte_size,
            minio_bucket=minio_bucket,
            object_key=object_key,
            extracted_text=extracted_text,
            language=language,
            metadata=dict(metadata or {}),
        )
        self.uploaded_documents[document_id] = document
        self._invalidate_archive_metadata(task_id)
        return document

    def get_user_document(
        self,
        document_id: str,
        owner_user_id: str,
    ) -> LimraUploadedDocument | None:
        document = self.uploaded_documents.get(document_id)
        if not document or document.owner_user_id != owner_user_id:
            return None
        return document

    def list_user_documents(
        self,
        *,
        owner_user_id: str,
        task_id: str | None = None,
    ) -> list[LimraUploadedDocument]:
        documents = [
            document
            for document in self.uploaded_documents.values()
            if document.owner_user_id == owner_user_id
            and (task_id is None or document.task_id == task_id)
        ]
        return sorted(documents, key=lambda document: document.document_id)

    def record_generated_report(
        self,
        *,
        report_id: str,
        task_id: str,
        report_type: str,
        markdown: str,
        html: str | None,
        pdf_object_key: str | None,
        evidence_refs: list[str],
        creator_user_id: str,
        metadata: Mapping[str, Any] | None,
    ) -> LimraGeneratedReport:
        report = LimraGeneratedReport(
            report_id=report_id,
            task_id=task_id,
            report_type=report_type,
            markdown=markdown,
            html=html,
            pdf_object_key=pdf_object_key,
            evidence_refs=list(evidence_refs),
            creator_user_id=creator_user_id,
            metadata=dict(metadata or {}),
        )
        self.generated_reports[(task_id, report_id)] = report
        self._invalidate_archive_metadata(task_id)
        return report

    def get_user_report(
        self,
        *,
        task_id: str,
        report_id: str,
        owner_user_id: str,
    ) -> LimraGeneratedReport | None:
        task = self.get_user_task(task_id, owner_user_id)
        if not task:
            return None
        return self.generated_reports.get((task_id, report_id))

    def list_task_reports(
        self,
        *,
        task_id: str,
    ) -> list[LimraGeneratedReport]:
        reports = [
            report
            for (report_task_id, _report_id), report in self.generated_reports.items()
            if report_task_id == task_id
        ]
        return sorted(
            reports,
            key=lambda report: (report.report_type != "final", report.report_id),
        )


class InMemoryLimraRuntimeState:
    def __init__(self) -> None:
        self.task_runtime: dict[str, dict[str, Any]] = {}

    async def get_task_runtime(
        self,
        task_id: str,
    ) -> dict[str, Any]:
        return dict(self.task_runtime.get(task_id, {}))

    async def try_open_stream(
        self,
        task_id: str,
        *,
        owner_user_id: str,
        stream_id: str,
        fields: dict[str, Any],
    ) -> bool:
        task_state = self.task_runtime.setdefault(task_id, {})
        if task_state.get("stream_state") == "open":
            return False
        task_state.update(
            {
                **fields,
                "owner_user_id": owner_user_id,
                "stream_id": stream_id,
                "stream_state": "open",
            }
        )
        return True

    async def update_task_runtime(
        self,
        task_id: str,
        fields: dict[str, Any],
    ) -> None:
        task_state = self.task_runtime.setdefault(task_id, {})
        task_state.update(fields)

    async def close_stream(
        self,
        task_id: str,
        *,
        stream_id: str,
        fields: dict[str, Any],
    ) -> bool:
        task_state = self.task_runtime.setdefault(task_id, {})
        if task_state.get("stream_id") not in {None, stream_id}:
            return False
        task_state.update(fields)
        task_state["stream_state"] = "closed"
        return True


class RedisLimraRuntimeState:
    TRY_OPEN_STREAM_SCRIPT = """
    -- limra_try_open_stream
    local stream_state = redis.call("HGET", KEYS[1], "stream_state")
    if stream_state == '"open"' then
        return 0
    end
    for index = 2, #ARGV, 2 do
        redis.call("HSET", KEYS[1], ARGV[index], ARGV[index + 1])
    end
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[1]))
    return 1
    """

    CLOSE_STREAM_SCRIPT = """
    -- limra_close_stream
    local current_stream_id = redis.call("HGET", KEYS[1], "stream_id")
    if current_stream_id and current_stream_id ~= ARGV[2] then
        return 0
    end
    for index = 3, #ARGV, 2 do
        redis.call("HSET", KEYS[1], ARGV[index], ARGV[index + 1])
    end
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[1]))
    return 1
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        key_prefix: str = "limra:runtime",
        ttl_seconds: int = 86_400,
    ) -> None:
        if redis_client is None:
            raise RuntimeError("limra_redis_runtime_state_missing")
        self.redis_client = redis_client
        self.key_prefix = key_prefix.rstrip(":")
        self.ttl_seconds = ttl_seconds

    async def get_task_runtime(
        self,
        task_id: str,
    ) -> dict[str, Any]:
        key = self.task_key(task_id)
        raw_state = await _maybe_await(self.redis_client.hgetall(key))
        return _runtime_hash_from_redis(raw_state)

    async def try_open_stream(
        self,
        task_id: str,
        *,
        owner_user_id: str,
        stream_id: str,
        fields: dict[str, Any],
    ) -> bool:
        key = self.task_key(task_id)
        mapping = _runtime_mapping(
            {
                **fields,
                "owner_user_id": owner_user_id,
                "stream_id": stream_id,
                "stream_state": "open",
            }
        )
        result = await _maybe_await(
            self.redis_client.eval(
                self.TRY_OPEN_STREAM_SCRIPT,
                1,
                key,
                self.ttl_seconds,
                *_flatten_runtime_mapping(mapping),
            )
        )
        return bool(result)

    async def update_task_runtime(
        self,
        task_id: str,
        fields: dict[str, Any],
    ) -> None:
        key = self.task_key(task_id)
        mapping = _runtime_mapping(fields)
        if not mapping:
            return
        await _maybe_await(self.redis_client.hset(key, mapping=mapping))
        await _maybe_await(self.redis_client.expire(key, self.ttl_seconds))

    async def close_stream(
        self,
        task_id: str,
        *,
        stream_id: str,
        fields: dict[str, Any],
    ) -> bool:
        key = self.task_key(task_id)
        mapping = _runtime_mapping({"stream_state": "closed", **fields})
        result = await _maybe_await(
            self.redis_client.eval(
                self.CLOSE_STREAM_SCRIPT,
                1,
                key,
                self.ttl_seconds,
                json.dumps(stream_id, ensure_ascii=False),
                *_flatten_runtime_mapping(mapping),
            )
        )
        return bool(result)

    def task_key(self, task_id: str) -> str:
        return f"{self.key_prefix}:task:{task_id}"


class InMemoryLimraObjectStorage:
    def __init__(self, *, bucket: str = "limra-memory") -> None:
        self.bucket = bucket
        self.objects: dict[str, dict[str, Any]] = {}

    async def put_object(
        self,
        *,
        object_key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> LimraStoredObject:
        stored = _stored_object(
            object_key=object_key,
            bucket=self.bucket,
            data=data,
            content_type=content_type,
            metadata=metadata,
        )
        self.objects[object_key] = {
            "data": bytes(data),
            "content_type": content_type,
            "metadata": stored.metadata,
            "sha256": stored.sha256,
        }
        return stored

    async def get_object(
        self,
        *,
        object_key: str,
    ) -> bytes:
        try:
            return bytes(self.objects[object_key]["data"])
        except KeyError as exc:
            raise FileNotFoundError(object_key) from exc


class S3LimraObjectStorage:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        region_name: str | None = None,
        s3_client: Any | None = None,
    ) -> None:
        if not bucket:
            raise RuntimeError("limra_object_bucket_missing")
        if not endpoint_url:
            raise RuntimeError("limra_s3_endpoint_url_missing")
        if not access_key_id or not secret_access_key:
            raise RuntimeError("limra_s3_credentials_missing")
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.region_name = region_name
        self.s3_client = s3_client or self._create_s3_client()

    def _create_s3_client(self) -> Any:
        try:
            import boto3
        except Exception as exc:  # pragma: no cover - depends on runtime image
            raise RuntimeError("limra_s3_client_dependency_missing") from exc
        client_kwargs = {
            "endpoint_url": self.endpoint_url,
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
        }
        if self.region_name:
            client_kwargs["region_name"] = self.region_name
        return boto3.client("s3", **client_kwargs)

    async def put_object(
        self,
        *,
        object_key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> LimraStoredObject:
        stored = _stored_object(
            object_key=object_key,
            bucket=self.bucket,
            data=data,
            content_type=content_type,
            metadata=metadata,
        )
        await _maybe_await(
            self.s3_client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=bytes(data),
                ContentType=stored.content_type,
                Metadata=stored.metadata,
            )
        )
        return stored

    async def get_object(
        self,
        *,
        object_key: str,
    ) -> bytes:
        try:
            response = await _maybe_await(
                self.s3_client.get_object(Bucket=self.bucket, Key=object_key)
            )
            body = response["Body"]
            data = await _maybe_await(body.read())
            return bytes(data)
        except Exception as exc:
            raise FileNotFoundError(object_key) from exc


async def _abort_playwright_route(route: Any) -> None:
    await route.abort()


class PlaywrightLimraPdfExporter:
    async def render_pdf(self, html_content: str) -> bytes:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - depends on runtime image
            raise RuntimeError("limra_playwright_dependency_missing") from exc

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            try:
                page = await browser.new_page()
                await page.route("**/*", _abort_playwright_route)
                await page.set_content(html_content, wait_until="load")
                pdf_bytes = await page.pdf(format="A4", print_background=True)
            finally:
                await browser.close()
        return bytes(pdf_bytes)


class PostgresLimraTaskRepository:
    POSTGRES_ARTIFACT_TABLES = {
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
    DOCUMENT_COLUMNS = """
        document_id,
        task_id,
        owner_user_id,
        original_filename,
        content_type,
        byte_size,
        minio_bucket,
        object_key,
        extracted_text,
        language,
        metadata
    """
    REPORT_COLUMNS = """
        report_id,
        task_id,
        report_type,
        markdown,
        html,
        pdf_object_key,
        evidence_refs,
        creator_user_id,
        metadata
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
            task_id,
            local_artifact_id,
            artifact_type,
            bucket,
            payload,
            evidence_refs,
            confidence,
            notes,
            source_event_type
        )
        VALUES (
            :task_id,
            :local_artifact_id,
            :artifact_type,
            :bucket,
            CAST(:payload AS jsonb),
            :evidence_refs,
            :confidence,
            :notes,
            :source_event_type
        )
        ON CONFLICT (task_id, artifact_type, local_artifact_id) DO UPDATE SET
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
        ORDER BY created_at ASC, local_artifact_id ASC
    """
    INSERT_ARTIFACT_TRACE_EVENT_SQL = """
        INSERT INTO limra_artifact_trace_events (
            task_id,
            event_type,
            artifact_type,
            bucket,
            local_artifact_id,
            payload,
            source_event_type
        )
        VALUES (
            :task_id,
            :event_type,
            :artifact_type,
            :bucket,
            :local_artifact_id,
            CAST(:payload AS jsonb),
            :source_event_type
        )
    """
    SELECT_ARTIFACT_TRACE_EVENTS_SQL = """
        SELECT
            event_type,
            artifact_type,
            bucket,
            local_artifact_id,
            payload,
            source_event_type
        FROM limra_artifact_trace_events
        WHERE task_id = :task_id
        ORDER BY created_at ASC, trace_event_id ASC
    """
    INSERT_EVIDENCE_SQL = """
        INSERT INTO limra_evidence_items (
            evidence_id,
            task_id,
            source_url,
            source_title,
            publisher,
            published_at,
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
            CAST(:published_at AS timestamptz),
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
        ON CONFLICT (task_id, evidence_id) DO UPDATE SET
            source_url = EXCLUDED.source_url,
            source_title = EXCLUDED.source_title,
            publisher = EXCLUDED.publisher,
            published_at = EXCLUDED.published_at,
            original_text = EXCLUDED.original_text,
            translated_text = EXCLUDED.translated_text,
            summary = EXCLUDED.summary,
            language = EXCLUDED.language,
            credibility = EXCLUDED.credibility,
            confidence = EXCLUDED.confidence,
            cross_verification = EXCLUDED.cross_verification,
            conflict_notes = EXCLUDED.conflict_notes,
            tool_name = EXCLUDED.tool_name,
            model_name = EXCLUDED.model_name,
            human_confirmed = EXCLUDED.human_confirmed,
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
            geometry,
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
            CASE
                WHEN :geometry_geojson IS NOT NULL
                    THEN ST_SetSRID(ST_GeomFromGeoJSON(:geometry_geojson), 4326)
                WHEN :geometry_wkt IS NOT NULL
                    THEN ST_SetSRID(ST_GeomFromText(:geometry_wkt), 4326)
                ELSE NULL
            END,
            :confidence,
            CAST(:metadata AS jsonb)
        )
        ON CONFLICT (task_id, entity_id) DO UPDATE SET
            entity_type = EXCLUDED.entity_type,
            display_name = EXCLUDED.display_name,
            canonical_name = EXCLUDED.canonical_name,
            country_code = EXCLUDED.country_code,
            geometry = EXCLUDED.geometry,
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
        ON CONFLICT (task_id, relation_id) DO UPDATE SET
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
            event_time,
            event_time_end,
            location_name,
            geometry,
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
            CAST(:event_time AS timestamptz),
            CAST(:event_time_end AS timestamptz),
            :location_name,
            CASE
                WHEN :geometry_geojson IS NOT NULL
                    THEN ST_SetSRID(ST_GeomFromGeoJSON(:geometry_geojson), 4326)
                WHEN :geometry_wkt IS NOT NULL
                    THEN ST_SetSRID(ST_GeomFromText(:geometry_wkt), 4326)
                ELSE NULL
            END,
            :risk_level,
            :confidence,
            :evidence_refs,
            CAST(:metadata AS jsonb)
        )
        ON CONFLICT (task_id, timeline_event_id) DO UPDATE SET
            event_title = EXCLUDED.event_title,
            event_type = EXCLUDED.event_type,
            event_time = EXCLUDED.event_time,
            event_time_end = EXCLUDED.event_time_end,
            location_name = EXCLUDED.location_name,
            geometry = EXCLUDED.geometry,
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
        ON CONFLICT (task_id, report_id) DO UPDATE SET
            markdown = EXCLUDED.markdown,
            html = EXCLUDED.html,
            evidence_refs = EXCLUDED.evidence_refs,
            creator_user_id = EXCLUDED.creator_user_id,
            metadata = EXCLUDED.metadata,
            updated_at = now()
    """
    UPSERT_GENERATED_REPORT_SQL = f"""
        INSERT INTO limra_generated_reports (
            report_id,
            task_id,
            report_type,
            markdown,
            html,
            pdf_object_key,
            evidence_refs,
            creator_user_id,
            metadata
        )
        VALUES (
            :report_id,
            :task_id,
            :report_type,
            :markdown,
            :html,
            :pdf_object_key,
            :evidence_refs,
            :creator_user_id,
            CAST(:metadata AS jsonb)
        )
        ON CONFLICT (task_id, report_id) DO UPDATE SET
            report_type = EXCLUDED.report_type,
            markdown = EXCLUDED.markdown,
            html = EXCLUDED.html,
            pdf_object_key = EXCLUDED.pdf_object_key,
            evidence_refs = EXCLUDED.evidence_refs,
            creator_user_id = EXCLUDED.creator_user_id,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        RETURNING {REPORT_COLUMNS}
    """
    SELECT_USER_GENERATED_REPORT_SQL = f"""
        SELECT {REPORT_COLUMNS}
        FROM limra_generated_reports reports
        JOIN limra_research_tasks tasks
          ON tasks.task_id = reports.task_id
        WHERE reports.task_id = :task_id
          AND reports.report_id = :report_id
          AND tasks.owner_user_id = :owner_user_id
    """
    SELECT_TASK_GENERATED_REPORTS_SQL = f"""
        SELECT {REPORT_COLUMNS}
        FROM limra_generated_reports
        WHERE task_id = :task_id
        ORDER BY created_at DESC, report_id ASC
    """
    INSERT_UPLOADED_DOCUMENT_SQL = f"""
        INSERT INTO limra_uploaded_documents (
            document_id,
            task_id,
            owner_user_id,
            original_filename,
            content_type,
            byte_size,
            minio_bucket,
            object_key,
            extracted_text,
            language,
            metadata
        )
        VALUES (
            :document_id,
            :task_id,
            :owner_user_id,
            :original_filename,
            :content_type,
            :byte_size,
            :minio_bucket,
            :object_key,
            :extracted_text,
            :language,
            CAST(:metadata AS jsonb)
        )
        ON CONFLICT (document_id) DO UPDATE SET
            task_id = EXCLUDED.task_id,
            original_filename = EXCLUDED.original_filename,
            content_type = EXCLUDED.content_type,
            byte_size = EXCLUDED.byte_size,
            minio_bucket = EXCLUDED.minio_bucket,
            object_key = EXCLUDED.object_key,
            extracted_text = EXCLUDED.extracted_text,
            language = EXCLUDED.language,
            metadata = EXCLUDED.metadata
        RETURNING {DOCUMENT_COLUMNS}
    """
    SELECT_USER_UPLOADED_DOCUMENT_SQL = f"""
        SELECT {DOCUMENT_COLUMNS}
        FROM limra_uploaded_documents
        WHERE document_id = :document_id
          AND owner_user_id = :owner_user_id
    """
    SELECT_USER_UPLOADED_DOCUMENTS_SQL = f"""
        SELECT {DOCUMENT_COLUMNS}
        FROM limra_uploaded_documents
        WHERE owner_user_id = :owner_user_id
          AND (:task_id IS NULL OR task_id = :task_id)
        ORDER BY created_at DESC, document_id ASC
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

    def _invalidate_archive_metadata(self, task_id: str | None) -> None:
        if not task_id:
            return
        try:
            self.update_task(
                task_id,
                archive_object_key=None,
                archive_zip_sha256=None,
            )
        except KeyError:
            log.warning(
                "Unable to invalidate limra archive metadata for missing task",
                extra={"task_id": task_id},
            )

    def record_artifact(
        self,
        task_id: str,
        artifact_type: str,
        artifact: dict[str, Any],
    ) -> None:
        artifact_id = _artifact_primary_id(artifact_type, artifact)
        bucket = ARTIFACT_BUCKETS[artifact_type]
        event_params = {
            "task_id": task_id,
            "local_artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "bucket": bucket,
            "payload": _json_dumps(artifact),
            "evidence_refs": _list_of_strings(artifact.get("evidence_refs")),
            "confidence": _optional_float(artifact.get("confidence")),
            "notes": _optional_string(artifact.get("notes")),
            "source_event_type": _optional_string(artifact.get("source_event_type")),
        }
        self._execute(self.INSERT_ARTIFACT_EVENT_SQL, event_params)
        self.record_artifact_trace_event(
            task_id,
            _artifact_trace_event_from_artifact(artifact_type, bucket, artifact),
        )
        try:
            self._record_typed_artifact(task_id, artifact_type, artifact, artifact_id)
        except Exception:
            log.exception("Failed to persist typed limra artifact %s", artifact_id)
        self._invalidate_archive_metadata(task_id)

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

    def record_artifact_trace_event(
        self,
        task_id: str,
        event: dict[str, Any],
    ) -> None:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {"value": payload}
        self._execute(
            self.INSERT_ARTIFACT_TRACE_EVENT_SQL,
            {
                "task_id": task_id,
                "event_type": str(event.get("type") or "artifact_event"),
                "artifact_type": _optional_string(event.get("artifact_type")),
                "bucket": _optional_string(event.get("bucket")),
                "local_artifact_id": _optional_string(event.get("local_artifact_id")),
                "payload": _json_dumps(payload),
                "source_event_type": _optional_string(event.get("source_event_type")),
            },
        )
        self._invalidate_archive_metadata(task_id)

    def get_artifact_trace_events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            self.SELECT_ARTIFACT_TRACE_EVENTS_SQL,
            {"task_id": task_id},
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            event: dict[str, Any] = {
                "type": row.get("event_type"),
                "payload": _json_loads(row.get("payload")),
            }
            if row.get("artifact_type"):
                event["artifact_type"] = row["artifact_type"]
            if row.get("bucket"):
                event["bucket"] = row["bucket"]
            if row.get("local_artifact_id"):
                event["local_artifact_id"] = row["local_artifact_id"]
            if row.get("source_event_type"):
                event["source_event_type"] = row["source_event_type"]
            events.append(event)
        return events

    def record_uploaded_document(
        self,
        *,
        document_id: str,
        owner_user_id: str,
        task_id: str | None,
        original_filename: str,
        content_type: str | None,
        byte_size: int,
        minio_bucket: str,
        object_key: str,
        extracted_text: str | None,
        language: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> LimraUploadedDocument:
        row = self._fetch_one(
            self.INSERT_UPLOADED_DOCUMENT_SQL,
            {
                "document_id": document_id,
                "owner_user_id": owner_user_id,
                "task_id": task_id,
                "original_filename": original_filename,
                "content_type": content_type,
                "byte_size": byte_size,
                "minio_bucket": minio_bucket,
                "object_key": object_key,
                "extracted_text": extracted_text,
                "language": language,
                "metadata": _json_dumps(metadata or {}),
            },
        )
        if not row:
            raise RuntimeError("limra_uploaded_document_insert_failed")
        self._invalidate_archive_metadata(task_id)
        return _uploaded_document_from_row(row)

    def get_user_document(
        self,
        document_id: str,
        owner_user_id: str,
    ) -> LimraUploadedDocument | None:
        row = self._fetch_one(
            self.SELECT_USER_UPLOADED_DOCUMENT_SQL,
            {"document_id": document_id, "owner_user_id": owner_user_id},
        )
        return _uploaded_document_from_row(row) if row else None

    def list_user_documents(
        self,
        *,
        owner_user_id: str,
        task_id: str | None = None,
    ) -> list[LimraUploadedDocument]:
        rows = self._fetch_all(
            self.SELECT_USER_UPLOADED_DOCUMENTS_SQL,
            {"owner_user_id": owner_user_id, "task_id": task_id},
        )
        return [_uploaded_document_from_row(row) for row in rows]

    def record_generated_report(
        self,
        *,
        report_id: str,
        task_id: str,
        report_type: str,
        markdown: str,
        html: str | None,
        pdf_object_key: str | None,
        evidence_refs: list[str],
        creator_user_id: str,
        metadata: Mapping[str, Any] | None,
    ) -> LimraGeneratedReport:
        row = self._fetch_one(
            self.UPSERT_GENERATED_REPORT_SQL,
            {
                "report_id": report_id,
                "task_id": task_id,
                "report_type": report_type,
                "markdown": markdown,
                "html": html,
                "pdf_object_key": pdf_object_key,
                "evidence_refs": _list_of_strings(evidence_refs),
                "creator_user_id": creator_user_id,
                "metadata": _json_dumps(metadata or {}),
            },
        )
        if not row:
            raise RuntimeError("limra_generated_report_insert_failed")
        self._invalidate_archive_metadata(task_id)
        return _generated_report_from_row(row)

    def get_user_report(
        self,
        *,
        task_id: str,
        report_id: str,
        owner_user_id: str,
    ) -> LimraGeneratedReport | None:
        row = self._fetch_one(
            self.SELECT_USER_GENERATED_REPORT_SQL,
            {
                "task_id": task_id,
                "report_id": report_id,
                "owner_user_id": owner_user_id,
            },
        )
        return _generated_report_from_row(row) if row else None

    def list_task_reports(
        self,
        *,
        task_id: str,
    ) -> list[LimraGeneratedReport]:
        rows = self._fetch_all(
            self.SELECT_TASK_GENERATED_REPORTS_SQL,
            {"task_id": task_id},
        )
        return [_generated_report_from_row(row) for row in rows]

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
            "published_at": _temporal_value(
                artifact,
                "published_at",
                "published_time",
                "published",
                "published_date",
            ),
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
            **_geometry_params(artifact),
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
            "event_time": _temporal_value(
                artifact,
                "event_time",
                "time",
                "timestamp",
                "date",
            ),
            "event_time_end": _temporal_value(
                artifact,
                "event_time_end",
                "time_end",
                "end_time",
                "end_date",
            ),
            "location_name": artifact.get("location_name")
            or _location_text(artifact.get("location")),
            **_geometry_params(artifact),
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
        database_url = str(
            env.get(LIMRA_DATABASE_URL_ENV) or env.get("DATABASE_URL") or ""
        )
        if not database_url:
            raise RuntimeError("limra_postgres_database_url_missing")
        return PostgresLimraTaskRepository(database_url)

    if backend in {"memory", "in-memory", "in_memory"}:
        allow_memory = str(env.get(LIMRA_ALLOW_IN_MEMORY_REPOSITORY_ENV, ""))
        if allow_memory.strip().lower() not in TRUTHY_ENV_VALUES:
            raise RuntimeError("limra_in_memory_repository_requires_explicit_fallback")
        return InMemoryLimraTaskRepository()

    raise RuntimeError(f"unsupported_limra_repository_backend:{backend}")


def create_limra_runtime_state_from_env(
    *,
    redis_client: Any | None,
    env: Any = os.environ,
) -> LimraRuntimeState:
    backend = str(env.get(LIMRA_RUNTIME_STATE_BACKEND_ENV, "redis")).strip().lower()
    if backend == "redis":
        if redis_client is None:
            raise RuntimeError("limra_redis_runtime_state_missing")
        return RedisLimraRuntimeState(
            redis_client,
            key_prefix=str(
                env.get(LIMRA_RUNTIME_STATE_KEY_PREFIX_ENV) or "limra:runtime"
            ),
            ttl_seconds=_runtime_state_ttl_seconds(
                env.get(LIMRA_RUNTIME_STATE_TTL_SECONDS_ENV)
            ),
        )

    if backend in {"memory", "in-memory", "in_memory"}:
        allow_memory = str(env.get(LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE_ENV, ""))
        if allow_memory.strip().lower() not in TRUTHY_ENV_VALUES:
            raise RuntimeError(
                "limra_in_memory_runtime_state_requires_explicit_fallback"
            )
        return InMemoryLimraRuntimeState()

    raise RuntimeError(f"unsupported_limra_runtime_state_backend:{backend}")


def create_limra_object_storage_from_env(
    env: Any = os.environ,
    *,
    s3_client: Any | None = None,
) -> LimraObjectStorage:
    backend = str(env.get(LIMRA_OBJECT_STORAGE_BACKEND_ENV, "s3")).strip().lower()
    if backend in {"s3", "minio"}:
        bucket = str(
            env.get(LIMRA_OBJECT_BUCKET_ENV)
            or env.get("S3_BUCKET")
            or env.get("MINIO_BUCKET")
            or ""
        ).strip()
        endpoint_url = str(env.get(LIMRA_OBJECT_STORAGE_ENDPOINT_ENV) or "").strip()
        access_key_id = str(env.get(LIMRA_OBJECT_ACCESS_KEY_ENV) or "").strip()
        secret_access_key = str(env.get(LIMRA_OBJECT_SECRET_KEY_ENV) or "").strip()
        region_name = str(env.get(LIMRA_OBJECT_REGION_ENV) or "").strip() or None
        return S3LimraObjectStorage(
            bucket=bucket,
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            region_name=region_name,
            s3_client=s3_client,
        )

    if backend in {"memory", "in-memory", "in_memory"}:
        allow_memory = str(env.get(LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE_ENV, ""))
        if allow_memory.strip().lower() not in TRUTHY_ENV_VALUES:
            raise RuntimeError(
                "limra_in_memory_object_storage_requires_explicit_fallback"
            )
        bucket = str(env.get(LIMRA_OBJECT_BUCKET_ENV) or "limra-memory").strip()
        return InMemoryLimraObjectStorage(bucket=bucket or "limra-memory")

    raise RuntimeError(f"unsupported_limra_object_storage_backend:{backend}")


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


def _normalize_scenario_id(scenario: str | None) -> str | None:
    if scenario is None:
        return None
    normalized = str(scenario).strip()
    return normalized or None


def _runner_query_for_scenario(query: str, scenario: str | None) -> str:
    scenario_id = _normalize_scenario_id(scenario)
    demo_scenario = LIMRA_DEMO_SCENARIOS.get(scenario_id or "")
    if demo_scenario is None:
        return query
    return demo_scenario.runner_query(query)


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


def get_runtime_state(request: Request) -> LimraRuntimeState:
    runtime_state = getattr(request.app.state, "limra_runtime_state", None)
    if runtime_state is None:
        runtime_state = create_limra_runtime_state_from_env(
            redis_client=getattr(request.app.state, "redis", None),
        )
        request.app.state.limra_runtime_state = runtime_state
    return runtime_state


def get_object_storage(request: Request) -> LimraObjectStorage:
    object_storage = getattr(request.app.state, "limra_object_storage", None)
    if object_storage is None:
        object_storage = create_limra_object_storage_from_env()
        request.app.state.limra_object_storage = object_storage
    return object_storage


def get_pdf_exporter(request: Request) -> LimraPdfExporter:
    pdf_exporter = getattr(request.app.state, "limra_pdf_exporter", None)
    if pdf_exporter is None:
        pdf_exporter = PlaywrightLimraPdfExporter()
        request.app.state.limra_pdf_exporter = pdf_exporter
    return pdf_exporter


@router.get("/scenarios")
async def list_demo_scenarios(
    user: LimraUser = Depends(get_current_limra_user),
) -> dict[str, Any]:
    return _assert_browser_safe(
        {
            "scenarios": [
                scenario.public_dict()
                for scenario in LIMRA_DEMO_SCENARIOS.values()
            ],
            "count": len(LIMRA_DEMO_SCENARIOS),
        }
    )


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
    scenario = _normalize_scenario_id(request_data.scenario)
    runner_query = _runner_query_for_scenario(query, scenario)
    task_id = str(uuid.uuid4())
    task = repo.create_task(
        task_id=task_id,
        owner_user_id=user.id,
        query=query,
        scenario=scenario,
        runner_task_id=None,
    )
    try:
        runner_payload = await research_client.create_research_task(
            query=runner_query,
            scenario=scenario,
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
    runtime_state: LimraRuntimeState = Depends(get_runtime_state),
) -> StreamingResponse:
    task = _get_owned_task(repo, task_id, user)
    return StreamingResponse(
        _limra_event_stream(task, user, repo, research_client, runtime_state),
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
    object_storage: LimraObjectStorage = Depends(get_object_storage),
) -> Response:
    task = _get_owned_task(repo, task_id, user)
    return await _download_archive(task, user, repo, object_storage)


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
    object_storage: LimraObjectStorage = Depends(get_object_storage),
) -> Response:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    return await _download_archive(task, user, repo, object_storage)


@router.get("/uploads")
async def list_uploaded_documents(
    task_id: str | None = None,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    if task_id:
        _get_owned_task(repo, task_id, user)
    documents = repo.list_user_documents(owner_user_id=user.id, task_id=task_id)
    return _assert_browser_safe(
        {"documents": [document.public_dict() for document in documents]}
    )


@router.post("/uploads", status_code=201)
async def upload_document(
    request: Request,
    file: UploadFile,
    form_task_id: str | None = Form(default=None, alias="task_id"),
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
    object_storage: LimraObjectStorage = Depends(get_object_storage),
) -> dict[str, Any]:
    await _reject_browser_supplied_object_key_request(request)
    query_task_id = request.query_params.get("task_id")
    if form_task_id and query_task_id and form_task_id != query_task_id:
        raise HTTPException(status_code=400, detail="conflicting_task_id")
    effective_task_id = form_task_id or query_task_id
    if effective_task_id:
        _get_owned_task(repo, effective_task_id, user)

    filename = _safe_original_filename(file.filename)
    content_type = _uploaded_content_type(file.content_type, filename)
    try:
        await file.seek(0)
    except Exception:
        pass
    data = await file.read()
    extracted_text = _extract_uploaded_document_text(
        data,
        filename=filename,
        content_type=content_type,
    )
    document_id = str(uuid.uuid4())
    object_key = build_limra_object_key(
        owner_user_id=user.id,
        category="uploads",
        task_id=effective_task_id,
        filename=filename,
        object_id=document_id,
    )
    stored = await object_storage.put_object(
        object_key=object_key,
        data=data,
        content_type=content_type,
        metadata={
            "document_id": document_id,
            "owner_user_id": user.id,
            "task_id": effective_task_id or "",
            "original_filename": filename,
            "content_type": content_type,
        },
    )
    document = repo.record_uploaded_document(
        document_id=document_id,
        owner_user_id=user.id,
        task_id=effective_task_id,
        original_filename=filename,
        content_type=content_type,
        byte_size=stored.size_bytes,
        minio_bucket=stored.bucket,
        object_key=stored.object_key,
        extracted_text=extracted_text,
        language=None,
        metadata={
            "sha256": stored.sha256,
            "upload_source": "api",
        },
    )
    return _assert_browser_safe(document.public_dict())


@router.get("/uploads/{document_id}")
async def get_uploaded_document(
    document_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    document = _get_owned_document(repo, document_id, user)
    return _assert_browser_safe(document.public_dict())


@router.get("/uploads/{document_id}/download")
async def download_uploaded_document(
    document_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
    object_storage: LimraObjectStorage = Depends(get_object_storage),
) -> Response:
    document = _get_owned_document(repo, document_id, user)
    try:
        data = await object_storage.get_object(object_key=document.object_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document_object_not_found") from exc
    return Response(
        data,
        media_type=document.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition_attachment(
                document.original_filename
            )
        },
    )


@router.post("/tasks/{task_id}/reports/pdf", status_code=201)
async def export_task_pdf(
    task_id: str,
    request: Request,
    form_data: dict[str, Any] | None = None,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
    object_storage: LimraObjectStorage = Depends(get_object_storage),
    pdf_exporter: LimraPdfExporter = Depends(get_pdf_exporter),
) -> dict[str, Any]:
    await _reject_browser_supplied_object_key_request(
        request,
        extra_fields=form_data or {},
    )
    task = _get_owned_task(repo, task_id, user)
    try:
        request_data = ReportPdfRequest.model_validate(form_data or {})
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="invalid_report_payload") from exc
    if (request_data.html or "").strip():
        raise HTTPException(status_code=400, detail="browser_report_html_not_allowed")

    scrubbed_report_id = scrub_limra_secrets(request_data.report_id)
    scrubbed_report_type = _safe_report_type(
        str(scrub_limra_secrets(request_data.report_type))
    )
    scrubbed_markdown = str(scrub_limra_secrets(request_data.markdown))
    scrubbed_evidence_refs = _list_of_strings(
        scrub_limra_secrets(request_data.evidence_refs)
    )

    report_id = _safe_report_id(
        str(scrubbed_report_id) if scrubbed_report_id is not None else None
    )
    report_html = _render_report_html(
        markdown=scrubbed_markdown,
        evidence_refs=scrubbed_evidence_refs,
    )
    try:
        pdf_bytes = await pdf_exporter.render_pdf(report_html)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="pdf_export_failed") from exc

    object_key = build_limra_object_key(
        owner_user_id=user.id,
        category="reports",
        task_id=task.task_id,
        filename=f"{report_id}.pdf",
        object_id=report_id,
    )
    stored = await object_storage.put_object(
        object_key=object_key,
        data=pdf_bytes,
        content_type="application/pdf",
        metadata=scrub_limra_secrets({
            "task_id": task.task_id,
            "report_id": report_id,
            "owner_user_id": user.id,
            "report_type": scrubbed_report_type,
        }),
    )
    report = repo.record_generated_report(
        report_id=report_id,
        task_id=task.task_id,
        report_type=scrubbed_report_type,
        markdown=scrubbed_markdown,
        html=report_html,
        pdf_object_key=stored.object_key,
        evidence_refs=scrubbed_evidence_refs,
        creator_user_id=user.id,
        metadata=scrub_limra_secrets({
            "pdf_bucket": stored.bucket,
            "pdf_sha256": stored.sha256,
            "pdf_size_bytes": stored.size_bytes,
            "exporter": "playwright",
        }),
    )
    return _assert_browser_safe(report.public_dict())


@router.get("/tasks/{task_id}/reports/{report_id}/pdf")
async def download_task_report_pdf(
    task_id: str,
    report_id: str,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
    object_storage: LimraObjectStorage = Depends(get_object_storage),
) -> Response:
    report = _get_owned_report(repo, task_id, report_id, user)
    if not report.pdf_object_key:
        raise HTTPException(status_code=404, detail="report_pdf_not_found")
    try:
        data = await object_storage.get_object(object_key=report.pdf_object_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="report_pdf_not_found") from exc
    return Response(
        data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition_attachment(
                f"{report.report_id}.pdf"
            )
        },
    )


async def _download_archive(
    task: LimraTask,
    user: LimraUser,
    repo: LimraTaskRepository,
    object_storage: LimraObjectStorage,
) -> Response:
    if task.archive_status != "ready":
        raise HTTPException(status_code=409, detail="archive_not_ready")

    archive_bytes = await _load_or_create_persisted_archive(
        task,
        user=user,
        repo=repo,
        object_storage=object_storage,
    )
    validate_archive_zip(archive_bytes)
    archive_bytes = _scrub_archive_zip(archive_bytes)
    validate_archive_zip(archive_bytes)
    return Response(
        archive_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="archive.zip"'},
    )


async def _load_or_create_persisted_archive(
    task: LimraTask,
    *,
    user: LimraUser,
    repo: LimraTaskRepository,
    object_storage: LimraObjectStorage,
) -> bytes:
    if task.archive_object_key:
        try:
            return await object_storage.get_object(object_key=task.archive_object_key)
        except FileNotFoundError:
            log.warning(
                "Persisted limra archive object missing; regenerating",
                extra={"task_id": task.task_id, "user_id": user.id},
            )

    archive_bytes = _build_persisted_archive_zip(task, repo)
    validate_archive_zip(archive_bytes)
    archive_bytes = _scrub_archive_zip(archive_bytes)
    validate_archive_zip(archive_bytes)
    object_key = build_limra_object_key(
        owner_user_id=task.owner_user_id,
        category="archives",
        task_id=task.task_id,
        filename="archive.zip",
        object_id=task.task_id,
    )
    stored = await object_storage.put_object(
        object_key=object_key,
        data=archive_bytes,
        content_type="application/zip",
        metadata=scrub_limra_secrets(
            {
                "task_id": task.task_id,
                "owner_user_id": task.owner_user_id,
                "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
                "generated_by": user.id,
            }
        ),
    )
    repo.update_task(
        task.task_id,
        archive_status="ready",
        archive_object_key=stored.object_key,
        archive_zip_sha256=stored.sha256,
    )
    task.archive_object_key = stored.object_key
    task.archive_zip_sha256 = stored.sha256
    return archive_bytes


def _build_persisted_archive_zip(
    task: LimraTask,
    repo: LimraTaskRepository,
) -> bytes:
    artifacts = repo.get_artifacts(task.task_id)
    artifact_events = repo.get_artifact_trace_events(task.task_id)
    artifact_warnings = [
        event for event in artifact_events if event.get("type") == "artifact_warning"
    ]
    reports = repo.list_task_reports(task_id=task.task_id)
    documents = repo.list_user_documents(
        owner_user_id=task.owner_user_id,
        task_id=task.task_id,
    )
    report = _select_archive_report(reports)
    evidence_refs = _archive_evidence_refs(report, artifacts)
    report_markdown = _archive_report_markdown(task, report, artifacts)
    report_html = _render_report_html(
        markdown=report_markdown,
        evidence_refs=evidence_refs,
    )
    metadata = {
        "task": {
            "task_id": task.task_id,
            "owner_user_id": task.owner_user_id,
            "query": task.query,
            "status": task.status,
            "archive_status": task.archive_status,
            "scenario": task.scenario,
            "error": task.error,
            "model_summary": task.model_summary or {},
        },
        "artifact_counts": {
            bucket: len(items)
            for bucket, items in artifacts.items()
        },
        "artifact_event_count": len(artifact_events),
        "artifact_warning_count": len(artifact_warnings),
        "reports": [report.public_dict() for report in reports],
        "uploaded_documents": [document.public_dict() for document in documents],
    }
    trace = {
        "task": metadata["task"],
        "artifacts": artifacts,
        "artifact_events": artifact_events,
        "artifact_warnings": artifact_warnings,
        "reports": [report.public_dict() for report in reports],
        "uploaded_documents": [document.public_dict() for document in documents],
    }
    members = {
        "metadata.json": _archive_json_text(metadata),
        "report.html": str(scrub_limra_secrets(report_html)),
        "report.md": str(scrub_limra_secrets(report_markdown)),
        "trace.json": _archive_json_text(trace),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member_name in ARCHIVE_MEMBER_ORDER:
            archive.writestr(member_name, members[member_name])
    return output.getvalue()


def _select_archive_report(
    reports: list[LimraGeneratedReport],
) -> LimraGeneratedReport | None:
    for report in reports:
        if report.report_type == "final":
            return report
    return reports[0] if reports else None


def _archive_evidence_refs(
    report: LimraGeneratedReport | None,
    artifacts: dict[str, list[dict[str, Any]]],
) -> list[str]:
    refs: list[str] = []
    refs.extend(report.evidence_refs if report else [])
    for evidence in artifacts.get("evidence", []):
        evidence_id = evidence.get("evidence_id") or evidence.get("id")
        if evidence_id:
            refs.append(str(evidence_id))
    return list(dict.fromkeys(refs))


def _archive_report_markdown(
    task: LimraTask,
    report: LimraGeneratedReport | None,
    artifacts: dict[str, list[dict[str, Any]]],
) -> str:
    if report and report.markdown.strip():
        return report.markdown
    report_sections = artifacts.get("report_sections", [])
    if report_sections:
        sections = []
        for section in report_sections:
            title = str(section.get("title") or section.get("section_id") or "Section")
            body = str(section.get("markdown") or section.get("content") or "")
            sections.append(f"## {title}\n\n{body}".strip())
        return "# limra report\n\n" + "\n\n".join(sections)
    return (
        "# limra report\n\n"
        f"Query: {task.query}\n\n"
        "No generated report content has been recorded yet."
    )


def _archive_json_text(value: Any) -> str:
    scrubbed = scrub_limra_secrets(value)
    scrubbed = json.loads(json.dumps(scrubbed, ensure_ascii=False, default=str))
    return json.dumps(scrubbed, ensure_ascii=False, sort_keys=True, indent=2)


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


def _scrub_archive_zip(archive_bytes: bytes) -> bytes:
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as source:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
                for member_name in ARCHIVE_MEMBER_ORDER:
                    raw_member = source.read(member_name)
                    try:
                        scrubbed = _scrub_archive_member_text(raw_member)
                        target.writestr(member_name, scrubbed.encode("utf-8"))
                    except UnicodeDecodeError:
                        target.writestr(member_name, raw_member)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=502, detail="invalid_archive_zip") from exc
    return output.getvalue()


def _scrub_archive_member_text(raw_member: bytes) -> str:
    text = raw_member.decode("utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _scrub_secret_text(text)
    return json.dumps(scrub_limra_secrets(payload), ensure_ascii=False)


def _get_owned_task(
    repo: LimraTaskRepository,
    task_id: str,
    user: LimraUser,
) -> LimraTask:
    task = repo.get_user_task(task_id, user.id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    return task


def _get_owned_document(
    repo: LimraTaskRepository,
    document_id: str,
    user: LimraUser,
) -> LimraUploadedDocument:
    document = repo.get_user_document(document_id, user.id)
    if not document:
        raise HTTPException(status_code=404, detail="document_not_found")
    return document


def _get_owned_report(
    repo: LimraTaskRepository,
    task_id: str,
    report_id: str,
    user: LimraUser,
) -> LimraGeneratedReport:
    report = repo.get_user_report(
        task_id=task_id,
        report_id=report_id,
        owner_user_id=user.id,
    )
    if not report:
        raise HTTPException(status_code=404, detail="report_not_found")
    return report


async def _limra_event_stream(
    task: LimraTask,
    user: LimraUser,
    repo: LimraTaskRepository,
    research_client: RunnerResearchClientProtocol,
    runtime_state: LimraRuntimeState,
) -> AsyncIterator[bytes]:
    current = repo.get_task(task.task_id) or task
    if current.status in FINAL_TASK_STATUSES:
        terminal_event = _terminal_status_event(current)
        await _record_runtime_event(runtime_state, task.task_id, terminal_event)
        await _mark_terminal_reattach_closed(
            runtime_state,
            task.task_id,
        )
        yield _sse_bytes(_assert_browser_safe(terminal_event))
        return

    stream_id = str(uuid.uuid4())
    opened = await runtime_state.try_open_stream(
        task.task_id,
        owner_user_id=user.id,
        stream_id=stream_id,
        fields={
            "status": "running",
            "archive_status": current.archive_status,
        },
    )
    if not opened:
        runtime_snapshot = await runtime_state.get_task_runtime(task.task_id)
        yield _sse_bytes(
            _assert_browser_safe(
                _active_stream_status_event(current, runtime_snapshot)
            )
        )
        return

    current = repo.update_task(task.task_id, status="running")
    await runtime_state.update_task_runtime(
        task.task_id,
        {
            "owner_user_id": user.id,
            "status": current.status,
            "archive_status": current.archive_status,
            "stream_id": stream_id,
            "stream_state": "open",
        },
    )
    saw_terminal_status = False
    stream_close_reason = "stream_exhausted"

    try:
        async for runner_event in research_client.stream_events(task=task, user=user):
            event = _normalize_runner_event(task, runner_event)
            applied_status = _apply_task_status_from_event(repo, task.task_id, event)
            saw_terminal_status = saw_terminal_status or applied_status in FINAL_TASK_STATUSES
            warning = _record_artifact_from_event(repo, task, event)
            await _record_runtime_event(runtime_state, task.task_id, event)
            if applied_status in FINAL_TASK_STATUSES:
                await runtime_state.update_task_runtime(
                    task.task_id,
                    {"terminal": True},
                )

            yield _sse_bytes(_assert_browser_safe(event))
            if warning:
                await _record_runtime_event(runtime_state, task.task_id, warning)
                yield _sse_bytes(_assert_browser_safe(warning))
            if applied_status in FINAL_TASK_STATUSES:
                stream_close_reason = f"terminal_{applied_status}"

        if not saw_terminal_status:
            status_event = await _authoritative_runner_status_event(
                repo,
                task,
                user,
                research_client,
            )
            if status_event:
                await _record_runtime_event(runtime_state, task.task_id, status_event)
                stream_close_reason = _stream_close_reason_from_event(
                    status_event,
                    stream_close_reason,
                )
                yield _sse_bytes(_assert_browser_safe(status_event))
    except RunnerStreamConflict as exc:
        stream_close_reason = "runner_stream_conflict"
        status_event = await _authoritative_runner_status_event(
            repo,
            task,
            user,
            research_client,
            reason=exc.reason,
        )
        if status_event:
            await _record_runtime_event(runtime_state, task.task_id, status_event)
            stream_close_reason = _stream_close_reason_from_event(
                status_event,
                stream_close_reason,
            )
            yield _sse_bytes(_assert_browser_safe(status_event))
    except asyncio.CancelledError:
        stream_close_reason = "event_stream_cancelled"
        repo.update_task(
            task.task_id,
            status="cancelled",
            archive_status="failed",
            error="event_stream_cancelled",
        )
        await runtime_state.update_task_runtime(
            task.task_id,
            {
                "status": "cancelled",
                "archive_status": "failed",
                "terminal": True,
                "error": "event_stream_cancelled",
            },
        )
        return
    except HTTPException as exc:
        stream_close_reason = "http_exception"
        current = repo.get_task(task.task_id)
        if current and current.status in FINAL_TASK_STATUSES:
            terminal_event = _terminal_status_event(current)
            await _record_runtime_event(runtime_state, task.task_id, terminal_event)
            yield _sse_bytes(_assert_browser_safe(terminal_event))
            return
        repo.update_task(
            task.task_id,
            status="failed",
            archive_status="failed",
            error=str(exc.detail),
        )
        error_event = {
            "task_id": task.task_id,
            "type": "error",
            "payload": {"error": exc.detail},
        }
        await _record_runtime_event(runtime_state, task.task_id, error_event)
        yield _sse_bytes(_assert_browser_safe(error_event))
    except Exception as exc:
        stream_close_reason = "limra_event_proxy_failed"
        current = repo.get_task(task.task_id)
        if current and current.status in FINAL_TASK_STATUSES:
            terminal_event = _terminal_status_event(current)
            await _record_runtime_event(runtime_state, task.task_id, terminal_event)
            yield _sse_bytes(_assert_browser_safe(terminal_event))
            return
        repo.update_task(
            task.task_id,
            status="failed",
            archive_status="failed",
            error=str(exc),
        )
        error_event = {
            "task_id": task.task_id,
            "type": "error",
            "payload": {"error": "limra_event_proxy_failed"},
        }
        await _record_runtime_event(runtime_state, task.task_id, error_event)
        yield _sse_bytes(_assert_browser_safe(error_event))
    finally:
        await _mark_runtime_stream_closed(
            runtime_state,
            task.task_id,
            stream_close_reason,
            stream_id=stream_id,
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


def _active_stream_status_event(
    task: LimraTask,
    runtime_snapshot: dict[str, Any],
) -> dict[str, Any]:
    status = runtime_snapshot.get("status") or task.status
    archive_status = runtime_snapshot.get("archive_status") or task.archive_status
    payload: dict[str, Any] = {
        "status": status,
        "archive_status": archive_status,
        "stream_state": runtime_snapshot.get("stream_state") or "open",
        "status_source": "limra_runtime_state",
        "reason": "stream_already_open",
    }
    if runtime_snapshot.get("terminal") is not None:
        payload["terminal"] = bool(runtime_snapshot["terminal"])
    return {
        "task_id": task.task_id,
        "type": "status",
        "payload": payload,
    }


async def _record_runtime_event(
    runtime_state: LimraRuntimeState,
    task_id: str,
    event: dict[str, Any],
) -> None:
    event_type = str(event.get("type") or "runner_event")
    payload = event.get("payload")
    fields: dict[str, Any] = {
        "last_event_type": event_type,
        "last_event": event,
    }
    if isinstance(payload, dict):
        status = payload.get("status")
        archive_status = payload.get("archive_status")
        data = payload.get("data")
        if not status and isinstance(data, dict):
            status = data.get("status")
        if not archive_status and isinstance(data, dict):
            archive_status = data.get("archive_status")
        if status:
            fields["status"] = str(status)
        if archive_status:
            fields["archive_status"] = str(archive_status)
        if payload.get("terminal") is not None:
            fields["terminal"] = bool(payload.get("terminal"))
        if status in FINAL_TASK_STATUSES:
            fields["terminal"] = True
        if payload.get("warning"):
            fields["last_warning"] = str(payload["warning"])
        if payload.get("error"):
            fields["error"] = str(payload["error"])
    if event_type == "error":
        fields["status"] = "failed"
        fields["archive_status"] = "failed"
        fields["terminal"] = True
        if isinstance(payload, dict) and payload.get("error"):
            fields["error"] = str(payload["error"])
        elif payload:
            fields["error"] = str(payload)
    await runtime_state.update_task_runtime(task_id, fields)


async def _mark_terminal_reattach_closed(
    runtime_state: LimraRuntimeState,
    task_id: str,
) -> None:
    runtime_snapshot = await runtime_state.get_task_runtime(task_id)
    if (
        runtime_snapshot.get("stream_state") == "open"
        and runtime_snapshot.get("stream_id")
    ):
        return
    await _mark_runtime_stream_closed(
        runtime_state,
        task_id,
        "terminal_reattach",
    )


async def _mark_runtime_stream_closed(
    runtime_state: LimraRuntimeState,
    task_id: str,
    reason: str,
    *,
    stream_id: str | None = None,
) -> None:
    fields = {
        "stream_close_reason": reason,
    }
    if stream_id is not None:
        await runtime_state.close_stream(task_id, stream_id=stream_id, fields=fields)
        return
    await runtime_state.update_task_runtime(task_id, {"stream_state": "closed", **fields})


def _stream_close_reason_from_event(event: dict[str, Any], default: str) -> str:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return default
    status = payload.get("status")
    if status in FINAL_TASK_STATUSES:
        return f"terminal_{status}"
    return default


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
    if event.get("type") == "artifact_warning":
        repo.record_artifact_trace_event(
            task.task_id,
            _artifact_trace_event_from_warning(event),
        )
        return None

    artifact_type, artifact_payload, metadata = _artifact_parts_from_event(event)
    if not artifact_type:
        return None

    if artifact_type not in ARTIFACT_BUCKETS:
        return _record_and_return_artifact_warning(
            repo,
            task,
            _artifact_warning(task.task_id, event, "unsupported_artifact_type"),
        )
    if not isinstance(artifact_payload, dict):
        return _record_and_return_artifact_warning(
            repo,
            task,
            _artifact_warning(task.task_id, event, "invalid_artifact_payload"),
        )

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


def _record_and_return_artifact_warning(
    repo: LimraTaskRepository,
    task: LimraTask,
    warning: dict[str, Any],
) -> dict[str, Any]:
    repo.record_artifact_trace_event(
        task.task_id,
        _artifact_trace_event_from_warning(warning),
    )
    return warning


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


def _artifact_trace_event_from_artifact(
    artifact_type: str,
    bucket: str,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    local_artifact_id = _artifact_primary_id(artifact_type, artifact)
    source_event_type = artifact.get("source_event_type")
    return {
        "type": ARTIFACT_TYPE_EVENTS.get(artifact_type, "artifact_recorded"),
        "artifact_type": artifact_type,
        "bucket": bucket,
        "local_artifact_id": local_artifact_id,
        "source_event_type": str(source_event_type) if source_event_type else None,
        "payload": dict(artifact),
    }


def _artifact_trace_event_from_warning(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {"warning": "invalid_artifact_warning", "payload": payload}
    return {
        "type": "artifact_warning",
        "artifact_type": payload.get("artifact_type"),
        "bucket": None,
        "local_artifact_id": None,
        "source_event_type": payload.get("source_event_type"),
        "payload": dict(payload),
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


def _uploaded_document_from_row(row: dict[str, Any]) -> LimraUploadedDocument:
    return LimraUploadedDocument(
        document_id=str(row["document_id"]),
        owner_user_id=str(row["owner_user_id"]),
        task_id=_optional_string(row.get("task_id")),
        original_filename=str(row.get("original_filename") or ""),
        content_type=_optional_string(row.get("content_type")),
        byte_size=int(row.get("byte_size") or 0),
        minio_bucket=str(row.get("minio_bucket") or ""),
        object_key=str(row.get("object_key") or ""),
        extracted_text=_optional_string(row.get("extracted_text")),
        language=_optional_string(row.get("language")),
        metadata=_json_loads(row.get("metadata")) or {},
    )


def _generated_report_from_row(row: dict[str, Any]) -> LimraGeneratedReport:
    return LimraGeneratedReport(
        report_id=str(row["report_id"]),
        task_id=str(row["task_id"]),
        report_type=str(row.get("report_type") or "final"),
        markdown=str(row.get("markdown") or ""),
        html=_optional_string(row.get("html")),
        pdf_object_key=_optional_string(row.get("pdf_object_key")),
        evidence_refs=_list_of_strings(row.get("evidence_refs")),
        creator_user_id=str(row.get("creator_user_id") or "limra"),
        metadata=_json_loads(row.get("metadata")) or {},
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


def _runtime_mapping(fields: dict[str, Any]) -> dict[str, str]:
    return {
        field: json.dumps(value, ensure_ascii=False)
        for field, value in fields.items()
        if value is not None
    }


def _flatten_runtime_mapping(mapping: dict[str, str]) -> list[str]:
    flattened: list[str] = []
    for field, value in mapping.items():
        flattened.extend([field, value])
    return flattened


def _runtime_hash_from_redis(raw_state: Any) -> dict[str, Any]:
    if not raw_state:
        return {}
    items = raw_state.items() if hasattr(raw_state, "items") else []
    return {
        _decode_redis_text(field): _json_loads(_decode_redis_text(value))
        for field, value in items
    }


def _decode_redis_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _runtime_state_ttl_seconds(value: Any) -> int:
    if value in (None, ""):
        return 86_400
    try:
        ttl_seconds = int(value)
    except (TypeError, ValueError):
        raise RuntimeError("limra_runtime_state_ttl_seconds_invalid") from None
    if ttl_seconds <= 0:
        raise RuntimeError("limra_runtime_state_ttl_seconds_invalid")
    return ttl_seconds


def _list_of_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def scrub_limra_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_secret_field_name(key):
                scrubbed[key] = LIMRA_SECRET_REDACTION
            else:
                scrubbed[key] = scrub_limra_secrets(item)
        return scrubbed
    if isinstance(value, list):
        return [scrub_limra_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_limra_secrets(item) for item in value)
    if isinstance(value, set):
        return {scrub_limra_secrets(item) for item in value}
    if isinstance(value, bytes):
        try:
            return _scrub_secret_text(value.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, str):
        return _scrub_secret_text(value)
    return value


def _is_secret_field_name(value: Any) -> bool:
    return bool(SECRET_FIELD_PATTERN.search(str(value)))


def _scrub_secret_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_TEXT_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(
                lambda match: f"{match.group(1)}{LIMRA_SECRET_REDACTION}",
                redacted,
            )
        else:
            redacted = pattern.sub(LIMRA_SECRET_REDACTION, redacted)
    return redacted


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


def _temporal_value(artifact: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = artifact.get(key)
        if value:
            return str(value)
    return None


def _geometry_params(artifact: dict[str, Any]) -> dict[str, str | None]:
    geometry = (
        artifact.get("geometry")
        or artifact.get("geojson")
        or artifact.get("wkt")
        or artifact.get("point")
    )

    if isinstance(geometry, str):
        stripped = geometry.strip()
        if stripped.startswith("{"):
            return {"geometry_geojson": stripped, "geometry_wkt": None}
        if stripped:
            return {"geometry_geojson": None, "geometry_wkt": stripped}

    if isinstance(geometry, dict):
        if geometry.get("type") and geometry.get("coordinates") is not None:
            return {
                "geometry_geojson": _json_dumps(geometry),
                "geometry_wkt": None,
            }
        coordinate_pair = _coordinate_pair(geometry)
        if coordinate_pair:
            return {
                "geometry_geojson": _json_dumps(
                    {"type": "Point", "coordinates": coordinate_pair}
                ),
                "geometry_wkt": None,
            }

    if isinstance(geometry, (list, tuple)):
        coordinate_pair = _coordinate_pair({"coordinates": geometry})
        if coordinate_pair:
            return {
                "geometry_geojson": _json_dumps(
                    {"type": "Point", "coordinates": coordinate_pair}
                ),
                "geometry_wkt": None,
            }

    coordinate_pair = _coordinate_pair(artifact)
    if not coordinate_pair and isinstance(artifact.get("location"), dict):
        coordinate_pair = _coordinate_pair(artifact["location"])
    if coordinate_pair:
        return {
            "geometry_geojson": _json_dumps(
                {"type": "Point", "coordinates": coordinate_pair}
            ),
            "geometry_wkt": None,
        }

    return {"geometry_geojson": None, "geometry_wkt": None}


def _coordinate_pair(value: dict[str, Any]) -> list[float] | None:
    coordinates = value.get("coordinates")
    if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
        lon, lat = coordinates[0], coordinates[1]
    else:
        lon = value.get("lon", value.get("lng", value.get("longitude")))
        lat = value.get("lat", value.get("latitude"))
    try:
        if lon is None or lat is None:
            return None
        return [float(lon), float(lat)]
    except (TypeError, ValueError):
        return None


def _location_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    return str(value)


def _sql_text(sql: str) -> Any:
    try:
        from sqlalchemy import text
    except ImportError:
        return sql

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


def build_limra_object_key(
    *,
    owner_user_id: str,
    category: str,
    task_id: str | None = None,
    filename: str | None = None,
    extension: str | None = None,
    object_id: str | None = None,
    key_prefix: str | None = None,
) -> str:
    normalized_category = category.strip().lower()
    if normalized_category not in OBJECT_KEY_CATEGORIES:
        raise ValueError(f"unsupported_limra_object_category:{category}")
    owner_digest = hashlib.sha256(owner_user_id.encode("utf-8")).hexdigest()[:24]
    prefix = _safe_object_segment(
        key_prefix if key_prefix is not None else os.getenv(LIMRA_OBJECT_KEY_PREFIX_ENV, "limra"),
        fallback="limra",
    )
    object_segment = _safe_object_segment(object_id or uuid.uuid4().hex, fallback=uuid.uuid4().hex)
    suffix = _safe_object_extension(filename=filename, extension=extension)
    parts = [prefix, "users", owner_digest]
    if task_id:
        parts.extend(["tasks", _safe_object_segment(task_id, fallback="task")])
    parts.extend([normalized_category, f"{object_segment}{suffix}"])
    return "/".join(parts)


def _stored_object(
    *,
    object_key: str,
    bucket: str,
    data: bytes,
    content_type: str,
    metadata: Mapping[str, Any] | None,
) -> LimraStoredObject:
    if not object_key or object_key.startswith("/") or "/../" in f"/{object_key}/":
        raise ValueError("invalid_limra_object_key")
    data_bytes = bytes(data)
    return LimraStoredObject(
        object_key=object_key,
        bucket=bucket,
        content_type=content_type or "application/octet-stream",
        size_bytes=len(data_bytes),
        sha256=hashlib.sha256(data_bytes).hexdigest(),
        metadata=_object_metadata(metadata or {}),
    )


def _object_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        clean_key = re.sub(r"[^A-Za-z0-9_.-]", "-", str(key)).strip("-_.")[:64]
        if not clean_key:
            continue
        clean[clean_key] = str(value)[:1024]
    return clean


def _safe_object_segment(value: str, *, fallback: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", str(value)).strip(".-/")
    return safe[:120] or fallback


def _safe_object_extension(
    *,
    filename: str | None = None,
    extension: str | None = None,
) -> str:
    candidate = extension or ""
    if not candidate and filename:
        basename = str(filename).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        _, candidate = os.path.splitext(basename)
    if candidate and not candidate.startswith("."):
        candidate = f".{candidate}"
    candidate = candidate.lower()
    if candidate in OBJECT_KEY_ALLOWED_EXTENSIONS:
        return candidate
    return ".bin"


def _safe_original_filename(filename: str | None) -> str:
    basename = (filename or "upload.bin").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    basename = basename.strip()[:255]
    return basename or "upload.bin"


def _content_disposition_attachment(filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", _safe_original_filename(filename))[:120]
    return f'attachment; filename="{safe or "upload.bin"}"'


def _safe_report_id(report_id: str | None) -> str:
    return _safe_object_segment(report_id or f"report-{uuid.uuid4().hex[:12]}", fallback="report")


def _safe_report_type(report_type: str | None) -> str:
    return _safe_object_segment(report_type or "final", fallback="final")


def _render_report_html(
    *,
    markdown: str,
    evidence_refs: list[str],
) -> str:
    body = _link_evidence_refs(_markdown_to_html(markdown), evidence_refs)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{REPORT_CSP}\">"
        "<title>limra report</title></head><body>"
        f"{body}</body></html>"
    )


def _markdown_to_html(markdown: str) -> str:
    blocks = []
    safe_markdown = _strip_active_report_markup(markdown)
    for paragraph in re.split(r"\n{2,}", safe_markdown.strip()):
        if not paragraph:
            continue
        escaped = html.escape(paragraph).replace("\n", "<br>")
        blocks.append(f"<p>{escaped}</p>")
    return "\n".join(blocks) or "<p></p>"


def _sanitize_report_html(value: str) -> str:
    return _markdown_to_html(_strip_active_report_markup(value))


def _strip_active_report_markup(value: str) -> str:
    clean = value or ""
    blocked = "|".join(re.escape(tag) for tag in REPORT_BLOCKED_HTML_TAGS)
    clean = re.sub(
        rf"(?is)<\s*({blocked})\b.*?<\s*/\s*\1\s*>",
        "",
        clean,
    )
    clean = re.sub(rf"(?is)<\s*(?:{blocked})\b[^>]*(?:/?>)", "", clean)
    clean = re.sub(r"(?is)<[^>]+>", "", clean)
    clean = re.sub(
        r"(?is)\b(?:srcdoc|src|href|xlink:href)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
        "",
        clean,
    )
    clean = re.sub(
        r"(?is)\bon[a-z0-9_-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
        "",
        clean,
    )
    clean = re.sub(r"(?i)\b(?:javascript|data|blob|file)\s*:", "", clean)
    return clean


def _link_evidence_refs(body: str, evidence_refs: list[str]) -> str:
    linked = body
    for ref in _list_of_strings(evidence_refs):
        safe_ref = html.escape(ref, quote=True)
        label = f"[{safe_ref}]"
        target = f"evidence-{safe_ref}"
        anchor = (
            f'<a href="#{target}" data-evidence-ref="{safe_ref}">'
            f"{label}</a>"
        )
        linked = linked.replace(label, anchor)
    return linked


def _uploaded_content_type(content_type: str | None, filename: str) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized:
        return normalized
    lower_name = filename.lower()
    if lower_name.endswith(".pdf"):
        return "application/pdf"
    if lower_name.endswith(".md"):
        return "text/markdown"
    if lower_name.endswith(".csv"):
        return "text/csv"
    if lower_name.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def _extract_uploaded_document_text(
    data: bytes,
    *,
    filename: str,
    content_type: str,
) -> str:
    lower_name = filename.lower()
    if content_type == "application/pdf" or lower_name.endswith(".pdf"):
        return _extract_pdf_text(data)
    if content_type.startswith("text/") or lower_name.endswith((".txt", ".md", ".csv")):
        return _decode_text_upload(data)
    raise HTTPException(status_code=415, detail="unsupported_upload_type")


def _decode_text_upload(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid_pdf_upload") from exc

    pages: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text.strip():
            pages.append(page_text.strip())
    return "\n\n".join(pages)


def _reject_browser_supplied_object_key_fields(fields: Mapping[str, Any]) -> None:
    forbidden = sorted(field for field in OBJECT_KEY_FORBIDDEN_FIELDS if field in fields)
    if forbidden:
        raise HTTPException(status_code=400, detail="object_key_server_generated")


async def _reject_browser_supplied_object_key_request(
    request: Request,
    *,
    extra_fields: Mapping[str, Any] | None = None,
) -> None:
    _reject_browser_supplied_object_key_fields(request.query_params)
    if extra_fields:
        _reject_browser_supplied_object_key_fields(extra_fields)

    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type in {"multipart/form-data", "application/x-www-form-urlencoded"}:
        try:
            form_data = await request.form()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid_form_payload") from exc
        _reject_browser_supplied_object_key_fields(form_data)
        return

    if content_type == "application/json":
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if isinstance(payload, Mapping):
            _reject_browser_supplied_object_key_fields(payload)


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
