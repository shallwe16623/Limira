import importlib

import dotenv
from omegaconf import OmegaConf

import src.config.settings as settings


def test_mcp_server_parameters_accept_missing_optional_tool_env(monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    for name in [
        "SERPER_API_KEY",
        "SERPER_BASE_URL",
        "JINA_API_KEY",
        "JINA_BASE_URL",
        "SUMMARY_LLM_BASE_URL",
        "SUMMARY_LLM_MODEL_NAME",
        "SUMMARY_LLM_API_KEY",
        "E2B_API_KEY",
    ]:
        monkeypatch.delenv(name, raising=False)

    reloaded_settings = importlib.reload(settings)
    agent_cfg = OmegaConf.create(
        {
            "tools": [
                "search_and_scrape_webpage",
                "jina_scrape_llm_summary",
                "tool-python",
            ],
            "tool_blacklist": [],
        }
    )

    configs, blacklist = reloaded_settings.create_mcp_server_parameters(
        OmegaConf.create({}),
        agent_cfg,
    )

    assert blacklist == set()
    env_by_name = {config["name"]: config["params"].env for config in configs}
    assert env_by_name["search_and_scrape_webpage"] == {
        "SERPER_API_KEY": "",
        "SERPER_BASE_URL": "https://google.serper.dev",
    }
    assert env_by_name["jina_scrape_llm_summary"] == {
        "JINA_API_KEY": "",
        "JINA_BASE_URL": "https://r.jina.ai",
        "SUMMARY_LLM_BASE_URL": "",
        "SUMMARY_LLM_MODEL_NAME": "",
        "SUMMARY_LLM_API_KEY": "",
    }
    assert env_by_name["tool-python"] == {"E2B_API_KEY": ""}
