import json

from limira_tools.limira_artifacts import (
    extract_evidence_refs,
    record_research_artifact,
    scrub_secrets,
)
from limira_tools.limira_evidence import ToolEvidenceLedger
from pipeline_helpers import expand_stream_message


SCMP_URL = (
    "https://www.scmp.com/news/china/diplomacy/article/3356419/"
    "us-adds-alibaba-byd-and-other-chinese-tech-champions-military-company-list"
)


def test_scrub_secrets_preserves_normal_news_urls_with_long_slugs():
    assert scrub_secrets(SCMP_URL) == SCMP_URL

    payload = {
        "url": SCMP_URL,
        "search_result": json.dumps({"title": "SCMP result", "url": SCMP_URL}),
    }

    scrubbed = scrub_secrets(payload)

    assert scrubbed["url"] == SCMP_URL
    assert SCMP_URL in scrubbed["search_result"]
    assert "[REDACTED]" not in scrubbed["search_result"]


def test_scrub_secrets_redacts_sensitive_url_query_values_without_breaking_url():
    url = f"{SCMP_URL}?api_key=secret-token-123&topic=byd"

    scrubbed = scrub_secrets(url)

    assert scrubbed.startswith(SCMP_URL)
    assert "secret-token-123" not in scrubbed
    assert "api_key=%5BREDACTED%5D" in scrubbed
    assert "topic=byd" in scrubbed


def test_record_research_artifact_preserves_evidence_source_url():
    artifact = record_research_artifact(
        "evidence",
        {
            "title": "SCMP source",
            "url": SCMP_URL,
            "summary": "Source-backed finding.",
        },
    )

    assert artifact["type"] == "evidence_collected"
    assert artifact["payload"]["url"] == SCMP_URL


def test_extract_evidence_refs_accepts_numeric_and_hash_ids_in_first_seen_order():
    refs = extract_evidence_refs(
        "Use [EVID-001], EVID-abcdef123456, [EVID-001], "
        "EVID-ABCDEF123456, EVID-abc, and EVID-abcdef1234567."
    )

    assert refs == ["EVID-001", "EVID-abcdef123456", "EVID-ABCDEF123456"]


def test_record_research_artifact_dedupes_and_validates_evidence_refs():
    artifact = record_research_artifact(
        "report_section",
        {"title": "Finding", "markdown": "Finding [EVID-001]"},
        evidence_refs=["EVID-001", "EVID-abcdef123456", "EVID-001"],
    )

    assert artifact["type"] == "report_section_generated"
    assert artifact["payload"]["evidence_refs"] == [
        "EVID-001",
        "EVID-abcdef123456",
    ]

    warning = record_research_artifact(
        "report_section",
        {"title": "Finding", "markdown": "Finding [EVID-abc]"},
        evidence_refs=["EVID-abc"],
    )

    assert warning["type"] == "artifact_warning"
    assert warning["payload"]["warning"] == "invalid_artifact_payload"
    assert warning["payload"]["errors"] == ["invalid evidence_ref: EVID-abc"]


def test_record_research_artifact_accepts_source_only_source_candidate():
    artifact = record_research_artifact(
        "source_candidate",
        {"source": "https://example.test/source-only"},
    )

    assert artifact["type"] == "source_candidate_collected"
    assert artifact["payload"]["source"] == "https://example.test/source-only"


