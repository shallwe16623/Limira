from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "[REDACTED]"

ARTIFACT_EVENT_TYPES: dict[str, str] = {
    "source_candidate": "source_candidate_collected",
    "retrieved_source": "retrieved_source_collected",
    "evidence": "evidence_collected",
    "finding": "finding_collected",
    "verified_claim": "verified_claim_collected",
    "entity": "entity_extracted",
    "relation": "relation_extracted",
    "timeline_event": "timeline_event_added",
    "map_feature": "map_feature_added",
    "verification": "verification_result",
    "report_section": "report_section_generated",
}

SUPPORTED_ARTIFACT_TYPES = frozenset(ARTIFACT_EVENT_TYPES)

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

AUTHORIZATION_HEADER = re.compile(r"(?im)(\bAuthorization\s*[:=]\s*)([^\r\n;,]+)")
COOKIE_HEADER = re.compile(r"(?im)(\b(?:Set-)?Cookie\s*[:=]\s*)([^\r\n]+)")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
EVIDENCE_REF_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(EVID-(?:\d{3,}|[0-9a-fA-F]{12}))(?![A-Za-z0-9_-])"
)
EVIDENCE_REF_FULL_PATTERN = re.compile(r"EVID-(?:\d{3,}|[0-9a-fA-F]{12})")
SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+){1,2}\b"),
    re.compile(
        r"\b("
        r"API_KEY|SERPER_API_KEY|JINA_API_KEY|E2B_API_KEY|OPENAI_API_KEY|"
        r"DEEPSEEK_API_KEY|TENCENTCLOUD_SECRET_ID|TENCENTCLOUD_SECRET_KEY"
        r")\s*[:=]\s*['\"]?[^'\"\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Za-z0-9_+/=]{40,}\b"),
)


def artifact_recording_prompt_instruction() -> str:
    return """When the `record_research_artifact` tool is available, use it to record OSINT artifacts as structured data before they are summarized in prose.

Record artifacts after search/scrape source discovery or evidence collection, entity or relation extraction, timeline or map extraction, verification work, and report section drafting. Supported `artifact_type` values are: source_candidate, retrieved_source, evidence, finding, verified_claim, entity, relation, timeline_event, map_feature, verification, report_section. Search snippets are source_candidate records only; promote to evidence only after a content-bearing retrieved_source exists. Treat validation warnings from this tool as non-fatal and continue the research task."""


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

    normalized_refs, ref_errors = _normalize_evidence_refs(evidence_refs)
    normalized_confidence, confidence_error = _normalize_confidence(confidence)
    artifact_payload = _prepare_artifact_payload(
        normalized_type,
        dict(payload),
        confidence=normalized_confidence,
        evidence_refs=normalized_refs,
    )
    errors = _validate_artifact_payload(normalized_type, artifact_payload)
    errors.extend(ref_errors)
    if confidence_error:
        errors.append(confidence_error)

    if errors:
        return _artifact_warning(
            "invalid_artifact_payload",
            normalized_type,
            errors,
            artifact_payload,
        )

    artifact_payload = dict(scrub_secrets(artifact_payload))
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


def record_research_artifact_json(
    artifact_type: Any,
    payload: Any,
    evidence_refs: Any = None,
    confidence: Any = None,
    notes: Any = None,
) -> str:
    return json.dumps(
        record_research_artifact(
            artifact_type=artifact_type,
            payload=payload,
            evidence_refs=evidence_refs,
            confidence=confidence,
            notes=notes,
        ),
        ensure_ascii=False,
        sort_keys=True,
    )


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


def is_evidence_ref(value: Any) -> bool:
    return (
        isinstance(value, str)
        and EVIDENCE_REF_FULL_PATTERN.fullmatch(value.strip()) is not None
    )


def extract_evidence_refs(text: Any) -> list[str]:
    if text is None:
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for match in EVIDENCE_REF_PATTERN.finditer(str(text)):
        ref = match.group(1)
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


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


def is_sensitive_key(key: Any) -> bool:
    key_text = str(key).lower()
    return key_text in SENSITIVE_ENV_NAMES or any(
        part in key_text for part in SENSITIVE_KEY_PARTS
    )


