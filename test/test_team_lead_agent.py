# Copyright Kiro Crew contributors
# SPDX-License-Identifier: Apache-2.0
"""``kirocrew-team-lead`` ships as a default agent, built like ``kirocrew-research``.

Three things are worth a test. The identity the installer swaps in. The dispatch
surface it mounts on top of ``build_agent_config()``: ``kirocrew-dashboard`` and
``kirocrew-work`` are ``opt_in`` servers that function skips, so a lead that only
NAMES them can dispatch nothing -- pinned on reachability, not on names. And the
skill, whose rules each name a mechanism, so each is pinned by that mechanism's name.

There is deliberately no test here for hand-edit detection, ownership digests, a start
gate or a payload generation. The spec is overwritten on every boot exactly as
``kirocrew-research`` is, so "is this file ours?" is never asked and there is nothing
to pin.
"""

from __future__ import annotations

import importlib
import json
import pathlib

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES, TEAM_LEAD_AGENT_FILENAME
from kiro_crew.agent_materialization import managed_mcp, service_agents

_SKILL = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "team-lead"
    / "SKILL.md"
)

#: The dispatch servers this installer mounts, the ``kirocrew`` subcommand each entry
#: launches, and the module that subcommand serves (``cli.py`` imports it by name).
_DISPATCH_SERVERS = {
    "kirocrew-dashboard": ("mcp-dashboard", "kiro_crew.mcp_dashboard"),
    "kirocrew-work": ("mcp-work", "kiro_crew.mcp_work"),
}


