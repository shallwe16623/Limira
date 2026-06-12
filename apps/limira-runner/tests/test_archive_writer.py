import json
import zipfile

import pytest

import archive_writer
from archive_writer import ResearchArchiveWriter, base_url_host, scrub_secrets


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

    assert "Limira Research Failed" in report
    assert "Captured events: 1" in report
    assert "failuresecret123456" not in report
    assert metadata["status"] == "failed"
    assert metadata["error"] == "Bearer [REDACTED]"


def test_cancelled_task_without_report_gets_diagnostic_report(tmp_path):
    writer = ResearchArchiveWriter(tmp_path, clock=lambda: "2026-06-06T12:00:00+00:00")
    writer.start("cancelled-task", "query", "user-a")
    writer.record_event({"event": "tool_call", "data": {"tool_name": "search"}})

    result = writer.complete(status="cancelled", error="Authorization: ApiKey abc def")

    assert result.archive_status == "ready"
    report = (result.archive_dir / "report.md").read_text(encoding="utf-8")
    metadata = json.loads(
        (result.archive_dir / "metadata.json").read_text(encoding="utf-8")
    )

    assert "Limira Research Cancelled" in report
    assert "Captured events: 1" in report
    assert "abc def" not in report
    assert metadata["status"] == "cancelled"
    assert metadata["error"] == "Authorization: [REDACTED]"


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


def test_base_url_host_removes_userinfo_path_query_and_fragment():
    assert (
        base_url_host("https://user:secret@api.example.com/path?api_key=x#frag")
        == "api.example.com"
    )
    assert base_url_host("api.example.com/path?token=x") == "api.example.com"
    assert base_url_host("user:secret@api.example.com/path") == "api.example.com"


def test_summarize_model_normalizes_caller_supplied_base_url_host(tmp_path):
    writer = ResearchArchiveWriter(tmp_path, clock=lambda: "2026-06-06T12:00:00+00:00")
    writer.start(
        "host-test",
        "query",
        "user-a",
        model_summary={
            "provider": "openai",
            "model": "gpt",
            "base_url_host": "user:secret@api.example.com/path?token=x",
        },
    )

    result = writer.complete(status="completed", report_markdown="done")
    metadata = json.loads(
        (result.archive_dir / "metadata.json").read_text(encoding="utf-8")
    )

    assert metadata["model"]["base_url_host"] == "api.example.com"
    assert "secret" not in json.dumps(metadata)
    assert "path" not in json.dumps(metadata)
    assert "token" not in json.dumps(metadata)


def test_metadata_includes_selected_research_graph_executor(tmp_path):
    writer = ResearchArchiveWriter(tmp_path, clock=lambda: "2026-06-06T12:00:00+00:00")
    writer.start("executor-metadata", "query", "user-a")
    writer.record_event(
        {
            "type": "research_graph_executor_selected",
            "payload": {
                "data": {
                    "research_graph_executor": "langgraph",
                    "token": "sk-secret",
                },
            },
        }
    )

    result = writer.complete(status="completed", report_markdown="done")
    metadata = json.loads(
        (result.archive_dir / "metadata.json").read_text(encoding="utf-8")
    )
    trace = json.loads((result.archive_dir / "trace.json").read_text(encoding="utf-8"))

    assert metadata["research_graph_executor"] == "langgraph"
    assert trace["events"][0]["type"] == "research_graph_executor_selected"
    serialized = json.dumps({"metadata": metadata, "trace": trace})
    assert "sk-secret" not in serialized


def test_scrub_secrets_redacts_full_authorization_string_values():
    payload = {
        "basic": "Authorization: Basic dXNlcjpzZWNyZXQ=",
        "api_header": "Authorization: ApiKey abc def",
        "bearer": "Authorization: Bearer abc.def.ghi",
        "delimited": "Authorization: ApiKey abc def; next=value",
    }

    scrubbed = scrub_secrets(payload)
    scrubbed_text = json.dumps(scrubbed)

    assert "dXNlcjpzZWNyZXQ" not in scrubbed_text
    assert "abc def" not in scrubbed_text
    assert "abc.def.ghi" not in scrubbed_text
    assert scrubbed["basic"] == "Authorization: [REDACTED]"
    assert scrubbed["api_header"] == "Authorization: [REDACTED]"
    assert scrubbed["bearer"] == "Authorization: [REDACTED]"
    assert scrubbed["delimited"] == "Authorization: [REDACTED]; next=value"


