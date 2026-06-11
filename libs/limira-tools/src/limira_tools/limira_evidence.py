from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .limira_artifacts import record_research_artifact


MAX_AUTO_EVIDENCE_PER_TOOL_CALL = 10
MAX_EVIDENCE_SUMMARY_CHARS = 4000


class ToolEvidenceLedger:
    """Derive audit source events from tool results.

    The model can still create richer artifacts, but source-bearing tools should
    automatically create source records so citations do not depend only on
    model-initiated recorder calls. Snippet-only search results remain source
    candidates until a content-bearing tool retrieves or summarizes the source.
    """

    def __init__(self, *, task_id: str):
        self.task_id = str(task_id)
        self._tool_inputs: dict[str, dict[str, Any]] = {}

    def events_from_message(self, message: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(message, Mapping) or message.get("event") != "tool_call":
            return []
        data = message.get("data")
        if not isinstance(data, Mapping):
            return []

        tool_call_id = str(data.get("tool_call_id") or "")
        tool_name = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, Mapping):
            return []

        if "result" not in tool_input:
            if tool_call_id and _has_user_tool_arguments(tool_input):
                self._tool_inputs[tool_call_id] = dict(tool_input)
            return []

        arguments = self._tool_inputs.get(tool_call_id, {})
        result = tool_input.get("result")
        return tool_evidence_events_from_result(
            task_id=self.task_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
            result=result,
        )


def tool_evidence_events_from_result(
    *,
    task_id: str,
    tool_name: str,
    tool_call_id: str | None,
    arguments: Mapping[str, Any] | None,
    result: Any,
) -> list[dict[str, Any]]:
    arguments = arguments if isinstance(arguments, Mapping) else {}
    normalized_tool = str(tool_name or "")
    if normalized_tool == "google_search":
        return _google_search_source_candidate_events(
            task_id=task_id,
            tool_name=normalized_tool,
            tool_call_id=str(tool_call_id or ""),
            arguments=arguments,
            result=result,
        )
    if normalized_tool == "scrape_and_extract_info":
        return _jina_summary_evidence_events(
            task_id=task_id,
            tool_name=normalized_tool,
            tool_call_id=str(tool_call_id or ""),
            arguments=arguments,
            result=result,
        )
    if normalized_tool in {"scrape", "scrape_website"}:
        return _scrape_evidence_events(
            task_id=task_id,
            tool_name=normalized_tool,
            tool_call_id=str(tool_call_id or ""),
            arguments=arguments,
            result=result,
        )
    return []


def _google_search_source_candidate_events(
    *,
    task_id: str,
    tool_name: str,
    tool_call_id: str,
    arguments: Mapping[str, Any],
    result: Any,
) -> list[dict[str, Any]]:
    parsed = _parse_json_mapping(result)
    if not parsed:
        return []
    organic = parsed.get("organic")
    if not isinstance(organic, list):
        return []
    search_parameters = parsed.get("searchParameters")
    query = ""
    if isinstance(search_parameters, Mapping):
        query = str(search_parameters.get("q") or "")
    query = query or str(arguments.get("q") or "")

    events: list[dict[str, Any]] = []
    for index, item in enumerate(organic[:MAX_AUTO_EVIDENCE_PER_TOOL_CALL]):
        if not isinstance(item, Mapping):
            continue
        url = _first_text(item, "link", "url", "source_url")
        title = _first_text(item, "title", "name") or url
        summary = _first_text(item, "snippet", "summary", "description") or title
        if not (url or summary):
            continue
        events.append(
            _source_candidate_event(
                task_id=task_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                index=index,
                title=title,
                url=url,
                summary=summary,
                source_type="web_search_result",
                confidence=0.25,
                query=query,
            )
        )
    return events


def _jina_summary_evidence_events(
    *,
    task_id: str,
    tool_name: str,
    tool_call_id: str,
    arguments: Mapping[str, Any],
    result: Any,
) -> list[dict[str, Any]]:
    parsed = _parse_json_mapping(result)
    if not parsed:
        return []
    if parsed.get("success") is False:
        return []
    url = _first_text(parsed, "url") or _first_text(arguments, "url")
    summary = _first_text(parsed, "extracted_info", "summary", "content")
    if not summary:
        return []
    return [
        _evidence_event(
            task_id=task_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            index=0,
            title=_title_from_url(url) or "Web page summary",
            url=url,
            summary=summary,
            source_type="web_page_summary",
            confidence=0.8,
            query=_first_text(arguments, "info_to_extract", "query", "q"),
        )
    ]


