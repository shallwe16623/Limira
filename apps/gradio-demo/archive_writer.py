import html
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


REDACTED = "[REDACTED]"
ARCHIVE_VERSION = 1
ARCHIVE_FILES = ("trace.json", "report.md", "metadata.json", "report.html")
VALID_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VALID_FINAL_STATUSES = {"completed", "failed", "cancelled"}
VALID_ARCHIVE_STATUSES = {"pending", "ready", "failed"}

SENSITIVE_KEY_PARTS = {
    "api_key",
    "authorization",
    "cookie",
    "set-cookie",
    "token",
    "secret",
}
SENSITIVE_ENV_NAMES = {
    "serper_api_key",
    "jina_api_key",
    "e2b_api_key",
    "openai_api_key",
    "deepseek_api_key",
    "tencentcloud_secret_id",
    "tencentcloud_secret_key",
}

SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"(Authorization\s*[:=]\s*)([^\s,;]+)", re.IGNORECASE),
    re.compile(r"((?:Set-)?Cookie\s*[:=]\s*)([^\n\r]+)", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+){1,2}\b"),
    re.compile(
        r"\b("
        r"API_KEY|SERPER_API_KEY|JINA_API_KEY|E2B_API_KEY|OPENAI_API_KEY|"
        r"DEEPSEEK_API_KEY|TENCENTCLOUD_SECRET_ID|TENCENTCLOUD_SECRET_KEY"
        r")\s*[:=]\s*['\"]?[^'\"\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Za-z0-9_+/=-]{32,}\b"),
)


