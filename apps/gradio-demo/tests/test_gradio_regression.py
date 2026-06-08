import inspect
import json
from pathlib import Path

import main as gradio_main
import prompt_patch
import pytest
from archive_writer import ResearchArchiveWriter
from limra_artifacts import record_research_artifact
from miroflow_tools.manager import ToolManager
from omegaconf import OmegaConf
from src.config.settings import create_mcp_server_parameters
from src.logging.task_logger import TaskLog


def test_gradio_fallback_entrypoints_remain_import_compatible():
    assert callable(gradio_main.gradio_run)
    assert callable(gradio_main.stop_current)
    assert callable(gradio_main.build_demo)
    assert callable(gradio_main.stream_events_optimized)

    gradio_run_signature = inspect.signature(gradio_main.gradio_run)
    assert list(gradio_run_signature.parameters) == ["query", "ui_state"]

    stream_signature = inspect.signature(gradio_main.stream_events_optimized)
    assert list(stream_signature.parameters) == [
        "task_id",
        "query",
        "_",
        "disconnect_check",
    ]


def test_task_log_save_keeps_old_task_json_contract(tmp_path):
    log_dir = tmp_path / "logs" / "api-server"
    task_log = TaskLog(
        log_dir=str(log_dir),
        task_id="old-trace-task",
        start_time="2026-06-06 12:00:00",
        input={"task_description": "query", "task_file_name": None},
    )

    saved_path = task_log.save()

    assert saved_path.startswith(str(log_dir / "task_old-trace-task_"))
    assert saved_path.endswith(".json")
    payload = json.loads((log_dir / saved_path.rsplit("/", 1)[-1]).read_text())
    assert payload["task_id"] == "old-trace-task"
    assert payload["log_dir"] == str(log_dir)


def test_archive_trace_does_not_replace_legacy_task_log_json(tmp_path):
    log_dir = tmp_path / "logs" / "api-server"
    task_log = TaskLog(
        log_dir=str(log_dir),
        task_id="legacy-task",
        start_time="2026-06-06 12:00:00",
        input={"task_description": "legacy query", "task_file_name": None},
    )
    legacy_path = Path(task_log.save())

    writer = ResearchArchiveWriter(
        tmp_path / "archives",
        clock=lambda: "2026-06-06T12:00:00+00:00",
    )
    writer.start("archive-task", "archive query", "user-a")
    writer.record_event(
        {
            "type": "message",
            "timestamp": "2026-06-06T12:00:01+00:00",
            "payload": {"event": "message", "data": {"delta": {"content": "done"}}},
        }
    )
    result = writer.complete(status="completed", report_markdown="# Report")
    archive_trace_path = result.archive_dir / "trace.json"

    assert legacy_path.exists()
    assert archive_trace_path.exists()
    assert legacy_path.parent == log_dir
    assert result.archive_dir.parent == tmp_path / "archives"
    assert archive_trace_path.parent == result.archive_dir
    assert legacy_path != archive_trace_path

    legacy_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
    archive_trace = json.loads(archive_trace_path.read_text(encoding="utf-8"))
    assert legacy_payload["task_id"] == "legacy-task"
    assert archive_trace["task_id"] == "archive-task"
    assert "events" in archive_trace
    assert "main_agent_message_history" in legacy_payload


@pytest.mark.parametrize(
    ("artifact_type", "payload", "expected_event"),
    [
        ("evidence", {"title": "Export notice"}, "evidence_collected"),
        ("entity", {"name": "Agency A"}, "entity_extracted"),
        (
            "relation",
            {"source_entity_id": "ENT-001", "target_entity_id": "ENT-002"},
            "relation_extracted",
        ),
        ("timeline_event", {"title": "Policy issued"}, "timeline_event_added"),
        ("map_feature", {"lat": 39.9, "lon": 116.4}, "map_feature_added"),
        ("verification", {"claim": "Claim A", "status": "verified"}, "verification_result"),
        (
            "report_section",
            {"markdown": "Finding references [EVID-001]"},
            "report_section_generated",
        ),
    ],
)
def test_record_research_artifact_supports_required_types(
    artifact_type, payload, expected_event
):
    event = record_research_artifact(
        artifact_type,
        payload,
        evidence_refs="EVID-001",
        confidence="0.75",
        notes="Authorization: Bearer artifactsecret123456",
    )

    assert event["type"] == expected_event
    assert event["payload"]["artifact_type"] == artifact_type
    assert event["payload"]["source_event_type"] == "record_research_artifact"
    assert event["payload"]["evidence_refs"] == ["EVID-001"]
    assert event["payload"]["confidence"] == 0.75
    assert event["payload"]["notes"] == "Authorization: [REDACTED]"


def test_record_research_artifact_invalid_payload_is_nonfatal_warning():
    event = record_research_artifact(
        "relation",
        {"source_entity_id": "ENT-001"},
        evidence_refs=["EVID-001", ""],
        confidence=2,
    )

    assert event["type"] == "artifact_warning"
    assert event["payload"]["warning"] == "invalid_artifact_payload"
    assert event["payload"]["artifact_type"] == "relation"
    assert event["payload"]["non_fatal"] is True
    assert "relation requires target entity" in event["payload"]["errors"]
    assert "evidence_refs cannot contain empty values" in event["payload"]["errors"]
    assert "confidence must be between 0 and 1" in event["payload"]["errors"]


