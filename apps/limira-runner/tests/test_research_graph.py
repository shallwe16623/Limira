import pytest
from omegaconf import OmegaConf

from archive_writer import scrub_secrets
from src.core import pipeline as pipeline_module
from src.core import research_graph as research_graph_module
from src.core.research_graph import (
    ResearchGraphExecutionResult,
    ResearchPhase,
    VerifiedClaim,
    build_initial_research_graph,
    evidence_id_for_source,
    graph_bootstrap_events,
    graph_task_description,
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


def _pipeline_cfg(*, graph_enabled: bool = False):
    agent_cfg = {
        "keep_tool_result": True,
        "main_agent": {"max_turns": 1},
        "sub_agents": None,
    }
    if graph_enabled:
        agent_cfg["research_graph"] = {"enabled": True}
    return OmegaConf.create(
        {
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
    )


def test_initial_research_graph_creates_bounded_scope_and_plan():
    state = build_initial_research_graph(
        task_id="task-graph",
        query="  Track BYD Section 1260H list status.  ",
        scenario="sanctions_export_controls",
        max_units=3,
    )

    assert state.phase == ResearchPhase.PLAN
    assert state.brief.original_query == "Track BYD Section 1260H list status."
    assert "sanctions_export_controls" in state.brief.scope
    assert len(state.plan.research_units) == 3
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
    assert state.context_only_upload_document_ids == ["doc-b"]
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
    assert [item["event"] for item in stream_queue.items[:3]] == [
        "research_brief_created",
        "research_plan_created",
        "message",
    ]
    assert (
        stream_queue.items[0]["data"]["brief"]["original_query"]
        == "Verify a company designation with primary sources"
    )
    assert stream_queue.items[1]["data"]["plan"]["research_units"]
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
    assert [item["event"] for item in stream_queue.items[:2]] == [
        "research_brief_created",
        "research_plan_created",
    ]
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
            "resume_policy",
            "recoverable_reason",
        }
        assert checkpoint["task_id"] == "task-pipeline-graph-phases"
        assert isinstance(checkpoint["source_ledger"], list)
        assert isinstance(checkpoint["evidence_ledger"], list)
        assert isinstance(checkpoint["executor_state"], dict)
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
        and item.get("support_type") == "supports"
        and item.get("evidence_ids")
        for item in verify_checkpoint["evidence_ledger"]
    )
    assert verify_checkpoint["executor_state"]["verified_claims"][0]["support_type"] == "supports"
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
                support_type="supports",
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
