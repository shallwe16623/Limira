from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid
import zipfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

try:
    import bcrypt as _bcrypt
except Exception:  # pragma: no cover - depends on optional runtime package
    _bcrypt = None


ARCHIVE_MEMBER_ORDER = ("metadata.json", "report.html", "report.md", "trace.json")
ARCHIVE_JSON_MEMBERS = {"metadata.json", "trace.json"}
ARCHIVE_MEMBER_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
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
LIMRA_SQLITE_DATABASE_PATH_ENV = "LIMRA_SQLITE_DATABASE_PATH"
LIMRA_ALLOW_IN_MEMORY_REPOSITORY_ENV = "LIMRA_ALLOW_IN_MEMORY_REPOSITORY"
LIMRA_RUNTIME_STATE_BACKEND_ENV = "LIMRA_RUNTIME_STATE_BACKEND"
LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE_ENV = "LIMRA_ALLOW_IN_MEMORY_RUNTIME_STATE"
LIMRA_RUNTIME_STATE_KEY_PREFIX_ENV = "LIMRA_RUNTIME_STATE_KEY_PREFIX"
LIMRA_RUNTIME_STATE_TTL_SECONDS_ENV = "LIMRA_RUNTIME_STATE_TTL_SECONDS"
LIMRA_OBJECT_STORAGE_BACKEND_ENV = "LIMRA_OBJECT_STORAGE_BACKEND"
LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE_ENV = "LIMRA_ALLOW_IN_MEMORY_OBJECT_STORAGE"
LIMRA_OBJECT_STORAGE_PATH_ENV = "LIMRA_OBJECT_STORAGE_PATH"
LIMRA_OBJECT_BUCKET_ENV = "LIMRA_OBJECT_BUCKET"
LIMRA_OBJECT_KEY_PREFIX_ENV = "LIMRA_OBJECT_KEY_PREFIX"
LIMRA_OBJECT_STORAGE_ENDPOINT_ENV = "S3_ENDPOINT_URL"
LIMRA_OBJECT_ACCESS_KEY_ENV = "AWS_ACCESS_KEY_ID"
LIMRA_OBJECT_SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"
LIMRA_OBJECT_REGION_ENV = "AWS_REGION"
LIMRA_UPLOAD_EMBEDDINGS_ENABLED_ENV = "LIMRA_UPLOAD_EMBEDDINGS_ENABLED"
LIMRA_EMBEDDING_PROVIDER_ENV = "LIMRA_EMBEDDING_PROVIDER"
LIMRA_EMBEDDING_MODEL_ENV = "LIMRA_EMBEDDING_MODEL"
LIMRA_EMBEDDING_DIMENSIONS_ENV = "LIMRA_EMBEDDING_DIMENSIONS"
LIMRA_PDF_DEBUG_DIR_ENV = "LIMRA_PDF_DEBUG_DIR"
LIMRA_PLAYWRIGHT_RUNTIME_PATH_ENV = "LIMRA_PLAYWRIGHT_RUNTIME_PATH"
LIMRA_AUTH_SQLITE_PATH_ENV = "LIMRA_AUTH_SQLITE_PATH"
LIMRA_LEGACY_AUTH_SQLITE_PATH_ENV = "LIMRA_LEGACY_AUTH_SQLITE_PATH"
LIMRA_AUTH_SECRET_ENV = "LIMRA_AUTH_SECRET"
LIMRA_AUTH_TOKEN_TTL_SECONDS_ENV = "LIMRA_AUTH_TOKEN_TTL_SECONDS"
LIMRA_AUTH_COOKIE_SECURE_ENV = "LIMRA_AUTH_COOKIE_SECURE"
LIMRA_AUTH_COOKIE_NAME = "limra_session"
LIMRA_AUTH_DEFAULT_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30
LIMRA_DEFAULT_EMBEDDING_DIMENSIONS = 1536
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
OBJECT_METADATA_SIDECAR_SUFFIX = ".metadata.json"
UPLOAD_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
}
UPLOAD_GENERIC_CONTENT_TYPES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
}
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
    "style-src 'unsafe-inline'; "
    "connect-src 'none'; "
    "img-src 'none'; "
    "font-src 'none'; "
    "media-src 'none'; "
    "object-src 'none'; "
    "frame-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)
SEARCH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
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
INTERNAL_ERROR_TEXT_PATTERNS = (
    re.compile(r"(?i)\bhttps?://"),
    re.compile(r"/mirothinker/"),
    re.compile(r"\blimra/users/"),
    re.compile(
        r"(?i)\b(?:object_key|minio_object_key|pdf_object_key|archive_object_key)\b"
    ),
    re.compile(r"(?i)\btraceback\b|File \"[^\"]+\", line \d+"),
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


class LimraAuthSigninRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=4096)


class LimraAuthSignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, max_length=320)


@dataclass(frozen=True)
class LimraUser:
    id: str
    role: str = "user"
    email: str | None = None
    name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True)
class LimraAuthRecord:
    id: str
    email: str
    name: str
    role: str
    password_hash: str
    active: bool = True


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
            "error": _public_error_text(self.error, fallback="limra_task_failed"),
            "model_summary": _public_model_summary(self.model_summary or {}),
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
    embedding: list[float] | None = None

    def public_dict(self) -> dict[str, Any]:
        download_available = _is_valid_uploaded_document_download_metadata(
            self.object_key,
            (self.metadata or {}).get("sha256"),
            (self.metadata or {}).get("download_unavailable"),
        )
        return {
            "document_id": self.document_id,
            "task_id": self.task_id,
            "filename": self.original_filename,
            "content_type": self.content_type,
            "byte_size": self.byte_size,
            "language": self.language,
            "extracted_text_chars": len(self.extracted_text or ""),
            "download_url": f"/api/limra/uploads/{self.document_id}/download"
            if download_available
            else None,
        }


@dataclass(frozen=True)
class LimraUploadedDocumentSearchResult:
    document: LimraUploadedDocument
    score: float
    snippet: str
    matched_terms: list[str]

    def public_dict(self) -> dict[str, Any]:
        payload = self.document.public_dict()
        payload.update(
            {
                "score": round(self.score, 3),
                "snippet": self.snippet,
                "matched_terms": list(self.matched_terms),
            }
        )
        return payload