@pytest.fixture
def installed(tmp_path, monkeypatch) -> dict:
    """The spec this installer actually writes, read back off disk."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_team_lead_agent()
    return json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))


def _reachable(spec: dict, server: str, tool: str) -> bool:
    """Whether *tool* on *server* is a tool the lead can actually call.

    Every hop kiro-cli needs: the server is CONFIGURED in ``mcpServers`` (declared
    only, it mounts nothing), its entry launches the subcommand that serves *tool*,
    that server really lists *tool*, and the server is declared in ``tools``.
    """
    entry = (spec.get("mcpServers") or {}).get(server)
    if not isinstance(entry, dict) or not entry.get("command"):
        return False
    subcommand, module = _DISPATCH_SERVERS[server]
    if subcommand not in (entry.get("args") or []):
        return False
    served = {t["name"] for t in importlib.import_module(module)._list_tools()}
    return tool in served and f"@{server}" in (spec.get("tools") or [])


class TestTheInstallerWritesADefaultAgent:
    """Mirrors what ``_install_research_agent`` is held to, on the same three fields."""

    def test_the_file_carries_this_agent_name_and_charter(self, installed):
        assert installed["name"] == "kirocrew-team-lead"
        assert installed["prompt"] == agent._TEAM_LEAD_SYSTEM_PROMPT
        assert installed["prompt"].startswith("# Kiro Crew Team Lead")
        assert installed["description"]

    def test_the_filename_is_owned_so_the_boot_sweep_covers_it(self):
        assert TEAM_LEAD_AGENT_FILENAME == "kirocrew-team-lead.json"
        assert TEAM_LEAD_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_beyond_the_dispatch_mount_it_is_the_research_shape(
        self, installed, tmp_path, monkeypatch
    ):
        """Everything the dispatch mount does not touch is exactly what
        ``build_agent_config`` produced, with the guide server dropped and three
        identity fields swapped -- the research shape."""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        baseline = service_agents._without_guide_server(agent.build_agent_config())
        mounted = {"name", "description", "prompt", "tools", "allowedTools", "mcpServers"}
        mounted |= {"permissions"}
        assert set(baseline) - {"permissions"} <= set(installed)
        for key in set(installed) - mounted:
            assert installed[key] == baseline[key], f"{key} was changed by this installer"
        for name, entry in baseline["mcpServers"].items():
            assert installed["mcpServers"][name] == entry
        assert set(installed["mcpServers"]) == set(baseline["mcpServers"]) | set(_DISPATCH_SERVERS)
        assert set(baseline["tools"]) <= set(installed["tools"])
        assert "autoApprove" not in json.dumps(installed)
        assert set(baseline["allowedTools"]) <= set(installed["allowedTools"])

    def test_our_own_prior_write_is_overwritten_without_a_backup(self, tmp_path, monkeypatch):
        """The contract an operator is told in one line: a customized copy belongs under
        another name, because a file this installer wrote is rewritten every boot — exactly
        as ``kirocrew-research`` and ``kirocrew-knowledge`` are. A file whose ``name`` is
        already ``kirocrew-team-lead`` is our own prior write, so it is overwritten in place
        and no ``.bak`` is made."""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"name": "kirocrew-team-lead", "prompt": "stale"}),
            encoding="utf-8",
        )

        service_agents._install_team_lead_agent()

        installed = json.loads(target.read_text(encoding="utf-8"))
        assert installed["name"] == "kirocrew-team-lead"
        assert installed["prompt"] == agent._TEAM_LEAD_SYSTEM_PROMPT
        assert not list(
            tmp_path.glob(f"{TEAM_LEAD_AGENT_FILENAME}.*.bak")
        ), "our own prior write should be overwritten, not backed up"

    def test_a_foreign_file_is_preserved_as_a_bak_on_first_install(self, tmp_path, monkeypatch):
        """GPT 6.1 F2. This stem is newly reserved, so a file already here may be a template
        the operator authored under this name. On first install a file that is NOT ours (its
        declared ``name`` is not ``kirocrew-team-lead``) is moved aside to a timestamped
        ``.bak`` and the managed spec is written. Fails before the fix (the old installer
        overwrote unconditionally, leaving no ``.bak``)."""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        operator_bytes = json.dumps(
            {"name": "my-own-lead", "prompt": "do not lose me", "allowedTools": []}
        )
        target.write_text(operator_bytes, encoding="utf-8")

        service_agents._install_team_lead_agent()

        # The managed spec is installed...
        assert json.loads(target.read_text(encoding="utf-8"))["name"] == "kirocrew-team-lead"
        # ...and the operator's file survives byte-for-byte as a timestamped .bak.
        baks = list(tmp_path.glob(f"{TEAM_LEAD_AGENT_FILENAME}.*.bak"))
        assert len(baks) == 1, "the operator's pre-existing file was not preserved as a .bak"
        assert baks[0].read_text(encoding="utf-8") == operator_bytes

    def test_the_migration_fires_at_most_once(self, tmp_path, monkeypatch):
        """It is a one-time migration, not ongoing tracking: after the foreign file is moved
        aside, the path holds our own spec, so the next boot overwrites it in place and makes
        no second ``.bak``."""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"name": "my-own-lead"}), encoding="utf-8")

        service_agents._install_team_lead_agent()  # moves the foreign file aside -> 1 .bak
        service_agents._install_team_lead_agent()  # our own spec now -> no new .bak

        baks = list(tmp_path.glob(f"{TEAM_LEAD_AGENT_FILENAME}.*.bak"))
        assert len(baks) == 1, "the migration backed up more than once"


class TestTheLeadCanDispatch:
    """The whole point of a team lead. ``build_agent_config`` skips every ``opt_in``
    server, so these tests fail on a spec that only names the servers, or that grants
    the verbs without configuring the server that serves them."""

    def test_session_create_and_work_ledger_record_are_reachable(self, installed):
        assert _reachable(installed, "kirocrew-dashboard", "session_create")
        assert _reachable(installed, "kirocrew-work", "work_ledger_record")

    def test_the_reachability_probe_can_see_a_dead_grant(self, installed):
        """Positive control: the probe reports a declared-but-unconfigured server, and
        a tool the server does not serve, as unreachable."""
        dead = json.loads(json.dumps(installed))
        dead["mcpServers"].pop("kirocrew-work")
        assert not _reachable(dead, "kirocrew-work", "work_ledger_record")
        assert not _reachable(installed, "kirocrew-dashboard", "no_such_tool")

    def test_each_entry_is_the_managed_opt_in_entry(self, installed):
        """``_managed_opt_in_entry`` is the one helper that carries ``type: registry``
        and the ``KIROCREW_HOME`` pin; matching it exactly is matching both."""
        for server, (subcommand, _module) in _DISPATCH_SERVERS.items():
            assert installed["mcpServers"][server] == managed_mcp._managed_opt_in_entry(subcommand)

    def test_both_entries_carry_the_registry_type_and_the_home_pin(self, tmp_path, monkeypatch):
        """The two fields that fail silently, forced on so their presence is
        observable rather than vacuously equal to an empty default."""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr(managed_mcp, "_mcp_registry_mode", lambda: True)
        monkeypatch.setattr(
            managed_mcp, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/pinned/home"}
        )
        service_agents._install_team_lead_agent()
        spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        for server in _DISPATCH_SERVERS:
            entry = spec["mcpServers"][server]
            assert entry["type"] == managed_mcp._MCP_REGISTRY_TYPE, server
            assert entry["env"]["KIROCREW_HOME"] == "/pinned/home", server

    def test_its_grants_are_the_goal_conductors_tuples_reused(self, installed):
        for ref in (*agent._CONDUCTOR_DASHBOARD_GRANTS, *agent._LEDGER_CONDUCTOR_WORK_GRANTS):
            assert ref in installed["allowedTools"], ref
        added = {
            r
            for r in installed["allowedTools"]
            if r.startswith(("@kirocrew-dashboard/", "@kirocrew-work/"))
        }
        assert added == {*agent._CONDUCTOR_DASHBOARD_GRANTS, *agent._LEDGER_CONDUCTOR_WORK_GRANTS}

    def test_the_grants_pass_through_the_governance_ceiling(self, tmp_path, monkeypatch):
        """A ceiling that withholds ``session_create`` keeps it off ``allowedTools``
        while the server stays mounted, so the call still reaches the gate."""
        from kiro_crew.agent_materialization import auto_approve

        withheld = "@kirocrew-dashboard/session_create"
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: ref != withheld)
        service_agents._install_team_lead_agent()
        spec = json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))
        assert withheld not in spec["allowedTools"]
        assert "@kirocrew-dashboard/session_read_message" in spec["allowedTools"]
        assert _reachable(spec, "kirocrew-dashboard", "session_create")

    def test_it_does_not_mount_the_panel_server(self, installed):
        assert "kirocrew-panel" not in installed["mcpServers"]
        assert not any("kirocrew-panel" in t for t in installed["tools"])

    def test_the_charter_points_at_both_skills_and_stays_short(self, installed):
        prompt = installed["prompt"]
        assert "`team-lead` skill" in prompt
        assert "`goal-conductor`" in prompt
        assert "session_create" in prompt
        assert len(prompt.splitlines()) < 40
        # Nothing copied from the conductor charter.
        for line in agent._CONDUCTOR_SYSTEM_PROMPT.splitlines():
            if len(line) > 60:
                assert line not in prompt, line


class TestTheSkillCarriesTheTeamProcedure:
    """The skill is the delta over ``goal-conductor``. Pinned on section and mechanism
    names rather than on wording, so the text can be copy-edited without a test edit."""

    @pytest.fixture
    def text(self) -> str:
        return _SKILL.read_text(encoding="utf-8")

    def test_the_skill_was_read_with_content(self, text):
        """Positive control for the absence assertions below."""
        assert len(text) > 1000
        assert "a phrase no rule in this skill contains" not in text

    def test_it_points_at_goal_conductor_as_its_procedure(self, text):
        assert "Read `goal-conductor` first" in text

    def test_it_keeps_every_section(self, text):
        for head in (
            "## 1. Echo the ask before you plan",
            "## 2. Register the work before you start it",
            "## 3. The do-it-yourself test",
            "## 4. Dispatch a conductor by default, and only one level deep",
            "## 5. Running the team itself",
        ):
            assert head in text, head

    def test_it_names_the_mechanisms_that_decide(self, text):
        for mechanism in ("resource_status", "MAX_DEPTH", "MAX_SLOTS_PER_CREATOR"):
            assert mechanism in text, mechanism

    def test_it_no_longer_calls_dispatch_a_follow_up(self, text):
        assert "planned follow-up" not in text.lower()

    def test_the_skill_describes_a_default_agent_not_a_crewmate(self, text):
        """The dashboard page verbs answer only inside a crewmate's own thread."""
        lowered = text.lower()
        for gone in ("crewmate", "dashboard_write", "dashboard_fields"):
            assert gone not in lowered, gone


