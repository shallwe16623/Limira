import pytest
from omegaconf import OmegaConf

from archive_writer import scrub_secrets
from src.core import pipeline as pipeline_module
from src.core import research_graph as research_graph_module
from src.core import research_langgraph as research_langgraph_module
from src.core.research_graph import (
    CompressedFinding,
    EvidenceItem,
    EvidenceStrictMode,
    LangGraphResearchUnitNode,
    ResearchGraphExecutionContext,
    ResearchGraphExecutionResult,
    ResearchGraphNodeOutput,
    ResearchGraphState,
    ResearchPhase,
    RetrieverRegistry,
    SourceCandidate,
    UploadedDocumentSearchRetriever,
    VerifierNode,
    WebSearchRetriever,
    VerifiedClaim,
    build_initial_research_graph,
    default_retriever_registry,
    evidence_id_for_source,
    graph_bootstrap_events,
    graph_task_description,
    parse_evidence_strict_mode,
    validate_report_evidence_refs,
)


class _CaptureQueue:
    def __init__(self):
        self.items = []

    async def put(self, item):
        self.items.append(item)


class _FakeToolManager:
    def __init__(self):
        self.task_log = None

    def set_task_log(self, task_log):
        self.task_log = task_log


class _FakeClientFactory:
    def __init__(self, **_kwargs):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeOrchestrator:
    task_descriptions = []

    def __init__(self, *, stream_queue=None, **_kwargs):
        self.stream_queue = stream_queue

    async def run_main_agent(self, **kwargs):
        self.__class__.task_descriptions.append(kwargs["task_description"])
        await self.stream_queue.put(
            {"event": "message", "data": {"delta": {"content": "legacy executor"}}}
        )
        return "summary", "final", None


class _LongSummaryOrchestrator:
    task_descriptions = []

    def __init__(self, *, stream_queue=None, **_kwargs):
        self.stream_queue = stream_queue

    async def run_main_agent(self, **kwargs):
        self.__class__.task_descriptions.append(kwargs["task_description"])
        return "long research summary " + ("x" * 25_000), "final", None


class _FailingOrchestrator:
    def __init__(self, **_kwargs):
        pass

    async def run_main_agent(self, **_kwargs):
        raise RuntimeError("graph executor failure")


class _MissingFinalOutputOrchestrator:
    outputs = ("", "", None)

    def __init__(self, **_kwargs):
        pass

    async def run_main_agent(self, **_kwargs):
        return self.__class__.outputs


def _pipeline_cfg(
    *,
    graph_enabled: bool = False,
    graph_executor: str | None = None,
    evidence_strict: str | None = None,
):
    agent_cfg = {
        "keep_tool_result": True,
        "main_agent": {"max_turns": 1},
        "sub_agents": None,
    }
    if graph_enabled:
        agent_cfg["research_graph"] = {"enabled": True}
    if graph_executor is not None:
        agent_cfg.setdefault("research_graph", {})["executor"] = graph_executor
    config = {
        "llm": {
            "provider": "openai-compatible",
            "base_url": "https://llm.test",
            "model_name": "test-model",
            "temperature": 0,
            "top_p": 1,
            "min_p": 0,
            "top_k": 0,
            "max_tokens": 4096,
            "repetition_penalty": 1,
            "async_client": False,
        },
        "agent": agent_cfg,
    }
    if evidence_strict is not None:
        config["limira"] = {"evidence": {"strict": evidence_strict}}
    return OmegaConf.create(config)


def test_research_graph_state_contains_ac2_contract_fields():
    required_fields = {
        "task_id",
        "query",
        "scenario",
        "source_policy",
        "upload_scope",
        "brief",
        "plan",
        "current_unit_id",
        "research_units",
        "retrieved_sources",
        "source_candidates",
        "evidence",
        "findings",
        "claims",
        "verified_claims",
        "report_sections",
        "warnings",
    }

    assert required_fields <= set(ResearchGraphState.model_fields)


def test_evidence_strict_mode_parser_and_report_ref_validation():
    assert parse_evidence_strict_mode(None) == EvidenceStrictMode.WARN
    assert parse_evidence_strict_mode("warn") == EvidenceStrictMode.WARN
    assert parse_evidence_strict_mode("block") == EvidenceStrictMode.BLOCK
    with pytest.raises(ValueError, match="invalid_evidence_strict_mode"):
        parse_evidence_strict_mode("silent")

    validation = validate_report_evidence_refs(
        markdown=(
            "Known [EVID-001], missing [EVID-999], malformed [EVID-abc], "
            "and truncated EVID-abcdef1234567."
        ),
        evidence_refs=["EVID-001", "EVID-999", "EVID-abc"],
        known_evidence_ids={"EVID-001"},
    )

    assert validation.evidence_refs == ["EVID-001", "EVID-999"]
    assert validation.unresolved_refs == ["EVID-999"]
    assert validation.invalid_refs == ["EVID-abc", "EVID-abcdef1234567"]


def _verifier_state(
    *,
    evidence: list[EvidenceItem] | None = None,
    finding_evidence_ids: list[str] | None = None,
    finding_summary: str = "Claim requires verification.",
    source_candidates: list[SourceCandidate] | None = None,
):
    state = build_initial_research_graph(
        task_id="task-verifier-classification",
        query="Verify a company designation with primary sources",
        max_units=1,
    )
    finding = CompressedFinding(
        id="finding-verifier",
        research_unit_id=state.plan.research_units[0].id,
        summary=finding_summary,
        evidence_ids=list(finding_evidence_ids or []),
        confidence=0.85,
    )
    return state.model_copy(
        update={
            "phase": ResearchPhase.COMPRESS,
            "source_candidates": list(source_candidates or []),
            "evidence": list(evidence or []),
            "findings": [finding],
        }
    )


def _evidence_item(evidence_id: str, summary: str) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        retrieved_source_id=f"RSRC-{evidence_id.removeprefix('EVID-')}",
        title=f"Evidence {evidence_id}",
        source_type="web",
        content_hash=(evidence_id.replace("-", "").lower() + "0" * 16)[:16],
        quote_or_summary=summary,
        confidence=0.8,
        tool_name="test_retriever",
    )


async def _run_verifier_for_state(
    state,
    *,
    evidence_strict_mode=EvidenceStrictMode.WARN,
):
    context = ResearchGraphExecutionContext(
        orchestrator=_FakeOrchestrator(),
        original_task_description="Verify a company designation with primary sources",
        task_id=state.task_id,
        evidence_strict_mode=evidence_strict_mode,
    )
    output = await VerifierNode().run(
        state,
        context,
        ResearchGraphNodeOutput(state=state),
    )
    return output.state.verified_claims[0], output


@pytest.mark.asyncio
async def test_verifier_classifies_content_bearing_evidence_as_supported():
    evidence = [
        _evidence_item("EVID-001", "The entity is listed under the program.")
    ]
    state = _verifier_state(
        evidence=evidence,
        finding_evidence_ids=["EVID-001"],
        finding_summary="The entity is listed under the program.",
    )

    claim, output = await _run_verifier_for_state(state)

    assert claim.support_type == "supported"
    assert claim.evidence_ids == ["EVID-001"]
    assert claim.confidence == 0.85
    assert output.artifact_events[0]["payload"]["support_type"] == "supported"


@pytest.mark.asyncio
async def test_verifier_classifies_no_evidence_as_insufficient():
    state = _verifier_state(
        finding_evidence_ids=[],
        finding_summary="The designation cannot be confirmed from evidence.",
    )

    claim, output = await _run_verifier_for_state(state)

    assert claim.support_type == "insufficient"
    assert claim.evidence_ids == []
    assert claim.confidence <= 0.2
    assert output.artifact_events[0]["payload"]["evidence_refs"] == []


