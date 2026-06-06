import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from mirothinker_deep_research import (
    SERVICE_TOKEN_HEADER,
    USER_ID_HEADER,
    Pipe,
    RunnerApiError,
)


USER_A = {"id": "user-a", "name": "User A"}
USER_B = {"id": "user-b", "name": "User B"}


@dataclass
class FakeRunner:
    final_status: str = "completed"
    archive_status: str = "ready"
    events_status_code: int = 200
    stream_events: list[dict[str, Any]] = field(default_factory=list)
    user_a_task_id: str = "task-user-a"
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client_factory(self):
        return httpx.AsyncClient(transport=self.transport())

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        user_id = request.headers.get(USER_ID_HEADER)
        service_token = request.headers.get(SERVICE_TOKEN_HEADER)
        if service_token != "shared-token":
            return json_response(401, {"error": "invalid_service_token"})

        path = request.url.path
        if request.method == "POST" and path == "/mirothinker/research":
            body = json.loads(request.content.decode())
            if "user_id" in body:
                return json_response(400, {"error": "body_user_id_rejected"})
            return json_response(
                202,
                {
                    "task_id": self.user_a_task_id,
                    "status": "queued",
                    "stream_url": f"/mirothinker/tasks/{self.user_a_task_id}/events",
                    "task_url": f"/mirothinker/tasks/{self.user_a_task_id}",
                },
            )

        if path.startswith(f"/mirothinker/tasks/{self.user_a_task_id}") and (
            user_id != "user-a"
        ):
            return json_response(404, {"error": "not_found"})

        if request.method == "GET" and path.endswith("/events"):
            if self.events_status_code != 200:
                return json_response(
                    self.events_status_code, {"error": "terminal_task"}
                )
            content = "".join(
                f"data: {json.dumps(event)}\n\n" for event in self.events()
            )
            return httpx.Response(200, content=content.encode("utf-8"))

        if request.method == "GET" and path.endswith("/archive.zip"):
            return httpx.Response(
                200,
                content=b"fake archive zip",
                headers={"Content-Type": "application/zip"},
            )

        if (
            request.method == "GET"
            and path == f"/mirothinker/tasks/{self.user_a_task_id}"
        ):
            return json_response(
                200,
                {
                    "task_id": self.user_a_task_id,
                    "status": self.final_status,
                    "archive_status": self.archive_status,
                    "download_url": f"/mirothinker/tasks/{self.user_a_task_id}/archive.zip"
                    if self.archive_status == "ready"
                    else None,
                },
            )

        if request.method == "POST" and path.endswith("/cancel"):
            return json_response(
                200,
                {
                    "task_id": self.user_a_task_id,
                    "status": "cancelled",
                    "archive_status": "ready",
                    "download_url": f"/mirothinker/tasks/{self.user_a_task_id}/archive.zip",
                    "cancel_requested": True,
                },
            )

        return json_response(404, {"error": "not_found"})

    def events(self) -> list[dict[str, Any]]:
        if self.stream_events:
            return self.stream_events
        return [
            {
                "task_id": self.user_a_task_id,
                "type": "message",
                "timestamp": "2026-06-06T12:00:00+00:00",
                "payload": {
                    "event": "message",
                    "data": {"delta": {"content": "progress update\n"}},
                },
            }
        ]


def json_response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def configured_pipe(fake_runner: FakeRunner) -> Pipe:
    pipe = Pipe(client_factory=fake_runner.client_factory)
    pipe.valves.RUNNER_BASE_URL = "http://runner.local"
    pipe.valves.RUNNER_SERVICE_TOKEN = "shared-token"
    pipe.valves.DOWNLOAD_BASE_URL = "https://open-webui.example/runner"
    return pipe


async def collect_pipe(pipe: Pipe, body: dict[str, Any], user=USER_A):
    events = []

    async def emit(event):
        events.append(event)

    chunks = []
    async for chunk in pipe.pipe(
        body,
        __user__=user,
        __metadata__={"user_prompt": "research from metadata"},
        __event_emitter__=emit,
    ):
        chunks.append(chunk)
    return "".join(chunks), events


@pytest.mark.asyncio
async def test_user_a_start_progress_final_and_download_ready():
    fake_runner = FakeRunner()
    pipe = configured_pipe(fake_runner)

    output, events = await collect_pipe(
        pipe,
        {
            "user_id": "attacker",
            "messages": [{"role": "user", "content": "browser body query"}],
        },
    )

    assert "progress update" in output
    assert "Research status: `completed`" in output
    assert "Download Trace ZIP: https://open-webui.example/runner/" in output
    assert events[0]["type"] == "status"
    assert events[-1]["data"]["done"] is True

    start_request = fake_runner.requests[0]
    assert start_request.headers[USER_ID_HEADER] == "user-a"
    assert start_request.headers[SERVICE_TOKEN_HEADER] == "shared-token"
    start_body = json.loads(start_request.content.decode())
    assert start_body["query"] == "research from metadata"
    assert "user_id" not in start_body


