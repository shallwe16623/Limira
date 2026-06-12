"""Support functions for the Limira research graph contract."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Literal

from .research_graph import (
    CompressedFinding,
    EVIDENCE_ID_FULL_PATTERN,
    EVIDENCE_ID_TOKEN_PATTERN,
    GRAPH_CONTENT_HASH_CHARS,
    GRAPH_FINDING_SUMMARY_MAX_CHARS,
    GRAPH_SOURCE_SUMMARY_MAX_CHARS,
    LANGGRAPH_RESUME_POLICY,
    VERIFIER_NEGATIVE_MARKERS,
    VERIFIER_POSITIVE_MARKERS,
    EvidenceItem,
    EvidenceReferenceValidation,
    EvidenceStrictMode,
    ReportSection,
    ResearchBrief,
    ResearchClaim,
    ResearchGraphExecutionContext,
    ResearchGraphExecutionResult,
    ResearchGraphNodeOutput,
    ResearchGraphState,
    ResearchPhase,
    ResearchPlan,
    ResearchUnit,
    ResearchUploadScope,
    RetrievedSource,
    SourceCandidate,
    SourcePolicy,
    UploadScopeSourceRef,
    UploadedDocumentSource,
    VerifiedClaim,
    build_initial_research_graph,
    default_research_graph_nodes,
    graph_checkpoint_event,
    graph_phase_event,
    parse_evidence_strict_mode,
    _next_graph_phase_value,
)

__all__ = [
    "apply_langgraph_resume_checkpoint",
    "_is_langgraph_resume_checkpoint",
    "_checkpoint_phase",
    "_checkpoint_string_list",
    "_checkpoint_unit_statuses",
    "_checkpoint_source_candidates",
    "_checkpoint_retrieved_sources",
    "_checkpoint_evidence",
    "_checkpoint_findings",
    "_checkpoint_verified_claims",
    "_checkpoint_report_sections",
    "_checkpoint_final_outputs",
    "_checkpoint_datetime",
    "_checkpoint_float",
    "_optional_checkpoint_text",
    "graph_error_event",
    "execute_research_graph",
    "graph_task_description",
    "research_unit_task_description",
    "evidence_id_for_source",
    "_stable_prefixed_id",
    "retrieved_source_id_for_source",
    "_source_policy_text",
    "_normalized_retriever_name",
    "_emit_graph_phase",
    "_emit_graph_checkpoint",
    "_emit_graph_artifact_events",
    "_emit_graph_error",
    "_validate_graph_final_outputs",
    "_required_output_text",
    "_verification_for_finding",
    "_verified_claim_confidence",
    "_linked_evidence_is_contradicted",
    "_evidence_polarity",
    "_finding_mentions_source_candidate",
    "_dedupe_preserve_order",
    "_verifier_invalid_ref_block_error",
    "_research_unit_id_from_evidence",
    "_source_ledger_for_checkpoint",
    "_evidence_ledger_for_checkpoint",
    "_upload_source_candidate_artifact_event",
    "_retrieved_source_artifact_event",
    "_evidence_artifact_event",
    "_finding_artifact_event",
    "_verified_claim_artifact_event",
    "_final_report_section_artifact_event",
    "validate_report_evidence_refs",
    "_report_evidence_ref_candidates",
    "_evidence_reference_warning_events",
    "_artifact_warning_event",
    "_evidence_reference_warning_strings",
    "_strict_evidence_block_error",
    "_source_candidate_artifact_event",
    "_source_candidate_from_upload",
    "_snippet_source_candidate_for_unit",
    "_legacy_adapter_source_candidate_for_unit",
    "_retrieved_source_from_upload_for_unit",
    "_evidence_from_retrieved_source",
    "_upload_source_for_candidate",
    "_upload_source_matches_unit",
    "_unit_search_terms",
    "_claim_state_from_findings",
    "_claim_state_from_verified_claims",
    "_merge_graph_claims",
    "_report_section_from_artifact_event",
    "_normalize_query",
    "_scope_text",
    "_research_units_for_query",
    "_required_sources",
    "_constraints",
    "_source_policy_from_context",
    "_upload_document_ids",
    "_upload_scope_state_from_context",
    "_initial_graph_warnings",
    "_upload_sources_from_context",
    "_upload_source_from_payload",
    "_short_content_hash",
    "_short_content_hash_from_digest",
    "_short_optional_content_hash",
    "_bounded_graph_text",
    "_bounded_optional_text",
    "_safe_int",
    "_upload_source_chunk_id",
    "_upload_source_candidate_id",
    "_upload_source_title",
    "_parse_upload_retrieved_at",
    "_upload_context_summary",
    "_upload_context_source_payloads",
    "_upload_context_document_ids",
    "_upload_context_document_ids_in_scope",
    "_normalized_upload_document_ids",
    "_query_variants",
    "_dedupe_queries",
]

def apply_langgraph_resume_checkpoint(
    state: ResearchGraphState,
    checkpoint: dict[str, Any] | None,
) -> ResearchGraphState:
    """Restore bounded graph state from a resumable LangGraph checkpoint."""

    if not _is_langgraph_resume_checkpoint(checkpoint):
        return state
    assert checkpoint is not None
    phase = _checkpoint_phase(checkpoint.get("phase")) or state.phase
    current_node = _checkpoint_phase(checkpoint.get("current_node"))
    resume_start_phase = current_node or _checkpoint_phase(_next_graph_phase_value(phase))
    source_ledger = checkpoint.get("source_ledger")
    evidence_ledger = checkpoint.get("evidence_ledger")
    source_entries = source_ledger if isinstance(source_ledger, list) else []
    evidence_entries = evidence_ledger if isinstance(evidence_ledger, list) else []
    completed_unit_ids = _checkpoint_string_list(checkpoint.get("completed_unit_ids"))
    pending_unit_ids = _checkpoint_string_list(checkpoint.get("pending_unit_ids"))
    unit_status_by_id = _checkpoint_unit_statuses(source_entries)
    if not completed_unit_ids:
        completed_unit_ids = [
            unit_id
            for unit_id, status in unit_status_by_id.items()
            if status == "completed"
        ]
    completed_unit_set = set(completed_unit_ids)
    pending_unit_set = set(pending_unit_ids)

    def restored_unit_status(unit: ResearchUnit) -> str:
        if unit.id in completed_unit_set:
            return "completed"
        if unit.id in pending_unit_set:
            return "pending"
        return unit_status_by_id.get(unit.id, unit.status)

    restored_units = [
        unit.model_copy(update={"status": restored_unit_status(unit)})
        for unit in state.plan.research_units
    ]
    restored_findings = _checkpoint_findings(evidence_entries)
    restored_verified_claims = _checkpoint_verified_claims(evidence_entries)
    restored_report_sections = _checkpoint_report_sections(evidence_entries)
    final_summary, final_boxed_answer = _checkpoint_final_outputs(
        evidence_entries,
        restored_report_sections,
        restored_verified_claims,
    )
    restored_claims = _merge_graph_claims(
        _claim_state_from_findings(restored_findings),
        _claim_state_from_verified_claims(restored_verified_claims),
    )
    return state.model_copy(
        update={
            "phase": phase,
            "plan": state.plan.model_copy(update={"research_units": restored_units}),
            "current_unit_id": _optional_checkpoint_text(
                checkpoint.get("current_research_unit"),
                80,
            ),
            "research_units": restored_units,
            "source_candidates": _checkpoint_source_candidates(source_entries),
            "retrieved_sources": _checkpoint_retrieved_sources(source_entries),
            "evidence": _checkpoint_evidence(evidence_entries),
            "findings": restored_findings,
            "claims": restored_claims,
            "verified_claims": restored_verified_claims,
            "report_sections": restored_report_sections,
            "final_summary": final_summary,
            "final_boxed_answer": final_boxed_answer,
            "resume_from_checkpoint": True,
            "resume_start_phase": resume_start_phase,
            "warnings": [
                *state.warnings,
                "langgraph_resume_from_checkpoint",
            ],
        }
    )


def _is_langgraph_resume_checkpoint(checkpoint: dict[str, Any] | None) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    executor = str(
        checkpoint.get("research_graph_executor")
        or (checkpoint.get("executor_state") or {}).get("research_graph_executor")
        or ""
    ).strip().lower()
    return (
        executor == "langgraph"
        and str(checkpoint.get("resume_policy") or "").strip()
        == LANGGRAPH_RESUME_POLICY
    )


def _checkpoint_phase(value: Any) -> ResearchPhase | None:
    if value is None:
        return None
    try:
        return ResearchPhase(str(value).strip())
    except ValueError:
        return None


def _checkpoint_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            result.append(text[:120])
            seen.add(text)
    return result


def _checkpoint_unit_statuses(entries: list[Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("ledger_type") not in {None, "research_unit"}:
            continue
        unit_id = str(entry.get("unit_id") or "").strip()
        status = str(entry.get("status") or "").strip()
        if unit_id and status in {"pending", "running", "completed", "failed"}:
            statuses[unit_id[:80]] = status
    return statuses


def _checkpoint_source_candidates(entries: list[Any]) -> list[SourceCandidate]:
    candidates: list[SourceCandidate] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("ledger_type") != "source_candidate":
            continue
        try:
            candidates.append(
                SourceCandidate(
                    candidate_id=_optional_checkpoint_text(
                        entry.get("candidate_id"),
                        120,
                    ),
                    title=_optional_checkpoint_text(entry.get("title"), 1_000),
                    summary=_optional_checkpoint_text(entry.get("summary"), 2_000),
                    source_type=str(entry.get("source_type") or "web")[:80],
                    source_state=str(
                        entry.get("source_state") or "source_candidate"
                    )[:80],
                    source_content_state=_optional_checkpoint_text(
                        entry.get("source_content_state"),
                        80,
                    ),
                    retrieval_status=_optional_checkpoint_text(
                        entry.get("retrieval_status"),
                        80,
                    ),
                    url=_optional_checkpoint_text(entry.get("url"), 4_000),
                    document_id=_optional_checkpoint_text(
                        entry.get("document_id"),
                        120,
                    ),
                    attached_document_id=_optional_checkpoint_text(
                        entry.get("attached_document_id"),
                        120,
                    ),
                    chunk_id=_optional_checkpoint_text(entry.get("chunk_id"), 120),
                    filename=_optional_checkpoint_text(entry.get("filename"), 1_000),
                    retrieved_at=_checkpoint_datetime(entry.get("retrieved_at")),
                    content_hash=_optional_checkpoint_text(
                        entry.get("content_hash"),
                        128,
                    ),
                    confidence=_checkpoint_float(entry.get("confidence"), 0.5),
                    source_event_type=_optional_checkpoint_text(
                        entry.get("source_event_type"),
                        120,
                    ),
                )
            )
        except ValueError:
            continue
    return candidates


def _checkpoint_retrieved_sources(entries: list[Any]) -> list[RetrievedSource]:
    sources: list[RetrievedSource] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("ledger_type") != "retrieved_source":
            continue
        source_id = str(entry.get("retrieved_source_id") or "").strip()
        quote = str(entry.get("quote_or_summary") or "").strip()
        content_hash = str(entry.get("content_hash") or "").strip()
        if not source_id or not quote or not content_hash:
            continue
        try:
            sources.append(
                RetrievedSource(
                    id=source_id[:120],
                    url=_optional_checkpoint_text(entry.get("url"), 4_000),
                    document_id=_optional_checkpoint_text(
                        entry.get("document_id"),
                        120,
                    ),
                    chunk_id=_optional_checkpoint_text(entry.get("chunk_id"), 120),
                    title=_optional_checkpoint_text(entry.get("title"), 1_000),
                    source_type=str(entry.get("source_type") or "web")[:80],
                    retrieved_at=_checkpoint_datetime(entry.get("retrieved_at"))
                    or datetime.now(timezone.utc),
                    content_hash=content_hash[:128],
                    quote_or_summary=_bounded_graph_text(
                        quote,
                        GRAPH_SOURCE_SUMMARY_MAX_CHARS,
                    ),
                    tool_name=str(entry.get("tool_name") or "checkpoint_resume")[:120],
                    confidence=_checkpoint_float(entry.get("confidence"), 0.5),
                )
            )
        except ValueError:
            continue
    return sources


def _checkpoint_evidence(entries: list[Any]) -> list[EvidenceItem]:
    evidence_items: list[EvidenceItem] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("ledger_type") not in {None, "evidence"}:
            continue
        evidence_id = str(entry.get("id") or "").strip()
        quote = str(entry.get("quote_or_summary") or "").strip()
        content_hash = str(entry.get("content_hash") or "").strip()
        if not evidence_id or not quote or not content_hash:
            continue
        try:
            evidence_items.append(
                EvidenceItem(
                    id=evidence_id[:120],
                    retrieved_source_id=_optional_checkpoint_text(
                        entry.get("retrieved_source_id"),
                        120,
                    ),
                    url=_optional_checkpoint_text(entry.get("url"), 4_000),
                    document_id=_optional_checkpoint_text(
                        entry.get("document_id"),
                        120,
                    ),
                    chunk_id=_optional_checkpoint_text(entry.get("chunk_id"), 120),
                    title=_optional_checkpoint_text(entry.get("title"), 1_000),
                    source_type=str(entry.get("source_type") or "web")[:80],
                    retrieved_at=_checkpoint_datetime(entry.get("retrieved_at"))
                    or datetime.now(timezone.utc),
                    content_hash=content_hash[:128],
                    quote_or_summary=_bounded_graph_text(
                        quote,
                        GRAPH_SOURCE_SUMMARY_MAX_CHARS,
                    ),
                    claims=_checkpoint_string_list(entry.get("claims"))[:50],
                    confidence=_checkpoint_float(entry.get("confidence"), 0.5),
                    tool_name=_optional_checkpoint_text(entry.get("tool_name"), 120),
                )
            )
        except ValueError:
            continue
    return evidence_items


def _checkpoint_findings(entries: list[Any]) -> list[CompressedFinding]:
    findings: list[CompressedFinding] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("ledger_type") != "finding":
            continue
        finding_id = str(entry.get("finding_id") or "").strip()
        unit_id = str(entry.get("research_unit_id") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        if not finding_id or not unit_id or not summary:
            continue
        try:
            findings.append(
                CompressedFinding(
                    id=finding_id[:120],
                    research_unit_id=unit_id[:80],
                    summary=_bounded_graph_text(
                        summary,
                        GRAPH_FINDING_SUMMARY_MAX_CHARS,
                    ),
                    evidence_ids=_checkpoint_string_list(entry.get("evidence_ids"))[:100],
                    confidence=_checkpoint_float(entry.get("confidence"), 0.5),
                )
            )
        except ValueError:
            continue
    return findings


def _checkpoint_verified_claims(entries: list[Any]) -> list[VerifiedClaim]:
    claims: list[VerifiedClaim] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("ledger_type") != "verified_claim":
            continue
        claim_id = str(entry.get("claim_id") or "").strip()
        claim_text = str(entry.get("claim") or "").strip()
        support_type = str(entry.get("support_type") or "").strip()
        if not claim_id or not claim_text or not support_type:
            continue
        try:
            claims.append(
                VerifiedClaim(
                    id=claim_id[:120],
                    claim=_bounded_graph_text(claim_text, 10_000),
                    support_type=support_type,  # type: ignore[arg-type]
                    evidence_ids=_checkpoint_string_list(entry.get("evidence_ids"))[:100],
                    rationale=_bounded_graph_text(
                        str(entry.get("rationale") or ""),
                        10_000,
                    ),
                    confidence=_checkpoint_float(entry.get("confidence"), 0.5),
                )
            )
        except ValueError:
            continue
    return claims


def _checkpoint_report_sections(entries: list[Any]) -> list[ReportSection]:
    sections: list[ReportSection] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("ledger_type") != "report_section":
            continue
        section_id = str(entry.get("section_id") or "").strip()
        title = str(entry.get("title") or "").strip()
        markdown = str(entry.get("markdown") or "").strip()
        if not section_id or not title or not markdown:
            continue
        try:
            sections.append(
                ReportSection(
                    section_id=section_id[:120],
                    title=title[:1_000],
                    markdown=_bounded_graph_text(markdown, 20_000),
                    evidence_refs=_checkpoint_string_list(entry.get("evidence_refs"))[:200],
                    source_event_type=_optional_checkpoint_text(
                        entry.get("source_event_type"),
                        120,
                    ),
                )
            )
        except ValueError:
            continue
    return sections


def _checkpoint_final_outputs(
    entries: list[Any],
    report_sections: list[ReportSection],
    verified_claims: list[VerifiedClaim],
) -> tuple[str | None, str | None]:
    final_summary = None
    final_boxed_answer = None
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("ledger_type") != "report_section":
            continue
        final_summary = _optional_checkpoint_text(
            entry.get("final_summary") or entry.get("markdown"),
            20_000,
        )
        final_boxed_answer = _optional_checkpoint_text(
            entry.get("final_boxed_answer"),
            10_000,
        )
        if final_summary:
            break
    if final_summary is None and report_sections:
        final_summary = report_sections[-1].markdown
    if final_boxed_answer is None and verified_claims:
        final_boxed_answer = verified_claims[0].claim
    return final_summary, final_boxed_answer


def _checkpoint_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _checkpoint_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(1.0, max(0.0, parsed))


def _optional_checkpoint_text(value: Any, max_chars: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_chars]


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
    evidence_strict_mode: Any | None = None,
) -> ResearchGraphExecutionResult:
    """Run the feature-flagged serial graph executor.

    The enabled path executes explicit graph nodes. Only `ResearchUnitNode` may
    call the legacy orchestrator, and it does so with a bounded single-unit
    prompt rather than the full graph plan.
    """

    strict_mode = parse_evidence_strict_mode(
        state.evidence_strict_mode
        if evidence_strict_mode is None
        else evidence_strict_mode
    )
    state = state.model_copy(update={"evidence_strict_mode": strict_mode})
    context = ResearchGraphExecutionContext(
        orchestrator=orchestrator,
        original_task_description=original_task_description,
        task_file_name=task_file_name,
        task_id=task_id,
        is_final_retry=is_final_retry,
        evidence_strict_mode=strict_mode,
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


def _verification_for_finding(
    finding: CompressedFinding,
    state: ResearchGraphState,
) -> tuple[str, list[str], str]:
    evidence_ids = [str(evidence_id).strip() for evidence_id in finding.evidence_ids]
    candidate_ids = {
        str(candidate.candidate_id or "").strip()
        for candidate in state.source_candidates
        if str(candidate.candidate_id or "").strip()
    }
    candidate_refs = [
        evidence_id for evidence_id in evidence_ids if evidence_id in candidate_ids
    ]
    known_evidence = {evidence.id: evidence for evidence in state.evidence}
    graph_evidence_ids = [
        evidence_id
        for evidence_id in evidence_ids
        if EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id)
    ]
    invalid_refs = [
        evidence_id
        for evidence_id in evidence_ids
        if evidence_id
        and evidence_id not in candidate_ids
        and (
            EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id) is None
            or evidence_id not in known_evidence
        )
    ]
    missing_refs = [
        evidence_id
        for evidence_id in graph_evidence_ids
        if evidence_id not in known_evidence
    ]
    invalid_or_missing_refs = _dedupe_preserve_order([*invalid_refs, *missing_refs])
    if invalid_or_missing_refs:
        return (
            "invalid_ref",
            invalid_or_missing_refs,
            "Verifier found missing or malformed evidence references.",
        )
    if graph_evidence_ids:
        linked_evidence = [known_evidence[evidence_id] for evidence_id in graph_evidence_ids]
        if _linked_evidence_is_contradicted(linked_evidence):
            return (
                "contradicted",
                graph_evidence_ids,
                "Verifier found opposing assertions across content-bearing evidence.",
            )
        return (
            "supported",
            graph_evidence_ids,
            "Verifier linked the finding to content-bearing retrieved evidence.",
        )
    if candidate_refs or _finding_mentions_source_candidate(finding, state.source_candidates):
        return (
            "weak",
            [],
            "Verifier found only source-candidate support without content-bearing evidence.",
        )
    return (
        "insufficient",
        [],
        "Verifier found no evidence references for this claim.",
    )


def _verified_claim_confidence(confidence: float, support_type: str) -> float:
    bounded = max(0.0, min(float(confidence), 1.0))
    if support_type == "supported":
        return bounded
    if support_type == "contradicted":
        return min(bounded, 0.6)
    if support_type == "weak":
        return min(bounded, 0.4)
    return min(bounded, 0.2)


def _linked_evidence_is_contradicted(evidence_items: list[EvidenceItem]) -> bool:
    if len(evidence_items) < 2:
        return False
    polarities = [_evidence_polarity(item.quote_or_summary) for item in evidence_items]
    return "positive" in polarities and "negative" in polarities


def _evidence_polarity(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").lower())
    if any(marker in normalized for marker in VERIFIER_NEGATIVE_MARKERS):
        return "negative"
    if any(marker in normalized for marker in VERIFIER_POSITIVE_MARKERS):
        return "positive"
    return "neutral"


def _finding_mentions_source_candidate(
    finding: CompressedFinding,
    candidates: list[SourceCandidate],
) -> bool:
    haystack = str(finding.summary or "").lower()
    if not haystack:
        return False
    for candidate in candidates:
        tokens = [
            candidate.candidate_id,
            candidate.title,
            candidate.summary,
            candidate.url,
        ]
        if any(
            token and str(token).strip().lower() in haystack
            for token in tokens
        ):
            return True
    return False


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _verifier_invalid_ref_block_error(evidence_ids: list[str]) -> str:
    refs = ",".join(_dedupe_preserve_order(evidence_ids)[:20]) or "missing"
    return f"research_graph_verifier_invalid_ref_block: invalid_ref={refs}"


def _research_unit_id_from_evidence(evidence: EvidenceItem, index: int) -> str:
    if evidence.url and evidence.url.startswith("graph://"):
        value = evidence.url.removeprefix("graph://").strip()
        if value:
            return value
    return f"unit-{index + 1}"


def _source_ledger_for_checkpoint(state: ResearchGraphState) -> list[dict[str, Any]]:
    unit_entries = [
        {
            "ledger_type": "research_unit",
            "unit_id": unit.id,
            "status": unit.status,
            "search_queries": list(unit.search_queries),
            "source_policy": unit.source_policy.model_dump(mode="json"),
        }
        for unit in state.plan.research_units
    ]
    candidate_entries = [
        {
            "ledger_type": "source_candidate",
            "candidate_id": candidate.candidate_id,
            "title": candidate.title,
            "summary": candidate.summary,
            "source_type": candidate.source_type,
            "source_state": candidate.source_state,
            "source_content_state": candidate.source_content_state,
            "retrieval_status": candidate.retrieval_status,
            "url": candidate.url,
            "document_id": candidate.document_id,
            "attached_document_id": candidate.attached_document_id,
            "chunk_id": candidate.chunk_id,
            "filename": candidate.filename,
            "retrieved_at": candidate.retrieved_at.isoformat()
            if candidate.retrieved_at
            else None,
            "content_hash": candidate.content_hash,
            "confidence": candidate.confidence,
            "source_event_type": candidate.source_event_type,
        }
        for candidate in state.source_candidates
    ]
    retrieved_entries = [
        {
            "ledger_type": "retrieved_source",
            "retrieved_source_id": source.id,
            "title": source.title,
            "source_type": source.source_type,
            "url": source.url,
            "document_id": source.document_id,
            "chunk_id": source.chunk_id,
            "content_hash": source.content_hash,
            "retrieved_at": source.retrieved_at.isoformat(),
            "quote_or_summary": _bounded_graph_text(
                source.quote_or_summary,
                GRAPH_SOURCE_SUMMARY_MAX_CHARS,
            ),
            "tool_name": source.tool_name,
            "confidence": source.confidence,
        }
        for source in state.retrieved_sources
    ]
    return unit_entries + candidate_entries + retrieved_entries


def _evidence_ledger_for_checkpoint(state: ResearchGraphState) -> list[dict[str, Any]]:
    evidence_entries = [
        {
            "ledger_type": "evidence",
            "id": evidence.id,
            "retrieved_source_id": evidence.retrieved_source_id,
            "source_type": evidence.source_type,
            "url": evidence.url,
            "document_id": evidence.document_id,
            "chunk_id": evidence.chunk_id,
            "title": evidence.title,
            "content_hash": evidence.content_hash,
            "retrieved_at": evidence.retrieved_at.isoformat(),
            "quote_or_summary": _bounded_graph_text(
                evidence.quote_or_summary,
                GRAPH_SOURCE_SUMMARY_MAX_CHARS,
            ),
            "claims": list(evidence.claims),
            "confidence": evidence.confidence,
            "tool_name": evidence.tool_name,
        }
        for evidence in state.evidence
    ]
    finding_entries = [
        {
            "ledger_type": "finding",
            "finding_id": finding.id,
            "research_unit_id": finding.research_unit_id,
            "summary": finding.summary,
            "evidence_ids": list(finding.evidence_ids),
            "confidence": finding.confidence,
        }
        for finding in state.findings
    ]
    claim_entries = [
        {
            "ledger_type": "verified_claim",
            "claim_id": claim.id,
            "claim": claim.claim,
            "support_type": claim.support_type,
            "evidence_ids": list(claim.evidence_ids),
            "rationale": claim.rationale,
            "confidence": claim.confidence,
        }
        for claim in state.verified_claims
    ]
    report_entries = [
        {
            "ledger_type": "report_section",
            "section_id": section.section_id,
            "title": section.title,
            "markdown": _bounded_graph_text(section.markdown, 20_000),
            "final_summary": _bounded_graph_text(state.final_summary, 20_000)
            if state.final_summary
            else None,
            "final_boxed_answer": _bounded_graph_text(
                state.final_boxed_answer,
                10_000,
            )
            if state.final_boxed_answer
            else None,
            "evidence_refs": list(section.evidence_refs),
            "source_event_type": section.source_event_type,
        }
        for section in state.report_sections
    ]
    return evidence_entries + finding_entries + claim_entries + report_entries


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
    known_evidence_ids = {evidence.id for evidence in state.evidence}
    evidence_refs = list(
        dict.fromkeys(
            evidence_id
            for claim in state.verified_claims
            if claim.support_type in {"supported", "contradicted"}
            for evidence_id in claim.evidence_ids
            if evidence_id in known_evidence_ids
            and EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id)
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


def validate_report_evidence_refs(
    *,
    markdown: Any,
    evidence_refs: Any,
    known_evidence_ids: set[str],
) -> EvidenceReferenceValidation:
    candidate_refs = _report_evidence_ref_candidates(markdown, evidence_refs)
    valid_refs: list[str] = []
    invalid_refs: list[str] = []
    seen_valid: set[str] = set()
    seen_invalid: set[str] = set()
    for evidence_ref in candidate_refs:
        if EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_ref):
            if evidence_ref not in seen_valid:
                seen_valid.add(evidence_ref)
                valid_refs.append(evidence_ref)
            continue
        if evidence_ref.startswith("EVID-") and evidence_ref not in seen_invalid:
            seen_invalid.add(evidence_ref)
            invalid_refs.append(evidence_ref)
    unresolved_refs = [
        evidence_ref
        for evidence_ref in valid_refs
        if evidence_ref not in known_evidence_ids
    ]
    return EvidenceReferenceValidation(
        evidence_refs=valid_refs,
        unresolved_refs=unresolved_refs,
        invalid_refs=invalid_refs,
    )


def _report_evidence_ref_candidates(markdown: Any, evidence_refs: Any) -> list[str]:
    refs: list[str] = []
    raw_refs = [evidence_refs] if isinstance(evidence_refs, str) else evidence_refs or []
    for evidence_ref in raw_refs:
        text = str(evidence_ref or "").strip()
        if text.startswith("EVID-"):
            refs.append(text)
    refs.extend(
        match.group(1)
        for match in EVIDENCE_ID_TOKEN_PATTERN.finditer(str(markdown or ""))
    )
    return refs


def _evidence_reference_warning_events(
    *,
    state: ResearchGraphState,
    validation: EvidenceReferenceValidation,
    artifact_type: str,
    local_artifact_id: str,
    source_event_type: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if validation.invalid_refs:
        events.append(
            _artifact_warning_event(
                state=state,
                warning="invalid_evidence_refs",
                artifact_type=artifact_type,
                local_artifact_id=local_artifact_id,
                evidence_refs=validation.invalid_refs,
                source_event_type=source_event_type,
            )
        )
    if validation.unresolved_refs:
        events.append(
            _artifact_warning_event(
                state=state,
                warning="unresolved_evidence_refs",
                artifact_type=artifact_type,
                local_artifact_id=local_artifact_id,
                evidence_refs=validation.unresolved_refs,
                source_event_type=source_event_type,
            )
        )
    return events


def _artifact_warning_event(
    *,
    state: ResearchGraphState,
    warning: str,
    artifact_type: str,
    local_artifact_id: str,
    evidence_refs: list[str],
    source_event_type: str,
) -> dict[str, Any]:
    return {
        "event": "artifact_warning",
        "type": "artifact_warning",
        "payload": {
            "task_id": state.task_id,
            "warning": warning,
            "artifact_type": artifact_type,
            "local_artifact_id": local_artifact_id,
            "evidence_refs": list(evidence_refs),
            "source_event_type": source_event_type,
        },
    }


def _evidence_reference_warning_strings(
    validation: EvidenceReferenceValidation,
) -> list[str]:
    warnings: list[str] = []
    if validation.invalid_refs:
        warnings.append(f"invalid_evidence_refs={','.join(validation.invalid_refs[:20])}")
    if validation.unresolved_refs:
        warnings.append(
            f"unresolved_evidence_refs={','.join(validation.unresolved_refs[:20])}"
        )
    return warnings


def _strict_evidence_block_error(validation: EvidenceReferenceValidation) -> str:
    parts = []
    if validation.invalid_refs:
        parts.append(f"invalid_evidence_refs={','.join(validation.invalid_refs[:20])}")
    if validation.unresolved_refs:
        parts.append(
            f"unresolved_evidence_refs={','.join(validation.unresolved_refs[:20])}"
        )
    detail = "; ".join(parts) if parts else "evidence_refs_invalid"
    return f"research_graph_evidence_strict_block: {detail}"


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


from .research_graph_uploads import *  # noqa: F403
