import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from archive_writer import (
    ArchiveResult,
    ResearchArchiveWriter,
    base_url_host,
    scrub_secrets,
    utc_now_iso,
)
from auth_adapter import (
    AuthContext,
    AuthError,
    authenticate_headers,
    reject_body_user_id,
)
from task_store import TaskRecord, TaskStore, create_task_store_from_env


MAX_QUERY_CHARS = 20_000

StreamEvents = Callable[..., Awaitable[Any]]
PipelineHelpers = tuple[
    Callable[..., Any],
    Callable[[], dict[str, Any]],
    Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    Callable[[dict[str, Any]], str],
]

TASK_STORE_KEY = web.AppKey("task_store", object)
ARCHIVE_ROOT_KEY = web.AppKey("archive_root", Path)
SERVICE_TOKEN_KEY = web.AppKey("service_token", str | None)
ARCHIVE_WRITER_KEY = web.AppKey("archive_writer_cls", type)
CLOCK_KEY = web.AppKey("clock", object)
STREAM_EVENTS_KEY = web.AppKey("stream_events", object)
INIT_RENDER_STATE_KEY = web.AppKey("init_render_state", object)
UPDATE_STATE_KEY = web.AppKey("update_state_with_event", object)
RENDER_MARKDOWN_KEY = web.AppKey("render_markdown", object)
TRANSPORT_CLOSING_KEY = web.AppKey("transport_closing", object)
CANCELLED_TASKS_KEY = web.AppKey("cancelled_tasks", set[str])
ACTIVE_TASKS_KEY = web.AppKey("active_tasks", set[str])
FINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_LLM_PROVIDER = "openai"
DEFAULT_MODEL_NAME = "deepseek-v4-pro"
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


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
    transport_closing: Callable[[web.Request], bool] | None = None,
    archive_writer_cls: type[ResearchArchiveWriter] = ResearchArchiveWriter,
    clock: Callable[[], str] = utc_now_iso,
) -> web.Application:
    app = web.Application()
    app[TASK_STORE_KEY] = task_store or create_task_store_from_env()
    app[ARCHIVE_ROOT_KEY] = archive_root or (Path(__file__).parent / "archives")
    app[SERVICE_TOKEN_KEY] = service_token if service_token is not None else os.getenv(
        "RUNNER_SERVICE_TOKEN"
    )
    app[ARCHIVE_WRITER_KEY] = archive_writer_cls
    app[CLOCK_KEY] = clock

    if (
        stream_events is None
        or init_render_state is None
        or update_state_with_event is None
        or render_markdown is None
    ):
        (
            default_stream_events,
            default_init_render_state,
            default_update_state_with_event,
            default_render_markdown,
        ) = _load_pipeline_helpers()
        stream_events = stream_events or default_stream_events
        init_render_state = init_render_state or default_init_render_state
        update_state_with_event = (
            update_state_with_event or default_update_state_with_event
        )
        render_markdown = render_markdown or default_render_markdown

    app[STREAM_EVENTS_KEY] = stream_events
    app[INIT_RENDER_STATE_KEY] = init_render_state
    app[UPDATE_STATE_KEY] = update_state_with_event
    app[RENDER_MARKDOWN_KEY] = render_markdown
    app[TRANSPORT_CLOSING_KEY] = transport_closing or _transport_closing
    app[CANCELLED_TASKS_KEY] = set()
    app[ACTIVE_TASKS_KEY] = set()

    app.router.add_post("/limira-runner/research", start_research)
    app.router.add_get("/limira-runner/tasks/{task_id}", get_task_status)
    app.router.add_post("/limira-runner/tasks/{task_id}/cancel", cancel_task)
    app.router.add_get("/limira-runner/tasks/{task_id}/events", stream_task_events)
    app.router.add_get("/limira-runner/tasks/{task_id}/archive.zip", download_archive)
    app.router.add_get("/health", healthcheck)
    return app