@pytest.mark.asyncio
async def test_verifier_classifies_source_candidate_only_support_as_weak():
    candidate = SourceCandidate(
        candidate_id="SRC-CANDIDATE-ONLY",
        title="Candidate-only source",
        summary="Snippet says the entity may be listed.",
        source_type="web_search",
        source_content_state="snippet_only",
        retrieval_status="candidate_only",
    )
    state = _verifier_state(
        source_candidates=[candidate],
        finding_evidence_ids=["SRC-CANDIDATE-ONLY"],
        finding_summary="SRC-CANDIDATE-ONLY suggests possible listing.",
    )

    claim, _output = await _run_verifier_for_state(state)

    assert claim.support_type == "weak"
    assert claim.evidence_ids == []
    assert "source-candidate" in claim.rationale


@pytest.mark.asyncio
async def test_verifier_classifies_missing_or_malformed_refs_as_invalid_ref():
    state = _verifier_state(
        evidence=[],
        finding_evidence_ids=["EVID-999", "EVID-1"],
        finding_summary="Claim cites missing and malformed refs.",
    )

    claim, output = await _run_verifier_for_state(state)

    assert claim.support_type == "invalid_ref"
    assert claim.evidence_ids == ["EVID-999", "EVID-1"]
    assert output.artifact_events[0]["payload"]["support_type"] == "invalid_ref"


@pytest.mark.asyncio
async def test_verifier_blocks_invalid_refs_in_strict_block_mode():
    state = _verifier_state(
        evidence=[],
        finding_evidence_ids=["EVID-999"],
        finding_summary="Claim cites a missing ref.",
    )

    with pytest.raises(ValueError, match="research_graph_verifier_invalid_ref_block"):
        await _run_verifier_for_state(
            state,
            evidence_strict_mode=EvidenceStrictMode.BLOCK,
        )


@pytest.mark.asyncio
async def test_verifier_classifies_opposing_evidence_as_contradicted():
    evidence = [
        _evidence_item("EVID-001", "The entity is listed under the program."),
        _evidence_item("EVID-002", "The entity is not listed under the program."),
    ]
    state = _verifier_state(
        evidence=evidence,
        finding_evidence_ids=["EVID-001", "EVID-002"],
        finding_summary="Sources disagree about whether the entity is listed.",
    )

    claim, _output = await _run_verifier_for_state(state)

    assert claim.support_type == "contradicted"
    assert claim.evidence_ids == ["EVID-001", "EVID-002"]
    assert claim.confidence <= 0.6


def test_initial_research_graph_creates_bounded_scope_and_plan():
    state = build_initial_research_graph(
        task_id="task-graph",
        query="  Track BYD Section 1260H list status.  ",
        scenario="sanctions_export_controls",
        max_units=3,
    )

    assert state.phase == ResearchPhase.PLAN
    assert state.task_id == "task-graph"
    assert state.query == "Track BYD Section 1260H list status."
    assert state.scenario == "sanctions_export_controls"
    assert state.evidence_strict_mode == EvidenceStrictMode.WARN
    assert state.brief.original_query == "Track BYD Section 1260H list status."
    assert "sanctions_export_controls" in state.brief.scope
    assert len(state.plan.research_units) == 3
    assert state.research_units == state.plan.research_units
    assert state.current_unit_id is None
    assert state.source_candidates == []
    assert state.claims == []
    assert state.report_sections == []
    assert state.warnings == []
    assert state.plan.research_units[0].id.startswith("unit-1-")
    assert state.plan.research_units[0].search_queries
    assert "evidence" in state.plan.expected_artifacts
    assert "Cross-check" in state.plan.verification_strategy


def test_initial_research_graph_applies_upload_context_and_source_policy():
    state = build_initial_research_graph(
        task_id="task-context",
        query="Assess export control exposure",
        scenario="sanctions_export_controls",
        document_ids=["doc-a", "doc-b"],
        upload_scope={
            "document_count": 2,
            "retrieval_status": "partial",
            "retrieved_document_ids": ["doc-a"],
            "context_only_document_ids": ["doc-b"],
            "source_payloads": [
                {
                    "document_id": "doc-a",
                    "filename": "memo.txt",
                    "retrieved_at": "2026-06-06T12:00:00+00:00",
                    "content_hash": "hash-a",
                    "text": "Uploaded memo states a controlled export exposure.",
                }
            ],
        },
        source_policy={
            "min_sources": 5,
            "prefer_primary_sources": False,
            "allow_secondary_sources": True,
            "require_retrieved_at": True,
            "prefer_uploaded_documents": True,
            "prefer_scenario_sources": True,
        },
    )

    assert "sanctions_export_controls" in state.brief.scope
    assert "2 attached upload source(s)" in state.brief.scope
    assert "Upload retrieval status: partial" in state.brief.scope
    assert any("attached upload documents" in item for item in state.brief.required_sources)
    assert any("retrieved upload text" in item and "doc-a" in item for item in state.brief.required_sources)
    assert any(
        "Uploaded memo states a controlled export exposure." in item
        for item in state.brief.required_sources
    )
    assert len(state.upload_sources) == 1
    assert state.upload_sources[0].document_id == "doc-a"
    assert state.upload_sources[0].chunk_id.startswith("UPLOAD-CHUNK-")
    assert state.upload_sources[0].source_type == "limira_upload"
    assert state.upload_sources[0].text == (
        "Uploaded memo states a controlled export exposure."
    )
    assert state.upload_scope.document_count == 2
    assert state.upload_scope.retrieval_status == "partial"
    assert state.upload_scope.retrieved_document_ids == ["doc-a"]
    assert state.upload_scope.context_only_document_ids == ["doc-b"]
    assert state.upload_scope.source_payload_count == 1
    assert state.upload_scope.source_payload_refs[0].document_id == "doc-a"
    assert state.upload_scope.source_payload_refs[0].filename == "memo.txt"
    assert (
        "Uploaded memo states a controlled export exposure."
        not in state.upload_scope.model_dump_json()
    )
    assert state.context_only_upload_document_ids == ["doc-b"]
    assert state.source_policy.min_sources == 5
    assert state.source_policy.prefer_primary_sources is False
    assert state.source_policy.prefer_uploaded_documents is True
    assert state.source_policy.prefer_scenario_sources is True
    assert state.warnings == ["context_only_upload_documents=doc-b"]
    assert any("uploaded document facts" in item for item in state.brief.constraints)
    assert any("context-only upload IDs" in item and "doc-b" in item for item in state.brief.constraints)
    assert state.plan.research_units[0].source_policy.min_sources == 5
    assert state.plan.research_units[0].source_policy.prefer_primary_sources is False
    assert state.plan.research_units[0].source_policy.prefer_uploaded_documents is True
    assert state.plan.research_units[0].source_policy.prefer_scenario_sources is True


def test_research_graph_bootstrap_events_are_serializable_and_ordered():
    state = build_initial_research_graph(
        task_id="task-graph",
        query="Verify a company designation with primary sources",
    )

    events = graph_bootstrap_events(state)

    assert [event["event"] for event in events] == [
        "research_brief_created",
        "research_plan_created",
    ]
    assert events[0]["data"]["phase"] == "scope"
    assert events[0]["data"]["brief"]["original_query"] == (
        "Verify a company designation with primary sources"
    )
    assert events[1]["data"]["phase"] == "plan"
    assert events[1]["data"]["plan"]["research_units"]


def test_graph_task_description_includes_plan_for_compatibility_executor():
    state = build_initial_research_graph(
        task_id="task-graph",
        query="Verify a company designation with primary sources",
    )

    task_description = graph_task_description(
        state,
        "Verify a company designation with primary sources",
    )

    assert task_description.startswith(
        "Verify a company designation with primary sources"
    )
    assert "## Limira Research Workflow" in task_description
    assert "### Research Units" in task_description
    assert "unit-1-background" in task_description
    assert "### Verification Strategy" in task_description
    assert "### Report Contract" in task_description


