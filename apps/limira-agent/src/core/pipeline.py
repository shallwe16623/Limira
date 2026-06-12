# Copyright (c) 2025 MiroMind
# This source code is licensed under the Apache 2.0 License.

"""
Task execution pipeline module.

This module provides:
- execute_task_pipeline: Main function to run a complete task from start to finish
- create_pipeline_components: Factory function to initialize all pipeline components

The pipeline orchestrates the interaction between LLM clients, tool managers,
and the orchestrator to execute complex multi-turn agent tasks.
"""

import traceback
import uuid
from typing import Any, Dict, List, Optional

from limira_tools.manager import ToolManager
from omegaconf import DictConfig, OmegaConf

from ..config.settings import (
    create_mcp_server_parameters,
    get_env_info,
)
from ..io.output_formatter import OutputFormatter
from ..llm.factory import ClientFactory
from ..logging.task_logger import (
    TaskLog,
    get_utc_plus_8_time,
)
from .orchestrator import Orchestrator
from .research_graph import (
    build_initial_research_graph,
    execute_research_graph,
    graph_bootstrap_events,
    graph_task_description,
)
from .research_langgraph import execute_langgraph_research


RESEARCH_GRAPH_EXECUTOR_LEGACY = "legacy"
RESEARCH_GRAPH_EXECUTOR_SERIAL = "serial"
RESEARCH_GRAPH_EXECUTOR_LANGGRAPH = "langgraph"
RESEARCH_GRAPH_EXECUTORS = {
    RESEARCH_GRAPH_EXECUTOR_LEGACY,
    RESEARCH_GRAPH_EXECUTOR_SERIAL,
    RESEARCH_GRAPH_EXECUTOR_LANGGRAPH,
}


async def execute_task_pipeline(
    cfg: DictConfig,
    task_id: str,
    task_description: str,
    task_file_name: str,
    main_agent_tool_manager: ToolManager,
    sub_agent_tool_managers: Dict[str, ToolManager],
    output_formatter: OutputFormatter,
    ground_truth: Optional[Any] = None,
    log_dir: str = "logs",
    stream_queue: Optional[Any] = None,
    tool_definitions: Optional[List[Dict[str, Any]]] = None,
    sub_agent_tool_definitions: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    is_final_retry: bool = False,
    research_context: Optional[Dict[str, Any]] = None,
):
    """
    Executes the full pipeline for a single task.

    Args:
        cfg: The Hydra configuration object.
        task_id: A unique identifier for this task run (used for logging).
        task_description: The description of the task for the LLM.
        task_file_name: The path to an associated file (empty string if none).
        main_agent_tool_manager: An initialized main agent ToolManager instance.
        sub_agent_tool_managers: Dictionary mapping sub-agent names to their ToolManager instances.
        output_formatter: An initialized OutputFormatter instance.
        ground_truth: The ground truth for the task (optional).
        log_dir: The directory to save the task log (default: "logs").
        stream_queue: A queue for streaming the task execution (optional).
        tool_definitions: The definitions of the tools for the main agent (optional).
        sub_agent_tool_definitions: The definitions of the tools for the sub-agents (optional).

    Returns:
        A tuple of (final_summary, final_boxed_answer, log_file_path, failure_experience_summary):
        - final_summary: A string with the final execution summary, or an error message.
        - final_boxed_answer: The extracted boxed answer from the LLM response.
        - log_file_path: The path to the saved task log file.
        - failure_experience_summary: Summary of failure experience for retry (None if successful).
    """
    # Create task log
    task_log = TaskLog(
        log_dir=log_dir,
        task_id=task_id,
        start_time=get_utc_plus_8_time(),
        input={"task_description": task_description, "task_file_name": task_file_name},
        env_info=get_env_info(cfg),
        ground_truth=ground_truth,
    )

    # Log task start
    task_log.log_step(
        "info", "Main | Task Start", f"--- Starting Task Execution: {task_id} ---"
    )

    # Set task_log for all ToolManager instances
    main_agent_tool_manager.set_task_log(task_log)
    if sub_agent_tool_managers:
        for sub_agent_tool_manager in sub_agent_tool_managers.values():
            sub_agent_tool_manager.set_task_log(task_log)

    try:
        graph_executor = _research_graph_executor(cfg)
        graph_state = build_initial_research_graph(
            task_id=task_id,
            query=task_description,
            scenario=_context_string(research_context, "scenario"),
            document_ids=_context_string_list(research_context, "document_ids"),
            upload_scope=_context_mapping(research_context, "upload_scope"),
            source_policy=_context_mapping(research_context, "source_policy"),
        )
        if stream_queue is not None:
            for event in graph_bootstrap_events(graph_state):
                await stream_queue.put(event)

        # Initialize LLM client
        random_uuid = str(uuid.uuid4())
        unique_id = f"{task_id}-{random_uuid}"
        llm_client = ClientFactory(task_id=unique_id, cfg=cfg, task_log=task_log)

        # Initialize orchestrator
        orchestrator = Orchestrator(
            main_agent_tool_manager=main_agent_tool_manager,
            sub_agent_tool_managers=sub_agent_tool_managers,
            llm_client=llm_client,
            output_formatter=output_formatter,
            cfg=cfg,
            task_log=task_log,
            stream_queue=stream_queue,
            tool_definitions=tool_definitions,
            sub_agent_tool_definitions=sub_agent_tool_definitions,
        )

        if graph_executor == RESEARCH_GRAPH_EXECUTOR_SERIAL:
            graph_result = await execute_research_graph(
                state=graph_state,
                orchestrator=orchestrator,
                original_task_description=task_description,
                task_file_name=task_file_name,
                task_id=task_id,
                is_final_retry=is_final_retry,
                stream_queue=stream_queue,
            )
            final_summary = graph_result.final_summary
            final_boxed_answer = graph_result.final_boxed_answer
            failure_experience_summary = graph_result.failure_experience_summary
        elif graph_executor == RESEARCH_GRAPH_EXECUTOR_LANGGRAPH:
            graph_result = await execute_langgraph_research(
                state=graph_state,
                orchestrator=orchestrator,
                original_task_description=task_description,
                task_file_name=task_file_name,
                task_id=task_id,
                is_final_retry=is_final_retry,
                stream_queue=stream_queue,
            )
            final_summary = graph_result.final_summary
            final_boxed_answer = graph_result.final_boxed_answer
            failure_experience_summary = graph_result.failure_experience_summary
        else:
            (
                final_summary,
                final_boxed_answer,
                failure_experience_summary,
            ) = await orchestrator.run_main_agent(
                task_description=graph_task_description(
                    graph_state,
                    task_description,
                ),
                task_file_name=task_file_name,
                task_id=task_id,
                is_final_retry=is_final_retry,
            )

        llm_client.close()

        task_log.final_boxed_answer = final_boxed_answer
        task_log.status = "success"

        # Store failure experience summary in task log if available
        if failure_experience_summary:
            task_log.trace_data["failure_experience_summary"] = (
                failure_experience_summary
            )

        log_file_path = task_log.save()
        return (
            final_summary,
            final_boxed_answer,
            log_file_path,
            failure_experience_summary,
        )

    except Exception as e:
        error_details = traceback.format_exc()
        task_log.log_step(
            "warning",
            "task_error_notification",
            f"An error occurred during task {task_id}",
        )
        task_log.log_step("error", "task_error_details", error_details)

        error_message = (
            f"Error executing task {task_id}:\n"
            f"Description: {task_description}\n"
            f"File: {task_file_name}\n"
            f"Error Type: {type(e).__name__}\n"
            f"Error Details:\n{error_details}"
        )

        task_log.status = "failed"
        task_log.error = error_details

        log_file_path = task_log.save()

        return error_message, "", log_file_path, None

    finally:
        task_log.end_time = get_utc_plus_8_time()

        # Record task summary to structured log
        task_log.log_step(
            "info",
            "task_execution_finished",
            f"Task {task_id} execution completed with status: {task_log.status}",
        )
        task_log.save()


