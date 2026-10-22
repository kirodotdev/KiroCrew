# SPDX-License-Identifier: Apache-2.0
"""``kirocrew-mate-conductor`` is the default template, renamed, minus sub-agents.

A crewmate's own chat runs on a source-defined spec whose prompt, tools and
``mcpServers`` are ``build_agent_config()``'s output, exactly as ``kirocrew.json``
carries them. It differs in ``name``, ``description`` and ``excludedTools``: the
conductor hands its work to a worker session, so the sub-agent tools are not in
its tool list, and the spawn gate refuses a spawn from it.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import MATE_CONDUCTOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_materialization import service_agents
from kiro_crew.config import paths as paths_mod


@pytest.fixture
def installed(tmp_path, monkeypatch) -> dict:
    """The spec this installer actually writes, read back off disk."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_mate_conductor_agent()
    return json.loads(
        (paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8")
    )


class TestTheInstallerWritesTheDefaultTemplateUnderItsOwnName:
    def test_the_file_carries_this_agents_name_and_description(self, installed):
        assert installed["name"] == "kirocrew-mate-conductor"
        assert installed["description"] == "The agent a crewmate's own chat runs on."

    def test_the_filename_is_owned_so_the_boot_sweep_covers_it(self):
        assert MATE_CONDUCTOR_AGENT_FILENAME == "kirocrew-mate-conductor.json"
        assert MATE_CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_beyond_name_and_description_it_is_the_default_templates_shape(
        self, installed, monkeypatch, tmp_path
    ):
        """Every other field matches ``build_agent_config()`` exactly."""
        template = agent.build_agent_config()
        for key, value in template.items():
            if key in ("name", "description", "excludedTools"):
                continue
            assert installed.get(key) == value, key

    def test_it_keeps_the_default_templates_built_in_tools(self, installed):
        """Only sub-agent spawning is taken away: the shell, the file writer and
        ``code`` are the default template's and stay mounted."""
        for tool in ("execute_bash", "fs_write", "code"):
            assert tool in installed["tools"]
            assert tool not in installed.get("excludedTools", [])
        assert "fs_read" in installed["tools"]
        assert "@kirocrew-mate" in installed["tools"]

    def test_it_still_lacks_the_spawn_tools(self, installed):
        assert "@kirocrew-core/spawn_run" in installed["excludedTools"]
        assert "@kirocrew-core/spawn_run" not in installed.get("allowedTools", [])

    def test_task_run_and_workflow_run_are_not_in_its_tool_list(self, installed):
        excluded = installed["excludedTools"]
        assert "@kirocrew-core/task_run" in excluded
        assert "@kirocrew-core/workflow_run" in excluded
        # Computer use and the browser tool stay.
        assert "@kirocrew-computer" in installed["tools"]
        assert "@kirocrew-core/browser" not in excluded


def test_the_user_servers_never_reach_the_conductor(tmp_path, monkeypatch):
    """Whatever servers the default template carries for the user, the conductor
    mounts only the gateway's own platform servers."""
    real = agent.build_agent_config

    def with_user_servers():
        config = real()
        config["mcpServers"] = {**config.get("mcpServers", {}), "builder-mcp": {"command": "b"}}
        config["tools"] = [*config["tools"], "@builder-mcp"]
        config["allowedTools"] = [*config.get("allowedTools", []), "@builder-mcp/search"]
        config["includeMcpJson"] = True
        return config

    monkeypatch.setattr(agent, "build_agent_config", with_user_servers)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_mate_conductor_agent()
    spec = json.loads(
        (paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8")
    )

    from kiro_crew.mate_worker import conductor_mcp_servers

    assert spec["includeMcpJson"] is False
    assert "builder-mcp" not in spec["mcpServers"]
    assert set(spec["mcpServers"]) <= conductor_mcp_servers()
    assert "kirocrew-mate" in spec["mcpServers"] and "kirocrew-core" in spec["mcpServers"]
    assert not any("builder-mcp" in ref for ref in [*spec["tools"], *spec["allowedTools"]])

    def test_the_sub_agent_tools_are_not_in_its_tool_list(self, installed):
        from kiro_crew.mate_worker import MATE_CONDUCTOR_EXCLUDED_TOOLS

        excluded = installed.get("excludedTools") or []
        for tool in ("spawn_run", "spawn_sub_agents", "spawn_continue", "spawn_steer"):
            assert f"@kirocrew-core/{tool}" in excluded
        assert set(MATE_CONDUCTOR_EXCLUDED_TOOLS) <= set(excluded)
        # The hand-off it does have stays mounted.
        assert not any("session_" in ref for ref in excluded)

    def test_the_prompt_is_the_same_native_stub_every_crewmate_runs_today(self, installed):
        assert installed["prompt"] == agent._NATIVE_PROMPT_STUB


def test_rebuild_agent_config_installs_it_alongside_the_default(tmp_path, monkeypatch):
    """A full ``rebuild_agent_config()`` writes the mate conductor too, not just a
    direct call to the installer -- this is the self-healing boot path a crewmate's
    turn actually depends on."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent.rebuild_agent_config(clean=True)
    assert (paths_mod.mate_agents_dir() / MATE_CONDUCTOR_AGENT_FILENAME).exists()


class TestIsKirocrewDefaultAgent:
    """``context_assembly``'s ``is_custom`` checks all defer to this predicate so
    the mate conductor is treated as the kirocrew default, not a user's custom
    agent -- see test_context.py's ``test_docs_still_injected_for_the_mate_conductor``
    for the behavioral proof."""

    def test_kirocrew_and_the_mate_conductor_are_both_the_default(self):
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert is_kirocrew_default_agent("kirocrew")
        assert is_kirocrew_default_agent("kirocrew-mate-conductor")

    def test_a_real_custom_agent_is_not(self):
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert not is_kirocrew_default_agent("code-reviewer")
        assert not is_kirocrew_default_agent("")
        assert not is_kirocrew_default_agent(None)

    def test_other_derived_specs_stay_out_of_scope(self):
        """Research/worker/team-lead are backend loops this fix does not touch;
        widening their classification is a separate decision."""
        from kiro_crew.agent_files import is_kirocrew_default_agent

        assert not is_kirocrew_default_agent("kirocrew-research")
        assert not is_kirocrew_default_agent("kirocrew-worker")
        assert not is_kirocrew_default_agent("kirocrew-team-lead")


def test_the_first_crewmates_implicit_fallback_names_the_mate_conductor():
    """``first_crewmate._ORDINARY_TEMPLATE`` is internal-only -- nothing displays
    its value, unlike ``change_card_catalog.DEFAULT_CREWMATE_TEMPLATE`` (the New
    crewmate dialog's "Built from" label and API payload), which deliberately
    stays "kirocrew": a UI label change is its own design decision, out of scope
    for step 1's pure sourcing move."""
    from kiro_crew import change_card_catalog
    from kiro_crew.agent_materialization import first_crewmate

    assert first_crewmate._ORDINARY_TEMPLATE == "kirocrew-mate-conductor"
    assert first_crewmate._default_template() == "kirocrew-mate-conductor"
    # The ``default`` binding a plain chat runs on is not a crewmate's.
    assert first_crewmate._PLAIN_TEMPLATE == "kirocrew"
    assert change_card_catalog.DEFAULT_CREWMATE_TEMPLATE == "kirocrew"


def test_the_spawn_gate_refuses_a_parent_on_the_conductor():
    from kiro_crew.subagent_parts.agent_policy import (
        _vet_parent_available_agents,
        conductor_spawn_refusal,
    )

    refusal = _vet_parent_available_agents(("kirocrew-mate-conductor", ()), "kirocrew")
    assert refusal is not None and "session_create" in refusal
    # Even with no child template resolved yet, and whatever the spec declares.
    assert _vet_parent_available_agents(("kirocrew-mate-conductor", ()), "") is not None
    assert conductor_spawn_refusal("kirocrew-mate-conductor") is not None
    # Every other parent keeps today's answer, the worker included.
    assert _vet_parent_available_agents(("kirocrew", ()), "kirocrew") is None
    assert _vet_parent_available_agents(("kirocrew-mate-worker", ()), "kirocrew") is None
    assert conductor_spawn_refusal("") is None


def test_the_member_working_protocol_hands_long_work_to_worker_sessions():
    from kiro_crew.context_assembly import member

    text = member._MEMBER_HOW_YOU_WORK_COMMON
    assert "session_create" in text and "end your turn" in text
    assert "[Worker report]" in text and "session_read_message" not in text
    assert "spawn_run" not in text


def test_the_operating_mode_block_dispatches_and_ends_instead_of_patrolling():
    from kiro_crew.context_assembly import member

    text = member.operating_mode_block("Atlas")
    assert "end your turn" in text and "[Worker report]" in text
    assert "monitor_start" not in text