def test_graph_task_description_surfaces_source_policy_flags():
    state = build_initial_research_graph(
        task_id="task-graph-policy",
        query="Verify a claim with constrained sources",
        source_policy={
            "min_sources": 4,
            "prefer_primary_sources": False,
            "allow_secondary_sources": False,
            "require_retrieved_at": False,
            "prefer_uploaded_documents": True,
            "prefer_scenario_sources": True,
        },
    )

    task_description = graph_task_description(
        state,
        "Verify a claim with constrained sources",
    )

    assert "Source target: at least 4" in task_description
    assert "prefer_primary_sources=False" in task_description
    assert "allow_secondary_sources=False" in task_description
    assert "require_retrieved_at=False" in task_description
    assert "prefer_uploaded_documents=True" in task_description
    assert "prefer_scenario_sources=True" in task_description


def test_langgraph_executor_builds_dependency_backed_stategraph():
    state = build_initial_research_graph(
        task_id="task-langgraph-builder",
        query="Verify a company designation with primary sources",
    )
    context = ResearchGraphExecutionContext(
        orchestrator=object(),
        original_task_description=state.brief.original_query,
        task_id=state.task_id,
    )

    graph = research_langgraph_module.build_langgraph_research_graph(
        context=context,
        stream_queue=None,
    )

    assert hasattr(graph, "ainvoke")
    assert research_langgraph_module.StateGraph.__module__.startswith("langgraph.")
    assert research_langgraph_module.LANGGRAPH_EXECUTOR_NAME == "langgraph"


def test_default_retriever_registry_resolves_required_retrievers():
    registry = default_retriever_registry()

    assert set(registry.names) >= {
        "web_search",
        "page_visit_or_jina_summary",
        "uploaded_document_search",
        "legacy_agent_adapter",
    }
    assert registry.resolve("web-search").name == "web_search"
    assert registry.resolve("page_visit_or_jina_summary").name == (
        "page_visit_or_jina_summary"
    )
    assert registry.resolve("uploaded_document_search").name == (
        "uploaded_document_search"
    )
    assert registry.resolve("legacy_agent_adapter").name == "legacy_agent_adapter"
    with pytest.raises(KeyError, match="unknown_retriever"):
        registry.resolve("unknown")
    registry.disable("web_search")
    with pytest.raises(RuntimeError, match="disabled_retriever:web_search"):
        registry.resolve("web_search")


@pytest.mark.asyncio
async def test_langgraph_executor_populates_ac2_state_contract_and_bounded_checkpoints():
    _FakeOrchestrator.task_descriptions = []
    upload_text = "Uploaded memo states the entity is listed under program X."
    initial_state = build_initial_research_graph(
        task_id="task-langgraph-state-contract",
        query="Assess uploaded document evidence",
        scenario="sanctions_export_controls",
        document_ids=["doc-upload"],
        upload_scope={
            "document_count": 1,
            "retrieval_status": "retrieved",
            "retrieved_document_ids": ["doc-upload"],
            "context_only_document_ids": [],
            "source_payloads": [
                {
                    "candidate_id": "SRC-UPLOAD-001",
                    "document_id": "doc-upload",
                    "attached_document_id": "attached-doc-upload",
                    "chunk_id": "UPLOAD-CHUNK-001",
                    "filename": "memo.txt",
                    "source_content_state": "content_bearing",
                    "retrieval_status": "retrieved",
                    "retrieved_at": "2026-06-06T12:00:00+00:00",
                    "content_hash": "a" * 64,
                    "text": upload_text,
                    "text_char_count": len(upload_text),
                }
            ],
        },
        source_policy={"min_sources": 3, "prefer_uploaded_documents": True},
        max_units=2,
    )
    stream_queue = _CaptureQueue()

    result = await research_langgraph_module.execute_langgraph_research(
        state=initial_state,
        orchestrator=_FakeOrchestrator(stream_queue=stream_queue),
        original_task_description="Assess uploaded document evidence",
        task_file_name="",
        task_id="task-langgraph-state-contract",
        is_final_retry=False,
        stream_queue=stream_queue,
    )

    final_state = result.state
    assert final_state.phase == ResearchPhase.COMPLETE
    assert final_state.query == "Assess uploaded document evidence"
    assert final_state.scenario == "sanctions_export_controls"
    assert final_state.source_policy.min_sources == 3
    assert final_state.source_policy.prefer_uploaded_documents is True
    assert final_state.upload_scope.document_count == 1
    assert final_state.upload_scope.source_payload_count == 1
    assert upload_text not in final_state.upload_scope.model_dump_json()
    assert final_state.current_unit_id.startswith("unit-2-")
    assert final_state.research_units == final_state.plan.research_units
    assert all(unit.status == "completed" for unit in final_state.research_units)
    upload_candidates = [
        candidate
        for candidate in final_state.source_candidates
        if candidate.source_type == "limira_upload"
    ]
    assert upload_candidates
    assert upload_candidates[0].document_id == "doc-upload"
    assert upload_candidates[0].chunk_id == "UPLOAD-CHUNK-001"
    assert final_state.retrieved_sources
    assert final_state.evidence
    assert final_state.findings
    assert final_state.claims
    assert any(claim.source == "finding" for claim in final_state.claims)
    assert any(claim.source == "verified_claim" for claim in final_state.claims)
    assert final_state.verified_claims
    assert final_state.report_sections
    assert "## Verified Claims" in final_state.report_sections[0].markdown
    assert final_state.report_sections[0].evidence_refs

    report_payloads = [
        item["payload"]
        for item in stream_queue.items
        if item.get("type") == "report_section_generated"
    ]
    assert report_payloads
    assert final_state.report_sections[0].markdown == report_payloads[0]["markdown"]
    checkpoints = [
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
    ]
    forbidden_checkpoint_fields = {
        "query",
        "scenario",
        "upload_scope",
        "source_candidates",
        "claims",
        "report_sections",
        "warnings",
    }
    assert checkpoints
    for checkpoint in checkpoints:
        assert checkpoint["research_graph_executor"] == "langgraph"
        assert forbidden_checkpoint_fields.isdisjoint(checkpoint)
    complete_checkpoint = checkpoints[-1]
    assert complete_checkpoint["phase"] == "complete"
    assert complete_checkpoint["status"] == "completed"
    assert complete_checkpoint["current_research_unit"].startswith("unit-2-")


@pytest.mark.asyncio
async def test_langgraph_research_unit_decomposes_upload_retrieval_without_legacy_adapter():
    _FakeOrchestrator.task_descriptions = []
    upload_text = "Uploaded document evidence confirms program X exposure."
    initial_state = build_initial_research_graph(
        task_id="task-langgraph-upload-decomposed",
        query="Assess uploaded document evidence",
        document_ids=["doc-upload"],
        upload_scope={
            "document_count": 1,
            "retrieval_status": "retrieved",
            "retrieved_document_ids": ["doc-upload"],
            "source_payloads": [
                {
                    "candidate_id": "SRC-UPLOAD-DECOMPOSED",
                    "document_id": "doc-upload",
                    "chunk_id": "UPLOAD-CHUNK-DECOMPOSED",
                    "filename": "memo.txt",
                    "source_content_state": "content_bearing",
                    "retrieval_status": "retrieved",
                    "retrieved_at": "2026-06-06T12:00:00+00:00",
                    "content_hash": "b" * 64,
                    "text": upload_text,
                }
            ],
        },
        max_units=1,
    )
    stream_queue = _CaptureQueue()

    result = await research_langgraph_module.execute_langgraph_research(
        state=initial_state,
        orchestrator=_FakeOrchestrator(stream_queue=stream_queue),
        original_task_description="Assess uploaded document evidence",
        task_file_name="",
        task_id="task-langgraph-upload-decomposed",
        is_final_retry=False,
        stream_queue=stream_queue,
    )

    assert _FakeOrchestrator.task_descriptions == []
    final_state = result.state
    assert any(
        candidate.source_content_state == "snippet_only"
        for candidate in final_state.source_candidates
    )
    assert any(
        candidate.source_type == "limira_upload"
        and candidate.source_content_state == "content_bearing"
        for candidate in final_state.source_candidates
    )
    assert all(
        source.source_type != "web_search"
        for source in final_state.retrieved_sources
    )
    assert all(item.source_type != "web_search" for item in final_state.evidence)
    assert any(
        source.source_type == "limira_upload"
        for source in final_state.retrieved_sources
    )
    assert any(item.source_type == "limira_upload" for item in final_state.evidence)
    assert final_state.findings

    typed_events = [
        (index, item.get("type"), item.get("payload", {}).get("source_type"))
        for index, item in enumerate(stream_queue.items)
        if item.get("type")
    ]
    upload_retrieved_index = next(
        index
        for index, event_type, source_type in typed_events
        if event_type == "retrieved_source_collected" and source_type == "limira_upload"
    )
    upload_evidence_index = next(
        index
        for index, event_type, source_type in typed_events
        if event_type == "evidence_collected" and source_type == "limira_upload"
    )
    assert upload_retrieved_index < upload_evidence_index

    research_checkpoint = next(
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
        and item["data"]["phase"] == "research"
    )
    executor_state = research_checkpoint["executor_state"]
    unit_id = final_state.plan.research_units[0].id
    assert executor_state["node"] == "LangGraphResearchUnitNode"
    assert research_checkpoint["current_research_unit"] == unit_id
    assert executor_state["current_unit_id"] == unit_id
    assert executor_state["completed_unit_ids"] == [unit_id]
    assert executor_state["source_candidate_count"] >= 2
    assert executor_state["retrieved_source_count"] == len(final_state.retrieved_sources)
    assert executor_state["evidence_count"] == len(final_state.evidence)
    assert executor_state["finding_count"] == len(final_state.findings)
    assert executor_state["resume_marker"] == "completed_unit_ids_available"
    assert executor_state["legacy_adapter_calls"] == 0
    assert executor_state["unit_substeps"][0]["steps"] == [
        "search",
        "retrieve",
        "promote",
        "synthesize",
    ]
    assert executor_state["unit_substeps"][0]["snippet_only_candidate_ids"]
    assert executor_state["unit_substeps"][0]["retrieved_source_ids"]
    assert executor_state["unit_substeps"][0]["evidence_ids"]
    assert executor_state["unit_substeps"][0]["finding_ids"]
    assert executor_state["unit_substeps"][0]["legacy_adapter_used"] is False