def create_pipeline_components(cfg: DictConfig):
    """
    Creates and initializes the core components of the agent pipeline.

    Args:
        cfg: The Hydra configuration object.

    Returns:
        Tuple of (main_agent_tool_manager, sub_agent_tool_managers, output_formatter)
    """
    # Create ToolManagers for main agent and sub-agents
    main_agent_mcp_server_configs, main_agent_blacklist = create_mcp_server_parameters(
        cfg, cfg.agent.main_agent
    )
    main_agent_tool_manager = ToolManager(
        main_agent_mcp_server_configs,
        tool_blacklist=main_agent_blacklist,
    )

    # Create OutputFormatter
    output_formatter = OutputFormatter()
    sub_agent_tool_managers = {}

    # For single agent mode
    if not cfg.agent.sub_agents:
        return main_agent_tool_manager, {}, output_formatter

    for sub_agent in cfg.agent.sub_agents:
        sub_agent_mcp_server_configs, sub_agent_blacklist = (
            create_mcp_server_parameters(cfg, cfg.agent.sub_agents[sub_agent])
        )
        sub_agent_tool_manager = ToolManager(
            sub_agent_mcp_server_configs,
            tool_blacklist=sub_agent_blacklist,
        )
        sub_agent_tool_managers[sub_agent] = sub_agent_tool_manager

    return main_agent_tool_manager, sub_agent_tool_managers, output_formatter


def _research_graph_executor(cfg: DictConfig) -> str:
    explicit = OmegaConf.select(cfg, "agent.research_graph.executor", default=None)
    if explicit is not None:
        executor = str(explicit).strip().lower()
        if executor in RESEARCH_GRAPH_EXECUTORS:
            return executor
        raise ValueError(
            "invalid_research_graph_executor: "
            f"{explicit!r}; expected one of "
            f"{', '.join(sorted(RESEARCH_GRAPH_EXECUTORS))}"
        )
    return (
        RESEARCH_GRAPH_EXECUTOR_SERIAL
        if _research_graph_execution_enabled(cfg)
        else RESEARCH_GRAPH_EXECUTOR_LEGACY
    )


def _research_graph_execution_enabled(cfg: DictConfig) -> bool:
    for path in (
        "agent.research_graph.enabled",
        "agent.research_graph_execution.enabled",
    ):
        value = OmegaConf.select(cfg, path, default=False)
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes", "on"}:
                return True
            continue
        if bool(value):
            return True
    return False


def _context_mapping(
    context: Optional[Dict[str, Any]],
    key: str,
) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    value = context.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _context_string(
    context: Optional[Dict[str, Any]],
    key: str,
) -> str | None:
    if not isinstance(context, dict):
        return None
    value = context.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _context_string_list(
    context: Optional[Dict[str, Any]],
    key: str,
) -> list[str]:
    if not isinstance(context, dict):
        return []
    value = context.get(key)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
