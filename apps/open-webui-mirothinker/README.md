# Open WebUI MiroThinker Integration

This directory contains a self-contained Open WebUI Function artifact for calling the MiroThinker Runner API after the Runner API and archive contracts are stable.

Open WebUI source is not present in this repository, so this integration is packaged as an installable Pipe Function rather than an in-tree Open WebUI page edit.

## Artifact

- `mirothinker_deep_research.py`

Install the file in Open WebUI as a Function. Open WebUI detects a top-level `Pipe` class, loads admin-configured Valves, and passes reserved arguments such as `__user__`, `__metadata__`, and `__event_emitter__` to the function.

Official Open WebUI references:

- https://docs.openwebui.com/features/extensibility/plugin/functions/
- https://docs.openwebui.com/features/extensibility/plugin/functions/pipe/
- https://docs.openwebui.com/features/extensibility/plugin/development/reserved-args/
- https://docs.openwebui.com/features/extensibility/plugin/development/events/
- https://docs.openwebui.com/features/extensibility/plugin/development/valves/

Pipelines are not used.

## Configuration

Configure these Function Valves in Open WebUI admin settings:

- `RUNNER_BASE_URL`: internal URL for the MiroThinker Runner API, for example `http://mirothinker-runner:8081`.
- `RUNNER_SERVICE_TOKEN`: shared service token matching `MIROTHINKER_SERVICE_TOKEN` on the Runner API. This is a masked Valve and must stay server-side.
- `DOWNLOAD_BASE_URL`: browser-reachable trusted proxy base URL for archive links. If blank or set to `RUNNER_BASE_URL`, archive links are disabled because browser clicks do not carry Runner API service headers.
- `REQUEST_TIMEOUT_SECONDS`: Runner API request timeout.

The Pipe obtains the authenticated user from Open WebUI's server-provided `__user__` context and injects:

- `X-MiroThinker-Service-Token`
- `X-OpenWebUI-User-Id`

The browser must never send or override `user_id`; any `user_id` in the chat body is ignored by the integration and is not forwarded to Runner API JSON.

Do not expose `RUNNER_SERVICE_TOKEN` to browser JavaScript. For downloads, put Runner API behind an Open WebUI backend route or reverse proxy that injects the trusted headers server-side and applies the same authenticated Open WebUI user context.

## Behavior

The Pipe:

1. Reads the research query from `__metadata__["user_prompt"]` or the latest user message.
2. Calls `POST /mirothinker/research`.
3. Consumes `GET /mirothinker/tasks/{task_id}/events`.
4. Emits Open WebUI status events for progress.
5. Polls `GET /mirothinker/tasks/{task_id}` after the stream completes.
6. Shows a download line only when `archive_status == "ready"` and `DOWNLOAD_BASE_URL` is an explicit trusted proxy URL.

Download text:

- Completed task with ready archive: `Download Trace ZIP`.
- Failed or cancelled task with ready archive: `Download Diagnostic ZIP`.
- Pending or failed archive, missing proxy, or direct Runner API download base: download remains disabled.

Cancellation uses `POST /mirothinker/tasks/{task_id}/cancel` through the same trusted server-side headers.

## Local Harness Tests

The tests in `tests/` use `httpx.MockTransport` as a fake Runner API. They do not call LLMs, tools, Open WebUI, or localhost sockets.

```bash
cd apps/open-webui-mirothinker
uv run pytest
uv run ruff check mirothinker_deep_research.py tests
uv run ruff format --check mirothinker_deep_research.py tests
```