def test_tool_evidence_ledger_derives_google_search_source_candidate():
    ledger = ToolEvidenceLedger(task_id="task-evidence")
    input_message = {
        "event": "tool_call",
        "data": {
            "tool_call_id": "call-search",
            "tool_name": "google_search",
            "tool_input": {"q": "BYD 1260H"},
        },
    }
    assert ledger.events_from_message(input_message) == []

    output_message = {
        "event": "tool_call",
        "data": {
            "tool_call_id": "call-search",
            "tool_name": "google_search",
            "tool_input": {
                "result": json.dumps(
                    {
                        "organic": [
                            {
                                "title": "DoD 1260H List",
                                "link": "https://example.test/dod-1260h.pdf",
                                "snippet": "Official list entry summary.",
                            }
                        ],
                        "searchParameters": {"q": "BYD 1260H"},
                    }
                )
            },
        },
    }

    events = ledger.events_from_message(output_message)

    assert len(events) == 1
    assert events[0]["type"] == "source_candidate_collected"
    payload = events[0]["payload"]
    assert payload["candidate_id"].startswith("SRC-")
    assert "evidence_id" not in payload
    assert payload["source_event_type"] == "tool_evidence_ledger"
    assert payload["source_type"] == "web_search_result"
    assert payload["source_state"] == "source_candidate"
    assert payload["source_content_state"] == "snippet_only"
    assert payload["candidate"] is True
    assert payload["title"] == "DoD 1260H List"
    assert payload["url"] == "https://example.test/dod-1260h.pdf"
    assert payload["query"] == "BYD 1260H"
    assert payload["confidence"] == 0.25
    assert payload["tool_name"] == "google_search"
    assert payload["retrieved_at"]
    assert len(payload["content_hash"]) == 32


def test_tool_evidence_ledger_derives_jina_summary_evidence():
    ledger = ToolEvidenceLedger(task_id="task-evidence")
    ledger.events_from_message(
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "call-jina",
                "tool_name": "scrape_and_extract_info",
                "tool_input": {
                    "url": SCMP_URL,
                    "info_to_extract": "designation status",
                },
            },
        }
    )

    events = ledger.events_from_message(
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "call-jina",
                "tool_name": "scrape_and_extract_info",
                "tool_input": {
                    "result": json.dumps(
                        {
                            "success": True,
                            "url": SCMP_URL,
                            "extracted_info": "BYD was reported as added.",
                        }
                    )
                },
            },
        }
    )

    assert len(events) == 1
    assert events[0]["type"] == "evidence_collected"
    payload = events[0]["payload"]
    assert payload["source_type"] == "web_page_summary"
    assert payload["source_state"] == "verified_evidence"
    assert payload["source_content_state"] == "content_bearing"
    assert payload["candidate"] is False
    assert payload["source_url"] == SCMP_URL
    assert payload["summary"] == "BYD was reported as added."
    assert payload["tool_name"] == "scrape_and_extract_info"
    assert payload["retrieved_at"]
    assert len(payload["content_hash"]) == 32


def test_expand_stream_message_appends_derived_source_after_filtered_tool_event():
    ledger = ToolEvidenceLedger(task_id="task-evidence")
    expand_stream_message(
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "call-search",
                "tool_name": "google_search",
                "tool_input": {"q": "BYD 1260H"},
            },
        },
        evidence_ledger=ledger,
    )
    result_message = {
        "event": "tool_call",
        "data": {
            "tool_call_id": "call-search",
            "tool_name": "google_search",
            "tool_input": {
                "result": json.dumps(
                    {
                        "organic": [
                            {
                                "title": "DoD 1260H List",
                                "link": "https://example.test/dod-1260h.pdf",
                                "snippet": "Official list entry summary.",
                            }
                        ],
                    }
                )
            },
        },
    }

    expanded = expand_stream_message(result_message, evidence_ledger=ledger)

    assert [event.get("event") or event.get("type") for event in expanded] == [
        "tool_call",
        "source_candidate_collected",
    ]
    assert expanded[0]["data"]["tool_input"]["result"] == json.dumps(
        {
            "organic": [
                {
                    "title": "DoD 1260H List",
                    "link": "https://example.test/dod-1260h.pdf",
                }
            ]
        },
        ensure_ascii=False,
    )
    assert expanded[1]["payload"]["source_event_type"] == "tool_evidence_ledger"
    assert expanded[1]["payload"]["source_state"] == "source_candidate"
