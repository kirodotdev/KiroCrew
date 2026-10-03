"""Contracts for the server-driven SDPM v0.10 PPTX agent."""

from __future__ import annotations

import json
from pathlib import Path

_APP_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "pptx_maker"
)

_EXPECTED_SDPM_TOOLS = {
    "@sdpm/start_presentation",
    "@sdpm/start_composing",
    "@sdpm/start_style",
    "@sdpm/start_translation",
    "@sdpm/init_deck_workspace",
    "@sdpm/check_specs",
    "@sdpm/analyze_template",
    "@sdpm/apply_style",
    "@sdpm/generate_pptx",
    "@sdpm/search_assets",
    "@sdpm/list_styles",
    "@sdpm/list_templates",
    "@sdpm/read_guides",
    "@sdpm/code_to_slide",
    "@sdpm/grid",
    "@sdpm/arch_diagram",
    "@sdpm/read_attachment",
    "@sdpm/import_attachment",
    "@sdpm/run_python",
    "@sdpm/run_style_python",
}


def _manifest() -> dict:
    return json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))


def _agent() -> dict:
    return json.loads((_APP_DIR / "agents" / "pptx-maker.json").read_text(encoding="utf-8"))


class TestSingleServerDrivenAgent:
    def test_manifest_declares_exactly_the_single_agent(self) -> None:
        assert _manifest()["agents"] == ["agents/pptx-maker.json"]
        assert _manifest()["version"] == "0.4.0"

    def test_role_text_is_not_embedded_or_loaded_from_files(self) -> None:
        agent = _agent()
        assert agent["name"] == "pptx-maker"
        assert "prompt" not in agent
        assert "resources" not in agent
        assert not (_APP_DIR / "prompts").exists()

    def test_agent_mounts_the_exact_v010_tool_contract(self) -> None:
        agent = _agent()
        mounted = {tool for tool in agent["tools"] if tool.startswith("@sdpm/")}
        assert mounted == _EXPECTED_SDPM_TOOLS
        assert set(_manifest()["permissions"]["mcpTools"]) == _EXPECTED_SDPM_TOOLS
        assert "@sdpm/hearing" not in agent["tools"]
        assert "@kirocrew-core/ask_question" in agent["tools"]

    def test_composition_delegates_to_copies_of_the_same_agent(self) -> None:
        subagent = _agent()["toolsSettings"]["subagent"]
        assert subagent == {
            "availableAgents": ["pptx-maker"],
            "trustedAgents": ["pptx-maker"],
        }
        assert "use_subagent" in _agent()["tools"]

    def test_composition_cannot_fall_back_to_crew_managed_spawns(self) -> None:
        """kirocrew-core is mounted whole, so without an exclusion the model may
        pick spawn_run over the harness's own use_subagent for composers."""
        excluded = set(_agent()["managedToolPolicy"]["exclude"])
        assert {"spawn_run", "spawn_sub_agents", "spawn_continue", "task_run"} <= excluded

    def test_mcp_environment_carries_the_deck_root_without_preapproval(self) -> None:
        agent = _agent()
        sdpm = agent["mcpServers"]["sdpm"]
        assert sdpm["args"] == [
            "run",
            "--no-sync",
            "--directory",
            "{ENGINE_MCP_DIR}",
            "python",
            "server_acp.py",
        ]
        assert sdpm["env"]["SDPM_DECK_ROOT"] == "{DECK_ROOT}"
        assert "autoApprove" not in sdpm
        assert "allowedTools" not in agent
