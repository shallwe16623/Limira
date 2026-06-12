import json

from limira_tools.limira_artifacts import record_research_artifact, scrub_secrets
from limira_tools.limira_evidence import ToolEvidenceLedger
from pipeline_helpers import (
    _init_render_state,
    _render_markdown,
    _update_state_with_event,
    expand_stream_message,
)


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


def test_tool_evidence_ledger_derives_google_search_evidence():
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
    assert events[0]["type"] == "evidence_collected"
    payload = events[0]["payload"]
    assert payload["evidence_id"].startswith("EVID-")
    assert payload["source_event_type"] == "tool_evidence_ledger"
    assert payload["source_type"] == "web_search_result"
    assert payload["title"] == "DoD 1260H List"
    assert payload["url"] == "https://example.test/dod-1260h.pdf"
    assert payload["query"] == "BYD 1260H"
    assert payload["confidence"] == 0.65
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
    payload = events[0]["payload"]
    assert payload["source_type"] == "web_page_summary"
    assert payload["source_url"] == SCMP_URL
    assert payload["summary"] == "BYD was reported as added."


def test_expand_stream_message_appends_derived_evidence_after_filtered_tool_event():
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
        "evidence_collected",
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


def test_render_markdown_uses_final_summary_without_intermediate_html_cards():
    state = _init_render_state()
    for message in [
        {
            "event": "start_of_agent",
            "data": {"agent_id": "agent-main", "agent_name": "main"},
        },
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "search-1",
                "tool_name": "google_search",
                "tool_input": {"q": "US China summit"},
            },
        },
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "search-1",
                "tool_name": "google_search",
                "tool_input": {
                    "result": json.dumps(
                        {
                            "organic": [
                                {
                                    "title": "Search result that should stay out",
                                    "link": "https://example.test/result",
                                }
                            ]
                        }
                    )
                },
            },
        },
        {
            "event": "end_of_agent",
            "data": {"agent_id": "agent-main", "agent_name": "main"},
        },
        {
            "event": "start_of_agent",
            "data": {"agent_id": "agent-final", "agent_name": "Final Summary"},
        },
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "final-tool",
                "tool_name": "record_research_artifact",
                "tool_input": {"artifact_type": "evidence"},
            },
        },
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "final-text",
                "tool_name": "show_text",
                "tool_input": {"text": "# Final answer\n\nOnly the final report body."},
            },
        },
        {
            "event": "end_of_agent",
            "data": {"agent_id": "agent-final", "agent_name": "Final Summary"},
        },
    ]:
        _update_state_with_event(state, message)

    markdown = _render_markdown(state)

    assert markdown == "# Final answer\n\nOnly the final report body."
    assert "<div" not in markdown
    assert "search-card" not in markdown
    assert "tool-card" not in markdown
    assert "Search result that should stay out" not in markdown


def test_render_markdown_uses_main_final_report_when_final_summary_has_no_body():
    state = _init_render_state()
    final_report = (
        "# 智库分析报告：美国意见领袖涉华言论\n\n"
        "这是一份完整研究报告，应作为归档正文，而不是前面的检索过程。\n\n"
        "## 一、核心判断\n\n"
        "报告正文第一部分，包含足够长的综合分析。"
        + ("补充事实、证据和判断。" * 80)
        + "\n\n## 二、风险评估\n\n"
        "报告正文第二部分，继续展开分析。"
        + ("补充风险、争议和后续观察点。" * 80)
    )
    for message in [
        {
            "event": "start_of_agent",
            "data": {"agent_id": "agent-main", "agent_name": "main"},
        },
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "search-1",
                "tool_name": "google_search",
                "tool_input": {"q": "US China summit"},
            },
        },
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "search-1",
                "tool_name": "google_search",
                "tool_input": {
                    "result": json.dumps(
                        {
                            "organic": [
                                {
                                    "title": "Intermediate search result",
                                    "link": "https://example.test/result",
                                }
                            ]
                        }
                    )
                },
            },
        },
        {
            "event": "tool_call",
            "data": {
                "tool_call_id": "main-final",
                "tool_name": "show_text",
                "tool_input": {"text": final_report},
            },
        },
        {
            "event": "end_of_agent",
            "data": {"agent_id": "agent-main", "agent_name": "main"},
        },
        {
            "event": "start_of_agent",
            "data": {"agent_id": "agent-final", "agent_name": "Final Summary"},
        },
        {
            "event": "end_of_agent",
            "data": {"agent_id": "agent-final", "agent_name": "Final Summary"},
        },
    ]:
        _update_state_with_event(state, message)

    markdown = _render_markdown(state)

    assert markdown == final_report
    assert "<div" not in markdown
    assert "search-card" not in markdown
    assert "Intermediate search result" not in markdown
