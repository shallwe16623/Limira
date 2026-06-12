"""Research graph contracts for the Limira deep-research workflow.

The enabled executor is intentionally serial and local for now. It exposes real
typed node boundaries while keeping the legacy agent available as a bounded
research-unit adapter and as the default fallback outside the graph flag.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EVIDENCE_ID_FULL_PATTERN = re.compile(r"EVID-(?:\d{3,}|[0-9a-fA-F]{12})")
EVIDENCE_ID_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(EVID-[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)
GRAPH_CONTENT_HASH_CHARS = 32
GRAPH_SOURCE_SUMMARY_MAX_CHARS = 20_000
GRAPH_FINDING_SUMMARY_MAX_CHARS = 10_000
LANGGRAPH_RESUME_POLICY = "resume_from_checkpoint"
LANGGRAPH_RESUME_RECOVERABLE_REASON = "langgraph_checkpoint_resumable"
VERIFIER_NEGATIVE_MARKERS = (
    "not listed",
    "not designated",
    "not sanctioned",
    "no sanctions",
    "no evidence",
    "does not appear",
    "not subject",
    "removed from",
    "absent from",
)
VERIFIER_POSITIVE_MARKERS = (
    "is listed",
    "listed under",
    "is designated",
    "designated under",
    "is sanctioned",
    "sanctioned under",
    "subject to",
    "confirmed",
    "appears on",
    "exposure is confirmed",
)


class EvidenceStrictMode(StrEnum):
    WARN = "warn"
    BLOCK = "block"


def parse_evidence_strict_mode(value: Any = None) -> EvidenceStrictMode:
    if value is None or value == "":
        return EvidenceStrictMode.WARN
    if isinstance(value, EvidenceStrictMode):
        return value
    mode = str(value).strip().lower()
    if mode == EvidenceStrictMode.WARN.value:
        return EvidenceStrictMode.WARN
    if mode == EvidenceStrictMode.BLOCK.value:
        return EvidenceStrictMode.BLOCK
    raise ValueError(
        "invalid_evidence_strict_mode: "
        f"{value!r}; expected one of block, warn"
    )


class ResearchPhase(StrEnum):
    SCOPE = "scope"
    PLAN = "plan"
    RESEARCH = "research"
    COMPRESS = "compress"
    VERIFY = "verify"
    WRITE = "write"
    RECONCILE = "reconcile"
    COMPLETE = "complete"


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_sources: int = Field(default=3, ge=1, le=50)
    prefer_primary_sources: bool = True
    allow_secondary_sources: bool = True
    require_retrieved_at: bool = True
    prefer_uploaded_documents: bool = False
    prefer_scenario_sources: bool = False


class ResearchBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_query: str = Field(min_length=1, max_length=20_000)
    clarified_question: str = Field(min_length=1, max_length=20_000)
    scope: str = Field(min_length=1, max_length=4_000)
    success_criteria: list[str] = Field(default_factory=list, max_length=20)
    required_sources: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)


class ResearchUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=4_000)
    search_queries: list[str] = Field(default_factory=list, min_length=1, max_length=8)
    source_policy: SourcePolicy = Field(default_factory=SourcePolicy)
    max_sources: int = Field(default=6, ge=1, le=25)
    status: Literal["pending", "running", "completed", "failed"] = "pending"


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_units: list[ResearchUnit] = Field(default_factory=list, min_length=1, max_length=12)
    expected_artifacts: list[str] = Field(default_factory=list, max_length=20)
    verification_strategy: str = Field(min_length=1, max_length=2_000)


class UploadScopeSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=120)
    attached_document_id: str | None = Field(default=None, max_length=120)
    chunk_id: str | None = Field(default=None, max_length=120)
    filename: str | None = Field(default=None, max_length=1_000)
    content_hash: str | None = Field(default=None, max_length=128)
    text_char_count: int | None = Field(default=None, ge=0)
    text_truncated: bool = False


class ResearchUploadScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_count: int = Field(default=0, ge=0, le=500)
    retrieval_status: str | None = Field(default=None, max_length=80)
    retrieved_document_ids: list[str] = Field(default_factory=list, max_length=500)
    context_only_document_ids: list[str] = Field(default_factory=list, max_length=500)
    source_payload_count: int = Field(default=0, ge=0, le=500)
    source_payload_refs: list[UploadScopeSourceRef] = Field(
        default_factory=list,
        max_length=100,
    )


class UploadedDocumentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str | None = Field(default=None, max_length=120)
    document_id: str = Field(min_length=1, max_length=120)
    attached_document_id: str | None = Field(default=None, max_length=120)
    chunk_id: str = Field(min_length=1, max_length=120)
    filename: str | None = Field(default=None, max_length=1_000)
    source_type: Literal["limira_upload"] = "limira_upload"
    retrieval_status: Literal["retrieved"] = "retrieved"
    source_content_state: Literal["content_bearing"] = "content_bearing"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = Field(min_length=16, max_length=128)
    text: str = Field(min_length=1, max_length=20_000)
    snippet: str | None = Field(default=None, max_length=2_000)
    text_char_count: int | None = Field(default=None, ge=1)
    text_truncated: bool = False


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    retrieved_source_id: str | None = Field(default=None, max_length=120)
    url: str | None = Field(default=None, max_length=4_000)
    document_id: str | None = Field(default=None, max_length=120)
    chunk_id: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=1_000)
    source_type: str = Field(default="web", max_length=80)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = Field(min_length=16, max_length=128)
    quote_or_summary: str = Field(min_length=1, max_length=20_000)
    claims: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    tool_name: str | None = Field(default=None, max_length=120)


class RetrievedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    url: str | None = Field(default=None, max_length=4_000)
    document_id: str | None = Field(default=None, max_length=120)
    chunk_id: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=1_000)
    source_type: str = Field(min_length=1, max_length=80)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = Field(min_length=16, max_length=128)
    quote_or_summary: str = Field(min_length=1, max_length=20_000)
    tool_name: str = Field(min_length=1, max_length=120)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CompressedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    research_unit_id: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=10_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class SourceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=1_000)
    summary: str | None = Field(default=None, max_length=2_000)
    source_type: str = Field(default="web", max_length=80)
    source_state: str = Field(default="source_candidate", max_length=80)
    source_content_state: str | None = Field(default=None, max_length=80)
    retrieval_status: str | None = Field(default=None, max_length=80)
    url: str | None = Field(default=None, max_length=4_000)
    document_id: str | None = Field(default=None, max_length=120)
    attached_document_id: str | None = Field(default=None, max_length=120)
    chunk_id: str | None = Field(default=None, max_length=120)
    filename: str | None = Field(default=None, max_length=1_000)
    retrieved_at: datetime | None = None
    content_hash: str | None = Field(default=None, max_length=128)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_event_type: str | None = Field(default=None, max_length=120)


class ResearchClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    claim: str = Field(min_length=1, max_length=10_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    source: Literal["finding", "verified_claim"] = "finding"
    support_type: str | None = Field(default=None, max_length=80)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class VerifiedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    claim: str = Field(min_length=1, max_length=10_000)
    support_type: Literal[
        "supported",
        "contradicted",
        "insufficient",
        "weak",
        "invalid_ref",
    ]
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    rationale: str = Field(default="", max_length=10_000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("evidence_ids")
    @classmethod
    def _normalize_evidence_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            evidence_id = str(item).strip()
            if evidence_id:
                normalized.append(evidence_id)
        return normalized

    @model_validator(mode="after")
    def _validate_supported_evidence_ids(self) -> "VerifiedClaim":
        if self.support_type not in {"supported", "contradicted"}:
            return self
        if not self.evidence_ids:
            raise ValueError("verified_claim_evidence_id_required")
        if any(
            EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id) is None
            for evidence_id in self.evidence_ids
        ):
            raise ValueError("verified_claim_evidence_id_invalid")
        return self


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=1_000)
    markdown: str = Field(min_length=1, max_length=20_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=200)
    source_event_type: str | None = Field(default=None, max_length=120)


class ResearchGraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=20_000)
    scenario: str | None = Field(default=None, max_length=1_000)
    source_policy: SourcePolicy = Field(default_factory=SourcePolicy)
    evidence_strict_mode: EvidenceStrictMode = EvidenceStrictMode.WARN
    upload_scope: ResearchUploadScope = Field(default_factory=ResearchUploadScope)
    phase: ResearchPhase = ResearchPhase.SCOPE
    brief: ResearchBrief
    plan: ResearchPlan
    current_unit_id: str | None = Field(default=None, max_length=80)
    research_units: list[ResearchUnit] = Field(default_factory=list, max_length=12)
    upload_sources: list[UploadedDocumentSource] = Field(default_factory=list, max_length=100)
    context_only_upload_document_ids: list[str] = Field(default_factory=list, max_length=100)
    source_candidates: list[SourceCandidate] = Field(default_factory=list, max_length=2_000)
    retrieved_sources: list[RetrievedSource] = Field(default_factory=list, max_length=2_000)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=2_000)
    findings: list[CompressedFinding] = Field(default_factory=list, max_length=500)
    claims: list[ResearchClaim] = Field(default_factory=list, max_length=500)
    verified_claims: list[VerifiedClaim] = Field(default_factory=list, max_length=500)
    report_sections: list[ReportSection] = Field(default_factory=list, max_length=100)
    final_summary: str | None = Field(default=None, max_length=20_000)
    final_boxed_answer: str | None = Field(default=None, max_length=10_000)
    warnings: list[str] = Field(default_factory=list, max_length=200)
    resume_from_checkpoint: bool = False
    resume_start_phase: ResearchPhase | None = None

    @field_validator("warnings")
    @classmethod
    def _bound_warnings(cls, value: list[str]) -> list[str]:
        return [
            str(item or "").strip()[:1_000]
            for item in value
            if str(item or "").strip()
        ]


class ResearchGraphExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    state: ResearchGraphState
    final_summary: str
    final_boxed_answer: str
    failure_experience_summary: Any = None


class ResearchGraphExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    orchestrator: Any
    original_task_description: str
    task_file_name: str | None = None
    task_id: str = "default_task"
    is_final_retry: bool = False
    evidence_strict_mode: EvidenceStrictMode = EvidenceStrictMode.WARN


class ResearchGraphNodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    state: ResearchGraphState
    current_research_unit: str | None = None
    executor_state: dict[str, Any] = Field(default_factory=dict)
    artifact_events: list[dict[str, Any]] = Field(default_factory=list)
    final_summary: str | None = None
    final_boxed_answer: str | None = None
    failure_experience_summary: Any = None


@dataclass(frozen=True)
class EvidenceReferenceValidation:
    evidence_refs: list[str]
    unresolved_refs: list[str]
    invalid_refs: list[str]

    @property
    def has_errors(self) -> bool:
        return bool(self.unresolved_refs or self.invalid_refs)


class ResearchGraphNode:
    phase: ResearchPhase

    async def run(
        self,
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
        previous: ResearchGraphNodeOutput | None = None,
    ) -> ResearchGraphNodeOutput:
        raise NotImplementedError


class UploadedDocumentSourceProvider:
    """Local task-scoped provider for text-bearing uploaded document chunks."""

    tool_name = "uploaded_document_source_provider"

    def __init__(self, sources: list[UploadedDocumentSource]):
        self._sources = list(sources)

    def retrieve(self) -> list[UploadedDocumentSource]:
        return [
            source
            for source in self._sources
            if source.retrieval_status == "retrieved"
            and source.source_content_state == "content_bearing"
            and bool(source.text.strip())
        ]


@dataclass(frozen=True)
class RetrieverRequest:
    state: ResearchGraphState
    unit: ResearchUnit
    unit_index: int
    context: ResearchGraphExecutionContext


@dataclass(frozen=True)
class RetrieverUnitResult:
    retriever_order: list[str]
    candidates: list[SourceCandidate]
    retrieved_sources: list[RetrievedSource]
    warnings: list[str]
    legacy_adapter_used: bool


class GraphRetriever:
    name: str

    async def search(self, request: RetrieverRequest) -> list[SourceCandidate]:
        return []

    async def retrieve(
        self,
        request: RetrieverRequest,
        candidate: SourceCandidate,
    ) -> RetrievedSource | None:
        return None


class RetrieverRegistry:
    def __init__(self) -> None:
        self._retrievers: dict[str, GraphRetriever] = {}
        self._disabled: set[str] = set()

    def register(self, retriever: GraphRetriever, *, enabled: bool = True) -> None:
        name = _normalized_retriever_name(retriever.name)
        if not name:
            raise ValueError("retriever_name_required")
        self._retrievers[name] = retriever
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)

    def resolve(self, name: str) -> GraphRetriever:
        normalized = _normalized_retriever_name(name)
        if normalized not in self._retrievers:
            raise KeyError(f"unknown_retriever:{normalized or 'empty'}")
        if normalized in self._disabled:
            raise RuntimeError(f"disabled_retriever:{normalized}")
        return self._retrievers[normalized]

    def disable(self, name: str) -> None:
        normalized = _normalized_retriever_name(name)
        if normalized not in self._retrievers:
            raise KeyError(f"unknown_retriever:{normalized or 'empty'}")
        self._disabled.add(normalized)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._retrievers)


class WebSearchRetriever(GraphRetriever):
    name = "web_search"

    async def search(self, request: RetrieverRequest) -> list[SourceCandidate]:
        return [
            _snippet_source_candidate_for_unit(
                request.state,
                request.unit,
                request.unit_index,
            )
        ]


class PageVisitOrJinaSummaryRetriever(GraphRetriever):
    name = "page_visit_or_jina_summary"

    async def search(self, request: RetrieverRequest) -> list[SourceCandidate]:
        if not request.unit.source_policy.prefer_scenario_sources:
            return []
        return [
            SourceCandidate(
                candidate_id=_stable_prefixed_id(
                    "SRC-PAGE",
                    f"{request.state.task_id}:{request.unit.id}:page_summary",
                ),
                title=f"Scenario-prioritized page summary for {request.unit.id}",
                summary=(
                    "Deterministic page-summary candidate for scenario-prioritized "
                    f"research on {request.unit.question}"
                ),
                source_type=self.name,
                source_state="source_candidate",
                source_content_state="content_bearing",
                retrieval_status="retrievable",
                url=f"jina://{request.unit.id}",
                confidence=0.45,
                source_event_type="research_graph_page_summary",
            )
        ]

    async def retrieve(
        self,
        request: RetrieverRequest,
        candidate: SourceCandidate,
    ) -> RetrievedSource | None:
        if candidate.source_type != self.name:
            return None
        summary = _bounded_graph_text(
            (
                f"Scenario-prioritized page summary for {request.unit.question}. "
                f"Query terms: {'; '.join(request.unit.search_queries)}."
            ),
            GRAPH_SOURCE_SUMMARY_MAX_CHARS,
        )
        return RetrievedSource(
            id=retrieved_source_id_for_source(
                task_id=request.state.task_id,
                source=f"langgraph://{request.unit.id}/page_summary",
                index=request.unit_index,
            ),
            url=candidate.url,
            title=candidate.title,
            source_type=self.name,
            content_hash=_short_content_hash(summary),
            quote_or_summary=summary,
            tool_name=self.name,
            confidence=candidate.confidence,
        )


class UploadedDocumentSearchRetriever(GraphRetriever):
    name = "uploaded_document_search"

    async def search(self, request: RetrieverRequest) -> list[SourceCandidate]:
        upload_sources = UploadedDocumentSourceProvider(
            request.state.upload_sources
        ).retrieve()
        matched_uploads = [
            source
            for source in upload_sources
            if _upload_source_matches_unit(source, request.unit)
        ]
        if not matched_uploads and request.unit.source_policy.prefer_uploaded_documents:
            matched_uploads = list(upload_sources)
        return [_source_candidate_from_upload(source) for source in matched_uploads]

    async def retrieve(
        self,
        request: RetrieverRequest,
        candidate: SourceCandidate,
    ) -> RetrievedSource | None:
        if candidate.source_type != "limira_upload":
            return None
        upload_sources = UploadedDocumentSourceProvider(
            request.state.upload_sources
        ).retrieve()
        upload_source = _upload_source_for_candidate(candidate, upload_sources)
        if upload_source is None:
            return None
        return _retrieved_source_from_upload_for_unit(
            state=request.state,
            unit=request.unit,
            upload_source=upload_source,
            index=request.unit_index,
        )


class LegacyAgentAdapterRetriever(GraphRetriever):
    name = "legacy_agent_adapter"

    async def search(self, request: RetrieverRequest) -> list[SourceCandidate]:
        return [
            _legacy_adapter_source_candidate_for_unit(
                request.state,
                request.unit,
                request.unit_index,
            )
        ]

    async def retrieve(
        self,
        request: RetrieverRequest,
        candidate: SourceCandidate,
    ) -> RetrievedSource | None:
        if candidate.source_type != self.name:
            return None
        final_summary, _boxed_answer, _failure_experience_summary = (
            await request.context.orchestrator.run_main_agent(
                task_description=research_unit_task_description(
                    request.state,
                    request.unit,
                    request.context.original_task_description,
                ),
                task_file_name=request.context.task_file_name,
                task_id=request.context.task_id,
                is_final_retry=request.context.is_final_retry,
            )
        )
        research_summary = _required_output_text(
            final_summary,
            "research_graph_research_output_required",
        )
        research_summary = _bounded_graph_text(
            research_summary,
            GRAPH_SOURCE_SUMMARY_MAX_CHARS,
        )
        return RetrievedSource(
            id=retrieved_source_id_for_source(
                task_id=request.state.task_id,
                source=f"langgraph://{request.unit.id}/legacy_agent_adapter",
                index=request.unit_index,
            ),
            url=f"graph://{request.unit.id}",
            title=request.unit.question,
            source_type=self.name,
            content_hash=_short_content_hash(research_summary),
            quote_or_summary=research_summary,
            tool_name=self.name,
            confidence=candidate.confidence,
        )


def default_retriever_registry() -> RetrieverRegistry:
    registry = RetrieverRegistry()
    registry.register(WebSearchRetriever())
    registry.register(PageVisitOrJinaSummaryRetriever())
    registry.register(UploadedDocumentSearchRetriever())
    registry.register(LegacyAgentAdapterRetriever())
    return registry


class ScopeNode(ResearchGraphNode):
    phase = ResearchPhase.SCOPE

    async def run(
        self,
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
        previous: ResearchGraphNodeOutput | None = None,
    ) -> ResearchGraphNodeOutput:
        scoped_state = state.model_copy(update={"phase": self.phase})
        return ResearchGraphNodeOutput(
            state=scoped_state,
            executor_state={
                "node": self.__class__.__name__,
                "scope_length": len(scoped_state.brief.scope),
                "success_criteria_count": len(scoped_state.brief.success_criteria),
            },
        )


class PlannerNode(ResearchGraphNode):
    phase = ResearchPhase.PLAN

    async def run(
        self,
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
        previous: ResearchGraphNodeOutput | None = None,
    ) -> ResearchGraphNodeOutput:
        if not state.plan.research_units:
            raise ValueError("research_graph_plan_required")
        planned_state = state.model_copy(update={"phase": self.phase})
        return ResearchGraphNodeOutput(
            state=planned_state,
            executor_state={
                "node": self.__class__.__name__,
                "research_unit_count": len(planned_state.plan.research_units),
                "expected_artifacts": list(planned_state.plan.expected_artifacts),
            },
        )


class ResearchUnitNode(ResearchGraphNode):
    phase = ResearchPhase.RESEARCH

    async def run(
        self,
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
        previous: ResearchGraphNodeOutput | None = None,
    ) -> ResearchGraphNodeOutput:
        retrieved_sources = list(state.retrieved_sources)
        source_candidates = list(state.source_candidates)
        evidence = list(state.evidence)
        artifact_events: list[dict[str, Any]] = []
        failure_experience_summary = None
        current_unit_id = None
        upload_provider = UploadedDocumentSourceProvider(state.upload_sources)
        upload_sources = upload_provider.retrieve()
        for upload_index, upload_source in enumerate(upload_sources):
            retrieved_source = self._retrieved_source_from_upload(
                state,
                upload_source,
                upload_index,
                upload_provider.tool_name,
            )
            evidence_item = self._evidence_from_upload_source(
                state,
                upload_source,
                retrieved_source,
                upload_index,
                upload_provider.tool_name,
            )
            retrieved_sources.append(retrieved_source)
            source_candidates.append(_source_candidate_from_upload(upload_source))
            evidence.append(evidence_item)
            artifact_events.extend(
                [
                    _upload_source_candidate_artifact_event(upload_source),
                    _retrieved_source_artifact_event(retrieved_source),
                    _evidence_artifact_event(evidence_item),
                ]
            )
        for index, unit in enumerate(state.plan.research_units):
            current_unit_id = unit.id
            final_summary, _boxed_answer, failure_experience_summary = (
                await context.orchestrator.run_main_agent(
                    task_description=research_unit_task_description(
                        state,
                        unit,
                        context.original_task_description,
                    ),
                    task_file_name=context.task_file_name,
                    task_id=context.task_id,
                    is_final_retry=context.is_final_retry,
                )
            )
            research_summary = _required_output_text(
                final_summary,
                "research_graph_research_output_required",
            )
            research_summary = _bounded_graph_text(
                research_summary,
                GRAPH_SOURCE_SUMMARY_MAX_CHARS,
            )
            content_hash = _short_content_hash(research_summary)
            retrieved_source = RetrievedSource(
                id=retrieved_source_id_for_source(
                    task_id=state.task_id,
                    source=f"graph://{unit.id}",
                    index=index,
                ),
                url=f"graph://{unit.id}",
                title=unit.question,
                source_type="graph_research_unit",
                content_hash=content_hash,
                quote_or_summary=research_summary,
                tool_name="serial_graph_research_unit",
                confidence=0.55,
            )
            evidence_item = EvidenceItem(
                id=evidence_id_for_source(
                    task_id=state.task_id,
                    source=f"graph://{unit.id}",
                    index=index,
                ),
                retrieved_source_id=retrieved_source.id,
                url=retrieved_source.url,
                title=unit.question,
                source_type=retrieved_source.source_type,
                retrieved_at=retrieved_source.retrieved_at,
                content_hash=content_hash,
                quote_or_summary=research_summary,
                claims=[research_summary],
                confidence=retrieved_source.confidence,
                tool_name=retrieved_source.tool_name,
            )
            retrieved_sources.append(retrieved_source)
            evidence.append(evidence_item)
            artifact_events.extend(
                [
                    _retrieved_source_artifact_event(retrieved_source),
                    _evidence_artifact_event(evidence_item),
                ]
            )
        completed_units = [
            unit.model_copy(update={"status": "completed"})
            for unit in state.plan.research_units
        ]
        researched_state = state.model_copy(
            update={
                "phase": self.phase,
                "plan": state.plan.model_copy(
                    update={"research_units": completed_units}
                ),
                "current_unit_id": current_unit_id,
                "research_units": completed_units,
                "source_candidates": source_candidates,
                "retrieved_sources": retrieved_sources,
                "evidence": evidence,
            }
        )
        return ResearchGraphNodeOutput(
            state=researched_state,
            current_research_unit=current_unit_id,
            executor_state={
                "node": self.__class__.__name__,
                "research_unit_count": len(completed_units),
                "retrieved_source_count": len(retrieved_sources),
                "evidence_count": len(evidence),
                "upload_source_provider": upload_provider.tool_name,
                "upload_source_count": len(upload_sources),
                "context_only_upload_document_ids": list(
                    state.context_only_upload_document_ids
                ),
                "legacy_adapter_calls": len(completed_units),
            },
            artifact_events=artifact_events,
            failure_experience_summary=failure_experience_summary,
        )

    def _retrieved_source_from_upload(
        self,
        state: ResearchGraphState,
        upload_source: UploadedDocumentSource,
        index: int,
        tool_name: str,
    ) -> RetrievedSource:
        return RetrievedSource(
            id=retrieved_source_id_for_source(
                task_id=state.task_id,
                source=f"upload://{upload_source.document_id}/{upload_source.chunk_id}",
                index=index,
            ),
            document_id=upload_source.document_id,
            chunk_id=upload_source.chunk_id,
            title=_upload_source_title(upload_source),
            source_type="limira_upload",
            retrieved_at=upload_source.retrieved_at,
            content_hash=upload_source.content_hash,
            quote_or_summary=upload_source.text,
            tool_name=tool_name,
            confidence=0.8,
        )

    def _evidence_from_upload_source(
        self,
        state: ResearchGraphState,
        upload_source: UploadedDocumentSource,
        retrieved_source: RetrievedSource,
        index: int,
        tool_name: str,
    ) -> EvidenceItem:
        return EvidenceItem(
            id=evidence_id_for_source(
                task_id=state.task_id,
                source=f"upload://{upload_source.document_id}/{upload_source.chunk_id}",
                index=index,
            ),
            retrieved_source_id=retrieved_source.id,
            document_id=upload_source.document_id,
            chunk_id=upload_source.chunk_id,
            title=retrieved_source.title,
            source_type="limira_upload",
            retrieved_at=upload_source.retrieved_at,
            content_hash=upload_source.content_hash,
            quote_or_summary=upload_source.text,
            claims=[upload_source.text],
            confidence=0.8,
            tool_name=tool_name,
        )


class LangGraphResearchUnitNode(ResearchGraphNode):
    phase = ResearchPhase.RESEARCH

    def __init__(
        self,
        *,
        retriever_registry: RetrieverRegistry | None = None,
        retriever_names: list[str] | None = None,
    ) -> None:
        self._retriever_registry = retriever_registry or default_retriever_registry()
        self._retriever_names = list(retriever_names) if retriever_names else None

    async def run(
        self,
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
        previous: ResearchGraphNodeOutput | None = None,
    ) -> ResearchGraphNodeOutput:
        retrieved_sources = list(state.retrieved_sources)
        source_candidates = list(state.source_candidates)
        evidence = list(state.evidence)
        findings = list(state.findings)
        artifact_events: list[dict[str, Any]] = []
        unit_substeps: list[dict[str, Any]] = []
        retriever_warnings: list[str] = []
        completed_unit_ids: list[str] = [
            unit.id for unit in state.plan.research_units if unit.status == "completed"
        ]
        current_unit_id = None
        legacy_adapter_calls = 0

        for unit_index, unit in enumerate(state.plan.research_units):
            current_unit_id = unit.id
            if unit.status == "completed":
                unit_substeps.append(
                    {
                        "unit_id": unit.id,
                        "steps": [],
                        "resume_action": "skipped_completed_unit",
                        "retriever_order": [],
                        "source_candidate_ids": [],
                        "snippet_only_candidate_ids": [],
                        "retrieved_source_ids": [],
                        "evidence_ids": [
                            item.id
                            for item in evidence
                            if _research_unit_id_from_evidence(item, unit_index)
                            == unit.id
                        ],
                        "finding_ids": [
                            finding.id
                            for finding in findings
                            if finding.research_unit_id == unit.id
                        ],
                        "legacy_adapter_used": False,
                        "warnings": [],
                    }
                )
                continue
            unit_result = await self._run_retriever_registry(
                state=state,
                unit=unit,
                unit_index=unit_index,
                context=context,
            )
            source_candidates.extend(unit_result.candidates)
            retrieved_sources.extend(unit_result.retrieved_sources)
            retriever_warnings.extend(unit_result.warnings)
            legacy_adapter_calls += int(unit_result.legacy_adapter_used)
            artifact_events.extend(
                _source_candidate_artifact_event(candidate)
                for candidate in unit_result.candidates
            )
            artifact_events.extend(
                _retrieved_source_artifact_event(source)
                for source in unit_result.retrieved_sources
            )

            unit_evidence: list[EvidenceItem] = []
            for retrieved_index, retrieved_source in enumerate(
                unit_result.retrieved_sources
            ):
                evidence_item = _evidence_from_retrieved_source(
                    state=state,
                    unit=unit,
                    retrieved_source=retrieved_source,
                    index=retrieved_index,
                )
                unit_evidence.append(evidence_item)
                evidence.append(evidence_item)
                artifact_events.append(_evidence_artifact_event(evidence_item))

            unit_findings = self._synthesize_findings(unit, unit_evidence, unit_index)
            findings.extend(unit_findings)
            if unit.id not in completed_unit_ids:
                completed_unit_ids.append(unit.id)
            unit_substeps.append(
                {
                    "unit_id": unit.id,
                    "steps": ["search", "retrieve", "promote", "synthesize"],
                    "retriever_order": unit_result.retriever_order,
                    "source_candidate_ids": [
                        candidate.candidate_id for candidate in unit_result.candidates
                    ],
                    "snippet_only_candidate_ids": [
                        candidate.candidate_id
                        for candidate in unit_result.candidates
                        if candidate.source_content_state == "snippet_only"
                    ],
                    "retrieved_source_ids": [
                        source.id for source in unit_result.retrieved_sources
                    ],
                    "evidence_ids": [item.id for item in unit_evidence],
                    "finding_ids": [finding.id for finding in unit_findings],
                    "legacy_adapter_used": unit_result.legacy_adapter_used,
                    "warnings": list(unit_result.warnings),
                }
            )

        completed_unit_set = set(completed_unit_ids)
        completed_units = [
            unit.model_copy(
                update={
                    "status": "completed"
                    if unit.id in completed_unit_set
                    else unit.status
                }
            )
            for unit in state.plan.research_units
        ]
        researched_state = state.model_copy(
            update={
                "phase": self.phase,
                "plan": state.plan.model_copy(
                    update={"research_units": completed_units}
                ),
                "current_unit_id": current_unit_id,
                "research_units": completed_units,
                "source_candidates": source_candidates,
                "retrieved_sources": retrieved_sources,
                "evidence": evidence,
                "findings": findings,
                "warnings": [*state.warnings, *retriever_warnings],
            }
        )
        return ResearchGraphNodeOutput(
            state=researched_state,
            current_research_unit=current_unit_id,
            executor_state={
                "node": self.__class__.__name__,
                "research_unit_count": len(completed_units),
                "current_unit_id": current_unit_id,
                "completed_unit_ids": completed_unit_ids,
                "source_candidate_count": len(source_candidates),
                "retrieved_source_count": len(retrieved_sources),
                "evidence_count": len(evidence),
                "finding_count": len(findings),
                "unit_substeps": unit_substeps,
                "resume_marker": "completed_unit_ids_available",
                "legacy_adapter_calls": legacy_adapter_calls,
                "retriever_warnings": retriever_warnings,
                "context_only_upload_document_ids": list(
                    state.context_only_upload_document_ids
                ),
            },
            artifact_events=artifact_events,
        )

    async def _run_retriever_registry(
        self,
        *,
        state: ResearchGraphState,
        unit: ResearchUnit,
        unit_index: int,
        context: ResearchGraphExecutionContext,
    ) -> RetrieverUnitResult:
        request = RetrieverRequest(
            state=state,
            unit=unit,
            unit_index=unit_index,
            context=context,
        )
        retriever_order = self._retriever_order(unit)
        non_legacy_names = [
            name for name in retriever_order if name != "legacy_agent_adapter"
        ]
        candidates, retrieved_sources, warnings = await self._run_retrievers(
            request,
            non_legacy_names,
        )
        legacy_adapter_used = False
        if not retrieved_sources and "legacy_agent_adapter" in retriever_order:
            legacy_candidates, legacy_sources, legacy_warnings = (
                await self._run_retrievers(request, ["legacy_agent_adapter"])
            )
            candidates.extend(legacy_candidates)
            retrieved_sources.extend(legacy_sources)
            warnings.extend(legacy_warnings)
            legacy_adapter_used = bool(legacy_sources)
        return RetrieverUnitResult(
            retriever_order=retriever_order,
            candidates=candidates,
            retrieved_sources=retrieved_sources,
            warnings=warnings,
            legacy_adapter_used=legacy_adapter_used,
        )

    async def _run_retrievers(
        self,
        request: RetrieverRequest,
        names: list[str],
    ) -> tuple[list[SourceCandidate], list[RetrievedSource], list[str]]:
        candidates: list[SourceCandidate] = []
        retrieved_sources: list[RetrievedSource] = []
        warnings: list[str] = []
        for name in names:
            try:
                retriever = self._retriever_registry.resolve(name)
            except Exception as exc:
                warnings.append(f"retriever_unavailable:{name}:{exc}")
                continue
            retriever_candidates = await retriever.search(request)
            candidates.extend(retriever_candidates)
            for candidate in retriever_candidates:
                retrieved_source = await retriever.retrieve(request, candidate)
                if retrieved_source is not None:
                    retrieved_sources.append(retrieved_source)
        return candidates, retrieved_sources, warnings

    def _retriever_order(self, unit: ResearchUnit) -> list[str]:
        if self._retriever_names is not None:
            return [_normalized_retriever_name(name) for name in self._retriever_names]
        ordered = [
            "web_search",
            "page_visit_or_jina_summary",
            "uploaded_document_search",
            "legacy_agent_adapter",
        ]
        if unit.source_policy.prefer_scenario_sources:
            ordered.remove("page_visit_or_jina_summary")
            ordered.insert(0, "page_visit_or_jina_summary")
        if unit.source_policy.prefer_uploaded_documents:
            ordered.remove("uploaded_document_search")
            ordered.insert(0, "uploaded_document_search")
        return ordered

    def _synthesize_findings(
        self,
        unit: ResearchUnit,
        evidence_items: list[EvidenceItem],
        unit_index: int,
    ) -> list[CompressedFinding]:
        if not evidence_items:
            return []
        summary = _bounded_graph_text(
            "\n\n".join(item.quote_or_summary for item in evidence_items),
            GRAPH_FINDING_SUMMARY_MAX_CHARS,
        )
        confidence = max(item.confidence for item in evidence_items)
        return [
            CompressedFinding(
                id=f"finding-langgraph-{unit_index + 1}-{unit.id}",
                research_unit_id=unit.id,
                summary=summary,
                evidence_ids=[item.id for item in evidence_items],
                confidence=confidence,
            )
        ]


class EvidenceCompressorNode(ResearchGraphNode):
    phase = ResearchPhase.COMPRESS

    async def run(
        self,
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
        previous: ResearchGraphNodeOutput | None = None,
    ) -> ResearchGraphNodeOutput:
        if not state.evidence:
            raise ValueError("research_graph_compressor_output_required")
        findings = list(state.findings) or self._compress_findings(state)
        compressed_state = state.model_copy(
            update={
                "phase": self.phase,
                "findings": findings,
                "claims": _merge_graph_claims(
                    state.claims,
                    _claim_state_from_findings(findings),
                ),
            }
        )
        return ResearchGraphNodeOutput(
            state=compressed_state,
            executor_state={
                "node": self.__class__.__name__,
                "finding_count": len(findings),
                "evidence_count": len(state.evidence),
                "findings": [
                    finding.model_dump(mode="json", exclude_none=True)
                    for finding in findings
                ],
            },
            artifact_events=[_finding_artifact_event(finding) for finding in findings],
            failure_experience_summary=(
                previous.failure_experience_summary if previous else None
            ),
        )

    def _compress_findings(self, state: ResearchGraphState) -> list[CompressedFinding]:
        findings: list[CompressedFinding] = []
        for index, evidence in enumerate(state.evidence):
            research_unit_id = _research_unit_id_from_evidence(evidence, index)
            findings.append(
                CompressedFinding(
                    id=f"finding-{index + 1}-{research_unit_id}",
                    research_unit_id=research_unit_id,
                    summary=_bounded_graph_text(
                        evidence.quote_or_summary,
                        GRAPH_FINDING_SUMMARY_MAX_CHARS,
                    ),
                    evidence_ids=[evidence.id],
                    confidence=evidence.confidence,
                )
            )
        return findings


class VerifierNode(ResearchGraphNode):
    phase = ResearchPhase.VERIFY

    async def run(
        self,
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
        previous: ResearchGraphNodeOutput | None = None,
    ) -> ResearchGraphNodeOutput:
        verified_claims = self._verify_claims(state)
        if not verified_claims:
            raise ValueError("research_graph_verifier_output_required")
        self._validate_verified_claim_evidence(verified_claims, state, context)
        verified_state = state.model_copy(
            update={
                "phase": self.phase,
                "verified_claims": verified_claims,
                "claims": _merge_graph_claims(
                    state.claims,
                    _claim_state_from_verified_claims(verified_claims),
                ),
            }
        )
        return ResearchGraphNodeOutput(
            state=verified_state,
            executor_state={
                "node": self.__class__.__name__,
                "verified_claim_count": len(verified_claims),
                "finding_count": len(state.findings),
                "verified_claims": [
                    claim.model_dump(mode="json", exclude_none=True)
                    for claim in verified_claims
                ],
            },
            artifact_events=[
                _verified_claim_artifact_event(claim) for claim in verified_claims
            ],
            failure_experience_summary=(
                previous.failure_experience_summary if previous else None
            ),
        )

    def _verify_claims(self, state: ResearchGraphState) -> list[VerifiedClaim]:
        verified_claims: list[VerifiedClaim] = []
        for index, finding in enumerate(state.findings):
            support_type, evidence_ids, rationale = _verification_for_finding(
                finding,
                state,
            )
            verified_claims.append(
                VerifiedClaim(
                    id=f"claim-{index + 1}-{finding.research_unit_id}",
                    claim=finding.summary,
                    support_type=support_type,
                    evidence_ids=evidence_ids,
                    rationale=rationale,
                    confidence=_verified_claim_confidence(
                        finding.confidence,
                        support_type,
                    ),
                )
            )
        return verified_claims

    def _validate_verified_claim_evidence(
        self,
        verified_claims: list[VerifiedClaim],
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
    ) -> None:
        known_evidence_ids = {evidence.id for evidence in state.evidence}
        for claim in verified_claims:
            raw_evidence_ids = getattr(claim, "evidence_ids", None)
            if not isinstance(raw_evidence_ids, (list, tuple, set)):
                raise ValueError("research_graph_verified_claim_evidence_required")
            evidence_ids = [str(evidence_id).strip() for evidence_id in raw_evidence_ids]
            support_type = str(getattr(claim, "support_type", "") or "")
            if support_type == "invalid_ref":
                if context.evidence_strict_mode == EvidenceStrictMode.BLOCK:
                    raise ValueError(_verifier_invalid_ref_block_error(evidence_ids))
                continue
            if support_type in {"insufficient", "weak"}:
                continue
            if support_type not in {"supported", "contradicted"}:
                raise ValueError("research_graph_verified_claim_evidence_required")
            if not evidence_ids:
                raise ValueError("research_graph_verified_claim_evidence_required")
            if any(
                EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id) is None
                for evidence_id in evidence_ids
            ):
                raise ValueError("research_graph_verified_claim_evidence_required")
            if any(evidence_id not in known_evidence_ids for evidence_id in evidence_ids):
                raise ValueError("research_graph_verified_claim_evidence_required")


class WriterNode(ResearchGraphNode):
    phase = ResearchPhase.WRITE

    async def run(
        self,
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
        previous: ResearchGraphNodeOutput | None = None,
    ) -> ResearchGraphNodeOutput:
        final_summary, final_boxed_answer = self._compose_report(state)
        final_summary = _required_output_text(
            final_summary,
            "research_graph_writer_output_required",
        )
        final_boxed_answer = _required_output_text(
            final_boxed_answer,
            "research_graph_writer_output_required",
        )
        report_event = _final_report_section_artifact_event(
            state,
            final_summary=final_summary,
        )
        validation = validate_report_evidence_refs(
            markdown=final_summary,
            evidence_refs=report_event["payload"].get("evidence_refs"),
            known_evidence_ids={evidence.id for evidence in state.evidence},
        )
        report_event["payload"]["evidence_refs"] = validation.evidence_refs
        if (
            context.evidence_strict_mode == EvidenceStrictMode.BLOCK
            and validation.has_errors
        ):
            raise ValueError(_strict_evidence_block_error(validation))
        evidence_warning_events = _evidence_reference_warning_events(
            state=state,
            validation=validation,
            artifact_type="report_section",
            local_artifact_id=report_event["payload"]["section_id"],
            source_event_type=report_event["payload"]["source_event_type"],
        )
        written_state = state.model_copy(
            update={
                "phase": self.phase,
                "warnings": [
                    *state.warnings,
                    *_evidence_reference_warning_strings(validation),
                ],
                "report_sections": [
                    *state.report_sections,
                    _report_section_from_artifact_event(report_event),
                ],
                "final_summary": final_summary,
                "final_boxed_answer": final_boxed_answer,
            }
        )
        return ResearchGraphNodeOutput(
            state=written_state,
            executor_state={
                "node": self.__class__.__name__,
                "verified_claim_count": len(state.verified_claims),
                "final_summary_length": len(final_summary),
                "evidence_strict_mode": context.evidence_strict_mode.value,
                "evidence_ref_warnings": [
                    event["payload"] for event in evidence_warning_events
                ],
            },
            artifact_events=[*evidence_warning_events, report_event],
            final_summary=final_summary,
            final_boxed_answer=final_boxed_answer,
            failure_experience_summary=(
                previous.failure_experience_summary if previous else None
            ),
        )

    def _compose_report(self, state: ResearchGraphState) -> tuple[str, str]:
        if not state.verified_claims:
            return "", ""
        known_evidence = {evidence.id: evidence for evidence in state.evidence}
        supported_claims = [
            claim
            for claim in state.verified_claims
            if claim.support_type == "supported"
            and self._valid_claim_evidence_refs(claim, known_evidence)
        ]
        conflict_claims = [
            claim
            for claim in state.verified_claims
            if claim.support_type == "contradicted"
            and self._valid_claim_evidence_refs(claim, known_evidence)
        ]
        uncertainty_claims = [
            claim
            for claim in state.verified_claims
            if claim.support_type in {"weak", "insufficient", "invalid_ref"}
            or (
                claim.support_type == "supported"
                and not self._valid_claim_evidence_refs(claim, known_evidence)
            )
        ]
        strongest_supported = max(
            supported_claims,
            key=lambda claim: claim.confidence,
            default=None,
        )
        answer = (
            strongest_supported.claim
            if strongest_supported is not None
            else (
                "The available evidence is insufficient to provide a settled "
                "answer."
            )
        )
        key_findings = (
            [
                self._claim_line(claim, known_evidence)
                for claim in sorted(
                    supported_claims,
                    key=lambda item: item.confidence,
                    reverse=True,
                )
            ]
            or ["- No high-confidence supported findings were verified."]
        )
        evidence_table = self._evidence_table(
            state.evidence,
            [*supported_claims, *conflict_claims],
        )
        uncertainties = (
            [
                self._uncertainty_line(claim)
                for claim in sorted(
                    uncertainty_claims,
                    key=lambda item: item.confidence,
                    reverse=True,
                )
            ]
            or ["- No unresolved weak, insufficient, or invalid-ref claims were found."]
        )
        conflicts = (
            [
                self._claim_line(claim, known_evidence)
                for claim in sorted(
                    conflict_claims,
                    key=lambda item: item.confidence,
                    reverse=True,
                )
            ]
            or ["- No direct evidence conflicts were detected."]
        )
        source_notes = [
            f"- Verification strategy: {self._markdown_cell(state.plan.verification_strategy)}",
            f"- Evidence items reviewed: {len(state.evidence)}.",
            (
                "- Unsupported classifications are not used as settled "
                "high-confidence conclusions."
            ),
        ]
        final_summary = (
            "## Answer\n"
            f"{answer}\n\n"
            "## Key Findings\n"
            f"{chr(10).join(key_findings)}\n\n"
            "## Evidence Table\n"
            f"{evidence_table}\n\n"
            "## Uncertainties\n"
            f"{chr(10).join(uncertainties)}\n\n"
            "## Conflicts\n"
            f"{chr(10).join(conflicts)}\n\n"
            "## Source Notes\n"
            f"{chr(10).join(source_notes)}"
        )
        return _bounded_graph_text(final_summary, 20_000), _bounded_graph_text(
            answer,
            10_000,
        )

    def _valid_claim_evidence_refs(
        self,
        claim: VerifiedClaim,
        known_evidence: dict[str, EvidenceItem],
    ) -> list[str]:
        valid_refs: list[str] = []
        for evidence_id in claim.evidence_ids:
            if (
                EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id)
                and evidence_id in known_evidence
                and evidence_id not in valid_refs
            ):
                valid_refs.append(evidence_id)
        return valid_refs

    def _claim_line(
        self,
        claim: VerifiedClaim,
        known_evidence: dict[str, EvidenceItem],
    ) -> str:
        refs = self._valid_claim_evidence_refs(claim, known_evidence)
        evidence_text = ", ".join(refs)
        rationale = self._markdown_cell(claim.rationale)
        if rationale:
            return f"- {claim.claim} [{evidence_text}] - {rationale}"
        return f"- {claim.claim} [{evidence_text}]"

    def _uncertainty_line(self, claim: VerifiedClaim) -> str:
        rationale = self._markdown_cell(claim.rationale)
        if rationale:
            return f"- {claim.claim} ({claim.support_type}) - {rationale}"
        return f"- {claim.claim} ({claim.support_type})"

    def _evidence_table(
        self,
        evidence_items: list[EvidenceItem],
        claims: list[VerifiedClaim],
    ) -> str:
        refs = {
            evidence_id
            for claim in claims
            for evidence_id in claim.evidence_ids
            if EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id)
        }
        rows = [
            "| Evidence ID | Source | Type | Relevance |",
            "| --- | --- | --- | --- |",
        ]
        for evidence in evidence_items:
            if evidence.id not in refs:
                continue
            rows.append(
                "| "
                f"{self._markdown_cell(evidence.id)} | "
                f"{self._markdown_cell(self._evidence_source_label(evidence))} | "
                f"{self._markdown_cell(evidence.source_type)} | "
                f"{self._markdown_cell(evidence.quote_or_summary, max_chars=240)} |"
            )
        if len(rows) == 2:
            rows.append("| None | No cited evidence | n/a | No supported evidence refs. |")
        return "\n".join(rows)

    def _evidence_source_label(self, evidence: EvidenceItem) -> str:
        return (
            evidence.title
            or evidence.url
            or evidence.document_id
            or evidence.retrieved_source_id
            or "source"
        )

    def _markdown_cell(self, value: Any, *, max_chars: int = 500) -> str:
        text = _bounded_graph_text(str(value or ""), max_chars)
        return " ".join(text.replace("|", "\\|").split())


class ReconcilerNode(ResearchGraphNode):
    phase = ResearchPhase.RECONCILE

    async def run(
        self,
        state: ResearchGraphState,
        context: ResearchGraphExecutionContext,
        previous: ResearchGraphNodeOutput | None = None,
    ) -> ResearchGraphNodeOutput:
        if previous is None:
            raise ValueError("research_graph_writer_output_required")
        final_summary, final_boxed_answer = _validate_graph_final_outputs(
            previous.final_summary,
            previous.final_boxed_answer,
        )
        reconciled_state = state.model_copy(
            update={
                "phase": self.phase,
                "final_summary": final_summary,
                "final_boxed_answer": final_boxed_answer,
            }
        )
        return ResearchGraphNodeOutput(
            state=reconciled_state,
            executor_state={
                "node": self.__class__.__name__,
                "verified_claim_count": len(state.verified_claims),
                "evidence_count": len(state.evidence),
            },
            final_summary=final_summary,
            final_boxed_answer=final_boxed_answer,
            failure_experience_summary=previous.failure_experience_summary,
        )


def default_research_graph_nodes() -> list[ResearchGraphNode]:
    return [
        ScopeNode(),
        PlannerNode(),
        ResearchUnitNode(),
        EvidenceCompressorNode(),
        VerifierNode(),
        WriterNode(),
        ReconcilerNode(),
    ]


def default_langgraph_research_graph_nodes() -> list[ResearchGraphNode]:
    return [
        ScopeNode(),
        PlannerNode(),
        LangGraphResearchUnitNode(),
        EvidenceCompressorNode(),
        VerifierNode(),
        WriterNode(),
        ReconcilerNode(),
    ]


def build_initial_research_graph(
    *,
    task_id: str,
    query: str,
    scenario: str | None = None,
    document_ids: list[str] | None = None,
    upload_scope: dict[str, Any] | None = None,
    source_policy: dict[str, Any] | None = None,
    evidence_strict_mode: Any = EvidenceStrictMode.WARN,
    max_units: int = 4,
) -> ResearchGraphState:
    """Create a deterministic graph seed before model-assisted planning.

    This is intentionally conservative: it creates a bounded plan from the query
    so the runner can expose scope/plan state immediately, while later phases can
    replace this planner with LLM- or LangGraph-backed planning.
    """

    normalized_query = _normalize_query(query)
    bounded_units = max(1, min(int(max_units), 8))
    upload_document_ids = _upload_document_ids(document_ids, upload_scope)
    upload_context = _upload_context_summary(upload_scope, upload_document_ids)
    upload_sources = _upload_sources_from_context(upload_context)
    upload_scope_state = _upload_scope_state_from_context(
        upload_context,
        upload_document_ids,
    )
    policy = _source_policy_from_context(source_policy)
    strict_mode = parse_evidence_strict_mode(evidence_strict_mode)
    scenario_text = _bounded_optional_text(scenario, 1_000)
    brief = ResearchBrief(
        original_query=normalized_query,
        clarified_question=normalized_query,
        scope=_scope_text(
            normalized_query,
            scenario_text,
            upload_document_ids,
            upload_context,
        ),
        success_criteria=[
            "Answer the user's question directly.",
            "Use source-backed claims and preserve source attribution.",
            "Separate confirmed facts from uncertainty or conflicting claims.",
        ],
        required_sources=_required_sources(
            upload_document_ids,
            scenario_text,
            upload_context,
        ),
        constraints=_constraints(upload_document_ids, upload_context),
    )
    units = _research_units_for_query(normalized_query, bounded_units, policy)
    plan = ResearchPlan(
        research_units=units,
        expected_artifacts=[
            "source_candidate",
            "retrieved_source",
            "evidence",
            "finding",
            "verified_claim",
            "entity",
            "timeline_event",
            "verification_result",
            "report_section",
        ],
        verification_strategy=(
            "Cross-check high-impact claims against at least two independent sources "
            "or mark the claim as weak or insufficient."
        ),
    )
    return ResearchGraphState(
        task_id=str(task_id),
        query=normalized_query,
        scenario=scenario_text,
        source_policy=policy,
        evidence_strict_mode=strict_mode,
        upload_scope=upload_scope_state,
        phase=ResearchPhase.PLAN,
        brief=brief,
        plan=plan,
        research_units=units,
        upload_sources=upload_sources,
        context_only_upload_document_ids=list(
            upload_context.get("context_only_document_ids") or []
        ),
        warnings=_initial_graph_warnings(upload_scope_state),
    )


def graph_bootstrap_events(state: ResearchGraphState) -> list[dict[str, Any]]:
    """Return stream events for the initial scope and plan phases."""

    return [
        {
            "event": "research_brief_created",
            "data": {
                "task_id": state.task_id,
                "phase": ResearchPhase.SCOPE.value,
                "brief": state.brief.model_dump(mode="json"),
            },
        },
        {
            "event": "research_plan_created",
            "data": {
                "task_id": state.task_id,
                "phase": ResearchPhase.PLAN.value,
                "plan": state.plan.model_dump(mode="json"),
            },
        },
    ]


def graph_phase_event(
    state: ResearchGraphState,
    phase: ResearchPhase,
) -> dict[str, Any]:
    """Return an additive stream event for serial graph execution progress."""

    return {
        "event": "research_graph_phase",
        "data": {
            "task_id": state.task_id,
            "phase": phase.value,
        },
    }


def graph_checkpoint_event(
    state: ResearchGraphState,
    phase: ResearchPhase,
    node_output: ResearchGraphNodeOutput,
    *,
    status: Literal["running", "completed", "failed"] = "running",
    research_graph_executor: str = "serial",
) -> dict[str, Any]:
    """Return a serializable checkpoint event for Runner durable persistence."""

    terminal = phase == ResearchPhase.COMPLETE and status == "completed"
    executor_state = dict(node_output.executor_state)
    executor_state["research_graph_executor"] = research_graph_executor
    resume_metadata = _resume_metadata_for_checkpoint(
        state,
        phase,
        terminal=terminal,
        research_graph_executor=research_graph_executor,
    )
    executor_state.update(
        {
            "last_completed_node": resume_metadata["last_completed_node"],
            "current_node": resume_metadata["current_node"],
            "completed_unit_ids": list(resume_metadata["completed_unit_ids"]),
            "pending_unit_ids": list(resume_metadata["pending_unit_ids"]),
            "resume_policy": resume_metadata["resume_policy"],
        }
    )
    return {
        "event": "research_graph_checkpoint",
        "data": {
            "task_id": state.task_id,
            "phase": phase.value,
            "status": status,
            "current_research_unit": (
                node_output.current_research_unit or state.current_unit_id
            ),
            "source_ledger": _source_ledger_for_checkpoint(state),
            "evidence_ledger": _evidence_ledger_for_checkpoint(state),
            "executor_state": executor_state,
            "research_graph_executor": research_graph_executor,
            **resume_metadata,
        },
    }


def _resume_metadata_for_checkpoint(
    state: ResearchGraphState,
    phase: ResearchPhase,
    *,
    terminal: bool,
    research_graph_executor: str,
) -> dict[str, Any]:
    completed_unit_ids = [
        unit.id for unit in state.plan.research_units if unit.status == "completed"
    ]
    pending_unit_ids = [
        unit.id for unit in state.plan.research_units if unit.status != "completed"
    ]
    current_node = None if terminal else _next_graph_phase_value(phase)
    if research_graph_executor == "langgraph" and not terminal:
        resume_policy = LANGGRAPH_RESUME_POLICY
        recoverable_reason = LANGGRAPH_RESUME_RECOVERABLE_REASON
    else:
        resume_policy = "terminal" if terminal else "fail_recoverable"
        recoverable_reason = (
            None if terminal else "serial_graph_checkpoint_not_resumable"
        )
    return {
        "last_completed_node": phase.value,
        "current_node": current_node,
        "completed_unit_ids": completed_unit_ids,
        "pending_unit_ids": pending_unit_ids,
        "resume_policy": resume_policy,
        "recoverable_reason": recoverable_reason,
    }


def _next_graph_phase_value(phase: ResearchPhase) -> str | None:
    order = [
        ResearchPhase.SCOPE,
        ResearchPhase.PLAN,
        ResearchPhase.RESEARCH,
        ResearchPhase.COMPRESS,
        ResearchPhase.VERIFY,
        ResearchPhase.WRITE,
        ResearchPhase.RECONCILE,
        ResearchPhase.COMPLETE,
    ]
    try:
        index = order.index(phase)
    except ValueError:
        return None
    if index + 1 >= len(order):
        return None
    return order[index + 1].value


from .research_graph_support import *  # noqa: F403
