"""A remote session never gets a tool that acts on the gateway machine.

Its engine runs on the gateway, so every Crew tool would run here too. Built-in
file/shell tools, set_project, file_send, the browser panel, pods and computer
use act on THIS machine and are unmounted from the session's agent; their
servers also refuse them in band.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import session_environment as se
from kiro_crew.acp.runtime import AcpRuntime, _without_local_only_grants
from kiro_crew.coder.session_launch import SessionEnvironmentChoices
from kiro_crew.mcp_tool_titles import _table

WORKSPACE = {"name": "workspace", "command": "bridge"}


def _runtime() -> AcpRuntime:
    runtime = object.__new__(AcpRuntime)
    runtime._remote_workspace = {"server": WORKSPACE, "workdir": "/workspace/app"}
    return runtime


def _servers(*names: str) -> list[dict]:
    return [{"name": n, "command": n} for n in names]


@pytest.fixture
def home(tmp_path, monkeypatch):
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    return tmp_path


def _bind(home, key: str, profile: str | None = "dev") -> None:
    SessionEnvironmentChoices(home / "coder" / "session-choices.json").set(key, profile)


# ── Unmounting ──────────────────────────────────────────────────────────


def test_a_whole_core_grant_becomes_every_core_tool_except_the_local_only_ones():
    grants = _without_local_only_grants(["@kirocrew-core", "@kirocrew-cron"])
    core = {g.split("/", 1)[1] for g in grants if g.startswith("@kirocrew-core/")}
    assert core == set(_table()["kirocrew-core"]) - se.LOCAL_ONLY_CORE_TOOLS
    assert core and not core & se.LOCAL_ONLY_CORE_TOOLS
    assert "@kirocrew-cron" in grants


def test_explicit_and_wildcard_grants_cannot_readmit_a_local_only_tool():
    grants = _without_local_only_grants(
        [
            "@kirocrew-core/file_send",
            "@kirocrew-core/memory_recall",
            "@kirocrew-core/b*",
            "@kirocrew-core/pod_*",
            "@kirocrew-computer",
            "@kirocrew-computer/computer_click",
        ]
    )
    assert "@kirocrew-core/memory_recall" in grants
    assert not any(g.endswith(("/file_send", "/browser", "/pod_up", "/pod_down")) for g in grants)
    assert "@kirocrew-core/pod_ls" in grants  # read-only: kept
    assert not any("*" in g for g in grants)
    assert not any(g.startswith("@kirocrew-computer") for g in grants)


def test_agent_tools_drop_builtins_builtin_group_and_local_only_tools():
    runtime = _runtime()
    servers = _servers("kirocrew-core", "kirocrew-computer", "kirocrew-cron")
    wire = runtime._remote_session_servers(servers)
    assert [s["name"] for s in wire] == ["kirocrew-core", "kirocrew-cron", "workspace"]
    [star] = runtime._remote_workspace_agents([{"name": "a", "tools": ["*"]}], wire)
    [listed] = runtime._remote_workspace_agents(
        [
            {
                "name": "b",
                "tools": ["read", "shell", "@builtin", "@kirocrew-core", "@kirocrew-computer"],
            }
        ],
        wire,
    )
    for agent in (star, listed):
        tools = agent["tools"]
        assert "@workspace" in tools
        assert not {"read", "shell", "@builtin", "@kirocrew-core"} & set(tools)
        assert "@kirocrew-core/set_project" not in tools
        assert "@kirocrew-core/spawn_run" in tools
        assert not any(t.startswith("@kirocrew-computer") for t in tools)


def test_a_local_session_is_untouched():
    runtime = object.__new__(AcpRuntime)
    runtime._remote_workspace = None
    servers = _servers("kirocrew-core", "kirocrew-computer")
    agents = [{"name": "a", "tools": ["*"]}]
    assert runtime._remote_session_servers(servers) is servers
    assert runtime._remote_workspace_agents(agents, servers) is agents


# ── In-band refusals ────────────────────────────────────────────────────


def test_refusal_follows_the_choice_record(home):
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "file_send") is None
    _bind(home, "dashboard:a")
    refusal = se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "file_send")
    assert refusal and "remote workspace" in refusal
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "memory_recall") is None
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-computer", "computer_click")
    # A subagent that inherited the environment is remote too.
    SessionEnvironmentChoices(home / "coder" / "session-choices.json").inherit(
        "subagent:x", "dashboard:a"
    )
    assert se.refuse_local_only_tool("subagent:x", "kirocrew-core", "browser")
    _bind(home, "dashboard:a", None)
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "file_send") is None


def test_an_unreadable_choice_record_refuses(home):
    path = home / "coder" / "session-choices.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "set_project")
    assert se.refuse_local_only_tool("", "kirocrew-core", "set_project") is None


def test_core_dispatch_refuses_before_the_handler_runs(home, monkeypatch):
    from kiro_crew import mcp_core

    _bind(home, "dashboard:a")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:a")
    ran: list[str] = []
    monkeypatch.setattr(mcp_core, "dispatch", lambda name, args: ran.append(name) or "ran")
    monkeypatch.setattr(mcp_core, "_deny_channel_agent_dispatch", lambda name, args: None)
    for tool in sorted(se.LOCAL_ONLY_CORE_TOOLS):
        assert "remote workspace" in mcp_core._call_tool_inner(tool, {})
    assert ran == []
    assert mcp_core._call_tool_inner("memory_recall", {}) == "ran"


def test_computer_use_dispatch_refuses_a_remote_session(home, monkeypatch):
    from kiro_crew.computer_use import tools

    _bind(home, "dashboard:a")
    name = sorted(tools.MCP_COMPUTER_SCHEMAS)[0]
    out = tools._dispatch(
        name,
        {},
        session_key="dashboard:a",
        agent="default",
        app="",
        approval_recorded=False,
    )
    payload = json.loads(out) if out.lstrip().startswith("{") else {"text": out}
    assert "remote workspace" in json.dumps(payload)