@dataclass(frozen=True)
class LimraUploadEmbeddingConfig:
    enabled: bool
    provider: str
    model: str
    dimensions: int


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
        has_valid_pdf_object = _is_valid_report_pdf_metadata(
            self.pdf_object_key,
            metadata.get("pdf_sha256"),
        )
        return {
            "report_id": self.report_id,
            "task_id": self.task_id,
            "report_type": self.report_type,
            "evidence_refs": list(self.evidence_refs),
            "markdown_chars": len(self.markdown or ""),
            "html_chars": len(self.html or ""),
            "pdf_size_bytes": metadata.get("pdf_size_bytes")
            if has_valid_pdf_object
            else None,
            "pdf_sha256": metadata.get("pdf_sha256") if has_valid_pdf_object else None,
            "pdf_url": f"/api/limra/tasks/{self.task_id}/reports/{self.report_id}/pdf"
            if has_valid_pdf_object
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

    def list_user_tasks(self, *, owner_user_id: str, limit: int) -> list[LimraTask]: ...

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

    def record_task_event_log(
        self,
        task_id: str,
        event: dict[str, Any],
        *,
        source: str = "runner_stream",
    ) -> None: ...

    def list_task_event_logs(
        self,
        task_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]: ...

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
        embedding: list[float] | None = None,
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

    def search_user_documents(
        self,
        *,
        owner_user_id: str,
        query: str,
        limit: int,
        task_id: str | None = None,
        query_embedding: list[float] | None = None,
    ) -> list[LimraUploadedDocumentSearchResult]: ...

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


class LimraUploadEmbeddingProvider(Protocol):
    async def embed_upload_text(
        self,
        text: str,
        *,
        config: LimraUploadEmbeddingConfig,
    ) -> list[float]: ...


class LimraPdfExporter(Protocol):
    async def render_pdf(self, html_content: str) -> bytes: ...


class InMemoryLimraTaskRepository:
    def __init__(self) -> None:
        self.tasks: dict[str, LimraTask] = {}
        self.artifacts: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.artifact_trace_events: dict[str, list[dict[str, Any]]] = {}
        self.task_event_logs: dict[str, list[dict[str, Any]]] = {}
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
        self.task_event_logs[task_id] = []
        return task

    def get_task(self, task_id: str) -> LimraTask | None:
        return self.tasks.get(task_id)

    def get_user_task(self, task_id: str, owner_user_id: str) -> LimraTask | None:
        task = self.get_task(task_id)
        if not task or task.owner_user_id != owner_user_id:
            return None
        return task

    def list_user_tasks(self, *, owner_user_id: str, limit: int) -> list[LimraTask]:
        tasks = [
            task
            for task in reversed(list(self.tasks.values()))
            if task.owner_user_id == owner_user_id
        ]
        return tasks[:limit]

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

    def record_task_event_log(
        self,
        task_id: str,
        event: dict[str, Any],
        *,
        source: str = "runner_stream",
    ) -> None:
        event_type = str(event.get("type") or "runner_event")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {"value": payload}
        self.task_event_logs.setdefault(task_id, []).append(
            {
                "event_log_id": str(uuid.uuid4()),
                "task_id": task_id,
                "event_type": event_type,
                "source": source,
                "payload": scrub_limra_secrets(payload),
                "created_at": time.time(),
            }
        )

    def list_task_event_logs(
        self,
        task_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        return [
            dict(event)
            for event in self.task_event_logs.get(task_id, [])[-max(1, limit):]
        ]

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
        embedding: list[float] | None = None,
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
            embedding=list(embedding) if embedding is not None else None,
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

    def search_user_documents(
        self,
        *,
        owner_user_id: str,
        query: str,
        limit: int,
        task_id: str | None = None,
        query_embedding: list[float] | None = None,
    ) -> list[LimraUploadedDocumentSearchResult]:
        documents = self.list_user_documents(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
        if query_embedding is not None:
            vector_results = _rank_uploaded_documents_by_embedding(
                documents,
                query,
                query_embedding,
                limit,
            )
            if vector_results:
                return vector_results
        return _rank_uploaded_documents(documents, query, limit)

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


class SQLiteLimraTaskRepository(InMemoryLimraTaskRepository):
    def __init__(self, database_path: str) -> None:
        super().__init__()
        self.database_path = os.path.abspath(os.path.expanduser(database_path))
        os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        self._init_db()
        self._load_state()

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS limra_repository_state (
                    state_key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=30)

    def _load_state(self) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM limra_repository_state WHERE state_key = ?",
                ("state",),
            ).fetchone()
        if not row:
            return
        try:
            state = json.loads(row[0])
        except json.JSONDecodeError:
            log.warning("Ignoring invalid limra SQLite repository state")
            return

        self.tasks = {
            str(task_id): LimraTask(**payload)
            for task_id, payload in (state.get("tasks") or {}).items()
            if isinstance(payload, dict)
        }
        self.artifacts = {
            str(task_id): _normalize_artifact_buckets(payload)
            for task_id, payload in (state.get("artifacts") or {}).items()
            if isinstance(payload, dict)
        }
        self.artifact_trace_events = {
            str(task_id): [dict(event) for event in events if isinstance(event, dict)]
            for task_id, events in (state.get("artifact_trace_events") or {}).items()
            if isinstance(events, list)
        }
        self.task_event_logs = {
            str(task_id): [dict(event) for event in events if isinstance(event, dict)]
            for task_id, events in (state.get("task_event_logs") or {}).items()
            if isinstance(events, list)
        }
        self.uploaded_documents = {
            str(document_id): LimraUploadedDocument(**payload)
            for document_id, payload in (state.get("uploaded_documents") or {}).items()
            if isinstance(payload, dict)
        }
        self.generated_reports = {}
        for payload in state.get("generated_reports") or []:
            if not isinstance(payload, dict):
                continue
            report = LimraGeneratedReport(**payload)
            self.generated_reports[(report.task_id, report.report_id)] = report

    def _persist_state(self) -> None:
        state = {
            "tasks": {
                task_id: asdict(task)
                for task_id, task in self.tasks.items()
            },
            "artifacts": self.artifacts,
            "artifact_trace_events": self.artifact_trace_events,
            "task_event_logs": self.task_event_logs,
            "uploaded_documents": {
                document_id: asdict(document)
                for document_id, document in self.uploaded_documents.items()
            },
            "generated_reports": [
                asdict(report)
                for report in self.generated_reports.values()
            ],
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO limra_repository_state (state_key, state_json, updated_at)
                VALUES (?, ?, CAST(strftime('%s', 'now') AS REAL))
                ON CONFLICT(state_key) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                ("state", json.dumps(state, ensure_ascii=False)),
            )

    def create_task(
        self,
        *,
        task_id: str,
        owner_user_id: str,
        query: str,
        scenario: str | None,
        runner_task_id: str | None,
    ) -> LimraTask:
        task = super().create_task(
            task_id=task_id,
            owner_user_id=owner_user_id,
            query=query,
            scenario=scenario,
            runner_task_id=runner_task_id,
        )
        self._persist_state()
        return task

    def update_task(self, task_id: str, **updates: Any) -> LimraTask:
        task = super().update_task(task_id, **updates)
        self._persist_state()
        return task

    def record_artifact(
        self,
        task_id: str,
        artifact_type: str,
        artifact: dict[str, Any],
    ) -> None:
        super().record_artifact(task_id, artifact_type, artifact)
        self._persist_state()

    def record_artifact_trace_event(
        self,
        task_id: str,
        event: dict[str, Any],
    ) -> None:
        super().record_artifact_trace_event(task_id, event)
        self._persist_state()

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
        embedding: list[float] | None = None,
    ) -> LimraUploadedDocument:
        document = super().record_uploaded_document(
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
            metadata=metadata,
            embedding=embedding,
        )
        self._persist_state()
        return document

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
        report = super().record_generated_report(
            report_id=report_id,
            task_id=task_id,
            report_type=report_type,
            markdown=markdown,
            html=html,
            pdf_object_key=pdf_object_key,
            evidence_refs=evidence_refs,
            creator_user_id=creator_user_id,
            metadata=metadata,
        )
        self._persist_state()
        return report


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
        object_key = validate_limra_object_key(object_key)
        stored = _stored_object(
            object_key=object_key,
            bucket=self.bucket,
            data=data,
            content_type=content_type,
            metadata=metadata,
        )
        self.objects[stored.object_key] = {
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
        object_key = validate_limra_object_key(object_key)
        try:
            return bytes(self.objects[object_key]["data"])
        except KeyError as exc:
            raise FileNotFoundError(object_key) from exc


class FileSystemLimraObjectStorage:
    def __init__(self, *, root_path: str, bucket: str = "limra-local") -> None:
        if not root_path:
            raise RuntimeError("limra_filesystem_object_storage_path_missing")
        self.root_path = os.path.abspath(os.path.expanduser(root_path))
        self.bucket = bucket or "limra-local"
        os.makedirs(self.root_path, exist_ok=True)

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
        object_path = self._object_path(stored.object_key)
        os.makedirs(os.path.dirname(object_path), exist_ok=True)
        with open(object_path, "wb") as file:
            file.write(bytes(data))
        with open(f"{object_path}.metadata.json", "w", encoding="utf-8") as file:
            json.dump(
                {
                    "object_key": stored.object_key,
                    "bucket": stored.bucket,
                    "content_type": stored.content_type,
                    "size_bytes": stored.size_bytes,
                    "sha256": stored.sha256,
                    "metadata": stored.metadata,
                },
                file,
                ensure_ascii=False,
            )
        return stored

    async def get_object(
        self,
        *,
        object_key: str,
    ) -> bytes:
        object_path = self._object_path(object_key)
        try:
            with open(object_path, "rb") as file:
                return file.read()
        except FileNotFoundError as exc:
            raise FileNotFoundError(object_key) from exc

    def _object_path(self, object_key: str) -> str:
        safe_object_key = validate_limra_object_key(object_key)
        object_path = os.path.abspath(os.path.join(self.root_path, *safe_object_key.split("/")))
        if object_path != self.root_path and not object_path.startswith(f"{self.root_path}{os.sep}"):
            raise ValueError("invalid_limra_object_key")
        return object_path


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
                Key=stored.object_key,
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
        object_key = validate_limra_object_key(object_key)
        try:
            response = await _maybe_await(
                self.s3_client.get_object(Bucket=self.bucket, Key=object_key)
            )
            body = response["Body"]
            data = await _maybe_await(body.read())
            return bytes(data)
        except Exception as exc:
            raise FileNotFoundError(object_key) from exc


class DisabledLimraUploadEmbeddingProvider:
    async def embed_upload_text(
        self,
        text: str,
        *,
        config: LimraUploadEmbeddingConfig,
    ) -> list[float]:
        raise RuntimeError("limra_upload_embedding_provider_unconfigured")


async def _abort_playwright_route(route: Any) -> None:
    await route.abort()


def _playwright_chromium_launch_env() -> dict[str, str] | None:
    configured_path = str(os.getenv(LIMRA_PLAYWRIGHT_RUNTIME_PATH_ENV) or "").strip()
    runtime_path = configured_path or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", ".playwright-local-libs")
    )
    if not os.path.isdir(runtime_path):
        return None

    env = dict(os.environ)
    library_dirs = [
        os.path.join(runtime_path, "usr", "lib", "x86_64-linux-gnu"),
        os.path.join(runtime_path, "lib", "x86_64-linux-gnu"),
    ]
    existing_library_path = str(env.get("LD_LIBRARY_PATH") or "").strip()
    library_path_parts = [
        library_dir for library_dir in library_dirs if os.path.isdir(library_dir)
    ]
    if existing_library_path:
        library_path_parts.append(existing_library_path)
    if library_path_parts:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(library_path_parts)

    fontconfig_file = os.path.join(runtime_path, "fonts.conf")
    if os.path.isfile(fontconfig_file):
        env["FONTCONFIG_FILE"] = fontconfig_file
    env["FONTCONFIG_PATH"] = runtime_path
    return env


class PlaywrightLimraPdfExporter:
    async def render_pdf(self, html_content: str) -> bytes:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - depends on runtime image
            log.warning("Playwright PDF dependency unavailable; using fallback exporter")
            return _render_report_pdf_fallback(html_content)

        try:
            async with async_playwright() as playwright:
                launch_kwargs: dict[str, Any] = {"args": ["--no-sandbox"]}
                launch_env = _playwright_chromium_launch_env()
                if launch_env is not None:
                    launch_kwargs["env"] = launch_env
                browser = await playwright.chromium.launch(**launch_kwargs)
                try:
                    page = await browser.new_page()
                    await page.route("**/*", _abort_playwright_route)
                    await page.set_content(html_content, wait_until="load")
                    visible_text = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
                    if not str(visible_text or "").strip():
                        raise RuntimeError("limra_pdf_blank_rendered_body")
                    await page.emulate_media(media="print")
                    pdf_bytes = await page.pdf(format="A4", print_background=True)
                    if _persisted_report_pdf_appears_blank(pdf_bytes):
                        raise RuntimeError("limra_pdf_blank_rendered_pdf")
                finally:
                    await browser.close()
            return bytes(pdf_bytes)
        except Exception as exc:
            log.warning("Playwright PDF export failed; using fallback exporter: %s", exc)
            return _render_report_pdf_fallback(html_content)


def _render_report_pdf_fallback(html_content: str) -> bytes:
    text = _plain_text_from_report_html(html_content)
    if not text.strip():
        raise RuntimeError("limra_pdf_blank_rendered_body")
    try:
        from fpdf import FPDF
    except Exception as exc:  # pragma: no cover - depends on runtime image
        raise RuntimeError("limra_pdf_fallback_dependency_missing") from exc

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    font_name = "Helvetica"
    font_path = _report_pdf_font_path()
    if font_path:
        pdf.add_font("NotoSansSC", "", font_path)
        font_name = "NotoSansSC"
    pdf.set_font(font_name, size=11)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            pdf.ln(4)
            continue
        try:
            pdf.multi_cell(pdf.epw, 6, line)
        except Exception:
            for chunk in _chunk_report_pdf_text_line(line):
                pdf.multi_cell(pdf.epw, 6, chunk)
    rendered = pdf.output()
    if isinstance(rendered, (bytes, bytearray)):
        return bytes(rendered)
    return str(rendered).encode("latin-1")


def _plain_text_from_report_html(html_content: str) -> str:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        raw_text = soup.get_text("\n")
    except Exception:
        raw_text = re.sub(r"(?i)<br\s*/?>", "\n", html_content)
        raw_text = re.sub(r"(?i)</(?:p|div|h[1-6]|li|tr)>", "\n", raw_text)
        raw_text = re.sub(r"<[^>]+>", " ", raw_text)
    lines = [html.unescape(line).strip() for line in raw_text.splitlines()]
    return "\n".join(line for line in lines if line)


def _chunk_report_pdf_text_line(line: str, chunk_size: int = 80) -> list[str]:
    return [line[index : index + chunk_size] for index in range(0, len(line), chunk_size)]


def _report_pdf_font_path() -> str | None:
    font_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "fonts",
        "NotoSansSC-Regular.ttf",
    )
    return font_path if os.path.exists(font_path) else None


