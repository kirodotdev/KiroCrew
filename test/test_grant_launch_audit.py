"""Stage 1 of binding a standing MCP tool grant to its server launch: observe only.

A grant names a server by name. The gate now records which launch a granted call's
server name resolves to and logs a launch that is new for the name. It must never
change a verdict.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from kiro_crew import tool_grant_launch_audit as audit
from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, HooksConfig, ToolCall


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import kiro_crew.config.paths as paths_mod

    agents = tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: agents)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    audit._reset_for_tests()
    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    yield agents, events
    audit._reset_for_tests()


def _spec(agents: Path, entry: dict, name: str = "a") -> None:
    (agents / f"{name}.json").write_text(
        json.dumps({"name": name, "tools": ["*"], "mcpServers": {"ops": entry}})
    )


def _call(work_dir: str = "") -> ToolCall:
    return ToolCall(
        title="Look up the fleet",
        mcp_server="ops",
        mcp_tool="list",
        identity_trusted=True,
        spec_write_cwd=work_dir,
    )


def _judged(manager, call) -> str:
    """The verdict, after the background observer has handled the call."""
    action = manager.judge(call, agent="a").action
    audit._wait_for_tests()
    return action


def test_env_is_not_part_of_the_launch_id(tmp_path):
    a = audit.launch_id({"command": "npx", "args": ["-y", "pkg"], "env": {"K": "1"}}, "")
    b = audit.launch_id({"command": "npx", "args": ["-y", "pkg"], "env": {"K": "2"}}, "")
    assert a == b
    assert a != audit.launch_id({"command": "npx", "args": ["-y", "pkg@2"]}, "")


def test_a_relative_script_folds_the_working_folder(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    for d in (one, two):
        d.mkdir()
        (d / "index.js").write_text("")
    entry = {"command": "node", "args": ["index.js"]}
    assert audit.launch_id(entry, str(one)) != audit.launch_id(entry, str(two))
    # A package name is no file in the folder: one id in every project.
    pkg = {"command": "npx", "args": ["-y", "@scope/pkg"]}
    assert audit.launch_id(pkg, str(one)) == audit.launch_id(pkg, str(two))
    # A relative command is run from the folder too.
    rel = {"command": "./server"}
    assert audit.launch_id(rel, str(one)) != audit.launch_id(rel, str(two))
    # No folder to fold: no id rather than a shared one.
    assert audit.launch_id(rel, "") is None


def test_a_url_server_has_an_id():
    assert audit.launch_id({"url": "https://a.example/mcp"}, "") != audit.launch_id(
        {"url": "https://b.example/mcp"}, ""
    )


@pytest.mark.parametrize(
    "grants",
    [
        {"auto_approve_tool_identities": ["@ops/list"]},
        {"auto_approve_tools": ["@ops/*"]},
    ],
)
def test_a_changed_launch_is_logged_once_and_still_approved(home, grants, caplog):
    agents, events = home
    manager = HookManager(HooksConfig(**grants))
    _spec(agents, {"command": "npx", "args": ["-y", "ops-mcp"]})
    assert _judged(manager, _call()) == TOOL_AUTO_APPROVE
    assert events == []

    _spec(agents, {"command": "npx", "args": ["-y", "other-mcp"]})
    with caplog.at_level(logging.WARNING, logger=audit.__name__):
        assert _judged(manager, _call()) == TOOL_AUTO_APPROVE
        assert _judged(manager, _call()) == TOOL_AUTO_APPROVE

    assert len(events) == 1
    kind = "identity" if "auto_approve_tool_identities" in grants else "pattern"
    assert events[0]["operation"] == "tool_grant_launch_mismatch"
    assert events[0]["resources"] == f"grant={kind} new_launches=1"
    # Count and kind only: no server or tool name leaves in the record.
    assert "ops" not in events[0]["resources"]
    assert len([r for r in caplog.records if "observe-only" in r.getMessage()]) == 1


def test_the_same_launch_logs_nothing(home):
    agents, events = home
    manager = HookManager(HooksConfig(auto_approve_tool_identities=["@ops/list"]))
    _spec(agents, {"command": "npx", "args": ["-y", "ops-mcp"], "env": {"T": "1"}})
    _judged(manager, _call())
    _spec(agents, {"command": "npx", "args": ["-y", "ops-mcp"], "env": {"T": "2"}})
    assert _judged(manager, _call()) == TOOL_AUTO_APPROVE
    assert events == []


def test_a_project_server_with_the_same_name_is_seen(home, tmp_path):
    agents, events = home
    manager = HookManager(HooksConfig(auto_approve_tool_identities=["@ops/list"]))
    _spec(agents, {"command": "npx", "args": ["-y", "ops-mcp"]})
    _judged(manager, _call())

    project = tmp_path / "clone"
    (project / ".kiro" / "settings").mkdir(parents=True)
    (project / "srv.js").write_text("")
    (project / ".kiro" / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"ops": {"command": "node", "args": ["srv.js"]}}})
    )
    assert _judged(manager, _call(str(project))) == TOOL_AUTO_APPROVE
    assert len(events) == 1


def test_an_audit_failure_never_changes_the_verdict(home, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("broken")

    monkeypatch.setattr(audit, "session_launch_ids", _boom)
    manager = HookManager(HooksConfig(auto_approve_tool_identities=["@ops/list"]))
    assert _judged(manager, _call()) == TOOL_AUTO_APPROVE


def test_a_title_only_grant_is_not_observed(home, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(audit, "observe", lambda *a, **k: seen.append(k["grant"]))
    manager = HookManager(HooksConfig(auto_approve_tools=["Look up *"]))
    _judged(manager, _call())
    unproven = ToolCall(title="Look up the fleet", mcp_server="ops", mcp_tool="list")
    manager.judge(unproven, agent="a")
    assert seen == []


def test_a_markdown_agent_is_seen(home):
    agents, events = home
    manager = HookManager(HooksConfig(auto_approve_tool_identities=["@ops/list"]))

    def _md(args: str) -> None:
        (agents / "a.md").write_text(
            "---\nname: a\ntools: ['*']\nmcpServers:\n  ops:\n    command: npx\n"
            f"    args: [{args}]\n---\nprompt\n"
        )

    _md("'-y', 'ops-mcp'")
    _judged(manager, _call())
    _md("'-y', 'other-mcp'")
    assert _judged(manager, _call()) == TOOL_AUTO_APPROVE
    assert len(events) == 1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_a_fifo_settings_file_is_not_read(home, tmp_path):
    agents, events = home
    manager = HookManager(HooksConfig(auto_approve_tool_identities=["@ops/list"]))
    project = tmp_path / "clone"
    (project / ".kiro" / "settings").mkdir(parents=True)
    os.mkfifo(project / ".kiro" / "settings" / "mcp.json")
    _spec(agents, {"command": "npx", "args": ["-y", "ops-mcp"]})

    assert _judged(manager, _call(str(project))) == TOOL_AUTO_APPROVE


def test_the_gate_does_not_wait_for_the_observer(home, monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(audit, "observe", lambda *a, **k: release.wait(10))
    manager = HookManager(HooksConfig(auto_approve_tool_identities=["@ops/list"]))
    try:
        for _ in range(audit._MAX_PENDING + 8):
            assert manager.judge(_call(), agent="a").action == TOOL_AUTO_APPROVE
    finally:
        release.set()


def test_a_remote_entry_with_a_placeholder_command_is_keyed_by_url():
    a = {"type": "http", "command": "http", "url": "https://a.example/mcp"}
    b = {"type": "http", "command": "http", "url": "https://b.example/mcp"}
    assert audit.launch_id(a, "") != audit.launch_id(b, "")


def test_a_cap_is_reported_once_with_counts(home, monkeypatch):
    agents, events = home
    monkeypatch.setattr(audit, "_MAX_SERVERS", 0)
    manager = HookManager(HooksConfig(auto_approve_tool_identities=["@ops/list"]))
    _spec(agents, {"command": "npx", "args": ["-y", "ops-mcp"]})
    _judged(manager, _call())
    _judged(manager, _call())

    truncated = [e for e in events if e["operation"] == "tool_grant_launch_audit_truncated"]
    assert len(truncated) == 1
    assert truncated[0]["resources"] == "dropped names=1 queued=0 reports=0"
    assert audit._DROPPED["names"] == 2
