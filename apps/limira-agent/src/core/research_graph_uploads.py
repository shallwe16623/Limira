"""Upload/context parsing helpers for the Limira research graph."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from .research_graph import (
    GRAPH_CONTENT_HASH_CHARS,
    ResearchUploadScope,
    SourcePolicy,
    UploadScopeSourceRef,
    UploadedDocumentSource,
)

__all__ = [
    "_upload_source_from_payload",
    "_short_content_hash",
    "_short_content_hash_from_digest",
    "_short_optional_content_hash",
    "_bounded_graph_text",
    "_bounded_optional_text",
    "_safe_int",
    "_upload_source_chunk_id",
    "_upload_source_candidate_id",
    "_upload_source_title",
    "_parse_upload_retrieved_at",
    "_upload_context_summary",
    "_upload_context_source_payloads",
    "_upload_context_document_ids",
    "_upload_context_document_ids_in_scope",
    "_normalized_upload_document_ids",
    "_query_variants",
    "_dedupe_queries",
]

def _upload_source_from_payload(
    payload: dict[str, Any],
    index: int,
) -> UploadedDocumentSource | None:
    document_id = str(payload.get("document_id") or "").strip()
    text = str(payload.get("text") or payload.get("snippet") or "").strip()
    if not document_id or not text:
        return None
    source_content_state = str(
        payload.get("source_content_state") or "content_bearing"
    ).strip()
    retrieval_status = str(payload.get("retrieval_status") or "retrieved").strip()
    if source_content_state != "content_bearing" or retrieval_status != "retrieved":
        return None
    attached_document_id = str(payload.get("attached_document_id") or "").strip()
    content_hash = str(payload.get("content_hash") or "").strip()
    if len(content_hash) < 16:
        content_hash = _short_content_hash(text)
    else:
        content_hash = _short_content_hash_from_digest(content_hash)
    chunk_id = str(payload.get("chunk_id") or "").strip()
    if not chunk_id:
        chunk_id = _upload_source_chunk_id(document_id, attached_document_id, index)
    candidate_id = str(payload.get("candidate_id") or "").strip()
    if not candidate_id:
        candidate_id = _upload_source_candidate_id(document_id, attached_document_id, index)
    text_char_count = payload.get("text_char_count")
    try:
        text_char_count_value = int(text_char_count) if text_char_count else len(text)
    except (TypeError, ValueError):
        text_char_count_value = len(text)
    return UploadedDocumentSource(
        candidate_id=candidate_id,
        document_id=document_id,
        attached_document_id=attached_document_id or None,
        chunk_id=chunk_id,
        filename=str(payload.get("filename") or document_id).strip() or None,
        retrieved_at=_parse_upload_retrieved_at(payload.get("retrieved_at")),
        content_hash=content_hash,
        text=text,
        snippet=str(payload.get("snippet") or text).strip()[:2_000],
        text_char_count=text_char_count_value,
        text_truncated=bool(payload.get("text_truncated")),
    )


def _short_content_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[
        :GRAPH_CONTENT_HASH_CHARS
    ]


def _short_content_hash_from_digest(value: str) -> str:
    text = str(value or "").strip()
    if len(text) < 16:
        return _short_content_hash(text)
    return text[:GRAPH_CONTENT_HASH_CHARS]


def _short_optional_content_hash(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _short_content_hash_from_digest(text)


def _bounded_graph_text(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _bounded_optional_text(value: Any, max_chars: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_chars]


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return integer if integer >= 0 else None


def _upload_source_chunk_id(
    document_id: str,
    attached_document_id: str | None,
    index: int,
) -> str:
    digest = hashlib.sha256(
        f"{document_id}:{attached_document_id or ''}:{index}".encode("utf-8")
    ).hexdigest()[:12]
    return f"UPLOAD-CHUNK-{digest}"


def _upload_source_candidate_id(
    document_id: str,
    attached_document_id: str | None,
    index: int,
) -> str:
    digest = hashlib.sha256(
        f"candidate:{document_id}:{attached_document_id or ''}:{index}".encode("utf-8")
    ).hexdigest()[:12]
    return f"SRC-UPLOAD-{digest}"


def _upload_source_title(source: UploadedDocumentSource) -> str:
    filename = (source.filename or source.document_id).strip()
    return f"Uploaded document: {filename} ({source.chunk_id})"


def _parse_upload_retrieved_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _upload_context_summary(
    upload_scope: dict[str, Any] | None,
    upload_document_ids: list[str],
) -> dict[str, Any]:
    if not isinstance(upload_scope, dict):
        return {}
    scoped_document_ids = set(upload_document_ids)
    retrieved_ids = _upload_context_document_ids_in_scope(
        upload_scope.get("retrieved_document_ids"),
        scoped_document_ids,
    )
    context_only_ids = _upload_context_document_ids_in_scope(
        upload_scope.get("context_only_document_ids"),
        scoped_document_ids,
    )
    status = str(upload_scope.get("retrieval_status") or "").strip()
    if not status and upload_document_ids:
        if retrieved_ids and context_only_ids:
            status = "partial"
        elif retrieved_ids:
            status = "retrieved"
        else:
            status = "context_only"
    return {
        "retrieval_status": status,
        "retrieved_document_ids": retrieved_ids,
        "context_only_document_ids": context_only_ids,
        "source_payloads": _upload_context_source_payloads(
            upload_scope.get("source_payloads"),
            allowed_document_ids=scoped_document_ids & set(retrieved_ids),
        ),
    }


def _upload_context_source_payloads(
    value: Any,
    *,
    allowed_document_ids: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    payloads: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("document_id") or "").strip()
        text = str(item.get("text") or item.get("snippet") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        dedupe_key = (document_id, chunk_id, text)
        if (
            not document_id
            or document_id not in allowed_document_ids
            or not text
            or dedupe_key in seen
        ):
            continue
        seen.add(dedupe_key)
        payloads.append(
            {
                "candidate_id": str(item.get("candidate_id") or "").strip(),
                "document_id": document_id,
                "attached_document_id": str(
                    item.get("attached_document_id") or ""
                ).strip(),
                "filename": str(item.get("filename") or document_id).strip(),
                "source_type": str(item.get("source_type") or "").strip(),
                "source_content_state": str(
                    item.get("source_content_state") or ""
                ).strip(),
                "retrieval_status": str(item.get("retrieval_status") or "").strip(),
                "retrieved_at": str(item.get("retrieved_at") or "").strip(),
                "content_hash": str(item.get("content_hash") or "").strip(),
                "chunk_id": chunk_id,
                "snippet": str(item.get("snippet") or text).strip()[:800],
                "text": text[:4000],
                "text_char_count": item.get("text_char_count"),
                "text_truncated": bool(item.get("text_truncated")),
            }
        )
    return payloads


def _upload_context_document_ids(value: Any) -> list[str]:
    return _normalized_upload_document_ids(value)


def _upload_context_document_ids_in_scope(value: Any, allowed_ids: set[str]) -> list[str]:
    if not allowed_ids:
        return []
    return [
        document_id
        for document_id in _upload_context_document_ids(value)
        if document_id in allowed_ids
    ]


def _normalized_upload_document_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for raw_id in value[:20]:
        document_id = str(raw_id or "").strip()
        if not document_id or document_id in seen:
            continue
        seen.add(document_id)
        ids.append(document_id)
    return ids


def _query_variants(query: str) -> list[str]:
    quoted = f'"{query}"' if len(query) < 180 else query
    return _dedupe_queries([query, quoted, f"{query} source", f"{query} report"])


def _dedupe_queries(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for query in queries:
        normalized = re.sub(r"\s+", " ", str(query or "")).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped or ["research query"]
