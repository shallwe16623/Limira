# Copyright (c) 2025 MiroMind
# This source code is licensed under the Apache 2.0 License.

"""LangGraph research executor entrypoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .research_graph import (
    ResearchGraphExecutionContext,
    ResearchGraphExecutionResult,
    ResearchGraphNode,
    ResearchGraphNodeOutput,
    ResearchGraphState,
    ResearchPhase,
    _emit_graph_artifact_events,
    _emit_graph_checkpoint,
    _emit_graph_error,
    _emit_graph_phase,
    _validate_graph_final_outputs,
    default_research_graph_nodes,
)


LANGGRAPH_EXECUTOR_NAME = "langgraph"


class LangGraphRuntimeState(TypedDict, total=False):
    graph_state: ResearchGraphState
    previous_output: ResearchGraphNodeOutput
    final_summary: str
    final_boxed_answer: str
    failure_experience_summary: Any


async def execute_langgraph_research(
    *,
    state: ResearchGraphState,
    orchestrator: Any,
    original_task_description: str,
    task_file_name: str,
    task_id: str,
    is_final_retry: bool,
    stream_queue: Any | None,
) -> ResearchGraphExecutionResult:
    """Run the LangGraph-backed executor using the existing graph contract."""

    context = ResearchGraphExecutionContext(
        orchestrator=orchestrator,
        original_task_description=original_task_description,
        task_file_name=task_file_name,
        task_id=task_id,
        is_final_retry=is_final_retry,
    )
    graph = build_langgraph_research_graph(context=context, stream_queue=stream_queue)
    runtime = await graph.ainvoke(
        {
            "graph_state": state,
            "previous_output": ResearchGraphNodeOutput(state=state),
        }
    )
    final_state = runtime["graph_state"]
    return ResearchGraphExecutionResult(
        state=final_state,
        final_summary=runtime["final_summary"],
        final_boxed_answer=runtime["final_boxed_answer"],
        failure_experience_summary=runtime.get("failure_experience_summary"),
    )


def build_langgraph_research_graph(
    *,
    context: ResearchGraphExecutionContext,
    stream_queue: Any | None,
):
    """Build the concrete LangGraph StateGraph for the research skeleton."""

    graph = StateGraph(LangGraphRuntimeState)
    node_names: list[str] = []
    for node in default_research_graph_nodes():
        node_name = node.phase.value
        node_names.append(node_name)
        graph.add_node(
            node_name,
            _langgraph_node_runner(
                node=node,
                context=context,
                stream_queue=stream_queue,
            ),
        )
    graph.add_node(
        "complete",
        _langgraph_complete_runner(stream_queue=stream_queue),
    )

    previous = START
    for node_name in node_names:
        graph.add_edge(previous, node_name)
        previous = node_name
    graph.add_edge(previous, "complete")
    graph.add_edge("complete", END)
    return graph.compile(name="limira-langgraph-research")


def _langgraph_node_runner(
    *,
    node: ResearchGraphNode,
    context: ResearchGraphExecutionContext,
    stream_queue: Any | None,
) -> Callable[[LangGraphRuntimeState], Any]:
    async def run(graph_runtime: LangGraphRuntimeState) -> LangGraphRuntimeState:
        previous = graph_runtime.get("previous_output")
        graph_state = _runtime_graph_state(graph_runtime)
        active_state = graph_state.model_copy(update={"phase": node.phase})
        await _emit_graph_phase(stream_queue, active_state, node.phase)
        try:
            output = await node.run(active_state, context, previous)
        except Exception as exc:
            await _emit_graph_error(stream_queue, active_state, exc)
            raise
        if output.state.phase != node.phase:
            output = output.model_copy(
                update={
                    "state": output.state.model_copy(update={"phase": node.phase})
                }
            )
        await _emit_graph_artifact_events(stream_queue, output.artifact_events)
        await _emit_graph_checkpoint(
            stream_queue,
            output.state,
            node.phase,
            output,
            research_graph_executor=LANGGRAPH_EXECUTOR_NAME,
        )
        return {
            "graph_state": output.state,
            "previous_output": output,
            "failure_experience_summary": output.failure_experience_summary,
        }

    return run


def _langgraph_complete_runner(
    *,
    stream_queue: Any | None,
) -> Callable[[LangGraphRuntimeState], Any]:
    async def run(graph_runtime: LangGraphRuntimeState) -> LangGraphRuntimeState:
        previous = graph_runtime.get("previous_output")
        if previous is None:
            raise ValueError("research_graph_final_output_required")
        final_summary, final_boxed_answer = _validate_graph_final_outputs(
            previous.final_summary,
            previous.final_boxed_answer,
        )
        complete_state = previous.state.model_copy(
            update={"phase": ResearchPhase.COMPLETE}
        )
        complete_output = ResearchGraphNodeOutput(
            state=complete_state,
            executor_state={
                "node": "LangGraphComplete",
                "verified_claim_count": len(complete_state.verified_claims),
                "evidence_count": len(complete_state.evidence),
            },
            final_summary=final_summary,
            final_boxed_answer=final_boxed_answer,
            failure_experience_summary=previous.failure_experience_summary,
        )
        await _emit_graph_phase(stream_queue, complete_state, ResearchPhase.COMPLETE)
        await _emit_final_report_message(stream_queue, complete_state, final_summary)
        await _emit_graph_checkpoint(
            stream_queue,
            complete_state,
            ResearchPhase.COMPLETE,
            complete_output,
            status="completed",
            research_graph_executor=LANGGRAPH_EXECUTOR_NAME,
        )
        return {
            "graph_state": complete_state,
            "previous_output": complete_output,
            "final_summary": final_summary,
            "final_boxed_answer": final_boxed_answer,
            "failure_experience_summary": previous.failure_experience_summary,
        }

    return run


async def _emit_final_report_message(
    stream_queue: Any | None,
    state: ResearchGraphState,
    final_summary: str,
) -> None:
    if stream_queue is None:
        return
    await stream_queue.put(
        {
            "event": "message",
            "data": {
                "task_id": state.task_id,
                "delta": {"content": final_summary},
                "source_event_type": "research_langgraph",
            },
        }
    )


def _runtime_graph_state(runtime: LangGraphRuntimeState) -> ResearchGraphState:
    graph_state = runtime.get("graph_state")
    if not isinstance(graph_state, ResearchGraphState):
        raise ValueError("research_langgraph_state_required")
    return graph_state
