import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from archive_writer import ResearchArchiveWriter, scrub_secrets, utc_now_iso
from auth_adapter import (
    AuthContext,
    AuthError,
    authenticate_headers,
    reject_body_user_id,
)
from task_store import TaskRecord, TaskStore


MAX_QUERY_CHARS = 20_000

StreamEvents = Callable[..., Awaitable[Any]]
GradioHelpers = tuple[
    Callable[..., Any],
    Callable[[], dict[str, Any]],
    Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    Callable[[dict[str, Any]], str],
]

TASK_STORE_KEY = web.AppKey("task_store", TaskStore)
ARCHIVE_ROOT_KEY = web.AppKey("archive_root", Path)
SERVICE_TOKEN_KEY = web.AppKey("service_token", str | None)
ARCHIVE_WRITER_KEY = web.AppKey("archive_writer_cls", type)
CLOCK_KEY = web.AppKey("clock", object)
STREAM_EVENTS_KEY = web.AppKey("stream_events", object)
INIT_RENDER_STATE_KEY = web.AppKey("init_render_state", object)
UPDATE_STATE_KEY = web.AppKey("update_state_with_event", object)
RENDER_MARKDOWN_KEY = web.AppKey("render_markdown", object)


def create_app(
    *,
    task_store: TaskStore | None = None,
    archive_root: Path | None = None,
    service_token: str | None = None,
    stream_events: Callable[..., Any] | None = None,
    init_render_state: Callable[[], dict[str, Any]] | None = None,
    update_state_with_event: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    | None = None,
    render_markdown: Callable[[dict[str, Any]], str] | None = None,
    archive_writer_cls: type[ResearchArchiveWriter] = ResearchArchiveWriter,
    clock: Callable[[], str] = utc_now_iso,
) -> web.Application:
    app = web.Application()
    app[TASK_STORE_KEY] = task_store or TaskStore(
        Path(__file__).parent / "runner_tasks.sqlite3"
    )
    app[ARCHIVE_ROOT_KEY] = archive_root or (Path(__file__).parent / "archives")
    app[SERVICE_TOKEN_KEY] = service_token
    app[ARCHIVE_WRITER_KEY] = archive_writer_cls
    app[CLOCK_KEY] = clock

    if (
        stream_events is None
        or init_render_state is None
        or update_state_with_event is None
        or render_markdown is None
    ):
        (
            stream_events,
            init_render_state,
            update_state_with_event,
            render_markdown,
        ) = _load_gradio_helpers()

    app[STREAM_EVENTS_KEY] = stream_events
    app[INIT_RENDER_STATE_KEY] = init_render_state
    app[UPDATE_STATE_KEY] = update_state_with_event
    app[RENDER_MARKDOWN_KEY] = render_markdown

    app.router.add_post("/mirothinker/research", start_research)
    app.router.add_get("/mirothinker/tasks/{task_id}", get_task_status)
    app.router.add_get("/mirothinker/tasks/{task_id}/events", stream_task_events)
    app.router.add_get("/mirothinker/tasks/{task_id}/archive.zip", download_archive)
    return app


async def start_research(request: web.Request) -> web.Response:
    auth = _authenticate(request)
    try:
        payload = await request.json()
    except Exception:
        return _error("invalid_json", status=400)
    try:
        reject_body_user_id(payload)
    except AuthError as exc:
        return _error(exc.code, status=exc.status)
    if not isinstance(payload, dict):
        return _error("invalid_json", status=400)

    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error("query_required", status=400)
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        return _error("query_too_long", status=400)

    task_id = str(uuid.uuid4())
    model_summary = _model_summary_from_env()
    store: TaskStore = request.app[TASK_STORE_KEY]
    store.create_task(
        task_id=task_id,
        user_id=auth.user_id,
        query=query,
        created_at=request.app[CLOCK_KEY](),
        model_summary=model_summary,
    )
    return web.json_response(
        {
            "task_id": task_id,
            "status": "queued",
            "stream_url": f"/mirothinker/tasks/{task_id}/events",
            "task_url": f"/mirothinker/tasks/{task_id}",
        },
        status=202,
    )


async def get_task_status(request: web.Request) -> web.Response:
    auth = _authenticate(request)
    record = _get_authorized_task(request, auth)
    if not record:
        return _not_found()
    return web.json_response(_task_response(record))


