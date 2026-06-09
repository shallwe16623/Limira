import json

from limira_tools.limira_artifacts import record_research_artifact, scrub_secrets


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

