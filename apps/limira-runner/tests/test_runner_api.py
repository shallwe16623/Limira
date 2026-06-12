import asyncio
import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from archive_writer import ResearchArchiveWriter
from runner_api import (
    ACTIVE_TASKS_KEY,
    CANCELLED_TASKS_KEY,
    MAX_CONTEXT_JSON_CHARS,
    INIT_RENDER_STATE_KEY,
    MAX_QUERY_CHARS,
    RENDER_MARKDOWN_KEY,
    STREAM_EVENTS_KEY,
    TASK_EVENT_LOG_KEY,
    TASK_WORKERS_KEY,
    UPDATE_STATE_KEY,
    _checkpoint_envelope,
    _reconcile_task_if_stale,
    _stream_task_context,
    _task_response,
    create_app,
)
from task_store import TaskRecord, TaskStore


USER_A_HEADERS = {
    "X-Limira-Runner-Service-Token": "shared",
    "X-Limira-User-Id": "user-a",
}
USER_B_HEADERS = {
    "X-Limira-Runner-Service-Token": "shared",
    "X-Limira-User-Id": "user-b",
}
ADMIN_HEADERS = {
    "X-Limira-Runner-Service-Token": "shared",
    "X-Limira-User-Id": "admin-user",
    "X-Limira-User-Role": "admin",
}
ARCHIVE_MEMBERS = [
    "metadata.json",
    "report.html",
    "report.md",
    "trace.json",
]


def init_state():
    return {"chunks": [], "errors": []}


def update_state(state, message):
    if message.get("event") == "message":
        state["chunks"].append(
            (message.get("data", {}).get("delta") or {}).get("content", "")
        )
    if message.get("event") == "error":
        state["errors"].append((message.get("data") or {}).get("error", ""))
    return state


def update_state_with_structured_secret(state, message):
    if message.get("event") == "message":
        data = message.get("data", {})
        state["chunks"].append((data.get("delta") or {}).get("content", ""))
        if "token" in data:
            state["chunks"].append(f"token={data['token']}")
    return state


def render_markdown(state):
    return "# Research Summary\n" + "".join(state["chunks"])


def render_markdown_failure(state):
    raise RuntimeError("Authorization: Bearer rendersecret123456")


async def completed_stream(task_id, query, _unused, disconnect_check=None):
    assert task_id
    assert query == "test query"
    assert disconnect_check is not None
    assert await disconnect_check() is False
    yield {"event": "heartbeat", "data": {"Authorization": "Bearer ignoredsecret"}}
    yield {
        "event": "message",
        "data": {"delta": {"content": "final from state"}},
    }


async def echo_query_stream(task_id, query, context, disconnect_check=None):
    assert task_id
    assert isinstance(query, str)
    assert isinstance(context, dict)
    assert disconnect_check is not None
    yield {
        "event": "message",
        "data": {"delta": {"content": f"query length {len(query)}"}},
    }


class ContextCaptureStream:
    def __init__(self):
        self.context = None

    async def stream(self, task_id, query, context, disconnect_check=None):
        assert task_id
        assert query == "context query"
        assert disconnect_check is not None
        self.context = context
        yield {
            "event": "message",
            "data": {"delta": {"content": "context propagated"}},
        }


class BlockingStream:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, task_id, query, context, disconnect_check=None):
        assert task_id
        assert query
        assert isinstance(context, dict)
        assert disconnect_check is not None
        self.started.set()
        await self.release.wait()
        if False:
            yield {}


async def structured_secret_stream(task_id, query, _unused, disconnect_check=None):
    yield {
        "event": "message",
        "data": {
            "delta": {"content": "safe visible content\n"},
            "token": "tiny-leak-value",
        },
    }


async def failed_stream(task_id, query, _unused, disconnect_check=None):
    yield {
        "event": "message",
        "data": {"delta": {"content": "partial"}},
    }
    raise RuntimeError("Authorization: Basic dXNlcjpzZWNyZXQ=")


def executor_marker_stream(executor):
    async def stream(task_id, query, _unused, disconnect_check=None):
        assert task_id
        assert query == "test query"
        assert disconnect_check is not None
        yield {
            "event": "research_graph_executor_selected",
            "data": {
                "task_id": task_id,
                "research_graph_executor": executor,
            },
        }
        yield {
            "event": "message",
            "data": {"delta": {"content": f"{executor} route complete"}},
        }

    return stream


def pipeline_error_stream(error_text):
    async def stream(task_id, query, _unused, disconnect_check=None):
        assert task_id
        assert query == "test query"
        assert disconnect_check is not None
        yield {
            "event": "error",
            "data": {
                "task_id": task_id,
                "error": error_text,
            },
        }

    return stream


async def cancelled_stream(task_id, query, _unused, disconnect_check=None):
    yield {
        "event": "message",
        "data": {"delta": {"content": "partial"}},
    }
    raise asyncio.CancelledError("client disconnected")


async def cancelled_secret_stream(task_id, query, _unused, disconnect_check=None):
    yield {
        "event": "message",
        "data": {"delta": {"content": "partial"}},
    }
    raise asyncio.CancelledError("Authorization: Bearer cancelledsecret123456")


class CancellationProbe:
    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.check_count = 0
        self.stream_count = 0

    async def stream(self, task_id, query, _unused, disconnect_check=None):
        assert disconnect_check is not None
        self.stream_count += 1
        yield {
            "event": "message",
            "data": {"delta": {"content": "partial"}},
        }
        self.started.set()
        while True:
            self.check_count += 1
            if await disconnect_check():
                self.stopped.set()
                return
            await asyncio.sleep(0.01)


class BackgroundExecutionProbe:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, task_id, query, _unused, disconnect_check=None):
        assert disconnect_check is not None
        self.started.set()
        yield {
            "event": "message",
            "data": {"delta": {"content": "partial"}},
        }
        await self.release.wait()
        assert await disconnect_check() is False
        yield {
            "event": "message",
            "data": {"delta": {"content": " final"}},
        }


class GraphCheckpointProbe:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, task_id, query, _unused, disconnect_check=None):
        assert disconnect_check is not None
        yield {
            "event": "research_graph_checkpoint",
            "data": {
                "phase": "verify",
                "status": "running",
                "current_research_unit": "unit-1-background",
                "source_ledger": [{"unit_id": "unit-1-background"}],
                "evidence_ledger": [{"id": "EVID-abcdef123456"}],
                "executor_state": {"node": "VerifierNode"},
                "resume_policy": "fail_recoverable",
                "recoverable_reason": "serial_graph_checkpoint_not_resumable",
            },
        }
        self.started.set()
        await self.release.wait()
        yield {
            "event": "message",
            "data": {"delta": {"content": " checkpoint complete"}},
        }


class GraphCheckpointWithExecutorProbe(GraphCheckpointProbe):
    async def stream(self, task_id, query, _unused, disconnect_check=None):
        assert disconnect_check is not None
        yield {
            "event": "research_graph_executor_selected",
            "data": {
                "task_id": task_id,
                "research_graph_executor": "serial",
            },
        }
        async for event in super().stream(task_id, query, _unused, disconnect_check):
            yield event


class ZipFailWriter(ResearchArchiveWriter):
    def _create_zip(self, zip_path):
        raise RuntimeError("zip unavailable")


class StartFailWriter(ResearchArchiveWriter):
    def start(self, *args, **kwargs):
        raise RuntimeError("Authorization: Bearer setupsecret123456")


class CompleteFailWriter(ResearchArchiveWriter):
    def complete(self, *args, **kwargs):
        raise RuntimeError("Authorization: Bearer finalsecret123456")


class StreamClaimRaceStore(TaskStore):
    def __init__(self, db_path):
        super().__init__(db_path)
        self.claimed_during_cancel = False

    def cancel_queued_task(self, task_id, *, started_at, completed_at, error):
        claimed = self.claim_queued_task(
            task_id,
            started_at="2026-06-06T12:59:59+00:00",
        )
        self.claimed_during_cancel = claimed is not None
        return super().cancel_queued_task(
            task_id,
            started_at=started_at,
            completed_at=completed_at,
            error=error,
        )