def test_filter_message_translates_record_research_artifact_tool_call():
    event = gradio_main.filter_message(
        {
            "event": "tool_call",
            "data": {
                "tool_name": "record_research_artifact",
                "tool_input": {
                    "artifact_type": "report_section",
                    "payload": {"markdown": "Finding references [EVID-001]"},
                    "evidence_refs": ["EVID-001"],
                    "confidence": 0.9,
                    "notes": "draft",
                },
            },
        }
    )

    assert event == {
        "type": "report_section_generated",
        "payload": {
            "artifact_type": "report_section",
            "source_event_type": "record_research_artifact",
            "markdown": "Finding references [EVID-001]",
            "evidence_refs": ["EVID-001"],
            "confidence": 0.9,
            "notes": "draft",
        },
    }


def test_filter_message_ignores_record_research_artifact_result_only_tool_output():
    message = {
        "event": "tool_call",
        "data": {
            "tool_name": "record_research_artifact",
            "tool_input": {"result": "already recorded"},
        },
    }

    assert gradio_main.filter_message(message) == message


def test_artifact_adapter_prompt_and_core_boundary_contract():
    assert (
        "When the `record_research_artifact` tool is available"
        in prompt_patch.CUSTOM_IDENTITY_PROMPT
    )
    assert "evidence, entity, relation, timeline_event" in (
        prompt_patch.CUSTOM_IDENTITY_PROMPT
    )

    adapter_source = (
        Path(__file__).resolve().parents[1] / "limra_artifacts.py"
    ).read_text(encoding="utf-8")
    assert "src.core" not in adapter_source
    assert "execute_task_pipeline" not in adapter_source
    assert "orchestrator" not in adapter_source

    shared_adapter_source = (
        Path(__file__).resolve().parents[3]
        / "libs"
        / "miroflow-tools"
        / "src"
        / "miroflow_tools"
        / "limra_artifacts.py"
    ).read_text(encoding="utf-8")
    assert "src.core" not in shared_adapter_source
    assert "execute_task_pipeline" not in shared_adapter_source
    assert "orchestrator" not in shared_adapter_source


def test_demo_config_registers_limra_artifact_recorder_server():
    demo_agent_config_path = (
        Path(__file__).resolve().parents[2]
        / "miroflow-agent"
        / "conf"
        / "agent"
        / "demo.yaml"
    )
    agent_cfg = OmegaConf.load(demo_agent_config_path).main_agent
    assert "limra_artifact_recorder" in agent_cfg.tools

    server_configs, tool_blacklist = create_mcp_server_parameters(
        OmegaConf.create({}),
        OmegaConf.create(
            {
                "tools": ["limra_artifact_recorder"],
                "tool_blacklist": agent_cfg.tool_blacklist,
            }
        ),
    )

    server_names = {config["name"] for config in server_configs}
    assert "limra_artifact_recorder" in server_names
    limra_server = next(
        config for config in server_configs if config["name"] == "limra_artifact_recorder"
    )
    assert limra_server["params"].args == [
        "-m",
        "miroflow_tools.dev_mcp_servers.limra_artifact_recorder",
    ]
    assert ("limra_artifact_recorder", "record_research_artifact") not in tool_blacklist


@pytest.mark.asyncio
async def test_tool_manager_exposes_and_executes_limra_artifact_recorder():
    agent_cfg = OmegaConf.create(
        {"tools": ["limra_artifact_recorder"], "tool_blacklist": []}
    )
    server_configs, tool_blacklist = create_mcp_server_parameters(
        OmegaConf.create({}),
        agent_cfg,
    )
    manager = ToolManager(server_configs, tool_blacklist)

    definitions = await manager.get_all_tool_definitions()

    limra_definition = next(
        server for server in definitions if server["name"] == "limra_artifact_recorder"
    )
    tools = {
        tool.get("name"): tool
        for tool in limra_definition["tools"]
        if "error" not in tool
    }
    assert "record_research_artifact" in tools
    schema_properties = tools["record_research_artifact"]["schema"]["properties"]
    assert {
        "artifact_type",
        "payload",
        "evidence_refs",
        "confidence",
        "notes",
    }.issubset(schema_properties)

    valid_result = await manager.execute_tool_call(
        "limra_artifact_recorder",
        "record_research_artifact",
        {
            "artifact_type": "evidence",
            "payload": {"title": "Export notice"},
            "evidence_refs": ["EVID-001"],
            "confidence": 0.8,
            "notes": "Authorization: Bearer mcpsecret123456",
        },
    )
    assert "error" not in valid_result
    valid_event = json.loads(valid_result["result"])
    assert valid_event["type"] == "evidence_collected"
    assert valid_event["payload"]["evidence_refs"] == ["EVID-001"]
    assert valid_event["payload"]["notes"] == "Authorization: [REDACTED]"

    warning_result = await manager.execute_tool_call(
        "limra_artifact_recorder",
        "record_research_artifact",
        {
            "artifact_type": "relation",
            "payload": {"source_entity_id": "ENT-001"},
            "confidence": 2,
        },
    )
    assert "error" not in warning_result
    warning_event = json.loads(warning_result["result"])
    assert warning_event["type"] == "artifact_warning"
    assert warning_event["payload"]["non_fatal"] is True
    assert "relation requires target entity" in warning_event["payload"]["errors"]
    assert "confidence must be between 0 and 1" in warning_event["payload"]["errors"]
