"""``hooks.auto_deny_tools`` withholds the static grants of the tools it names.

An ``allowedTools`` entry is approved by the backend with no permission request:
on KAS it becomes an allow rule, so a granted ``web_fetch`` runs without reaching
the gate that enforces the operator's deny. ``may_skip_gate_now`` (and
its per-session form) is the chokepoint every grant writer asks, so a ref a deny
pattern names gets no grant from any of them and its calls reach the gate.
"""

from __future__ import annotations

import json

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.agent_materialization.auto_approve import _apply_allowed_tools_ceiling
from kiro_crew.platform import governance


def _deny(tmp_path, monkeypatch, hooks: dict) -> None:
    """A scratch Crew home whose ``config.json`` holds *hooks*, read by a real load."""
    from kiro_crew.config import live

    home = tmp_path / "kirocrew-home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(live, "snapshot", lambda: None)
    monkeypatch.setattr(governance, "_CONFIRM_PATTERNS_MEMO", None)


def test_a_denied_builtin_loses_its_grant(tmp_path, monkeypatch) -> None:
    _deny(tmp_path, monkeypatch, {"auto_deny_tools": ["web_fetch"]})
    assert governance._auto_deny_tool_patterns() == ("web_fetch",)
    assert not governance.may_skip_gate_now("web_fetch")
    assert governance.may_skip_gate_now("web_search")


def test_without_a_deny_list_every_grant_stands(tmp_path, monkeypatch) -> None:
    _deny(tmp_path, monkeypatch, {"auto_approve_tools": ["web_fetch"]})
    assert governance._auto_deny_tool_patterns() == ()
    for ref in ("web_fetch", "web_search", "@github", "@github/create_issue", "*"):
        assert governance.may_skip_gate_now(ref)


@pytest.mark.parametrize("junk", ["web_fetch", None, {"web_fetch": True}])
def test_a_deny_list_that_is_not_a_list_denies_nothing_here_as_at_the_gate(
    tmp_path, monkeypatch, junk
) -> None:
    from kiro_crew.hooks import HooksConfig

    _deny(tmp_path, monkeypatch, {"auto_deny_tools": junk})
    assert HooksConfig.from_dict({"auto_deny_tools": junk}).auto_deny_tools == []
    assert governance.may_skip_gate_now("web_fetch")


def test_a_rule_under_the_kas_id_withholds_the_kiro_cli_grant(tmp_path, monkeypatch) -> None:
    """KAS runs ``web_search`` as ``remote_web_search``; the gate matches either."""
    _deny(tmp_path, monkeypatch, {"auto_deny_tools": ["remote_web_search"]})
    assert not governance.may_skip_gate_now("web_search")
    assert governance.may_skip_gate_now("web_fetch")


def test_the_kas_projection_sends_no_allow_for_a_denied_tool(tmp_path, monkeypatch) -> None:
    _deny(tmp_path, monkeypatch, {"auto_deny_tools": ["WEB_FETCH"]})
    authored = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
    spec = _spec(allowedTools=["web_fetch", "web_search"], permissions=authored)
    out = to_client_custom_agent("a", spec, "p")
    assert out["permissions"]["rules"] == [{"capability": "web_search", "effect": "allow"}]


def test_the_spec_writer_drops_the_denied_grant(tmp_path, monkeypatch) -> None:
    _deny(tmp_path, monkeypatch, {"auto_deny_tools": ["web_*"]})
    config = {"tools": ["web_fetch", "use_subagent"], "allowedTools": ["web_fetch", "use_subagent"]}
    _apply_allowed_tools_ceiling(config, source="test", audit=False)
    assert config["allowedTools"] == ["use_subagent"]
    assert config["tools"] == ["web_fetch", "use_subagent"]