@pytest.mark.asyncio
async def test_langgraph_research_unit_uses_legacy_only_as_fallback_retriever():
    _FakeOrchestrator.task_descriptions = []
    initial_state = build_initial_research_graph(
        task_id="task-langgraph-legacy-fallback",
        query="Verify a company designation with primary sources",
        max_units=1,
    )
    stream_queue = _CaptureQueue()

    result = await research_langgraph_module.execute_langgraph_research(
        state=initial_state,
        orchestrator=_FakeOrchestrator(stream_queue=stream_queue),
        original_task_description="Verify a company designation with primary sources",
        task_file_name="",
        task_id="task-langgraph-legacy-fallback",
        is_final_retry=False,
        stream_queue=stream_queue,
    )

    assert len(_FakeOrchestrator.task_descriptions) == 1
    assert "## Research Unit Node" in _FakeOrchestrator.task_descriptions[0]
    assert "## Limira Research Workflow" not in _FakeOrchestrator.task_descriptions[0]
    assert any(
        candidate.source_type == "legacy_agent_adapter"
        for candidate in result.state.source_candidates
    )
    assert any(
        source.source_type == "legacy_agent_adapter"
        for source in result.state.retrieved_sources
    )
    assert any(
        item.source_type == "legacy_agent_adapter"
        for item in result.state.evidence
    )

    typed_events = [
        (index, item.get("type"), item.get("payload", {}).get("source_type"))
        for index, item in enumerate(stream_queue.items)
        if item.get("type")
    ]
    legacy_candidate_index = next(
        index
        for index, event_type, source_type in typed_events
        if event_type == "source_candidate_collected"
        and source_type == "legacy_agent_adapter"
    )
    legacy_retrieved_index = next(
        index
        for index, event_type, source_type in typed_events
        if event_type == "retrieved_source_collected"
        and source_type == "legacy_agent_adapter"
    )
    legacy_evidence_index = next(
        index
        for index, event_type, source_type in typed_events
        if event_type == "evidence_collected"
        and source_type == "legacy_agent_adapter"
    )
    assert legacy_candidate_index < legacy_retrieved_index < legacy_evidence_index

    research_checkpoint = next(
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
        and item["data"]["phase"] == "research"
    )
    executor_state = research_checkpoint["executor_state"]
    assert executor_state["legacy_adapter_calls"] == 1
    assert executor_state["unit_substeps"][0]["legacy_adapter_used"] is True
    assert executor_state["unit_substeps"][0]["steps"] == [
        "search",
        "retrieve",
        "promote",
        "synthesize",
    ]


@pytest.mark.asyncio
async def test_langgraph_uploaded_document_retriever_searches_by_unit_query_terms():
    _FakeOrchestrator.task_descriptions = []
    matching_text = "Program X exposure is confirmed in this uploaded memo."
    unrelated_text = "This document discusses unrelated market expansion."
    initial_state = build_initial_research_graph(
        task_id="task-langgraph-upload-search",
        query="Investigate program X exposure",
        document_ids=["doc-match", "doc-miss"],
        upload_scope={
            "document_count": 2,
            "retrieval_status": "retrieved",
            "retrieved_document_ids": ["doc-match", "doc-miss"],
            "source_payloads": [
                {
                    "candidate_id": "SRC-UPLOAD-MATCH",
                    "document_id": "doc-match",
                    "chunk_id": "UPLOAD-CHUNK-MATCH",
                    "filename": "match.txt",
                    "source_content_state": "content_bearing",
                    "retrieval_status": "retrieved",
                    "retrieved_at": "2026-06-06T12:00:00+00:00",
                    "content_hash": "c" * 64,
                    "text": matching_text,
                },
                {
                    "candidate_id": "SRC-UPLOAD-MISS",
                    "document_id": "doc-miss",
                    "chunk_id": "UPLOAD-CHUNK-MISS",
                    "filename": "miss.txt",
                    "source_content_state": "content_bearing",
                    "retrieval_status": "retrieved",
                    "retrieved_at": "2026-06-06T12:01:00+00:00",
                    "content_hash": "d" * 64,
                    "text": unrelated_text,
                },
            ],
        },
        max_units=1,
    )
    stream_queue = _CaptureQueue()

    result = await research_langgraph_module.execute_langgraph_research(
        state=initial_state,
        orchestrator=_FakeOrchestrator(stream_queue=stream_queue),
        original_task_description="Investigate program X exposure",
        task_file_name="",
        task_id="task-langgraph-upload-search",
        is_final_retry=False,
        stream_queue=stream_queue,
    )

    assert _FakeOrchestrator.task_descriptions == []
    assert {
        candidate.document_id
        for candidate in result.state.source_candidates
        if candidate.source_type == "limira_upload"
    } == {"doc-match"}
    assert {
        source.document_id
        for source in result.state.retrieved_sources
        if source.source_type == "limira_upload"
    } == {"doc-match"}
    assert {
        item.document_id
        for item in result.state.evidence
        if item.source_type == "limira_upload"
    } == {"doc-match"}
    assert matching_text in result.final_summary
    assert unrelated_text not in result.final_summary

    research_checkpoint = next(
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
        and item["data"]["phase"] == "research"
    )
    assert research_checkpoint["executor_state"]["unit_substeps"][0][
        "retriever_order"
    ] == [
        "web_search",
        "page_visit_or_jina_summary",
        "uploaded_document_search",
        "legacy_agent_adapter",
    ]