async def stream_task_events(request: web.Request) -> web.StreamResponse:
    auth = _authenticate(request)
    record = _get_authorized_task(request, auth)
    if not record:
        raise web.HTTPNotFound(text=json.dumps({"error": "not_found"}))

    store: TaskStore = request.app[TASK_STORE_KEY]
    clock: Callable[[], str] = request.app[CLOCK_KEY]
    started_at = clock()
    record = store.update_task(record.task_id, status="running", started_at=started_at)

    writer = request.app[ARCHIVE_WRITER_KEY](request.app[ARCHIVE_ROOT_KEY], clock=clock)
    writer.start(
        task_id=record.task_id,
        query=record.query,
        user_id=record.user_id,
        model_summary=record.model_summary,
        start_time=started_at,
    )
    state = request.app[INIT_RENDER_STATE_KEY]()
    cancelled = False
    status = "completed"
    error = None

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    async def disconnect_check() -> bool:
        return cancelled

    try:
        async for message in request.app[STREAM_EVENTS_KEY](
            record.task_id,
            record.query,
            None,
            disconnect_check,
        ):
            normalized = normalize_stream_event(record.task_id, message, clock())
            if normalized["type"] != "heartbeat":
                writer.record_event(normalized)
                state = request.app[UPDATE_STATE_KEY](state, message)
                if normalized["type"] == "error":
                    status = "failed"
                    error = _event_error(normalized)
            await _write_sse(response, normalized)
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError) as exc:
        cancelled = True
        status = "cancelled"
        error = str(exc) or "client disconnected"
    except Exception as exc:
        status = "failed"
        error = str(exc)
        try:
            await _write_sse(
                response,
                {
                    "task_id": record.task_id,
                    "type": "error",
                    "timestamp": clock(),
                    "payload": {"error": scrub_secrets(error)},
                },
            )
        except Exception:
            pass
    finally:
        report_markdown = None
        if status == "completed":
            report_markdown = request.app[RENDER_MARKDOWN_KEY](state)

        end_time = clock()
        archive_result = writer.complete(
            state=state,
            status=status,
            error=error,
            report_markdown=report_markdown,
            end_time=end_time,
        )
        store.update_task(
            record.task_id,
            status=status,
            archive_status=archive_result.archive_status,
            archive_dir=str(archive_result.archive_dir),
            archive_zip_path=str(archive_result.archive_zip_path)
            if archive_result.archive_zip_path
            else None,
            completed_at=end_time,
            error=scrub_secrets(error),
            warnings=archive_result.warnings,
        )
        try:
            await response.write_eof()
        except Exception:
            pass

    return response


async def download_archive(request: web.Request) -> web.StreamResponse:
    auth = _authenticate(request)
    record = _get_authorized_task(request, auth)
    if not record:
        raise web.HTTPNotFound(text=json.dumps({"error": "not_found"}))
    if record.archive_status != "ready" or not record.archive_zip_path:
        return _error("archive_not_ready", status=409)

    zip_path = Path(record.archive_zip_path)
    if not zip_path.exists():
        return _error("archive_not_ready", status=409)
    return web.FileResponse(
        zip_path,
        headers={"Content-Disposition": 'attachment; filename="archive.zip"'},
    )


def normalize_stream_event(
    task_id: str,
    message: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    event_type = message.get("type") or message.get("event") or "unknown"
    payload = message.get("payload") if "payload" in message else dict(message)
    return scrub_secrets(
        {
            "task_id": task_id,
            "type": event_type,
            "timestamp": timestamp,
            "payload": payload,
        }
    )


def _authenticate(request: web.Request) -> AuthContext:
    try:
        return authenticate_headers(request.headers, request.app[SERVICE_TOKEN_KEY])
    except AuthError as exc:
        _raise_http_error(exc.code, exc.status)


def _get_authorized_task(
    request: web.Request,
    auth: AuthContext,
) -> TaskRecord | None:
    store: TaskStore = request.app[TASK_STORE_KEY]
    record = store.get_task(request.match_info["task_id"])
    if not record:
        return None
    if not auth.is_admin and record.user_id != auth.user_id:
        return None
    return record


def _task_response(record: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "query": record.query,
        "user_id": record.user_id,
        "status": record.status,
        "archive_status": record.archive_status,
        "download_url": f"/mirothinker/tasks/{record.task_id}/archive.zip"
        if record.archive_status == "ready"
        else None,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "error": scrub_secrets(record.error),
        "warnings": record.warnings or [],
    }


async def _write_sse(response: web.StreamResponse, event: dict[str, Any]) -> None:
    payload = json.dumps(event, ensure_ascii=False)
    await response.write(f"data: {payload}\n\n".encode("utf-8"))


def _event_error(event: dict[str, Any]) -> str | None:
    payload = event.get("payload")
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
        if payload.get("error"):
            return str(payload["error"])
    return None


def _error(code: str, status: int) -> web.Response:
    return web.json_response({"error": code}, status=status)


def _not_found() -> web.Response:
    return _error("not_found", status=404)


def _raise_http_error(code: str, status: int) -> None:
    error_classes = {
        400: web.HTTPBadRequest,
        401: web.HTTPUnauthorized,
        403: web.HTTPForbidden,
        404: web.HTTPNotFound,
        409: web.HTTPConflict,
        500: web.HTTPInternalServerError,
    }
    error_cls = error_classes.get(status, web.HTTPInternalServerError)
    raise error_cls(text=json.dumps({"error": code}), content_type="application/json")


def _model_summary_from_env() -> dict[str, Any]:
    return {
        "provider": os.getenv("DEFAULT_LLM_PROVIDER"),
        "model": os.getenv("DEFAULT_MODEL_NAME"),
        "base_url": os.getenv("BASE_URL"),
    }


def _load_gradio_helpers() -> GradioHelpers:
    from main import (  # noqa: PLC0415
        _init_render_state,
        _render_markdown,
        _update_state_with_event,
        stream_events_optimized,
    )

    return (
        stream_events_optimized,
        _init_render_state,
        _update_state_with_event,
        _render_markdown,
    )


if __name__ == "__main__":
    port = int(os.getenv("MIROTHINKER_RUNNER_PORT", "8081"))
    web.run_app(create_app(), host="0.0.0.0", port=port)
