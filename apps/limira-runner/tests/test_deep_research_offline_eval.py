import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from omegaconf import OmegaConf

from limira_tools.limira_evidence import tool_evidence_events_from_result
from runner_api import (
    ACTIVE_TASKS_KEY,
    PENDING_STARTUP_WORKERS_KEY,
    TASK_WORKERS_KEY,
    _reconcile_task_if_stale,
    _start_pending_task_workers,
    create_app,
)
from src.core import pipeline as pipeline_module
from src.core.research_graph import (
    CompressedFinding,
    EvidenceItem,
    EvidenceStrictMode,
    LangGraphResearchUnitNode,
    ResearchGraphExecutionContext,
    ResearchGraphExecutionResult,
    ResearchGraphNodeOutput,
    ResearchPhase,
    RetrieverRegistry,
    UploadedDocumentSearchRetriever,
    UploadedDocumentSourceProvider,
    VerifiedClaim,
    VerifierNode,
    WriterNode,
    build_initial_research_graph,
    execute_research_graph,
    graph_task_description,
)
from task_store import TaskStore


ROOT = Path(__file__).resolve().parents[3]
LIMIRA_BACKEND = ROOT / "apps/limira-web/backend"
sys.path.insert(0, str(LIMIRA_BACKEND))

from limira_backend.routers import limira  # noqa: E402


class _OfflinePdfExporter:
    pdf_bytes = b"%PDF-1.7\nfake limira offline eval report with rendered content\n%%EOF"

    def __init__(self):
        self.html_inputs = []

    async def render_pdf(self, html_content):
        self.html_inputs.append(html_content)
        return self.pdf_bytes


class _OfflineCaptureQueue:
    def __init__(self):
        self.items = []

    async def put(self, item):
        self.items.append(item)


class _OfflineFailingOrchestrator:
    async def run_main_agent(self, **_kwargs):
        raise RuntimeError("offline graph node failure")


class _OfflineOrchestrator:
    task_descriptions = []

    def __init__(self, **_kwargs):
        pass

    async def run_main_agent(self, **kwargs):
        self.__class__.task_descriptions.append(kwargs["task_description"])
        return "offline legacy summary", "offline legacy final", None


class _OfflineToolManager:
    def __init__(self):
        self.task_log = None

    def set_task_log(self, task_log):
        self.task_log = task_log


class _OfflineClientFactory:
    def __init__(self, **_kwargs):
        pass

    def close(self):
        pass


class _InstrumentedUploadRetriever(UploadedDocumentSearchRetriever):
    def __init__(self):
        self.search_calls = []
        self.retrieve_calls = []

    async def search(self, request):
        self.search_calls.append(
            {
                "unit_id": request.unit.id,
                "query": request.unit.question,
                "upload_count": len(request.state.upload_sources),
            }
        )
        return await super().search(request)

    async def retrieve(self, request, candidate):
        self.retrieve_calls.append(candidate.candidate_id)
        return await super().retrieve(request, candidate)


class _ResumeProbeStream:
    def __init__(self):
        self.contexts = []
        self.checkpoint = None

    async def stream(self, task_id, query, context, disconnect_check):
        self.contexts.append(dict(context or {}))
        resume_checkpoint = context["resume_checkpoint"]
        self.checkpoint = resume_checkpoint
        yield {
            "event": "research_graph_checkpoint",
            "data": {
                **resume_checkpoint,
                "phase": "compress",
                "status": "running",
                "last_completed_node": "research",
                "current_node": "verify",
                "executor_state": {
                    **resume_checkpoint["executor_state"],
                    "node": "EvidenceCompressorNode",
                },
            },
        }
        yield {
            "event": "message",
            "data": {"delta": {"content": "resume completed"}},
        }


class _OfflineResearchClient:
    def __init__(
        self,
        *,
        runner_task_id="runner-offline-eval",
        events=None,
        status_payload=None,
    ):
        self.runner_task_id = runner_task_id
        self.events = list(events or [])
        self.status_payload = status_payload or {
            "task_id": runner_task_id,
            "status": "completed",
            "archive_status": "ready",
        }
        self.create_calls = []
        self.stream_calls = []
        self.status_calls = []

    async def create_research_task(
        self,
        *,
        query,
        scenario,
        user,
        conversation_id=None,
        document_ids=None,
        upload_scope=None,
        source_policy=None,
    ):
        self.create_calls.append(
            {
                "query": query,
                "scenario": scenario,
                "user": user,
                "conversation_id": conversation_id,
                "document_ids": list(document_ids or []),
                "upload_scope": dict(upload_scope or {}),
                "source_policy": dict(source_policy or {}),
            }
        )
        return {
            "task_id": self.runner_task_id,
            "status": "queued",
            "stream_url": f"/limira-runner/tasks/{self.runner_task_id}/events",
            "task_url": f"/limira-runner/tasks/{self.runner_task_id}",
        }

    async def stream_events(self, *, task, user):
        self.stream_calls.append({"task": task, "user": user})
        for event in self.events:
            yield event

    async def get_task_status(self, *, task, user):
        self.status_calls.append({"task": task, "user": user})
        return dict(self.status_payload)


def _uploaded_document(
    *,
    document_id,
    source_document_id=None,
    extracted_text,
    filename="upload.txt",
):
    metadata = {"sha256": f"sha-{document_id}"}
    if source_document_id:
        metadata["source_document_id"] = source_document_id
    return limira.LimiraUploadedDocument(
        document_id=document_id,
        owner_user_id="user-a",
        task_id="task-upload-doc",
        original_filename=filename,
        content_type="text/plain",
        byte_size=len(extracted_text or ""),
        minio_bucket="limira-artifacts",
        object_key=f"limira/users/hash/uploads/{document_id}.txt",
        extracted_text=extracted_text,
        language="en",
        metadata=metadata,
    )


