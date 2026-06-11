import json
import sys
from pathlib import Path

import pytest

from limira_tools.limira_evidence import tool_evidence_events_from_result
from runner_api import ACTIVE_TASKS_KEY, _reconcile_task_if_stale, create_app
from src.core.research_graph import (
    build_initial_research_graph,
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
    assert text_payload["text"] == source_text.extracted_text
    assert text_payload["retrieved_at"]
    assert text_payload["content_hash"]

    assert empty_payload["source_type"] == "limira_upload"
    assert empty_payload["source_content_state"] == "context_only"
    assert empty_payload["retrieval_status"] == "context_only"
    assert empty_payload["document_id"] == "doc-empty"
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
    assert source_payloads[0]["text"] == source_text.extracted_text
    assert source_payloads[0]["content_hash"] == text_payload["content_hash"]
    assert source_payloads[0]["retrieved_at"]


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