@pytest.mark.asyncio
async def test_langgraph_upload_retriever_rejects_out_of_scope_source_payloads():
    _FakeOrchestrator.task_descriptions = []
    unowned_text = "Unowned upload payload claims program X exposure is confirmed."
    initial_state = build_initial_research_graph(
        task_id="task-langgraph-upload-scope",
        query="Investigate program X exposure",
        document_ids=["doc-owned"],
        upload_scope={
            "document_count": 2,
            "retrieval_status": "retrieved",
            "retrieved_document_ids": ["doc-owned", "doc-unowned"],
            "source_payloads": [
                {
                    "candidate_id": "SRC-UPLOAD-UNOWNED",
                    "document_id": "doc-unowned",
                    "chunk_id": "UPLOAD-CHUNK-UNOWNED",
                    "filename": "unowned.txt",
                    "source_content_state": "content_bearing",
                    "retrieval_status": "retrieved",
                    "retrieved_at": "2026-06-06T12:02:00+00:00",
                    "content_hash": "e" * 64,
                    "text": unowned_text,
                }
            ],
        },
        source_policy={"prefer_uploaded_documents": True},
        max_units=1,
    )
    assert initial_state.upload_scope.retrieved_document_ids == ["doc-owned"]
    assert initial_state.upload_scope.source_payload_count == 0
    assert initial_state.upload_scope.source_payload_refs == []
    assert initial_state.upload_sources == []

    stream_queue = _CaptureQueue()
    result = await research_langgraph_module.execute_langgraph_research(
        state=initial_state,
        orchestrator=_FakeOrchestrator(stream_queue=stream_queue),
        original_task_description="Investigate program X exposure",
        task_file_name="",
        task_id="task-langgraph-upload-scope",
        is_final_retry=False,
        stream_queue=stream_queue,
    )

    assert {
        candidate.document_id
        for candidate in result.state.source_candidates
        if candidate.source_type == "limira_upload"
    } == set()
    assert {
        source.document_id
        for source in result.state.retrieved_sources
        if source.source_type == "limira_upload"
    } == set()
    assert {
        item.document_id
        for item in result.state.evidence
        if item.source_type == "limira_upload"
    } == set()
    assert unowned_text not in result.final_summary
    assert "doc-unowned" not in repr(stream_queue.items)


@pytest.mark.asyncio
async def test_langgraph_retriever_order_respects_source_policy_priorities():
    _FakeOrchestrator.task_descriptions = []
    initial_state = build_initial_research_graph(
        task_id="task-langgraph-policy-priority",
        query="Assess scenario source priority",
        source_policy={
            "prefer_uploaded_documents": True,
            "prefer_scenario_sources": True,
        },
        max_units=1,
    )
    stream_queue = _CaptureQueue()

    result = await research_langgraph_module.execute_langgraph_research(
        state=initial_state,
        orchestrator=_FakeOrchestrator(stream_queue=stream_queue),
        original_task_description="Assess scenario source priority",
        task_file_name="",
        task_id="task-langgraph-policy-priority",
        is_final_retry=False,
        stream_queue=stream_queue,
    )

    assert _FakeOrchestrator.task_descriptions == []
    assert any(
        source.source_type == "page_visit_or_jina_summary"
        for source in result.state.retrieved_sources
    )
    research_checkpoint = next(
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
        and item["data"]["phase"] == "research"
    )
    assert research_checkpoint["executor_state"]["unit_substeps"][0][
        "retriever_order"
    ] == [
        "uploaded_document_search",
        "page_visit_or_jina_summary",
        "web_search",
        "legacy_agent_adapter",
    ]
    assert research_checkpoint["executor_state"]["legacy_adapter_calls"] == 0


@pytest.mark.asyncio
async def test_langgraph_unknown_or_disabled_retrievers_emit_warnings():
    _FakeOrchestrator.task_descriptions = []
    registry = RetrieverRegistry()
    registry.register(WebSearchRetriever(), enabled=False)
    registry.register(UploadedDocumentSearchRetriever())
    node = LangGraphResearchUnitNode(
        retriever_registry=registry,
        retriever_names=[
            "unknown_retriever",
            "web_search",
            "uploaded_document_search",
        ],
    )
    state = build_initial_research_graph(
        task_id="task-langgraph-retriever-warning",
        query="Assess context-only upload",
        document_ids=["doc-empty"],
        upload_scope={
            "document_count": 1,
            "retrieval_status": "context_only",
            "context_only_document_ids": ["doc-empty"],
            "source_payloads": [],
        },
        max_units=1,
    )
    context = ResearchGraphExecutionContext(
        orchestrator=_FakeOrchestrator(),
        original_task_description="Assess context-only upload",
        task_id="task-langgraph-retriever-warning",
    )

    output = await node.run(state, context, ResearchGraphNodeOutput(state=state))

    assert _FakeOrchestrator.task_descriptions == []
    assert output.state.evidence == []
    assert output.executor_state["retriever_warnings"]
    assert any(
        "unknown_retriever:unknown_retriever" in warning
        for warning in output.executor_state["retriever_warnings"]
    )
    assert any(
        "disabled_retriever:web_search" in warning
        for warning in output.executor_state["retriever_warnings"]
    )
    assert output.executor_state["unit_substeps"][0]["warnings"] == (
        output.executor_state["retriever_warnings"]
    )
    assert output.state.warnings[-2:] == output.executor_state["retriever_warnings"]


def test_evidence_id_for_source_is_stable_per_task_source_and_index():
    first = evidence_id_for_source(task_id="task-a", source="https://example.test", index=0)
    second = evidence_id_for_source(task_id="task-a", source="https://example.test", index=0)
    different_index = evidence_id_for_source(
        task_id="task-a",
        source="https://example.test",
        index=1,
    )

    assert first == second
    assert first.startswith("EVID-")
    assert first != different_index


@pytest.mark.asyncio
async def test_pipeline_emits_research_graph_bootstrap_before_legacy_executor(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    cfg = _pipeline_cfg()
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=cfg,
        task_id="task-pipeline-graph",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
        research_context={
            "scenario": "sanctions_export_controls",
            "document_ids": ["doc-a"],
            "upload_scope": {
                "document_count": 1,
                "retrieval_status": "retrieved",
                "retrieved_document_ids": ["doc-a"],
            },
            "source_policy": {"min_sources": 5},
        },
    )

    assert result[0] == "summary"
    assert [item["event"] for item in stream_queue.items[:4]] == [
        "research_graph_executor_selected",
        "research_brief_created",
        "research_plan_created",
        "message",
    ]
    assert stream_queue.items[0]["data"]["research_graph_executor"] == "legacy"
    assert (
        stream_queue.items[1]["data"]["brief"]["original_query"]
        == "Verify a company designation with primary sources"
    )
    assert stream_queue.items[2]["data"]["plan"]["research_units"]
    assert len(_FakeOrchestrator.task_descriptions) == 1
    assert "## Limira Research Workflow" in _FakeOrchestrator.task_descriptions[0]
    assert "### Research Units" in _FakeOrchestrator.task_descriptions[0]
    assert "sanctions_export_controls" in _FakeOrchestrator.task_descriptions[0]
    assert "1 attached upload source(s)" in _FakeOrchestrator.task_descriptions[0]
    assert "Upload retrieval status: retrieved" in _FakeOrchestrator.task_descriptions[0]
    assert "Use retrieved upload text for document IDs: doc-a" in _FakeOrchestrator.task_descriptions[0]
    assert "at least 5" in _FakeOrchestrator.task_descriptions[0]
    assert not any(item["event"] == "research_graph_phase" for item in stream_queue.items)


