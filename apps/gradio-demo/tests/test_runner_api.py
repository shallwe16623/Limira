import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from archive_writer import ResearchArchiveWriter
from runner_api import CANCELLED_TASKS_KEY, create_app
from task_store import TaskStore


USER_A_HEADERS = {
    "X-MiroThinker-Service-Token": "shared",
    "X-OpenWebUI-User-Id": "user-a",
}
USER_B_HEADERS = {
    "X-MiroThinker-Service-Token": "shared",
    "X-OpenWebUI-User-Id": "user-b",
}
ADMIN_HEADERS = {
    "X-MiroThinker-Service-Token": "shared",
    "X-OpenWebUI-User-Id": "admin-user",
    "X-OpenWebUI-User-Role": "admin",
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


def render_markdown(state):
    return "# Research Summary\n" + "".join(state["chunks"])


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


async def failed_stream(task_id, query, _unused, disconnect_check=None):
    yield {
        "event": "message",
        "data": {"delta": {"content": "partial"}},
    }
    raise RuntimeError("Authorization: Basic dXNlcjpzZWNyZXQ=")


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


class ZipFailWriter(ResearchArchiveWriter):
    def _create_zip(self, zip_path):
        raise RuntimeError("zip unavailable")


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


async def make_client(
    tmp_path,
    stream_events=completed_stream,
    writer_cls=ResearchArchiveWriter,
    transport_closing=None,
    task_store=None,
):
    store = task_store or TaskStore(tmp_path / "tasks.sqlite3")
    app = create_app(
        task_store=store,
        archive_root=tmp_path / "archives",
        service_token="shared",
        stream_events=stream_events,
        init_render_state=init_state,
        update_state_with_event=update_state,
        render_markdown=render_markdown,
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


async def start_task(client, headers=USER_A_HEADERS, query="test query"):
    response = await client.post(
        "/mirothinker/research",
        headers=headers,
        json={"query": query, "client_options": {"stream": True}},
    )
    assert response.status == 202
    return await response.json()


async def complete_task(client, headers=USER_A_HEADERS):
    start_payload = await start_task(client, headers=headers)
    task_id = start_payload["task_id"]
    events_response = await client.get(
        f"/mirothinker/tasks/{task_id}/events",
        headers=headers,
    )
    assert events_response.status == 200
    await events_response.text()
    return task_id


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
    assert f"MiroThinker Research {expected_status.title()}" in report
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
            f"/mirothinker/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        sse_events = parse_sse(await events_response.text())

        assert [event["type"] for event in sse_events] == ["heartbeat", "message"]
        assert sse_events[0]["task_id"] == task_id
        assert sse_events[1]["payload"]["event"] == "message"

        status_response = await client.get(
            f"/mirothinker/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        assert status_payload["status"] == "completed"
        assert status_payload["archive_status"] == "ready"
        assert (
            status_payload["download_url"]
            == f"/mirothinker/tasks/{task_id}/archive.zip"
        )

        record = store.get_task(task_id)
        assert record.archive_dir is not None
        archive_dir = Path(record.archive_dir)
        trace = json.loads((archive_dir / "trace.json").read_text(encoding="utf-8"))
        assert [event["type"] for event in trace["events"]] == ["message"]
        assert "ignoredsecret" not in json.dumps(trace)

        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "final from state" in report

        download_response = await client.get(
            f"/mirothinker/tasks/{task_id}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert download_response.status == 200
        with zipfile.ZipFile(io.BytesIO(await download_response.read())) as archive:
            assert sorted(archive.namelist()) == ARCHIVE_MEMBERS
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_rejects_foreign_user_and_not_ready_download(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]

        foreign_status = await client.get(
            f"/mirothinker/tasks/{task_id}",
            headers=USER_B_HEADERS,
        )
        assert foreign_status.status == 404

        foreign_events = await client.get(
            f"/mirothinker/tasks/{task_id}/events",
            headers=USER_B_HEADERS,
        )
        assert foreign_events.status == 404

        not_ready_download = await client.get(
            f"/mirothinker/tasks/{task_id}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert not_ready_download.status == 409
        assert (await not_ready_download.json())["error"] == "archive_not_ready"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_allows_admin_but_blocks_foreign_download(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_response = await client.get(
            f"/mirothinker/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert events_response.status == 200
        await events_response.text()

        foreign_download = await client.get(
            f"/mirothinker/tasks/{task_id}/archive.zip",
            headers=USER_B_HEADERS,
        )
        assert foreign_download.status == 404

        admin_status = await client.get(
            f"/mirothinker/tasks/{task_id}",
            headers=ADMIN_HEADERS,
        )
        assert admin_status.status == 200

        admin_download = await client.get(
            f"/mirothinker/tasks/{task_id}/archive.zip",
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
            f"/mirothinker/tasks/{start_payload['task_id']}",
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
            f"/mirothinker/tasks/{start_payload['task_id']}/events",
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
            f"/mirothinker/tasks/{task_id}/archive.zip",
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
            f"/mirothinker/tasks/{start_payload['task_id']}/cancel",
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
            f"/mirothinker/tasks/{start_payload['task_id']}",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["user_id"] == "user-a"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_events(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.get(
            f"/mirothinker/tasks/{start_payload['task_id']}/events",
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
            f"/mirothinker/tasks/{task_id}/archive.zip",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
            assert sorted(archive.namelist()) == ARCHIVE_MEMBERS
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_admin_allowed_for_cancel(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        response = await client.post(
            f"/mirothinker/tasks/{start_payload['task_id']}/cancel",
            headers=ADMIN_HEADERS,
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["status"] == "cancelled"
        assert payload["archive_status"] == "ready"
        record = store.get_task(start_payload["task_id"])
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
                f"/mirothinker/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        cancel_response = await client.post(
            f"/mirothinker/tasks/{task_id}/cancel",
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
            f"/mirothinker/tasks/{task_id}",
            headers=USER_A_HEADERS,
        )
        status_payload = await status_response.json()
        assert status_payload["status"] == "cancelled"
        assert status_payload["archive_status"] == "ready"

        record = store.get_task(task_id)
        archive_dir = Path(record.archive_dir)
        metadata = json.loads((archive_dir / "metadata.json").read_text())
        assert metadata["status"] == "cancelled"
        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "MiroThinker Research Cancelled" in report
        assert_zip_members(record.archive_zip_path)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_cancel_endpoint_finalizes_queued_task(tmp_path):
    client, store = await make_client(tmp_path)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]

        cancel_response = await client.post(
            f"/mirothinker/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()
        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "cancelled"
        assert cancel_payload["archive_status"] == "ready"
        assert task_id not in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        archive_dir = Path(record.archive_dir)
        metadata = json.loads((archive_dir / "metadata.json").read_text())
        assert metadata["status"] == "cancelled"
        assert metadata["error"] == "task cancelled before stream started"
        report = (archive_dir / "report.md").read_text(encoding="utf-8")
        assert "MiroThinker Research Cancelled" in report
        assert_zip_members(record.archive_zip_path)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_queued_cancel_keeps_signal_when_stream_claim_wins(tmp_path):
    store = StreamClaimRaceStore(tmp_path / "tasks.sqlite3")
    client, _store = await make_client(tmp_path, task_store=store)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]

        cancel_response = await client.post(
            f"/mirothinker/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        cancel_payload = await cancel_response.json()

        assert store.claimed_during_cancel is True
        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["status"] == "running"
        assert cancel_payload["archive_status"] == "pending"
        assert task_id in client.server.app[CANCELLED_TASKS_KEY]

        record = store.get_task(task_id)
        assert record.status == "running"
        assert record.archive_status == "pending"
        assert record.archive_dir is None
        assert record.started_at == "2026-06-06T12:59:59+00:00"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_rejects_duplicate_running_event_stream(tmp_path):
    probe = CancellationProbe()
    client, _store = await make_client(tmp_path, stream_events=probe.stream)
    try:
        start_payload = await start_task(client)
        task_id = start_payload["task_id"]
        events_task = asyncio.create_task(
            client.get(
                f"/mirothinker/tasks/{task_id}/events",
                headers=USER_A_HEADERS,
            )
        )
        await asyncio.wait_for(probe.started.wait(), timeout=1)

        duplicate_response = await client.get(
            f"/mirothinker/tasks/{task_id}/events",
            headers=USER_A_HEADERS,
        )
        assert duplicate_response.status == 409
        assert await duplicate_response.json() == {"error": "task_already_running"}
        assert probe.stream_count == 1

        cancel_response = await client.post(
            f"/mirothinker/tasks/{task_id}/cancel",
            headers=USER_A_HEADERS,
        )
        assert cancel_response.status == 200
        events_response = await asyncio.wait_for(events_task, timeout=1)
        assert events_response.status == 200
        await asyncio.wait_for(events_response.text(), timeout=1)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_transport_close_stops_active_stream(tmp_path):
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
                f"/mirothinker/tasks/{task_id}/events",
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
        await asyncio.wait_for(probe.stopped.wait(), timeout=1)
        await asyncio.wait_for(events_response.text(), timeout=1)

        status_payload = None
        for _ in range(20):
            status_response = await client.get(
                f"/mirothinker/tasks/{task_id}",
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
        foreign_task = await start_task(client)
        foreign_cancel = await client.post(
            f"/mirothinker/tasks/{foreign_task['task_id']}/cancel",
            headers=USER_B_HEADERS,
        )
        assert foreign_cancel.status == 404

        admin_task = await start_task(client)
        admin_cancel = await client.post(
            f"/mirothinker/tasks/{admin_task['task_id']}/cancel",
            headers=ADMIN_HEADERS,
        )
        assert admin_cancel.status == 200
        admin_payload = await admin_cancel.json()
        assert admin_payload["status"] == "cancelled"
        assert admin_payload["archive_status"] == "ready"
        assert admin_payload["cancel_requested"] is True

        record = store.get_task(admin_task["task_id"])
        assert record.status == "cancelled"
        assert record.archive_zip_path is not None

        completed_events = await client.get(
            f"/mirothinker/tasks/{admin_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert completed_events.status == 409
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runner_api_validation_rejects_untrusted_inputs(tmp_path):
    client, _store = await make_client(tmp_path)
    try:
        missing_auth = await client.post(
            "/mirothinker/research",
            json={"query": "x"},
        )
        assert missing_auth.status == 401

        body_user = await client.post(
            "/mirothinker/research",
            headers=USER_A_HEADERS,
            json={"query": "x", "user_id": "attacker"},
        )
        assert body_user.status == 400

        empty_query = await client.post(
            "/mirothinker/research",
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
            f"/mirothinker/tasks/{task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert response.status == 200
        await response.text()

        status_response = await client.get(
            f"/mirothinker/tasks/{task['task_id']}",
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
            f"/mirothinker/tasks/{task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert response.status == 200
        await response.text()

        status_response = await client.get(
            f"/mirothinker/tasks/{task['task_id']}",
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
            f"/mirothinker/tasks/{task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert response.status == 200
        await response.text()

        status_response = await client.get(
            f"/mirothinker/tasks/{task['task_id']}",
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
            f"/mirothinker/tasks/{task['task_id']}/archive.zip",
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
            f"/mirothinker/tasks/{failed_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert failed_events.status == 200
        failed_status = await failed_client.get(
            f"/mirothinker/tasks/{failed_task['task_id']}",
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
            f"/mirothinker/tasks/{cancelled_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert cancelled_events.status == 200
        cancelled_status = await cancelled_client.get(
            f"/mirothinker/tasks/{cancelled_task['task_id']}",
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
            f"/mirothinker/tasks/{archive_failed_task['task_id']}/events",
            headers=USER_A_HEADERS,
        )
        assert archive_failed_events.status == 200
        archive_failed_status = await archive_failed_client.get(
            f"/mirothinker/tasks/{archive_failed_task['task_id']}",
            headers=USER_A_HEADERS,
        )
        archive_failed_payload = await archive_failed_status.json()
        assert archive_failed_payload["status"] == "completed"
        assert archive_failed_payload["archive_status"] == "failed"

        archive_failed_download = await archive_failed_client.get(
            f"/mirothinker/tasks/{archive_failed_task['task_id']}/archive.zip",
            headers=USER_A_HEADERS,
        )
        assert archive_failed_download.status == 409
    finally:
        await archive_failed_client.close()