async def healthcheck(request: web.Request) -> web.Response:
    return web.json_response({"status": True})


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
            "stream_url": f"/limira-runner/tasks/{task_id}/events",
            "task_url": f"/limira-runner/tasks/{task_id}",
        },
        status=202,
    )


async def get_task_status(request: web.Request) -> web.Response:
    auth = _authenticate(request)
    record = _get_authorized_task(request, auth)
    if not record:
        return _not_found()
    return web.json_response(_task_response(record))


async def cancel_task(request: web.Request) -> web.Response:
    auth = _authenticate(request)
    record = _get_authorized_task(request, auth)
    if not record:
        return _not_found()
    if record.status in FINAL_TASK_STATUSES:
        return web.json_response(_task_response(record))

    if record.status == "running" and not _task_has_active_worker(
        request.app,
        record.task_id,
    ):
        record = _finalize_running_without_worker_cancellation(request, record)
        return web.json_response({**_task_response(record), "cancel_requested": True})

    _request_task_cancel(request.app, record.task_id)
    if record.status == "queued":
        record = _finalize_queued_cancellation(request, record)
    return web.json_response({**_task_response(record), "cancel_requested": True})


async def stream_task_events(request: web.Request) -> web.StreamResponse:
    auth = _authenticate(request)
    record = _get_authorized_task(request, auth)
    if not record:
        raise web.HTTPNotFound(text=json.dumps({"error": "not_found"}))
    if record.status in FINAL_TASK_STATUSES:
        raise web.HTTPConflict(
            text=json.dumps({"error": "task_already_finished"}),
            content_type="application/json",
        )
    if record.status == "running":
        raise web.HTTPConflict(
            text=json.dumps({"error": "task_already_running"}),
            content_type="application/json",
        )

    store: TaskStore = request.app[TASK_STORE_KEY]
    clock: Callable[[], str] = request.app[CLOCK_KEY]
    started_at = clock()
    claimed_record = store.claim_queued_task(record.task_id, started_at=started_at)
    if not claimed_record:
        current_record = store.get_task(record.task_id)
        if not current_record:
            raise web.HTTPNotFound(text=json.dumps({"error": "not_found"}))
        if current_record.status in FINAL_TASK_STATUSES:
            raise web.HTTPConflict(
                text=json.dumps({"error": "task_already_finished"}),
                content_type="application/json",
            )
        raise web.HTTPConflict(
            text=json.dumps({"error": "task_already_running"}),
            content_type="application/json",
        )
    record = claimed_record
    _register_active_task(request.app, record.task_id)

    writer = request.app[ARCHIVE_WRITER_KEY](request.app[ARCHIVE_ROOT_KEY], clock=clock)
    status = "completed"
    error = None
    state: dict[str, Any] = {}
    writer_started = False
    response_prepared = False
    setup_failed_before_response = False

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )

    try:
        writer.start(
            task_id=record.task_id,
            query=record.query,
            user_id=record.user_id,
            model_summary=record.model_summary,
            start_time=started_at,
        )
        writer_started = True
        state = request.app[INIT_RENDER_STATE_KEY]()
        await response.prepare(request)
        response_prepared = True

        async def disconnect_check() -> bool:
            return _task_cancel_requested(request.app, record.task_id) or request.app[
                TRANSPORT_CLOSING_KEY
            ](request)

        async for message in request.app[STREAM_EVENTS_KEY](
            record.task_id,
            record.query,
            None,
            disconnect_check,
        ):
            normalized = normalize_stream_event(record.task_id, message, clock())
            if normalized["type"] != "heartbeat":
                writer.record_event(normalized)
                state = request.app[UPDATE_STATE_KEY](state, scrub_secrets(message))
                if normalized["type"] == "error":
                    status = "failed"
                    error = _event_error(normalized)
            await _write_sse(response, normalized)
        if await disconnect_check():
            status = "cancelled"
            error = "task cancelled"
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError) as exc:
        _request_task_cancel(request.app, record.task_id)
        status = "cancelled"
        error = str(exc) or "client disconnected"
        setup_failed_before_response = not response_prepared
    except Exception as exc:
        status = "failed"
        error = str(exc)
        setup_failed_before_response = not response_prepared
        if response_prepared:
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
        end_time = clock()
        try:
            if writer_started:
                report_markdown = None
                render_error = None
                try:
                    if status == "completed":
                        report_markdown = request.app[RENDER_MARKDOWN_KEY](state)
                except Exception as exc:
                    render_error = exc

                if render_error:
                    archive_updates = {
                        "archive_status": "failed",
                        "archive_dir": str(writer.archive_dir)
                        if writer.archive_dir
                        else None,
                        "archive_zip_path": None,
                        "warnings": [
                            scrub_secrets(f"report rendering failed: {render_error}")
                        ],
                    }
                else:
                    try:
                        archive_result = writer.complete(
                            state=state,
                            status=status,
                            error=error,
                            report_markdown=report_markdown,
                            end_time=end_time,
                        )
                        archive_updates = _archive_result_updates(archive_result)
                    except Exception as exc:
                        archive_updates = {
                            "archive_status": "failed",
                            "archive_dir": str(writer.archive_dir)
                            if writer.archive_dir
                            else None,
                            "archive_zip_path": None,
                            "warnings": [
                                scrub_secrets(f"archive finalization failed: {exc}")
                            ],
                        }
                store.update_task(
                    record.task_id,
                    status=status,
                    completed_at=end_time,
                    error=scrub_secrets(error),
                    **archive_updates,
                )
            else:
                store.update_task(
                    record.task_id,
                    status=status,
                    archive_status="failed",
                    archive_dir=None,
                    archive_zip_path=None,
                    completed_at=end_time,
                    error=scrub_secrets(error),
                    warnings=["stream setup failed before archive writer started"],
                )
        finally:
            _clear_active_task(request.app, record.task_id)
            _clear_task_cancel(request.app, record.task_id)
            if response_prepared:
                try:
                    await response.write_eof()
                except Exception:
                    pass

    if setup_failed_before_response:
        raise web.HTTPInternalServerError(
            text=json.dumps({"error": "stream_setup_failed"}),
            content_type="application/json",
        )

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


