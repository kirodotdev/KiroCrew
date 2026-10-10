"""``hooks.confirm_tools``: tool patterns that always ask before running.

A user-added MCP server is written into the agent's ``allowedTools`` on every
rebuild, and an ``allowedTools`` entry is approved by the backend without a
permission request, so the approval gate never sees the call. ``confirm_tools``
is the operator's opt-in to keep a server on the gate. It is consulted inside
``may_skip_gate_now``, the chokepoint every static grant writer calls, so a
server it names stays mounted but gets no ``allowedTools`` grant or
``autoApprove`` from any writer, and at the gate a match outranks an
``auto_approve_tools`` grant. With the key unset nothing changes.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    HookManager,
    HooksConfig,
)
from kiro_crew.platform import governance

# ── config parsing ────────────────────────────────────────────────────────────


def test_confirm_tools_defaults_to_empty() -> None:
    assert HooksConfig.from_dict({}).confirm_tools == []
    assert HooksConfig().confirm_tools == []


def test_confirm_tools_parses_and_round_trips() -> None:
    cfg = HooksConfig.from_dict({"confirm_tools": ["@atlassian/*", "@github/create_*"]})
    assert cfg.confirm_tools == ["@atlassian/*", "@github/create_*"]
    assert HooksConfig.from_dict(cfg.to_dict()).confirm_tools == cfg.confirm_tools


@pytest.mark.parametrize("junk", ["@atlassian", 1, None, {"a": 1}])
def test_confirm_tools_junk_degrades_to_empty(junk: object) -> None:
    assert HooksConfig.from_dict({"confirm_tools": junk}).confirm_tools == []


def test_confirm_tools_drops_non_string_entries() -> None:
    cfg = HooksConfig.from_dict({"confirm_tools": ["@jira", 3, None]})
    assert cfg.confirm_tools == ["@jira"]


# ── server matching (rebuild side) ────────────────────────────────────────────


@pytest.mark.parametrize(
    "patterns",
    [
        ("@atlassian",),
        ("@atlassian/*",),
        ("@atlassian/createJiraIssue",),
        ("Running: @atlassian/create*",),
        ("running: @atlassian/create*",),
        ("RUNNING: @atlassian",),
        ("@ATLASSIAN",),
        ("@atl*",),
        ("@*",),
        ("*",),
        ("*createJira*",),
        ("Running: *",),
        ("?atlassian*",),
        ("@*create*",),
        ("Running*",),
        ("@atlassian?create*",),
        ("@a*",),
    ],
)
def test_server_requires_confirmation_matches(patterns: tuple[str, ...]) -> None:
    assert governance.server_requires_confirmation("atlassian", patterns)


@pytest.mark.parametrize(
    "patterns",
    [
        (),
        ("@github",),
        ("@atlassian-cloud",),
        ("execute_bash",),
        ("Running: git*",),
        ("Look up*",),
        ("@",),
        ("@gh*",),
        ("@github/*",),
    ],
)
def test_server_requires_confirmation_does_not_match(patterns: tuple[str, ...]) -> None:
    assert not governance.server_requires_confirmation("atlassian", patterns)


def test_confirm_tool_patterns_unreadable_config_yields_none(monkeypatch) -> None:
    from kiro_crew.config import live, loader

    def _boom(*_a, **_k):
        raise OSError("config unreadable")

    monkeypatch.setattr(live, "snapshot", lambda: None)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: _boom()))
    assert governance._confirm_tool_patterns() == ()


@pytest.mark.parametrize(
    ("hooks", "expected"),
    [
        ({}, ()),
        ({"confirm_tools": "@jira"}, ()),
        ({"confirm_tools": ["@jira", 3, None]}, ("@jira",)),
    ],
)
def test_confirm_tool_patterns_reads_the_hooks_section(monkeypatch, hooks, expected) -> None:
    from kiro_crew.config import live

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.hooks = hooks  # type: ignore[attr-defined]
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    assert governance._confirm_tool_patterns() == expected


# ── the chokepoint every static grant writer calls ───────────────────────────


def _patterns(monkeypatch, patterns: tuple[str, ...]) -> None:
    monkeypatch.setattr(governance, "_confirm_tool_patterns", lambda: patterns)


def test_chokepoint_unchanged_without_confirm_tools(monkeypatch) -> None:
    _patterns(monkeypatch, ())
    assert governance.may_skip_gate_now("@atlassian")
    assert governance.may_skip_gate_now("@atlassian/createJiraIssue")


def test_chokepoint_withholds_a_named_server(monkeypatch) -> None:
    _patterns(monkeypatch, ("@atlassian/create*",))
    assert not governance.may_skip_gate_now("@atlassian")
    assert not governance.may_skip_gate_now("@atlassian/getJiraIssue")
    assert governance.may_skip_gate_now("@github")


def test_chokepoint_leaves_builtins_alone(monkeypatch) -> None:
    _patterns(monkeypatch, ("*",))
    assert governance._operator_requires_confirmation("@atlassian")
    # A builtin is not an MCP server; its grant is decided by the existing floor.
    assert not governance._operator_requires_confirmation("fs_read")


# ── rebuild: the grant is withheld only for named servers ────────────────────


def _rebuild(tmp_path, monkeypatch, servers: dict, patterns: tuple[str, ...]) -> dict:
    from kiro_crew.agent import rebuild_agent_config

    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}))
    (agent_dir / "prompt.md").write_text("prompt")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    settings_dir = tmp_path / ".kiro" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "mcp.json").write_text(json.dumps({"mcpServers": servers}))
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
    monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
    monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", "/usr/bin/kirocrew")
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: f"/usr/bin/{cmd}")
    _patterns(monkeypatch, patterns)

    rebuild_agent_config()
    return json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))


def test_rebuild_without_confirm_tools_keeps_the_grant(tmp_path, monkeypatch) -> None:
    data = _rebuild(tmp_path, monkeypatch, {"atlassian": {"command": "atl"}}, ())
    assert "@atlassian" in data["tools"]
    assert "@atlassian" in data["allowedTools"]


def test_rebuild_withholds_the_grant_for_a_named_server(tmp_path, monkeypatch) -> None:
    data = _rebuild(
        tmp_path,
        monkeypatch,
        {"atlassian": {"command": "atl"}, "github": {"command": "gh"}},
        ("@atlassian/create*",),
    )
    # Still mounted, so the agent keeps the server's tools.
    assert "atlassian" in data["mcpServers"]
    assert "@atlassian" in data["tools"]
    # No blanket grant in either spelling, so each call raises a permission request.
    assert not [ref for ref in data["allowedTools"] if ref.startswith("@atlassian")]
    # A server the operator did not name is untouched.
    assert "@github" in data["allowedTools"]


def test_rebuild_drops_the_emitted_auto_approve_for_a_named_server(tmp_path, monkeypatch) -> None:
    data = _rebuild(
        tmp_path,
        monkeypatch,
        {
            "atlassian": {"command": "atl", "autoApprove": ["createJiraIssue"]},
            "github": {"command": "gh", "autoApprove": ["get_issue"]},
        },
        ("@atlassian",),
    )
    assert "autoApprove" not in data["mcpServers"]["atlassian"]
    assert data["mcpServers"]["github"].get("autoApprove") == ["get_issue"]


def test_rebuild_strips_a_grant_left_from_an_earlier_build(tmp_path, monkeypatch) -> None:
    _rebuild(tmp_path, monkeypatch, {"atlassian": {"command": "atl"}}, ())
    # The same tree rebuilt after the operator opts in.
    from kiro_crew.agent import rebuild_agent_config

    _patterns(monkeypatch, ("@atlassian",))
    rebuild_agent_config()
    data = json.loads((tmp_path / ".kiro" / "agents" / "kirocrew.json").read_text(encoding="utf-8"))
    assert "@atlassian" in data["tools"]
    assert not [ref for ref in data["allowedTools"] if ref.startswith("@atlassian")]


_GATE_PROBE_PATTERNS = (
    "@atlassian",
    "@atlassian/*",
    "@*create*",
    "*create*",
    "Running*",
    "running: @atl*",
    "@a?lassian/create*",
    "[@]atlassian*",
    "@gh*",
    "Look up*",
    "execute_bash",
    "@github/*",
)


@pytest.mark.parametrize("pattern", _GATE_PROBE_PATTERNS)
def test_a_pattern_the_gate_applies_always_withholds_the_static_grant(pattern: str) -> None:
    """The two sides must never disagree in the direction that hides a call.

    If the gate would ask for a call on the server, the static grant that keeps
    the call away from the gate has to be withheld.
    """
    mgr = HookManager(HooksConfig(auto_approve_tools=["@atlassian/*"], confirm_tools=[pattern]))
    gate_asks = _mcp_call(mgr, "atlassian", "createJiraIssue") == TOOL_ALLOW
    if gate_asks:
        assert governance.server_requires_confirmation("atlassian", (pattern,))


# ── dashboard enable: the same chokepoint withholds the grant ────────────────


def test_dashboard_enable_withholds_the_grant_for_a_named_server(tmp_path, monkeypatch) -> None:
    from unittest.mock import patch

    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

    agent_cfg = tmp_path / "kirocrew.json"
    agent_cfg.write_text(
        json.dumps({"name": "kirocrew", "mcpServers": {}, "tools": [], "allowedTools": []})
    )
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "atlassian": {"command": "atl", "autoApprove": ["createJiraIssue"]},
                    "github": {"command": "gh"},
                }
            }
        )
    )
    _patterns(monkeypatch, ("@atlassian",))
    with (
        patch("kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", mcp_json),
        patch(
            "kiro_crew.dashboard.handlers.agents._installed_agent_config", return_value=agent_cfg
        ),
    ):
        _sync_mcp_to_agent("atlassian", enabled=True)
        _sync_mcp_to_agent("github", enabled=True)
    cfg = json.loads(agent_cfg.read_text(encoding="utf-8"))
    assert "@atlassian" in cfg["tools"]
    assert "@atlassian" not in cfg["allowedTools"]
    assert "autoApprove" not in cfg["mcpServers"]["atlassian"]
    assert "@github" in cfg["allowedTools"]


# ── gate: a confirm match outranks an operator grant ─────────────────────────


def _mcp_call(mgr: HookManager, server: str, tool: str, *, title: str = "") -> str:
    return mgr.on_tool_call(
        title or f"Running: @{server}/{tool}",
        mcp_server_name=server,
        mcp_tool_name=tool,
        mcp_identity_trusted=True,
    ).action


def test_gate_grant_applies_without_confirm_tools() -> None:
    mgr = HookManager(HooksConfig(auto_approve_tools=["@atlassian/*"]))
    assert _mcp_call(mgr, "atlassian", "createJiraIssue") == TOOL_AUTO_APPROVE


@pytest.mark.parametrize("pattern", ["@atlassian", "@atlassian/create*", "Running: @atlassian/*"])
def test_gate_confirm_outranks_the_grant(pattern: str) -> None:
    mgr = HookManager(HooksConfig(auto_approve_tools=["@atlassian/*"], confirm_tools=[pattern]))
    assert _mcp_call(mgr, "atlassian", "createJiraIssue") == TOOL_ALLOW


def test_gate_confirm_leaves_other_tools_granted() -> None:
    mgr = HookManager(
        HooksConfig(auto_approve_tools=["@atlassian/*"], confirm_tools=["@atlassian/create*"])
    )
    assert _mcp_call(mgr, "atlassian", "getJiraIssue") == TOOL_AUTO_APPROVE


def test_gate_confirm_cannot_be_dodged_by_the_title() -> None:
    mgr = HookManager(
        HooksConfig(auto_approve_tools=["@atlassian/*"], confirm_tools=["@atlassian/create*"])
    )
    action = _mcp_call(mgr, "atlassian", "createJiraIssue", title="Look up the ticket")
    assert action == TOOL_ALLOW


def test_gate_confirm_does_not_override_a_deny() -> None:
    mgr = HookManager(
        HooksConfig(auto_deny_tools=["@atlassian/delete*"], confirm_tools=["@atlassian"])
    )
    assert _mcp_call(mgr, "atlassian", "deleteJiraIssue") == TOOL_DENY