@dataclass
class ArchiveResult:
    task_id: str
    archive_dir: Path
    archive_status: str
    archive_zip_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def archive_timestamp(timestamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y%m%d-%H%M%S")


def validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not VALID_TASK_ID.match(task_id):
        raise ValueError("task_id contains unsafe characters")
    if ".." in task_id or "/" in task_id or "\\" in task_id:
        raise ValueError("task_id contains path traversal characters")
    return task_id


def is_sensitive_key(key: Any) -> bool:
    key_text = str(key).lower()
    return key_text in SENSITIVE_ENV_NAMES or any(
        part in key_text for part in SENSITIVE_KEY_PARTS
    )


def scrub_string(value: str) -> str:
    scrubbed = value
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("(Authorization"):
            scrubbed = pattern.sub(r"\1" + REDACTED, scrubbed)
        elif pattern.pattern.startswith("((?:Set-)?Cookie"):
            scrubbed = pattern.sub(r"\1" + REDACTED, scrubbed)
        elif pattern.pattern.startswith(r"\b("):
            scrubbed = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", scrubbed)
        elif pattern.pattern.startswith("Bearer"):
            scrubbed = pattern.sub(f"Bearer {REDACTED}", scrubbed)
        else:
            scrubbed = pattern.sub(REDACTED, scrubbed)
    return scrubbed


def scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            scrubbed[key] = REDACTED if is_sensitive_key(key) else scrub_secrets(item)
        return scrubbed
    if isinstance(value, (list, tuple, set)):
        return [scrub_secrets(item) for item in value]
    if isinstance(value, str):
        return scrub_string(value)
    return value


def base_url_host(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlparse(base_url)
    if parsed.netloc:
        return parsed.netloc
    if parsed.scheme:
        return parsed.path or None
    return base_url.split("/", 1)[0] or None


def summarize_model(model_summary: dict[str, Any] | None) -> dict[str, Any]:
    model_summary = scrub_secrets(model_summary or {})
    base_url = model_summary.get("base_url")
    return {
        "provider": model_summary.get("provider"),
        "model": model_summary.get("model") or model_summary.get("model_name"),
        "base_url_host": model_summary.get("base_url_host") or base_url_host(base_url),
    }


class ResearchArchiveWriter:
    def __init__(
        self,
        archive_root: Path,
        clock: Callable[[], str] | None = None,
    ):
        self.archive_root = Path(archive_root)
        self.clock = clock or utc_now_iso
        self.task_id: str | None = None
        self.query: str | None = None
        self.user_id: str | None = None
        self.start_time: str | None = None
        self.model_summary: dict[str, Any] = {}
        self.archive_dir: Path | None = None
        self.events: list[dict[str, Any]] = []

    def start(
        self,
        task_id: str,
        query: str,
        user_id: str,
        model_summary: dict[str, Any] | None = None,
        start_time: str | None = None,
    ) -> None:
        task_id = validate_task_id(task_id)
        self.task_id = task_id
        self.query = query
        self.user_id = user_id
        self.start_time = start_time or self.clock()
        self.model_summary = summarize_model(model_summary)
        self.events = []

        dirname = f"{archive_timestamp(self.start_time)}_{task_id}"
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.archive_dir = self.archive_root / dirname
        self.archive_dir.mkdir(mode=0o700, exist_ok=False)

    def record_event(self, event: dict[str, Any]) -> None:
        self._ensure_started()
        event_type = event.get("type") or event.get("event") or "unknown"
        if event_type == "heartbeat":
            return

        timestamp = event.get("timestamp") or self.clock()
        payload = event.get("payload") if "payload" in event else dict(event)
        normalized = {
            "type": event_type,
            "timestamp": timestamp,
            "payload": payload,
        }
        self.events.append(scrub_secrets(normalized))

    def complete(
        self,
        state: dict[str, Any] | None = None,
        status: str = "completed",
        error: str | None = None,
        report_markdown: str | None = None,
        end_time: str | None = None,
    ) -> ArchiveResult:
        self._ensure_started()
        if status not in VALID_FINAL_STATUSES:
            raise ValueError(f"unsupported final status: {status}")

        end_time = end_time or self.clock()
        metadata = self._metadata(status, "ready", error, end_time)
        report = self._report(status, error, state, report_markdown)

        self._write_json(
            "trace.json",
            {
                "version": ARCHIVE_VERSION,
                "task_id": self.task_id,
                "events": self.events,
            },
        )
        self._write_text("report.md", scrub_secrets(report))
        self._write_text("report.html", self._render_html(scrub_secrets(report)))
        self._write_json("metadata.json", metadata)

        warnings: list[str] = []
        zip_path: Path | None = self.archive_dir / "archive.zip"  # type: ignore[operator]
        archive_status = "ready"
        try:
            self._create_zip(zip_path)
        except Exception as exc:
            archive_status = "failed"
            zip_path = None
            warnings.append(f"archive.zip creation failed: {exc}")
            self._write_json(
                "metadata.json", self._metadata(status, "failed", error, end_time)
            )

        return ArchiveResult(
            task_id=self.task_id or "",
            archive_dir=self.archive_dir or self.archive_root,
            archive_status=archive_status,
            archive_zip_path=zip_path,
            warnings=warnings,
        )

    def _metadata(
        self,
        status: str,
        archive_status: str,
        error: str | None,
        end_time: str,
    ) -> dict[str, Any]:
        if archive_status not in VALID_ARCHIVE_STATUSES:
            raise ValueError(f"unsupported archive status: {archive_status}")
        return scrub_secrets(
            {
                "version": ARCHIVE_VERSION,
                "task_id": self.task_id,
                "query": self.query,
                "user_id": self.user_id,
                "start_time": self.start_time,
                "end_time": end_time,
                "status": status,
                "archive_status": archive_status,
                "archive_filename": "archive.zip",
                "model": self.model_summary,
                "error": error,
            }
        )

    def _report(
        self,
        status: str,
        error: str | None,
        state: dict[str, Any] | None,
        report_markdown: str | None,
    ) -> str:
        if report_markdown:
            return report_markdown
        if status == "completed":
            return "*No report content was provided.*"

        lines = [
            f"# MiroThinker Research {status.title()}",
            "",
            f"- Task ID: {self.task_id}",
            f"- Status: {status}",
        ]
        if error:
            lines.append(f"- Error: {error}")
        if state:
            errors = state.get("errors") if isinstance(state, dict) else None
            if errors:
                lines.append("")
                lines.append("## Errors")
                for item in errors:
                    lines.append(f"- {item}")
        lines.append("")
        lines.append(f"Captured events: {len(self.events)}")
        return "\n".join(lines)

    def _render_html(self, report_markdown: str) -> str:
        escaped = html.escape(report_markdown)
        return (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="utf-8" />\n'
            "    <title>MiroThinker Research Report</title>\n"
            "  </head>\n"
            "  <body>\n"
            "    <main>\n"
            f"      <pre>{escaped}</pre>\n"
            "    </main>\n"
            "  </body>\n"
            "</html>\n"
        )

    def _write_json(self, filename: str, payload: dict[str, Any]) -> None:
        assert self.archive_dir is not None
        path = self.archive_dir / filename
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_text(self, filename: str, content: str) -> None:
        assert self.archive_dir is not None
        path = self.archive_dir / filename
        path.write_text(content, encoding="utf-8")

    def _create_zip(self, zip_path: Path) -> None:
        assert self.archive_dir is not None
        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for filename in ARCHIVE_FILES:
                archive.write(self.archive_dir / filename, arcname=filename)

    def _ensure_started(self) -> None:
        if not self.task_id or self.archive_dir is None:
            raise RuntimeError("archive writer has not been started")
