"""Deterministic verifier semantics for the Limira research graph."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .research_graph import (
    CompressedFinding,
    EVIDENCE_ID_FULL_PATTERN,
    VERIFIER_NEGATIVE_MARKERS,
    VERIFIER_POSITIVE_MARKERS,
    EvidenceItem,
    EvidenceSupportDetail,
    ResearchGraphState,
    SourceCandidate,
    VerifiedClaim,
)


BACKGROUND_MARKERS = (
    "background",
    "background only",
    "context only",
    "company profile",
    "founded",
    "headquartered",
    "incorporated",
    "overview",
    "history",
)
CURRENT_CLAIM_MARKERS = (
    "current",
    "currently",
    "as of today",
    "as of now",
    "at present",
    "latest",
    "still",
)
ENTAILMENT_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "source",
    "sources",
    "the",
    "this",
    "to",
    "under",
    "whether",
    "with",
}


def _verification_for_finding(
    finding: CompressedFinding,
    state: ResearchGraphState,
) -> dict[str, Any]:
    evidence_ids = [str(evidence_id).strip() for evidence_id in finding.evidence_ids]
    candidate_ids = {
        str(candidate.candidate_id or "").strip()
        for candidate in state.source_candidates
        if str(candidate.candidate_id or "").strip()
    }
    candidate_refs = [
        evidence_id for evidence_id in evidence_ids if evidence_id in candidate_ids
    ]
    known_evidence = {evidence.id: evidence for evidence in state.evidence}
    graph_evidence_ids = [
        evidence_id
        for evidence_id in evidence_ids
        if EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id)
    ]
    invalid_refs = [
        evidence_id
        for evidence_id in evidence_ids
        if evidence_id
        and evidence_id not in candidate_ids
        and (
            EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id) is None
            or evidence_id not in known_evidence
        )
    ]
    missing_refs = [
        evidence_id
        for evidence_id in graph_evidence_ids
        if evidence_id not in known_evidence
    ]
    invalid_or_missing_refs = _dedupe_preserve_order([*invalid_refs, *missing_refs])
    if invalid_or_missing_refs:
        return _verification_decision(
            support_type="invalid_ref",
            evidence_ids=invalid_or_missing_refs,
            rationale="Verifier found missing or malformed evidence references.",
            evidence_details=[
                EvidenceSupportDetail(
                    evidence_id=evidence_id,
                    support_type="invalid_ref",
                    rationale="Missing or malformed evidence reference.",
                )
                for evidence_id in invalid_or_missing_refs
            ],
        )
    if graph_evidence_ids:
        linked_evidence = [known_evidence[evidence_id] for evidence_id in graph_evidence_ids]
        evidence_details = [
            _evidence_support_detail_for_finding(finding, evidence)
            for evidence in linked_evidence
        ]
        counterevidence_ids = [
            detail.evidence_id
            for detail in evidence_details
            if detail.support_type == "contradictory"
        ]
        temporal_context = _verification_temporal_context(evidence_details)
        direct_evidence_ids = [
            detail.evidence_id
            for detail in evidence_details
            if detail.support_type == "direct"
        ]
        if counterevidence_ids or _linked_evidence_is_contradicted(linked_evidence):
            return _verification_decision(
                "contradicted",
                graph_evidence_ids,
                "Verifier found opposing assertions across content-bearing evidence.",
                evidence_details=evidence_details,
                counterevidence_ids=counterevidence_ids,
                temporal_context=temporal_context,
            )
        if direct_evidence_ids:
            return _verification_decision(
                "supported",
                direct_evidence_ids,
                "Verifier found direct content-bearing evidence that entails the claim.",
                evidence_details=evidence_details,
                temporal_context=temporal_context,
            )
        if any(detail.support_type == "stale" for detail in evidence_details):
            return _verification_decision(
                "weak",
                graph_evidence_ids,
                "Verifier found evidence with stale or incompatible temporal scope.",
                evidence_details=evidence_details,
                temporal_context=temporal_context,
            )
        if any(detail.support_type == "background" for detail in evidence_details):
            return _verification_decision(
                "weak",
                graph_evidence_ids,
                "Verifier found background-only evidence that does not entail the claim.",
                evidence_details=evidence_details,
                temporal_context=temporal_context,
            )
        return _verification_decision(
            "insufficient",
            graph_evidence_ids,
            "Verifier found cited evidence, but its content does not entail the claim.",
            evidence_details=evidence_details,
            temporal_context=temporal_context,
        )
    if candidate_refs or _finding_mentions_source_candidate(finding, state.source_candidates):
        return _verification_decision(
            "weak",
            [],
            "Verifier found only source-candidate support without content-bearing evidence.",
        )
    return _verification_decision(
        "insufficient",
        [],
        "Verifier found no evidence references for this claim.",
    )


def _verified_claim_has_high_confidence_support(
    claim: VerifiedClaim,
    known_evidence: dict[str, EvidenceItem],
) -> bool:
    if claim.support_type != "supported":
        return False
    valid_refs = [
        evidence_id
        for evidence_id in claim.evidence_ids
        if EVIDENCE_ID_FULL_PATTERN.fullmatch(evidence_id)
        and evidence_id in known_evidence
    ]
    if not valid_refs or claim.counterevidence_ids:
        return False
    temporal_context = str(claim.temporal_context or "").lower()
    if "stale" in temporal_context or "incompatible" in temporal_context:
        return False
    if not claim.evidence_details:
        return True
    valid_ref_set = set(valid_refs)
    direct_refs = {
        detail.evidence_id
        for detail in claim.evidence_details
        if detail.support_type == "direct"
    }
    blocking_refs = {
        detail.evidence_id
        for detail in claim.evidence_details
        if detail.support_type
        in {"contradictory", "stale", "background", "unrelated", "invalid_ref"}
    }
    return bool(direct_refs & valid_ref_set) and not bool(blocking_refs & valid_ref_set)


def _verification_decision(
    support_type: str,
    evidence_ids: list[str],
    rationale: str,
    *,
    evidence_details: list[EvidenceSupportDetail] | None = None,
    counterevidence_ids: list[str] | None = None,
    temporal_context: str | None = None,
) -> dict[str, Any]:
    return {
        "support_type": support_type,
        "evidence_ids": _dedupe_preserve_order(evidence_ids),
        "rationale": rationale,
        "evidence_details": list(evidence_details or []),
        "counterevidence_ids": _dedupe_preserve_order(counterevidence_ids or []),
        "temporal_context": temporal_context,
    }


def _evidence_support_detail_for_finding(
    finding: CompressedFinding,
    evidence: EvidenceItem,
) -> EvidenceSupportDetail:
    claim_text = str(finding.summary or "")
    evidence_text = str(evidence.quote_or_summary or "")
    temporal_context = _evidence_temporal_context(claim_text, evidence)
    if _evidence_contradicts_claim(claim_text, evidence_text):
        support_type = "contradictory"
        rationale = "Evidence polarity conflicts with the claim."
    elif _evidence_is_temporally_stale(claim_text, evidence):
        support_type = "stale"
        rationale = "Evidence date or retrieved-at timestamp is stale for the claim."
    elif _evidence_directly_supports_claim(claim_text, evidence):
        support_type = "direct"
        rationale = "Evidence excerpt directly entails the claim."
    elif _evidence_is_background_only(evidence_text):
        support_type = "background"
        rationale = "Evidence provides background context but not claim support."
    else:
        support_type = "unrelated"
        rationale = "Evidence mentions related terms but does not entail the claim."
    return EvidenceSupportDetail(
        evidence_id=evidence.id,
        support_type=support_type,
        excerpt=_bounded_text(evidence_text, 1_000),
        temporal_context=temporal_context,
        rationale=rationale,
    )


def _evidence_contradicts_claim(claim_text: str, evidence_text: str) -> bool:
    claim_polarity = _evidence_polarity(claim_text)
    evidence_polarity = _evidence_polarity(evidence_text)
    return (
        claim_polarity == "positive"
        and evidence_polarity == "negative"
    ) or (
        claim_polarity == "negative"
        and evidence_polarity == "positive"
    )


def _evidence_directly_supports_claim(
    claim_text: str,
    evidence: EvidenceItem,
) -> bool:
    normalized_claim = _normalized_text(claim_text)
    evidence_texts = [
        str(evidence.quote_or_summary or ""),
        *(str(claim or "") for claim in evidence.claims),
    ]
    claim_terms = _significant_terms(normalized_claim)
    for evidence_text in evidence_texts:
        normalized_evidence = _normalized_text(evidence_text)
        if not normalized_evidence:
            continue
        if (
            len(normalized_claim) >= 24
            and normalized_claim in normalized_evidence
        ) or (
            len(normalized_evidence) >= 24
            and normalized_evidence in normalized_claim
        ):
            return True
        evidence_terms = set(_significant_terms(normalized_evidence))
        if not claim_terms or not evidence_terms:
            continue
        overlap = set(claim_terms) & evidence_terms
        required = max(2, int(len(set(claim_terms)) * 0.55))
        if len(overlap) >= required:
            return True
    return False


def _evidence_is_background_only(evidence_text: str) -> bool:
    normalized = _normalized_text(evidence_text)
    return any(marker in normalized for marker in BACKGROUND_MARKERS)


def _evidence_is_temporally_stale(
    claim_text: str,
    evidence: EvidenceItem,
) -> bool:
    claim_years = _years(claim_text)
    evidence_years = _years(evidence.quote_or_summary)
    evidence_years.append(evidence.retrieved_at.year)
    if claim_years and max(evidence_years) < max(claim_years):
        return True
    normalized_claim = _normalized_text(claim_text)
    if any(marker in normalized_claim for marker in CURRENT_CLAIM_MARKERS):
        current_year = datetime.now(timezone.utc).year
        if max(evidence_years) < current_year - 1:
            return True
    return False


def _evidence_temporal_context(
    claim_text: str,
    evidence: EvidenceItem,
) -> str | None:
    claim_years = _years(claim_text)
    evidence_years = _years(evidence.quote_or_summary)
    if evidence.retrieved_at:
        evidence_years.append(evidence.retrieved_at.year)
    if not claim_years and not evidence_years:
        return None
    parts: list[str] = []
    if claim_years:
        parts.append(f"claim_years={','.join(str(year) for year in claim_years)}")
    if evidence_years:
        years = ",".join(str(year) for year in sorted(set(evidence_years)))
        parts.append(f"evidence_years={years}")
    if _evidence_is_temporally_stale(claim_text, evidence):
        parts.append("temporal_status=stale_or_incompatible")
    else:
        parts.append("temporal_status=compatible")
    return _bounded_text("; ".join(parts), 500)


def _verification_temporal_context(
    evidence_details: list[EvidenceSupportDetail],
) -> str | None:
    contexts = [
        detail.temporal_context
        for detail in evidence_details
        if detail.temporal_context
    ]
    if not contexts:
        return None
    return _bounded_text("; ".join(dict.fromkeys(contexts)), 500)


def _linked_evidence_is_contradicted(evidence_items: list[EvidenceItem]) -> bool:
    if len(evidence_items) < 2:
        return False
    polarities = [_evidence_polarity(item.quote_or_summary) for item in evidence_items]
    return "positive" in polarities and "negative" in polarities


def _evidence_polarity(text: str) -> str:
    normalized = _normalized_text(text)
    if any(marker in normalized for marker in VERIFIER_NEGATIVE_MARKERS):
        return "negative"
    if any(marker in normalized for marker in VERIFIER_POSITIVE_MARKERS):
        return "positive"
    return "neutral"


def _finding_mentions_source_candidate(
    finding: CompressedFinding,
    candidates: list[SourceCandidate],
) -> bool:
    haystack = str(finding.summary or "").lower()
    if not haystack:
        return False
    for candidate in candidates:
        tokens = [candidate.candidate_id, candidate.title, candidate.summary, candidate.url]
        if any(token and str(token).strip().lower() in haystack for token in tokens):
            return True
    return False


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _significant_terms(value: str) -> list[str]:
    terms = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", value)
    return [
        term
        for term in dict.fromkeys(terms)
        if term not in ENTAILMENT_STOP_WORDS
    ][:80]


def _years(value: Any) -> list[int]:
    years: list[int] = []
    for match in re.findall(r"\b(?:19|20)\d{2}\b", str(value or "")):
        year = int(match)
        if 1900 <= year <= 2100 and year not in years:
            years.append(year)
    return years


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _bounded_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."