def test_scrub_secrets_preserves_news_urls_with_long_slugs():
    url = (
        "https://www.scmp.com/news/china/diplomacy/article/3356419/"
        "us-adds-alibaba-byd-and-other-chinese-tech-champions-military-company-list"
    )
    payload = {
        "url": url,
        "search_result": json.dumps({"title": "SCMP result", "url": url}),
        "source_with_query": f"{url}?api_key=secret-token-123&topic=byd",
    }

    scrubbed = scrub_secrets(payload)

    assert scrubbed["url"] == url
    assert url in scrubbed["search_result"]
    assert scrubbed["source_with_query"].startswith(url)
    assert "secret-token-123" not in scrubbed["source_with_query"]
    assert "topic=byd" in scrubbed["source_with_query"]


def test_archive_and_extracted_zip_do_not_contain_exact_secret_values(tmp_path):
    secrets = [
        "sk-querysecret123456",
        "baseurlsecret123456",
        "sk-urlsecret123456",
        "sk-modelsecret123456",
        "eventbearersecret123456",
        "cookiesessionsecret123456",
        "serpersecret123456",
        "errorbearersecret123456",
        "sk-stateerror123456",
        "statecookiesecret123456",
    ]
    writer = ResearchArchiveWriter(
        tmp_path / "archives",
        clock=lambda: "2026-06-06T12:00:00+00:00",
    )
    writer.start(
        task_id="secret-scan",
        query=f"research query OPENAI_API_KEY={secrets[0]}",
        user_id="user-a",
        model_summary={
            "provider": "deepseek",
            "model": "deepseek-v4",
            "base_url": (
                f"https://user:{secrets[1]}@api.example.com/path?api_key={secrets[2]}"
            ),
            "api_key": secrets[3],
        },
    )
    writer.record_event(
        {
            "type": "tool_call",
            "timestamp": "2026-06-06T12:00:01+00:00",
            "payload": {
                "event": "tool_call",
                "data": {
                    "Authorization": f"Bearer {secrets[4]}",
                    "Cookie": f"session={secrets[5]}",
                    "headers": {
                        "Set-Cookie": f"token={secrets[9]}",
                    },
                    "note": f"SERPER_API_KEY={secrets[6]}",
                },
            },
        }
    )

    result = writer.complete(
        status="failed",
        error=f"Authorization: Bearer {secrets[7]}",
        state={
            "errors": [
                f"DEEPSEEK_API_KEY={secrets[8]}",
                f"Cookie: state={secrets[9]}",
            ]
        },
    )

    assert result.archive_status == "ready"
    assert result.archive_zip_path is not None

    for path in result.archive_dir.rglob("*"):
        if path.is_file() and path.name != "archive.zip":
            assert_no_exact_secret_values(path.read_text(encoding="utf-8"), secrets)

    extract_dir = tmp_path / "unzipped"
    with zipfile.ZipFile(result.archive_zip_path) as archive:
        names = archive.namelist()
        assert sorted(names) == [
            "metadata.json",
            "report.html",
            "report.md",
            "trace.json",
        ]
        assert all(not name.startswith("/") for name in names)
        assert all(".." not in name.split("/") for name in names)
        assert all(".env" not in name for name in names)
        assert all("logs/" not in name for name in names)
        assert all("__pycache__" not in name for name in names)
        archive.extractall(extract_dir)

    for path in extract_dir.rglob("*"):
        if path.is_file():
            assert_no_exact_secret_values(path.read_text(encoding="utf-8"), secrets)


def assert_no_exact_secret_values(text: str, secrets: list[str]) -> None:
    for secret in secrets:
        assert secret not in text
