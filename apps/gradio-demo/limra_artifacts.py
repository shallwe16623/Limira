from __future__ import annotations

from typing import Any

from archive_writer import scrub_secrets


ARTIFACT_EVENT_TYPES: dict[str, str] = {
    "evidence": "evidence_collected",
    "entity": "entity_extracted",
    "relation": "relation_extracted",
    "timeline_event": "timeline_event_added",
    "map_feature": "map_feature_added",
    "verification": "verification_result",
    "report_section": "report_section_generated",
}

SUPPORTED_ARTIFACT_TYPES = frozenset(ARTIFACT_EVENT_TYPES)


def artifact_recording_prompt_instruction() -> str:
    return """When the `record_research_artifact` tool is available, use it to record OSINT artifacts as structured data before they are summarized in prose.

Record artifacts after search/scrape evidence collection, entity or relation extraction, timeline or map extraction, verification work, and report section drafting. Supported `artifact_type` values are: evidence, entity, relation, timeline_event, map_feature, verification, report_section. Treat validation warnings from this tool as non-fatal and continue the research task."""


def record_research_artifact(
    artifact_type: Any,
    payload: Any,
    evidence_refs: Any = None,
    confidence: Any = None,
    notes: Any = None,
) -> dict[str, Any]:
    normalized_type = _normalize_artifact_type(artifact_type)
    if normalized_type not in SUPPORTED_ARTIFACT_TYPES:
        return _artifact_warning(
            "unsupported_artifact_type",
            normalized_type or "unknown",
            [f"unsupported artifact_type: {artifact_type!r}"],
            payload,
        )
    if not isinstance(payload, dict):
        return _artifact_warning(
            "invalid_artifact_payload",
            normalized_type,
            ["payload must be an object"],
            payload,
        )

    errors = _validate_artifact_payload(normalized_type, payload)
    normalized_refs, ref_errors = _normalize_evidence_refs(evidence_refs)
    errors.extend(ref_errors)
    normalized_confidence, confidence_error = _normalize_confidence(confidence)
    if confidence_error:
        errors.append(confidence_error)

    if errors:
        return _artifact_warning(
            "invalid_artifact_payload",
            normalized_type,
            errors,
            payload,
        )

    artifact_payload = dict(scrub_secrets(payload))
    artifact_payload.setdefault("artifact_type", normalized_type)
    artifact_payload.setdefault("source_event_type", "record_research_artifact")
    if normalized_refs is not None:
        artifact_payload["evidence_refs"] = normalized_refs
    if normalized_confidence is not None:
        artifact_payload["confidence"] = normalized_confidence
    if notes is not None and str(notes).strip():
        artifact_payload["notes"] = scrub_secrets(str(notes))

    return {
        "type": ARTIFACT_EVENT_TYPES[normalized_type],
        "payload": artifact_payload,
    }


def artifact_event_from_tool_call(message: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("event") != "tool_call":
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    if data.get("tool_name") != "record_research_artifact":
        return None

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return _artifact_warning(
            "invalid_artifact_payload",
            "unknown",
            ["record_research_artifact tool_input must be an object"],
            tool_input,
        )

    # Tool output events often carry only {"result": ...}; do not duplicate records.
    if "artifact_type" not in tool_input and "payload" not in tool_input:
        return None

    return record_research_artifact(
        artifact_type=tool_input.get("artifact_type"),
        payload=tool_input.get("payload"),
        evidence_refs=tool_input.get("evidence_refs"),
        confidence=tool_input.get("confidence"),
        notes=tool_input.get("notes"),
    )


def _normalize_artifact_type(value: Any) -> str:
    if value is None:
        return "unknown"
    return str(value).strip().lower().replace("-", "_")


def _validate_artifact_payload(
    artifact_type: str,
    payload: dict[str, Any],
) -> list[str]:
    if artifact_type == "evidence":
        return _require_any(
            payload,
            ("title", "source_url", "url", "summary", "text", "quote", "content"),
            "evidence requires source, title, summary, text, quote, or content",
        )
    if artifact_type == "entity":
        return _require_any(payload, ("name",), "entity requires name")
    if artifact_type == "relation":
        errors = _require_any(
            payload,
            ("source_entity_id", "source_entity", "source"),
            "relation requires source entity",
        )
        errors.extend(
            _require_any(
                payload,
                ("target_entity_id", "target_entity", "target"),
                "relation requires target entity",
            )
        )
        return errors
    if artifact_type == "timeline_event":
        return _require_any(
            payload,
            ("title", "event", "summary"),
            "timeline_event requires title, event, or summary",
        )
    if artifact_type == "map_feature":
        return _require_any(
            payload,
            ("geometry", "location", "lat", "latitude"),
            "map_feature requires geometry, location, or coordinates",
        )
    if artifact_type == "verification":
        return _require_any(
            payload,
            ("claim", "summary", "status", "result"),
            "verification requires claim, summary, status, or result",
        )
    if artifact_type == "report_section":
        return _require_any(
            payload,
            ("markdown", "title", "summary"),
            "report_section requires markdown, title, or summary",
        )
    return [f"unsupported artifact_type: {artifact_type}"]


def _require_any(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    message: str,
) -> list[str]:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return []
    return [message]


def _normalize_evidence_refs(value: Any) -> tuple[list[str] | None, list[str]]:
    if value is None:
        return None, []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple, set)):
        return None, ["evidence_refs must be a string or list of strings"]

    refs: list[str] = []
    for item in values:
        ref = str(item).strip()
        if not ref:
            return None, ["evidence_refs cannot contain empty values"]
        refs.append(ref)
    return refs, []


def _normalize_confidence(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None, "confidence must be numeric"
    if confidence < 0 or confidence > 1:
        return None, "confidence must be between 0 and 1"
    return confidence, None


def _artifact_warning(
    warning: str,
    artifact_type: str,
    errors: list[str],
    payload: Any,
) -> dict[str, Any]:
    return {
        "type": "artifact_warning",
        "payload": {
            "warning": warning,
            "artifact_type": artifact_type,
            "errors": errors,
            "non_fatal": True,
            "source_event_type": "record_research_artifact",
            "payload": scrub_secrets(payload),
        },
    }
