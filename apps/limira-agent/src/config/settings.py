# Copyright (c) 2025 MiroMind
# This source code is licensed under the Apache 2.0 License.

"""
Runtime configuration for the Limira research runner.

Only the tools used by the Limira production path are configured here:
search/scrape, Jina-backed extraction, structured artifact recording, and the
Python sandbox.
"""

import os
import sys

from dotenv import load_dotenv
from mcp import StdioServerParameters
from omegaconf import DictConfig


load_dotenv()


def _env_string(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default)


SERPER_API_KEY = _env_string("SERPER_API_KEY")
SERPER_BASE_URL = _env_string("SERPER_BASE_URL", "https://google.serper.dev")

JINA_API_KEY = _env_string("JINA_API_KEY")
JINA_BASE_URL = _env_string("JINA_BASE_URL", "https://r.jina.ai")

E2B_API_KEY = _env_string("E2B_API_KEY")

SUMMARY_LLM_API_KEY = _env_string("SUMMARY_LLM_API_KEY")
SUMMARY_LLM_BASE_URL = _env_string("SUMMARY_LLM_BASE_URL")
SUMMARY_LLM_MODEL_NAME = _env_string("SUMMARY_LLM_MODEL_NAME")


def create_mcp_server_parameters(cfg: DictConfig, agent_cfg: DictConfig):
    configs = []
    tools = set(agent_cfg.get("tools", []) or [])

    if "search_and_scrape_webpage" in tools:
        configs.append(
            {
                "name": "search_and_scrape_webpage",
                "params": StdioServerParameters(
                    command=sys.executable,
                    args=[
                        "-m",
                        "limira_tools.dev_mcp_servers.search_and_scrape_webpage",
                    ],
                    env={
                        "SERPER_API_KEY": SERPER_API_KEY,
                        "SERPER_BASE_URL": SERPER_BASE_URL,
                    },
                ),
            }
        )

    if "jina_scrape_llm_summary" in tools:
        configs.append(
            {
                "name": "jina_scrape_llm_summary",
                "params": StdioServerParameters(
                    command=sys.executable,
                    args=[
                        "-m",
                        "limira_tools.dev_mcp_servers.jina_scrape_llm_summary",
                    ],
                    env={
                        "JINA_API_KEY": JINA_API_KEY,
                        "JINA_BASE_URL": JINA_BASE_URL,
                        "SUMMARY_LLM_BASE_URL": SUMMARY_LLM_BASE_URL,
                        "SUMMARY_LLM_MODEL_NAME": SUMMARY_LLM_MODEL_NAME,
                        "SUMMARY_LLM_API_KEY": SUMMARY_LLM_API_KEY,
                    },
                ),
            }
        )

    if "limira_artifact_recorder" in tools:
        configs.append(
            {
                "name": "limira_artifact_recorder",
                "params": StdioServerParameters(
                    command=sys.executable,
                    args=[
                        "-m",
                        "limira_tools.dev_mcp_servers.limira_artifact_recorder",
                    ],
                ),
            }
        )

    if "tool-python" in tools:
        configs.append(
            {
                "name": "tool-python",
                "params": StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "limira_tools.mcp_servers.python_mcp_server"],
                    env={"E2B_API_KEY": E2B_API_KEY},
                ),
            }
        )

    blacklist = {
        (blacklist_item[0], blacklist_item[1])
        for blacklist_item in agent_cfg.get("tool_blacklist", [])
    }
    return configs, blacklist


def expose_sub_agents_as_tools(sub_agents_cfg: DictConfig):
    sub_agents_server_params = []
    for sub_agent in sub_agents_cfg.keys():
        if "agent-browsing" in sub_agent:
            sub_agents_server_params.append(
                {
                    "name": "agent-browsing",
                    "tools": [
                        {
                            "name": "search_and_browse",
                            "description": (
                                "Search and browse for a clearly scoped missing "
                                "piece of information, then return the result."
                            ),
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "subtask": {"title": "Subtask", "type": "string"}
                                },
                                "required": ["subtask"],
                                "title": "search_and_browseArguments",
                            },
                        }
                    ],
                }
            )
    return sub_agents_server_params


def get_env_info(cfg: DictConfig) -> dict:
    return {
        "llm_provider": cfg.llm.provider,
        "llm_base_url": cfg.llm.base_url,
        "llm_model_name": cfg.llm.model_name,
        "llm_temperature": cfg.llm.temperature,
        "llm_top_p": cfg.llm.top_p,
        "llm_min_p": cfg.llm.min_p,
        "llm_top_k": cfg.llm.top_k,
        "llm_max_tokens": cfg.llm.max_tokens,
        "llm_repetition_penalty": cfg.llm.repetition_penalty,
        "llm_async_client": cfg.llm.async_client,
        "keep_tool_result": cfg.agent.keep_tool_result,
        "main_agent_max_turns": cfg.agent.main_agent.max_turns,
        **(
            {
                f"sub_{sub_agent}_max_turns": cfg.agent.sub_agents[sub_agent].max_turns
                for sub_agent in cfg.agent.sub_agents
            }
            if cfg.agent.sub_agents is not None
            else {}
        ),
        "has_serper_api_key": bool(SERPER_API_KEY),
        "has_jina_api_key": bool(JINA_API_KEY),
        "has_e2b_api_key": bool(E2B_API_KEY),
        "has_summary_llm_api_key": bool(SUMMARY_LLM_API_KEY),
        "jina_base_url": JINA_BASE_URL,
        "serper_base_url": SERPER_BASE_URL,
        "summary_llm_base_url": SUMMARY_LLM_BASE_URL,
    }