@pytest.mark.asyncio
async def test_pipeline_routes_to_graph_executor_when_feature_flag_enabled(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    async def fake_execute_research_graph(**kwargs):
        await kwargs["stream_queue"].put(
            {
                "event": "test_graph_executor_used",
                "data": {"task_id": kwargs["task_id"]},
            }
        )
        return ResearchGraphExecutionResult(
            state=kwargs["state"],
            final_summary="graph summary",
            final_boxed_answer="graph final",
            failure_experience_summary="retry context",
        )

    monkeypatch.setattr(
        pipeline_module,
        "execute_research_graph",
        fake_execute_research_graph,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-enabled",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[0] == "graph summary"
    assert result[1] == "graph final"
    assert result[3] == "retry context"
    assert _FakeOrchestrator.task_descriptions == []
    assert stream_queue.items[-1]["event"] == "test_graph_executor_used"


@pytest.mark.asyncio
async def test_pipeline_routes_to_explicit_legacy_executor(tmp_path, monkeypatch):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    async def fail_serial_executor(**_kwargs):
        raise AssertionError("serial executor should not run")

    def fail_langgraph_loader():
        raise AssertionError("langgraph executor should not run")

    monkeypatch.setattr(
        pipeline_module,
        "execute_research_graph",
        fail_serial_executor,
    )
    monkeypatch.setattr(
        pipeline_module,
        "_load_langgraph_executor",
        fail_langgraph_loader,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True, graph_executor="legacy"),
        task_id="task-pipeline-explicit-legacy",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[0] == "summary"
    assert _FakeOrchestrator.task_descriptions
    assert stream_queue.items[0]["data"]["research_graph_executor"] == "legacy"
    assert stream_queue.items[-1]["event"] == "message"


@pytest.mark.asyncio
async def test_pipeline_routes_to_explicit_serial_executor(tmp_path, monkeypatch):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    async def fake_serial_executor(**kwargs):
        await kwargs["stream_queue"].put(
            {
                "event": "test_serial_executor_used",
                "data": {"task_id": kwargs["task_id"]},
            }
        )
        return ResearchGraphExecutionResult(
            state=kwargs["state"],
            final_summary="serial summary",
            final_boxed_answer="serial final",
            failure_experience_summary=None,
        )

    def fail_langgraph_loader():
        raise AssertionError("langgraph executor should not run")

    monkeypatch.setattr(
        pipeline_module,
        "execute_research_graph",
        fake_serial_executor,
    )
    monkeypatch.setattr(
        pipeline_module,
        "_load_langgraph_executor",
        fail_langgraph_loader,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_executor="serial"),
        task_id="task-pipeline-explicit-serial",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[0] == "serial summary"
    assert result[1] == "serial final"
    assert _FakeOrchestrator.task_descriptions == []
    assert stream_queue.items[0]["data"]["research_graph_executor"] == "serial"
    assert stream_queue.items[-1]["event"] == "test_serial_executor_used"


@pytest.mark.asyncio
async def test_pipeline_routes_to_explicit_langgraph_executor(tmp_path, monkeypatch):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    async def fail_serial_executor(**_kwargs):
        raise AssertionError("serial executor should not run")

    async def fake_langgraph_executor(**kwargs):
        assert kwargs["evidence_strict_mode"] == EvidenceStrictMode.BLOCK
        assert kwargs["state"].evidence_strict_mode == EvidenceStrictMode.BLOCK
        await kwargs["stream_queue"].put(
            {
                "event": "test_langgraph_executor_used",
                "data": {"task_id": kwargs["task_id"]},
            }
        )
        return ResearchGraphExecutionResult(
            state=kwargs["state"],
            final_summary="langgraph summary",
            final_boxed_answer="langgraph final",
            failure_experience_summary="langgraph retry",
        )

    monkeypatch.setattr(
        pipeline_module,
        "execute_research_graph",
        fail_serial_executor,
    )
    monkeypatch.setattr(
        pipeline_module,
        "_load_langgraph_executor",
        lambda: fake_langgraph_executor,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_executor="langgraph", evidence_strict="block"),
        task_id="task-pipeline-explicit-langgraph",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[0] == "langgraph summary"
    assert result[1] == "langgraph final"
    assert result[3] == "langgraph retry"
    assert _FakeOrchestrator.task_descriptions == []
    assert stream_queue.items[0]["data"]["research_graph_executor"] == "langgraph"
    assert stream_queue.items[-1]["event"] == "test_langgraph_executor_used"


@pytest.mark.asyncio
async def test_pipeline_rejects_invalid_graph_executor(tmp_path, monkeypatch):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_executor="bogus"),
        task_id="task-pipeline-invalid-executor",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert "invalid_research_graph_executor" in result[0]
    assert "langgraph, legacy, serial" in result[0]
    assert result[1] == ""
    assert _FakeOrchestrator.task_descriptions == []
    assert stream_queue.items[-1]["event"] == "error"
    assert "invalid_research_graph_executor" in stream_queue.items[-1]["data"]["error"]


@pytest.mark.asyncio
async def test_pipeline_rejects_invalid_evidence_strict_mode(tmp_path, monkeypatch):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True, evidence_strict="silent"),
        task_id="task-pipeline-invalid-evidence-strict",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert "invalid_evidence_strict_mode" in result[0]
    assert result[1] == ""
    assert _FakeOrchestrator.task_descriptions == []
    assert stream_queue.items[-1]["event"] == "error"
    assert "invalid_evidence_strict_mode" in stream_queue.items[-1]["data"]["error"]


@pytest.mark.asyncio
async def test_langgraph_loader_failure_fails_clear_route_error(tmp_path, monkeypatch):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    def fail_langgraph_loader():
        raise RuntimeError("langgraph_executor_unavailable: missing dependency")

    monkeypatch.setattr(
        pipeline_module,
        "_load_langgraph_executor",
        fail_langgraph_loader,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_executor="langgraph"),
        task_id="task-pipeline-langgraph-unavailable",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert "langgraph_executor_unavailable" in result[0]
    assert result[1] == ""
    assert _FakeOrchestrator.task_descriptions == []
    assert stream_queue.items[0]["data"]["research_graph_executor"] == "langgraph"
    assert stream_queue.items[-1]["event"] == "error"
    assert "langgraph_executor_unavailable" in stream_queue.items[-1]["data"]["error"]