def scrub_string(value: str) -> str:
    protected_urls: list[str] = []

    def protect_url(match: re.Match[str]) -> str:
        protected_urls.append(scrub_url(match.group(0)))
        return f"__LIMIRA_URL_{len(protected_urls) - 1}__"

    scrubbed = URL_PATTERN.sub(protect_url, value)
    scrubbed = scrub_non_url_text(scrubbed)
    for index, url in enumerate(protected_urls):
        scrubbed = scrubbed.replace(f"__LIMIRA_URL_{index}__", url)
    return scrubbed


def scrub_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return REDACTED

    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return value

    hostname = parts.hostname
    if not hostname:
        return value
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    try:
        port = parts.port
    except ValueError:
        return REDACTED
    if port is not None:
        host = f"{host}:{port}"

    query = urlencode(
        [
            (
                key,
                REDACTED
                if is_sensitive_key(key)
                else scrub_non_url_text(value, include_long_tokens=False),
            )
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, host, parts.path, query, parts.fragment))


def scrub_non_url_text(value: str, *, include_long_tokens: bool = True) -> str:
    scrubbed = AUTHORIZATION_HEADER.sub(r"\1" + REDACTED, value)
    scrubbed = COOKIE_HEADER.sub(r"\1" + REDACTED, scrubbed)
    for pattern in SECRET_PATTERNS:
        if not include_long_tokens and pattern.pattern == r"\b[A-Za-z0-9_+/=]{40,}\b":
            continue
        if pattern.pattern.startswith(r"\b("):
            scrubbed = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", scrubbed)
        elif pattern.pattern.startswith("Bearer"):
            scrubbed = pattern.sub(f"Bearer {REDACTED}", scrubbed)
        else:
            scrubbed = pattern.sub(REDACTED, scrubbed)
    return scrubbed


def _normalize_artifact_type(value: Any) -> str:
    if value is None:
        return "unknown"
    return str(value).strip().lower().replace("-", "_")


