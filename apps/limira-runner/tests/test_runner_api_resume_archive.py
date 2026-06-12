import asyncio
import json
import zipfile
from pathlib import Path

import pytest

from runner_api import TASK_WORKERS_KEY, _checkpoint_envelope, create_app
from task_store import TaskStore
from test_runner_api import (
    init_state,
    render_markdown,
    seed_running_task,
    update_state,
    wait_for_task_status,
)


class ResumedArchiveContextProbe:
    def __init__(self):
        self.contexts = []

    async def stream(self, task_id, query, context, disconnect_check=None):
        assert task_id
        assert query == "stale running query"
        assert disconnect_check is not None
        assert isinstance(context, dict)
        self.contexts.append(context)
        yield {
            "event": "message",
            "data": {"delta": {"content": " resumed final"}},
        }


@pytest.mark.asyncio
async def test_resumed_langgraph_archive_trace_includes_scrubbed_checkpoint_context(
    tmp_path,
):
    secrets = [
        "owner-secret-resume",
        "sk-resume-source-secret123456",
        "service-token-resume-secret123456",
        "raw-prompt-resume-secret123456",
        "model-internal-resume-secret123456",
        "camel owner private value",
        "plain api credential value",
        "camel model internal text",
        "camel raw prompt text",
        "camel service token words",
        "raw model output private text",
        "model input transcript private text",
        "raw model private text",
    ]
    store = TaskStore(tmp_path / "tasks.sqlite3")
    record = seed_running_task(
        store,
        task_id="task-resume-archive-context",
        worker_id="worker-resume",
        lease_expires_at="2026-06-06T12:05:00+00:00",
    )
    checkpoint = _checkpoint_envelope(
        {
            "phase": "research",
            "status": "running",
            "current_research_unit": "unit-1",
            "source_ledger": [
                {
                    "ledger_type": "retrieved_source",
                    "source_id": "RSRC-PRE",
                    "source_type": "page_visit_or_jina_summary",
                    "title": "Pre-resume source",
                    "summary": "pre-resume source text " + ("x" * 5_000),
                    "content_hash": "a" * 32,
                    "owner_user_id": secrets[0],
                    "api_key": secrets[1],
                    "raw_prompt": secrets[3],
                    "ownerUserId": secrets[5],
                    "apiKey": secrets[6],
                    "modelInternal": secrets[7],
                    "rawPrompt": secrets[8],
                    "runnerServiceToken": secrets[9],
                }
            ],
            "evidence_ledger": [
                {
                    "ledger_type": "evidence",
                    "evidence_id": "EVID-PRE",
                    "retrieved_source_id": "RSRC-PRE",
                    "summary": "pre-resume evidence",
                    "content_hash": "b" * 32,
                    "service_token": secrets[2],
                    "modelOutput": secrets[10],
                    "modelInput": secrets[11],
                    "rawModel": secrets[12],
                }
            ],
            "executor_state": {
                "research_graph_executor": "langgraph",
                "node": "ResearchNode",
                "resume_status": "running",
                "resume_queued_at": "2026-06-06T12:10:00+00:00",
                "recovery_reason": "expired_lease",
                "raw_prompt": secrets[3],
                "model_internal": secrets[4],
            },
            "research_graph_executor": "langgraph",
            "last_completed_node": "research",
            "current_node": "compress",
            "completed_unit_ids": ["unit-1"],
            "pending_unit_ids": ["unit-2"],
            "resume_policy": "resume_from_checkpoint",
            "recoverable_reason": "langgraph_checkpoint_resumable",
        }
    )
    store.write_task_checkpoint(
        record.task_id,
        checkpoint=checkpoint,
        updated_at="2026-06-06T12:04:00+00:00",
    )
    probe = ResumedArchiveContextProbe()
    app = create_app(
        task_store=store,
        archive_root=tmp_path / "archives",
        service_token="shared",
        stream_events=probe.stream,
        init_render_state=init_state,
        update_state_with_event=update_state,
        render_markdown=render_markdown,
        clock=lambda: "2026-06-06T12:10:00+00:00",
    )

    completed = await wait_for_task_status(
        store,
        record.task_id,
        "completed",
        timeout=2.0,
    )
    worker = app[TASK_WORKERS_KEY].get(record.task_id)
    if worker is not None:
        await asyncio.wait_for(worker, timeout=1.0)

    trace = json.loads(
        (Path(completed.archive_dir) / "trace.json").read_text(encoding="utf-8")
    )
    event_types = [event["type"] for event in trace["events"]]
    resume_events = [
        event for event in trace["events"] if event["type"] == "resume_archive_context"
    ]
    resume_payload = resume_events[0]["payload"]
    serialized_trace = json.dumps(trace, ensure_ascii=False)

    assert probe.contexts[0]["resume_checkpoint"]["phase"] == "research"
    assert event_types[:2] == ["resume_archive_context", "message"]
    assert event_types.count("resume_archive_context") == 1
    assert event_types.count("message") == 1
    assert event_types.count("source_candidate_collected") == 0
    assert event_types.count("evidence_collected") == 0
    assert event_types.count("report_section_generated") == 0
    assert resume_payload["research_graph_executor"] == "langgraph"
    assert resume_payload["checkpoint"]["phase"] == "research"
    assert resume_payload["checkpoint"]["status"] == "queued"
    assert resume_payload["checkpoint"]["current_node"] == "compress"
    assert resume_payload["checkpoint"]["current_research_unit"] == "unit-1"
    assert resume_payload["checkpoint"]["updated_at"] == "2026-06-06T12:10:00+00:00"
    assert resume_payload["checkpoint"]["completed_unit_ids"] == ["unit-1"]
    assert resume_payload["checkpoint"]["pending_unit_ids"] == ["unit-2"]
    assert resume_payload["source_ledger_count"] == 1
    assert resume_payload["evidence_ledger_count"] == 1
    assert resume_payload["source_ledger"][0]["source_id"] == "RSRC-PRE"
    assert resume_payload["source_ledger"][0]["content_hash"] == "a" * 32
    assert len(resume_payload["source_ledger"][0]["summary"]) <= 500
    assert resume_payload["evidence_ledger"][0]["evidence_id"] == "EVID-PRE"
    assert resume_payload["executor_state"]["resume_status"] == "queued"
    assert resume_payload["executor_state"]["recovery_reason"] == "expired_lease"
    assert (
        resume_payload["executor_state"]["resume_checkpoint_updated_at"]
        == "2026-06-06T12:04:00+00:00"
    )
    assert "owner_user_id" not in serialized_trace
    assert "raw_prompt" not in serialized_trace
    assert "model_internal" not in serialized_trace

    with zipfile.ZipFile(completed.archive_zip_path) as archive:
        zip_trace = json.loads(archive.read("trace.json").decode("utf-8"))
    assert zip_trace["events"] == trace["events"]
    serialized_zip_trace = json.dumps(zip_trace, ensure_ascii=False)

    for serialized in (serialized_trace, serialized_zip_trace):
        assert "ownerUserId" not in serialized
        assert "apiKey" not in serialized
        assert "modelInternal" not in serialized
        assert "rawPrompt" not in serialized
        assert "runnerServiceToken" not in serialized
        assert "modelOutput" not in serialized
        assert "modelInput" not in serialized
        assert "rawModel" not in serialized
        for secret in secrets:
            assert secret not in serialized