@pytest.mark.asyncio
async def test_langgraph_route_is_not_satisfied_by_serial_executor_patch(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    async def fake_serial_executor(**kwargs):
        return ResearchGraphExecutionResult(
            state=kwargs["state"],
            final_summary="serial summary",
            final_boxed_answer="serial final",
            failure_experience_summary=None,
        )

    monkeypatch.setattr(
        pipeline_module,
        "execute_research_graph",
        fake_serial_executor,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_executor="langgraph"),
        task_id="task-pipeline-langgraph-not-serial",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert "## Verified Claims" in result[0]
    assert result[1] == "summary"
    assert "serial summary" not in result[0]
    assert len(_FakeOrchestrator.task_descriptions) == 4
    assert all(
        "## Research Unit Node" in task_description
        for task_description in _FakeOrchestrator.task_descriptions
    )
    assert stream_queue.items[0]["data"]["research_graph_executor"] == "langgraph"
    checkpoints = [
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
    ]
    assert [item["phase"] for item in checkpoints] == [
        "scope",
        "plan",
        "research",
        "compress",
        "verify",
        "write",
        "reconcile",
        "complete",
    ]
    assert all(
        checkpoint["research_graph_executor"] == "langgraph"
        for checkpoint in checkpoints
    )
    assert all(
        checkpoint["executor_state"]["research_graph_executor"] == "langgraph"
        for checkpoint in checkpoints
    )
    report_events = [
        item
        for item in stream_queue.items
        if item.get("type") == "report_section_generated"
    ]
    assert report_events
    final_messages = [
        item
        for item in stream_queue.items
        if item.get("event") == "message"
        and item.get("data", {}).get("source_event_type") == "research_langgraph"
    ]
    assert final_messages
    assert "## Verified Claims" in final_messages[-1]["data"]["delta"]["content"]


@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_emits_serial_phase_events(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-phases",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert "## Verified Claims" in result[0]
    assert result[1] == "summary"
    assert [item["event"] for item in stream_queue.items[:3]] == [
        "research_graph_executor_selected",
        "research_brief_created",
        "research_plan_created",
    ]
    assert stream_queue.items[0]["data"]["research_graph_executor"] == "serial"
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item["event"] == "research_graph_phase"
    ] == [
        "scope",
        "plan",
        "research",
        "compress",
        "verify",
        "write",
        "reconcile",
        "complete",
    ]
    checkpoints = [
        item["data"]
        for item in stream_queue.items
        if item["event"] == "research_graph_checkpoint"
    ]
    assert [item["phase"] for item in checkpoints] == [
        "scope",
        "plan",
        "research",
        "compress",
        "verify",
        "write",
        "reconcile",
        "complete",
    ]
    for checkpoint in checkpoints:
        assert set(checkpoint) == {
            "task_id",
            "phase",
            "status",
            "current_research_unit",
            "source_ledger",
            "evidence_ledger",
            "executor_state",
            "research_graph_executor",
            "resume_policy",
            "recoverable_reason",
        }
        assert checkpoint["task_id"] == "task-pipeline-graph-phases"
        assert checkpoint["research_graph_executor"] == "serial"
        assert isinstance(checkpoint["source_ledger"], list)
        assert isinstance(checkpoint["evidence_ledger"], list)
        assert isinstance(checkpoint["executor_state"], dict)
        assert checkpoint["executor_state"]["research_graph_executor"] == "serial"
    research_checkpoint = next(
        item for item in checkpoints if item["phase"] == "research"
    )
    assert research_checkpoint["current_research_unit"].startswith("unit-4-")
    assert len(research_checkpoint["evidence_ledger"]) == 4
    assert any(
        item.get("retrieved_source_id", "").startswith("RSRC-")
        for item in research_checkpoint["source_ledger"]
    )
    assert [
        item["type"]
        for item in stream_queue.items
        if item.get("type")
        in {
            "retrieved_source_collected",
            "evidence_collected",
            "finding_collected",
            "verified_claim_collected",
        }
    ][:4] == [
        "retrieved_source_collected",
        "evidence_collected",
        "retrieved_source_collected",
        "evidence_collected",
    ]
    verify_checkpoint = next(
        item for item in checkpoints if item["phase"] == "verify"
    )
    assert any(
        item.get("claim_id", "").startswith("claim-")
        and item.get("support_type") == "supported"
        and item.get("evidence_ids")
        for item in verify_checkpoint["evidence_ledger"]
    )
    assert verify_checkpoint["executor_state"]["verified_claims"][0]["support_type"] == "supported"
    complete_checkpoint = checkpoints[-1]
    assert complete_checkpoint["status"] == "completed"
    assert complete_checkpoint["resume_policy"] == "terminal"
    assert complete_checkpoint["recoverable_reason"] is None
    report_events = [
        item
        for item in stream_queue.items
        if item.get("type") == "report_section_generated"
    ]
    assert len(report_events) == 1
    report_payload = report_events[0]["payload"]
    assert report_payload["section_id"] == "REPORT-GRAPH-FINAL"
    assert report_payload["title"] == "Final graph report"
    assert "## Verified Claims" in report_payload["markdown"]
    assert report_payload["content"] == report_payload["markdown"]
    assert report_payload["evidence_refs"]
    assert report_payload["source_event_type"] == "research_graph"
    assert any(item["event"] == "message" for item in stream_queue.items)
    assert len(_FakeOrchestrator.task_descriptions) == 4
    assert all(
        "## Research Unit Node" in task_description
        for task_description in _FakeOrchestrator.task_descriptions
    )
    assert all(
        "## Limira Research Workflow" not in task_description
        for task_description in _FakeOrchestrator.task_descriptions
    )


@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_retrieves_upload_sources(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)
    upload_text = "Uploaded memo states the entity is listed under program X."
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-upload-source",
        task_description="Assess uploaded document evidence",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
        research_context={
            "document_ids": ["doc-upload"],
            "upload_scope": {
                "document_count": 1,
                "retrieval_status": "retrieved",
                "retrieved_document_ids": ["doc-upload"],
                "context_only_document_ids": [],
                "source_payloads": [
                    {
                        "candidate_id": "SRC-UPLOAD-001",
                        "document_id": "doc-upload",
                        "attached_document_id": "attached-doc-upload",
                        "chunk_id": "UPLOAD-CHUNK-001",
                        "filename": "memo.txt",
                        "source_type": "limira_upload",
                        "source_content_state": "content_bearing",
                        "retrieval_status": "retrieved",
                        "retrieved_at": "2026-06-06T12:00:00+00:00",
                        "content_hash": "a" * 64,
                        "snippet": upload_text,
                        "text": upload_text,
                        "text_char_count": len(upload_text),
                    }
                ],
            },
            "source_policy": {"min_sources": 3},
        },
    )

    assert upload_text in result[0]
    upload_events = [
        item
        for item in stream_queue.items
        if item.get("payload", {}).get("source_type") == "limira_upload"
    ]
    assert [item["type"] for item in upload_events[:3]] == [
        "source_candidate_collected",
        "retrieved_source_collected",
        "evidence_collected",
    ]
    retrieved_payload = upload_events[1]["payload"]
    evidence_payload = upload_events[2]["payload"]
    assert retrieved_payload["document_id"] == "doc-upload"
    assert retrieved_payload["chunk_id"] == "UPLOAD-CHUNK-001"
    assert retrieved_payload["content_hash"] == "a" * 32
    assert scrub_secrets(retrieved_payload)["content_hash"] == "a" * 32
    assert retrieved_payload["retrieved_at"] == "2026-06-06T12:00:00+00:00"
    assert retrieved_payload["tool_name"] == "uploaded_document_source_provider"
    assert evidence_payload["document_id"] == "doc-upload"
    assert evidence_payload["chunk_id"] == "UPLOAD-CHUNK-001"
    assert evidence_payload["summary"] == upload_text
    assert evidence_payload["content_hash"] == "a" * 32
    assert scrub_secrets(evidence_payload)["content_hash"] == "a" * 32

    research_checkpoint = next(
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
        and item["data"]["phase"] == "research"
    )
    assert research_checkpoint["executor_state"]["upload_source_count"] == 1
    assert any(
        item.get("source_type") == "limira_upload"
        and item.get("chunk_id") == "UPLOAD-CHUNK-001"
        and item.get("content_hash") == "a" * 32
        for item in research_checkpoint["source_ledger"]
    )
    assert any(
        item.get("source_type") == "limira_upload"
        and item.get("chunk_id") == "UPLOAD-CHUNK-001"
        and item.get("content_hash") == "a" * 32
        for item in research_checkpoint["evidence_ledger"]
    )
    assert len(_FakeOrchestrator.task_descriptions) == 4


@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_bounds_long_research_summaries_and_hashes(
    tmp_path, monkeypatch
):
    _LongSummaryOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _LongSummaryOrchestrator)
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-long-summary",
        task_description="Summarize a long research output",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert "## Verified Claims" in result[0]
    retrieved_payloads = [
        item["payload"]
        for item in stream_queue.items
        if item.get("type") == "retrieved_source_collected"
        and item["payload"].get("source_type") == "graph_research_unit"
    ]
    evidence_payloads = [
        item["payload"]
        for item in stream_queue.items
        if item.get("type") == "evidence_collected"
        and item["payload"].get("source_type") == "graph_research_unit"
    ]
    finding_payloads = [
        item["payload"]
        for item in stream_queue.items
        if item.get("type") == "finding_collected"
    ]
    claim_payloads = [
        item["payload"]
        for item in stream_queue.items
        if item.get("type") == "verified_claim_collected"
    ]

    assert retrieved_payloads
    assert evidence_payloads
    assert finding_payloads
    assert claim_payloads
    assert all(len(payload["summary"]) == 20_000 for payload in retrieved_payloads)
    assert all(len(payload["summary"]) == 20_000 for payload in evidence_payloads)
    assert all(len(payload["summary"]) == 10_000 for payload in finding_payloads)
    assert all(len(payload["claim"]) == 10_000 for payload in claim_payloads)
    for payload in [*retrieved_payloads, *evidence_payloads]:
        assert len(payload["content_hash"]) == 32
        assert scrub_secrets(payload)["content_hash"] == payload["content_hash"]

    research_checkpoint = next(
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
        and item["data"]["phase"] == "research"
    )
    checkpoint_hashes = [
        item["content_hash"]
        for item in [
            *research_checkpoint["source_ledger"],
            *research_checkpoint["evidence_ledger"],
        ]
        if item.get("content_hash")
    ]
    assert checkpoint_hashes
    assert all(len(content_hash) == 32 for content_hash in checkpoint_hashes)
    assert len(_LongSummaryOrchestrator.task_descriptions) == 4