def _request_task_cancel(app: web.Application, task_id: str) -> None:
    app[CANCELLED_TASKS_KEY].add(task_id)


def _clear_task_cancel(app: web.Application, task_id: str) -> None:
    app[CANCELLED_TASKS_KEY].discard(task_id)


def _task_cancel_requested(app: web.Application, task_id: str) -> bool:
    return task_id in app[CANCELLED_TASKS_KEY]


def _register_active_task(app: web.Application, task_id: str) -> None:
    app[ACTIVE_TASKS_KEY].add(task_id)


def _clear_active_task(app: web.Application, task_id: str) -> None:
    app[ACTIVE_TASKS_KEY].discard(task_id)


def _task_has_active_worker(app: web.Application, task_id: str) -> bool:
    return task_id in app[ACTIVE_TASKS_KEY]


def _transport_closing(request: web.Request) -> bool:
    transport = request.transport
    return transport is None or transport.is_closing()


def _finalize_running_without_worker_cancellation(
    request: web.Request,
    record: TaskRecord,
) -> TaskRecord:
    clock: Callable[[], str] = request.app[CLOCK_KEY]
    cancelled_at = clock()
    error = "task cancelled because no active stream worker was registered"
    store: TaskStore = request.app[TASK_STORE_KEY]
    writer = request.app[ARCHIVE_WRITER_KEY](request.app[ARCHIVE_ROOT_KEY], clock=clock)

    try:
        writer.start(
            task_id=record.task_id,
            query=record.query,
            user_id=record.user_id,
            model_summary=record.model_summary,
            start_time=record.started_at or cancelled_at,
        )
        end_time = clock()
        archive_result = writer.complete(
            state={},
            status="cancelled",
            error=error,
            end_time=end_time,
        )
        updated = store.update_task(
            record.task_id,
            status="cancelled",
            completed_at=end_time,
            error=scrub_secrets(error),
            **_archive_result_updates(archive_result),
        )
    except Exception as exc:
        end_time = clock()
        final_error = f"{error}; archive finalization failed: {exc}"
        updated = store.update_task(
            record.task_id,
            status="cancelled",
            archive_status="failed",
            archive_dir=None,
            archive_zip_path=None,
            completed_at=end_time,
            error=scrub_secrets(final_error),
            warnings=["cancelled running task without active stream worker"],
        )

    _clear_active_task(request.app, record.task_id)
    _clear_task_cancel(request.app, record.task_id)
    return updated


