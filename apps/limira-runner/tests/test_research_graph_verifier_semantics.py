import pytest

from src.core.research_graph import (
    CompressedFinding,
    EvidenceItem,
    EvidenceSupportDetail,
    ResearchGraphExecutionContext,
    ResearchGraphNodeOutput,
    ResearchPhase,
    VerifiedClaim,
    VerifierNode,
    WriterNode,
    build_initial_research_graph,
)


class _FakeOrchestrator:
    async def run_main_agent(self, **_kwargs):
        raise AssertionError("verifier semantic tests must stay deterministic")


def _context(task_id="task-verifier-semantics"):
    return ResearchGraphExecutionContext(
        orchestrator=_FakeOrchestrator(),
        original_task_description="Verify claim semantics",
        task_id=task_id,
    )


def _state_with_finding(
    *,
    claim: str,
    evidence: list[EvidenceItem],
    evidence_ids: list[str],
):
    state = build_initial_research_graph(
        task_id="task-verifier-semantics",
        query="Verify Entity A program X status",
        max_units=1,
    )
    return state.model_copy(
        update={
            "phase": ResearchPhase.COMPRESS,
            "evidence": evidence,
            "findings": [
                CompressedFinding(
                    id="finding-verifier-semantics",
                    research_unit_id=state.plan.research_units[0].id,
                    summary=claim,
                    evidence_ids=evidence_ids,
                    confidence=0.86,
                )
            ],
        }
    )


def _evidence(evidence_id: str, text: str, *, retrieved_at=None) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        retrieved_source_id=f"RSRC-{evidence_id.removeprefix('EVID-')}",
        title=f"Evidence {evidence_id}",
        source_type="web",
        retrieved_at=retrieved_at or "2026-06-06T12:00:00+00:00",
        content_hash=(evidence_id.replace("-", "").lower() + "0" * 32)[:32],
        quote_or_summary=text,
        claims=[text],
        confidence=0.8,
        tool_name="test_retriever",
    )


async def _verify(state):
    output = await VerifierNode().run(
        state,
        _context(state.task_id),
        ResearchGraphNodeOutput(state=state),
    )
    return output.state.verified_claims[0], output


@pytest.mark.asyncio
async def test_verifier_payload_includes_evidence_detail_and_temporal_context():
    state = _state_with_finding(
        claim="Entity A is listed under program X in 2026.",
        evidence=[
            _evidence(
                "EVID-001",
                "Entity A is listed under program X in 2026.",
            )
        ],
        evidence_ids=["EVID-001"],
    )

    claim, output = await _verify(state)

    assert claim.support_type == "supported"
    assert claim.evidence_details[0].support_type == "direct"
    assert "Entity A is listed" in claim.evidence_details[0].excerpt
    assert "temporal_status=compatible" in (claim.temporal_context or "")
    payload = output.artifact_events[0]["payload"]
    assert payload["evidence_details"][0]["support_type"] == "direct"
    assert payload["temporal_context"] == claim.temporal_context
    assert output.executor_state["verified_claims"][0]["evidence_details"][0][
        "support_type"
    ] == "direct"


@pytest.mark.asyncio
async def test_verifier_does_not_support_same_entity_background_evidence():
    state = _state_with_finding(
        claim="Entity A is listed under program X.",
        evidence=[
            _evidence(
                "EVID-001",
                "Company profile background: Entity A was founded in 2020 and is "
                "headquartered in Shenzhen.",
            )
        ],
        evidence_ids=["EVID-001"],
    )

    claim, _output = await _verify(state)

    assert claim.support_type == "weak"
    assert claim.evidence_details[0].support_type == "background"
    assert "does not entail" in claim.rationale


@pytest.mark.asyncio
async def test_verifier_does_not_support_same_entity_status_mismatch():
    state = _state_with_finding(
        claim="Entity A is listed under program X.",
        evidence=[
            _evidence(
                "EVID-001",
                "Entity A applied for program X and remains under review.",
            )
        ],
        evidence_ids=["EVID-001"],
    )

    claim, verify_output = await _verify(state)
    write_output = await WriterNode().run(
        verify_output.state,
        _context(state.task_id),
        verify_output,
    )

    assert claim.support_type in {"weak", "insufficient"}
    assert {detail.support_type for detail in claim.evidence_details} == {"unrelated"}
    assert "Entity A is listed" not in write_output.final_summary.split(
        "## Evidence Table",
        1,
    )[0]
    assert write_output.final_boxed_answer == (
        "The available evidence is insufficient to provide a settled answer."
    )
    assert write_output.artifact_events[-1]["payload"]["evidence_refs"] == []


@pytest.mark.asyncio
async def test_verifier_downgrades_stale_current_claim_evidence():
    state = _state_with_finding(
        claim="Entity A is currently listed under program X in 2026.",
        evidence=[
            _evidence(
                "EVID-001",
                "In 2020, Entity A was listed under program X.",
                retrieved_at="2020-01-01T00:00:00+00:00",
            )
        ],
        evidence_ids=["EVID-001"],
    )

    claim, _output = await _verify(state)

    assert claim.support_type == "weak"
    assert claim.evidence_details[0].support_type == "stale"
    assert "temporal_status=stale_or_incompatible" in (claim.temporal_context or "")


@pytest.mark.asyncio
async def test_verifier_represents_counterevidence_as_contradicted():
    state = _state_with_finding(
        claim="Entity A is listed under program X.",
        evidence=[
            _evidence(
                "EVID-001",
                "Entity A is not listed under program X.",
            )
        ],
        evidence_ids=["EVID-001"],
    )

    claim, _output = await _verify(state)

    assert claim.support_type == "contradicted"
    assert claim.counterevidence_ids == ["EVID-001"]
    assert claim.evidence_details[0].support_type == "contradictory"


@pytest.mark.asyncio
async def test_writer_excludes_stale_supported_claim_from_high_confidence_answer():
    state = build_initial_research_graph(
        task_id="task-writer-verifier-gating",
        query="Verify Entity A program X status",
        max_units=1,
    )
    evidence = [
        _evidence(
            "EVID-001",
            "In 2020, Entity A was listed under program X.",
            retrieved_at="2020-01-01T00:00:00+00:00",
        )
    ]
    stale_claim = VerifiedClaim(
        id="claim-stale-supported",
        claim="Entity A is currently listed under program X in 2026.",
        support_type="supported",
        evidence_ids=["EVID-001"],
        evidence_details=[
            EvidenceSupportDetail(
                evidence_id="EVID-001",
                support_type="stale",
                excerpt=evidence[0].quote_or_summary,
                temporal_context="temporal_status=stale_or_incompatible",
                rationale="Evidence is stale for the current claim.",
            )
        ],
        temporal_context="temporal_status=stale_or_incompatible",
        rationale="Evidence is stale for the current claim.",
        confidence=0.9,
    )
    state = state.model_copy(
        update={
            "phase": ResearchPhase.VERIFY,
            "evidence": evidence,
            "verified_claims": [stale_claim],
        }
    )

    output = await WriterNode().run(
        state,
        _context(state.task_id),
        ResearchGraphNodeOutput(state=state),
    )

    assert output.final_boxed_answer == (
        "The available evidence is insufficient to provide a settled answer."
    )
    assert "Entity A is currently listed" not in output.final_summary.split(
        "## Evidence Table",
        1,
    )[0]
    assert "Entity A is currently listed" in output.final_summary
    assert output.artifact_events[-1]["payload"]["evidence_refs"] == []
