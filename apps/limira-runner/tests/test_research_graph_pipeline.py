from types import SimpleNamespace

import pytest

from archive_writer import scrub_secrets
import pipeline_helpers
from src.core import pipeline as pipeline_module
from src.core import research_graph as research_graph_module
from src.core.research_graph import (
    ResearchGraphExecutionResult,
    ResearchPhase,
    VerifiedClaim,
)
from test_research_graph import (
    _CaptureQueue,
    _FailingOrchestrator,
    _FakeClientFactory,
    _FakeOrchestrator,
    _FakeToolManager,
    _LongSummaryOrchestrator,
    _MissingFinalOutputOrchestrator,
    _pipeline_cfg,
)


class _TrackingClientFactory:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.__class__.instances.append(self)

    def close(self):
        self.closed = True


def test_preloaded_pipeline_components_create_fresh_task_runtime(monkeypatch):
    created = []

    class _PreloadFakeToolManager:
        def __init__(self, name):
            self.name = name

        async def get_all_tool_definitions(self):
            return [{"name": self.name, "tools": []}]

    def fake_create_pipeline_components(_cfg):
        index = len(created)
        manager = _PreloadFakeToolManager(f"manager-{index}")
        formatter = object()
        created.append((manager, formatter))
        return manager, {}, formatter

    monkeypatch.setattr(
        pipeline_helpers,
        "_preload_cache",
        {
            "cfg": None,
            "tool_definitions": None,
            "sub_agent_tool_definitions": None,
            "loaded": False,
        },
    )
    monkeypatch.setattr(
        pipeline_helpers,
        "load_limira_config",
        lambda _overrides: SimpleNamespace(agent=SimpleNamespace(sub_agents=None)),
    )
    monkeypatch.setattr(
        pipeline_helpers,
        "create_pipeline_components",
        fake_create_pipeline_components,
    )

    pipeline_helpers._ensure_preloaded()
    first_runtime = pipeline_helpers._create_task_pipeline_components()
    second_runtime = pipeline_helpers._create_task_pipeline_components()

    assert pipeline_helpers._preload_cache["tool_definitions"] == [
        {"name": "manager-0", "tools": []}
    ]
    assert first_runtime[0] is not second_runtime[0]
    assert first_runtime[2] is not second_runtime[2]
    assert first_runtime[0] is not created[0][0]
    assert second_runtime[0] is not created[0][0]


@pytest.mark.asyncio
async def test_pipeline_applies_langgraph_resume_checkpoint_context(tmp_path, monkeypatch):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    async def fake_langgraph_executor(**kwargs):
        state = kwargs["state"]
        assert state.resume_from_checkpoint is True
        assert state.resume_start_phase == ResearchPhase.COMPRESS
        assert state.evidence[0].id == "EVID-001"
        return ResearchGraphExecutionResult(
            state=state,
            final_summary="resumed langgraph summary",
            final_boxed_answer="resumed langgraph final",
            failure_experience_summary=None,
        )

    monkeypatch.setattr(
        pipeline_module,
        "_load_langgraph_executor",
        lambda: fake_langgraph_executor,
    )
    stream_queue = _CaptureQueue()
    checkpoint = {
        "phase": "research",
        "status": "queued",
        "current_node": "compress",
        "research_graph_executor": "langgraph",
        "resume_policy": "resume_from_checkpoint",
        "completed_unit_ids": ["unit-1-verify-a-company-designation"],
        "pending_unit_ids": [],
        "source_ledger": [
            {
                "ledger_type": "research_unit",
                "unit_id": "unit-1-verify-a-company-designation",
                "status": "completed",
            }
        ],
        "evidence_ledger": [
            {
                "ledger_type": "evidence",
                "id": "EVID-001",
                "source_type": "web",
                "content_hash": "a" * 32,
                "retrieved_at": "2026-06-06T12:00:00+00:00",
                "quote_or_summary": "Checkpoint evidence.",
            }
        ],
    }

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_executor="langgraph"),
        task_id="task-pipeline-langgraph-resume",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        research_context={"resume_checkpoint": checkpoint},
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[0] == "resumed langgraph summary"
    assert result[1] == "resumed langgraph final"


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
async def test_pipeline_closes_llm_client_after_successful_legacy_execution(
    tmp_path, monkeypatch
):
    _TrackingClientFactory.instances = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _TrackingClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_executor="legacy"),
        task_id="task-pipeline-close-success",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[0] == "summary"
    assert len(_TrackingClientFactory.instances) == 1
    assert _TrackingClientFactory.instances[0].closed is True


@pytest.mark.asyncio
async def test_pipeline_closes_llm_client_when_legacy_orchestrator_fails(
    tmp_path, monkeypatch
):
    _TrackingClientFactory.instances = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _TrackingClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FailingOrchestrator)
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_executor="legacy"),
        task_id="task-pipeline-close-legacy-failure",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert "graph executor failure" in result[0]
    assert len(_TrackingClientFactory.instances) == 1
    assert _TrackingClientFactory.instances[0].closed is True
    assert all(item.get("event") != "error" for item in stream_queue.items)


@pytest.mark.asyncio
async def test_pipeline_closes_llm_client_when_langgraph_executor_fails(
    tmp_path, monkeypatch
):
    _TrackingClientFactory.instances = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _TrackingClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    async def fail_langgraph_executor(**_kwargs):
        raise RuntimeError("langgraph executor failure")

    monkeypatch.setattr(
        pipeline_module,
        "_load_langgraph_executor",
        lambda: fail_langgraph_executor,
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_pipeline_cfg(graph_executor="langgraph"),
        task_id="task-pipeline-close-langgraph-failure",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert "langgraph executor failure" in result[0]
    assert len(_TrackingClientFactory.instances) == 1
    assert _TrackingClientFactory.instances[0].closed is True
    assert all(item.get("event") != "error" for item in stream_queue.items)


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

    assert "## Key Findings" in result[0]
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
    assert "## Key Findings" in final_messages[-1]["data"]["delta"]["content"]


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

    assert "## Key Findings" in result[0]
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
            "last_completed_node",
            "current_node",
            "completed_unit_ids",
            "pending_unit_ids",
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
    assert "## Key Findings" in report_payload["markdown"]
    assert report_payload["content"] == report_payload["markdown"]
    assert report_payload["evidence_refs"]
    assert report_payload["source_event_type"] == "research_graph"
    final_message_index = next(
        index
        for index, item in enumerate(stream_queue.items)
        if item.get("event") == "message"
        and item.get("data", {}).get("source_event_type") == "research_graph"
        and "## Key Findings" in item.get("data", {}).get("delta", {}).get("content", "")
    )
    complete_checkpoint_index = next(
        index
        for index, item in enumerate(stream_queue.items)
        if item.get("event") == "research_graph_checkpoint"
        and item.get("data", {}).get("phase") == "complete"
    )
    assert final_message_index < complete_checkpoint_index
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

    assert "## Key Findings" in result[0]
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

    assert "## Key Findings" in result[0]
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