# --------------------------------------------------------------------------- #
# GPT 6.1 F1: a failed team-lead rewrite must leave the ceiling HELD, not synced.
# --------------------------------------------------------------------------- #


class _RebuildRig:
    """A private agents dir with the machine-specific inputs pinned, enough to run one
    ``rebuild_agent_config``. Mirrors the dashboard-author test's rig."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

        self.agents = tmp_path / "agents"
        self.agents.mkdir()
        binary = tmp_path / "bin" / "kirocrew"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", self.agents)
        monkeypatch.setattr(agent, "_KIROCREW_BIN", str(binary))
        monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
        monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", tmp_path / "no-hooks")
        monkeypatch.setattr(
            "kiro_crew.apps.bridges._mcp_json_path", lambda: self.agents / "kirocrew.json"
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_cli.installed_kiro_cli_version",
            lambda: SPEC_PERMISSIONS_MIN_VERSION,
        )


def test_a_failed_team_lead_install_holds_the_ceiling_instead_of_syncing(
    tmp_path, monkeypatch
) -> None:
    """GPT 6.1 F1. The team-lead spec's ``allowedTools`` is re-filtered through the
    governance ceiling on every boot (it derives from ``build_agent_config``). So a
    ceiling-TIGHTENING rebuild whose team-lead rewrite failed leaves the previous list,
    with the now-revoked grants, on disk. Swallowing that at debug would let
    ``reproject_for_ceiling_change`` advance its generation memo and the next poll would
    NOT retry. The fix marks the hold so ``_held_out`` reports True, which keeps the
    ceiling from being recorded as projected and makes the next maintenance poll retry.

    Fails before the fix: the old installer swallowed the failure at debug level, so the
    rebuild reported ``held == [False]`` -- a synced ceiling over a stale grant list."""
    _RebuildRig(tmp_path, monkeypatch)

    def _boom() -> None:
        raise OSError("team-lead agents dir unwritable")

    monkeypatch.setattr(service_agents, "_install_team_lead_agent", _boom)

    # The lead is optional, so the rebuild does NOT propagate the failure (unlike the
    # dashboard-author spec, which re-raises). It completes and reports the hold.
    held: list[bool] = []
    _reported, _wrote = agent.rebuild_agent_config_reporting(_held_out=held)

    assert held == [True], (
        "a failed team-lead rewrite must mark the ceiling held so a tightened ceiling "
        "is retried, not recorded as synced over the stale grant list"
    )


def test_a_failed_research_install_holds_the_ceiling_instead_of_syncing(
    tmp_path, monkeypatch
) -> None:
    """First Principles Item 4 / GPT 6.1 F1, the sibling the team-lead hold left unfixed.

    ``_install_research_agent`` derives from ``build_agent_config()`` exactly as
    team-lead does, so its ``allowedTools`` is re-filtered through the governance ceiling
    on every boot. A ceiling-TIGHTENING rebuild whose research rewrite failed therefore
    leaves the previous list, with the now-revoked grants, on disk. The old installer
    swallowed that failure at debug, so the rebuild reported the ceiling as synced and
    the next poll did not retry. The fix folds a ``research_held`` flag into the rebuild
    hold, so ``_held_out`` reports True and the next maintenance poll retries.

    Fails before the fix: research failure was swallowed at debug, so ``held == [False]``."""
    _RebuildRig(tmp_path, monkeypatch)

    def _boom() -> None:
        raise OSError("research agents dir unwritable")

    monkeypatch.setattr(service_agents, "_install_research_agent", _boom)

    # Research is a background service agent, so the rebuild does not propagate the
    # failure; it completes and reports the hold, like team-lead above.
    held: list[bool] = []
    _reported, _wrote = agent.rebuild_agent_config_reporting(_held_out=held)

    assert held == [True], (
        "a failed research rewrite must mark the ceiling held so a tightened ceiling "
        "is retried, not recorded as synced over the stale grant list"
    )