class PostgresLimraTaskRepository:
    POSTGRES_ARTIFACT_TABLES = {
        "limra_research_tasks",
        "limra_artifact_events",
        "limra_artifact_trace_events",
        "limra_task_event_logs",
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
        embedding,
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
    SELECT_USER_TASKS_SQL = f"""
        SELECT {TASK_COLUMNS}
        FROM limra_research_tasks
        WHERE owner_user_id = :owner_user_id
        ORDER BY created_at DESC, task_id DESC
        LIMIT :limit
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
    INSERT_TASK_EVENT_LOG_SQL = """
        INSERT INTO limra_task_event_logs (
            task_id,
            event_type,
            source,
            payload
        )
        VALUES (
            :task_id,
            :event_type,
            :source,
            CAST(:payload AS jsonb)
        )
    """
    SELECT_TASK_EVENT_LOGS_SQL = """
        SELECT event_log_id, task_id, event_type, source, payload, created_at
        FROM limra_task_event_logs
        WHERE task_id = :task_id
        ORDER BY created_at DESC, event_log_id DESC
        LIMIT :limit
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
            embedding,
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
            CAST(:embedding AS vector),
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
            embedding = EXCLUDED.embedding,
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
    SEARCH_USER_UPLOADED_DOCUMENTS_BY_VECTOR_SQL = f"""
        SELECT
            {DOCUMENT_COLUMNS},
            1 - (embedding <=> CAST(:query_embedding AS vector)) AS limra_search_score
        FROM limra_uploaded_documents
        WHERE owner_user_id = :owner_user_id
          AND (:task_id IS NULL OR task_id = :task_id)
          AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:query_embedding AS vector) ASC,
                 created_at DESC,
                 document_id ASC
        LIMIT :limit
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

    def list_user_tasks(self, *, owner_user_id: str, limit: int) -> list[LimraTask]:
        rows = self._fetch_all(
            self.SELECT_USER_TASKS_SQL,
            {"owner_user_id": owner_user_id, "limit": limit},
        )
        return [_task_from_row(row) for row in rows]

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

    def record_task_event_log(
        self,
        task_id: str,
        event: dict[str, Any],
        *,
        source: str = "runner_stream",
    ) -> None:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {"value": payload}
        self._execute(
            self.INSERT_TASK_EVENT_LOG_SQL,
            {
                "task_id": task_id,
                "event_type": str(event.get("type") or "runner_event"),
                "source": source,
                "payload": _json_dumps(scrub_limra_secrets(payload)),
            },
        )

    def list_task_event_logs(
        self,
        task_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            self.SELECT_TASK_EVENT_LOGS_SQL,
            {"task_id": task_id, "limit": max(1, limit)},
        )
        events: list[dict[str, Any]] = []
        for row in reversed(rows):
            events.append(
                {
                    "event_log_id": row.get("event_log_id"),
                    "task_id": row.get("task_id"),
                    "event_type": row.get("event_type"),
                    "source": row.get("source"),
                    "payload": _json_loads(row.get("payload")),
                    "created_at": str(row.get("created_at") or ""),
                }
            )
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
        embedding: list[float] | None = None,
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
                "embedding": _vector_param(embedding),
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

    def search_user_documents(
        self,
        *,
        owner_user_id: str,
        query: str,
        limit: int,
        task_id: str | None = None,
        query_embedding: list[float] | None = None,
    ) -> list[LimraUploadedDocumentSearchResult]:
        if query_embedding is not None:
            rows = self._fetch_all(
                self.SEARCH_USER_UPLOADED_DOCUMENTS_BY_VECTOR_SQL,
                {
                    "owner_user_id": owner_user_id,
                    "task_id": task_id,
                    "query_embedding": _vector_param(query_embedding),
                    "limit": limit,
                },
            )
            results = [
                _uploaded_document_vector_search_result(
                    _uploaded_document_from_row(row),
                    query,
                    float(row.get("limra_search_score") or 0.0),
                )
                for row in rows
            ]
            if results:
                return results
        documents = self.list_user_documents(
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
        return _rank_uploaded_documents(documents, query, limit)

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


def _limra_data_dir(env: Any = os.environ) -> str:
    return os.path.abspath(
        os.path.expanduser(
            str(
                env.get("DATA_DIR")
                or os.path.join(os.path.dirname(__file__), "..", "..", "data")
            )
        )
    )


def _limra_sqlite_database_path(env: Any = os.environ) -> str:
    raw_path = str(
        env.get(LIMRA_SQLITE_DATABASE_PATH_ENV)
        or env.get(LIMRA_DATABASE_URL_ENV)
        or ""
    ).strip()
    if raw_path.startswith("sqlite:///"):
        raw_path = raw_path.removeprefix("sqlite:///")
    elif raw_path.startswith("sqlite://"):
        raw_path = raw_path.removeprefix("sqlite://")
    if raw_path:
        return os.path.abspath(os.path.expanduser(raw_path))
    return os.path.join(_limra_data_dir(env), "limra_repository.sqlite3")


def _limra_auth_sqlite_path(env: Any = os.environ) -> str:
    raw_path = str(env.get(LIMRA_AUTH_SQLITE_PATH_ENV) or "").strip()
    if raw_path.startswith("sqlite:///"):
        raw_path = raw_path.removeprefix("sqlite:///")
    elif raw_path.startswith("sqlite://"):
        raw_path = raw_path.removeprefix("sqlite://")
    if raw_path:
        return os.path.abspath(os.path.expanduser(raw_path))
    return os.path.join(_limra_data_dir(env), "limra_auth.sqlite3")


def _limra_legacy_auth_sqlite_path(env: Any = os.environ) -> str:
    raw_path = str(env.get(LIMRA_LEGACY_AUTH_SQLITE_PATH_ENV) or "").strip()
    if raw_path.startswith("sqlite:///"):
        raw_path = raw_path.removeprefix("sqlite:///")
    elif raw_path.startswith("sqlite://"):
        raw_path = raw_path.removeprefix("sqlite://")
    if raw_path:
        return os.path.abspath(os.path.expanduser(raw_path))
    return os.path.join(_limra_data_dir(env), "legacy_auth.sqlite3")


def _limra_auth_token_ttl_seconds(env: Any = os.environ) -> int:
    raw_value = str(env.get(LIMRA_AUTH_TOKEN_TTL_SECONDS_ENV) or "").strip()
    if not raw_value:
        return LIMRA_AUTH_DEFAULT_TOKEN_TTL_SECONDS
    try:
        return max(60, int(raw_value))
    except ValueError:
        return LIMRA_AUTH_DEFAULT_TOKEN_TTL_SECONDS


def _limra_auth_secret(env: Any = os.environ) -> bytes:
    secret = str(env.get(LIMRA_AUTH_SECRET_ENV) or "").strip()
    if not secret:
        secret = "limra-local-development-secret"
    return secret.encode("utf-8")


def _limra_auth_cookie_secure(env: Any = os.environ) -> bool:
    return str(env.get(LIMRA_AUTH_COOKIE_SECURE_ENV) or "").strip().lower() in TRUTHY_ENV_VALUES


def _limra_auth_connect(env: Any = os.environ) -> sqlite3.Connection:
    database_path = _limra_auth_sqlite_path(env)
    os.makedirs(os.path.dirname(database_path), exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    _ensure_limra_auth_schema(connection)
    _migrate_legacy_auth_if_needed(connection, env)
    return connection


def _ensure_limra_auth_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS limra_auth_users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_limra_auth_users_email ON limra_auth_users(email)"
    )
    connection.commit()


def _migrate_legacy_auth_if_needed(connection: sqlite3.Connection, env: Any = os.environ) -> None:
    existing = connection.execute("SELECT COUNT(*) FROM limra_auth_users").fetchone()
    if existing and int(existing[0] or 0) > 0:
        return

    legacy_path = _limra_legacy_auth_sqlite_path(env)
    if not os.path.exists(legacy_path):
        return
    if os.path.abspath(legacy_path) == os.path.abspath(_limra_auth_sqlite_path(env)):
        return

    legacy = None
    try:
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
        table_names = {
            str(row["name"])
            for row in legacy.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('auth', 'user')"
            )
        }
        if not {"auth", "user"} <= table_names:
            return
        rows = legacy.execute(
            """
            SELECT
                auth.id AS id,
                auth.email AS email,
                auth.password AS password_hash,
                COALESCE(auth.active, 1) AS active,
                COALESCE(user.name, auth.email) AS name,
                COALESCE(user.role, 'user') AS role
            FROM auth
            LEFT JOIN user ON user.id = auth.id
            WHERE auth.id IS NOT NULL
                AND auth.email IS NOT NULL
                AND auth.password IS NOT NULL
            """
        ).fetchall()
    except Exception:
        log.exception("limra legacy auth migration failed")
        return
    finally:
        if legacy is not None:
            try:
                legacy.close()
            except Exception:
                pass

    timestamp = int(time.time())
    for row in rows:
        connection.execute(
            """
            INSERT OR IGNORE INTO limra_auth_users (
                id, email, name, role, password_hash, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(row["id"]),
                str(row["email"]).strip().lower(),
                str(row["name"] or row["email"]),
                str(row["role"] or "user"),
                str(row["password_hash"]),
                1 if row["active"] else 0,
                timestamp,
                timestamp,
            ),
        )
    connection.commit()


def _limra_auth_user_from_row(row: sqlite3.Row | Mapping[str, Any]) -> LimraAuthRecord:
    return LimraAuthRecord(
        id=str(row["id"]),
        email=str(row["email"]),
        name=str(row["name"]),
        role=str(row["role"] or "user"),
        password_hash=str(row["password_hash"]),
        active=bool(row["active"]),
    )


def _limra_auth_get_user_by_email(email: str, env: Any = os.environ) -> LimraAuthRecord | None:
    normalized_email = _normalize_auth_email(email)
    with _limra_auth_connect(env) as connection:
        row = connection.execute(
            """
            SELECT id, email, name, role, password_hash, active
            FROM limra_auth_users
            WHERE lower(email) = lower(?)
            """,
            (normalized_email,),
        ).fetchone()
    return _limra_auth_user_from_row(row) if row else None


def _limra_auth_get_user_by_id(user_id: str, env: Any = os.environ) -> LimraAuthRecord | None:
    with _limra_auth_connect(env) as connection:
        row = connection.execute(
            """
            SELECT id, email, name, role, password_hash, active
            FROM limra_auth_users
            WHERE id = ?
            """,
            (str(user_id),),
        ).fetchone()
    return _limra_auth_user_from_row(row) if row else None