class RenewingStaleRecoveryRaceStore(TaskStore):
    def __init__(self, db_path):
        super().__init__(db_path)
        self.renewed_during_recovery = False

    def finalize_stale_running_task(
        self,
        task_id,
        *,
        completed_at,
        error,
        warnings=None,
        lease_checked_at=None,
    ):
        current = self.get_task(task_id)
        if current and current.worker_id:
            self.renew_task_lease(
                task_id,
                worker_id=current.worker_id,
                heartbeat_at=completed_at,
                lease_expires_at="2026-06-06T13:00:00+00:00",
            )
            self.renewed_during_recovery = True
        return super().finalize_stale_running_task(
            task_id,
            completed_at=completed_at,
            error=error,
            warnings=warnings,
            lease_checked_at=lease_checked_at,
        )


class RenewingCancelRaceStore(TaskStore):
    def __init__(self, db_path):
        super().__init__(db_path)
        self.renewed_during_cancel = False

    def cancel_stale_running_task(
        self,
        task_id,
        *,
        completed_at,
        error,
        archive_status,
        archive_dir=None,
        archive_zip_path=None,
        warnings=None,
        lease_checked_at=None,
    ):
        current = self.get_task(task_id)
        if current and current.worker_id:
            self.renew_task_lease(
                task_id,
                worker_id=current.worker_id,
                heartbeat_at=completed_at,
                lease_expires_at="2026-06-06T13:00:00+00:00",
            )
            self.renewed_during_cancel = True
        return super().cancel_stale_running_task(
            task_id,
            completed_at=completed_at,
            error=error,
            archive_status=archive_status,
            archive_dir=archive_dir,
            archive_zip_path=archive_zip_path,
            warnings=warnings,
            lease_checked_at=lease_checked_at,
        )