def _finalize_queued_cancellation(
    request: web.Request,
    record: TaskRecord,
) -> TaskRecord:
    clock: Callable[[], str] = request.app[CLOCK_KEY]
    cancelled_at = clock()
    error = "task cancelled before stream started"
    store: TaskStore = request.app[TASK_STORE_KEY]
    claimed_record = store.cancel_queued_task(
        record.task_id,
        started_at=cancelled_at,
        completed_at=cancelled_at,
        error=scrub_secrets(error),
    )
    if not claimed_record:
        current_record = store.get_task(record.task_id)
        if current_record and current_record.status in FINAL_TASK_STATUSES:
            _clear_task_cancel(request.app, record.task_id)
        return current_record or record

    writer = request.app[ARCHIVE_WRITER_KEY](request.app[ARCHIVE_ROOT_KEY], clock=clock)
    try:
        writer.start(
            task_id=claimed_record.task_id,
            query=claimed_record.query,
            user_id=claimed_record.user_id,
            model_summary=claimed_record.model_summary,
            start_time=cancelled_at,
        )
        end_time = clock()
        archive_result = writer.complete(
            state={},
            status="cancelled",
            error=error,
            end_time=end_time,
        )
        updated = store.update_task(
            claimed_record.task_id,
            completed_at=end_time,
            **_archive_result_updates(archive_result),
        )
    except Exception as exc:
        end_time = clock()
        updated = store.update_task(
            claimed_record.task_id,
            archive_status="failed",
            archive_dir=str(writer.archive_dir) if writer.archive_dir else None,
            archive_zip_path=None,
            completed_at=end_time,
            warnings=[
                scrub_secrets(f"queued cancellation archive finalization failed: {exc}")
            ],
        )
    finally:
        _clear_task_cancel(request.app, claimed_record.task_id)
    return updated


def _archive_result_updates(archive_result: ArchiveResult) -> dict[str, Any]:
    return {
        "archive_status": archive_result.archive_status,
        "archive_dir": str(archive_result.archive_dir),
        "archive_zip_path": str(archive_result.archive_zip_path)
        if archive_result.archive_zip_path
        else None,
        "warnings": archive_result.warnings,
    }


def _task_response(record: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "query": record.query,
        "user_id": record.user_id,
        "status": record.status,
        "archive_status": record.archive_status,
        "download_url": f"/limira-runner/tasks/{record.task_id}/archive.zip"
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
        "provider": _env_or_default("DEFAULT_LLM_PROVIDER", DEFAULT_LLM_PROVIDER),
        "model": _env_or_default("DEFAULT_MODEL_NAME", DEFAULT_MODEL_NAME),
        "base_url_host": base_url_host(
            _env_or_default("BASE_URL", DEFAULT_LLM_BASE_URL)
        ),
    }


def _load_pipeline_helpers() -> PipelineHelpers:
    from pipeline_helpers import (  # noqa: PLC0415
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
    port = int(os.getenv("LIMIRA_RUNNER_INTERNAL_PORT", "8081"))
    web.run_app(create_app(), host="0.0.0.0", port=port)
