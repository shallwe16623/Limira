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

from pydantic import BaseModel, ConfigDict, Field, field_validator


EVIDENCE_ID_FULL_PATTERN = re.compile(r"EVID-(?:\d{3,}|[0-9a-fA-F]{12})")
GRAPH_CONTENT_HASH_CHARS = 32
GRAPH_SOURCE_SUMMARY_MAX_CHARS = 20_000
GRAPH_FINDING_SUMMARY_MAX_CHARS = 10_000


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
    support_type: Literal["supports", "contradicts", "contextual", "weak"]
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    rationale: str = Field(default="", max_length=10_000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            evidence_id = str(item).strip()
            if EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id) is None:
                raise ValueError("verified_claim_evidence_id_invalid")
            normalized.append(evidence_id)
        return normalized


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
    warnings: list[str] = Field(default_factory=list, max_length=200)

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


class ResearchGraphNodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    state: ResearchGraphState
    current_research_unit: str | None = None
    executor_state: dict[str, Any] = Field(default_factory=dict)
    artifact_events: list[dict[str, Any]] = Field(default_factory=list)
    final_summary: str | None = None
    final_boxed_answer: str | None = None
    failure_experience_summary: Any = None


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
        completed_unit_ids: list[str] = []
        current_unit_id = None
        legacy_adapter_calls = 0

        for unit_index, unit in enumerate(state.plan.research_units):
            current_unit_id = unit.id
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
        self._validate_verified_claim_evidence(verified_claims, state)
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
            if not finding.evidence_ids:
                continue
            verified_claims.append(
                VerifiedClaim(
                    id=f"claim-{index + 1}-{finding.research_unit_id}",
                    claim=finding.summary,
                    support_type="supports",
                    evidence_ids=list(finding.evidence_ids),
                    rationale=(
                        "Serial graph verifier linked the finding to retrieved "
                        "research-unit evidence."
                    ),
                    confidence=finding.confidence,
                )
            )
        return verified_claims

    def _validate_verified_claim_evidence(
        self,
        verified_claims: list[VerifiedClaim],
        state: ResearchGraphState,
    ) -> None:
        known_evidence_ids = {evidence.id for evidence in state.evidence}
        for claim in verified_claims:
            raw_evidence_ids = getattr(claim, "evidence_ids", None)
            if not isinstance(raw_evidence_ids, (list, tuple, set)):
                raise ValueError("research_graph_verified_claim_evidence_required")
            evidence_ids = [str(evidence_id).strip() for evidence_id in raw_evidence_ids]
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
        written_state = state.model_copy(
            update={
                "phase": self.phase,
                "report_sections": [
                    *state.report_sections,
                    _report_section_from_artifact_event(report_event),
                ],
            }
        )
        return ResearchGraphNodeOutput(
            state=written_state,
            executor_state={
                "node": self.__class__.__name__,
                "verified_claim_count": len(state.verified_claims),
                "final_summary_length": len(final_summary),
            },
            artifact_events=[report_event],
            final_summary=final_summary,
            final_boxed_answer=final_boxed_answer,
            failure_experience_summary=(
                previous.failure_experience_summary if previous else None
            ),
        )

    def _compose_report(self, state: ResearchGraphState) -> tuple[str, str]:
        if not state.verified_claims:
            return "", ""
        claim_lines = [
            f"- {claim.claim} ({claim.support_type}; evidence: "
            f"{', '.join(claim.evidence_ids)})"
            for claim in state.verified_claims
        ]
        final_summary = (
            "## Answer\n"
            f"{state.brief.clarified_question}\n\n"
            "## Verified Claims\n"
            f"{chr(10).join(claim_lines)}\n\n"
            "## Verification Notes\n"
            f"{state.plan.verification_strategy}"
        )
        return final_summary, state.verified_claims[0].claim


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
        reconciled_state = state.model_copy(update={"phase": self.phase})
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
            "or mark the claim as weak/contextual."
        ),
    )
    return ResearchGraphState(
        task_id=str(task_id),
        query=normalized_query,
        scenario=scenario_text,
        source_policy=policy,
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
            "resume_policy": "terminal" if terminal else "fail_recoverable",
            "recoverable_reason": (
                None if terminal else "serial_graph_checkpoint_not_resumable"
            ),
        },
    }


def graph_error_event(
    state: ResearchGraphState,
    error: Exception,
) -> dict[str, Any]:
    """Return a stream error event for fatal graph execution failures."""

    return {
        "event": "error",
        "data": {
            "task_id": state.task_id,
            "phase": state.phase.value,
            "error": str(error),
        },
    }


