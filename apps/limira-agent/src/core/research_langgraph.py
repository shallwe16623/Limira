# Copyright (c) 2025 MiroMind
# This source code is licensed under the Apache 2.0 License.

"""LangGraph research executor entrypoint.

Round 0 wires the product routing seam. The full LangGraph graph and dependency
integration lands in the executor implementation round; until then this module
must fail clearly instead of falling back to the serial or legacy executor.
"""

from typing import Any

from .research_graph import ResearchGraphExecutionResult, ResearchGraphState


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
    raise RuntimeError(
        "langgraph_executor_not_implemented: "
        "install/declare the LangGraph dependency and implement "
        "apps.limira-agent.src.core.research_langgraph before using "
        "agent.research_graph.executor=langgraph"
    )
