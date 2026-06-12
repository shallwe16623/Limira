import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
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
MAX_CONTEXT_STRING_CHARS = 120
MAX_DOCUMENT_IDS = 20
MAX_CONTEXT_JSON_CHARS = 20_000
STALE_RUNNING_SECONDS_ENV = "RUNNER_STALE_RUNNING_SECONDS"
DEFAULT_STALE_RUNNING_SECONDS = 60 * 60

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
TASK_WORKERS_KEY = web.AppKey("task_workers", dict[str, asyncio.Task])
PENDING_STARTUP_WORKERS_KEY = web.AppKey("pending_startup_workers", set[str])
TASK_EVENT_LOG_KEY = web.AppKey("task_event_log", dict[str, list[dict[str, Any]]])
RUNNER_WORKER_ID_KEY = web.AppKey("runner_worker_id", str)
TASK_SUBSCRIBERS_KEY = web.AppKey(
    "task_subscribers",
    dict[str, set[asyncio.Queue[dict[str, Any] | None]]],
)
FINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_LLM_PROVIDER = "openai"
DEFAULT_MODEL_NAME = "deepseek-v4-pro"
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
MAX_REPLAY_EVENTS = 1000
DURABLE_EVENT_POLL_INTERVAL_SECONDS = 0.05
GRAPH_CHECKPOINT_PHASES = {
    "scope",
    "plan",
    "research",
    "compress",
    "verify",
    "write",
    "reconcile",
    "complete",
}
RESEARCH_GRAPH_EXECUTORS = {"legacy", "serial", "langgraph"}
log = logging.getLogger(__name__)


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
    app[TASK_WORKERS_KEY] = {}
    app[PENDING_STARTUP_WORKERS_KEY] = set()
    app[TASK_EVENT_LOG_KEY] = {}
    app[RUNNER_WORKER_ID_KEY] = f"runner-{uuid.uuid4()}"
    app[TASK_SUBSCRIBERS_KEY] = {}
    app.on_startup.append(_start_pending_task_workers)
    _reconcile_stale_running_tasks(app)

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
    try:
        task_context = _task_context_from_payload(payload, query=query)
    except ValueError as exc:
        return _error(str(exc), status=400)

    task_id = str(uuid.uuid4())
    model_summary = _model_summary_from_env()
    store: TaskStore = request.app[TASK_STORE_KEY]
    record = store.create_task(
        task_id=task_id,
        user_id=auth.user_id,
        query=query,
        created_at=request.app[CLOCK_KEY](),
        model_summary=model_summary,
        context=task_context,
    )
    _ensure_task_worker(request.app, record.task_id)
    return web.json_response(
        {
            "task_id": task_id,
            "status": record.status,
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
    record = _reconcile_task_if_stale(request.app, record)
    return web.json_response(_task_response(record))


async def cancel_task(request: web.Request) -> web.Response:
    auth = _authenticate(request)
    record = _get_authorized_task(request, auth)
    if not record:
        return _not_found()
    if record.status in FINAL_TASK_STATUSES:
        return web.json_response(_task_response(record))

    if record.status == "running":
        lease_state = _classify_task_lease(request.app, record)
        if lease_state == "external_active":
            return _error("task_owned_by_active_worker", status=409)
        if lease_state in {"expired", "missing"} and not _task_has_active_worker(
            request.app,
            record.task_id,
        ):
            record = _finalize_running_without_worker_cancellation(request, record)
            if record.status == "running":
                return _error("task_owned_by_active_worker", status=409)
            return web.json_response(
                {**_task_response(record), "cancel_requested": True}
            )

    if record.status == "running" and not _task_has_active_worker(
        request.app,
        record.task_id,
    ):
        record = _finalize_running_without_worker_cancellation(request, record)
        if record.status == "running":
            return _error("task_owned_by_active_worker", status=409)
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
    record = _reconcile_task_if_stale(request.app, record)

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    queue = _subscribe_task_events(request.app, record.task_id)
    response_prepared = False
    try:
        await response.prepare(request)
        response_prepared = True
        if record.status == "queued":
            _ensure_task_worker(request.app, record.task_id)
        replay_records = _task_event_records_snapshot(request.app, record.task_id)
        replay_events = [_durable_record_event(record) for record in replay_records]
        for event in replay_events:
            await _write_sse(response, event)
        current = _task_store(request.app).get_task(record.task_id)
        if current and current.status in FINAL_TASK_STATUSES:
            return response
        if current and _classify_task_lease(request.app, current) == "external_active":
            await _tail_external_durable_events(
                request,
                response,
                current.task_id,
                seen_event_cursor=_durable_record_cursor(replay_records[-1])
                if replay_records
                else None,
            )
            return response
        while True:
            event = await queue.get()
            if event is None:
                return response
            await _write_sse(response, event)
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        return response
    finally:
        _unsubscribe_task_events(request.app, record.task_id, queue)
        if response_prepared:
            try:
                await response.write_eof()
            except Exception:
                pass

    return response


async def _tail_external_durable_events(
    request: web.Request,
    response: web.StreamResponse,
    task_id: str,
    *,
    seen_event_cursor: Any,
) -> None:
    app = request.app
    while True:
        await asyncio.sleep(DURABLE_EVENT_POLL_INTERVAL_SECONDS)
        seen_event_cursor = await _write_new_durable_events(
            app,
            response,
            task_id,
            seen_event_cursor,
        )
        record = _task_store(app).get_task(task_id)
        if not record:
            return
        if record.status in FINAL_TASK_STATUSES:
            await _write_new_durable_events(app, response, task_id, seen_event_cursor)
            return
        if _classify_task_lease(app, record) == "external_active":
            continue
        reconciled = _reconcile_task_if_stale(app, record)
        seen_event_cursor = await _write_new_durable_events(
            app,
            response,
            task_id,
            seen_event_cursor,
        )
        if reconciled.status in FINAL_TASK_STATUSES:
            return


async def _write_new_durable_events(
    app: web.Application,
    response: web.StreamResponse,
    task_id: str,
    seen_event_cursor: Any,
) -> Any:
    durable_records = _list_durable_task_event_records(
        app,
        task_id,
        after_cursor=seen_event_cursor,
    )
    if durable_records:
        cached_events = app[TASK_EVENT_LOG_KEY].setdefault(task_id, [])
        cached_events.extend(_durable_record_event(record) for record in durable_records)
        if len(cached_events) > MAX_REPLAY_EVENTS:
            del cached_events[:-MAX_REPLAY_EVENTS]
    for record in durable_records:
        event = _durable_record_event(record)
        await _write_sse(response, event)
        seen_event_cursor = _durable_record_cursor(record)
    return seen_event_cursor


async def _run_task_worker(app: web.Application, task_id: str) -> None:
    store = _task_store(app)
    record = store.get_task(task_id)
    if not record or record.status in FINAL_TASK_STATUSES:
        _notify_task_finished(app, task_id)
        return

    clock: Callable[[], str] = app[CLOCK_KEY]
    started_at = clock()
    worker_id = _task_worker_id(app, record.task_id)
    claimed_record = store.claim_queued_task(
        record.task_id,
        started_at=started_at,
        worker_id=worker_id,
        lease_expires_at=_lease_expires_at_from(started_at),
    )
    if not claimed_record:
        current_record = store.get_task(record.task_id)
        if current_record and current_record.status in FINAL_TASK_STATUSES:
            _notify_task_finished(app, task_id)
        return

    record = claimed_record
    _register_active_task(app, record.task_id)
    writer = app[ARCHIVE_WRITER_KEY](app[ARCHIVE_ROOT_KEY], clock=clock)
    status = "completed"
    error = None
    state: dict[str, Any] = {}
    writer_started = False

    try:
        writer.start(
            task_id=record.task_id,
            query=record.query,
            user_id=record.user_id,
            model_summary=record.model_summary,
            start_time=started_at,
        )
        writer_started = True
        state = app[INIT_RENDER_STATE_KEY]()
        _write_task_checkpoint(
            app,
            record.task_id,
            _worker_start_checkpoint_payload(record, state, started_at),
            updated_at=started_at,
        )

        async def cancel_check() -> bool:
            return _task_cancel_requested(app, record.task_id)

        async for message in app[STREAM_EVENTS_KEY](
            record.task_id,
            record.query,
            _stream_task_context(record),
            cancel_check,
        ):
            normalized = normalize_stream_event(record.task_id, message, clock())
            _renew_task_lease(app, record.task_id, worker_id, normalized["timestamp"])
            if normalized["type"] != "heartbeat":
                writer.record_event(normalized)
                state = app[UPDATE_STATE_KEY](state, scrub_secrets(message))
                if normalized["type"] == "error":
                    status = "failed"
                    error = _event_error(normalized)
            _append_task_event(app, record.task_id, normalized)
            event_executor = _research_graph_executor_from_event(normalized)
            graph_checkpoint = _graph_checkpoint_from_event(normalized)
            if graph_checkpoint is not None:
                graph_checkpoint = _checkpoint_with_research_graph_executor(
                    graph_checkpoint,
                    event_executor
                    or _checkpoint_research_graph_executor(
                        _read_task_checkpoint(app, record.task_id)
                    ),
                )
                _write_task_checkpoint(
                    app,
                    record.task_id,
                    graph_checkpoint,
                    updated_at=normalized["timestamp"],
                )
            else:
                _write_task_checkpoint(
                    app,
                    record.task_id,
                    _stream_checkpoint_payload(
                        app,
                        record.task_id,
                        status,
                        normalized["type"],
                        state,
                        research_graph_executor=event_executor,
                    ),
                    updated_at=normalized["timestamp"],
                )
        if await cancel_check():
            status = "cancelled"
            error = "task cancelled"
    except asyncio.CancelledError as exc:
        status = "cancelled"
        error = str(exc) or "task cancelled"
    except Exception as exc:
        status = "failed"
        error = str(exc)
        _append_task_event(
            app,
            record.task_id,
            {
                "task_id": record.task_id,
                "type": "error",
                "timestamp": clock(),
                "payload": {"error": scrub_secrets(error)},
            },
        )
    finally:
        end_time = clock()
        try:
            if writer_started:
                report_markdown = None
                render_error = None
                try:
                    if status == "completed":
                        report_markdown = app[RENDER_MARKDOWN_KEY](state)
                except Exception as exc:
                    render_error = exc

                if render_error:
                    archive_updates = {
                        "archive_status": "failed",
                        "archive_dir": str(writer.archive_dir) if writer.archive_dir else None,
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
            _write_task_checkpoint(
                app,
                record.task_id,
                _finish_checkpoint_payload(app, record.task_id, status, error, state),
                updated_at=end_time,
            )
            _clear_durable_task_lease(app, record.task_id, worker_id)
            _clear_active_task(app, record.task_id)
            _clear_task_cancel(app, record.task_id)
            _notify_task_finished(app, record.task_id)


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


def _task_store(app: web.Application) -> TaskStore:
    return app[TASK_STORE_KEY]


def _ensure_task_worker(app: web.Application, task_id: str) -> None:
    workers = app[TASK_WORKERS_KEY]
    worker = workers.get(task_id)
    if worker and not worker.done():
        return
    record = _task_store(app).get_task(task_id)
    if not record or record.status in FINAL_TASK_STATUSES:
        return
    worker = asyncio.create_task(_run_task_worker(app, task_id))
    workers[task_id] = worker

    def _forget_worker(done: asyncio.Task) -> None:
        if workers.get(task_id) is done:
            workers.pop(task_id, None)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("limira runner background task failed: task_id=%s", task_id)

    worker.add_done_callback(_forget_worker)


def _task_event_snapshot(
    app: web.Application,
    task_id: str,
) -> list[dict[str, Any]]:
    return [
        _durable_record_event(record)
        for record in _task_event_records_snapshot(app, task_id)
    ]


def _task_event_records_snapshot(
    app: web.Application,
    task_id: str,
) -> list[dict[str, Any]]:
    durable_records = _list_durable_task_event_records(app, task_id)
    if durable_records:
        app[TASK_EVENT_LOG_KEY][task_id] = [
            _durable_record_event(record) for record in durable_records
        ][-MAX_REPLAY_EVENTS:]
        return durable_records
    return [
        {"cursor": None, "event": event}
        for event in list(app[TASK_EVENT_LOG_KEY].get(task_id, []))
    ]


def _subscribe_task_events(
    app: web.Application,
    task_id: str,
) -> asyncio.Queue[dict[str, Any] | None]:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    app[TASK_SUBSCRIBERS_KEY].setdefault(task_id, set()).add(queue)
    return queue


def _unsubscribe_task_events(
    app: web.Application,
    task_id: str,
    queue: asyncio.Queue[dict[str, Any] | None],
) -> None:
    subscribers = app[TASK_SUBSCRIBERS_KEY].get(task_id)
    if not subscribers:
        return
    subscribers.discard(queue)
    if not subscribers:
        app[TASK_SUBSCRIBERS_KEY].pop(task_id, None)


def _append_task_event(
    app: web.Application,
    task_id: str,
    event: dict[str, Any],
) -> None:
    _append_durable_task_event(app, task_id, event)
    events = app[TASK_EVENT_LOG_KEY].setdefault(task_id, [])
    events.append(event)
    if len(events) > MAX_REPLAY_EVENTS:
        del events[:-MAX_REPLAY_EVENTS]
    for queue in list(app[TASK_SUBSCRIBERS_KEY].get(task_id, ())):
        queue.put_nowait(event)


def _notify_task_finished(app: web.Application, task_id: str) -> None:
    for queue in list(app[TASK_SUBSCRIBERS_KEY].get(task_id, ())):
        queue.put_nowait(None)


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
    worker = app[TASK_WORKERS_KEY].get(task_id)
    return task_id in app[ACTIVE_TASKS_KEY] or bool(worker and not worker.done())


def _classify_task_lease(app: web.Application, record: TaskRecord) -> str:
    if record.status != "running":
        return "missing"
    if _task_has_active_worker(app, record.task_id):
        return "local_active"
    if not record.worker_id or not record.lease_expires_at:
        return "missing"
    lease_expires_at = _parse_timestamp(record.lease_expires_at)
    now = _parse_timestamp(app[CLOCK_KEY]())
    if lease_expires_at is None or now is None:
        return "missing"
    if now <= lease_expires_at:
        return "external_active"
    return "expired"


def _task_worker_id(app: web.Application, task_id: str) -> str:
    return f"{app[RUNNER_WORKER_ID_KEY]}:{task_id}:{uuid.uuid4()}"


def _lease_expires_at_from(timestamp: str) -> str:
    parsed = _parse_timestamp(timestamp) or datetime.now(timezone.utc)
    return (parsed + timedelta(seconds=_stale_running_seconds())).isoformat()


def _renew_task_lease(
    app: web.Application,
    task_id: str,
    worker_id: str,
    heartbeat_at: str,
) -> None:
    renew = getattr(_task_store(app), "renew_task_lease", None)
    if renew is None:
        return
    renew(
        task_id,
        worker_id=worker_id,
        heartbeat_at=heartbeat_at,
        lease_expires_at=_lease_expires_at_from(heartbeat_at),
    )


def _clear_durable_task_lease(
    app: web.Application,
    task_id: str,
    worker_id: str,
) -> None:
    clear = getattr(_task_store(app), "clear_task_lease", None)
    if clear is not None:
        clear(task_id, worker_id=worker_id)


def _write_task_checkpoint(
    app: web.Application,
    task_id: str,
    checkpoint: dict[str, Any],
    *,
    updated_at: str,
) -> None:
    writer = getattr(_task_store(app), "write_task_checkpoint", None)
    if writer is None:
        return
    writer(
        task_id,
        checkpoint=scrub_secrets(_checkpoint_envelope(checkpoint)),
        updated_at=updated_at,
    )


def _finish_checkpoint_payload(
    app: web.Application,
    task_id: str,
    status: str,
    error: str | None,
    state: dict[str, Any],
) -> dict[str, Any]:
    previous = _read_task_checkpoint(app, task_id)
    if _is_graph_checkpoint(previous):
        checkpoint = dict(previous)
        checkpoint["status"] = status
        if status == "completed":
            checkpoint["resume_policy"] = "terminal"
            checkpoint["recoverable_reason"] = None
        elif status in FINAL_TASK_STATUSES:
            checkpoint.setdefault("resume_policy", "fail_recoverable")
            checkpoint.setdefault(
                "recoverable_reason",
                "graph_checkpoint_terminal_task",
            )
        return _checkpoint_with_research_graph_executor(
            checkpoint,
            _checkpoint_research_graph_executor(previous),
        )

    return _checkpoint_with_research_graph_executor(
        {
            "phase": "finished",
            "status": status,
            "error": scrub_secrets(error),
            "render_state": state,
        },
        _checkpoint_research_graph_executor(previous),
    )


def _stream_checkpoint_payload(
    app: web.Application,
    task_id: str,
    status: str,
    event_type: str,
    state: dict[str, Any],
    *,
    research_graph_executor: str | None = None,
) -> dict[str, Any]:
    previous = _read_task_checkpoint(app, task_id)
    executor = research_graph_executor or _checkpoint_research_graph_executor(previous)
    if _is_graph_checkpoint(previous):
        checkpoint = dict(previous)
        checkpoint["status"] = status
        return _checkpoint_with_research_graph_executor(checkpoint, executor)

    return _checkpoint_with_research_graph_executor(
        {
            "phase": "stream",
            "status": status,
            "event_count": len(_task_event_snapshot(app, task_id)),
            "last_event_type": event_type,
            "render_state": state,
        },
        executor,
    )


def _checkpoint_with_research_graph_executor(
    checkpoint: dict[str, Any],
    research_graph_executor: str | None,
) -> dict[str, Any]:
    executor = _normalized_research_graph_executor(research_graph_executor)
    if executor is None:
        return checkpoint
    updated = dict(checkpoint)
    updated["research_graph_executor"] = executor
    executor_state = updated.get("executor_state")
    if not isinstance(executor_state, dict):
        executor_state = {}
    else:
        executor_state = dict(executor_state)
    executor_state["research_graph_executor"] = executor
    updated["executor_state"] = executor_state
    return updated


def _stream_task_context(record: TaskRecord) -> dict[str, Any]:
    context = dict(record.context or {})
    if _is_resumable_langgraph_checkpoint(record.checkpoint):
        context["resume_checkpoint"] = record.checkpoint
    return context


def _worker_start_checkpoint_payload(
    record: TaskRecord,
    render_state: dict[str, Any],
    started_at: str,
) -> dict[str, Any]:
    checkpoint = record.checkpoint
    if not _is_resumable_langgraph_checkpoint(checkpoint):
        return {
            "phase": "started",
            "status": "running",
            "event_count": 0,
            "render_state": render_state,
        }
    resumed = dict(checkpoint or {})
    executor_state = resumed.get("executor_state")
    if not isinstance(executor_state, dict):
        executor_state = {}
    else:
        executor_state = dict(executor_state)
    executor_state.update(
        {
            "resume_worker_started_at": started_at,
            "resume_status": "running",
        }
    )
    resumed["status"] = "running"
    resumed["executor_state"] = executor_state
    return resumed


def _checkpoint_research_graph_executor(
    checkpoint: dict[str, Any] | None,
) -> str | None:
    if not isinstance(checkpoint, dict):
        return None
    executor = _normalized_research_graph_executor(
        checkpoint.get("research_graph_executor")
    )
    if executor is not None:
        return executor
    executor_state = checkpoint.get("executor_state")
    if isinstance(executor_state, dict):
        return _normalized_research_graph_executor(
            executor_state.get("research_graph_executor")
        )
    return None


def _is_resumable_langgraph_checkpoint(checkpoint: dict[str, Any] | None) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    return (
        _checkpoint_research_graph_executor(checkpoint) == "langgraph"
        and str(checkpoint.get("resume_policy") or "").strip()
        == "resume_from_checkpoint"
    )


def _checkpoint_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(scrub_secrets(item or "")).strip()
        if text and text not in seen:
            result.append(text[:120])
            seen.add(text)
    return result


def _research_graph_executor_from_event(event: dict[str, Any]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = [payload.get("research_graph_executor")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("research_graph_executor"))
    checkpoint = payload.get("checkpoint")
    if isinstance(checkpoint, dict):
        candidates.append(checkpoint.get("research_graph_executor"))
    for candidate in candidates:
        executor = _normalized_research_graph_executor(candidate)
        if executor is not None:
            return executor
    return None


def _normalized_research_graph_executor(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    executor = value.strip().lower()
    return executor if executor in RESEARCH_GRAPH_EXECUTORS else None


def _read_task_checkpoint(
    app: web.Application,
    task_id: str,
) -> dict[str, Any] | None:
    reader = getattr(_task_store(app), "get_task_checkpoint", None)
    if reader is None:
        return None
    checkpoint = reader(task_id)
    return checkpoint if isinstance(checkpoint, dict) else None


def _is_graph_checkpoint(checkpoint: dict[str, Any] | None) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    return str(checkpoint.get("phase") or "") in GRAPH_CHECKPOINT_PHASES


def _checkpoint_envelope(checkpoint: dict[str, Any]) -> dict[str, Any]:
    raw = dict(checkpoint or {})
    phase = str(raw.get("phase") or "unknown")
    status = str(raw.get("status") or "running")
    executor_state = raw.get("executor_state")
    if not isinstance(executor_state, dict):
        executor_state = {}
    for key in ("event_count", "last_event_type", "render_state"):
        if key in raw:
            executor_state[key] = raw[key]
    research_graph_executor = _normalized_research_graph_executor(
        raw.get("research_graph_executor")
    ) or _normalized_research_graph_executor(
        executor_state.get("research_graph_executor")
    )
    if research_graph_executor is not None:
        executor_state["research_graph_executor"] = research_graph_executor

    terminal = (
        phase in {"finished", "recovered", "complete"}
        and status in FINAL_TASK_STATUSES
    )
    resume_policy = raw.get("resume_policy")
    if not isinstance(resume_policy, str) or not resume_policy.strip():
        resume_policy = "terminal" if terminal else "fail_recoverable"
    recoverable_reason = raw.get("recoverable_reason")
    if recoverable_reason is None and resume_policy == "fail_recoverable":
        recoverable_reason = "legacy_stream_checkpoint_not_resumable"

    source_ledger = raw.get("source_ledger")
    if not isinstance(source_ledger, list):
        source_ledger = []
    evidence_ledger = raw.get("evidence_ledger")
    if not isinstance(evidence_ledger, list):
        evidence_ledger = []

    return {
        "phase": phase,
        "status": status,
        "current_research_unit": raw.get("current_research_unit"),
        "source_ledger": source_ledger,
        "evidence_ledger": evidence_ledger,
        "executor_state": executor_state,
        "research_graph_executor": research_graph_executor,
        "last_completed_node": _optional_operational_text(
            raw.get("last_completed_node")
        ),
        "current_node": _optional_operational_text(raw.get("current_node")),
        "completed_unit_ids": _checkpoint_string_list(raw.get("completed_unit_ids")),
        "pending_unit_ids": _checkpoint_string_list(raw.get("pending_unit_ids")),
        "resume_policy": resume_policy,
        "recoverable_reason": recoverable_reason,
    }


def _graph_checkpoint_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("type") != "research_graph_checkpoint":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    checkpoint = payload.get("checkpoint")
    return checkpoint if isinstance(checkpoint, dict) else None


def _append_durable_task_event(
    app: web.Application,
    task_id: str,
    event: dict[str, Any],
) -> None:
    appender = getattr(_task_store(app), "append_task_event", None)
    if appender is None:
        return
    appender(task_id, event, created_at=str(event.get("timestamp") or app[CLOCK_KEY]()))


def _list_durable_task_events(
    app: web.Application,
    task_id: str,
) -> list[dict[str, Any]]:
    return [
        _durable_record_event(record)
        for record in _list_durable_task_event_records(app, task_id)
    ]


def _list_durable_task_event_records(
    app: web.Application,
    task_id: str,
    *,
    after_cursor: Any = None,
) -> list[dict[str, Any]]:
    record_lister = getattr(_task_store(app), "list_task_event_records", None)
    if record_lister is not None:
        return [
            _durable_event_record(item)
            for item in record_lister(
                task_id,
                limit=MAX_REPLAY_EVENTS,
                after_cursor=after_cursor,
            )
        ]
    lister = getattr(_task_store(app), "list_task_events", None)
    if lister is None:
        return []
    if after_cursor is not None:
        return []
    return [
        {"cursor": None, "event": event}
        for event in lister(task_id, limit=MAX_REPLAY_EVENTS)
    ]


def _durable_event_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"cursor": None, "event": {}}
    event = value.get("event")
    if not isinstance(event, dict):
        event = {}
    return {"cursor": value.get("cursor"), "event": event}


def _durable_record_event(record: dict[str, Any]) -> dict[str, Any]:
    event = record.get("event") if isinstance(record, dict) else None
    return event if isinstance(event, dict) else {}


def _durable_record_cursor(record: dict[str, Any]) -> Any:
    return record.get("cursor") if isinstance(record, dict) else None


def _reconcile_stale_running_tasks(
    app: web.Application,
    *,
    limit: int = 100,
) -> list[TaskRecord]:
    store = _task_store(app)
    if not hasattr(store, "list_running_tasks"):
        return []
    reconciled: list[TaskRecord] = []
    for record in store.list_running_tasks(limit=limit):
        updated = _reconcile_task_if_stale(app, record)
        if updated.status in FINAL_TASK_STATUSES and updated.status != record.status:
            reconciled.append(updated)
    return reconciled


def _reconcile_task_if_stale(
    app: web.Application,
    record: TaskRecord,
) -> TaskRecord:
    if record.status != "running":
        return record
    reason = _stale_running_reason(app, record)
    if not reason:
        return record
    resumed = _resume_stale_langgraph_task(app, record, reason=reason)
    if resumed is not None:
        return resumed
    return _finalize_stale_running_task(app, record, reason=reason)


def _stale_running_reason(
    app: web.Application,
    record: TaskRecord,
) -> str | None:
    if record.status != "running":
        return None
    lease_state = _classify_task_lease(app, record)
    if lease_state == "external_active":
        return None
    if lease_state == "expired":
        return "expired_lease"
    if not _task_has_active_worker(app, record.task_id):
        return "no_active_worker"
    if not record.started_at:
        return "missing_started_at"

    now = _parse_timestamp(app[CLOCK_KEY]())
    started_at = _parse_timestamp(record.started_at)
    if now is None or started_at is None:
        return None
    stale_seconds = _stale_running_seconds()
    if (now - started_at).total_seconds() <= stale_seconds:
        return None

    heartbeat_at = _latest_heartbeat_at(app, record.task_id)
    if heartbeat_at is not None:
        if (now - heartbeat_at).total_seconds() > stale_seconds:
            return "stale_heartbeat"
        return None
    return "stale_started_at"


def _finalize_stale_running_task(
    app: web.Application,
    record: TaskRecord,
    *,
    reason: str,
) -> TaskRecord:
    completed_at = app[CLOCK_KEY]()
    error = f"stale_running_task_recovered:{reason}"
    warning = f"stale running task recovered: {reason}"
    store = _task_store(app)
    finalizer = getattr(store, "finalize_stale_running_task", None)
    if finalizer is None:
        return record
    updated = finalizer(
        record.task_id,
        completed_at=completed_at,
        error=error,
        warnings=[warning],
        lease_checked_at=completed_at,
    )
    if updated is None:
        return store.get_task(record.task_id) or record

    event = {
        "task_id": record.task_id,
        "type": "error",
        "timestamp": completed_at,
        "payload": {
            "status": "failed",
            "archive_status": "failed",
            "terminal": True,
            "error": error,
            "warning": warning,
            "recovery_reason": reason,
        },
    }
    _append_task_event(app, record.task_id, event)
    _write_task_checkpoint(
        app,
        record.task_id,
        {
            "phase": "recovered",
            "status": "failed",
            "executor_state": {"recovery_reason": reason},
            "resume_policy": "terminal",
            "recoverable_reason": error,
        },
        updated_at=completed_at,
    )
    _clear_active_task(app, record.task_id)
    _clear_task_cancel(app, record.task_id)
    _notify_task_finished(app, record.task_id)
    return updated


def _resume_stale_langgraph_task(
    app: web.Application,
    record: TaskRecord,
    *,
    reason: str,
) -> TaskRecord | None:
    if not _is_resumable_langgraph_checkpoint(record.checkpoint):
        return None
    resumed_at = app[CLOCK_KEY]()
    warning = f"stale running LangGraph task queued for resume: {reason}"
    store = _task_store(app)
    resumer = getattr(store, "resume_stale_running_task", None)
    if resumer is None:
        return None
    resumed = resumer(
        record.task_id,
        resumed_at=resumed_at,
        warnings=[warning],
        lease_checked_at=resumed_at,
    )
    if resumed is None:
        return store.get_task(record.task_id) or record

    event = {
        "task_id": record.task_id,
        "type": "status",
        "timestamp": resumed_at,
        "payload": {
            "status": "queued",
            "archive_status": "pending",
            "terminal": False,
            "warning": warning,
            "recovery_reason": reason,
            "resume_policy": "resume_from_checkpoint",
        },
    }
    _append_task_event(app, record.task_id, event)
    checkpoint = dict(record.checkpoint or {})
    executor_state = checkpoint.get("executor_state")
    if not isinstance(executor_state, dict):
        executor_state = {}
    else:
        executor_state = dict(executor_state)
    executor_state.update(
        {
            "recovery_reason": reason,
            "resume_queued_at": resumed_at,
            "resume_status": "queued",
        }
    )
    checkpoint.update(
        {
            "status": "queued",
            "executor_state": executor_state,
            "resume_policy": "resume_from_checkpoint",
            "recoverable_reason": None,
        }
    )
    _write_task_checkpoint(app, record.task_id, checkpoint, updated_at=resumed_at)
    _clear_active_task(app, record.task_id)
    _clear_task_cancel(app, record.task_id)
    if not _ensure_task_worker_if_loop_running(app, record.task_id):
        _queue_task_worker_on_startup(app, record.task_id)
    return _task_store(app).get_task(record.task_id) or resumed


def _ensure_task_worker_if_loop_running(app: web.Application, task_id: str) -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    _ensure_task_worker(app, task_id)
    worker = app[TASK_WORKERS_KEY].get(task_id)
    return bool(worker and not worker.done())


def _queue_task_worker_on_startup(app: web.Application, task_id: str) -> None:
    app[PENDING_STARTUP_WORKERS_KEY].add(task_id)


async def _start_pending_task_workers(app: web.Application) -> None:
    pending = sorted(app[PENDING_STARTUP_WORKERS_KEY])
    app[PENDING_STARTUP_WORKERS_KEY].clear()
    for task_id in pending:
        record = _task_store(app).get_task(task_id)
        if record and record.status == "queued":
            _ensure_task_worker(app, task_id)


def _latest_heartbeat_at(
    app: web.Application,
    task_id: str,
) -> datetime | None:
    record = _task_store(app).get_task(task_id)
    if record and record.heartbeat_at:
        heartbeat = _parse_timestamp(record.heartbeat_at)
        if heartbeat is not None:
            return heartbeat
    for event in reversed(_task_event_snapshot(app, task_id)):
        if event.get("type") != "heartbeat":
            continue
        timestamp = _parse_timestamp(event.get("timestamp"))
        if timestamp is not None:
            return timestamp
    return None


def _stale_running_seconds() -> int:
    try:
        value = int(str(os.getenv(STALE_RUNNING_SECONDS_ENV, "")).strip())
    except (TypeError, ValueError):
        value = DEFAULT_STALE_RUNNING_SECONDS
    return max(1, value)


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _transport_closing(request: web.Request) -> bool:
    transport = request.transport
    return transport is None or transport.is_closing()


def _finalize_running_without_worker_cancellation(
    request: web.Request,
    record: TaskRecord,
) -> TaskRecord:
    if _classify_task_lease(request.app, record) == "external_active":
        return record
    clock: Callable[[], str] = request.app[CLOCK_KEY]
    cancelled_at = clock()
    error = "task cancelled because no active stream worker was registered"
    store: TaskStore = request.app[TASK_STORE_KEY]
    canceller = getattr(store, "cancel_stale_running_task", None)
    if canceller is None:
        return record
    claimed = canceller(
        record.task_id,
        completed_at=cancelled_at,
        error=scrub_secrets(error),
        archive_status="failed",
        archive_dir=None,
        archive_zip_path=None,
        warnings=["cancelled running task without active stream worker"],
        lease_checked_at=cancelled_at,
    )
    if claimed is None:
        return store.get_task(record.task_id) or record

    writer = request.app[ARCHIVE_WRITER_KEY](request.app[ARCHIVE_ROOT_KEY], clock=clock)

    try:
        writer.start(
            task_id=claimed.task_id,
            query=claimed.query,
            user_id=claimed.user_id,
            model_summary=claimed.model_summary,
            start_time=claimed.started_at or cancelled_at,
        )
        end_time = clock()
        archive_result = writer.complete(
            state={},
            status="cancelled",
            error=error,
            end_time=end_time,
        )
        updated = store.update_task(
            claimed.task_id,
            completed_at=end_time,
            error=scrub_secrets(error),
            **_archive_result_updates(archive_result),
        )
    except Exception as exc:
        end_time = clock()
        final_error = f"{error}; archive finalization failed: {exc}"
        updated = store.update_task(
            claimed.task_id,
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
        "operational_status": _task_operational_status(record),
    }


def _task_operational_status(record: TaskRecord) -> dict[str, Any]:
    checkpoint = record.checkpoint if isinstance(record.checkpoint, dict) else {}
    source_ledger = checkpoint.get("source_ledger")
    evidence_ledger = checkpoint.get("evidence_ledger")
    executor_state = checkpoint.get("executor_state")
    research_graph_executor = _checkpoint_research_graph_executor(checkpoint)
    lease_state = "released"
    if record.status == "running":
        lease_state = "leased" if record.worker_id and record.lease_expires_at else "missing"

    return {
        "lease": {
            "state": lease_state,
            "worker_present": bool(record.worker_id),
            "lease_expires_at": record.lease_expires_at,
            "heartbeat_at": record.heartbeat_at,
            "attempt": record.attempt,
        },
        "checkpoint": {
            "phase": _optional_operational_text(checkpoint.get("phase")),
            "status": _optional_operational_text(checkpoint.get("status")),
            "research_graph_executor": research_graph_executor,
            "last_completed_node": _optional_operational_text(
                checkpoint.get("last_completed_node")
            ),
            "current_node": _optional_operational_text(checkpoint.get("current_node")),
            "updated_at": record.checkpoint_updated_at,
            "current_research_unit_present": bool(
                checkpoint.get("current_research_unit")
            ),
            "resume_policy": _optional_operational_text(
                checkpoint.get("resume_policy")
            ),
            "recoverable_reason": _optional_operational_text(
                checkpoint.get("recoverable_reason")
            ),
            "source_ledger_count": len(source_ledger)
            if isinstance(source_ledger, list)
            else 0,
            "evidence_ledger_count": len(evidence_ledger)
            if isinstance(evidence_ledger, list)
            else 0,
            "completed_unit_count": len(
                checkpoint.get("completed_unit_ids")
                if isinstance(checkpoint.get("completed_unit_ids"), list)
                else []
            ),
            "pending_unit_count": len(
                checkpoint.get("pending_unit_ids")
                if isinstance(checkpoint.get("pending_unit_ids"), list)
                else []
            ),
            "executor_state_present": isinstance(executor_state, dict)
            and bool(executor_state),
        },
        "recovery": {
            "reason": _task_recovery_reason(record, checkpoint),
        },
    }


def _optional_operational_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(scrub_secrets(value)).strip()
    return text or None


def _task_recovery_reason(
    record: TaskRecord,
    checkpoint: dict[str, Any],
) -> str | None:
    error = str(record.error or "")
    prefix = "stale_running_task_recovered:"
    if error.startswith(prefix):
        return _optional_operational_text(error.removeprefix(prefix))
    if checkpoint.get("phase") == "recovered":
        return _optional_operational_text(checkpoint.get("recoverable_reason"))
    return None


def _task_context_from_payload(
    payload: dict[str, Any],
    *,
    query: str,
) -> dict[str, Any]:
    scenario = _optional_context_string(payload.get("scenario"), "scenario")
    conversation_id = _optional_context_string(
        payload.get("conversation_id"),
        "conversation_id",
    )
    document_ids = _document_ids_from_payload(payload.get("document_ids"))
    upload_scope = _context_mapping_from_payload(
        payload.get("upload_scope"),
        "upload_scope",
    )
    source_policy = _context_mapping_from_payload(
        payload.get("source_policy"),
        "source_policy",
    )

    upload_scope.setdefault("source_type", "limira_upload")
    upload_scope.setdefault("document_ids", list(document_ids))
    upload_scope.setdefault("document_count", len(document_ids))

    default_source_policy = {
        "prefer_primary_sources": True,
        "allow_secondary_sources": True,
        "require_retrieved_at": True,
        "prefer_uploaded_documents": bool(document_ids),
        "prefer_scenario_sources": bool(scenario),
    }
    default_source_policy.update(source_policy)

    context = {
        "query": query,
        "scenario": scenario,
        "conversation_id": conversation_id,
        "document_ids": document_ids,
        "upload_scope": upload_scope,
        "source_policy": default_source_policy,
    }
    _assert_context_json_size(
        {key: value for key, value in context.items() if key != "query"}
    )
    return context


def _optional_context_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name}_invalid")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_CONTEXT_STRING_CHARS:
        raise ValueError(f"{field_name}_invalid")
    return normalized


def _document_ids_from_payload(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("document_ids_invalid")
    if len(value) > MAX_DOCUMENT_IDS:
        raise ValueError("too_many_documents")
    document_ids: list[str] = []
    seen: set[str] = set()
    for raw_document_id in value:
        if not isinstance(raw_document_id, str):
            raise ValueError("document_ids_invalid")
        document_id = raw_document_id.strip()
        if not document_id:
            continue
        if len(document_id) > MAX_CONTEXT_STRING_CHARS:
            raise ValueError("document_ids_invalid")
        if document_id not in seen:
            seen.add(document_id)
            document_ids.append(document_id)
    return document_ids


def _context_mapping_from_payload(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name}_invalid")
    _assert_context_json_size(value)
    return dict(value)


def _assert_context_json_size(value: Any) -> None:
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("task_context_invalid") from exc
    if len(serialized) > MAX_CONTEXT_JSON_CHARS:
        raise ValueError("task_context_too_large")


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