def _archive_member_texts(archive_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        return {
            member_name: archive.read(member_name).decode("utf-8")
            for member_name in archive.namelist()
        }


def _offline_pipeline_cfg(*, graph_executor="langgraph", evidence_strict=None):
    config = {
        "llm": {
            "provider": "openai-compatible",
            "base_url": "https://llm.test",
            "model_name": "offline-eval-model",
            "temperature": 0,
            "top_p": 1,
            "min_p": 0,
            "top_k": 0,
            "max_tokens": 4096,
            "repetition_penalty": 1,
            "async_client": False,
        },
        "agent": {
            "keep_tool_result": True,
            "main_agent": {"max_turns": 1},
            "sub_agents": None,
            "research_graph": {"executor": graph_executor},
        },
    }
    if evidence_strict is not None:
        config["limira"] = {"evidence": {"strict": evidence_strict}}
    return OmegaConf.create(config)


def _offline_graph_context(*, evidence_strict_mode=EvidenceStrictMode.WARN):
    return ResearchGraphExecutionContext(
        orchestrator=_OfflineOrchestrator(),
        original_task_description="offline eval",
        task_id="offline-eval",
        evidence_strict_mode=evidence_strict_mode,
    )


def _offline_evidence(
    evidence_id: str,
    text: str,
    *,
    retrieved_source_id: str | None = None,
    retrieved_at: str = "2026-06-06T12:00:00+00:00",
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        retrieved_source_id=retrieved_source_id or f"RSRC-{evidence_id}",
        title=f"Evidence {evidence_id}",
        source_type="web",
        retrieved_at=retrieved_at,
        content_hash="a" * 32,
        quote_or_summary=text,
        claims=[text],
        confidence=0.8,
    )


def _markdown_section(markdown: str, title: str) -> str:
    marker = f"## {title}\n"
    assert marker in markdown
    return markdown.split(marker, 1)[1].split("\n## ", 1)[0]


@pytest.mark.asyncio
async def test_offline_eval_missing_ref_records_final_report_warning():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    exporter = _OfflinePdfExporter()
    user = limira.LimiraUser("user-a")
    hash_ref = "EVID-abcdef123456"
    task = repo.create_task(
        task_id="eval-missing-ref",
        owner_user_id=user.id,
        query="offline missing reference eval",
        scenario=None,
        runner_task_id="runner-eval-missing-ref",
    )

    report = await limira._store_task_report_pdf(
        task=task,
        user=user,
        repo=repo,
        object_storage=storage,
        pdf_exporter=exporter,
        report_id="eval-missing-ref-report",
        report_type="final",
        markdown=f"Finding cites [EVID-999] and [{hash_ref}].",
        evidence_refs=["EVID-999", hash_ref, "EVID-abc"],
        metadata={"source_event_type": "offline_eval"},
        record_cloud_document=False,
    )

    warnings = [
        event["payload"]
        for event in repo.get_artifact_trace_events(task.task_id)
        if event["type"] == "artifact_warning"
    ]

    assert report.evidence_refs == ["EVID-999", hash_ref]
    assert "EVID-abc" not in report.evidence_refs
    assert warnings[-1]["warning"] == "unresolved_evidence_refs"
    assert warnings[-1]["artifact_type"] == "final_report"
    assert warnings[-1]["local_artifact_id"] == "eval-missing-ref-report"
    assert warnings[-1]["source_event_type"] == "offline_eval"
    assert warnings[-1]["evidence_refs"] == ["EVID-999", hash_ref]


def test_offline_eval_snippet_only_search_is_source_candidate_not_evidence():
    events = tool_evidence_events_from_result(
        task_id="eval-snippet-only",
        tool_name="google_search",
        tool_call_id="call-search",
        arguments={"q": "BYD 1260H"},
        result=json.dumps(
            {
                "organic": [
                    {
                        "title": "DoD 1260H List",
                        "link": "https://example.test/dod-1260h.pdf",
                        "snippet": "Snippet-only search result.",
                    }
                ],
                "searchParameters": {"q": "BYD 1260H"},
            }
        ),
    )

    assert [event["type"] for event in events] == ["source_candidate_collected"]
    payload = events[0]["payload"]
    assert payload["candidate_id"].startswith("SRC-")
    assert "evidence_id" not in payload
    assert payload["source_type"] == "web_search_result"
    assert payload["source_state"] == "source_candidate"
    assert payload["source_content_state"] == "snippet_only"
    assert payload["candidate"] is True
    assert payload["confidence"] == 0.25
    assert payload["retrieved_at"]
    assert payload["content_hash"]


def test_offline_eval_upload_doc_source_candidates_cover_text_and_context_only():
    source_text = _uploaded_document(
        document_id="doc-text",
        extracted_text="Uploaded memo states the company is listed.",
        filename="memo.txt",
    )
    source_empty = _uploaded_document(
        document_id="doc-empty",
        extracted_text="",
        filename="empty.pdf",
    )
    attached_text = _uploaded_document(
        document_id="attached-doc-text",
        source_document_id="doc-text",
        extracted_text=source_text.extracted_text,
        filename="memo.txt",
    )
    attached_empty = _uploaded_document(
        document_id="attached-doc-empty",
        source_document_id="doc-empty",
        extracted_text="",
        filename="empty.pdf",
    )

    text_payload = limira._upload_source_candidate_payload(
        task_id="task-upload-doc",
        document=attached_text,
    )
    empty_payload = limira._upload_source_candidate_payload(
        task_id="task-upload-doc",
        document=attached_empty,
    )
    upload_scope = limira._runner_upload_scope_for_documents(
        [source_text, source_empty],
        [attached_text, attached_empty],
    )
    source_payloads = upload_scope.pop("source_payloads")

    assert text_payload["source_type"] == "limira_upload"
    assert text_payload["source_state"] == "source_candidate"
    assert text_payload["source_content_state"] == "content_bearing"
    assert text_payload["retrieval_status"] == "retrieved"
    assert text_payload["document_id"] == "doc-text"
    assert text_payload["attached_document_id"] == "attached-doc-text"
    assert text_payload["chunk_id"].startswith("UPLOAD-CHUNK-")
    assert text_payload["text"] == source_text.extracted_text
    assert text_payload["retrieved_at"]
    assert text_payload["content_hash"]

    assert empty_payload["source_type"] == "limira_upload"
    assert empty_payload["source_content_state"] == "context_only"
    assert empty_payload["retrieval_status"] == "context_only"
    assert empty_payload["document_id"] == "doc-empty"
    assert empty_payload["chunk_id"].startswith("UPLOAD-CHUNK-")
    assert "text" not in empty_payload
    assert "retrieved_at" not in empty_payload
    assert "content_hash" not in empty_payload

    assert upload_scope == {
        "source_type": "limira_upload",
        "document_ids": ["doc-text", "doc-empty"],
        "attached_document_ids": ["attached-doc-text", "attached-doc-empty"],
        "document_count": 2,
        "retrieval_status": "partial",
        "retrieved_document_ids": ["doc-text"],
        "context_only_document_ids": ["doc-empty"],
    }
    assert len(source_payloads) == 1
    assert source_payloads[0]["document_id"] == "doc-text"
    assert source_payloads[0]["attached_document_id"] == "attached-doc-text"
    assert source_payloads[0]["chunk_id"] == text_payload["chunk_id"]
    assert source_payloads[0]["text"] == source_text.extracted_text
    assert source_payloads[0]["content_hash"] == text_payload["content_hash"]
    assert source_payloads[0]["retrieved_at"]


def test_offline_eval_upload_doc_graph_provider_retrieves_text_chunks_only():
    state = build_initial_research_graph(
        task_id="eval-upload-provider",
        query="Use uploaded memo",
        document_ids=["doc-text", "doc-empty"],
        upload_scope={
            "document_count": 2,
            "retrieval_status": "partial",
            "retrieved_document_ids": ["doc-text"],
            "context_only_document_ids": ["doc-empty"],
            "source_payloads": [
                {
                    "document_id": "doc-text",
                    "attached_document_id": "attached-doc-text",
                    "chunk_id": "UPLOAD-CHUNK-A",
                    "filename": "memo.txt",
                    "source_type": "limira_upload",
                    "source_content_state": "content_bearing",
                    "retrieval_status": "retrieved",
                    "retrieved_at": "2026-06-06T12:00:00+00:00",
                    "content_hash": "f" * 64,
                    "text": "Uploaded memo states the company is listed.",
                },
                {
                    "document_id": "doc-text",
                    "attached_document_id": "attached-doc-text",
                    "chunk_id": "UPLOAD-CHUNK-B",
                    "filename": "memo.txt",
                    "source_type": "limira_upload",
                    "source_content_state": "content_bearing",
                    "retrieval_status": "retrieved",
                    "retrieved_at": "2026-06-06T12:01:00+00:00",
                    "content_hash": "e" * 64,
                    "text": "Second uploaded memo chunk adds the listing date.",
                },
                {
                    "document_id": "doc-empty",
                    "attached_document_id": "attached-doc-empty",
                    "source_type": "limira_upload",
                    "source_content_state": "context_only",
                    "retrieval_status": "context_only",
                    "text": "",
                },
            ],
        },
    )

    retrieved = UploadedDocumentSourceProvider(state.upload_sources).retrieve()

    assert len(retrieved) == 2
    assert retrieved[0].document_id == "doc-text"
    assert retrieved[0].attached_document_id == "attached-doc-text"
    assert [source.chunk_id for source in retrieved] == [
        "UPLOAD-CHUNK-A",
        "UPLOAD-CHUNK-B",
    ]
    assert retrieved[0].source_type == "limira_upload"
    assert retrieved[0].source_content_state == "content_bearing"
    assert retrieved[0].retrieval_status == "retrieved"
    assert retrieved[0].content_hash == "f" * 32
    assert state.context_only_upload_document_ids == ["doc-empty"]


@pytest.mark.asyncio
async def test_offline_eval_graph_node_failure_uses_error_event_contract():
    state = build_initial_research_graph(
        task_id="eval-graph-node-failure",
        query="Verify graph node failure handling",
        max_units=1,
    )
    stream_queue = _OfflineCaptureQueue()

    with pytest.raises(RuntimeError, match="offline graph node failure"):
        await execute_research_graph(
            state=state,
            orchestrator=_OfflineFailingOrchestrator(),
            original_task_description="Verify graph node failure handling",
            task_id="eval-graph-node-failure",
            stream_queue=stream_queue,
        )

    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_phase"
    ] == ["scope", "plan", "research"]
    assert [
        item["data"]["phase"]
        for item in stream_queue.items
        if item.get("event") == "research_graph_checkpoint"
    ] == ["scope", "plan"]
    assert stream_queue.items[-1] == {
        "event": "error",
        "data": {
            "task_id": "eval-graph-node-failure",
            "phase": "research",
            "error": "offline graph node failure",
        },
    }


