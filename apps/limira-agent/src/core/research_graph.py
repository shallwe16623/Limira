"""Research graph contracts for the Limira deep-research workflow.

The current executor is still the compatibility single-agent loop. These models
define the state shape that will let us migrate toward a LangGraph-style graph
one node at a time without breaking runner, SSE, archive, or frontend contracts.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    url: str | None = Field(default=None, max_length=4_000)
    title: str | None = Field(default=None, max_length=1_000)
    source_type: str = Field(default="web", max_length=80)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = Field(min_length=16, max_length=128)
    quote_or_summary: str = Field(min_length=1, max_length=20_000)
    claims: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CompressedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    research_unit_id: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=10_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class VerifiedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    claim: str = Field(min_length=1, max_length=10_000)
    support_type: Literal["supports", "contradicts", "contextual", "weak"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    rationale: str = Field(default="", max_length=10_000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ResearchGraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=120)
    phase: ResearchPhase = ResearchPhase.SCOPE
    brief: ResearchBrief
    plan: ResearchPlan
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=2_000)
    findings: list[CompressedFinding] = Field(default_factory=list, max_length=500)
    verified_claims: list[VerifiedClaim] = Field(default_factory=list, max_length=500)


class ResearchGraphExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    state: ResearchGraphState
    final_summary: str
    final_boxed_answer: str
    failure_experience_summary: Any = None


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
    policy = _source_policy_from_context(source_policy)
    brief = ResearchBrief(
        original_query=normalized_query,
        clarified_question=normalized_query,
        scope=_scope_text(normalized_query, scenario, upload_document_ids, upload_context),
        success_criteria=[
            "Answer the user's question directly.",
            "Use source-backed claims and preserve source attribution.",
            "Separate confirmed facts from uncertainty or conflicting claims.",
        ],
        required_sources=_required_sources(upload_document_ids, scenario, upload_context),
        constraints=_constraints(upload_document_ids, upload_context),
    )
    units = _research_units_for_query(normalized_query, bounded_units, policy)
    plan = ResearchPlan(
        research_units=units,
        expected_artifacts=[
            "evidence",
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
        phase=ResearchPhase.PLAN,
        brief=brief,
        plan=plan,
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
    """Run the feature-flagged serial graph adapter around the legacy executor.

    The graph is intentionally serial and conservative. Scope and plan are
    deterministic state phases, research delegates to the existing orchestrator,
    and verify/write/complete are explicit phase boundaries for later nodes.
    """

    current_state = state
    for phase in (ResearchPhase.SCOPE, ResearchPhase.PLAN, ResearchPhase.RESEARCH):
        current_state = current_state.model_copy(update={"phase": phase})
        await _emit_graph_phase(stream_queue, current_state, phase)

    (
        final_summary,
        final_boxed_answer,
        failure_experience_summary,
    ) = await orchestrator.run_main_agent(
        task_description=graph_task_description(current_state, original_task_description),
        task_file_name=task_file_name,
        task_id=task_id,
        is_final_retry=is_final_retry,
    )
    try:
        final_summary, final_boxed_answer = _validate_graph_final_outputs(
            final_summary,
            final_boxed_answer,
        )
    except ValueError as exc:
        await _emit_graph_error(stream_queue, current_state, exc)
        raise

    for phase in (ResearchPhase.VERIFY, ResearchPhase.WRITE, ResearchPhase.COMPLETE):
        current_state = current_state.model_copy(update={"phase": phase})
        await _emit_graph_phase(stream_queue, current_state, phase)

    return ResearchGraphExecutionResult(
        state=current_state,
        final_summary=final_summary,
        final_boxed_answer=final_boxed_answer,
        failure_experience_summary=failure_experience_summary,
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


def evidence_id_for_source(*, task_id: str, source: str, index: int = 0) -> str:
    digest = hashlib.sha256(f"{task_id}:{source}:{index}".encode("utf-8")).hexdigest()
    return f"EVID-{digest[:12]}"


def _source_policy_text(source_policy: SourcePolicy) -> str:
    return (
        f"prefer_primary_sources={source_policy.prefer_primary_sources}; "
        f"allow_secondary_sources={source_policy.allow_secondary_sources}; "
        f"require_retrieved_at={source_policy.require_retrieved_at}"
    )


async def _emit_graph_phase(
    stream_queue: Any,
    state: ResearchGraphState,
    phase: ResearchPhase,
) -> None:
    if stream_queue is not None:
        await stream_queue.put(graph_phase_event(state, phase))


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
    summary = _required_output_text(final_summary)
    boxed_answer = _required_output_text(final_boxed_answer)
    return summary, boxed_answer


def _required_output_text(value: Any) -> str:
    if value is None:
        raise ValueError("research_graph_final_output_required")
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        raise ValueError("research_graph_final_output_required")
    return text


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
    if not raw_ids:
        return []
    deduped: list[str] = []
    seen: set[str] = set()
    for raw_id in raw_ids[:20]:
        document_id = str(raw_id or "").strip()
        if not document_id or document_id in seen:
            continue
        seen.add(document_id)
        deduped.append(document_id)
    return deduped


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
    }


def _upload_context_document_ids(value: Any) -> list[str]:
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