async def execute_research_graph(
    *,
    state: ResearchGraphState,
    orchestrator: Any,
    original_task_description: str,
    task_file_name: str | None = None,
    task_id: str = "default_task",
    is_final_retry: bool = False,
    stream_queue: Any = None,
) -> ResearchGraphExecutionResult:
    """Run the feature-flagged serial graph executor.

    The enabled path executes explicit graph nodes. Only `ResearchUnitNode` may
    call the legacy orchestrator, and it does so with a bounded single-unit
    prompt rather than the full graph plan.
    """

    context = ResearchGraphExecutionContext(
        orchestrator=orchestrator,
        original_task_description=original_task_description,
        task_file_name=task_file_name,
        task_id=task_id,
        is_final_retry=is_final_retry,
    )
    current_output = ResearchGraphNodeOutput(state=state)
    error_state = state
    try:
        for node in default_research_graph_nodes():
            active_state = current_output.state.model_copy(update={"phase": node.phase})
            error_state = active_state
            await _emit_graph_phase(stream_queue, active_state, node.phase)
            current_output = await node.run(active_state, context, current_output)
            if current_output.state.phase != node.phase:
                current_output = current_output.model_copy(
                    update={
                        "state": current_output.state.model_copy(
                            update={"phase": node.phase}
                        )
                    }
                )
            error_state = current_output.state
            await _emit_graph_artifact_events(stream_queue, current_output.artifact_events)
            await _emit_graph_checkpoint(
                stream_queue,
                current_output.state,
                node.phase,
                current_output,
            )

        final_summary, final_boxed_answer = _validate_graph_final_outputs(
            current_output.final_summary,
            current_output.final_boxed_answer,
        )
    except Exception as exc:
        await _emit_graph_error(stream_queue, error_state, exc)
        raise

    complete_state = current_output.state.model_copy(
        update={"phase": ResearchPhase.COMPLETE}
    )
    complete_output = ResearchGraphNodeOutput(
        state=complete_state,
        executor_state={
            "node": "Complete",
            "verified_claim_count": len(complete_state.verified_claims),
            "evidence_count": len(complete_state.evidence),
        },
        final_summary=final_summary,
        final_boxed_answer=final_boxed_answer,
        failure_experience_summary=current_output.failure_experience_summary,
    )
    await _emit_graph_phase(stream_queue, complete_state, ResearchPhase.COMPLETE)
    await _emit_graph_checkpoint(
        stream_queue,
        complete_state,
        ResearchPhase.COMPLETE,
        complete_output,
        status="completed",
    )

    return ResearchGraphExecutionResult(
        state=complete_state,
        final_summary=final_summary,
        final_boxed_answer=final_boxed_answer,
        failure_experience_summary=current_output.failure_experience_summary,
    )


def graph_task_description(
    state: ResearchGraphState,
    original_task_description: str,
) -> str:
    """Build the compatibility executor prompt from graph state.

    This keeps the current single-agent executor, but makes it operate from the
    same brief/plan contract that later graph nodes will use.
    """

    unit_lines = []
    for unit in state.plan.research_units:
        queries = "; ".join(unit.search_queries)
        source_policy = _source_policy_text(unit.source_policy)
        unit_lines.append(
            f"- {unit.id}: {unit.question}\n"
            f"  Search queries: {queries}\n"
            f"  Source target: at least {unit.source_policy.min_sources}, "
            f"max {unit.max_sources} sources\n"
            f"  Source policy: {source_policy}"
        )
    success_criteria = "\n".join(f"- {item}" for item in state.brief.success_criteria)
    required_sources = "\n".join(f"- {item}" for item in state.brief.required_sources)
    constraints = "\n".join(f"- {item}" for item in state.brief.constraints)
    expected_artifacts = ", ".join(state.plan.expected_artifacts)
    return (
        f"{str(original_task_description or '').strip()}\n\n"
        "## Limira Research Workflow\n\n"
        "Follow this scoped research graph before writing the final answer. "
        "Use the available search, scrape, local-analysis, and artifact tools. "
        "Treat tool-derived evidence as the source ledger and keep claims "
        "grounded in retrieved sources.\n\n"
        f"### Scope\n{state.brief.scope}\n\n"
        f"### Success Criteria\n{success_criteria}\n\n"
        f"### Required Source Policy\n{required_sources}\n\n"
        f"### Constraints\n{constraints}\n\n"
        "### Research Units\n"
        f"{chr(10).join(unit_lines)}\n\n"
        "### Verification Strategy\n"
        f"{state.plan.verification_strategy}\n\n"
        "### Expected Structured Artifacts\n"
        f"{expected_artifacts}\n\n"
        "### Report Contract\n"
        "Write an answer-first final report. Separate confirmed facts, "
        "uncertain claims, and unresolved contradictions. Preserve source URLs "
        "and dates whenever available."
    )


def research_unit_task_description(
    state: ResearchGraphState,
    unit: ResearchUnit,
    original_task_description: str,
) -> str:
    """Build a bounded legacy-adapter prompt for one graph research unit."""

    return (
        f"{str(original_task_description or '').strip()}\n\n"
        "## Research Unit Node\n\n"
        "Execute only this research unit. Do not write the final report. "
        "Return concise source-grounded findings for the compressor node.\n\n"
        f"Unit ID: {unit.id}\n"
        f"Question: {unit.question}\n"
        f"Search queries: {'; '.join(unit.search_queries)}\n"
        f"Source target: at least {unit.source_policy.min_sources}, "
        f"max {unit.max_sources} sources\n"
        f"Source policy: {_source_policy_text(unit.source_policy)}\n\n"
        f"Scope: {state.brief.scope}\n"
        f"Constraints: {'; '.join(state.brief.constraints)}"
    )


def evidence_id_for_source(*, task_id: str, source: str, index: int = 0) -> str:
    digest = hashlib.sha256(f"{task_id}:{source}:{index}".encode("utf-8")).hexdigest()
    return f"EVID-{digest[:12]}"