async def make_client(
    tmp_path,
    stream_events=completed_stream,
    writer_cls=ResearchArchiveWriter,
    transport_closing=None,
    task_store=None,
    update_state_func=update_state,
    render_markdown_func=render_markdown,
):
    store = task_store or TaskStore(tmp_path / "tasks.sqlite3")
    app = create_app(
        task_store=store,
        archive_root=tmp_path / "archives",
        service_token="shared",
        stream_events=stream_events,
        init_render_state=init_state,
        update_state_with_event=update_state_func,
        render_markdown=render_markdown_func,
        transport_closing=transport_closing,
        archive_writer_cls=writer_cls,
        clock=Clock(),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, store


class Clock:
    def __init__(self):
        self.index = 0

    def __call__(self):
        self.index += 1
        return f"2026-06-06T12:00:{self.index:02d}+00:00"


def parse_sse(body):
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


async def read_sse_data_line(response, timeout=1.0):
    while True:
        line = await asyncio.wait_for(response.content.readline(), timeout=timeout)
        assert line
        if line.startswith(b"data: "):
            return line


def assert_public_task_response_hides_internal_identifiers(payload):
    serialized = json.dumps(payload, ensure_ascii=False)
    forbidden_fields = (
        "user_id",
        "owner_user_id",
        "object_key",
        "objectKey",
        "archive_object_key",
        "archiveObjectKey",
        "pdf_object_key",
        "pdfObjectKey",
    )
    for field in forbidden_fields:
        assert field not in payload
        assert field not in serialized

    for marker in (
        "limira/users/",
        "X-Limira-Runner-Service-Token",
        "x-limira-runner-service-token",
        "shared",
        "Authorization",
        "authorization",
        "Cookie",
        "cookie",
        "Set-Cookie",
    ):
        assert marker not in serialized


def assert_checkpoint_envelope(
    checkpoint,
    *,
    phase,
    status,
    resume_policy=None,
):
    assert checkpoint["phase"] == phase
    assert checkpoint["status"] == status
    assert "current_research_unit" in checkpoint
    assert checkpoint["source_ledger"] == []
    assert checkpoint["evidence_ledger"] == []
    assert isinstance(checkpoint["executor_state"], dict)
    assert "resume_policy" in checkpoint
    assert "recoverable_reason" in checkpoint
    if resume_policy is not None:
        assert checkpoint["resume_policy"] == resume_policy


def test_create_app_preserves_partial_helper_overrides(tmp_path, monkeypatch):
    async def custom_stream():
        yield {}

    async def default_stream():
        yield {}

    def default_init():
        return {"default": True}

    def default_update(state, message):
        return state

    def custom_render(state):
        return "custom"

    def default_render(state):
        return "default"

    monkeypatch.setattr(
        "runner_api._load_pipeline_helpers",
        lambda: (default_stream, default_init, default_update, default_render),
    )

    app = create_app(
        task_store=TaskStore(tmp_path / "tasks.sqlite3"),
        archive_root=tmp_path / "archives",
        service_token="shared",
        stream_events=custom_stream,
        render_markdown=custom_render,
        clock=Clock(),
    )

    assert app[STREAM_EVENTS_KEY] is custom_stream
    assert app[RENDER_MARKDOWN_KEY] is custom_render
    assert app[INIT_RENDER_STATE_KEY] is default_init
    assert app[UPDATE_STATE_KEY] is default_update


def test_checkpoint_envelope_reserves_recoverable_runner_fields():
    checkpoint = _checkpoint_envelope(
        {
            "phase": "started",
            "status": "running",
            "event_count": 0,
            "render_state": {"safe": True},
        }
    )

    assert_checkpoint_envelope(
        checkpoint,
        phase="started",
        status="running",
        resume_policy="fail_recoverable",
    )
    assert checkpoint["current_research_unit"] is None
    assert checkpoint["executor_state"]["event_count"] == 0
    assert checkpoint["executor_state"]["render_state"] == {"safe": True}
    assert checkpoint["recoverable_reason"] == "legacy_stream_checkpoint_not_resumable"


def test_task_response_exposes_secret_safe_operational_status():
    record = TaskRecord(
        task_id="task-observable",
        user_id="user-a",
        query="observable query",
        status="running",
        archive_status="pending",
        archive_dir=None,
        archive_zip_path=None,
        created_at="2026-06-06T11:59:59+00:00",
        started_at="2026-06-06T12:00:00+00:00",
        error="stale_running_task_recovered:OPENAI_API_KEY=sk-secret",
        model_summary={
            "provider": "deepseek",
            "base_url": "https://example.invalid/v1?api_key=sk-secret",
        },
        worker_id="runner-one:task-observable:worker-secret",
        lease_expires_at="2026-06-06T12:10:00+00:00",
        heartbeat_at="2026-06-06T12:05:00+00:00",
        attempt=2,
        checkpoint={
            "phase": "verify",
            "status": "running",
            "last_completed_node": "verify",
            "current_node": "write",
            "current_research_unit": "unit with sk-secret",
            "source_ledger": [{"url": "https://example.test/secret"}],
            "evidence_ledger": [{"evidence_id": "EVID-001"}],
            "executor_state": {"token": "sk-secret"},
            "completed_unit_ids": ["unit-1", "unit-2"],
            "pending_unit_ids": ["unit-3"],
            "resume_policy": "fail_recoverable",
            "recoverable_reason": "OPENAI_API_KEY=sk-secret",
        },
        checkpoint_updated_at="2026-06-06T12:05:01+00:00",
    )

    payload = _task_response(record)
    operational = payload["operational_status"]

    assert_public_task_response_hides_internal_identifiers(payload)
    assert operational["lease"] == {
        "state": "leased",
        "worker_present": True,
        "lease_expires_at": "2026-06-06T12:10:00+00:00",
        "heartbeat_at": "2026-06-06T12:05:00+00:00",
        "attempt": 2,
    }
    assert operational["checkpoint"]["phase"] == "verify"
    assert operational["checkpoint"]["status"] == "running"
    assert operational["checkpoint"]["last_completed_node"] == "verify"
    assert operational["checkpoint"]["current_node"] == "write"
    assert operational["checkpoint"]["current_research_unit_present"] is True
    assert operational["checkpoint"]["source_ledger_count"] == 1
    assert operational["checkpoint"]["evidence_ledger_count"] == 1
    assert operational["checkpoint"]["completed_unit_count"] == 2
    assert operational["checkpoint"]["pending_unit_count"] == 1
    assert operational["checkpoint"]["executor_state_present"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "runner-one:task-observable:worker-secret" not in serialized
    assert "unit with sk-secret" not in serialized
    assert "\"executor_state\"" not in serialized
    assert "sk-secret" not in serialized
    assert "base_url" not in serialized


@pytest.mark.parametrize("executor", ["legacy", "serial", "langgraph"])
def test_task_response_exposes_selected_research_graph_executor(executor):
    record = TaskRecord(
        task_id=f"task-executor-{executor}",
        user_id="user-a",
        query="observable query",
        status="completed",
        archive_status="ready",
        archive_dir=None,
        archive_zip_path=None,
        created_at="2026-06-06T11:59:59+00:00",
        started_at="2026-06-06T12:00:00+00:00",
        completed_at="2026-06-06T12:00:10+00:00",
        model_summary={},
        checkpoint={
            "phase": "finished",
            "status": "completed",
            "executor_state": {"research_graph_executor": executor},
            "research_graph_executor": executor,
            "resume_policy": "terminal",
            "recoverable_reason": None,
        },
    )

    payload = _task_response(record)

    assert_public_task_response_hides_internal_identifiers(payload)
    assert (
        payload["operational_status"]["checkpoint"]["research_graph_executor"]
        == executor
    )
    assert "\"executor_state\"" not in json.dumps(payload, ensure_ascii=False)


def test_stale_recovery_requeues_resumable_langgraph_checkpoint(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    app = create_app(
        task_store=store,
        archive_root=tmp_path / "archives",
        service_token="shared",
        stream_events=completed_stream,
        init_render_state=init_state,
        update_state_with_event=update_state,
        render_markdown=render_markdown,
        clock=lambda: "2026-06-06T12:10:00+00:00",
    )
    record = seed_running_task(
        store,
        task_id="task-resumable-langgraph",
        worker_id="worker-resume",
        lease_expires_at="2026-06-06T12:05:00+00:00",
    )
    checkpoint = _checkpoint_envelope(
        {
            "phase": "research",
            "status": "running",
            "current_research_unit": "unit-1",
            "source_ledger": [{"ledger_type": "research_unit", "unit_id": "unit-1"}],
            "evidence_ledger": [],
            "executor_state": {"research_graph_executor": "langgraph"},
            "research_graph_executor": "langgraph",
            "last_completed_node": "research",
            "current_node": "compress",
            "completed_unit_ids": ["unit-1"],
            "pending_unit_ids": [],
            "resume_policy": "resume_from_checkpoint",
            "recoverable_reason": "langgraph_checkpoint_resumable",
        }
    )
    store.write_task_checkpoint(
        record.task_id,
        checkpoint=checkpoint,
        updated_at="2026-06-06T12:04:00+00:00",
    )
    running = store.get_task(record.task_id)

    resumed = _reconcile_task_if_stale(app, running)

    assert resumed.status == "queued"
    assert resumed.archive_status == "pending"
    assert resumed.worker_id is None
    assert resumed.lease_expires_at is None
    assert resumed.error is None
    assert resumed.warnings == [
        "stale running LangGraph task queued for resume: expired_lease"
    ]
    persisted_checkpoint = store.get_task_checkpoint(record.task_id)
    assert persisted_checkpoint["status"] == "queued"
    assert persisted_checkpoint["resume_policy"] == "resume_from_checkpoint"
    assert persisted_checkpoint["recoverable_reason"] is None
    assert persisted_checkpoint["executor_state"]["resume_status"] == "queued"
    assert store.list_task_events(record.task_id)[-1]["payload"]["status"] == "queued"
    stream_context = _stream_task_context(resumed)
    assert stream_context["resume_checkpoint"]["phase"] == "research"
    assert stream_context["resume_checkpoint"]["current_node"] == "compress"


@pytest.mark.asyncio
async def test_stale_recovery_schedules_worker_and_preserves_resume_checkpoint(
    tmp_path,
):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    blocking_stream = BlockingStream()
    app = create_app(
        task_store=store,
        archive_root=tmp_path / "archives",
        service_token="shared",
        stream_events=blocking_stream.stream,
        init_render_state=init_state,
        update_state_with_event=update_state,
        render_markdown=render_markdown,
        clock=lambda: "2026-06-06T12:10:00+00:00",
    )
    record = seed_running_task(
        store,
        task_id="task-resumable-langgraph-worker",
        worker_id="worker-resume",
        lease_expires_at="2026-06-06T12:05:00+00:00",
    )
    checkpoint = _checkpoint_envelope(
        {
            "phase": "research",
            "status": "running",
            "current_research_unit": "unit-1",
            "source_ledger": [{"ledger_type": "research_unit", "unit_id": "unit-1"}],
            "evidence_ledger": [{"ledger_type": "evidence", "id": "EVID-001"}],
            "executor_state": {"research_graph_executor": "langgraph"},
            "research_graph_executor": "langgraph",
            "last_completed_node": "research",
            "current_node": "compress",
            "completed_unit_ids": ["unit-1"],
            "pending_unit_ids": [],
            "resume_policy": "resume_from_checkpoint",
            "recoverable_reason": "langgraph_checkpoint_resumable",
        }
    )
    store.write_task_checkpoint(
        record.task_id,
        checkpoint=checkpoint,
        updated_at="2026-06-06T12:04:00+00:00",
    )
    running = store.get_task(record.task_id)

    resumed = _reconcile_task_if_stale(app, running)
    await asyncio.wait_for(blocking_stream.started.wait(), timeout=1.0)

    worker = app[TASK_WORKERS_KEY].get(record.task_id)
    persisted_checkpoint = store.get_task_checkpoint(record.task_id)
    active = store.get_task(record.task_id)

    assert resumed.status == "queued"
    assert worker is not None
    assert not worker.done()
    assert active.status == "running"
    assert persisted_checkpoint["phase"] == "research"
    assert persisted_checkpoint["status"] == "running"
    assert persisted_checkpoint["resume_policy"] == "resume_from_checkpoint"
    assert persisted_checkpoint["current_node"] == "compress"
    assert persisted_checkpoint["source_ledger"] == checkpoint["source_ledger"]
    assert persisted_checkpoint["evidence_ledger"] == checkpoint["evidence_ledger"]
    assert persisted_checkpoint["executor_state"]["resume_status"] == "running"
    worker.cancel()
    await worker


async def start_task(client, headers=USER_A_HEADERS, query="test query"):
    response = await client.post(
        "/limira-runner/research",
        headers=headers,
        json={"query": query, "client_options": {"stream": True}},
    )
    assert response.status == 202
    return await response.json()


def seed_queued_task(store, user_id="user-a", query="test query"):
    return store.create_task(
        task_id=str(uuid.uuid4()),
        user_id=user_id,
        query=query,
        created_at="2026-06-06T11:59:59+00:00",
        model_summary={},
    )


async def complete_task(client, headers=USER_A_HEADERS):
    start_payload = await start_task(client, headers=headers)
    task_id = start_payload["task_id"]
    events_response = await client.get(
        f"/limira-runner/tasks/{task_id}/events",
        headers=headers,
    )
    assert events_response.status == 200
    await events_response.text()
    return task_id


async def wait_for_task_status(store, task_id, status, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = store.get_task(task_id)
        if record and record.status == status:
            return record
        await asyncio.sleep(0.01)
    record = store.get_task(task_id)
    actual = record.status if record else None
    raise AssertionError(f"task {task_id} status is {actual!r}, expected {status!r}")


def seed_running_task(
    store,
    *,
    user_id="user-a",
    query="stale running query",
    task_id=None,
    started_at="2026-06-06T10:00:00+00:00",
    worker_id=None,
    lease_expires_at=None,
):
    record = store.create_task(
        task_id=task_id or str(uuid.uuid4()),
        user_id=user_id,
        query=query,
        created_at="2026-06-06T09:59:59+00:00",
        model_summary={},
    )
    claimed = store.claim_queued_task(
        record.task_id,
        started_at=started_at,
        worker_id=worker_id,
        lease_expires_at=lease_expires_at,
    )
    assert claimed is not None
    return claimed


def archive_dir_for(store, task_id):
    record = store.get_task(task_id)
    assert record is not None
    assert record.archive_dir is not None
    return Path(record.archive_dir)


def assert_zip_members(zip_path):
    with zipfile.ZipFile(zip_path) as archive:
        assert sorted(archive.namelist()) == ARCHIVE_MEMBERS


def assert_diagnostic_archive(store, task_id, expected_status, forbidden_text):
    archive_dir = archive_dir_for(store, task_id)
    expected_files = {*ARCHIVE_MEMBERS, "archive.zip"}
    assert expected_files.issubset({path.name for path in archive_dir.iterdir()})

    metadata = json.loads((archive_dir / "metadata.json").read_text(encoding="utf-8"))
    trace = json.loads((archive_dir / "trace.json").read_text(encoding="utf-8"))
    report = (archive_dir / "report.md").read_text(encoding="utf-8")
    report_html = (archive_dir / "report.html").read_text(encoding="utf-8")

    assert metadata["status"] == expected_status
    assert trace["events"]
    assert report.strip()
    assert f"Limira Research {expected_status.title()}" in report
    assert "<!doctype html>" in report_html
    assert forbidden_text not in json.dumps(metadata)
    assert forbidden_text not in json.dumps(trace)
    assert forbidden_text not in report
    assert forbidden_text not in report_html

    record = store.get_task(task_id)
    assert record.archive_zip_path is not None
    assert_zip_members(record.archive_zip_path)


@pytest.mark.asyncio
async def test_runner_api_start_events_status_and_download(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]

        events_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        sse_events = parse_sse(await events_response.text())

        assert [event["type"] for event in sse_events] == ["heartbeat", "message"]
        assert sse_events[0]["task_id"] == task_id
        assert sse_events[1]["payload"]["event"] == "message"

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        assert status_payload["status"] == "completed"
        assert status_payload["archive_status"] == "ready"
        assert (
            status_payload["download_url"]
            == f"/limira-runner/tasks/{task_id}/archive.zip"
        )
        assert_public_task_response_hides_internal_identifiers(status_payload)

        record = store.get_task(task_id)
        assert record.archive_dir is not None
        archive_dir = Path(record.archive_dir)
        trace = json.loads((archive_dir / "trace.json").read_text(encoding="utf-8"))
        assert [event["type"] for event in trace["events"]] == ["message"]
        assert "ignoredsecret" not in json.dumps(trace)

        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "final from state" in report

        download_response = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert download_response.status == 200
        with zipfile.ZipFile(io.BytesIO(await download_response.read())) as archive:
            assert sorted(archive.namelist()) == ARCHIVE_MEMBERS
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_startup_recovers_stale_running_task_without_worker(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    running = seed_running_task(store)
    client, store = await make_client(tmp_path, task_store=store)
    try:
        recovered = store.get_task(running.task_id)
        assert recovered.status == "failed"
        assert recovered.archive_status == "failed"
        assert recovered.completed_at == "2026-06-06T12:00:01+00:00"
        assert recovered.error == "stale_running_task_recovered:no_active_worker"
        assert recovered.warnings == [
            "stale running task recovered: no_active_worker"
        ]
        assert_checkpoint_envelope(
            recovered.checkpoint,
            phase="recovered",
            status="failed",
            resume_policy="terminal",
        )
        assert recovered.checkpoint["executor_state"]["recovery_reason"] == (
            "no_active_worker"
        )

        status_response = await client.get(
            f"/limira-runner/tasks/{running.task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        assert status_payload["status"] == "failed"
        assert status_payload["archive_status"] == "failed"
        assert status_payload["download_url"] is None
        assert status_payload["error"] == "stale_running_task_recovered:no_active_worker"
        assert status_payload["warnings"] == [
            "stale running task recovered: no_active_worker"
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_startup_preserves_lease_renewed_during_stale_recovery(
    tmp_path,
):
    store = RenewingStaleRecoveryRaceStore(tmp_path / "tasks.sqlite3")
    running = seed_running_task(
        store,
        worker_id="runner-other:task-stale-race:worker",
        lease_expires_at="2026-06-06T11:00:00+00:00",
    )
    client, store = await make_client(tmp_path, task_store=store)
    try:
        current = store.get_task(running.task_id)

        assert store.renewed_during_recovery is True
        assert current.status == "running"
        assert current.worker_id == "runner-other:task-stale-race:worker"
        assert current.lease_expires_at == "2026-06-06T13:00:00+00:00"
        assert current.completed_at is None
        assert current.error is None
        assert current.checkpoint == {}
        assert store.list_task_events(running.task_id) == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_status_recovers_stale_running_task_without_worker(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        running = seed_running_task(store)

        status_response = await client.get(
            f"/limira-runner/tasks/{running.task_id}",
            headers=USER_A_HEADERS,
        )
        payload = await status_response.json()
        recovered = store.get_task(running.task_id)

        assert status_response.status == 200
        assert payload["status"] == "failed"
        assert payload["archive_status"] == "failed"
        assert payload["error"] == "stale_running_task_recovered:no_active_worker"
        assert recovered.status == "failed"
        assert recovered.completed_at == "2026-06-06T12:00:01+00:00"
        assert_checkpoint_envelope(
            recovered.checkpoint,
            phase="recovered",
            status="failed",
            resume_policy="terminal",
        )
        assert client.server.app[ACTIVE_TASKS_KEY] == set()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_event_stream_recovers_stale_running_task_without_worker(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        running = seed_running_task(store)

        events_response = await client.get(
            f"/limira-runner/tasks/{running.task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        events = parse_sse(await events_response.text())
        recovered = store.get_task(running.task_id)

        assert recovered.status == "failed"
        assert [event["type"] for event in events] == ["error"]
        assert events[0]["payload"]["status"] == "failed"
        assert events[0]["payload"]["archive_status"] == "failed"
        assert events[0]["payload"]["error"] == (
            "stale_running_task_recovered:no_active_worker"
        )
        assert events[0]["payload"]["warning"] == (
            "stale running task recovered: no_active_worker"
        )
        assert events[0]["payload"]["recovery_reason"] == "no_active_worker"
        assert_checkpoint_envelope(
            recovered.checkpoint,
            phase="recovered",
            status="failed",
            resume_policy="terminal",
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_event_stream_polls_durable_events_for_external_lease(
    tmp_path,
):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    running = seed_running_task(
        store,
        task_id="task-external-stream",
        worker_id="runner-other:task-external-stream:worker",
        lease_expires_at="2026-06-06T13:00:00+00:00",
    )
    initial_event = {
        "task_id": running.task_id,
        "type": "message",
        "timestamp": "2026-06-06T12:00:00+00:00",
        "payload": {"delta": {"content": "initial durable replay"}},
    }
    later_event = {
        "task_id": running.task_id,
        "type": "message",
        "timestamp": "2026-06-06T12:00:02+00:00",
        "payload": {"delta": {"content": "externally appended"}},
    }
    store.append_task_event(
        running.task_id,
        initial_event,
        created_at=initial_event["timestamp"],
    )
    client, store = await make_client(tmp_path, task_store=store)
    try:
        events_response = await client.get(
            f"/limira-runner/tasks/{running.task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        first_line = await read_sse_data_line(events_response)
        assert b"initial durable replay" in first_line

        store.append_task_event(
            running.task_id,
            later_event,
            created_at=later_event["timestamp"],
        )
        second_line = await read_sse_data_line(events_response)
        assert b"externally appended" in second_line

        store.update_task(
            running.task_id,
            status="completed",
            completed_at="2026-06-06T12:00:03+00:00",
        )
        await asyncio.wait_for(events_response.text(), timeout=1)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_status_preserves_healthy_active_running_task(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        running = seed_running_task(
            store,
            started_at="2026-06-06T12:00:00+00:00",
        )
        client.server.app[ACTIVE_TASKS_KEY].add(running.task_id)

        status_response = await client.get(
            f"/limira-runner/tasks/{running.task_id}",
            headers=USER_A_HEADERS,
        )
        payload = await status_response.json()
        current = store.get_task(running.task_id)

        assert status_response.status == 200
        assert payload["status"] == "running"
        assert payload["archive_status"] == "pending"
        assert payload["error"] is None
        assert current.status == "running"
        assert current.completed_at is None
    finally:
        client.server.app[ACTIVE_TASKS_KEY].discard(running.task_id)
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_status_recovers_active_task_with_stale_started_at(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("RUNNER_STALE_RUNNING_SECONDS", "60")
    client, store = await make_client(tmp_path)
    try:
        running = seed_running_task(
            store,
            started_at="2026-06-06T10:00:00+00:00",
        )
        client.server.app[ACTIVE_TASKS_KEY].add(running.task_id)

        status_response = await client.get(
            f"/limira-runner/tasks/{running.task_id}",
            headers=USER_A_HEADERS,
        )
        payload = await status_response.json()
        current = store.get_task(running.task_id)

        assert status_response.status == 200
        assert payload["status"] == "failed"
        assert payload["error"] == "stale_running_task_recovered:stale_started_at"
        assert current.status == "failed"
        assert current.warnings == [
            "stale running task recovered: stale_started_at"
        ]
        assert running.task_id not in client.server.app[ACTIVE_TASKS_KEY]
    finally:
        client.server.app[ACTIVE_TASKS_KEY].discard(running.task_id)
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_accepts_max_length_query_with_default_context(tmp_path):
    client, store = await make_client(tmp_path, stream_events=echo_query_stream)
    try:
        max_query = "q" * MAX_QUERY_CHARS
        response = await client.post(
            "/limira-runner/research",
            headers=USER_A_HEADERS,
            json={"query": max_query},
        )

        assert response.status == 202
        task_id = (await response.json())["task_id"]
        completed = await wait_for_task_status(store, task_id, "completed")
        assert completed.query == max_query
        assert completed.context["query"] == max_query

        oversized_response = await client.post(
            "/limira-runner/research",
            headers=USER_A_HEADERS,
            json={"query": f"{max_query}x"},
        )
        assert oversized_response.status == 400
        assert await oversized_response.json() == {"error": "query_too_long"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_still_rejects_oversized_optional_context(tmp_path):
    client, _store = await make_client(tmp_path, stream_events=echo_query_stream)
    try:
        response = await client.post(
            "/limira-runner/research",
            headers=USER_A_HEADERS,
            json={
                "query": "short query",
                "upload_scope": {"blob": "x" * MAX_CONTEXT_JSON_CHARS},
            },
        )

        assert response.status == 400
        assert await response.json() == {"error": "task_context_too_large"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_persists_and_streams_task_context(tmp_path):
    context_probe = ContextCaptureStream()
    client, store = await make_client(tmp_path, stream_events=context_probe.stream)
    try:
        response = await client.post(
            "/limira-runner/research",
            headers=USER_A_HEADERS,
            json={
                "query": "context query",
                "scenario": "sanctions_export_controls",
                "conversation_id": "conversation-a",
                "document_ids": ["doc-a", "doc-a", "doc-b"],
                "upload_scope": {
                    "document_ids": ["doc-a", "doc-b"],
                    "document_count": 2,
                    "retrieval_status": "context_only",
                    "source_payloads": [
                        {
                            "document_id": "doc-a",
                            "text": "Uploaded excerpt visible to runner.",
                            "content_hash": "hash-a",
                            "retrieved_at": "2026-06-06T12:00:00+00:00",
                        }
                    ],
                },
                "source_policy": {
                    "min_sources": 5,
                    "prefer_uploaded_documents": True,
                },
            },
        )
        assert response.status == 202
        task_id = (await response.json())["task_id"]

        completed = await wait_for_task_status(store, task_id, "completed")
        assert completed.context["query"] == "context query"
        assert completed.context["scenario"] == "sanctions_export_controls"
        assert completed.context["conversation_id"] == "conversation-a"
        assert completed.context["document_ids"] == ["doc-a", "doc-b"]
        assert completed.context["upload_scope"]["document_count"] == 2
        assert completed.context["upload_scope"]["source_payloads"][0]["text"] == (
            "Uploaded excerpt visible to runner."
        )
        assert completed.context["source_policy"]["min_sources"] == 5
        assert completed.context["source_policy"]["prefer_uploaded_documents"] is True
        assert context_probe.context == completed.context

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        assert "context" not in status_payload
        assert_public_task_response_hides_internal_identifiers(status_payload)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_task_continues_after_event_client_disconnect(tmp_path):
    probe = BackgroundExecutionProbe()
    client, store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        await asyncio.wait_for(probe.started.wait(), timeout=1)
        running = await wait_for_task_status(store, task_id, "running")
        assert running.archive_status == "pending"

        events_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        first_line = await asyncio.wait_for(events_response.content.readline(), timeout=1)
        assert b"partial" in first_line
        events_response.release()

        probe.release.set()
        completed = await wait_for_task_status(store, task_id, "completed")
        assert completed.archive_status == "ready"

        replay_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert replay_response.status == 200
        replay_events = parse_sse(await replay_response.text())
        assert [event["type"] for event in replay_events] == ["message", "message"]
        assert store.get_task(task_id).status == "completed"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_persists_durable_events_checkpoint_and_replays_after_restart(
    tmp_path,
):
    client, store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        events = parse_sse(await events_response.text())
        completed = await wait_for_task_status(store, task_id, "completed")

        assert [event["type"] for event in events] == ["heartbeat", "message"]
        assert completed.worker_id is None
        assert completed.lease_expires_at is None
        assert completed.heartbeat_at is None
        assert_checkpoint_envelope(
            completed.checkpoint,
            phase="finished",
            status="completed",
            resume_policy="terminal",
        )
        assert [event["type"] for event in store.list_task_events(task_id)] == [
            "heartbeat",
            "message",
        ]
    finally:
        await client.close()

    restarted_app = create_app(
        task_store=store,
        archive_root=tmp_path / "archives-restarted",
        service_token="shared",
        stream_events=completed_stream,
        init_render_state=init_state,
        update_state_with_event=update_state,
        render_markdown=render_markdown,
        clock=Clock(),
    )
    assert restarted_app[TASK_EVENT_LOG_KEY] == {}
    restarted_client = TestClient(TestServer(restarted_app))
    await restarted_client.start_server()
    try:
        replay_response = await restarted_client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert replay_response.status == 200
        replay_events = parse_sse(await replay_response.text())
        assert [event["type"] for event in replay_events] == ["heartbeat", "message"]
        assert [event["type"] for event in restarted_app[TASK_EVENT_LOG_KEY][task_id]] == [
            "heartbeat",
            "message",
        ]
    finally:
        await restarted_client.close()


@pytest.mark.parametrize("executor", ["legacy", "serial", "langgraph"])
@pytest.mark.asyncio
async def test_runner_api_persists_executor_marker_in_operational_status(
    tmp_path,
    executor,
):
    client, store = await make_client(
        tmp_path,
        stream_events=executor_marker_stream(executor),
    )
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        events = parse_sse(await events_response.text())
        completed = await wait_for_task_status(store, task_id, "completed")

        assert [event["type"] for event in events] == [
            "research_graph_executor_selected",
            "message",
        ]
        assert completed.checkpoint["research_graph_executor"] == executor
        assert (
            completed.checkpoint["executor_state"]["research_graph_executor"]
            == executor
        )

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        assert status_response.status == 200
        status_payload = await status_response.json()
        assert_public_task_response_hides_internal_identifiers(status_payload)
        assert (
            status_payload["operational_status"]["checkpoint"][
                "research_graph_executor"
            ]
            == executor
        )
    finally:
        await client.close()


@pytest.mark.parametrize(
    "error_text",
    [
        "invalid_research_graph_executor: 'bogus'",
        "langgraph_executor_not_implemented: install/declare the LangGraph dependency",
    ],
)
@pytest.mark.asyncio
async def test_runner_api_pipeline_error_event_marks_task_failed(
    tmp_path,
    error_text,
):
    client, store = await make_client(
        tmp_path,
        stream_events=pipeline_error_stream(error_text),
    )
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        events = parse_sse(await events_response.text())
        failed = await wait_for_task_status(store, task_id, "failed")

        assert [event["type"] for event in events] == ["error"]
        assert error_text in (failed.error or "")
        assert failed.archive_status == "ready"

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        assert status_response.status == 200
        status_payload = await status_response.json()
        assert status_payload["status"] == "failed"
        assert error_text in status_payload["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_persists_graph_checkpoint_stream_events(tmp_path):
    probe = GraphCheckpointProbe()
    client, store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/limira-runner/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        running = await wait_for_task_status(store, task_id, "running")
        assert running.checkpoint["phase"] == "verify"
        assert running.checkpoint["status"] == "running"
        assert running.checkpoint["resume_policy"] == "fail_recoverable"
        assert running.checkpoint["current_research_unit"] == "unit-1-background"
        assert running.checkpoint["source_ledger"] == [
            {"unit_id": "unit-1-background"}
        ]
        assert running.checkpoint["evidence_ledger"] == [
            {"id": "EVID-abcdef123456"}
        ]
        assert running.checkpoint["executor_state"] == {"node": "VerifierNode"}
        assert running.checkpoint["recoverable_reason"] == (
            "serial_graph_checkpoint_not_resumable"
        )

        probe.release.set()
        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        await asyncio.wait_for(events_response.text(), timeout=1)
        completed = await wait_for_task_status(store, task_id, "completed")
        assert completed.checkpoint["phase"] == "verify"
        assert completed.checkpoint["status"] == "completed"
        assert completed.checkpoint["resume_policy"] == "terminal"
        assert completed.checkpoint["recoverable_reason"] is None
        assert completed.checkpoint["current_research_unit"] == "unit-1-background"
        assert completed.checkpoint["source_ledger"] == [
            {"unit_id": "unit-1-background"}
        ]
        assert completed.checkpoint["evidence_ledger"] == [
            {"id": "EVID-abcdef123456"}
        ]
        assert completed.checkpoint["executor_state"] == {"node": "VerifierNode"}
        operational_checkpoint = _task_response(completed)["operational_status"][
            "checkpoint"
        ]
        assert operational_checkpoint["source_ledger_count"] == 1
        assert operational_checkpoint["evidence_ledger_count"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_preserves_executor_marker_on_graph_checkpoint(tmp_path):
    probe = GraphCheckpointWithExecutorProbe()
    client, store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/limira-runner/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        running = await wait_for_task_status(store, task_id, "running")
        assert running.checkpoint["phase"] == "verify"
        assert running.checkpoint["research_graph_executor"] == "serial"
        assert (
            running.checkpoint["executor_state"]["research_graph_executor"] == "serial"
        )

        probe.release.set()
        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        await asyncio.wait_for(events_response.text(), timeout=1)
        completed = await wait_for_task_status(store, task_id, "completed")
        assert completed.checkpoint["research_graph_executor"] == "serial"
        assert (
            completed.checkpoint["executor_state"]["research_graph_executor"]
            == "serial"
        )
        operational_checkpoint = _task_response(completed)["operational_status"][
            "checkpoint"
        ]
        assert operational_checkpoint["research_graph_executor"] == "serial"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_renews_durable_lease_while_task_is_active(tmp_path):
    probe = BackgroundExecutionProbe()
    client, store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/limira-runner/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        await asyncio.wait_for(probe.started.wait(), timeout=1)
        running = await wait_for_task_status(store, task_id, "running")

        assert running.worker_id
        assert running.lease_expires_at
        assert running.heartbeat_at
        assert running.attempt == 1
        assert_checkpoint_envelope(
            running.checkpoint,
            phase="stream",
            status="completed",
            resume_policy="fail_recoverable",
        )
        assert running.checkpoint["executor_state"]["last_event_type"] == "message"

        probe.release.set()
        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        await asyncio.wait_for(events_response.text(), timeout=1)
        completed = await wait_for_task_status(store, task_id, "completed")
        assert completed.worker_id is None
        assert_checkpoint_envelope(
            completed.checkpoint,
            phase="finished",
            status="completed",
            resume_policy="terminal",
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_persists_host_only_model_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEFAULT_MODEL_NAME", "deepseek-chat")
    monkeypatch.setenv(
        "BASE_URL",
        "https://user:secret-token@api.deepseek.com/v1?api_key=query-secret#frag",
    )
    client, store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        record = store.get_task(start_payload["task_id"])
        serialized_model_summary = json.dumps(record.model_summary)

        assert record.model_summary == {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url_host": "api.deepseek.com",
        }
        assert "base_url" not in record.model_summary
        assert "secret-token" not in serialized_model_summary
        assert "query-secret" not in serialized_model_summary
        assert "/v1" not in serialized_model_summary
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_uses_deepseek_defaults_for_model_summary(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("DEFAULT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEFAULT_MODEL_NAME", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    client, store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        record = store.get_task(start_payload["task_id"])

        assert record.model_summary == {
            "provider": "openai",
            "model": "deepseek-v4-pro",
            "base_url_host": "api.deepseek.com",
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_scrubs_structured_secrets_before_report_rendering(
    tmp_path,
):
    client, store = await make_client(
        tmp_path,
        stream_events=structured_secret_stream,
        update_state_func=update_state_with_structured_secret,
    )
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]

        events_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        sse_payload = await events_response.text()

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        assert status_payload["status"] == "completed"
        assert status_payload["archive_status"] == "ready"

        record = store.get_task(task_id)
        archive_dir = Path(record.archive_dir)
        artifact_text = "\n".join(
            [
                sse_payload,
                (archive_dir / "trace.json").read_text(encoding="utf-8"),
                (archive_dir / "metadata.json").read_text(encoding="utf-8"),
                (archive_dir / "report.md").read_text(encoding="utf-8"),
                (archive_dir / "report.html").read_text(encoding="utf-8"),
            ]
        )
        with zipfile.ZipFile(record.archive_zip_path) as archive:
            zip_text = "\n".join(
                archive.read(name).decode("utf-8") for name in archive.namelist()
            )

        assert "tiny-leak-value" not in artifact_text
        assert "tiny-leak-value" not in zip_text
        assert "token=[REDACTED]" in artifact_text
        assert '"token": "[REDACTED]"' in artifact_text
        assert "safe visible content" in artifact_text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_stream_setup_failure_finalizes_claimed_task(tmp_path):
    client, store = await make_client(tmp_path, writer_cls=StartFailWriter)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        await wait_for_task_status(store, task_id, "failed")

        events_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        setup_events = parse_sse(await events_response.text())
        assert setup_events[-1]["type"] == "error"

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        serialized_status = json.dumps(status_payload)
        assert status_payload["status"] == "failed"
        assert status_payload["archive_status"] == "failed"
        assert status_payload["download_url"] is None
        assert status_payload["completed_at"] is not None
        assert status_payload["error"] == "Authorization: [REDACTED]"
        assert "setupsecret123456" not in serialized_status

        record = store.get_task(task_id)
        assert record.status == "failed"
        assert record.archive_status == "failed"
        assert record.archive_dir is None
        assert record.archive_zip_path is None
        assert_checkpoint_envelope(
            record.checkpoint,
            phase="finished",
            status="failed",
            resume_policy="terminal",
        )
        assert "setupsecret123456" not in json.dumps(record.to_dict())

        retry_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert retry_response.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_archive_finalization_failure_finalizes_claimed_task(
    tmp_path,
):
    client, store = await make_client(tmp_path, writer_cls=CompleteFailWriter)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        await wait_for_task_status(store, task_id, "completed")

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        serialized_status = json.dumps(status_payload)

        assert status_payload["status"] == "completed"
        assert status_payload["archive_status"] == "failed"
        assert status_payload["download_url"] is None
        assert status_payload["completed_at"] is not None
        assert status_payload["error"] is None
        assert status_payload["warnings"] == [
            "archive finalization failed: Authorization: [REDACTED]"
        ]
        assert "finalsecret123456" not in serialized_status
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]
        assert task_id not in client.server.app[ACTIVE_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "completed"
        assert record.archive_status == "failed"
        assert record.archive_dir is not None
        assert record.archive_zip_path is None
        assert "finalsecret123456" not in json.dumps(record.to_dict())

        retry_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert retry_response.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_renderer_failure_finalizes_claimed_task(tmp_path):
    client, store = await make_client(
        tmp_path,
        render_markdown_func=render_markdown_failure,
    )
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        await wait_for_task_status(store, task_id, "completed")

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        serialized_status = json.dumps(status_payload)

        assert status_payload["status"] == "completed"
        assert status_payload["archive_status"] == "failed"
        assert status_payload["download_url"] is None
        assert status_payload["completed_at"] is not None
        assert status_payload["error"] is None
        assert status_payload["warnings"] == [
            "report rendering failed: Authorization: [REDACTED]"
        ]
        assert "rendersecret123456" not in serialized_status
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]
        assert task_id not in client.server.app[ACTIVE_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "completed"
        assert record.archive_status == "failed"
        assert record.archive_zip_path is None
        assert "rendersecret123456" not in json.dumps(record.to_dict())

        retry_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert retry_response.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_rejects_foreign_user_and_not_ready_download(tmp_path):
    probe = BackgroundExecutionProbe()
    client, _store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        foreign_status = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_B_HEADERS,
        )
        assert foreign_status.status == 404

        foreign_events = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_B_HEADERS,
        )
        assert foreign_events.status == 404

        not_ready_download = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert not_ready_download.status == 409
        assert (await not_ready_download.json())["error"] == "archive_not_ready"
        probe.release.set()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_allows_admin_but_blocks_foreign_download(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        await events_response.text()

        foreign_download = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=USER_B_HEADERS,
        )
        assert foreign_download.status == 404

        admin_status = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=ADMIN_HEADERS,
        )
        assert admin_status.status == 200

        admin_download = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=ADMIN_HEADERS,
        )
        assert admin_download.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_foreign_user_denied_for_status(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{start_payload['task_id']}",
            headers=USER_B_HEADERS,
        )
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_foreign_user_denied_for_events(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{start_payload['task_id']}/events",
            headers=USER_B_HEADERS,
        )
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_foreign_user_denied_for_archive_download(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        task_id = await complete_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=USER_B_HEADERS,
        )
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_foreign_user_denied_for_cancel(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.post(
            f"/limira-runner/tasks/{start_payload['task_id']}/cancel",
            headers=USER_B_HEADERS,
        )
        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_status(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{start_payload['task_id']}",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["status"] in {"queued", "running", "completed"}
        assert_public_task_response_hides_internal_identifiers(payload)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_events(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{start_payload['task_id']}/events",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        sse_events = parse_sse(await response.text())
        assert [event["type"] for event in sse_events] == ["heartbeat", "message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_archive_download(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        task_id = await complete_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task_id}/archive.zip",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
            assert sorted(archive.namelist()) == ARCHIVE_MEMBERS
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_cancel(tmp_path):
    probe = CancellationProbe()
    client, store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        await asyncio.wait_for(probe.started.wait(), timeout=1)
        response = await client.post(
            f"/limira-runner/tasks/{start_payload['task_id']}/cancel",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["status"] == "running"
        assert payload["archive_status"] == "pending"
        assert payload["cancel_requested"] is True
        assert_public_task_response_hides_internal_identifiers(payload)
        await wait_for_task_status(store, start_payload["task_id"], "cancelled")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_queued_cancel(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        record = seed_queued_task(store)
        response = await client.post(
            f"/limira-runner/tasks/{record.task_id}/cancel",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["status"] == "cancelled"
        assert payload["archive_status"] == "ready"
        assert payload["cancel_requested"] is True
        assert_public_task_response_hides_internal_identifiers(payload)
        record = store.get_task(record.task_id)
        assert record.status == "cancelled"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_endpoint_stops_active_stream_and_archives(tmp_path):
    probe = CancellationProbe()
    client, store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/limira-runner/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        await asyncio.wait_for(probe.started.wait(), timeout=1)
        assert task_id in client.server.app[ACTIVE_TASKS_KEY]

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        assert (await cancel_response.json())["cancel_requested"] is True

        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        await asyncio.wait_for(events_response.text(), timeout=1)
        await asyncio.wait_for(probe.stopped.wait(), timeout=1)
        assert probe.check_count > 0

        status_response = await client.get(
            f"/limira-runner/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        assert status_payload["status"] == "cancelled"
        assert status_payload["archive_status"] == "ready"
        assert_public_task_response_hides_internal_identifiers(status_payload)

        record = store.get_task(task_id)
        archive_dir = Path(record.archive_dir)
        metadata = json.loads((archive_dir / "metadata.json").read_text())
        assert metadata["status"] == "cancelled"
        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "Limira Research Cancelled" in report
        assert_zip_members(record.archive_zip_path)
        assert task_id not in client.server.app[ACTIVE_TASKS_KEY]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_finalizes_running_task_without_active_worker(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        seeded = seed_queued_task(store)
        task_id = seeded.task_id
        claimed = store.claim_queued_task(
            task_id,
            started_at="2026-06-06T12:30:00+00:00",
        )
        assert claimed is not None
        assert task_id not in client.server.app[ACTIVE_TASKS_KEY]

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()

        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["archive_status"] == "ready"
        assert cancel_payload["download_url"] == (
            f"/limira-runner/tasks/{task_id}/archive.zip"
        )
        assert_public_task_response_hides_internal_identifiers(cancel_payload)
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]
        assert task_id not in client.server.app[ACTIVE_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "cancelled"
        assert record.archive_status == "ready"
        assert record.completed_at is not None
        archive_dir = Path(record.archive_dir)
        metadata = json.loads((archive_dir / "metadata.json").read_text())
        assert metadata["status"] == "cancelled"
        assert metadata["error"] == (
            "task cancelled because no active stream worker was registered"
        )
        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "Limira Research Cancelled" in report
        assert_zip_members(record.archive_zip_path)

        retry_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert retry_response.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_rejects_running_task_owned_by_healthy_external_lease(
    tmp_path,
):
    client, store = await make_client(tmp_path)
    try:
        seeded = seed_running_task(
            store,
            worker_id="runner-other:task-external:worker",
            lease_expires_at="2026-06-06T13:00:00+00:00",
        )

        cancel_response = await client.post(
            f"/limira-runner/tasks/{seeded.task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        cancel_payload = await cancel_response.json()
        current = store.get_task(seeded.task_id)

        assert cancel_response.status == 409
        assert cancel_payload["error"] == "task_owned_by_active_worker"
        assert current.status == "running"
        assert current.worker_id == "runner-other:task-external:worker"
        assert current.lease_expires_at == "2026-06-06T13:00:00+00:00"
        assert current.completed_at is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_finalizes_expired_external_lease_without_worker(
    tmp_path,
):
    client, store = await make_client(tmp_path)
    try:
        seeded = seed_running_task(
            store,
            worker_id="runner-other:task-expired:worker",
            lease_expires_at="2026-06-06T11:00:00+00:00",
        )

        cancel_response = await client.post(
            f"/limira-runner/tasks/{seeded.task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        cancel_payload = await cancel_response.json()
        current = store.get_task(seeded.task_id)

        assert cancel_response.status == 200
        assert cancel_payload["status"] == "cancelled"
        assert current.status == "cancelled"
        assert current.error.startswith(
            "task cancelled because no active stream worker was registered"
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_preserves_lease_renewed_during_no_worker_cancel(
    tmp_path,
):
    store = RenewingCancelRaceStore(tmp_path / "tasks.sqlite3")
    client, store = await make_client(tmp_path, task_store=store)
    try:
        seeded = seed_running_task(
            store,
            worker_id="runner-other:task-cancel-race:worker",
            lease_expires_at="2026-06-06T11:00:00+00:00",
        )

        cancel_response = await client.post(
            f"/limira-runner/tasks/{seeded.task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        cancel_payload = await cancel_response.json()
        current = store.get_task(seeded.task_id)

        assert cancel_response.status == 409
        assert cancel_payload["error"] == "task_owned_by_active_worker"
        assert store.renewed_during_cancel is True
        assert current.status == "running"
        assert current.worker_id == "runner-other:task-cancel-race:worker"
        assert current.lease_expires_at == "2026-06-06T13:00:00+00:00"
        assert current.completed_at is None
        assert current.error is None
        assert current.archive_dir is None
        assert store.list_task_events(seeded.task_id) == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_endpoint_finalizes_queued_task(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        task_id = seed_queued_task(store).task_id

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()
        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["archive_status"] == "ready"
        assert_public_task_response_hides_internal_identifiers(cancel_payload)
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        archive_dir = Path(record.archive_dir)
        metadata = json.loads((archive_dir / "metadata.json").read_text())
        assert metadata["status"] == "cancelled"
        assert metadata["error"] == "task cancelled before stream started"
        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "Limira Research Cancelled" in report
        assert_zip_members(record.archive_zip_path)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_queued_cancel_start_failure_sets_archive_failed(tmp_path):
    client, store = await make_client(tmp_path, writer_cls=StartFailWriter)
    try:
        task_id = seed_queued_task(store).task_id

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()
        serialized_payload = json.dumps(cancel_payload)

        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["archive_status"] == "failed"
        assert cancel_payload["download_url"] is None
        assert cancel_payload["error"] == "task cancelled before stream started"
        assert cancel_payload["warnings"] == [
            "queued cancellation archive finalization failed: Authorization: [REDACTED]"
        ]
        assert "setupsecret123456" not in serialized_payload
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "cancelled"
        assert record.archive_status == "failed"
        assert record.archive_dir is None
        assert record.archive_zip_path is None
        assert "setupsecret123456" not in json.dumps(record.to_dict())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_queued_cancel_complete_failure_sets_archive_failed(tmp_path):
    client, store = await make_client(tmp_path, writer_cls=CompleteFailWriter)
    try:
        task_id = seed_queued_task(store).task_id

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()
        serialized_payload = json.dumps(cancel_payload)

        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["archive_status"] == "failed"
        assert cancel_payload["download_url"] is None
        assert cancel_payload["error"] == "task cancelled before stream started"
        assert cancel_payload["warnings"] == [
            "queued cancellation archive finalization failed: Authorization: [REDACTED]"
        ]
        assert "finalsecret123456" not in serialized_payload
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "cancelled"
        assert record.archive_status == "failed"
        assert record.archive_dir is not None
        assert record.archive_zip_path is None
        assert "finalsecret123456" not in json.dumps(record.to_dict())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_queued_cancel_keeps_signal_when_stream_claim_wins(tmp_path):
    store = StreamClaimRaceStore(tmp_path / "tasks.sqlite3")
    client, _store = await make_client(tmp_path, task_store=store)
    try:
        task_id = seed_queued_task(store).task_id

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()

        assert store.claimed_during_cancel is True
        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "running"
        assert cancel_payload["archive_status"] == "pending"
        assert_public_task_response_hides_internal_identifiers(cancel_payload)
        assert task_id in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "running"
        assert record.archive_status == "pending"
        assert record.archive_dir is None
        assert record.started_at == "2026-06-06T12:59:59+00:00"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_allows_duplicate_running_event_stream_subscription(tmp_path):
    probe = CancellationProbe()
    client, _store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/limira-runner/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        duplicate_response = await client.get(
            f"/limira-runner/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert duplicate_response.status == 200
        assert probe.stream_count == 1

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        await asyncio.wait_for(events_response.text(), timeout=1)
        await asyncio.wait_for(duplicate_response.text(), timeout=1)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_transport_close_does_not_cancel_background_task(tmp_path):
    probe = CancellationProbe()
    transport_closed = False

    def transport_closing(_request):
        return transport_closed

    client, _store = await make_client(
        tmp_path,
        stream_events=probe.stream,
        transport_closing=transport_closing,
    )
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/limira-runner/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        first_line = await asyncio.wait_for(
            events_response.content.readline(),
            timeout=1,
        )
        assert first_line.startswith(b"data: ")
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        transport_closed = True
        await asyncio.sleep(0.05)
        assert not probe.stopped.is_set()

        cancel_response = await client.post(
            f"/limira-runner/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        await asyncio.wait_for(probe.stopped.wait(), timeout=1)
        await asyncio.wait_for(events_response.text(), timeout=1)

        status_payload = None
        for _ in range(20):
            status_response = await client.get(
                f"/limira-runner/tasks/{task_id}",
                headers=USER_A_HEADERS,
            )
            status_payload = await status_response.json()
            if status_payload["status"] == "cancelled":
                break
            await asyncio.sleep(0.02)
        assert status_payload["status"] == "cancelled"
        assert status_payload["archive_status"] == "ready"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_endpoint_enforces_owner_and_admin(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        foreign_task = seed_queued_task(store)
        foreign_cancel = await client.post(
            f"/limira-runner/tasks/{foreign_task.task_id}/cancel",
            headers=USER_B_HEADERS,
        )
        assert foreign_cancel.status == 404

        admin_task = seed_queued_task(store)
        admin_cancel = await client.post(
            f"/limira-runner/tasks/{admin_task.task_id}/cancel",
            headers=ADMIN_HEADERS,
        )
        assert admin_cancel.status == 200
        admin_payload = await admin_cancel.json()
        assert admin_payload["status"] == "cancelled"
        assert admin_payload["archive_status"] == "ready"
        assert admin_payload["cancel_requested"] is True
        assert_public_task_response_hides_internal_identifiers(admin_payload)

        record = store.get_task(admin_task.task_id)
        assert record.status == "cancelled"
        assert record.archive_zip_path is not None

        completed_events = await client.get(
            f"/limira-runner/tasks/{admin_task.task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert completed_events.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_validation_rejects_untrusted_inputs(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        missing_auth = await client.post(
            "/limira-runner/research",
            json={"query": "x"},
        )
        assert missing_auth.status == 401

        body_user = await client.post(
            "/limira-runner/research",
            headers=USER_A_HEADERS,
            json={"query": "x", "user_id": "attacker"},
        )
        assert body_user.status == 400

        empty_query = await client.post(
            "/limira-runner/research",
            headers=USER_A_HEADERS,
            json={"query": "  "},
        )
        assert empty_query.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_failed_outcome_writes_scrubbed_diagnostic_archive(tmp_path):
    client, store = await make_client(tmp_path, stream_events=failed_stream)
    try:
        task = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert response.status == 200
        await response.text()

        status_response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}",
            headers=USER_A_HEADERS,
        )
        payload = await status_response.json()
        assert payload["status"] == "failed"
        assert payload["archive_status"] == "ready"
        assert "dXNlcjpzZWNyZXQ" not in json.dumps(payload)

        assert_diagnostic_archive(
            store,
            task["task_id"],
            expected_status="failed",
            forbidden_text="dXNlcjpzZWNyZXQ",
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancelled_outcome_writes_scrubbed_diagnostic_archive(
    tmp_path,
):
    client, store = await make_client(tmp_path, stream_events=cancelled_secret_stream)
    try:
        task = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert response.status == 200
        await response.text()

        status_response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}",
            headers=USER_A_HEADERS,
        )
        payload = await status_response.json()
        assert payload["status"] == "cancelled"
        assert payload["archive_status"] == "ready"
        assert "cancelledsecret123456" not in json.dumps(payload)

        assert_diagnostic_archive(
            store,
            task["task_id"],
            expected_status="cancelled",
            forbidden_text="cancelledsecret123456",
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_archive_failed_exposes_warning_without_failing_research(
    tmp_path,
):
    client, store = await make_client(tmp_path, writer_cls=ZipFailWriter)
    try:
        task = await start_task(client)
        response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert response.status == 200
        await response.text()

        status_response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}",
            headers=USER_A_HEADERS,
        )
        payload = await status_response.json()
        assert payload["status"] == "completed"
        assert payload["archive_status"] == "failed"
        assert payload["download_url"] is None
        assert payload["warnings"] == ["archive.zip creation failed: zip unavailable"]

        record = store.get_task(task["task_id"])
        assert record.status == "completed"
        assert record.archive_status == "failed"
        assert record.archive_zip_path is None

        download_response = await client.get(
            f"/limira-runner/tasks/{task['task_id']}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert download_response.status == 409
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_failed_cancelled_and_archive_failed_outcomes(tmp_path):
    failed_client, _store = await make_client(
        tmp_path / "failed", stream_events=failed_stream
    )
    try:
        failed_task = await start_task(failed_client)
        failed_events = await failed_client.get(
            f"/limira-runner/tasks/{failed_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert failed_events.status == 200
        failed_status = await failed_client.get(
            f"/limira-runner/tasks/{failed_task['task_id']}",
            headers=USER_A_HEADERS,
        )
        failed_payload = await failed_status.json()
        assert failed_payload["status"] == "failed"
        assert failed_payload["archive_status"] == "ready"
        assert "dXNlcjpzZWNyZXQ" not in json.dumps(failed_payload)
    finally:
        await failed_client.close()

    cancelled_client, _store = await make_client(
        tmp_path / "cancelled",
        stream_events=cancelled_stream,
    )
    try:
        cancelled_task = await start_task(cancelled_client)
        cancelled_events = await cancelled_client.get(
            f"/limira-runner/tasks/{cancelled_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert cancelled_events.status == 200
        cancelled_status = await cancelled_client.get(
            f"/limira-runner/tasks/{cancelled_task['task_id']}",
            headers=USER_A_HEADERS,
        )
        cancelled_payload = await cancelled_status.json()
        assert cancelled_payload["status"] == "cancelled"
        assert cancelled_payload["archive_status"] == "ready"
    finally:
        await cancelled_client.close()

    archive_failed_client, _store = await make_client(
        tmp_path / "archive-failed",
        writer_cls=ZipFailWriter,
    )
    try:
        archive_failed_task = await start_task(archive_failed_client)
        archive_failed_events = await archive_failed_client.get(
            f"/limira-runner/tasks/{archive_failed_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert archive_failed_events.status == 200
        archive_failed_status = await archive_failed_client.get(
            f"/limira-runner/tasks/{archive_failed_task['task_id']}",
            headers=USER_A_HEADERS,
        )
        archive_failed_payload = await archive_failed_status.json()
        assert archive_failed_payload["status"] == "completed"
        assert archive_failed_payload["archive_status"] == "failed"

        archive_failed_download = await archive_failed_client.get(
            f"/limira-runner/tasks/{archive_failed_task['task_id']}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert archive_failed_download.status == 409
    finally:
        await archive_failed_client.close()