def test_offline_eval_scenario_policy_reaches_graph_prompt():
    state = build_initial_research_graph(
        task_id="eval-scenario-policy",
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
                    "text": "Uploaded memo says the entity appears on the list.",
                }
            ],
        },
        source_policy={
            "min_sources": 4,
            "prefer_primary_sources": False,
            "allow_secondary_sources": False,
            "require_retrieved_at": False,
        },
    )

    description = graph_task_description(state, "Assess export control exposure")

    assert "sanctions_export_controls" in state.brief.scope
    assert "Upload retrieval status: partial" in state.brief.scope
    assert "Uploaded memo says the entity appears on the list." in description
    assert "content_hash=hash-a" in description
    assert any(
        "retrieved upload text" in item and "doc-a" in item
        for item in state.brief.required_sources
    )
    assert any(
        "context-only upload IDs" in item and "doc-b" in item
        for item in state.brief.constraints
    )
    assert "Source target: at least 4" in description
    assert "prefer_primary_sources=False" in description
    assert "allow_secondary_sources=False" in description
    assert "require_retrieved_at=False" in description


def test_offline_eval_lease_takeover_requires_expired_or_missing_lease(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="eval-lease-takeover",
        user_id="user-a",
        query="lease takeover",
        created_at="2026-06-06T12:00:00+00:00",
    )
    claimed = store.claim_queued_task(
        "eval-lease-takeover",
        started_at="2026-06-06T12:01:00+00:00",
        worker_id="worker-a",
        lease_expires_at="2026-06-06T12:10:00+00:00",
    )

    duplicate_claim = store.claim_queued_task(
        "eval-lease-takeover",
        started_at="2026-06-06T12:02:00+00:00",
        worker_id="worker-b",
        lease_expires_at="2026-06-06T12:12:00+00:00",
    )
    wrong_worker_renewal = store.renew_task_lease(
        "eval-lease-takeover",
        worker_id="worker-b",
        heartbeat_at="2026-06-06T12:03:00+00:00",
        lease_expires_at="2026-06-06T12:13:00+00:00",
    )
    active_recovery = store.finalize_stale_running_task(
        "eval-lease-takeover",
        completed_at="2026-06-06T12:05:00+00:00",
        error="stale_running_task_recovered:expired_lease",
        warnings=["stale running task recovered: expired_lease"],
        lease_checked_at="2026-06-06T12:05:00+00:00",
    )
    expired_recovery = store.finalize_stale_running_task(
        "eval-lease-takeover",
        completed_at="2026-06-06T12:11:00+00:00",
        error="stale_running_task_recovered:expired_lease",
        warnings=["stale running task recovered: expired_lease"],
        lease_checked_at="2026-06-06T12:11:00+00:00",
    )

    assert claimed is not None
    assert duplicate_claim is None
    assert wrong_worker_renewal is None
    assert active_recovery is None
    assert expired_recovery is not None
    assert expired_recovery.status == "failed"
    assert expired_recovery.error == "stale_running_task_recovered:expired_lease"
    assert expired_recovery.worker_id == "worker-a"
    assert expired_recovery.lease_expires_at == "2026-06-06T12:10:00+00:00"


