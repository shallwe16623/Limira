"""
title: MiroThinker Deep Research
author: MiroThinker
version: 0.1.0
required_open_webui_version: 0.6.0
requirements: httpx,pydantic
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, Field


SERVICE_TOKEN_HEADER = "X-MiroThinker-Service-Token"
USER_ID_HEADER = "X-OpenWebUI-User-Id"
JSON_HEADERS = {"Content-Type": "application/json"}

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]
HttpClientFactory = Callable[[], httpx.AsyncClient]


class RunnerApiError(RuntimeError):
    def __init__(self, status_code: int, error: str):
        super().__init__(f"Runner API returned {status_code}: {error}")
        self.status_code = status_code
        self.error = error


@dataclass(frozen=True)
class RunnerConfig:
    base_url: str
    service_token: str
    request_timeout_seconds: float
    download_base_url: str


class MiroThinkerRunnerClient:
    def __init__(
        self,
        config: RunnerConfig,
        client_factory: HttpClientFactory | None = None,
    ):
        self.config = config
        self._client_factory = client_factory or self._default_client_factory

    async def start_research(self, *, query: str, user_id: str) -> dict[str, Any]:
        payload = {"query": query, "client_options": {"stream": True}}
        return await self._request_json(
            "POST",
            "/mirothinker/research",
            user_id=user_id,
            json_body=payload,
            expected_status={200, 202},
        )

    async def get_task_status(self, *, task_id: str, user_id: str) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/mirothinker/tasks/{task_id}",
            user_id=user_id,
            expected_status={200},
        )

    async def cancel_task(self, *, task_id: str, user_id: str) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            f"/mirothinker/tasks/{task_id}/cancel",
            user_id=user_id,
            expected_status={200},
        )

    async def stream_events(
        self,
        *,
        task_id: str,
        user_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._client_factory() as client:
            async with client.stream(
                "GET",
                self._url(f"/mirothinker/tasks/{task_id}/events"),
                headers=self._headers(user_id),
                timeout=self.config.request_timeout_seconds,
            ) as response:
                await self._ensure_success(response, {200})
                async for line in response.aiter_lines():
                    event = parse_sse_line(line)
                    if event is not None:
                        yield event

    async def download_archive(self, *, task_id: str, user_id: str) -> bytes:
        async with self._client_factory() as client:
            response = await client.request(
                "GET",
                self._url(f"/mirothinker/tasks/{task_id}/archive.zip"),
                headers=self._headers(user_id),
                timeout=self.config.request_timeout_seconds,
            )
        await self._ensure_success(response, {200})
        return response.content

    def download_url(self, task_status: dict[str, Any]) -> str | None:
        if task_status.get("archive_status") != "ready":
            return None
        raw_url = task_status.get("download_url")
        if not isinstance(raw_url, str) or not raw_url:
            return None
        base_url = self.config.download_base_url or self.config.base_url
        return absolute_url(base_url, raw_url)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        user_id: str,
        expected_status: set[int],
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._client_factory() as client:
            response = await client.request(
                method,
                self._url(path),
                headers={**JSON_HEADERS, **self._headers(user_id)},
                json=json_body,
                timeout=self.config.request_timeout_seconds,
            )
        await self._ensure_success(response, expected_status)
        if not response.content:
            return {}
        return response.json()

    async def _ensure_success(
        self,
        response: httpx.Response,
        expected_status: set[int],
    ) -> None:
        if response.status_code in expected_status:
            return
        error = "runner_api_error"
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                error = payload["error"]
        except Exception:
            if response.text:
                error = response.text[:200]
        raise RunnerApiError(response.status_code, error)

    def _headers(self, user_id: str) -> dict[str, str]:
        return {
            SERVICE_TOKEN_HEADER: self.config.service_token,
            USER_ID_HEADER: user_id,
        }

    def _url(self, path: str) -> str:
        return absolute_url(self.config.base_url, path)

    def _default_client_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient()


class Pipe:
    class Valves(BaseModel):
        RUNNER_BASE_URL: str = Field(
            default="http://localhost:8081",
            description="Internal base URL for the MiroThinker Runner API.",
        )
        RUNNER_SERVICE_TOKEN: str = Field(
            default="",
            description="Shared service token sent only from Open WebUI server side.",
            json_schema_extra={"input": {"type": "password"}},
        )
        DOWNLOAD_BASE_URL: str = Field(
            default="",
            description=(
                "Optional browser-reachable base URL for archive download links. "
                "Defaults to RUNNER_BASE_URL."
            ),
        )
        REQUEST_TIMEOUT_SECONDS: float = Field(
            default=60.0,
            description="HTTP timeout for Runner API requests.",
        )

    def __init__(self, client_factory: HttpClientFactory | None = None):
        self.valves = self.Valves()
        self._client_factory = client_factory

    def pipes(self) -> list[dict[str, str]]:
        return [
            {"id": "mirothinker-deep-research", "name": "MiroThinker Deep Research"}
        ]

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __metadata__: dict[str, Any] | None = None,
        __event_emitter__: EventEmitter | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self.run_research(
            body=body,
            user_context=__user__,
            metadata=__metadata__,
            event_emitter=__event_emitter__,
        ):
            yield chunk

    async def run_research(
        self,
        *,
        body: dict[str, Any],
        user_context: dict[str, Any] | None,
        metadata: dict[str, Any] | None = None,
        event_emitter: EventEmitter | None = None,
    ) -> AsyncIterator[str]:
        user_id = open_webui_user_id(user_context)
        query = extract_query(body, metadata)
        client = self._client()

        await emit_status(event_emitter, "Starting MiroThinker research", done=False)
        task = await client.start_research(query=query, user_id=user_id)
        task_id = required_str(task, "task_id")

        yield "## MiroThinker Deep Research\n\n"
        yield f"Task `{task_id}` started.\n\n"

        report_chunks: list[str] = []
        async for event in client.stream_events(task_id=task_id, user_id=user_id):
            description = event_description(event)
            await emit_status(event_emitter, description, done=False)
            visible_text = visible_event_text(event)
            if visible_text:
                report_chunks.append(visible_text)
                yield visible_text

        task_status = await client.get_task_status(task_id=task_id, user_id=user_id)
        final_text = render_final_response(
            client=client,
            task_status=task_status,
            report_text="".join(report_chunks).strip(),
        )
        await emit_status(
            event_emitter,
            f"MiroThinker research {task_status.get('status', 'finished')}",
            done=True,
        )
        yield final_text

    async def cancel_research(
        self,
        *,
        task_id: str,
        __user__: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_id = open_webui_user_id(__user__)
        return await self._client().cancel_task(task_id=task_id, user_id=user_id)

    def _client(self) -> MiroThinkerRunnerClient:
        config = RunnerConfig(
            base_url=self.valves.RUNNER_BASE_URL,
            service_token=self.valves.RUNNER_SERVICE_TOKEN,
            request_timeout_seconds=self.valves.REQUEST_TIMEOUT_SECONDS,
            download_base_url=self.valves.DOWNLOAD_BASE_URL,
        )
        return MiroThinkerRunnerClient(config, client_factory=self._client_factory)


def open_webui_user_id(user_context: dict[str, Any] | None) -> str:
    if not isinstance(user_context, dict):
        raise ValueError("Open WebUI user context is required")
    user_id = str(user_context.get("id") or "").strip()
    if not user_id:
        raise ValueError("Open WebUI user context does not include an id")
    return user_id


def extract_query(
    body: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> str:
    if metadata and isinstance(metadata.get("user_prompt"), str):
        query = metadata["user_prompt"].strip()
        if query:
            return query

    messages = body.get("messages") if isinstance(body, dict) else None
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

    query = body.get("query") if isinstance(body, dict) else None
    if isinstance(query, str) and query.strip():
        return query.strip()
    raise ValueError("A research query is required")


def required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RunnerApiError(502, f"runner response missing {key}")
    return value


async def emit_status(
    event_emitter: EventEmitter | None,
    description: str,
    *,
    done: bool,
) -> None:
    if event_emitter is None:
        return
    await event_emitter(
        {
            "type": "status",
            "data": {"description": description, "done": done, "hidden": False},
        }
    )


def parse_sse_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line.removeprefix("data:").strip()
    if not data or data == "[DONE]":
        return None
    payload = json.loads(data)
    return payload if isinstance(payload, dict) else None


def event_description(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "event")
    if event_type == "heartbeat":
        return "MiroThinker is still running"
    return f"MiroThinker event: {event_type}"


def visible_event_text(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ""
    source_event = payload.get("event") or event.get("type")
    data = payload.get("data")
    if source_event == "message" and isinstance(data, dict):
        delta = data.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            return delta["content"]
    if source_event == "error":
        return f"\n\nResearch error: {safe_error_text(data)}\n"
    return ""


def safe_error_text(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("error"), str):
        return value["error"]
    if isinstance(value, str):
        return value
    return "Runner reported an error"


def render_final_response(
    *,
    client: MiroThinkerRunnerClient,
    task_status: dict[str, Any],
    report_text: str,
) -> str:
    status = str(task_status.get("status") or "unknown")
    archive_status = str(task_status.get("archive_status") or "unknown")
    sections = ["\n\n---\n", f"Research status: `{status}`\n"]
    if report_text:
        sections.append("\nFinal report preview:\n\n")
        sections.append(report_text)
        sections.append("\n")

    download_url = client.download_url(task_status)
    if download_url:
        label = (
            "Download Diagnostic ZIP"
            if status in {"failed", "cancelled"}
            else "Download Trace ZIP"
        )
        sections.append(f"\n{label}: {download_url}\n")
    else:
        sections.append(f"\nArchive download disabled: `{archive_status}`.\n")
    return "".join(sections)


def absolute_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))
