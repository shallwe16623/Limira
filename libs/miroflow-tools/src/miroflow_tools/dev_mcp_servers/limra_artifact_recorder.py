from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from miroflow_tools.limra_artifacts import record_research_artifact_json


mcp = FastMCP("limra_artifact_recorder")


@mcp.tool()
async def record_research_artifact(
    artifact_type: str,
    payload: dict[str, Any],
    evidence_refs: list[str] | None = None,
    confidence: float | None = None,
    notes: str | None = None,
) -> str:
    """
    Record a structured limra OSINT artifact without failing the research task.

    Args:
        artifact_type: One of evidence, entity, relation, timeline_event, map_feature, verification, or report_section.
        payload: Artifact-specific JSON object.
        evidence_refs: Optional evidence IDs referenced by this artifact.
        confidence: Optional confidence score from 0 to 1.
        notes: Optional extraction notes.

    Returns:
        Compact JSON event. Valid records return a typed limra artifact event; invalid records return a non-fatal artifact_warning event.
    """
    return record_research_artifact_json(
        artifact_type=artifact_type,
        payload=payload,
        evidence_refs=evidence_refs,
        confidence=confidence,
        notes=notes,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