def test_the_per_session_predicate_withholds_too(tmp_path, monkeypatch) -> None:
    _deny(tmp_path, monkeypatch, {"auto_deny_tools": ["use_subagent"]})
    permits = governance.may_skip_gate_for_session("dashboard:chat-1", None)
    assert not permits("use_subagent")
    assert permits("web_fetch")
    spec = _spec(toolsSettings={"subagent": {"trustedAgents": ["helper"]}})
    assert "permissions" not in to_client_custom_agent("a", spec, "p")


@pytest.mark.parametrize(
    ("ref", "withheld"),
    [
        ("@ops", True),
        ("@ops/*", True),
        ("@ops/query_*", True),
        ("@ops/delete_all", True),
        ("@OPS/Delete_All", True),
        ("@ops/read", False),
        ("@github", False),
        ("@github/delete_repo", False),
        ("web_fetch", False),
    ],
)
def test_an_mcp_grant_is_withheld_where_a_pattern_names_its_server_or_tool(
    monkeypatch, ref: str, withheld: bool
) -> None:
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: ("@ops/delete_*",))
    assert governance._operator_denies(ref) is withheld


@pytest.mark.parametrize(
    "pattern",
    ["@ops", "Running: @ops/delete_all", "mcp__ops__delete_all", "mcp__ops__*", "delete_all"],
)
def test_every_spelling_the_gate_denies_under_withholds_the_tool_grant(
    monkeypatch, pattern: str
) -> None:
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: (pattern,))
    assert governance._operator_denies("@ops/delete_all")


@pytest.mark.parametrize(
    "pattern",
    [
        "read_private",
        "READ_PRIVATE",
        "read_*",
        "*_private",
        "*",
        "@*/read_*",
        "@o*",
        "mcp__*",
        "mcp__ops*",
        "running:*",
        "Running: @ops*",
        "Running: *",
        "* @ops/*",
        "running:[ ]@ops/*",
    ],
)
def test_a_pattern_that_can_name_a_tool_of_the_server_withholds_its_whole_grant(
    monkeypatch, pattern: str
) -> None:
    """The gate judges ``@ops/read_private`` under its bare name and every MCP spelling."""
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: (pattern,))
    for ref in ("@ops", "@ops/q*", "@ops/*"):
        assert governance._operator_denies(ref), ref


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "@other",
        "@other/*",
        "@other/read_private",
        "Running: @other/*",
        "mcp__other__*",
        "*rm -rf*",
        "rm -rf *",
        "Running: git*",
    ],
)
def test_a_pattern_that_cannot_name_a_tool_of_the_server_keeps_its_whole_grant(
    monkeypatch, pattern: str
) -> None:
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: (pattern,))
    assert not governance._operator_denies("@ops")
    assert not governance._operator_denies("@ops/q*")


def test_a_bare_tool_pattern_keeps_the_exact_grant_of_another_tool(monkeypatch) -> None:
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: ("read_private",))
    assert governance._operator_denies("@ops/read_private")
    assert not governance._operator_denies("@ops/other_tool")


def test_the_kas_projection_sends_no_server_allow_under_a_bare_tool_deny(
    tmp_path, monkeypatch
) -> None:
    _deny(tmp_path, monkeypatch, {"auto_deny_tools": ["read_private"]})
    assert not governance.may_skip_gate_now("@ops")
    spec = _spec(allowedTools=["@ops", "@ops/other_tool", "@github/create_issue"])
    out = to_client_custom_agent("a", spec, "p")
    assert out["permissions"]["rules"] == [
        {"capability": "mcp", "match": ["github/create_issue", "ops/other_tool"], "effect": "allow"}
    ]


def test_a_pattern_naming_no_tool_or_server_keeps_the_grants(monkeypatch) -> None:
    """A shell-text rule binds the calls that reach the gate; it withdraws no grant."""
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: ("*rm -rf*",))
    for ref in ("@ops", "@ops/read", "web_fetch", "web_search"):
        assert not governance._operator_denies(ref)


