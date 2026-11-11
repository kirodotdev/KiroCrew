"""The job-tool declaration: what each shipped agent never loads, and what it costs.

``agent_sdk.tool_search.JOB_TOOLS`` is the one place that names an MCP tool to
keep out of Tool Search deferral for an agent whose spec grants the loader. These
tests hold it to three things: every row names real tools of a shipped agent, the
set a shipped spec really gets is exactly its row, and each row fits the schema
budget it states. The wire half (the handshake that sends the list) is in
``test_acp_tool_search_wire.py``.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_sdk.tool_search import (
    JOB_TOOLS,
    job_tool_ids,
    kas_never_defer_ids,
    spec_grants_tool_search,
)
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mcp_discovery_managed import _MANAGED_SERVER_TOOL_MODULES

_SHIPPED_AGENTS = {name[: -len(".json")] for name in OWNED_KIRO_AGENT_FILES}


def _served_tools(server: str) -> dict[str, dict[str, Any]]:
    """Name -> descriptor for every tool *server* answers ``tools/list`` with."""
    module = importlib.import_module(_MANAGED_SERVER_TOOL_MODULES[server])
    return {tool["name"]: tool for tool in module._list_tools()}


def _schema_tokens(tool_id: str) -> int:
    """The budget's unit: the bytes of one tool's wire descriptor over 3.25.

    That is the ratio the engine's own context breakdown reports for these
    schemas (3,521 tokens for the 11,419 bytes of ``spawn_run`` and
    ``spawn_sub_agents``), so a budget reads in the tokens an operator sees.
    """
    server, _, name = tool_id.partition("::")
    tool = _served_tools(server)[name]
    wire = {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "inputSchema": tool.get("inputSchema", {}),
    }
    return len(json.dumps(wire, ensure_ascii=False).encode("utf-8")) * 4 // 13


# ── the declaration is data about shipped agents and real tools ──


def test_every_row_is_a_shipped_agent():
    assert set(JOB_TOOLS) <= _SHIPPED_AGENTS


@pytest.mark.parametrize("agent_name", sorted(JOB_TOOLS))
def test_every_declared_tool_is_served_by_its_server(agent_name):
    for tool_id in JOB_TOOLS[agent_name].ids:
        server, sep, name = tool_id.partition("::")
        assert sep and name, tool_id
        assert name in _served_tools(server), f"{tool_id} is not a tool {server} serves"


@pytest.mark.parametrize("agent_name", sorted(JOB_TOOLS))
def test_a_row_names_each_tool_once(agent_name):
    ids = JOB_TOOLS[agent_name].ids
    assert len(set(ids)) == len(ids)


# ── the budget ──


@pytest.mark.parametrize("agent_name", sorted(JOB_TOOLS))
def test_each_row_fits_its_budget(agent_name):
    """A row's tools ride in every request of that agent, so a tool may join one
    (or a description may grow) only while the row still fits what it states."""
    row = JOB_TOOLS[agent_name]
    cost = sum(_schema_tokens(tool_id) for tool_id in row.ids)
    assert cost <= row.budget_tokens, (
        f"{agent_name}: its job tools cost {cost} schema tokens per request, "
        f"over the {row.budget_tokens} its row states"
    )


def test_no_budget_is_wide_enough_to_hold_a_whole_server():
    """The budget is what stops a row from becoming "defer nothing"."""
    smallest_server = min(
        sum(_schema_tokens(f"{server}::{name}") for name in _served_tools(server))
        for server in ("kirocrew-core", "kirocrew-dashboard")
    )
    assert max(row.budget_tokens for row in JOB_TOOLS.values()) < smallest_server


# ── the reader ──

_ALL_SERVERS = ["tool_search", "@kirocrew-core", "@kirocrew-dashboard", "@kirocrew-work"]


class TestJobToolIds:
    @pytest.mark.parametrize("agent_name", sorted(JOB_TOOLS))
    def test_a_spec_that_mounts_the_servers_gets_exactly_its_row(self, agent_name):
        assert job_tool_ids(agent_name, {"tools": _ALL_SERVERS}) == sorted(
            JOB_TOOLS[agent_name].ids
        )

    @pytest.mark.parametrize(
        "agent_name",
        [None, "", "my-own-agent", "kirocrew-research", "example-app:assistant", "kirocrew-WORKER"],
    )
    def test_an_agent_with_no_row_lists_nothing(self, agent_name):
        """An operator's own agent, an app's agent and a crew member's template
        are not in the table, so their deferral is what it was."""
        assert job_tool_ids(agent_name, {"tools": _ALL_SERVERS}) == []

    def test_only_what_the_spec_mounts_is_listed(self):
        spec = {"tools": ["tool_search", "@kirocrew-work"]}
        assert job_tool_ids("kirocrew-conductor", spec) == [
            "kirocrew-work::accept_eval",
            "kirocrew-work::work_brief",
            "kirocrew-work::work_ledger_read",
            "kirocrew-work::work_ledger_record",
        ]

    def test_a_single_tool_grant_lists_that_tool_alone(self):
        spec = {"tools": ["tool_search", "@kirocrew-core/spawn_run", "@kirocrew-core/wait"]}
        assert job_tool_ids("kirocrew", spec) == ["kirocrew-core::spawn_run"]

    def test_the_wildcard_reaches_the_servers_the_spec_declares(self):
        spec = {"tools": "*", "mcpServers": {"kirocrew-core": {}, "third-party": {}}}
        assert job_tool_ids("kirocrew", spec) == sorted(JOB_TOOLS["kirocrew"].ids)

    @pytest.mark.parametrize("spec", [None, {}, {"tools": None}, {"tools": ["fs_read"]}])
    def test_a_spec_that_mounts_no_declared_server_lists_nothing(self, spec):
        assert job_tool_ids("kirocrew", spec) == []

    @pytest.mark.parametrize("agent_name", sorted(JOB_TOOLS))
    def test_a_tool_outside_the_row_is_never_listed(self, agent_name):
        """Everything else a server serves stays deferred: the list is the row,
        whatever the spec mounts."""
        listed = set(job_tool_ids(agent_name, {"tools": _ALL_SERVERS}))
        everything = set(
            kas_never_defer_ids({"tools": _ALL_SERVERS}, lambda server: list(_served_tools(server)))
        )
        assert listed == set(JOB_TOOLS[agent_name].ids)
        assert listed < everything
        assert "kirocrew-core::memory_recall" in everything - listed

    def test_the_answer_is_the_same_for_every_session_of_an_agent(self):
        """Fixed per agent: the tool definitions its requests open with do not
        move between sessions, so the cached prefix stays valid."""
        spec = {"tools": list(reversed(_ALL_SERVERS))}
        assert job_tool_ids("kirocrew-worker", spec) == job_tool_ids(
            "kirocrew-worker", {"tools": _ALL_SERVERS}
        )


# ── the shipped specs ──


@pytest.mark.skipif(sys.platform == "win32", reason="the installers write POSIX launch paths")
def test_each_shipped_agent_gets_exactly_its_declared_set(tmp_path: Path, monkeypatch):
    """Run the real installers and read each declared agent's spec back: it grants
    the loader, and the set it keeps direct is its row, so every server a row names
    is one that agent's shipped spec mounts."""
    agents = tmp_path / "agents"
    agents.mkdir()
    mates = tmp_path / "mates" / "agents"
    launcher = tmp_path / "kirocrew"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(agent, "_KIROCREW_BIN", str(launcher))
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
    monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", hooks)
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: agents / "kirocrew.json")
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )

    agent.rebuild_agent_config()

    for name, row in sorted(JOB_TOOLS.items()):
        path = next(
            (d / f"{name}.json" for d in (agents, mates) if (d / f"{name}.json").is_file()), None
        )
        assert path is not None, f"{name} was not installed"
        spec = json.loads(path.read_text(encoding="utf-8"))
        assert spec_grants_tool_search(spec), f"{name} grants no loader"
        assert job_tool_ids(name, spec) == sorted(row.ids), name