def _scrape_evidence_events(
    *,
    task_id: str,
    tool_name: str,
    tool_call_id: str,
    arguments: Mapping[str, Any],
    result: Any,
) -> list[dict[str, Any]]:
    url = _first_text(arguments, "url", "source_url")
    if not url:
        parsed = _parse_json_mapping(result)
        if parsed:
            url = _first_text(parsed, "url", "source_url")
            result = _first_text(parsed, "content", "text", "summary") or result
    summary = _summary_text(result)
    if not (url and summary):
        return []
    return [
        _evidence_event(
            task_id=task_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            index=0,
            title=_title_from_url(url) or "Scraped web page",
            url=url,
            summary=summary,
            source_type="web_page_content",
            confidence=0.75,
            query="",
        )
    ]


def _evidence_event(
    *,
    task_id: str,
    tool_name: str,
    tool_call_id: str,
    index: int,
    title: str,
    url: str,
    summary: str,
    source_type: str,
    confidence: float,
    query: str,
) -> dict[str, Any]:
    summary = _summary_text(summary)
    source = url or title or summary
    content_hash = hashlib.sha256(
        f"{source}\n{summary}".encode("utf-8", errors="replace")
    ).hexdigest()[:32]
    evidence_id = _evidence_id(
        task_id=task_id,
        source=source,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        index=index,
    )
    payload: dict[str, Any] = {
        "evidence_id": evidence_id,
        "title": title or source,
        "summary": summary,
        "quote_or_summary": summary,
        "source_type": source_type,
        "source_state": "verified_evidence",
        "source_content_state": "content_bearing",
        "candidate": False,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash,
        "source_event_type": "tool_evidence_ledger",
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
    }
    if url:
        payload["url"] = url
        payload["source_url"] = url
    if query:
        payload["query"] = query
    return record_research_artifact(
        "evidence",
        payload,
        evidence_refs=[evidence_id],
        confidence=confidence,
    )


def _source_candidate_event(
    *,
    task_id: str,
    tool_name: str,
    tool_call_id: str,
    index: int,
    title: str,
    url: str,
    summary: str,
    source_type: str,
    confidence: float,
    query: str,
) -> dict[str, Any]:
    summary = _summary_text(summary)
    source = url or title or summary
    content_hash = hashlib.sha256(
        f"{source}\n{summary}".encode("utf-8", errors="replace")
    ).hexdigest()[:32]
    candidate_id = _source_candidate_id(
        task_id=task_id,
        source=source,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        index=index,
    )
    payload: dict[str, Any] = {
        "candidate_id": candidate_id,
        "title": title or source,
        "summary": summary,
        "snippet": summary,
        "source_type": source_type,
        "source_state": "source_candidate",
        "source_content_state": "snippet_only",
        "candidate": True,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash,
        "source_event_type": "tool_evidence_ledger",
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
    }
    if url:
        payload["url"] = url
        payload["source_url"] = url
    if query:
        payload["query"] = query
    return record_research_artifact(
        "source_candidate",
        payload,
        confidence=confidence,
    )


def _evidence_id(
    *,
    task_id: str,
    source: str,
    tool_name: str,
    tool_call_id: str,
    index: int,
) -> str:
    digest = hashlib.sha256(
        f"{task_id}:{tool_name}:{tool_call_id}:{source}:{index}".encode("utf-8")
    ).hexdigest()
    return f"EVID-{digest[:12]}"


def _source_candidate_id(
    *,
    task_id: str,
    source: str,
    tool_name: str,
    tool_call_id: str,
    index: int,
) -> str:
    digest = hashlib.sha256(
        f"{task_id}:{tool_name}:{tool_call_id}:{source}:{index}".encode("utf-8")
    ).hexdigest()
    return f"SRC-{digest[:12]}"


def _parse_json_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is None or isinstance(value, (Mapping, list, tuple, set)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _summary_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text[:MAX_EVIDENCE_SUMMARY_CHARS]


def _title_from_url(url: str) -> str:
    text = str(url or "").rstrip("/")
    if not text:
        return ""
    return text.rsplit("/", 1)[-1] or text


def _has_user_tool_arguments(value: Mapping[str, Any]) -> bool:
    return any(str(key) not in {"result", "error"} for key in value)