def _prepare_artifact_payload(
    artifact_type: str,
    payload: dict[str, Any],
    *,
    confidence: float | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    prepared = dict(payload)
    if confidence is not None:
        prepared.setdefault("confidence", confidence)
    if artifact_type == "retrieved_source":
        prepared.setdefault("retrieved_source_id", _stable_artifact_id("RSRC", prepared))
    elif artifact_type == "evidence":
        if evidence_refs:
            prepared.setdefault("evidence_id", evidence_refs[0])
        prepared.setdefault("evidence_id", _stable_artifact_id("EVID", prepared))
    elif artifact_type == "finding":
        prepared.setdefault("finding_id", _stable_artifact_id("FIND", prepared))
    elif artifact_type == "verified_claim":
        prepared.setdefault("claim_id", _stable_artifact_id("CLAIM", prepared))
    return prepared


def _validate_artifact_payload(
    artifact_type: str,
    payload: dict[str, Any],
) -> list[str]:
    if artifact_type == "source_candidate":
        return _require_any(
            payload,
            (
                "source",
                "title",
                "source_url",
                "url",
                "summary",
                "snippet",
                "description",
            ),
            "source_candidate requires source, title, summary, snippet, or description",
        )
    if artifact_type == "retrieved_source":
        return _validate_retrieved_source_payload(payload)
    if artifact_type == "evidence":
        return _validate_evidence_payload(payload)
    if artifact_type == "finding":
        return _validate_finding_payload(payload)
    if artifact_type == "verified_claim":
        return _validate_verified_claim_payload(payload)
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


def _validate_retrieved_source_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(
        _require_any(
            payload,
            ("source_url", "url", "document_id"),
            "retrieved_source requires source_url, url, or document_id",
        )
    )
    errors.extend(
        _require_any(
            payload,
            ("summary", "text", "quote", "content", "quote_or_summary"),
            "retrieved_source requires content-bearing text, summary, quote, or content",
        )
    )
    errors.extend(_require_any(payload, ("source_type",), "retrieved_source requires source_type"))
    errors.extend(_require_any(payload, ("retrieved_at",), "retrieved_source requires retrieved_at"))
    errors.extend(_require_any(payload, ("content_hash",), "retrieved_source requires content_hash"))
    errors.extend(_require_any(payload, ("tool_name",), "retrieved_source requires tool_name"))
    errors.extend(_require_any(payload, ("confidence",), "retrieved_source requires confidence"))
    content_state = str(payload.get("source_content_state") or "").strip()
    if content_state != "content_bearing":
        errors.append("retrieved_source requires content_bearing source_content_state")
    if str(payload.get("source_state") or "").strip() == "source_candidate":
        errors.append("source_candidate cannot be recorded as retrieved_source")
    return errors


def _validate_evidence_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(
        _require_any(
            payload,
            ("title", "source_url", "url", "summary", "text", "quote", "content", "quote_or_summary"),
            "evidence requires source, title, summary, text, quote, or content",
        )
    )
    errors.extend(_require_any(payload, ("source_type",), "evidence requires source_type"))
    errors.extend(_require_any(payload, ("retrieved_at",), "evidence requires retrieved_at"))
    errors.extend(_require_any(payload, ("content_hash",), "evidence requires content_hash"))
    evidence_id = str(payload.get("evidence_id") or "").strip()
    if not is_evidence_ref(evidence_id):
        errors.append("evidence requires a stable EVID-* evidence_id")
    if bool(payload.get("candidate")):
        errors.append("source candidates cannot be promoted to evidence")
    if str(payload.get("source_state") or "").strip() == "source_candidate":
        errors.append("source_candidate cannot be promoted to evidence")
    content_state = str(payload.get("source_content_state") or "").strip()
    if content_state != "content_bearing":
        errors.append("evidence requires content-bearing retrieved source material")
    return errors


def _validate_finding_payload(payload: dict[str, Any]) -> list[str]:
    errors = _require_any(payload, ("summary", "claim"), "finding requires summary or claim")
    evidence_refs = _evidence_refs_from_payload(payload)
    if not evidence_refs:
        errors.append("finding requires evidence_ids or evidence_refs")
    else:
        errors.extend(_invalid_evidence_ref_errors(evidence_refs, "finding"))
    return errors


def _validate_verified_claim_payload(payload: dict[str, Any]) -> list[str]:
    errors = _require_any(payload, ("claim",), "verified_claim requires claim")
    support_type = str(payload.get("support_type") or "").strip()
    if support_type not in {"supports", "contradicts", "contextual", "weak"}:
        errors.append("verified_claim requires support_type supports, contradicts, contextual, or weak")
    evidence_refs = _evidence_refs_from_payload(payload)
    if not evidence_refs:
        errors.append("verified_claim requires evidence_ids or evidence_refs")
    else:
        errors.extend(_invalid_evidence_ref_errors(evidence_refs, "verified_claim"))
    return errors


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


def _evidence_refs_from_payload(payload: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for key in ("evidence_ids", "evidence_refs"):
        value = payload.get(key)
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, (list, tuple, set)):
            continue
        for item in values:
            ref = str(item).strip()
            if ref and ref not in seen:
                refs.append(ref)
                seen.add(ref)
    return refs


def _invalid_evidence_ref_errors(refs: list[str], artifact_type: str) -> list[str]:
    return [
        f"{artifact_type} has invalid evidence id: {ref}"
        for ref in refs
        if not is_evidence_ref(ref)
    ]


def _stable_artifact_id(prefix: str, payload: dict[str, Any]) -> str:
    material = "|".join(
        str(payload.get(key) or "")
        for key in (
            "evidence_id",
            "retrieved_source_id",
            "claim_id",
            "finding_id",
            "content_hash",
            "source_url",
            "url",
            "document_id",
            "title",
            "summary",
            "quote_or_summary",
            "claim",
        )
    )
    digest = hashlib.sha256(
        material.encode("utf-8", errors="replace")
    ).hexdigest()
    return f"{prefix}-{digest[:12]}"


def _normalize_evidence_refs(value: Any) -> tuple[list[str] | None, list[str]]:
    if value is None:
        return None, []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple, set)):
        return None, ["evidence_refs must be a string or list of strings"]

    refs: list[str] = []
    seen: set[str] = set()
    for item in values:
        ref = str(item).strip()
        if not ref:
            return None, ["evidence_refs cannot contain empty values"]
        if not is_evidence_ref(ref):
            return None, [f"invalid evidence_ref: {ref}"]
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
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
