import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from archive_writer import ResearchArchiveWriter
from runner_api import create_app
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


class ZipFailWriter(ResearchArchiveWriter):
    def _create_zip(self, zip_path):
        raise RuntimeError("zip unavailable")


async def make_client(
    tmp_path, stream_events=completed_stream, writer_cls=ResearchArchiveWriter
):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    app = create_app(
        task_store=store,
        archive_root=tmp_path / "archives",
        service_token="shared",
        stream_events=stream_events,
        init_render_state=init_state,
        update_state_with_event=update_state,
        render_markdown=render_markdown,
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
            assert sorted(archive.namelist()) == [
                "metadata.json",
                "report.html",
                "report.md",
                "trace.json",
            ]
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