@pytest.mark.parametrize("ref", ["*", "@*", "@o*/read", "fs_*"])
def test_a_wildcard_grant_is_withheld_while_any_pattern_is_set(monkeypatch, ref: str) -> None:
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: ("*rm -rf*",))
    assert governance._operator_denies(ref)
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: ())
    assert not governance._operator_denies(ref)


def test_doctor_learns_the_operators_deny_list_withheld_the_grant(tmp_path, monkeypatch) -> None:
    """The withhold is named as the operator's own rule, not the security policy."""
    _deny(tmp_path, monkeypatch, {"auto_deny_tools": ["read_private"]})
    assert governance.withheld_by_auto_deny_tools("@ops")
    assert not governance.withheld_by_confirm_tools("@ops")
    assert not governance.withheld_by_auto_deny_tools("@ops/other_tool")


@pytest.mark.parametrize("pattern", ["*curl*", "*sudo*", "web_fetch", "WEB_FETCH"])
def test_a_pattern_naming_no_tool_of_crews_own_servers_keeps_their_grants(
    monkeypatch, pattern: str
) -> None:
    """Their tools are known, so a deny that matches none of them withdraws nothing."""
    from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: (pattern,))
    for server in KIROCREW_BIN_MCP_SERVERS:
        assert not governance._operator_denies(f"@{server}"), server
        assert not governance._operator_denies(f"@{server}/q*"), server
    assert governance._operator_denies("@ops"), "a third-party server's tools are not known"


@pytest.mark.parametrize(
    "pattern",
    [
        "spawn_run",
        "SPAWN_*",
        "@kirocrew-core/spawn_run",
        "Running: @kirocrew-core/spawn_*",
        "mcp__kirocrew-core__spawn_run",
        "@kirocrew-core",
        "delete_*",
    ],
)
def test_a_pattern_naming_a_real_tool_of_crews_own_server_withholds_it(
    monkeypatch, pattern: str
) -> None:
    """``delete_*`` names ``kirocrew-core``'s ``delete_message``, so it withholds too."""
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: (pattern,))
    assert governance._operator_denies("@kirocrew-core")
    assert not governance._operator_denies("@kirocrew-cron")


def _no_tools(server: str) -> list[str]:
    return []


def _unreadable(server: str) -> list[str]:
    raise OSError("tool module unreadable")


@pytest.mark.parametrize("listing", [_no_tools, lambda server: None, _unreadable])
def test_a_managed_tool_list_that_cannot_be_read_withholds_the_grant(monkeypatch, listing) -> None:
    import kiro_crew.mcp_discovery_managed as managed
    from kiro_crew.platform import server_pattern_reach

    monkeypatch.setattr(managed, "managed_tool_names_for_policy", listing)
    monkeypatch.setattr(governance, "_auto_deny_tool_patterns", lambda: ("*curl*",))
    assert governance._operator_denies("@kirocrew-core")
    assert server_pattern_reach._managed_spellings.cache_info().currsize == 0, "nothing is kept"


def test_the_default_crew_servers_keep_their_grants_under_a_shell_deny(
    tmp_path, monkeypatch
) -> None:
    import kiro_crew.agent as agent_mod
    from kiro_crew.acp import default_crew_servers

    _deny(tmp_path, monkeypatch, {"auto_deny_tools": ["*curl*"]})
    entries = {
        server: {"command": "test-core"} for server in default_crew_servers.DEFAULT_CREW_SERVERS
    }
    monkeypatch.setattr(agent_mod, "managed_mcp_spec_entry", lambda name, **_kw: entries.get(name))
    monkeypatch.setattr("kiro_crew.apps.manager.owning_app_for_agent", lambda agent: None)
    granted = [f"@{server}" for server in default_crew_servers.DEFAULT_CREW_SERVERS]
    spec = {"name": "mine", "tools": ["fs_read"], "allowedTools": list(granted)}
    out = default_crew_servers.with_default_crew_servers("mine", spec)
    assert out["allowedTools"] == granted
    assert not governance.may_skip_gate_now("@ops")