def test_offline_eval_checkpoint_resume_envelope_survives_store_restart(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    store = TaskStore(db_path)
    store.create_task(
        task_id="eval-checkpoint-resume",
        user_id="user-a",
        query="checkpoint resume",
        created_at="2026-06-06T12:00:00+00:00",
    )
    store.claim_queued_task(
        "eval-checkpoint-resume",
        started_at="2026-06-06T12:01:00+00:00",
        worker_id="worker-a",
        lease_expires_at="2026-06-06T12:11:00+00:00",
    )
    checkpoint = {
        "phase": "research",
        "status": "running",
        "current_research_unit": "unit-1-background",
        "source_ledger": [{"retrieved_source_id": "RSRC-001"}],
        "evidence_ledger": [{"id": "EVID-001"}],
        "executor_state": {"node": "ResearchUnitNode", "unit_index": 1},
        "resume_policy": "fail_recoverable",
        "recoverable_reason": "serial_graph_checkpoint_not_resumable",
    }

    checkpointed = store.write_task_checkpoint(
        "eval-checkpoint-resume",
        checkpoint=checkpoint,
        updated_at="2026-06-06T12:02:00+00:00",
    )
    restarted = TaskStore(db_path)
    persisted = restarted.get_task_checkpoint("eval-checkpoint-resume")
    record = restarted.get_task("eval-checkpoint-resume")

    assert checkpointed is not None
    assert persisted == checkpoint
    assert record.checkpoint == checkpoint
    assert record.checkpoint_updated_at == "2026-06-06T12:02:00+00:00"
    assert set(persisted) == {
        "phase",
        "status",
        "current_research_unit",
        "source_ledger",
        "evidence_ledger",
        "executor_state",
        "resume_policy",
        "recoverable_reason",
    }
    assert persisted["resume_policy"] == "fail_recoverable"
    assert persisted["recoverable_reason"] == "serial_graph_checkpoint_not_resumable"


@pytest.mark.asyncio
async def test_offline_eval_case_resume_from_checkpoint_preserves_ledgers(tmp_path):
    db_path = tmp_path / "tasks.sqlite3"
    store = TaskStore(db_path)
    task_id = "case-resume-from-checkpoint"
    store.create_task(
        task_id=task_id,
        user_id="user-a",
        query="checkpoint resume",
        created_at="2026-06-06T12:00:00+00:00",
        context={"source_policy": {"prefer_uploaded_documents": True}},
    )
    store.claim_queued_task(
        task_id,
        started_at="2026-06-06T12:01:00+00:00",
        worker_id="worker-stale",
        lease_expires_at="2026-06-06T12:05:00+00:00",
    )
    checkpoint = {
        "phase": "research",
        "status": "running",
        "current_research_unit": "unit-1-background",
        "source_ledger": [
            {
                "ledger_type": "research_unit",
                "unit_id": "unit-1-background",
                "status": "completed",
            }
        ],
        "evidence_ledger": [
            {
                "ledger_type": "evidence",
                "id": "EVID-001",
                "retrieved_source_id": "RSRC-001",
                "content_hash": "a" * 32,
            }
        ],
        "executor_state": {"research_graph_executor": "langgraph"},
        "research_graph_executor": "langgraph",
        "last_completed_node": "research",
        "current_node": "compress",
        "completed_unit_ids": ["unit-1-background"],
        "pending_unit_ids": [],
        "resume_policy": "resume_from_checkpoint",
        "recoverable_reason": "langgraph_checkpoint_resumable",
    }
    store.write_task_checkpoint(
        task_id,
        checkpoint=checkpoint,
        updated_at="2026-06-06T12:04:00+00:00",
    )
    stream_probe = _ResumeProbeStream()
    app = create_app(
        task_store=store,
        archive_root=tmp_path / "archives",
        service_token="shared",
        stream_events=stream_probe.stream,
        clock=lambda: "2026-06-06T12:10:00+00:00",
    )

    queued = store.get_task(task_id)
    assert queued.status == "queued"
    assert store.get_task_checkpoint(task_id)["status"] == "queued"
    if task_id in app[PENDING_STARTUP_WORKERS_KEY]:
        await _start_pending_task_workers(app)
    else:
        assert task_id in app[TASK_WORKERS_KEY]

    worker = app[TASK_WORKERS_KEY][task_id]
    await asyncio.wait_for(worker, timeout=2.0)

    final_checkpoint = store.get_task_checkpoint(task_id)
    assert stream_probe.contexts[0]["resume_checkpoint"]["source_ledger"] == (
        checkpoint["source_ledger"]
    )
    assert stream_probe.contexts[0]["resume_checkpoint"]["evidence_ledger"] == (
        checkpoint["evidence_ledger"]
    )
    assert final_checkpoint["source_ledger"] == checkpoint["source_ledger"]
    assert final_checkpoint["evidence_ledger"] == checkpoint["evidence_ledger"]
    assert final_checkpoint["resume_policy"] == "terminal"
    assert {
        item["id"] for item in final_checkpoint["evidence_ledger"] if "id" in item
    } == {"EVID-001"}
    assert store.get_task(task_id).status == "completed"


@pytest.mark.asyncio
async def test_offline_eval_case_upload_search_used():
    upload_text = "Uploaded program-x memo states the entity is listed."
    state = build_initial_research_graph(
        task_id="case-upload-search-used",
        query="Assess program-x listing from uploaded memo",
        document_ids=["doc-upload"],
        upload_scope={
            "document_count": 1,
            "retrieval_status": "retrieved",
            "retrieved_document_ids": ["doc-upload"],
            "context_only_document_ids": [],
            "source_payloads": [
                {
                    "document_id": "doc-upload",
                    "attached_document_id": "attached-doc-upload",
                    "chunk_id": "UPLOAD-CHUNK-001",
                    "filename": "program-x-memo.txt",
                    "source_type": "limira_upload",
                    "source_content_state": "content_bearing",
                    "retrieval_status": "retrieved",
                    "retrieved_at": "2026-06-06T12:00:00+00:00",
                    "content_hash": "b" * 64,
                    "text": upload_text,
                    "text_char_count": len(upload_text),
                }
            ],
        },
        source_policy={"prefer_uploaded_documents": True},
        max_units=1,
    )
    upload_retriever = _InstrumentedUploadRetriever()
    registry = RetrieverRegistry()
    registry.register(upload_retriever)
    research_output = await LangGraphResearchUnitNode(
        retriever_registry=registry,
        retriever_names=["uploaded_document_search"],
    ).run(
        state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=state),
    )
    verify_output = await VerifierNode().run(
        research_output.state,
        _offline_graph_context(),
        research_output,
    )

    assert upload_retriever.search_calls
    assert upload_retriever.retrieve_calls
    assert upload_text in verify_output.state.evidence[0].quote_or_summary
    assert verify_output.state.evidence[0].document_id == "doc-upload"
    assert verify_output.state.verified_claims[0].support_type == "supported"
    assert verify_output.state.verified_claims[0].evidence_ids == [
        verify_output.state.evidence[0].id
    ]


