import inspect
import json
from pathlib import Path

import main as gradio_main
from archive_writer import ResearchArchiveWriter
from src.logging.task_logger import TaskLog


def test_gradio_fallback_entrypoints_remain_import_compatible():
    assert callable(gradio_main.gradio_run)
    assert callable(gradio_main.stop_current)
    assert callable(gradio_main.build_demo)
    assert callable(gradio_main.stream_events_optimized)

    gradio_run_signature = inspect.signature(gradio_main.gradio_run)
    assert list(gradio_run_signature.parameters) == ["query", "ui_state"]

    stream_signature = inspect.signature(gradio_main.stream_events_optimized)
    assert list(stream_signature.parameters) == [
        "task_id",
        "query",
        "_",
        "disconnect_check",
    ]


def test_task_log_save_keeps_old_task_json_contract(tmp_path):
    log_dir = tmp_path / "logs" / "api-server"
    task_log = TaskLog(
        log_dir=str(log_dir),
        task_id="old-trace-task",
        start_time="2026-06-06 12:00:00",
        input={"task_description": "query", "task_file_name": None},
    )

    saved_path = task_log.save()

    assert saved_path.startswith(str(log_dir / "task_old-trace-task_"))
    assert saved_path.endswith(".json")
    payload = json.loads((log_dir / saved_path.rsplit("/", 1)[-1]).read_text())
    assert payload["task_id"] == "old-trace-task"
    assert payload["log_dir"] == str(log_dir)


def test_archive_trace_does_not_replace_legacy_task_log_json(tmp_path):
    log_dir = tmp_path / "logs" / "api-server"
    task_log = TaskLog(
        log_dir=str(log_dir),
        task_id="legacy-task",
        start_time="2026-06-06 12:00:00",
        input={"task_description": "legacy query", "task_file_name": None},
    )
    legacy_path = Path(task_log.save())

    writer = ResearchArchiveWriter(
        tmp_path / "archives",
        clock=lambda: "2026-06-06T12:00:00+00:00",
    )
    writer.start("archive-task", "archive query", "user-a")
    writer.record_event(
        {
            "type": "message",
            "timestamp": "2026-06-06T12:00:01+00:00",
            "payload": {"event": "message", "data": {"delta": {"content": "done"}}},
        }
    )
    result = writer.complete(status="completed", report_markdown="# Report")
    archive_trace_path = result.archive_dir / "trace.json"

    assert legacy_path.exists()
    assert archive_trace_path.exists()
    assert legacy_path.parent == log_dir
    assert result.archive_dir.parent == tmp_path / "archives"
    assert archive_trace_path.parent == result.archive_dir
    assert legacy_path != archive_trace_path

    legacy_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
    archive_trace = json.loads(archive_trace_path.read_text(encoding="utf-8"))
    assert legacy_payload["task_id"] == "legacy-task"
    assert archive_trace["task_id"] == "archive-task"
    assert "events" in archive_trace
    assert "main_agent_message_history" in legacy_payload
