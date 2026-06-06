import json
import zipfile

import pytest

import archive_writer
from archive_writer import ResearchArchiveWriter, scrub_secrets


def test_complete_success_creates_expected_archive(tmp_path):
    writer = ResearchArchiveWriter(tmp_path, clock=lambda: "2026-06-06T12:00:00+00:00")
    writer.start(
        task_id="task-1",
        query="research query",
        user_id="user-a",
        model_summary={
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com/chat/completions?api_key=secret",
            "api_key": "model-secret",
        },
    )

    writer.record_event({"event": "heartbeat", "data": {"token": "ignored-secret"}})
    writer.record_event(
        {
            "event": "tool_call",
            "data": {
                "tool_name": "google_search",
                "Authorization": "Bearer abcdefghijklmnop",
                "nested": {"OPENAI_API_KEY": "sk-secretvalue"},
            },
        }
    )

    result = writer.complete(
        status="completed",
        report_markdown="# Report\nAuthorization: Bearer abcdefghijklmnop\n<script>x</script>",
        end_time="2026-06-06T12:01:00+00:00",
    )

    assert result.archive_status == "ready"
    assert result.archive_zip_path is not None
    assert result.archive_zip_path.exists()

    archive_dir = result.archive_dir
    assert {path.name for path in archive_dir.iterdir()} == {
        "trace.json",
        "report.md",
        "metadata.json",
        "report.html",
        "archive.zip",
    }

    trace = json.loads((archive_dir / "trace.json").read_text(encoding="utf-8"))
    assert trace["version"] == 1
    assert trace["task_id"] == "task-1"
    assert len(trace["events"]) == 1
    assert trace["events"][0]["type"] == "tool_call"

    trace_text = json.dumps(trace)
    assert "abcdefghijklmnop" not in trace_text
    assert "sk-secretvalue" not in trace_text
    assert "[REDACTED]" in trace_text

    metadata = json.loads((archive_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["archive_status"] == "ready"
    assert metadata["model"] == {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "base_url_host": "api.deepseek.com",
    }
    assert "chat/completions" not in json.dumps(metadata)

    report = (archive_dir / "report.md").read_text(encoding="utf-8")
    assert "abcdefghijklmnop" not in report
    assert "[REDACTED]" in report

    report_html = (archive_dir / "report.html").read_text(encoding="utf-8")
    assert "<!doctype html>" in report_html
    assert "<main>" in report_html
    assert "&lt;script&gt;x&lt;/script&gt;" in report_html
    assert "<script>x</script>" not in report_html

    with zipfile.ZipFile(result.archive_zip_path) as archive:
        assert sorted(archive.namelist()) == [
            "metadata.json",
            "report.html",
            "report.md",
            "trace.json",
        ]


def test_failed_task_without_report_gets_diagnostic_report(tmp_path):
    writer = ResearchArchiveWriter(tmp_path, clock=lambda: "2026-06-06T12:00:00+00:00")
    writer.start("failed-task", "query", "user-a")
    writer.record_event(
        {"event": "message", "data": {"delta": {"content": "progress"}}}
    )

    result = writer.complete(status="failed", error="Bearer failuresecret123456")

    assert result.archive_status == "ready"
    report = (result.archive_dir / "report.md").read_text(encoding="utf-8")
    metadata = json.loads(
        (result.archive_dir / "metadata.json").read_text(encoding="utf-8")
    )

    assert "MiroThinker Research Failed" in report
    assert "Captured events: 1" in report
    assert "failuresecret123456" not in report
    assert metadata["status"] == "failed"
    assert metadata["error"] == "Bearer [REDACTED]"


def test_start_rejects_path_traversal_task_id(tmp_path):
    writer = ResearchArchiveWriter(tmp_path)

    with pytest.raises(ValueError):
        writer.start("../bad", "query", "user-a")


def test_zip_failure_marks_archive_failed(tmp_path, monkeypatch):
    writer = ResearchArchiveWriter(tmp_path, clock=lambda: "2026-06-06T12:00:00+00:00")
    writer.start("zip-fail", "query", "user-a")

    def fail_zip(*args, **kwargs):
        raise RuntimeError("zip unavailable")

    monkeypatch.setattr(archive_writer.zipfile, "ZipFile", fail_zip)

    result = writer.complete(status="completed", report_markdown="done")

    assert result.archive_status == "failed"
    assert result.archive_zip_path is None
    assert result.warnings == ["archive.zip creation failed: zip unavailable"]
    metadata = json.loads(
        (result.archive_dir / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["archive_status"] == "failed"


def test_scrub_secrets_recurses_through_nested_values():
    payload = {
        "headers": {
            "Authorization": "Bearer token-should-vanish",
            "Cookie": "session=supersecret",
        },
        "items": [
            "OPENAI_API_KEY=sk-abc123456789",
            {"nested_token": "eyJaaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc"},
        ],
    }

    scrubbed = scrub_secrets(payload)
    scrubbed_text = json.dumps(scrubbed)

    assert "token-should-vanish" not in scrubbed_text
    assert "supersecret" not in scrubbed_text
    assert "sk-abc123456789" not in scrubbed_text
    assert "eyJaaaaaaaaaaaa" not in scrubbed_text
    assert scrubbed["headers"]["Authorization"] == "[REDACTED]"
    assert scrubbed["headers"]["Cookie"] == "[REDACTED]"