@pytest.mark.asyncio
async def test_offline_eval_case_contradiction_detected():
    state = build_initial_research_graph(
        task_id="case-contradiction-detected",
        query="Assess listing contradiction",
        max_units=1,
    )
    evidence = [
        _offline_evidence("EVID-001", "The entity is listed under the program."),
        _offline_evidence("EVID-002", "The entity is not listed under the program."),
    ]
    state = state.model_copy(
        update={
            "phase": ResearchPhase.COMPRESS,
            "evidence": evidence,
            "findings": [
                CompressedFinding(
                    id="finding-conflict",
                    research_unit_id="unit-1-background",
                    summary="Sources disagree about whether the entity is listed.",
                    evidence_ids=["EVID-001", "EVID-002"],
                    confidence=0.8,
                )
            ],
        }
    )

    verify_output = await VerifierNode().run(
        state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=state),
    )
    write_output = await WriterNode().run(
        verify_output.state,
        _offline_graph_context(),
        verify_output,
    )

    claim = verify_output.state.verified_claims[0]
    assert claim.support_type == "contradicted"
    assert claim.evidence_ids == ["EVID-001", "EVID-002"]
    assert "Sources disagree" in _markdown_section(
        write_output.final_summary,
        "Conflicts",
    )
    assert "Sources disagree" not in _markdown_section(
        write_output.final_summary,
        "Key Findings",
    )