def _limra_auth_insert_user(
    *,
    email: str,
    password: str,
    name: str | None = None,
    role: str = "user",
    env: Any = os.environ,
) -> LimraAuthRecord:
    normalized_email = _normalize_auth_email(email)
    normalized_name = str(name or normalized_email).strip() or normalized_email
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="password_too_long")
    timestamp = int(time.time())
    password_hash = _limra_hash_password(password)
    user_id = str(uuid.uuid4())
    try:
        with _limra_auth_connect(env) as connection:
            connection.execute(
                """
                INSERT INTO limra_auth_users (
                    id, email, name, role, password_hash, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    user_id,
                    normalized_email,
                    normalized_name,
                    role,
                    password_hash,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="email_already_registered") from exc
    created = _limra_auth_get_user_by_id(user_id, env)
    if created is None:
        raise HTTPException(status_code=500, detail="auth_user_create_failed")
    return created


def _normalize_auth_email(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if "@" not in normalized:
        raise HTTPException(status_code=400, detail="invalid_email")
    return normalized


def _limra_hash_password(password: str) -> str:
    iterations = 390_000
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return "$".join(
        (
            "limra-pbkdf2-sha256",
            str(iterations),
            _limra_auth_b64encode(salt),
            _limra_auth_b64encode(digest),
        )
    )


def _limra_verify_password(password: str, password_hash: str) -> bool:
    if password_hash.startswith("limra-pbkdf2-sha256$"):
        parts = password_hash.split("$")
        if len(parts) != 4:
            return False
        try:
            iterations = int(parts[1])
            salt = _limra_auth_b64decode(parts[2])
            expected = _limra_auth_b64decode(parts[3])
        except Exception:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(digest, expected)
    if _bcrypt is None:
        return False
    try:
        return _bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _limra_auth_public_user(
    user: LimraAuthRecord | LimraUser,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    payload = {
        "id": user.id,
        "email": getattr(user, "email", None),
        "name": getattr(user, "name", None),
        "role": user.role,
    }
    if token is not None:
        payload["token"] = token
        payload["token_type"] = "bearer"
    return payload


def _limra_user_from_auth_record(user: LimraAuthRecord) -> LimraUser:
    return LimraUser(id=user.id, role=user.role, email=user.email, name=user.name)


def _limra_auth_b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _limra_auth_b64decode(payload: str) -> bytes:
    padding = "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode((payload + padding).encode("ascii"))


def _limra_issue_auth_token(user: LimraAuthRecord, env: Any = os.environ) -> str:
    now = int(time.time())
    data = {
        "sub": user.id,
        "email": user.email,
        "role": user.role,
        "iat": now,
        "exp": now + _limra_auth_token_ttl_seconds(env),
    }
    encoded_payload = _limra_auth_b64encode(
        json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        _limra_auth_secret(env),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_limra_auth_b64encode(signature)}"


def _limra_auth_token_subject(token: str, env: Any = os.environ) -> str | None:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(
        _limra_auth_secret(env),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        supplied = _limra_auth_b64decode(encoded_signature)
    except Exception:
        return None
    if not hmac.compare_digest(expected, supplied):
        return None
    try:
        payload = json.loads(_limra_auth_b64decode(encoded_payload).decode("utf-8"))
    except Exception:
        return None
    expires_at = int(payload.get("exp") or 0)
    if expires_at < int(time.time()):
        return None
    subject = payload.get("sub")
    return str(subject) if subject else None


def _limra_auth_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    cookie_token = request.cookies.get(LIMRA_AUTH_COOKIE_NAME)
    return str(cookie_token).strip() if cookie_token else None


def _set_limra_auth_cookie(response: Response, token: str, env: Any = os.environ) -> None:
    response.set_cookie(
        LIMRA_AUTH_COOKIE_NAME,
        token,
        httponly=True,
        secure=_limra_auth_cookie_secure(env),
        samesite="lax",
        max_age=_limra_auth_token_ttl_seconds(env),
        path="/",
    )


def _clear_limra_auth_cookie(response: Response) -> None:
    response.delete_cookie(LIMRA_AUTH_COOKIE_NAME, path="/")


def _limra_auth_user_from_request(
    request: Request,
    *,
    require_admin: bool = False,
    env: Any = os.environ,
) -> LimraUser:
    token = _limra_auth_bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="not_authenticated")
    user_id = _limra_auth_token_subject(token, env)
    if not user_id:
        raise HTTPException(status_code=401, detail="not_authenticated")
    user = _limra_auth_get_user_by_id(user_id, env)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="not_authenticated")
    if require_admin and user.role != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
    return _limra_user_from_auth_record(user)


def create_limra_task_repository_from_env(env: Any = os.environ) -> LimraTaskRepository:
    backend = str(env.get(LIMRA_REPOSITORY_BACKEND_ENV, "postgres")).strip().lower()
    if backend in {"postgres", "postgresql"}:
        database_url = str(
            env.get(LIMRA_DATABASE_URL_ENV) or env.get("DATABASE_URL") or ""
        )
        if not database_url:
            raise RuntimeError("limra_postgres_database_url_missing")
        return PostgresLimraTaskRepository(database_url)

    if backend in {"sqlite", "sqlite3"}:
        return SQLiteLimraTaskRepository(_limra_sqlite_database_path(env))

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

    if backend in {"filesystem", "file", "local"}:
        bucket = str(env.get(LIMRA_OBJECT_BUCKET_ENV) or "limra-local").strip()
        root_path = str(
            env.get(LIMRA_OBJECT_STORAGE_PATH_ENV)
            or os.path.join(_limra_data_dir(env), "limra_objects")
        )
        return FileSystemLimraObjectStorage(
            root_path=root_path,
            bucket=bucket or "limra-local",
        )

    raise RuntimeError(f"unsupported_limra_object_storage_backend:{backend}")


def create_limra_upload_embedding_config_from_env(
    env: Any = os.environ,
) -> LimraUploadEmbeddingConfig:
    enabled = (
        str(env.get(LIMRA_UPLOAD_EMBEDDINGS_ENABLED_ENV, ""))
        .strip()
        .lower()
        in TRUTHY_ENV_VALUES
    )
    provider = str(env.get(LIMRA_EMBEDDING_PROVIDER_ENV, "disabled")).strip()
    provider = provider or "disabled"
    model = str(env.get(LIMRA_EMBEDDING_MODEL_ENV, "")).strip()
    dimensions = _embedding_dimensions_from_env(
        env.get(LIMRA_EMBEDDING_DIMENSIONS_ENV, LIMRA_DEFAULT_EMBEDDING_DIMENSIONS)
    )

    if enabled and provider.lower() in {"disabled", "none", "off"}:
        raise RuntimeError("limra_upload_embedding_provider_required")
    if enabled and not model:
        raise RuntimeError("limra_upload_embedding_model_required")
    if enabled and dimensions != LIMRA_DEFAULT_EMBEDDING_DIMENSIONS:
        raise RuntimeError("limra_upload_embedding_dimensions_schema_mismatch")

    return LimraUploadEmbeddingConfig(
        enabled=enabled,
        provider=provider,
        model=model,
        dimensions=dimensions,
    )


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
        "X-Limra-User-Id": user.id,
        "X-Limra-User-Role": user.role,
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


async def get_current_limra_user(request: Request) -> LimraUser:
    return _limra_auth_user_from_request(request)


async def get_current_limra_admin(request: Request) -> LimraUser:
    return _limra_auth_user_from_request(request, require_admin=True)


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


async def get_upload_embedding_config(request: Request) -> LimraUploadEmbeddingConfig:
    config = getattr(request.app.state, "limra_upload_embedding_config", None)
    if config is None:
        config = create_limra_upload_embedding_config_from_env()
        request.app.state.limra_upload_embedding_config = config
    return config


async def get_upload_embedding_provider(request: Request) -> LimraUploadEmbeddingProvider:
    provider = getattr(request.app.state, "limra_upload_embedding_provider", None)
    if provider is None:
        provider = DisabledLimraUploadEmbeddingProvider()
        request.app.state.limra_upload_embedding_provider = provider
    return provider


def get_pdf_exporter(request: Request) -> LimraPdfExporter:
    pdf_exporter = getattr(request.app.state, "limra_pdf_exporter", None)
    if pdf_exporter is None:
        pdf_exporter = PlaywrightLimraPdfExporter()
        request.app.state.limra_pdf_exporter = pdf_exporter
    return pdf_exporter


@router.post("/auth/signup", status_code=201)
async def limra_auth_signup(
    request_data: LimraAuthSignupRequest,
    response: Response,
) -> dict[str, Any]:
    user = _limra_auth_insert_user(
        email=request_data.email,
        password=request_data.password,
        name=request_data.name,
    )
    token = _limra_issue_auth_token(user)
    _set_limra_auth_cookie(response, token)
    return _limra_auth_public_user(user, token=token)


@router.post("/auth/signin")
async def limra_auth_signin(
    request_data: LimraAuthSigninRequest,
    response: Response,
) -> dict[str, Any]:
    user = _limra_auth_get_user_by_email(request_data.email)
    if user is None or not user.active or not _limra_verify_password(
        request_data.password,
        user.password_hash,
    ):
        raise HTTPException(status_code=401, detail="invalid_credentials")
    token = _limra_issue_auth_token(user)
    _set_limra_auth_cookie(response, token)
    return _limra_auth_public_user(user, token=token)


@router.post("/auth/signout")
async def limra_auth_signout(response: Response) -> dict[str, Any]:
    _clear_limra_auth_cookie(response)
    return {"ok": True}


@router.get("/auth/session")
async def limra_auth_session(
    user: LimraUser = Depends(get_current_limra_user),
) -> dict[str, Any]:
    return _limra_auth_public_user(user)


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
        public_detail = _public_error_text(
            exc.detail,
            fallback="runner_research_start_failed",
        ) or "runner_research_start_failed"
        repo.update_task(
            task.task_id,
            status="failed",
            archive_status="failed",
            error=public_detail,
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail=public_detail,
        ) from exc
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


@router.get("/tasks")
async def list_tasks(
    limit: int = Query(default=30, ge=1, le=100),
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    tasks = repo.list_user_tasks(owner_user_id=user.id, limit=limit)
    payload = {"tasks": [task.public_dict() for task in tasks], "count": len(tasks)}
    return _assert_browser_safe(payload)


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
    artifacts = _public_artifacts(repo.get_artifacts(task_id))
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


@router.get("/admin/tasks/{task_id}/event-logs")
async def admin_get_task_event_logs(
    task_id: str,
    limit: int = Query(default=500, ge=1, le=2000),
    user: LimraUser = Depends(get_current_limra_admin),
    repo: LimraTaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    events = repo.list_task_event_logs(task_id, limit=limit)
    return _assert_browser_safe(
        {
            "task_id": task_id,
            "count": len(events),
            "events": events,
            "admin": user.id,
        }
    )


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
    embedding_config: LimraUploadEmbeddingConfig = Depends(
        get_upload_embedding_config
    ),
    embedding_provider: LimraUploadEmbeddingProvider = Depends(
        get_upload_embedding_provider
    ),
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
    embedding, embedding_metadata = await _uploaded_document_embedding(
        extracted_text,
        config=embedding_config,
        provider=embedding_provider,
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
            **embedding_metadata,
        },
        embedding=embedding,
    )
    return _assert_browser_safe(document.public_dict())


@router.get("/uploads/search")
async def search_uploaded_documents(
    query: str,
    task_id: str | None = None,
    limit: int = 10,
    user: LimraUser = Depends(get_current_limra_user),
    repo: LimraTaskRepository = Depends(get_task_repository),
    embedding_config: LimraUploadEmbeddingConfig = Depends(
        get_upload_embedding_config
    ),
    embedding_provider: LimraUploadEmbeddingProvider = Depends(
        get_upload_embedding_provider
    ),
) -> dict[str, Any]:
    trimmed_query = query.strip()
    if not trimmed_query:
        raise HTTPException(status_code=400, detail="search_query_required")
    if task_id:
        _get_owned_task(repo, task_id, user)
    bounded_limit = max(1, min(int(limit), 25))
    query_embedding = await _uploaded_document_search_embedding(
        trimmed_query,
        config=embedding_config,
        provider=embedding_provider,
    )
    results = repo.search_user_documents(
        owner_user_id=user.id,
        task_id=task_id,
        query=trimmed_query,
        limit=bounded_limit,
        query_embedding=query_embedding,
    )
    return _assert_browser_safe(
        {
            "query": trimmed_query,
            "task_id": task_id,
            "documents": [result.public_dict() for result in results],
        }
    )


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
    metadata_failure = _uploaded_document_download_metadata_failure_reason(document)
    if metadata_failure is not None:
        if metadata_failure != "document_download_unavailable":
            _mark_uploaded_document_download_unavailable(
                document,
                repo=repo,
                reason=metadata_failure,
            )
        raise HTTPException(status_code=404, detail="document_object_not_found")
    try:
        data = await object_storage.get_object(object_key=document.object_key)
    except FileNotFoundError as exc:
        _mark_uploaded_document_download_unavailable(
            document,
            repo=repo,
            reason="document_object_missing",
        )
        raise HTTPException(status_code=404, detail="document_object_not_found") from exc
    except ValueError as exc:
        _mark_uploaded_document_download_unavailable(
            document,
            repo=repo,
            reason="invalid_document_object_key",
        )
        raise HTTPException(status_code=404, detail="document_object_not_found") from exc
    reuse_failure = _uploaded_document_reuse_failure_reason(data, document=document)
    if reuse_failure is not None:
        log.warning(
            "Persisted limra uploaded document object failed validation; clearing download metadata",
            extra={
                "document_id": document.document_id,
                "task_id": document.task_id,
                "user_id": document.owner_user_id,
                "reason": reuse_failure,
            },
        )
        _mark_uploaded_document_download_unavailable(
            document,
            repo=repo,
            reason=reuse_failure,
        )
        raise HTTPException(status_code=404, detail="document_object_not_found")
    return Response(
        data,
        media_type=document.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition_attachment(
                document.original_filename
            ),
            "X-Content-Type-Options": "nosniff",
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
    scrubbed_markdown = _report_markdown_from_value(
        scrub_limra_secrets(request_data.markdown)
    )
    if not scrubbed_markdown.strip():
        raise HTTPException(status_code=400, detail="empty_report_markdown")
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
        if _persisted_report_pdf_appears_blank(pdf_bytes):
            raise RuntimeError("limra_pdf_blank_rendered_pdf")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="pdf_export_failed") from exc
    pdf_debug_artifacts = _write_pdf_debug_artifacts(
        task_id=task.task_id,
        report_id=report_id,
        html_content=report_html,
        pdf_bytes=pdf_bytes,
    )

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
    report_metadata = scrub_limra_secrets({
        "pdf_bucket": stored.bucket,
        "pdf_sha256": stored.sha256,
        "pdf_size_bytes": stored.size_bytes,
        "exporter": "playwright",
    })
    if pdf_debug_artifacts:
        report_metadata["pdf_debug"] = pdf_debug_artifacts
    report = repo.record_generated_report(
        report_id=report_id,
        task_id=task.task_id,
        report_type=scrubbed_report_type,
        markdown=scrubbed_markdown,
        html=report_html,
        pdf_object_key=stored.object_key,
        evidence_refs=scrubbed_evidence_refs,
        creator_user_id=user.id,
        metadata=report_metadata,
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
        _clear_report_pdf_metadata(report, repo=repo)
        raise HTTPException(status_code=404, detail="report_pdf_not_found") from exc
    except ValueError as exc:
        _clear_report_pdf_metadata(report, repo=repo)
        raise HTTPException(status_code=404, detail="report_pdf_not_found") from exc
    reuse_failure = _persisted_report_pdf_reuse_failure_reason(data, report=report)
    if reuse_failure is not None:
        log.warning(
            "Persisted limra report PDF object failed validation; clearing metadata",
            extra={
                "task_id": report.task_id,
                "report_id": report.report_id,
                "reason": reuse_failure,
            },
        )
        _clear_report_pdf_metadata(report, repo=repo)
        raise HTTPException(status_code=404, detail="report_pdf_not_found")
    return Response(
        data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition_attachment(
                f"{report.report_id}.pdf"
            ),
            "X-Content-Type-Options": "nosniff",
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
        headers={
            "Content-Disposition": 'attachment; filename="archive.zip"',
            "X-Content-Type-Options": "nosniff",
        },
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
            archive_bytes = await object_storage.get_object(object_key=task.archive_object_key)
            reuse_failure = _persisted_archive_reuse_failure_reason(
                archive_bytes,
                task=task,
            )
            if reuse_failure is None:
                public_archive_bytes = _scrub_archive_zip(archive_bytes)
                validate_archive_zip(public_archive_bytes)
                if public_archive_bytes != archive_bytes:
                    await _store_persisted_archive_bytes(
                        task,
                        user=user,
                        repo=repo,
                        object_storage=object_storage,
                        object_key=task.archive_object_key,
                        archive_bytes=public_archive_bytes,
                    )
                return public_archive_bytes
            log.warning(
                "Persisted limra archive object failed validation; regenerating",
                extra={
                    "task_id": task.task_id,
                    "user_id": user.id,
                    "reason": reuse_failure,
                },
            )
            _clear_persisted_archive_metadata(task, repo=repo)
        except FileNotFoundError:
            log.warning(
                "Persisted limra archive object missing; regenerating",
                extra={"task_id": task.task_id, "user_id": user.id},
            )
            _clear_persisted_archive_metadata(task, repo=repo)
        except ValueError:
            log.warning(
                "Persisted limra archive object key failed validation; regenerating",
                extra={
                    "task_id": task.task_id,
                    "user_id": user.id,
                    "reason": "invalid_archive_object_key",
                },
            )
            _clear_persisted_archive_metadata(task, repo=repo)

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
    await _store_persisted_archive_bytes(
        task,
        user=user,
        repo=repo,
        object_storage=object_storage,
        object_key=object_key,
        archive_bytes=archive_bytes,
    )
    return archive_bytes


async def _store_persisted_archive_bytes(
    task: LimraTask,
    *,
    user: LimraUser,
    repo: LimraTaskRepository,
    object_storage: LimraObjectStorage,
    object_key: str,
    archive_bytes: bytes,
) -> LimraStoredObject:
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
    task.archive_status = "ready"
    task.archive_object_key = stored.object_key
    task.archive_zip_sha256 = stored.sha256
    return stored


def _persisted_archive_reuse_failure_reason(
    archive_bytes: bytes,
    *,
    task: LimraTask,
) -> str | None:
    try:
        validate_archive_zip(archive_bytes)
    except HTTPException as exc:
        return str(exc.detail or "invalid_archive_zip")
    if not task.archive_zip_sha256:
        return "archive_sha_missing"
    actual_sha = hashlib.sha256(archive_bytes).hexdigest()
    if actual_sha != task.archive_zip_sha256:
        return "archive_sha_mismatch"
    return None


def _clear_persisted_archive_metadata(
    task: LimraTask,
    *,
    repo: LimraTaskRepository,
) -> None:
    repo.update_task(
        task.task_id,
        archive_object_key=None,
        archive_zip_sha256=None,
    )
    task.archive_object_key = None
    task.archive_zip_sha256 = None


def _clear_report_pdf_metadata(
    report: LimraGeneratedReport,
    *,
    repo: LimraTaskRepository,
) -> None:
    metadata = scrub_limra_secrets(_drop_report_pdf_metadata(report.metadata or {}))
    repo.record_generated_report(
        report_id=report.report_id,
        task_id=report.task_id,
        report_type=report.report_type,
        markdown=report.markdown,
        html=report.html,
        pdf_object_key=None,
        evidence_refs=report.evidence_refs,
        creator_user_id=report.creator_user_id,
        metadata=metadata,
    )
    report.pdf_object_key = None
    report.metadata = metadata


def _write_pdf_debug_artifacts(
    *,
    task_id: str,
    report_id: str,
    html_content: str,
    pdf_bytes: bytes,
) -> dict[str, str] | None:
    debug_dir = str(os.getenv(LIMRA_PDF_DEBUG_DIR_ENV) or "").strip()
    if not debug_dir:
        return None
    os.makedirs(debug_dir, exist_ok=True)
    prefix = "-".join(
        [
            _safe_object_segment(task_id, fallback="task"),
            _safe_object_segment(report_id, fallback="report"),
            uuid.uuid4().hex[:12],
        ]
    )
    html_path = os.path.join(debug_dir, f"{prefix}.html")
    pdf_path = os.path.join(debug_dir, f"{prefix}.pdf")
    manifest_path = os.path.join(debug_dir, f"{prefix}.json")
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(html_content)
    with open(pdf_path, "wb") as handle:
        handle.write(pdf_bytes)
    manifest = {
        "task_id": task_id,
        "report_id": report_id,
        "html_path": html_path,
        "pdf_path": pdf_path,
        "html_bytes": len(html_content.encode("utf-8")),
        "pdf_bytes": len(pdf_bytes),
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
    }
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
    log.info(
        "Saved limra PDF debug artifacts",
        extra={
            "task_id": task_id,
            "report_id": report_id,
            "html_path": html_path,
            "pdf_path": pdf_path,
        },
    )
    return {
        "html_path": html_path,
        "pdf_path": pdf_path,
        "manifest_path": manifest_path,
    }


def _mark_uploaded_document_download_unavailable(
    document: LimraUploadedDocument,
    *,
    repo: LimraTaskRepository,
    reason: str,
) -> None:
    metadata = scrub_limra_secrets(
        {
            **_drop_uploaded_document_download_metadata(document.metadata or {}),
            "download_unavailable": reason,
        }
    )
    repo.record_uploaded_document(
        document_id=document.document_id,
        owner_user_id=document.owner_user_id,
        task_id=document.task_id,
        original_filename=document.original_filename,
        content_type=document.content_type,
        byte_size=document.byte_size,
        minio_bucket=document.minio_bucket,
        object_key=document.object_key,
        extracted_text=document.extracted_text,
        language=document.language,
        metadata=metadata,
        embedding=document.embedding,
    )
    document.metadata = metadata


def _drop_uploaded_document_download_metadata(
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in metadata.items()
        if str(key) not in {"sha256", "download_unavailable"}
    }


def _uploaded_document_download_metadata_failure_reason(
    document: LimraUploadedDocument,
) -> str | None:
    metadata = document.metadata or {}
    if metadata.get("download_unavailable"):
        return "document_download_unavailable"
    if not _is_valid_limra_object_key(document.object_key):
        return "invalid_document_object_key"
    expected_sha = metadata.get("sha256")
    if not isinstance(expected_sha, str) or not expected_sha.strip():
        return "document_sha_missing"
    if re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha.strip()) is None:
        return "document_sha_malformed"
    return None


def _uploaded_document_reuse_failure_reason(
    document_bytes: bytes,
    document: LimraUploadedDocument,
) -> str | None:
    expected_sha = (document.metadata or {}).get("sha256")
    if not isinstance(expected_sha, str) or not expected_sha.strip():
        return "document_sha_missing"
    expected_sha = expected_sha.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
        return "document_sha_malformed"
    actual_sha = hashlib.sha256(document_bytes).hexdigest()
    if actual_sha != expected_sha:
        return "document_sha_mismatch"
    return None


def _persisted_report_pdf_reuse_failure_reason(
    pdf_bytes: bytes,
    *,
    report: LimraGeneratedReport,
) -> str | None:
    metadata = report.metadata or {}
    expected_sha = metadata.get("pdf_sha256")
    if not isinstance(expected_sha, str) or not expected_sha.strip():
        return "pdf_sha_missing"
    expected_sha = expected_sha.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
        return "pdf_sha_malformed"
    actual_sha = hashlib.sha256(pdf_bytes).hexdigest()
    if actual_sha != expected_sha:
        return "pdf_sha_mismatch"
    if _persisted_report_pdf_appears_blank(pdf_bytes):
        return "pdf_blank"
    return None


def _persisted_report_pdf_appears_blank(pdf_bytes: bytes) -> bool:
    if not pdf_bytes.strip().startswith(b"%PDF-"):
        return False
    stream_lengths = [
        int(match.group(1))
        for match in re.finditer(rb"/Length\s+(\d+)\s*>>\s*stream", pdf_bytes)
    ]
    return bool(stream_lengths) and all(length == 0 for length in stream_lengths)


def _drop_report_pdf_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in metadata.items()
        if str(key) != "pdf_bucket" and not str(key).startswith("pdf_")
    }


def _build_persisted_archive_zip(
    task: LimraTask,
    repo: LimraTaskRepository,
) -> bytes:
    artifacts = _normalize_report_section_artifacts(repo.get_artifacts(task.task_id))
    artifact_events = _normalize_report_section_trace_events(
        repo.get_artifact_trace_events(task.task_id)
    )
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
    public_model_summary = _public_model_summary(task.model_summary or {})
    metadata = {
        "task": {
            "task_id": task.task_id,
            "owner_user_id": task.owner_user_id,
            "query": task.query,
            "status": task.status,
            "archive_status": task.archive_status,
            "scenario": task.scenario,
            "error": task.error,
            "model_summary": public_model_summary,
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
        "metadata.json": _archive_json_text(metadata, member_name="metadata.json"),
        "report.html": str(scrub_limra_secrets(report_html)),
        "report.md": str(scrub_limra_secrets(report_markdown)),
        "trace.json": _archive_json_text(trace, member_name="trace.json"),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member_name in ARCHIVE_MEMBER_ORDER:
            _write_archive_member(archive, member_name, members[member_name])
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
        return _report_markdown_from_value(report.markdown)
    report_sections = artifacts.get("report_sections", [])
    if report_sections:
        sections = []
        for section in report_sections:
            title = str(section.get("title") or section.get("section_id") or "Section")
            body = _report_markdown_from_value(
                section.get("markdown") or section.get("content") or section
            )
            sections.append(f"## {title}\n\n{body}".strip())
        return "# limra report\n\n" + "\n\n".join(sections)
    return (
        "# limra report\n\n"
        f"Query: {task.query}\n\n"
        "No generated report content has been recorded yet."
    )


def _archive_json_text(value: Any, *, member_name: str | None = None) -> str:
    value = _normalize_archive_json_payload(value, member_name=member_name)
    scrubbed = scrub_limra_secrets(value)
    if member_name is not None:
        scrubbed = _public_archive_member_payload(member_name, scrubbed)
    scrubbed = json.loads(json.dumps(scrubbed, ensure_ascii=False, default=str))
    return json.dumps(scrubbed, ensure_ascii=False, sort_keys=True, indent=2)


def _normalize_archive_json_payload(value: Any, *, member_name: str | None) -> Any:
    if member_name != "trace.json" or not isinstance(value, Mapping):
        return value
    trace = dict(value)
    artifacts = trace.get("artifacts")
    if isinstance(artifacts, Mapping):
        trace["artifacts"] = _normalize_report_section_artifacts(artifacts)
    artifact_events = trace.get("artifact_events")
    if isinstance(artifact_events, list):
        trace["artifact_events"] = _normalize_report_section_trace_events(
            [dict(event) for event in artifact_events if isinstance(event, dict)]
        )
    return trace


def _public_archive_member_payload(member_name: str, payload: Any) -> Any:
    if member_name not in {"metadata.json", "trace.json"}:
        return payload
    if not isinstance(payload, Mapping):
        return payload
    public_payload = dict(payload)
    task_payload = public_payload.get("task")
    if isinstance(task_payload, Mapping):
        public_task = dict(task_payload)
        if "model_summary" in public_task:
            public_task["model_summary"] = _public_model_summary(
                public_task.get("model_summary") or {}
            )
        public_payload["task"] = public_task
    return public_payload


def validate_archive_zip(archive_bytes: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = archive.namelist()
            if names != list(ARCHIVE_MEMBER_ORDER):
                raise HTTPException(status_code=502, detail="invalid_archive_members")
            if any(name.startswith("/") or ".." in name.split("/") for name in names):
                raise HTTPException(status_code=502, detail="unsafe_archive_member")
            for member_name in ARCHIVE_MEMBER_ORDER:
                text = _decode_archive_text_member(archive.read(member_name))
                if member_name in ARCHIVE_JSON_MEMBERS:
                    _parse_archive_json_member(text)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=502, detail="invalid_archive_zip") from exc


def _scrub_archive_zip(archive_bytes: bytes) -> bytes:
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as source:
            raw_members = {
                member_name: source.read(member_name)
                for member_name in ARCHIVE_MEMBER_ORDER
            }
            try:
                report_markdown = _scrub_archive_member_text(
                    "report.md",
                    raw_members["report.md"],
                )
            except UnicodeDecodeError:
                report_markdown = _archive_member_decode_failure_text("report.md")
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
                for member_name in ARCHIVE_MEMBER_ORDER:
                    raw_member = raw_members[member_name]
                    try:
                        if member_name == "report.md":
                            scrubbed = report_markdown
                        elif member_name == "report.html":
                            scrubbed = str(
                                scrub_limra_secrets(
                                    _render_report_html(
                                        markdown=report_markdown,
                                        evidence_refs=_evidence_refs_from_markdown(
                                            report_markdown
                                        ),
                                    )
                                )
                            )
                        else:
                            scrubbed = _scrub_archive_member_text(member_name, raw_member)
                        _write_archive_member(
                            target,
                            member_name,
                            scrubbed.encode("utf-8"),
                        )
                    except UnicodeDecodeError:
                        if member_name in ARCHIVE_JSON_MEMBERS:
                            raise HTTPException(
                                status_code=502,
                                detail="invalid_archive_member_encoding",
                            )
                        fallback = _archive_member_decode_failure_text(member_name)
                        _write_archive_member(
                            target,
                            member_name,
                            fallback.encode("utf-8"),
                        )
    except (KeyError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=502, detail="invalid_archive_zip") from exc
    return output.getvalue()


def _write_archive_member(
    archive: zipfile.ZipFile,
    member_name: str,
    data: str | bytes,
) -> None:
    member = zipfile.ZipInfo(member_name, date_time=ARCHIVE_MEMBER_TIMESTAMP)
    member.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(member, data)


def _scrub_archive_member_text(member_name: str, raw_member: bytes) -> str:
    try:
        text = raw_member.decode("utf-8")
    except UnicodeDecodeError as exc:
        if member_name in ARCHIVE_JSON_MEMBERS:
            raise HTTPException(
                status_code=502,
                detail="invalid_archive_member_encoding",
            ) from exc
        raise
    if member_name in ARCHIVE_JSON_MEMBERS:
        payload = _parse_archive_json_member(text)
        return _archive_json_text(payload, member_name=member_name)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _scrub_secret_text(text)
    if member_name == "report.md":
        return str(scrub_limra_secrets(_report_markdown_from_value(payload)))
    return _archive_json_text(payload, member_name=member_name)


def _decode_archive_text_member(raw_member: bytes) -> str:
    try:
        return raw_member.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="invalid_archive_member_encoding",
        ) from exc


def _parse_archive_json_member(text: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="invalid_archive_member_json",
        ) from exc
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=502, detail="invalid_archive_member_json")
    return payload


def _archive_member_decode_failure_text(member_name: str) -> str:
    if member_name in ARCHIVE_JSON_MEMBERS:
        return _archive_json_text(
            {"archive_member_error": "invalid_text_encoding"},
            member_name=member_name,
        )
    if member_name == "report.html":
        return "<!doctype html><main>Archive member could not be decoded.</main>"
    return "# limra report\n\nArchive member could not be decoded."


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
        await _record_stream_event(repo, runtime_state, task.task_id, terminal_event)
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
        active_event = _active_stream_status_event(current, runtime_snapshot)
        _record_task_event_log(repo, task.task_id, active_event)
        yield _sse_bytes(_assert_browser_safe(active_event))
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
    current_agent_name: str | None = None

    try:
        async for runner_event in research_client.stream_events(task=task, user=user):
            event = _normalize_runner_event(task, runner_event)
            event_payload = event.get("payload")
            if event.get("type") == "start_of_agent" and isinstance(event_payload, dict):
                current_agent_name = str(event_payload.get("agent_name") or "")
            elif event.get("type") == "end_of_agent":
                current_agent_name = None
            event = _scrub_final_show_text_event(
                event,
                current_agent_name=current_agent_name,
            )
            event = _scrub_runner_artifact_event(event)
            event = _scrub_runner_event(event, task_id=task.task_id)
            applied_status = _apply_task_status_from_event(repo, task.task_id, event)
            saw_terminal_status = saw_terminal_status or applied_status in FINAL_TASK_STATUSES
            if applied_status in FINAL_TASK_STATUSES and isinstance(
                event.get("payload"), dict
            ):
                event["payload"]["terminal"] = True
            warning = _record_artifact_from_event(repo, task, event)
            final_report_event = _record_final_show_text_report_from_event(
                repo,
                task,
                event,
                current_agent_name=current_agent_name,
            )
            await _record_stream_event(repo, runtime_state, task.task_id, event)
            if applied_status in FINAL_TASK_STATUSES:
                await runtime_state.update_task_runtime(
                    task.task_id,
                    {"terminal": True},
                )

            yield _sse_bytes(_assert_browser_safe(event))
            if warning:
                await _record_stream_event(repo, runtime_state, task.task_id, warning)
                yield _sse_bytes(_assert_browser_safe(warning))
            if final_report_event:
                await _record_stream_event(repo, runtime_state, task.task_id, final_report_event)
                yield _sse_bytes(_assert_browser_safe(final_report_event))
            if applied_status in FINAL_TASK_STATUSES:
                stream_close_reason = f"terminal_{applied_status}"
                break

        if not saw_terminal_status:
            status_event = await _authoritative_runner_status_event(
                repo,
                task,
                user,
                research_client,
            )
            if status_event:
                await _record_stream_event(repo, runtime_state, task.task_id, status_event)
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
            await _record_stream_event(repo, runtime_state, task.task_id, status_event)
            stream_close_reason = _stream_close_reason_from_event(
                status_event,
                stream_close_reason,
            )
            yield _sse_bytes(_assert_browser_safe(status_event))
    except asyncio.CancelledError:
        current = repo.get_task(task.task_id)
        if current and current.status in FINAL_TASK_STATUSES:
            stream_close_reason = f"terminal_{current.status}"
            await runtime_state.update_task_runtime(
                task.task_id,
                {
                    "status": current.status,
                    "archive_status": current.archive_status,
                    "terminal": True,
                    "error": current.error,
                },
            )
            return
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
            await _record_stream_event(repo, runtime_state, task.task_id, terminal_event)
            yield _sse_bytes(_assert_browser_safe(terminal_event))
            return
        public_error = _public_error_text(
            exc.detail,
            fallback="limra_event_proxy_failed",
        )
        repo.update_task(
            task.task_id,
            status="failed",
            archive_status="failed",
            error=public_error,
        )
        error_event = {
            "task_id": task.task_id,
            "type": "error",
            "payload": {"error": public_error},
        }
        await _record_stream_event(repo, runtime_state, task.task_id, error_event)
        yield _sse_bytes(_assert_browser_safe(error_event))
    except Exception as exc:
        stream_close_reason = "limra_event_proxy_failed"
        current = repo.get_task(task.task_id)
        if current and current.status in FINAL_TASK_STATUSES:
            terminal_event = _terminal_status_event(current)
            await _record_stream_event(repo, runtime_state, task.task_id, terminal_event)
            yield _sse_bytes(_assert_browser_safe(terminal_event))
            return
        repo.update_task(
            task.task_id,
            status="failed",
            archive_status="failed",
            error="limra_event_proxy_failed",
        )
        error_event = {
            "task_id": task.task_id,
            "type": "error",
            "payload": {"error": "limra_event_proxy_failed"},
        }
        await _record_stream_event(repo, runtime_state, task.task_id, error_event)
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
        warning = _public_error_text(
            exc.detail,
            fallback="runner_status_warning",
        )
        public_reason = _public_reason_text(reason)
        return {
            "task_id": task.task_id,
            "type": "status",
            "payload": {
                "status": current.status if current else task.status,
                "archive_status": current.archive_status if current else task.archive_status,
                "status_source": "limra",
                "warning": warning,
                **({"reason": public_reason} if public_reason else {}),
            },
        }

    current = _apply_authoritative_runner_status(
        repo,
        task.task_id,
        scrub_limra_secrets(runner_status),
    )
    payload: dict[str, Any] = {
        "status": current.status,
        "archive_status": current.archive_status,
        "terminal": current.status in FINAL_TASK_STATUSES,
        "status_source": "runner",
    }
    if reason:
        payload["reason"] = _public_reason_text(reason)
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
        updates["error"] = _public_error_text(
            runner_status["error"],
            fallback="runner_task_failed",
        )
    return repo.update_task(task_id, **updates)


def _terminal_status_event(task: LimraTask, *, reason: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": task.status,
        "archive_status": task.archive_status,
        "terminal": True,
    }
    if task.error:
        payload["error"] = _public_error_text(
            task.error,
            fallback="limra_task_failed",
        )
    if reason:
        payload["reason"] = _public_reason_text(reason)
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


async def _record_stream_event(
    repo: LimraTaskRepository,
    runtime_state: LimraRuntimeState,
    task_id: str,
    event: dict[str, Any],
) -> None:
    _record_task_event_log(repo, task_id, event)
    await _record_runtime_event(runtime_state, task_id, event)


def _record_task_event_log(
    repo: LimraTaskRepository,
    task_id: str,
    event: dict[str, Any],
) -> None:
    try:
        repo.record_task_event_log(
            task_id,
            _assert_browser_safe(event),
            source="runner_stream",
        )
    except Exception:
        log.warning(
            "Failed to persist limra task event log",
            extra={"task_id": task_id},
            exc_info=True,
        )


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
        if isinstance(payload, dict) and payload.get("error"):
            updates["error"] = _public_error_text(
                payload["error"],
                fallback="runner_task_failed",
            )
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
    if artifact_type == "report_section":
        artifact = _normalize_report_section_artifact(artifact)
    if metadata.get("evidence_refs") is not None:
        artifact.setdefault("evidence_refs", metadata["evidence_refs"])
    if metadata.get("confidence") is not None:
        artifact.setdefault("confidence", metadata["confidence"])
    if metadata.get("notes") is not None:
        artifact.setdefault("notes", metadata["notes"])
    _ensure_artifact_id(repo, task.task_id, artifact_type, artifact)
    repo.record_artifact(task.task_id, artifact_type, artifact)
    return None


def _record_final_show_text_report_from_event(
    repo: LimraTaskRepository,
    task: LimraTask,
    event: dict[str, Any],
    *,
    current_agent_name: str | None,
) -> dict[str, Any] | None:
    if current_agent_name != "Final Summary" or event.get("type") != "tool_call":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("tool_name") != "show_text":
        return None
    tool_input = payload.get("tool_input")
    markdown = _show_text_markdown(tool_input)
    if not markdown:
        return None
    scrubbed_markdown = str(scrub_limra_secrets(markdown)).strip()
    if not scrubbed_markdown:
        return None
    artifacts = repo.get_artifacts(task.task_id)
    for section in artifacts.get("report_sections", []):
        if (
            section.get("source_event_type") == "final_summary_show_text"
            and str(section.get("markdown") or "").strip() == scrubbed_markdown
        ):
            return None
    artifact = {
        "artifact_type": "report_section",
        "title": _report_title_from_value(tool_input, fallback="最终回答"),
        "markdown": scrubbed_markdown,
        "source_event_type": "final_summary_show_text",
        "evidence_refs": _evidence_refs_from_markdown(scrubbed_markdown),
    }
    _ensure_artifact_id(repo, task.task_id, "report_section", artifact)
    repo.record_artifact(task.task_id, "report_section", artifact)
    return {
        "task_id": task.task_id,
        "type": "report_section_generated",
        "payload": artifact,
    }


def _scrub_final_show_text_event(
    event: dict[str, Any],
    *,
    current_agent_name: str | None,
) -> dict[str, Any]:
    if current_agent_name != "Final Summary" or event.get("type") != "tool_call":
        return event
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("tool_name") != "show_text":
        return event
    scrubbed_event = dict(event)
    scrubbed_payload = dict(payload)
    scrubbed_tool_input = scrub_limra_secrets(payload.get("tool_input"))
    scrubbed_markdown = _show_text_markdown(scrubbed_tool_input)
    scrubbed_title = _report_title_from_value(scrubbed_tool_input)
    if scrubbed_markdown:
        if isinstance(scrubbed_tool_input, Mapping):
            scrubbed_tool_input = dict(scrubbed_tool_input)
            scrubbed_tool_input["text"] = scrubbed_markdown
        else:
            scrubbed_tool_input = {"text": scrubbed_markdown}
        if scrubbed_title:
            scrubbed_tool_input.setdefault("title", scrubbed_title)
    scrubbed_payload["tool_input"] = scrubbed_tool_input
    scrubbed_event["payload"] = scrubbed_payload
    return scrubbed_event


def _scrub_runner_artifact_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    if event_type not in {
        *ARTIFACT_EVENT_TYPES.keys(),
        "artifact",
        "artifact_recorded",
        "record_research_artifact",
        "artifact_warning",
    }:
        return event
    scrubbed_event = dict(event)
    scrubbed_event["payload"] = scrub_limra_secrets(event.get("payload"))
    return scrubbed_event


def _scrub_runner_event(event: dict[str, Any], *, task_id: str) -> dict[str, Any]:
    event_type = str(event.get("type") or "runner_event")
    scrubbed_value = scrub_limra_secrets(event)
    scrubbed_event = dict(scrubbed_value) if isinstance(scrubbed_value, dict) else {}
    scrubbed_event["task_id"] = task_id
    scrubbed_event["type"] = str(scrub_limra_secrets(event_type))
    scrubbed_event = _shape_runner_event_public_errors(scrubbed_event)
    return scrubbed_event


def _shape_runner_event_public_errors(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return event
    shaped_event = dict(event)
    shaped_payload = dict(payload)
    if shaped_payload.get("error"):
        shaped_payload["error"] = _public_error_text(
            shaped_payload["error"],
            fallback="runner_task_failed",
        )
    if shaped_payload.get("warning"):
        shaped_payload["warning"] = _public_error_text(
            shaped_payload["warning"],
            fallback="runner_status_warning",
        )
    if shaped_payload.get("reason"):
        shaped_payload["reason"] = _public_reason_text(shaped_payload["reason"])
    data = shaped_payload.get("data")
    if isinstance(data, dict):
        shaped_data = dict(data)
        if shaped_data.get("error"):
            shaped_data["error"] = _public_error_text(
                shaped_data["error"],
                fallback="runner_task_failed",
            )
        if shaped_data.get("warning"):
            shaped_data["warning"] = _public_error_text(
                shaped_data["warning"],
                fallback="runner_status_warning",
            )
        if shaped_data.get("reason"):
            shaped_data["reason"] = _public_reason_text(shaped_data["reason"])
        shaped_payload["data"] = shaped_data
    shaped_event["payload"] = shaped_payload
    return shaped_event


def _show_text_markdown(tool_input: Any) -> str:
    value: Any = ""
    if isinstance(tool_input, dict):
        value = tool_input.get("text") or ""
        result = tool_input.get("result")
        if not value and isinstance(result, dict):
            value = result.get("text") or result
    elif isinstance(tool_input, str):
        value = tool_input
    return _report_markdown_from_value(value)


REPORT_TEXT_FIELDS = ("markdown", "content", "text", "summary")
REPORT_TITLE_FIELDS = ("title", "report_title", "name")
REPORT_ID_FIELDS = ("section_id", "report_id", "id")


def _report_wrapper_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _report_markdown_from_value(value: Any) -> str:
    wrapper = _report_wrapper_mapping(value)
    if wrapper is not None:
        for field in REPORT_TEXT_FIELDS:
            if field not in wrapper:
                continue
            markdown = _report_markdown_from_value(wrapper.get(field))
            if markdown:
                return markdown
        if "payload" in wrapper:
            markdown = _report_markdown_from_value(wrapper.get("payload"))
            if markdown:
                return markdown
        return ""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _report_field_from_value(value: Any, fields: tuple[str, ...]) -> str | None:
    wrapper = _report_wrapper_mapping(value)
    if wrapper is None:
        return None
    for field in fields:
        raw = wrapper.get(field)
        if raw is None or isinstance(raw, (Mapping, list, tuple, set)):
            continue
        text = str(raw).strip()
        if text:
            return text
    for field in (*REPORT_TEXT_FIELDS, "payload", "result"):
        nested = wrapper.get(field)
        if nested is None or nested is value:
            continue
        text = _report_field_from_value(nested, fields)
        if text:
            return text
    return None


def _report_title_from_value(value: Any, *, fallback: str | None = None) -> str | None:
    return _report_field_from_value(value, REPORT_TITLE_FIELDS) or fallback


def _report_section_id_from_value(value: Any) -> str | None:
    candidate = _report_field_from_value(value, REPORT_ID_FIELDS)
    if candidate and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", candidate):
        return candidate
    return None


def _normalize_report_section_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(artifact)
    source = (
        normalized.get("markdown")
        or normalized.get("content")
        or normalized.get("text")
        or normalized.get("summary")
        or normalized
    )
    markdown = _report_markdown_from_value(source)
    if markdown:
        normalized["markdown"] = markdown
        for field in ("content", "text", "summary"):
            if field in normalized and _report_wrapper_mapping(normalized[field]) is not None:
                normalized[field] = markdown
    title = _report_title_from_value(source)
    if title and not normalized.get("title"):
        normalized["title"] = title
    section_id = _report_section_id_from_value(source)
    if section_id and not normalized.get("section_id"):
        normalized["section_id"] = section_id
    return normalized


def _normalize_report_section_artifacts(
    artifacts: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    normalized = _normalize_artifact_buckets(artifacts)
    normalized["report_sections"] = [
        _normalize_report_section_artifact(section)
        for section in normalized.get("report_sections", [])
    ]
    return normalized


def _normalize_report_section_trace_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_events: list[dict[str, Any]] = []
    for event in events:
        normalized_event = dict(event)
        payload = normalized_event.get("payload")
        if (
            normalized_event.get("artifact_type") == "report_section"
            and isinstance(payload, dict)
        ):
            normalized_event["payload"] = _normalize_report_section_artifact(payload)
        normalized_events.append(normalized_event)
    return normalized_events


def _public_artifacts(artifacts: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    scrubbed = scrub_limra_secrets(artifacts)
    return _normalize_report_section_artifacts(scrubbed if isinstance(scrubbed, Mapping) else {})


def _evidence_refs_from_markdown(markdown: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\[(EVID-\d{3,})\]", markdown):
        ref = match.group(1)
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


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


def _normalize_artifact_buckets(value: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    buckets = _empty_artifact_buckets()
    for bucket in buckets:
        items = value.get(bucket)
        if isinstance(items, list):
            buckets[bucket] = [dict(item) for item in items if isinstance(item, dict)]
    return buckets


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
        embedding=_embedding_from_value(row.get("embedding")),
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


def _embedding_dimensions_from_env(value: Any) -> int:
    try:
        dimensions = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("limra_upload_embedding_dimensions_invalid") from exc
    if dimensions <= 0:
        raise RuntimeError("limra_upload_embedding_dimensions_invalid")
    return dimensions


async def _uploaded_document_embedding(
    extracted_text: str | None,
    *,
    config: LimraUploadEmbeddingConfig,
    provider: LimraUploadEmbeddingProvider,
) -> tuple[list[float] | None, dict[str, Any]]:
    metadata: dict[str, Any] = {
        "embedding": {
            "enabled": config.enabled,
            "provider": config.provider,
            "model": config.model,
            "dimensions": config.dimensions,
        }
    }
    if not config.enabled:
        metadata["embedding"]["status"] = "disabled"
        return None, metadata

    text = (extracted_text or "").strip()
    if not text:
        metadata["embedding"]["status"] = "skipped_empty_text"
        return None, metadata

    try:
        raw_embedding = await provider.embed_upload_text(text, config=config)
        embedding = [float(value) for value in raw_embedding]
    except RuntimeError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="upload_embedding_failed") from exc

    if len(embedding) != config.dimensions:
        raise HTTPException(status_code=500, detail="upload_embedding_dimension_mismatch")
    metadata["embedding"]["status"] = "stored"
    return embedding, metadata


async def _uploaded_document_search_embedding(
    query: str,
    *,
    config: LimraUploadEmbeddingConfig,
    provider: LimraUploadEmbeddingProvider,
) -> list[float] | None:
    if not config.enabled:
        return None
    try:
        raw_embedding = await provider.embed_upload_text(query, config=config)
        embedding = [float(value) for value in raw_embedding]
    except Exception:
        log.warning(
            "Limra upload search embedding unavailable; falling back to lexical search",
            extra={"embedding_provider": config.provider},
        )
        return None
    if len(embedding) != config.dimensions:
        raise HTTPException(
            status_code=500,
            detail="upload_search_embedding_dimension_mismatch",
        )
    return embedding


def _embedding_from_value(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("[") and stripped.endswith("]"):
            stripped = stripped[1:-1]
        parts = [part.strip() for part in stripped.split(",") if part.strip()]
        return [float(part) for part in parts] if parts else None
    if isinstance(value, (list, tuple)):
        return [float(part) for part in value]
    return None


def _vector_param(value: list[float] | None) -> str | None:
    if value is None:
        return None
    return "[" + ",".join(f"{float(part):.12g}" for part in value) + "]"


def _search_terms(query: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for match in SEARCH_TOKEN_PATTERN.findall(query.lower()):
        term = match.strip("._-")
        if not term or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def _rank_uploaded_documents(
    documents: list[LimraUploadedDocument],
    query: str,
    limit: int,
) -> list[LimraUploadedDocumentSearchResult]:
    terms = _search_terms(query)
    if not terms:
        return []
    results = [
        result
        for result in (
            _uploaded_document_search_result(document, terms)
            for document in documents
        )
        if result is not None
    ]
    results.sort(
        key=lambda result: (
            -result.score,
            result.document.original_filename.lower(),
            result.document.document_id,
        )
    )
    return results[:limit]


def _rank_uploaded_documents_by_embedding(
    documents: list[LimraUploadedDocument],
    query: str,
    query_embedding: list[float],
    limit: int,
) -> list[LimraUploadedDocumentSearchResult]:
    results = [
        result
        for result in (
            _uploaded_document_vector_search_result(
                document,
                query,
                _cosine_similarity(query_embedding, document.embedding),
            )
            for document in documents
            if document.embedding is not None
        )
        if result is not None
    ]
    results.sort(
        key=lambda result: (
            -result.score,
            result.document.original_filename.lower(),
            result.document.document_id,
        )
    )
    return results[:limit]


def _cosine_similarity(
    left: list[float],
    right: list[float] | None,
) -> float | None:
    if right is None or len(left) != len(right):
        return None
    dot = sum(
        float(left_value) * float(right_value)
        for left_value, right_value in zip(left, right)
    )
    left_norm = sum(float(value) * float(value) for value in left) ** 0.5
    right_norm = sum(float(value) * float(value) for value in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    return dot / (left_norm * right_norm)


def _uploaded_document_vector_search_result(
    document: LimraUploadedDocument,
    query: str,
    score: float | None,
) -> LimraUploadedDocumentSearchResult | None:
    if score is None:
        return None
    terms = _search_terms(query)
    matched_terms = [
        term
        for term in terms
        if term in (document.extracted_text or "").lower()
        or term in (document.original_filename or "").lower()
    ]
    return LimraUploadedDocumentSearchResult(
        document=document,
        score=score,
        snippet=_uploaded_document_search_snippet(
            document.extracted_text or "",
            document.original_filename or "",
            matched_terms or terms,
        ),
        matched_terms=matched_terms,
    )


def _uploaded_document_search_result(
    document: LimraUploadedDocument,
    terms: list[str],
) -> LimraUploadedDocumentSearchResult | None:
    haystack_text = document.extracted_text or ""
    filename = document.original_filename or ""
    text_lower = haystack_text.lower()
    filename_lower = filename.lower()
    matched_terms = [
        term for term in terms if term in text_lower or term in filename_lower
    ]
    if not matched_terms:
        return None

    score = 0.0
    for term in matched_terms:
        score += text_lower.count(term)
        score += filename_lower.count(term) * 2
    return LimraUploadedDocumentSearchResult(
        document=document,
        score=score,
        snippet=_uploaded_document_search_snippet(haystack_text, filename, matched_terms),
        matched_terms=matched_terms,
    )


def _uploaded_document_search_snippet(
    text: str,
    filename: str,
    terms: list[str],
) -> str:
    source = text.strip() or filename
    if not source:
        return ""
    source_lower = source.lower()
    first_index = min(
        (source_lower.find(term) for term in terms if source_lower.find(term) >= 0),
        default=0,
    )
    start = max(first_index - 60, 0)
    end = min(start + 180, len(source))
    snippet = source[start:end].strip()
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(source):
        snippet = f"{snippet}..."
    return _browser_safe_text(snippet)


def _browser_safe_text(value: str) -> str:
    scrubbed = str(scrub_limra_secrets(value))
    for needle in FORBIDDEN_BROWSER_SUBSTRINGS:
        scrubbed = scrubbed.replace(needle, LIMRA_SECRET_REDACTION)
    return scrubbed


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
            scrubbed_key = _scrub_mapping_key(key)
            if _is_secret_field_name(key):
                scrubbed[scrubbed_key] = LIMRA_SECRET_REDACTION
            else:
                scrubbed[scrubbed_key] = scrub_limra_secrets(item)
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


def _scrub_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(scrub_limra_secrets(str(value)))


def _public_error_text(value: Any, *, fallback: str) -> str | None:
    scrubbed = _scrub_optional_text(value)
    if scrubbed is None:
        return None
    if any(pattern.search(scrubbed) for pattern in INTERNAL_ERROR_TEXT_PATTERNS):
        return fallback
    return scrubbed


def _public_reason_text(value: Any) -> str | None:
    return _public_error_text(value, fallback="runner_stream_conflict")


def _public_model_summary(value: Any) -> Any:
    scrubbed = scrub_limra_secrets(value)
    return _public_metadata_value(scrubbed)


def _public_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        public: dict[Any, Any] = {}
        for key, item in value.items():
            public_key = _public_metadata_key(key)
            if public_key is None:
                continue
            public[public_key] = _public_metadata_value(item)
        return public
    if isinstance(value, list):
        return [_public_metadata_value(item) for item in value]
    if isinstance(value, tuple):
        return [_public_metadata_value(item) for item in value]
    if isinstance(value, set):
        return [_public_metadata_value(item) for item in sorted(value, key=str)]
    if isinstance(value, bytes):
        try:
            return _public_metadata_value(value.decode("utf-8"))
        except UnicodeDecodeError:
            return LIMRA_SECRET_REDACTION
    if isinstance(value, str):
        return _public_error_text(
            value,
            fallback="limra_internal_value_redacted",
        )
    return value


def _public_metadata_key(key: Any) -> str | None:
    key_text = str(key)
    if _is_internal_metadata_field_name(key_text):
        return None
    public_key = _public_error_text(
        key_text,
        fallback="limra_internal_field_redacted",
    )
    if not public_key or public_key == "limra_internal_field_redacted":
        return None
    return public_key


def _is_internal_metadata_field_name(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_")
    return bool(
        re.search(
            r"(?:^|_)(?:object_key|minio_object_key|pdf_object_key|"
            r"archive_object_key|runner_task_id|runner_url|service_token)(?:$|_)",
            normalized,
        )
    )


def _scrub_mapping_key(key: Any) -> Any:
    scrubbed_key = scrub_limra_secrets(key)
    try:
        hash(scrubbed_key)
    except TypeError:
        return _scrub_secret_text(str(key))
    return scrubbed_key


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
    object_key = validate_limra_object_key(object_key)
    data_bytes = bytes(data)
    return LimraStoredObject(
        object_key=object_key,
        bucket=bucket,
        content_type=content_type or "application/octet-stream",
        size_bytes=len(data_bytes),
        sha256=hashlib.sha256(data_bytes).hexdigest(),
        metadata=_object_metadata(metadata or {}),
    )


def validate_limra_object_key(object_key: str) -> str:
    if not isinstance(object_key, str):
        raise ValueError("invalid_limra_object_key")
    object_key = object_key.strip()
    if not object_key or object_key.startswith("/") or "\\" in object_key:
        raise ValueError("invalid_limra_object_key")
    segments = object_key.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("invalid_limra_object_key")
    if any(segment.endswith(OBJECT_METADATA_SIDECAR_SUFFIX) for segment in segments):
        raise ValueError("invalid_limra_object_key")
    return object_key


def _is_valid_limra_object_key(object_key: str | None) -> bool:
    if not object_key:
        return False
    try:
        validate_limra_object_key(object_key)
    except ValueError:
        return False
    return True


def _is_valid_report_pdf_metadata(
    object_key: str | None,
    pdf_sha256: Any,
) -> bool:
    return (
        _is_valid_limra_object_key(object_key)
        and isinstance(pdf_sha256, str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", pdf_sha256.strip()) is not None
    )


def _is_valid_uploaded_document_download_metadata(
    object_key: str | None,
    sha256: Any,
    download_unavailable: Any,
) -> bool:
    return (
        not download_unavailable
        and _is_valid_limra_object_key(object_key)
        and isinstance(sha256, str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", sha256.strip()) is not None
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
    style = (
        "<style>"
        "@page{size:A4;margin:18mm;}"
        "html,body{margin:0;padding:0;background:#fff;color:#111;}"
        "body{font-family:\"Noto Sans CJK SC\",\"Noto Sans SC\",\"Microsoft YaHei\","
        "\"PingFang SC\",Arial,sans-serif;font-size:11pt;line-height:1.55;}"
        "h1,h2,h3,h4,h5,h6{color:#111;line-height:1.25;margin:0 0 0.55em;}"
        "p,ul,ol,table{margin:0 0 0.9em;}"
        "table{width:100%;border-collapse:collapse;}"
        "th,td{border:1px solid #bbb;padding:4px 6px;text-align:left;}"
        "a{color:#0645ad;text-decoration:none;}"
        "*{-webkit-print-color-adjust:exact;print-color-adjust:exact;}"
        "</style>"
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{REPORT_CSP}\">"
        f"<title>limra report</title>{style}</head><body>"
        f"{body}</body></html>"
    )


def _markdown_to_html(markdown: str) -> str:
    safe_markdown = _strip_active_report_markup(markdown)
    lines = safe_markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            blocks.append(
                f"<h{level}>{_markdown_inline_to_html(heading.group(2).strip())}</h{level}>"
            )
            index += 1
            continue
        if re.match(r"^\s*---+\s*$", line):
            blocks.append("<hr>")
            index += 1
            continue
        if _is_markdown_table(lines, index):
            table_html, index = _markdown_table_to_html(lines, index)
            blocks.append(table_html)
            continue
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while index < len(lines) and re.match(r"^\s*[-*]\s+", lines[index]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[index]))
                index += 1
            blocks.append(
                "<ul>"
                + "".join(
                    f"<li>{_markdown_inline_to_html(item)}</li>" for item in items
                )
                + "</ul>"
            )
            continue
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while index < len(lines) and re.match(r"^\s*\d+\.\s+", lines[index]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[index]))
                index += 1
            blocks.append(
                "<ol>"
                + "".join(
                    f"<li>{_markdown_inline_to_html(item)}</li>" for item in items
                )
                + "</ol>"
            )
            continue
        paragraph = []
        while (
            index < len(lines)
            and lines[index].strip()
            and not re.match(r"^(#{1,6})\s+(.+)$", lines[index])
            and not _is_markdown_table(lines, index)
            and not re.match(r"^\s*[-*]\s+", lines[index])
            and not re.match(r"^\s*\d+\.\s+", lines[index])
            and not re.match(r"^\s*---+\s*$", lines[index])
        ):
            paragraph.append(lines[index])
            index += 1
        blocks.append(
            "<p>"
            + "<br>".join(_markdown_inline_to_html(item) for item in paragraph)
            + "</p>"
        )
    return "\n".join(blocks) or "<p></p>"


def _is_markdown_table(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and re.match(
            r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$",
            lines[index + 1],
        )
        is not None
    )


def _markdown_table_to_html(lines: list[str], index: int) -> tuple[str, int]:
    header = _split_markdown_table_row(lines[index])
    index += 2
    rows: list[list[str]] = []
    while index < len(lines) and "|" in lines[index] and lines[index].strip():
        rows.append(_split_markdown_table_row(lines[index]))
        index += 1
    header_html = "".join(
        f"<th>{_markdown_inline_to_html(cell)}</th>" for cell in header
    )
    row_html = "".join(
        "<tr>"
        + "".join(
            f"<td>{_markdown_inline_to_html(row[cell_index] if cell_index < len(row) else '')}</td>"
            for cell_index, _ in enumerate(header)
        )
        + "</tr>"
        for row in rows
    )
    return (
        f"<table><thead><tr>{header_html}</tr></thead><tbody>{row_html}</tbody></table>",
        index,
    )


def _split_markdown_table_row(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _markdown_inline_to_html(value: str) -> str:
    escaped = html.escape(value, quote=True)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    return re.sub(
        r"(https?://[^\s<]+[^<.,;:!?)\]\s])",
        r'<a href="\1" rel="noreferrer">\1</a>',
        escaped,
    )


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
    if normalized in UPLOAD_ALLOWED_CONTENT_TYPES:
        return normalized
    if normalized not in UPLOAD_GENERIC_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="unsupported_upload_type")
    lower_name = filename.lower()
    if lower_name.endswith(".pdf"):
        return "application/pdf"
    if lower_name.endswith(".md"):
        return "text/markdown"
    if lower_name.endswith(".csv"):
        return "text/csv"
    if lower_name.endswith(".txt"):
        return "text/plain"
    raise HTTPException(status_code=415, detail="unsupported_upload_type")


def _extract_uploaded_document_text(
    data: bytes,
    *,
    filename: str,
    content_type: str,
) -> str:
    if content_type == "application/pdf":
        return _extract_pdf_text(data)
    if content_type in {"text/plain", "text/markdown", "text/csv"}:
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