@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_does_not_promote_context_only_uploads(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-context-only-upload",
        task_description="Assess context-only upload",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
        research_context={
            "document_ids": ["doc-empty"],
            "upload_scope": {
                "document_count": 1,
                "retrieval_status": "context_only",
                "retrieved_document_ids": [],
                "context_only_document_ids": ["doc-empty"],
                "source_payloads": [],
            },
        },
    )

    assert "## Verified Claims" in result[0]
    assert not any(
        item.get("payload", {}).get("source_type") == "limira_upload"
        for item in stream_queue.items
    )
    research_checkpoint = next(
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
        and item["data"]["phase"] == "research"
    )
    assert research_checkpoint["executor_state"]["upload_source_count"] == 0
    assert research_checkpoint["executor_state"]["context_only_upload_document_ids"] == [
        "doc-empty"
    ]
    assert all(
        item.get("source_type") != "limira_upload"
        for item in research_checkpoint["evidence_ledger"]
    )
    assert len(_FakeOrchestrator.task_descriptions) == 4


@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_failures_use_pipeline_error_handling(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FailingOrchestrator)
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-failure",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[1] == ""
    assert "RuntimeError" in result[0]
    assert "graph executor failure" in result[0]
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item["event"] == "research_graph_phase"
    ] == ["scope", "plan", "research"]


@pytest.mark.parametrize(
    "final_outputs",
    [
        ("", "", None),
        ("   ", "\n\t", None),
        (None, "final", None),
    ],
)
@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_rejects_missing_research_outputs(
    tmp_path, monkeypatch, final_outputs
):
    _MissingFinalOutputOrchestrator.outputs = final_outputs
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(
        pipeline_module,
        "Orchestrator",
        _MissingFinalOutputOrchestrator,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-missing-output",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[1] == ""
    assert "research_graph_research_output_required" in result[0]
    error_events = [
        item for item in stream_queue.items if item.get("event") == "error"
    ]
    assert len(error_events) == 1
    assert error_events[0]["data"] == {
        "task_id": "task-pipeline-graph-missing-output",
        "phase": "research",
        "error": "research_graph_research_output_required",
    }
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item["event"] == "research_graph_phase"
    ] == ["scope", "plan", "research"]


@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_rejects_missing_verifier_output(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(
        research_graph_module.VerifierNode,
        "_verify_claims",
        lambda self, state: [],
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-missing-verifier",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[1] == ""
    assert "research_graph_verifier_output_required" in result[0]
    error_events = [
        item for item in stream_queue.items if item.get("event") == "error"
    ]
    assert error_events[-1]["data"] == {
        "task_id": "task-pipeline-graph-missing-verifier",
        "phase": "verify",
        "error": "research_graph_verifier_output_required",
    }
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item["event"] == "research_graph_phase"
    ] == ["scope", "plan", "research", "compress", "verify"]
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item["event"] == "research_graph_checkpoint"
    ] == ["scope", "plan", "research", "compress"]


@pytest.mark.parametrize(
    "evidence_ids",
    [
        None,
        [],
        ["EVID-999"],
        ["EVID-1"],
    ],
)
@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_rejects_verified_claim_without_valid_evidence(
    tmp_path, monkeypatch, evidence_ids
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    def fake_verify_claims(self, state):
        return [
            VerifiedClaim.model_construct(
                id="claim-invalid-evidence",
                claim="Claim is not linked to valid graph evidence.",
                support_type="supported",
                evidence_ids=evidence_ids,
                rationale="Bypass model validation to exercise runtime graph guard.",
                confidence=0.9,
            )
        ]

    monkeypatch.setattr(
        research_graph_module.VerifierNode,
        "_verify_claims",
        fake_verify_claims,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-invalid-claim-evidence",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[1] == ""
    assert "research_graph_verified_claim_evidence_required" in result[0]
    error_events = [
        item for item in stream_queue.items if item.get("event") == "error"
    ]
    assert error_events[-1]["data"] == {
        "task_id": "task-pipeline-graph-invalid-claim-evidence",
        "phase": "verify",
        "error": "research_graph_verified_claim_evidence_required",
    }
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item["event"] == "research_graph_phase"
    ] == ["scope", "plan", "research", "compress", "verify"]
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item["event"] == "research_graph_checkpoint"
    ] == ["scope", "plan", "research", "compress"]
    assert not any(
        item.get("type") == "verified_claim_collected"
        for item in stream_queue.items
    )


@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_warns_for_missing_report_refs(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    def compose_report_with_bad_refs(self, state):
        known_ref = state.verified_claims[0].evidence_ids[0]
        return (
            f"## Answer\nKnown {known_ref}; missing EVID-999; malformed EVID-abc.",
            "Strict-mode warn answer",
        )

    monkeypatch.setattr(
        research_graph_module.WriterNode,
        "_compose_report",
        compose_report_with_bad_refs,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-evidence-strict-warn",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[1] == "Strict-mode warn answer"
    warnings = [
        item["payload"]
        for item in stream_queue.items
        if item.get("type") == "artifact_warning"
    ]
    assert [
        warning["warning"]
        for warning in warnings
        if warning["artifact_type"] == "report_section"
    ] == ["invalid_evidence_refs", "unresolved_evidence_refs"]
    assert warnings[-2]["evidence_refs"] == ["EVID-abc"]
    assert warnings[-1]["evidence_refs"] == ["EVID-999"]
    report_payload = next(
        item["payload"]
        for item in stream_queue.items
        if item.get("type") == "report_section_generated"
    )
    assert "EVID-999" in report_payload["evidence_refs"]
    assert "EVID-abc" not in report_payload["evidence_refs"]
    write_checkpoint = next(
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
        and item["data"]["phase"] == "write"
    )
    assert write_checkpoint["executor_state"]["evidence_strict_mode"] == "warn"
    assert len(write_checkpoint["executor_state"]["evidence_ref_warnings"]) == 2
    complete_checkpoint = [
        item["data"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
    ][-1]
    assert complete_checkpoint["status"] == "completed"


@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_blocks_missing_report_refs(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    def compose_report_with_bad_refs(self, state):
        known_ref = state.verified_claims[0].evidence_ids[0]
        return (
            f"## Answer\nKnown {known_ref}; missing EVID-999; malformed EVID-abc.",
            "Strict-mode block answer",
        )

    monkeypatch.setattr(
        research_graph_module.WriterNode,
        "_compose_report",
        compose_report_with_bad_refs,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True, evidence_strict="block"),
        task_id="task-pipeline-evidence-strict-block",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[1] == ""
    assert "research_graph_evidence_strict_block" in result[0]
    assert "EVID-999" in result[0]
    assert "EVID-abc" in result[0]
    error_events = [
        item for item in stream_queue.items if item.get("event") == "error"
    ]
    assert error_events[-1]["data"]["phase"] == "write"
    assert "research_graph_evidence_strict_block" in error_events[-1]["data"]["error"]
    assert not any(
        item.get("type") == "report_section_generated"
        for item in stream_queue.items
    )
    assert not any(
        item.get("type") == "artifact_warning"
        for item in stream_queue.items
    )
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
    ] == ["scope", "plan", "research", "compress", "verify"]


@pytest.mark.asyncio
async def test_feature_flagged_graph_executor_rejects_missing_writer_output(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(
        research_graph_module.WriterNode,
        "_compose_report",
        lambda self, state: ("", ""),
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_enabled=True),
        task_id="task-pipeline-graph-missing-writer",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[1] == ""
    assert "research_graph_writer_output_required" in result[0]
    error_events = [
        item for item in stream_queue.items if item.get("event") == "error"
    ]
    assert error_events[-1]["data"] == {
        "task_id": "task-pipeline-graph-missing-writer",
        "phase": "write",
        "error": "research_graph_writer_output_required",
    }
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item["event"] == "research_graph_phase"
    ] == ["scope", "plan", "research", "compress", "verify", "write"]
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item["event"] == "research_graph_checkpoint"
    ] == ["scope", "plan", "research", "compress", "verify"]
    assert not any(
        item.get("type") == "report_section_generated"
        for item in stream_queue.items
    )