@pytest.mark.asyncio
async def test_offline_eval_case_strict_missing_ref_blocks_report():
    class _BadRefWriter(WriterNode):
        def _compose_report(self, state):
            return (
                "## Answer\nKnown EVID-001; missing EVID-999.",
                "Known EVID-001; missing EVID-999.",
            )

    state = build_initial_research_graph(
        task_id="case-strict-missing-ref-blocks-report",
        query="Assess strict evidence mode",
        max_units=1,
    )
    state = state.model_copy(
        update={
            "phase": ResearchPhase.VERIFY,
            "evidence": [
                _offline_evidence("EVID-001", "Primary evidence supports the claim.")
            ],
            "verified_claims": [
                VerifiedClaim(
                    id="claim-supported",
                    claim="Primary evidence supports the claim.",
                    support_type="supported",
                    evidence_ids=["EVID-001"],
                    rationale="Collected evidence supports the claim.",
                    confidence=0.9,
                )
            ],
        }
    )

    with pytest.raises(ValueError, match="research_graph_evidence_strict_block"):
        await _BadRefWriter().run(
            state,
            _offline_graph_context(evidence_strict_mode=EvidenceStrictMode.BLOCK),
            ResearchGraphNodeOutput(state=state),
        )

    warn_output = await _BadRefWriter().run(
        state,
        _offline_graph_context(evidence_strict_mode=EvidenceStrictMode.WARN),
        ResearchGraphNodeOutput(state=state),
    )
    assert warn_output.artifact_events[0]["payload"]["warning"] == (
        "unresolved_evidence_refs"
    )
    assert warn_output.artifact_events[-1]["type"] == "report_section_generated"


@pytest.mark.asyncio
async def test_offline_eval_case_langgraph_executor_routes(monkeypatch, tmp_path):
    _OfflineOrchestrator.task_descriptions = []
    route_probe = {"langgraph_called": False}

    async def fake_langgraph_executor(**kwargs):
        route_probe["langgraph_called"] = True
        return ResearchGraphExecutionResult(
            state=kwargs["state"],
            final_summary="langgraph route summary",
            final_boxed_answer="langgraph route answer",
            failure_experience_summary=None,
        )

    async def forbidden_serial_executor(**_kwargs):
        raise AssertionError("serial executor must not satisfy langgraph route")

    monkeypatch.setattr(pipeline_module, "ClientFactory", _OfflineClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _OfflineOrchestrator)
    monkeypatch.setattr(pipeline_module, "_load_langgraph_executor", lambda: fake_langgraph_executor)
    monkeypatch.setattr(pipeline_module, "execute_research_graph", forbidden_serial_executor)
    stream_queue = _OfflineCaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=_offline_pipeline_cfg(graph_executor="langgraph"),
        task_id="case-langgraph-executor-routes",
        task_description="Route through langgraph",
        task_file_name="",
        main_agent_tool_manager=_OfflineToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert route_probe["langgraph_called"] is True
    assert result[0] == "langgraph route summary"
    assert result[1] == "langgraph route answer"
    assert _OfflineOrchestrator.task_descriptions == []
    assert stream_queue.items[0]["data"]["research_graph_executor"] == "langgraph"


@pytest.mark.asyncio
async def test_offline_eval_case_scenario_changes_source_policy():
    state = build_initial_research_graph(
        task_id="case-scenario-changes-source-policy",
        query="Assess export control exposure",
        scenario="sanctions_export_controls",
        source_policy={"prefer_scenario_sources": True},
        max_units=1,
    )
    output = await LangGraphResearchUnitNode().run(
        state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=state),
    )
    substeps = output.executor_state["unit_substeps"][0]

    assert state.plan.research_units[0].source_policy.prefer_scenario_sources is True
    assert substeps["retriever_order"][0] == "page_visit_or_jina_summary"
    assert output.state.retrieved_sources
    assert output.state.retrieved_sources[0].source_type == "page_visit_or_jina_summary"
    assert output.executor_state["legacy_adapter_calls"] == 0


@pytest.mark.asyncio
async def test_offline_eval_case_snippet_only_cannot_support_claim():
    state = build_initial_research_graph(
        task_id="case-snippet-only-cannot-support-claim",
        query="BYD 1260H listing",
        max_units=1,
    )
    research_output = await LangGraphResearchUnitNode(
        retriever_names=["web_search"],
    ).run(
        state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=state),
    )
    candidate = research_output.state.source_candidates[0]
    candidate_only_state = research_output.state.model_copy(
        update={
            "phase": ResearchPhase.COMPRESS,
            "findings": [
                CompressedFinding(
                    id="finding-snippet-only",
                    research_unit_id="unit-1-background",
                    summary="Snippet-only candidate suggests possible listing.",
                    evidence_ids=[candidate.candidate_id],
                    confidence=0.7,
                )
            ],
        }
    )
    verify_output = await VerifierNode().run(
        candidate_only_state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=candidate_only_state),
    )

    assert candidate.source_content_state == "snippet_only"
    assert not research_output.state.evidence
    assert verify_output.state.verified_claims[0].support_type == "weak"
    assert verify_output.state.verified_claims[0].evidence_ids == []


