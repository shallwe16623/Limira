import inspect
import json

import main as gradio_main
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
