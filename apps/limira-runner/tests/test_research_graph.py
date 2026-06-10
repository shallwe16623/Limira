import pytest
from omegaconf import OmegaConf

from src.core import pipeline as pipeline_module
from src.core.research_graph import (
    ResearchPhase,
    build_initial_research_graph,
    evidence_id_for_source,
    graph_bootstrap_events,
    graph_task_description,
)


class _CaptureQueue:
    def __init__(self):
        self.items = []

    async def put(self, item):
        self.items.append(item)


class _FakeToolManager:
    def __init__(self):
        self.task_log = None

    def set_task_log(self, task_log):
        self.task_log = task_log


class _FakeClientFactory:
    def __init__(self, **_kwargs):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeOrchestrator:
    task_descriptions = []

    def __init__(self, *, stream_queue=None, **_kwargs):
        self.stream_queue = stream_queue

    async def run_main_agent(self, **kwargs):
        self.__class__.task_descriptions.append(kwargs["task_description"])
        await self.stream_queue.put(
            {"event": "message", "data": {"delta": {"content": "legacy executor"}}}
        )
        return "summary", "final", None


def test_initial_research_graph_creates_bounded_scope_and_plan():
    state = build_initial_research_graph(
        task_id="task-graph",
        query="  Track BYD Section 1260H list status.  ",
        scenario="sanctions_export_controls",
        max_units=3,
    )

    assert state.phase == ResearchPhase.PLAN
    assert state.brief.original_query == "Track BYD Section 1260H list status."
    assert "sanctions_export_controls" in state.brief.scope
    assert len(state.plan.research_units) == 3
    assert state.plan.research_units[0].id.startswith("unit-1-")
    assert state.plan.research_units[0].search_queries
    assert "evidence" in state.plan.expected_artifacts
    assert "Cross-check" in state.plan.verification_strategy


def test_direct_answer_research_graph_uses_lightweight_source_targets():
    state = build_initial_research_graph(
        task_id="task-direct",
        query="请问比亚迪在1260H名单上吗",
    )

    assert len(state.plan.research_units) == 2
    assert state.plan.research_units[0].id == "unit-1-direct-answer"
    assert [unit.source_policy.min_sources for unit in state.plan.research_units] == [
        1,
        1,
    ]
    assert [unit.max_sources for unit in state.plan.research_units] == [2, 2]
    assert state.plan.expected_artifacts == [
        "evidence",
        "verification_result",
        "report_section",
    ]
    assert "direct answer" in state.plan.verification_strategy
    assert any("Stop searching" in item for item in state.brief.constraints)

    task_description = graph_task_description(
        state,
        "请问比亚迪在1260H名单上吗",
    )

    assert "unit-1-direct-answer" in task_description
    assert "Source target: at least 1, max 2 sources" in task_description
    assert "Keep the final answer concise" in task_description


def test_research_graph_bootstrap_events_are_serializable_and_ordered():
    state = build_initial_research_graph(
        task_id="task-graph",
        query="Verify a company designation with primary sources",
    )

    events = graph_bootstrap_events(state)

    assert [event["event"] for event in events] == [
        "research_brief_created",
        "research_plan_created",
    ]
    assert events[0]["data"]["phase"] == "scope"
    assert events[0]["data"]["brief"]["original_query"] == (
        "Verify a company designation with primary sources"
    )
    assert events[1]["data"]["phase"] == "plan"
    assert events[1]["data"]["plan"]["research_units"]


def test_graph_task_description_includes_plan_for_compatibility_executor():
    state = build_initial_research_graph(
        task_id="task-graph",
        query="Verify a company designation with primary sources",
    )

    task_description = graph_task_description(
        state,
        "Verify a company designation with primary sources",
    )

    assert task_description.startswith(
        "Verify a company designation with primary sources"
    )
    assert "## Limira Research Workflow" in task_description
    assert "### Research Units" in task_description
    assert "unit-1-background" in task_description
    assert "### Verification Strategy" in task_description
    assert "### Report Contract" in task_description


def test_evidence_id_for_source_is_stable_per_task_source_and_index():
    first = evidence_id_for_source(task_id="task-a", source="https://example.test", index=0)
    second = evidence_id_for_source(task_id="task-a", source="https://example.test", index=0)
    different_index = evidence_id_for_source(
        task_id="task-a",
        source="https://example.test",
        index=1,
    )

    assert first == second
    assert first.startswith("EVID-")
    assert first != different_index


@pytest.mark.asyncio
async def test_pipeline_emits_research_graph_bootstrap_before_legacy_executor(
    tmp_path, monkeypatch
):
    _FakeOrchestrator.task_descriptions = []
    monkeypatch.setattr(pipeline_module, "ClientFactory", _FakeClientFactory)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _FakeOrchestrator)

    cfg = OmegaConf.create(
        {
            "llm": {
                "provider": "openai-compatible",
                "base_url": "https://llm.test",
                "model_name": "test-model",
                "temperature": 0,
                "top_p": 1,
                "min_p": 0,
                "top_k": 0,
                "max_tokens": 4096,
                "repetition_penalty": 1,
                "async_client": False,
            },
            "agent": {
                "keep_tool_result": True,
                "main_agent": {"max_turns": 1},
                "sub_agents": None,
            },
        }
    )
    stream_queue = _CaptureQueue()

    result = await pipeline_module.execute_task_pipeline(
        cfg=cfg,
        task_id="task-pipeline-graph",
        task_description="Verify a company designation with primary sources",
        task_file_name="",
        main_agent_tool_manager=_FakeToolManager(),
        sub_agent_tool_managers={},
        output_formatter=object(),
        log_dir=str(tmp_path),
        stream_queue=stream_queue,
    )

    assert result[0] == "summary"
    assert [item["event"] for item in stream_queue.items[:3]] == [
        "research_brief_created",
        "research_plan_created",
        "message",
    ]
    assert (
        stream_queue.items[0]["data"]["brief"]["original_query"]
        == "Verify a company designation with primary sources"
    )
    assert stream_queue.items[1]["data"]["plan"]["research_units"]
    assert len(_FakeOrchestrator.task_descriptions) == 1
    assert "## Limira Research Workflow" in _FakeOrchestrator.task_descriptions[0]
    assert "### Research Units" in _FakeOrchestrator.task_descriptions[0]