@pytest.mark.asyncio
async def test_offline_eval_case_same_entity_background_is_not_supported():
    state = build_initial_research_graph(
        task_id="case-background-does-not-support-claim",
        query="Assess Entity A listing",
        max_units=1,
    )
    state = state.model_copy(
        update={
            "phase": ResearchPhase.COMPRESS,
            "evidence": [
                _offline_evidence(
                    "EVID-001",
                    "Company profile background: Entity A was founded in 2020.",
                )
            ],
            "findings": [
                CompressedFinding(
                    id="finding-background-only",
                    research_unit_id="unit-1-background",
                    summary="Entity A is listed under program X.",
                    evidence_ids=["EVID-001"],
                    confidence=0.8,
                )
            ],
        }
    )

    verify_output = await VerifierNode().run(
        state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=state),
    )
    write_output = await WriterNode().run(
        verify_output.state,
        _offline_graph_context(),
        verify_output,
    )

    claim = verify_output.state.verified_claims[0]
    assert claim.support_type == "weak"
    assert claim.evidence_details[0].support_type == "background"
    assert "Entity A is listed" not in _markdown_section(
        write_output.final_summary,
        "Key Findings",
    )
    assert "Entity A is listed" in _markdown_section(
        write_output.final_summary,
        "Uncertainties",
    )


@pytest.mark.asyncio
async def test_offline_eval_case_same_entity_status_mismatch_is_not_supported():
    state = build_initial_research_graph(
        task_id="case-status-mismatch-does-not-support-claim",
        query="Assess Entity A listing",
        max_units=1,
    )
    state = state.model_copy(
        update={
            "phase": ResearchPhase.COMPRESS,
            "evidence": [
                _offline_evidence(
                    "EVID-001",
                    "Entity A applied for program X and remains under review.",
                )
            ],
            "findings": [
                CompressedFinding(
                    id="finding-status-mismatch",
                    research_unit_id="unit-1-background",
                    summary="Entity A is listed under program X.",
                    evidence_ids=["EVID-001"],
                    confidence=0.8,
                )
            ],
        }
    )

    verify_output = await VerifierNode().run(
        state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=state),
    )
    write_output = await WriterNode().run(
        verify_output.state,
        _offline_graph_context(),
        verify_output,
    )
    claim = verify_output.state.verified_claims[0]

    assert claim.support_type in {"weak", "insufficient"}
    assert {detail.support_type for detail in claim.evidence_details} == {"unrelated"}
    assert "Entity A is listed" not in _markdown_section(
        write_output.final_summary,
        "Key Findings",
    )
    assert "Entity A is listed" in _markdown_section(
        write_output.final_summary,
        "Uncertainties",
    )


@pytest.mark.asyncio
async def test_offline_eval_case_candidate_under_review_is_not_supported():
    state = build_initial_research_graph(
        task_id="case-candidate-under-review-does-not-support-claim",
        query="Assess Entity A listing",
        max_units=1,
    )
    state = state.model_copy(
        update={
            "phase": ResearchPhase.COMPRESS,
            "evidence": [
                _offline_evidence(
                    "EVID-001",
                    "Entity A is listed as a candidate for program X and "
                    "remains under review.",
                )
            ],
            "findings": [
                CompressedFinding(
                    id="finding-candidate-under-review",
                    research_unit_id="unit-1-background",
                    summary="Entity A is listed under program X.",
                    evidence_ids=["EVID-001"],
                    confidence=0.8,
                )
            ],
        }
    )

    verify_output = await VerifierNode().run(
        state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=state),
    )
    write_output = await WriterNode().run(
        verify_output.state,
        _offline_graph_context(),
        verify_output,
    )
    claim = verify_output.state.verified_claims[0]

    assert claim.support_type in {"weak", "insufficient"}
    assert "direct" not in {
        detail.support_type for detail in claim.evidence_details
    }
    assert "Entity A is listed" not in _markdown_section(
        write_output.final_summary,
        "Key Findings",
    )
    assert "Entity A is listed" in _markdown_section(
        write_output.final_summary,
        "Uncertainties",
    )
    assert write_output.artifact_events[-1]["payload"]["evidence_refs"] == []


@pytest.mark.asyncio
async def test_offline_eval_case_mixed_history_with_confirmed_status_is_supported():
    state = build_initial_research_graph(
        task_id="case-mixed-history-confirms-claim",
        query="Assess Entity A listing",
        max_units=1,
    )
    state = state.model_copy(
        update={
            "phase": ResearchPhase.COMPRESS,
            "evidence": [
                _offline_evidence(
                    "EVID-001",
                    "Entity A applied for program X in January and was confirmed "
                    "listed under program X in June 2026.",
                )
            ],
            "findings": [
                CompressedFinding(
                    id="finding-mixed-history-confirmed",
                    research_unit_id="unit-1-background",
                    summary="Entity A is listed under program X.",
                    evidence_ids=["EVID-001"],
                    confidence=0.8,
                )
            ],
        }
    )

    verify_output = await VerifierNode().run(
        state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=state),
    )
    write_output = await WriterNode().run(
        verify_output.state,
        _offline_graph_context(),
        verify_output,
    )
    claim = verify_output.state.verified_claims[0]

    assert claim.support_type == "supported"
    assert {detail.support_type for detail in claim.evidence_details} == {"direct"}
    assert "Entity A is listed" in _markdown_section(
        write_output.final_summary,
        "Key Findings",
    )
    assert "Entity A is listed" not in _markdown_section(
        write_output.final_summary,
        "Uncertainties",
    )