@pytest.mark.asyncio
async def test_foreign_user_denied_for_status_events_and_archive():
    fake_runner = FakeRunner()
    pipe = configured_pipe(fake_runner)
    client = pipe._client()

    with pytest.raises(RunnerApiError) as exc_info:
        await client.get_task_status(task_id="task-user-a", user_id="user-b")

    assert exc_info.value.status_code == 404
    status_request = fake_runner.requests[-1]
    assert status_request.headers[USER_ID_HEADER] == "user-b"

    with pytest.raises(RunnerApiError) as events_exc:
        async for _event in client.stream_events(
            task_id="task-user-a", user_id="user-b"
        ):
            pass
    assert events_exc.value.status_code == 404

    with pytest.raises(RunnerApiError) as archive_exc:
        await client.download_archive(task_id="task-user-a", user_id="user-b")
    assert archive_exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_uses_trusted_cancel_endpoint():
    fake_runner = FakeRunner()
    pipe = configured_pipe(fake_runner)

    payload = await pipe.cancel_research(task_id="task-user-a", __user__=USER_A)

    assert payload["cancel_requested"] is True
    cancel_request = fake_runner.requests[-1]
    assert cancel_request.method == "POST"
    assert cancel_request.url.path == "/mirothinker/tasks/task-user-a/cancel"
    assert cancel_request.headers[USER_ID_HEADER] == "user-a"
    assert cancel_request.headers[SERVICE_TOKEN_HEADER] == "shared-token"


@pytest.mark.asyncio
async def test_pending_archive_disables_download():
    fake_runner = FakeRunner(archive_status="pending")
    pipe = configured_pipe(fake_runner)

    output, _events = await collect_pipe(pipe, {"query": "research"})

    assert "Archive download disabled: `pending`." in output
    assert "Download Trace ZIP" not in output
    assert "/archive.zip" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "download_base_url",
    ["", "http://runner.local/", "http://token@runner.local/"],
)
async def test_ready_archive_requires_explicit_download_proxy(download_base_url):
    fake_runner = FakeRunner()
    pipe = configured_pipe(fake_runner)
    pipe.valves.DOWNLOAD_BASE_URL = download_base_url

    output, _events = await collect_pipe(pipe, {"query": "research"})

    assert "Archive download disabled: `proxy_required`." in output
    assert "Download Trace ZIP" not in output
    assert "/archive.zip" not in output


@pytest.mark.asyncio
async def test_show_text_tool_call_events_are_rendered():
    fake_runner = FakeRunner(
        stream_events=[
            {
                "task_id": "task-user-a",
                "type": "tool_call",
                "timestamp": "2026-06-06T12:00:00+00:00",
                "payload": {
                    "event": "tool_call",
                    "data": {
                        "tool_name": "show_text",
                        "tool_call_id": "show-text-1",
                        "delta_input": {"text": "show_text delta report\n"},
                    },
                },
            },
            {
                "task_id": "task-user-a",
                "type": "tool_call",
                "timestamp": "2026-06-06T12:00:01+00:00",
                "payload": {
                    "event": "tool_call",
                    "data": {
                        "tool_name": "show_text",
                        "tool_call_id": "show-text-1",
                        "tool_input": json.dumps({"text": "show_text final report\n"}),
                    },
                },
            },
        ]
    )
    pipe = configured_pipe(fake_runner)

    output, _events = await collect_pipe(pipe, {"query": "research"})

    assert "show_text delta report" in output
    assert "show_text final report" in output
    assert "Final report preview" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_failed_and_cancelled_ready_archive_use_diagnostic_download_text(status):
    fake_runner = FakeRunner(final_status=status, archive_status="ready")
    pipe = configured_pipe(fake_runner)

    output, _events = await collect_pipe(pipe, {"query": "research"})

    assert f"Research status: `{status}`" in output
    assert "Download Diagnostic ZIP: https://open-webui.example/runner/" in output


@pytest.mark.asyncio
async def test_terminal_events_conflict_polls_final_cancelled_status():
    fake_runner = FakeRunner(
        final_status="cancelled",
        archive_status="ready",
        events_status_code=409,
    )
    pipe = configured_pipe(fake_runner)

    output, _events = await collect_pipe(pipe, {"query": "research"})

    assert "Research status: `cancelled`" in output
    assert "Download Diagnostic ZIP: https://open-webui.example/runner/" in output