def _stable_prefixed_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def retrieved_source_id_for_source(
    *, task_id: str, source: str, index: int = 0
) -> str:
    digest = hashlib.sha256(
        f"{task_id}:retrieved:{source}:{index}".encode("utf-8")
    ).hexdigest()
    return f"RSRC-{digest[:12]}"


def _source_policy_text(source_policy: SourcePolicy) -> str:
    return (
        f"prefer_primary_sources={source_policy.prefer_primary_sources}; "
        f"allow_secondary_sources={source_policy.allow_secondary_sources}; "
        f"require_retrieved_at={source_policy.require_retrieved_at}; "
        f"prefer_uploaded_documents={source_policy.prefer_uploaded_documents}; "
        f"prefer_scenario_sources={source_policy.prefer_scenario_sources}"
    )


def _normalized_retriever_name(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


async def _emit_graph_phase(
    stream_queue: Any,
    state: ResearchGraphState,
    phase: ResearchPhase,
) -> None:
    if stream_queue is not None:
        await stream_queue.put(graph_phase_event(state, phase))


async def _emit_graph_checkpoint(
    stream_queue: Any,
    state: ResearchGraphState,
    phase: ResearchPhase,
    node_output: ResearchGraphNodeOutput,
    *,
    status: Literal["running", "completed", "failed"] = "running",
    research_graph_executor: str = "serial",
) -> None:
    if stream_queue is not None:
        await stream_queue.put(
            graph_checkpoint_event(
                state,
                phase,
                node_output,
                status=status,
                research_graph_executor=research_graph_executor,
            )
        )


async def _emit_graph_artifact_events(
    stream_queue: Any,
    artifact_events: list[dict[str, Any]],
) -> None:
    if stream_queue is None:
        return
    for event in artifact_events:
        await stream_queue.put(event)


async def _emit_graph_error(
    stream_queue: Any,
    state: ResearchGraphState,
    error: Exception,
) -> None:
    if stream_queue is not None:
        await stream_queue.put(graph_error_event(state, error))


def _validate_graph_final_outputs(
    final_summary: Any,
    final_boxed_answer: Any,
) -> tuple[str, str]:
    summary = _required_output_text(
        final_summary,
        "research_graph_final_output_required",
    )
    boxed_answer = _required_output_text(
        final_boxed_answer,
        "research_graph_final_output_required",
    )
    return summary, boxed_answer


def _required_output_text(value: Any, error_name: str) -> str:
    if value is None:
        raise ValueError(error_name)
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        raise ValueError(error_name)
    return text


def _research_unit_id_from_evidence(evidence: EvidenceItem, index: int) -> str:
    if evidence.url and evidence.url.startswith("graph://"):
        value = evidence.url.removeprefix("graph://").strip()
        if value:
            return value
    return f"unit-{index + 1}"


def _source_ledger_for_checkpoint(state: ResearchGraphState) -> list[dict[str, Any]]:
    unit_entries = [
        {
            "unit_id": unit.id,
            "status": unit.status,
            "search_queries": list(unit.search_queries),
            "source_policy": unit.source_policy.model_dump(mode="json"),
        }
        for unit in state.plan.research_units
    ]
    retrieved_entries = [
        {
            "retrieved_source_id": source.id,
            "source_type": source.source_type,
            "url": source.url,
            "document_id": source.document_id,
            "chunk_id": source.chunk_id,
            "content_hash": source.content_hash,
            "retrieved_at": source.retrieved_at.isoformat(),
            "tool_name": source.tool_name,
            "confidence": source.confidence,
        }
        for source in state.retrieved_sources
    ]
    return unit_entries + retrieved_entries


def _evidence_ledger_for_checkpoint(state: ResearchGraphState) -> list[dict[str, Any]]:
    evidence_entries = [
        {
            "id": evidence.id,
            "retrieved_source_id": evidence.retrieved_source_id,
            "source_type": evidence.source_type,
            "url": evidence.url,
            "document_id": evidence.document_id,
            "chunk_id": evidence.chunk_id,
            "title": evidence.title,
            "content_hash": evidence.content_hash,
            "retrieved_at": evidence.retrieved_at.isoformat(),
            "confidence": evidence.confidence,
            "tool_name": evidence.tool_name,
        }
        for evidence in state.evidence
    ]
    finding_entries = [
        {
            "finding_id": finding.id,
            "evidence_ids": list(finding.evidence_ids),
            "confidence": finding.confidence,
        }
        for finding in state.findings
    ]
    claim_entries = [
        {
            "claim_id": claim.id,
            "support_type": claim.support_type,
            "evidence_ids": list(claim.evidence_ids),
            "confidence": claim.confidence,
        }
        for claim in state.verified_claims
    ]
    return evidence_entries + finding_entries + claim_entries


def _upload_source_candidate_artifact_event(source: UploadedDocumentSource) -> dict[str, Any]:
    return {
        "event": "source_candidate_collected",
        "type": "source_candidate_collected",
        "payload": {
            "candidate_id": source.candidate_id,
            "title": _upload_source_title(source),
            "summary": source.snippet or source.text,
            "snippet": source.snippet or source.text,
            "source_type": source.source_type,
            "source_state": "source_candidate",
            "source_content_state": source.source_content_state,
            "retrieval_status": source.retrieval_status,
            "document_id": source.document_id,
            "attached_document_id": source.attached_document_id,
            "chunk_id": source.chunk_id,
            "filename": source.filename,
            "retrieved_at": source.retrieved_at.isoformat(),
            "content_hash": source.content_hash,
            "confidence": 0.55,
            "candidate": True,
            "source_event_type": "research_graph_upload_source_provider",
        },
    }


def _retrieved_source_artifact_event(source: RetrievedSource) -> dict[str, Any]:
    return {
        "event": "retrieved_source_collected",
        "type": "retrieved_source_collected",
        "payload": {
            "retrieved_source_id": source.id,
            "title": source.title,
            "url": source.url,
            "document_id": source.document_id,
            "chunk_id": source.chunk_id,
            "summary": source.quote_or_summary,
            "quote_or_summary": source.quote_or_summary,
            "source_type": source.source_type,
            "source_state": "retrieved_source",
            "source_content_state": "content_bearing",
            "retrieval_status": "retrieved",
            "retrieved_at": source.retrieved_at.isoformat(),
            "content_hash": source.content_hash,
            "tool_name": source.tool_name,
            "confidence": source.confidence,
            "candidate": False,
            "source_event_type": "research_graph",
        },
    }


def _evidence_artifact_event(evidence: EvidenceItem) -> dict[str, Any]:
    return {
        "event": "evidence_collected",
        "type": "evidence_collected",
        "payload": {
            "evidence_id": evidence.id,
            "retrieved_source_id": evidence.retrieved_source_id,
            "title": evidence.title,
            "url": evidence.url,
            "document_id": evidence.document_id,
            "chunk_id": evidence.chunk_id,
            "summary": evidence.quote_or_summary,
            "quote_or_summary": evidence.quote_or_summary,
            "source_type": evidence.source_type,
            "source_state": "evidence_item",
            "source_content_state": "content_bearing",
            "retrieval_status": "retrieved",
            "retrieved_at": evidence.retrieved_at.isoformat(),
            "content_hash": evidence.content_hash,
            "tool_name": evidence.tool_name,
            "confidence": evidence.confidence,
            "candidate": False,
            "evidence_refs": [evidence.id],
            "source_event_type": "research_graph",
        },
    }


def _finding_artifact_event(finding: CompressedFinding) -> dict[str, Any]:
    return {
        "event": "finding_collected",
        "type": "finding_collected",
        "payload": {
            "finding_id": finding.id,
            "research_unit_id": finding.research_unit_id,
            "summary": finding.summary,
            "evidence_ids": list(finding.evidence_ids),
            "evidence_refs": list(finding.evidence_ids),
            "confidence": finding.confidence,
            "source_event_type": "research_graph",
        },
    }


def _verified_claim_artifact_event(claim: VerifiedClaim) -> dict[str, Any]:
    return {
        "event": "verified_claim_collected",
        "type": "verified_claim_collected",
        "payload": {
            "claim_id": claim.id,
            "claim": claim.claim,
            "support_type": claim.support_type,
            "evidence_ids": list(claim.evidence_ids),
            "evidence_refs": list(claim.evidence_ids),
            "rationale": claim.rationale,
            "confidence": claim.confidence,
            "source_event_type": "research_graph",
        },
    }


def _final_report_section_artifact_event(
    state: ResearchGraphState,
    *,
    final_summary: str,
) -> dict[str, Any]:
    evidence_refs = list(
        dict.fromkeys(
            evidence_id
            for claim in state.verified_claims
            for evidence_id in claim.evidence_ids
        )
    )
    return {
        "event": "report_section_generated",
        "type": "report_section_generated",
        "payload": {
            "section_id": "REPORT-GRAPH-FINAL",
            "title": "Final graph report",
            "markdown": final_summary,
            "content": final_summary,
            "evidence_refs": evidence_refs,
            "source_event_type": "research_graph",
        },
    }


def _source_candidate_artifact_event(candidate: SourceCandidate) -> dict[str, Any]:
    retrieved_at = (
        candidate.retrieved_at.isoformat() if candidate.retrieved_at else None
    )
    return {
        "event": "source_candidate_collected",
        "type": "source_candidate_collected",
        "payload": {
            "candidate_id": candidate.candidate_id,
            "title": candidate.title,
            "summary": candidate.summary,
            "snippet": candidate.summary,
            "source_type": candidate.source_type,
            "source_state": candidate.source_state,
            "source_content_state": candidate.source_content_state,
            "retrieval_status": candidate.retrieval_status,
            "url": candidate.url,
            "document_id": candidate.document_id,
            "attached_document_id": candidate.attached_document_id,
            "chunk_id": candidate.chunk_id,
            "filename": candidate.filename,
            "retrieved_at": retrieved_at,
            "content_hash": candidate.content_hash,
            "confidence": candidate.confidence,
            "candidate": True,
            "source_event_type": candidate.source_event_type,
        },
    }


def _source_candidate_from_upload(source: UploadedDocumentSource) -> SourceCandidate:
    return SourceCandidate(
        candidate_id=source.candidate_id,
        title=_upload_source_title(source),
        summary=source.snippet or _bounded_graph_text(source.text, 2_000),
        source_type=source.source_type,
        source_state="source_candidate",
        source_content_state=source.source_content_state,
        retrieval_status=source.retrieval_status,
        document_id=source.document_id,
        attached_document_id=source.attached_document_id,
        chunk_id=source.chunk_id,
        filename=source.filename,
        retrieved_at=source.retrieved_at,
        content_hash=source.content_hash,
        confidence=0.55,
        source_event_type="research_graph_upload_source_provider",
    )


def _snippet_source_candidate_for_unit(
    state: ResearchGraphState,
    unit: ResearchUnit,
    unit_index: int,
) -> SourceCandidate:
    query = unit.search_queries[0] if unit.search_queries else unit.question
    candidate_id = _stable_prefixed_id(
        "SRC-SNIP",
        f"{state.task_id}:{unit.id}:{query}:{unit_index}",
    )
    return SourceCandidate(
        candidate_id=candidate_id,
        title=f"Search snippet candidate for {unit.id}",
        summary=f"Snippet-only candidate from query: {query}",
        source_type="web_search",
        source_state="source_candidate",
        source_content_state="snippet_only",
        retrieval_status="candidate_only",
        url=f"search://{unit.id}/{unit_index + 1}",
        confidence=0.25,
        source_event_type="research_graph_search",
    )


def _legacy_adapter_source_candidate_for_unit(
    state: ResearchGraphState,
    unit: ResearchUnit,
    unit_index: int,
) -> SourceCandidate:
    candidate_id = _stable_prefixed_id(
        "SRC-LEGACY",
        f"{state.task_id}:{unit.id}:legacy_agent_adapter:{unit_index}",
    )
    return SourceCandidate(
        candidate_id=candidate_id,
        title=f"Legacy adapter fallback for {unit.id}",
        summary=unit.question,
        source_type="legacy_agent_adapter",
        source_state="source_candidate",
        source_content_state="content_bearing",
        retrieval_status="pending",
        url=f"graph://{unit.id}",
        confidence=0.4,
        source_event_type="research_graph_legacy_adapter",
    )


def _retrieved_source_from_upload_for_unit(
    *,
    state: ResearchGraphState,
    unit: ResearchUnit,
    upload_source: UploadedDocumentSource,
    index: int,
) -> RetrievedSource:
    return RetrievedSource(
        id=retrieved_source_id_for_source(
            task_id=state.task_id,
            source=(
                f"langgraph://{unit.id}/upload/"
                f"{upload_source.document_id}/{upload_source.chunk_id}"
            ),
            index=index,
        ),
        document_id=upload_source.document_id,
        chunk_id=upload_source.chunk_id,
        title=_upload_source_title(upload_source),
        source_type="limira_upload",
        retrieved_at=upload_source.retrieved_at,
        content_hash=upload_source.content_hash,
        quote_or_summary=upload_source.text,
        tool_name="uploaded_document_search",
        confidence=0.8,
    )


def _evidence_from_retrieved_source(
    *,
    state: ResearchGraphState,
    unit: ResearchUnit,
    retrieved_source: RetrievedSource,
    index: int,
) -> EvidenceItem:
    source_key = (
        f"langgraph://{unit.id}/evidence/"
        f"{retrieved_source.source_type}/{retrieved_source.id}"
    )
    return EvidenceItem(
        id=evidence_id_for_source(
            task_id=state.task_id,
            source=source_key,
            index=index,
        ),
        retrieved_source_id=retrieved_source.id,
        url=retrieved_source.url,
        document_id=retrieved_source.document_id,
        chunk_id=retrieved_source.chunk_id,
        title=retrieved_source.title,
        source_type=retrieved_source.source_type,
        retrieved_at=retrieved_source.retrieved_at,
        content_hash=retrieved_source.content_hash,
        quote_or_summary=retrieved_source.quote_or_summary,
        claims=[retrieved_source.quote_or_summary],
        confidence=retrieved_source.confidence,
        tool_name=retrieved_source.tool_name,
    )


def _upload_source_for_candidate(
    candidate: SourceCandidate,
    upload_sources: list[UploadedDocumentSource],
) -> UploadedDocumentSource | None:
    for source in upload_sources:
        if (
            source.document_id == candidate.document_id
            and source.chunk_id == candidate.chunk_id
        ):
            return source
    return None


def _upload_source_matches_unit(
    source: UploadedDocumentSource,
    unit: ResearchUnit,
) -> bool:
    terms = _unit_search_terms(unit)
    if not terms:
        return True
    corpus = " ".join(
        item
        for item in (
            source.text,
            source.snippet or "",
            source.filename or "",
            source.document_id,
        )
        if item
    ).lower()
    return any(term in corpus for term in terms)


def _unit_search_terms(unit: ResearchUnit) -> list[str]:
    raw_text = " ".join([unit.question, *unit.search_queries])
    terms = re.findall(r"[a-z0-9][a-z0-9_-]{3,}", raw_text.lower())
    stop_words = {
        "about",
        "against",
        "changed",
        "claim",
        "claims",
        "company",
        "context",
        "core",
        "facts",
        "official",
        "primary",
        "question",
        "recent",
        "report",
        "reports",
        "research",
        "source",
        "sources",
        "timeline",
        "update",
        "verification",
        "verify",
        "what",
        "which",
        "with",
    }
    return [term for term in dict.fromkeys(terms) if term not in stop_words][:20]


def _claim_state_from_findings(findings: list[CompressedFinding]) -> list[ResearchClaim]:
    return [
        ResearchClaim(
            id=f"claim-state-{finding.id}",
            claim=finding.summary,
            evidence_ids=list(finding.evidence_ids),
            source="finding",
            confidence=finding.confidence,
        )
        for finding in findings
    ]


def _claim_state_from_verified_claims(
    verified_claims: list[VerifiedClaim],
) -> list[ResearchClaim]:
    return [
        ResearchClaim(
            id=claim.id,
            claim=claim.claim,
            evidence_ids=list(claim.evidence_ids),
            source="verified_claim",
            support_type=claim.support_type,
            confidence=claim.confidence,
        )
        for claim in verified_claims
    ]


def _merge_graph_claims(
    existing: list[ResearchClaim],
    incoming: list[ResearchClaim],
) -> list[ResearchClaim]:
    merged: list[ResearchClaim] = []
    seen: set[tuple[str, str]] = set()
    for claim in [*existing, *incoming]:
        key = (claim.source, claim.id)
        if key in seen:
            continue
        seen.add(key)
        merged.append(claim)
    return merged[:500]


def _report_section_from_artifact_event(event: dict[str, Any]) -> ReportSection:
    payload = event.get("payload") if isinstance(event, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    return ReportSection(
        section_id=str(payload.get("section_id") or "REPORT-GRAPH-FINAL").strip(),
        title=str(payload.get("title") or "Final graph report").strip(),
        markdown=_bounded_graph_text(
            str(payload.get("markdown") or payload.get("content") or "").strip(),
            20_000,
        ),
        evidence_refs=[
            str(item).strip()
            for item in payload.get("evidence_refs") or []
            if str(item).strip()
        ][:200],
        source_event_type=_bounded_optional_text(
            payload.get("source_event_type"),
            120,
        ),
    )


def _normalize_query(query: str) -> str:
    normalized = re.sub(r"\s+", " ", str(query or "")).strip()
    if not normalized:
        raise ValueError("query_required")
    return normalized


def _scope_text(
    query: str,
    scenario: str | None,
    upload_document_ids: list[str] | None = None,
    upload_context: dict[str, Any] | None = None,
) -> str:
    upload_count = len(upload_document_ids or [])
    retrieval_status = str((upload_context or {}).get("retrieval_status") or "").strip()
    upload_note = (
        f" Use {upload_count} attached upload source(s) as scoped user-provided context."
        if upload_count
        else ""
    )
    if upload_note and retrieval_status:
        upload_note = f"{upload_note} Upload retrieval status: {retrieval_status}."
    if scenario:
        return f"Research scenario '{scenario}' for: {query}.{upload_note}"
    return f"Research the user question end to end: {query}.{upload_note}"


def _research_units_for_query(
    query: str,
    max_units: int,
    source_policy: SourcePolicy,
) -> list[ResearchUnit]:
    base_queries = _query_variants(query)
    unit_specs = [
        (
            "background",
            f"What are the core facts and context for: {query}",
            base_queries[:2],
        ),
        (
            "primary-sources",
            f"What do authoritative or primary sources say about: {query}",
            [f"{query} official source", f"{query} government filing"],
        ),
        (
            "recent-updates",
            f"What changed recently and what dates matter for: {query}",
            [f"{query} latest update", f"{query} timeline"],
        ),
        (
            "verification",
            f"Which claims about this topic need verification or contradiction checks: {query}",
            [f"{query} controversy", f"{query} conflicting reports"],
        ),
    ]
    return [
        ResearchUnit(
            id=f"unit-{index + 1}-{unit_id}",
            question=question,
            search_queries=_dedupe_queries(queries or [query]),
            source_policy=source_policy.model_copy(),
            max_sources=6,
        )
        for index, (unit_id, question, queries) in enumerate(unit_specs[:max_units])
    ]


def _required_sources(
    upload_document_ids: list[str],
    scenario: str | None,
    upload_context: dict[str, Any] | None = None,
) -> list[str]:
    required_sources = [
        "Primary or official sources when available.",
        "Recent secondary reporting for context when primary sources are incomplete.",
    ]
    if scenario:
        required_sources.append(
            f"Apply the '{scenario}' scenario source priorities when selecting sources."
        )
    if upload_document_ids:
        required_sources.append(
            "Use attached upload documents as user-provided source candidates before "
            "web-only secondary context."
        )
    retrieved_ids = (upload_context or {}).get("retrieved_document_ids") or []
    if retrieved_ids:
        required_sources.append(
            "Use retrieved upload text for document IDs: "
            f"{', '.join(str(item) for item in retrieved_ids)}."
        )
    source_payloads = (upload_context or {}).get("source_payloads") or []
    if source_payloads:
        required_sources.append(
            "Use these retrieved upload excerpts as source text before web-only context:"
        )
        for payload in source_payloads[:8]:
            document_id = str(payload.get("document_id") or "unknown").strip()
            filename = str(payload.get("filename") or document_id).strip()
            retrieved_at = str(payload.get("retrieved_at") or "").strip()
            content_hash = str(payload.get("content_hash") or "").strip()
            text = str(payload.get("text") or payload.get("snippet") or "").strip()
            if not text:
                continue
            metadata = "; ".join(
                item
                for item in (
                    f"file={filename}",
                    f"retrieved_at={retrieved_at}" if retrieved_at else "",
                    f"content_hash={content_hash}" if content_hash else "",
                )
                if item
            )
            required_sources.append(f"Upload {document_id} ({metadata}): {text}")
    return required_sources


def _constraints(
    upload_document_ids: list[str],
    upload_context: dict[str, Any] | None = None,
) -> list[str]:
    constraints = [
        "Do not invent citations.",
        "Prefer evidence that can be archived and later rechecked.",
    ]
    if upload_document_ids:
        constraints.append(
            "Do not claim uploaded document facts were verified unless the retrieved "
            "upload chunk or parsed document content is cited."
        )
    context_only_ids = (upload_context or {}).get("context_only_document_ids") or []
    if context_only_ids:
        constraints.append(
            "Treat context-only upload IDs as attachment metadata only until text is "
            f"retrieved: {', '.join(str(item) for item in context_only_ids)}."
        )
    return constraints


def _source_policy_from_context(source_policy: dict[str, Any] | None) -> SourcePolicy:
    if not isinstance(source_policy, dict):
        return SourcePolicy()
    candidate = {
        key: source_policy[key]
        for key in (
            "min_sources",
            "prefer_primary_sources",
            "allow_secondary_sources",
            "require_retrieved_at",
            "prefer_uploaded_documents",
            "prefer_scenario_sources",
        )
        if key in source_policy
    }
    try:
        return SourcePolicy.model_validate(candidate)
    except Exception:
        return SourcePolicy()


def _upload_document_ids(
    document_ids: list[str] | None,
    upload_scope: dict[str, Any] | None,
) -> list[str]:
    raw_ids = document_ids
    if not raw_ids and isinstance(upload_scope, dict):
        value = upload_scope.get("document_ids")
        if isinstance(value, list):
            raw_ids = value
    return _normalized_upload_document_ids(raw_ids)


def _upload_scope_state_from_context(
    upload_context: dict[str, Any],
    upload_document_ids: list[str],
) -> ResearchUploadScope:
    payloads = [
        payload
        for payload in upload_context.get("source_payloads") or []
        if isinstance(payload, dict)
    ]
    return ResearchUploadScope(
        document_count=len(upload_document_ids),
        retrieval_status=_bounded_optional_text(
            upload_context.get("retrieval_status"),
            80,
        ),
        retrieved_document_ids=[
            str(item).strip()
            for item in upload_context.get("retrieved_document_ids") or []
            if str(item).strip()
        ][:500],
        context_only_document_ids=[
            str(item).strip()
            for item in upload_context.get("context_only_document_ids") or []
            if str(item).strip()
        ][:500],
        source_payload_count=len(payloads),
        source_payload_refs=[
            UploadScopeSourceRef(
                document_id=str(payload.get("document_id") or "").strip(),
                attached_document_id=_bounded_optional_text(
                    payload.get("attached_document_id"),
                    120,
                ),
                chunk_id=_bounded_optional_text(payload.get("chunk_id"), 120),
                filename=_bounded_optional_text(payload.get("filename"), 1_000),
                content_hash=_short_optional_content_hash(payload.get("content_hash")),
                text_char_count=_safe_int(payload.get("text_char_count")),
                text_truncated=bool(payload.get("text_truncated")),
            )
            for payload in payloads[:100]
            if str(payload.get("document_id") or "").strip()
        ],
    )


def _initial_graph_warnings(upload_scope: ResearchUploadScope) -> list[str]:
    if not upload_scope.context_only_document_ids:
        return []
    context_only_ids = ", ".join(upload_scope.context_only_document_ids[:20])
    return [f"context_only_upload_documents={context_only_ids}"]


def _upload_sources_from_context(
    upload_context: dict[str, Any],
) -> list[UploadedDocumentSource]:
    sources: list[UploadedDocumentSource] = []
    for index, payload in enumerate(upload_context.get("source_payloads") or []):
        if not isinstance(payload, dict):
            continue
        source = _upload_source_from_payload(payload, index)
        if source is not None:
            sources.append(source)
    return sources


def _upload_source_from_payload(
    payload: dict[str, Any],
    index: int,
) -> UploadedDocumentSource | None:
    document_id = str(payload.get("document_id") or "").strip()
    text = str(payload.get("text") or payload.get("snippet") or "").strip()
    if not document_id or not text:
        return None
    source_content_state = str(
        payload.get("source_content_state") or "content_bearing"
    ).strip()
    retrieval_status = str(payload.get("retrieval_status") or "retrieved").strip()
    if source_content_state != "content_bearing" or retrieval_status != "retrieved":
        return None
    attached_document_id = str(payload.get("attached_document_id") or "").strip()
    content_hash = str(payload.get("content_hash") or "").strip()
    if len(content_hash) < 16:
        content_hash = _short_content_hash(text)
    else:
        content_hash = _short_content_hash_from_digest(content_hash)
    chunk_id = str(payload.get("chunk_id") or "").strip()
    if not chunk_id:
        chunk_id = _upload_source_chunk_id(document_id, attached_document_id, index)
    candidate_id = str(payload.get("candidate_id") or "").strip()
    if not candidate_id:
        candidate_id = _upload_source_candidate_id(document_id, attached_document_id, index)
    text_char_count = payload.get("text_char_count")
    try:
        text_char_count_value = int(text_char_count) if text_char_count else len(text)
    except (TypeError, ValueError):
        text_char_count_value = len(text)
    return UploadedDocumentSource(
        candidate_id=candidate_id,
        document_id=document_id,
        attached_document_id=attached_document_id or None,
        chunk_id=chunk_id,
        filename=str(payload.get("filename") or document_id).strip() or None,
        retrieved_at=_parse_upload_retrieved_at(payload.get("retrieved_at")),
        content_hash=content_hash,
        text=text,
        snippet=str(payload.get("snippet") or text).strip()[:2_000],
        text_char_count=text_char_count_value,
        text_truncated=bool(payload.get("text_truncated")),
    )


def _short_content_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[
        :GRAPH_CONTENT_HASH_CHARS
    ]


def _short_content_hash_from_digest(value: str) -> str:
    text = str(value or "").strip()
    if len(text) < 16:
        return _short_content_hash(text)
    return text[:GRAPH_CONTENT_HASH_CHARS]


def _short_optional_content_hash(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _short_content_hash_from_digest(text)


def _bounded_graph_text(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _bounded_optional_text(value: Any, max_chars: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_chars]


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return integer if integer >= 0 else None


def _upload_source_chunk_id(
    document_id: str,
    attached_document_id: str | None,
    index: int,
) -> str:
    digest = hashlib.sha256(
        f"{document_id}:{attached_document_id or ''}:{index}".encode("utf-8")
    ).hexdigest()[:12]
    return f"UPLOAD-CHUNK-{digest}"


def _upload_source_candidate_id(
    document_id: str,
    attached_document_id: str | None,
    index: int,
) -> str:
    digest = hashlib.sha256(
        f"candidate:{document_id}:{attached_document_id or ''}:{index}".encode("utf-8")
    ).hexdigest()[:12]
    return f"SRC-UPLOAD-{digest}"


def _upload_source_title(source: UploadedDocumentSource) -> str:
    filename = (source.filename or source.document_id).strip()
    return f"Uploaded document: {filename} ({source.chunk_id})"


def _parse_upload_retrieved_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _upload_context_summary(
    upload_scope: dict[str, Any] | None,
    upload_document_ids: list[str],
) -> dict[str, Any]:
    if not isinstance(upload_scope, dict):
        return {}
    retrieved_ids = _upload_context_document_ids(
        upload_scope.get("retrieved_document_ids")
    )
    context_only_ids = _upload_context_document_ids(
        upload_scope.get("context_only_document_ids")
    )
    status = str(upload_scope.get("retrieval_status") or "").strip()
    if not status and upload_document_ids:
        if retrieved_ids and context_only_ids:
            status = "partial"
        elif retrieved_ids:
            status = "retrieved"
        else:
            status = "context_only"
    return {
        "retrieval_status": status,
        "retrieved_document_ids": retrieved_ids,
        "context_only_document_ids": context_only_ids,
        "source_payloads": _upload_context_source_payloads(
            upload_scope.get("source_payloads")
        ),
    }


def _upload_context_source_payloads(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    payloads: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("document_id") or "").strip()
        text = str(item.get("text") or item.get("snippet") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        dedupe_key = (document_id, chunk_id, text)
        if not document_id or not text or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        payloads.append(
            {
                "candidate_id": str(item.get("candidate_id") or "").strip(),
                "document_id": document_id,
                "attached_document_id": str(
                    item.get("attached_document_id") or ""
                ).strip(),
                "filename": str(item.get("filename") or document_id).strip(),
                "source_type": str(item.get("source_type") or "").strip(),
                "source_content_state": str(
                    item.get("source_content_state") or ""
                ).strip(),
                "retrieval_status": str(item.get("retrieval_status") or "").strip(),
                "retrieved_at": str(item.get("retrieved_at") or "").strip(),
                "content_hash": str(item.get("content_hash") or "").strip(),
                "chunk_id": chunk_id,
                "snippet": str(item.get("snippet") or text).strip()[:800],
                "text": text[:4000],
                "text_char_count": item.get("text_char_count"),
                "text_truncated": bool(item.get("text_truncated")),
            }
        )
    return payloads


def _upload_context_document_ids(value: Any) -> list[str]:
    return _normalized_upload_document_ids(value)


def _normalized_upload_document_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for raw_id in value[:20]:
        document_id = str(raw_id or "").strip()
        if not document_id or document_id in seen:
            continue
        seen.add(document_id)
        ids.append(document_id)
    return ids


def _query_variants(query: str) -> list[str]:
    quoted = f'"{query}"' if len(query) < 180 else query
    return _dedupe_queries([query, quoted, f"{query} source", f"{query} report"])


def _dedupe_queries(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for query in queries:
        normalized = re.sub(r"\s+", " ", str(query or "")).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped or ["research query"]