@pytest.mark.asyncio
async def test_offline_eval_case_stale_current_evidence_is_not_supported():
    state = build_initial_research_graph(
        task_id="case-stale-current-evidence",
        query="Assess current Entity A listing",
        max_units=1,
    )
    state = state.model_copy(
        update={
            "phase": ResearchPhase.COMPRESS,
            "evidence": [
                _offline_evidence(
                    "EVID-001",
                    "In 2020, Entity A was listed under program X.",
                    retrieved_at="2020-01-01T00:00:00+00:00",
                )
            ],
            "findings": [
                CompressedFinding(
                    id="finding-stale-current",
                    research_unit_id="unit-1-background",
                    summary="Entity A is currently listed under program X in 2026.",
                    evidence_ids=["EVID-001"],
                    confidence=0.8,
                )
            ],
        }
    )

    verify_output = await VerifierNode().run(
        state,
        _offline_graph_context(),
        ResearchGraphNodeOutput(state=state),
    )
    claim = verify_output.state.verified_claims[0]

    assert claim.support_type == "weak"
    assert claim.evidence_details[0].support_type == "stale"
    assert "stale_or_incompatible" in (claim.temporal_context or "")


def test_offline_eval_restart_recovery_fails_stale_and_preserves_running_terminal(
    tmp_path,
):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.create_task(
        task_id="eval-stale",
        user_id="user-a",
        query="stale task",
        created_at="2026-06-06T10:00:00+00:00",
    )
    store.claim_queued_task(
        "eval-stale",
        started_at="2026-06-06T10:01:00+00:00",
    )
    store.create_task(
        task_id="eval-terminal",
        user_id="user-a",
        query="terminal task",
        created_at="2026-06-06T10:00:00+00:00",
    )
    store.update_task(
        "eval-terminal",
        status="completed",
        archive_status="ready",
        completed_at="2026-06-06T10:02:00+00:00",
    )

    app = create_app(
        task_store=store,
        clock=lambda: "2026-06-06T12:00:00+00:00",
    )

    recovered = store.get_task("eval-stale")
    terminal = store.get_task("eval-terminal")
    assert recovered.status == "failed"
    assert recovered.archive_status == "failed"
    assert recovered.error == "stale_running_task_recovered:no_active_worker"
    assert recovered.warnings == ["stale running task recovered: no_active_worker"]
    assert terminal.status == "completed"
    assert terminal.archive_status == "ready"
    assert terminal.completed_at == "2026-06-06T10:02:00+00:00"

    store.create_task(
        task_id="eval-healthy",
        user_id="user-a",
        query="healthy active task",
        created_at="2026-06-06T11:58:00+00:00",
    )
    healthy = store.claim_queued_task(
        "eval-healthy",
        started_at="2026-06-06T11:59:30+00:00",
    )
    app[ACTIVE_TASKS_KEY].add("eval-healthy")

    current = _reconcile_task_if_stale(app, healthy)

    assert current.status == "running"
    assert store.get_task("eval-healthy").status == "running"
    assert store.get_task("eval-healthy").completed_at is None


@pytest.mark.asyncio
async def test_offline_eval_server_side_ingestion_converges_without_browser_sse():
    repo = limira.InMemoryLimiraTaskRepository()
    storage = limira.InMemoryLimiraObjectStorage()
    pdf_exporter = _OfflinePdfExporter()
    runtime_state = limira.InMemoryLimiraRuntimeState()
    user = limira.LimiraUser("user-a")
    research = _OfflineResearchClient(
        runner_task_id="runner-server-side-ingestion",
        events=[
            {
                "task_id": "runner-server-side-ingestion",
                "type": "evidence_collected",
                "payload": {
                    "evidence_id": "EVID-001",
                    "title": "Offline source",
                    "summary": "Offline source content supports the report.",
                    "source_url": "https://example.test/offline",
                    "source_type": "web_page_summary",
                    "source_state": "evidence_item",
                    "source_content_state": "content_bearing",
                    "retrieved_at": "2026-06-06T12:00:00+00:00",
                    "content_hash": "a" * 32,
                },
            },
            {
                "task_id": "runner-server-side-ingestion",
                "type": "record_research_artifact",
                "payload": {
                    "artifact_type": "report_section",
                    "payload": {
                        "section_id": "REPORT-001",
                        "title": "Offline final report",
                        "markdown": "# Offline final report\n\nSupported by [EVID-001].",
                    },
                    "evidence_refs": ["EVID-001"],
                },
            },
            {
                "task_id": "runner-server-side-ingestion",
                "type": "status",
                "payload": {"status": "completed", "archive_status": "ready"},
            },
        ],
    )

    created = await limira.create_research_task(
        {"query": "server side ingestion offline eval"},
        request=None,
        user=user,
        repo=repo,
        research_client=research,
        runtime_state=runtime_state,
        object_storage=storage,
        pdf_exporter=pdf_exporter,
    )
    await limira._await_server_ingestion_task(created["task_id"])

    task = repo.get_task(created["task_id"])
    artifacts = await limira.get_task_artifacts(created["task_id"], user=user, repo=repo)
    reports = repo.list_task_reports(task_id=created["task_id"])
    event_types = [
        event["event_type"]
        for event in repo.list_task_event_logs(created["task_id"], limit=20)
    ]

    assert research.stream_calls
    assert task.status == "completed"
    assert task.archive_status == "ready"
    assert task.archive_object_key in storage.objects
    assert artifacts["evidence"][0]["evidence_id"] == "EVID-001"
    assert artifacts["report_sections"][0]["section_id"] == "REPORT-001"
    assert [report.report_id for report in reports] == ["REPORT-001"]
    assert reports[0].pdf_object_key in storage.objects
    assert event_types == [
        "evidence_collected",
        "record_research_artifact",
        "archive_generated",
        "status",
    ]

    archive_response = await limira.download_task_archive(
        created["task_id"],
        user=user,
        repo=repo,
        object_storage=storage,
    )
    trace = json.loads(_archive_member_texts(archive_response.body)["trace.json"])
    assert trace["artifacts"]["evidence"][0]["evidence_id"] == "EVID-001"
    assert trace["reports"][0]["report_id"] == "REPORT-001"
    assert trace["task"]["status"] == "completed"
