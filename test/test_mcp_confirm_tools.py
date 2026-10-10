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


def test_confirm_tools_is_restart_marked() -> None:
    """The static grants are rebuilt at restart, so a saved change must say so."""
    from kiro_crew.config.schema import requires_restart

    assert requires_restart("hooks.confirm_tools")
    assert not requires_restart("hooks.auto_approve_tools")
    assert not requires_restart("hooks")


def test_confirm_tools_survives_a_real_load(tmp_path, monkeypatch) -> None:
    """The declared schema node keeps a list with a stray entry instead of stripping it."""
    from kiro_crew.config import live

    _home(tmp_path, monkeypatch, base='{"hooks": {"confirm_tools": ["@atlassian", 3]}}')
    monkeypatch.setattr(live, "snapshot", lambda: None)
    assert governance._confirm_tool_patterns() == ("@atlassian",)


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


def _home(tmp_path, monkeypatch, *, base: str | None = None, local: str | None = None):
    """A scratch Crew home holding the given ``config.json`` / ``config.local.json`` text."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    if base is not None:
        (home / "config.json").write_text(base, encoding="utf-8")
    if local is not None:
        (home / "config.local.json").write_text(local, encoding="utf-8")
    return home


#: A ``config.json`` that does not parse (trailing comma) and never names the key.
_TORN_WITHOUT_KEY = '{"hooks": {"auto_approve_tools": ["@github/*"]},}'
#: The same tear in a file that does name the key.
_TORN_WITH_KEY = '{"hooks": {"confirm_tools": ["@atlassian/*"]},}'


def _failing_load(monkeypatch) -> None:
    from kiro_crew.config import live, loader

    def _boom(*_a, **_k):
        raise OSError("config unreadable")

    monkeypatch.setattr(live, "snapshot", lambda: None)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: _boom()))


def test_confirm_tool_patterns_failed_read_of_a_file_naming_the_key_is_unknown(
    tmp_path, monkeypatch
) -> None:
    _home(tmp_path, monkeypatch, base=_TORN_WITH_KEY)
    _failing_load(monkeypatch)
    assert governance._confirm_tool_patterns() is None


def test_confirm_tool_patterns_failed_read_without_the_key_is_empty(tmp_path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch, base='{"hooks": {}}')
    _failing_load(monkeypatch)
    assert governance._confirm_tool_patterns() == ()


class _Cfg:
    def __init__(self, hooks, degraded=frozenset(), base_unreadable=False) -> None:
        self.hooks = hooks
        self.degraded_sections = frozenset(degraded)
        self._base_unreadable = base_unreadable


@pytest.mark.parametrize(
    ("hooks", "expected"),
    [
        (None, ()),
        ({}, ()),
        ({"confirm_tools": "@jira"}, ()),
        ({"confirm_tools": ["@jira", 3, None]}, ("@jira",)),
        ("not-an-object", ()),
    ],
)
def test_confirm_tool_patterns_reads_the_hooks_section(
    tmp_path, monkeypatch, hooks, expected
) -> None:
    from kiro_crew.config import live

    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "snapshot", lambda: _Cfg(hooks))
    assert governance._confirm_tool_patterns() == expected


_DEGRADED_LOADS = [
    ({"*"}, False),
    ({"*", "*config.json"}, False),
    ({"hooks"}, False),
    (set(), True),
]


@pytest.mark.parametrize(("degraded", "base_unreadable"), _DEGRADED_LOADS)
def test_confirm_tool_patterns_degraded_load_of_a_file_naming_the_key_is_unknown(
    tmp_path, monkeypatch, degraded, base_unreadable
) -> None:
    from kiro_crew.config import live

    _home(tmp_path, monkeypatch, base=_TORN_WITH_KEY)
    cfg = _Cfg({}, degraded=degraded, base_unreadable=base_unreadable)
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    assert governance._confirm_tool_patterns() is None


@pytest.mark.parametrize(("degraded", "base_unreadable"), _DEGRADED_LOADS)
def test_confirm_tool_patterns_degraded_load_without_the_key_is_empty(
    tmp_path, monkeypatch, degraded, base_unreadable
) -> None:
    """The key unset in every file: a degraded load changes nothing."""
    from kiro_crew.config import live

    _home(tmp_path, monkeypatch, base=_TORN_WITHOUT_KEY, local='{"skills": {}}')
    cfg = _Cfg({}, degraded=degraded, base_unreadable=base_unreadable)
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    assert governance._confirm_tool_patterns() == ()


def test_confirm_tool_patterns_key_in_the_local_overlay_is_unknown(tmp_path, monkeypatch) -> None:
    from kiro_crew.config import live

    _home(tmp_path, monkeypatch, base='{"hooks": {}}', local='{"hooks": {"confirm_tools": [')
    monkeypatch.setattr(live, "snapshot", lambda: _Cfg({}, degraded={"*"}))
    assert governance._confirm_tool_patterns() is None


def test_confirm_tool_patterns_unreadable_bytes_are_unknown(tmp_path, monkeypatch) -> None:
    """Bytes that cannot be read cannot show the key unset."""
    from kiro_crew.config import live, loader

    _home(tmp_path, monkeypatch, base="{}")

    def _unreadable(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(loader, "read_config_text", _unreadable)
    monkeypatch.setattr(live, "snapshot", lambda: _Cfg({}, base_unreadable=True))
    assert governance._confirm_tool_patterns() is None


def test_confirm_tool_patterns_unrelated_degraded_section_still_reads(monkeypatch) -> None:
    from kiro_crew.config import live

    cfg = _Cfg({"confirm_tools": ["@jira"]}, degraded={"telegram"})
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    assert governance._confirm_tool_patterns() == ("@jira",)


def test_confirm_tool_patterns_stale_degradation_flag_reads_the_values(
    tmp_path, monkeypatch
) -> None:
    """The loader keeps a repaired tear flagged; files that parse again are read as written."""
    from kiro_crew.config import live

    _home(tmp_path, monkeypatch, base='{"hooks": {"confirm_tools": ["@jira"]}}')
    cfg = _Cfg({"confirm_tools": ["@jira"]}, degraded={"*", "*config.json"})
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    assert governance._confirm_tool_patterns() == ("@jira",)
    assert governance.may_skip_gate_now("@github")
    assert not governance.may_skip_gate_now("@jira")


@pytest.mark.parametrize("hooks_value", ['"x"', "[]", "3"])
def test_confirm_tool_patterns_non_object_hooks_in_a_file_naming_the_key_is_unknown(
    tmp_path, monkeypatch, hooks_value
) -> None:
    """An overlay whose ``hooks`` is not an object hides the base file's list."""
    from kiro_crew.config import live

    _home(
        tmp_path,
        monkeypatch,
        base='{"hooks": {"confirm_tools": ["@jira"]}}',
        local='{"hooks": ' + hooks_value + "}",
    )
    monkeypatch.setattr(live, "snapshot", lambda: _Cfg({}, degraded={"hooks"}))
    assert governance._confirm_tool_patterns() is None


def test_trailing_comma_config_withholds_static_grants(tmp_path, monkeypatch) -> None:
    """A config.json that names the key but does not parse loads as empty hooks.

    Read as "no patterns" that would write the blanket grant a confirm list in
    the broken file was there to withhold, so the chokepoint withholds it.
    """
    from kiro_crew.config import live, loader

    _home(tmp_path, monkeypatch, base=_TORN_WITH_KEY)
    monkeypatch.setattr(live, "snapshot", lambda: None)
    loaded = loader.KiroCrewConfig.load()
    assert not (loaded.hooks or {}).get("confirm_tools")
    assert governance._confirm_tool_patterns() is None
    assert not governance.may_skip_gate_now("@atlassian")
    assert not governance.may_skip_gate_now("@github")


_CREW_GRANTS = ("@kirocrew-core", "@kirocrew-cron/cron_list", "@kirocrew-guide/find_ui")


def test_trailing_comma_config_without_the_key_keeps_static_grants(tmp_path, monkeypatch) -> None:
    """Broken config.json, key unset: every grant is kept, Crew's own included.

    Only an operator who wrote ``confirm_tools`` can have asked for a prompt, so
    a tear in a file that never names it behaves as the key unset does.
    """
    from kiro_crew.config import live, loader

    _home(tmp_path, monkeypatch, base=_TORN_WITHOUT_KEY)
    monkeypatch.setattr(live, "snapshot", lambda: None)
    loaded = loader.KiroCrewConfig.load()
    assert loaded.degraded_sections, "the trailing comma must degrade the load"
    assert governance._confirm_tool_patterns() == ()
    for ref in (*_CREW_GRANTS, "@atlassian", "@github/create_issue"):
        assert governance.may_skip_gate_now(ref), ref
    assert not governance._operator_requires_confirmation("*")


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


def test_chokepoint_withholds_every_server_when_the_list_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(governance, "_confirm_tool_patterns", lambda: None)
    assert not governance.may_skip_gate_now("@atlassian")
    assert not governance.may_skip_gate_now("@github/create_issue")
    assert not governance._operator_requires_confirmation("fs_read")


@pytest.mark.parametrize("ref", ["*", "@*", "@atl*", "@*/create*", "?", "[@]*"])
def test_chokepoint_withholds_wildcard_grants_that_cover_mcp(monkeypatch, ref) -> None:
    _patterns(monkeypatch, ("@atlassian/create*",))
    assert governance._operator_requires_confirmation(ref)
    assert not governance.may_skip_gate_now(ref)


@pytest.mark.parametrize("ref", ["*", "@*", "@atl*"])
def test_wildcard_grants_unchanged_without_confirm_tools(monkeypatch, ref) -> None:
    _patterns(monkeypatch, ())
    assert not governance._operator_requires_confirmation(ref)


@pytest.mark.parametrize("ref", ["fs_*", "web_fetch", "@", ""])
def test_refs_that_cover_no_mcp_call_are_left_alone(monkeypatch, ref) -> None:
    _patterns(monkeypatch, ("*",))
    assert not governance._operator_requires_confirmation(ref)


def test_wildcard_grant_withheld_when_the_list_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(governance, "_confirm_tool_patterns", lambda: None)
    assert governance._operator_requires_confirmation("*")
    assert not governance._operator_requires_confirmation("fs_*")


def test_agent_config_sanitize_drops_a_star_grant(monkeypatch) -> None:
    """``allowedTools: ["*"]`` would auto-approve the confirmed calls outright."""
    _patterns(monkeypatch, ("@atlassian/create*",))
    cfg: dict = {"allowedTools": ["*", "fs_read", "@github"]}
    governance.sanitize_agent_config_governance(cfg, audit=False)
    assert "*" not in cfg["allowedTools"]
    assert "@github" in cfg["allowedTools"]


def test_chokepoint_leaves_builtins_alone(monkeypatch) -> None:
    _patterns(monkeypatch, ("*",))
    assert governance._operator_requires_confirmation("@atlassian")
    # A builtin is not an MCP server; its grant is decided by the existing floor.
    assert not governance._operator_requires_confirmation("fs_read")


# ── rebuild: the grant is withheld only for named servers ────────────────────


_LIVE = object()


def _rebuild(
    tmp_path, monkeypatch, servers: dict, patterns, *, template: dict | None = None
) -> dict:
    """Rebuild the agent spec; ``patterns=_LIVE`` reads the real config instead of a stub."""
    from kiro_crew.agent import rebuild_agent_config

    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "defaults.json").write_text(json.dumps(template or {"name": "kirocrew"}))
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
    if patterns is not _LIVE:
        _patterns(monkeypatch, patterns)

    rebuild_agent_config()
    return json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))


def test_rebuild_on_a_torn_config_without_the_key_keeps_crew_grants(tmp_path, monkeypatch) -> None:
    """Broken config.json, key unset: the template's ``@kirocrew-*`` grants are kept."""
    from kiro_crew.config import live

    _home(tmp_path, monkeypatch, base=_TORN_WITHOUT_KEY)
    monkeypatch.setattr(live, "snapshot", lambda: None)
    template = {"name": "kirocrew", "allowedTools": list(_CREW_GRANTS)}
    data = _rebuild(
        tmp_path, monkeypatch, {"atlassian": {"command": "atl"}}, _LIVE, template=template
    )
    for ref in (*_CREW_GRANTS, "@atlassian"):
        assert ref in data["allowedTools"], ref


def test_rebuild_on_a_torn_config_naming_the_key_withholds_crew_grants(
    tmp_path, monkeypatch
) -> None:
    """The key in a file that does not parse: the list is unknown, so nothing is granted."""
    from kiro_crew.config import live

    _home(tmp_path, monkeypatch, base=_TORN_WITH_KEY)
    monkeypatch.setattr(live, "snapshot", lambda: None)
    template = {"name": "kirocrew", "allowedTools": list(_CREW_GRANTS)}
    data = _rebuild(
        tmp_path, monkeypatch, {"atlassian": {"command": "atl"}}, _LIVE, template=template
    )
    assert "@kirocrew-core" in data["tools"]
    assert not [ref for ref in data["allowedTools"] if ref.startswith("@")]


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


def test_gate_withholds_operator_grants_while_the_list_is_unknown() -> None:
    mgr = HookManager(HooksConfig(auto_approve_tools=["@atlassian/*"], confirm_tools_unknown=True))
    assert _mcp_call(mgr, "atlassian", "createJiraIssue") == TOOL_ALLOW


def test_confirm_tools_unknown_is_not_serialized() -> None:
    cfg = HooksConfig(confirm_tools_unknown=True)
    assert "confirm_tools_unknown" not in cfg.to_dict()
    assert not HooksConfig.from_dict(cfg.to_dict()).confirm_tools_unknown


def _boot_hook_manager() -> HookManager:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.hooks import hooks_config_from_config_dict

    return HookManager(hooks_config_from_config_dict(KiroCrewConfig.load().hooks))


def test_boot_on_a_torn_overlay_naming_the_key_keeps_the_call_on_the_gate(
    tmp_path, monkeypatch
) -> None:
    """The load keeps the base grant and drops the torn overlay's confirm list."""
    from kiro_crew.config import live

    _home(
        tmp_path,
        monkeypatch,
        base='{"hooks": {"auto_approve_tools": ["@atlassian/*"]}}',
        local='{"hooks": {"confirm_tools": ["@atlassian/create*"]},}',
    )
    monkeypatch.setattr(live, "snapshot", lambda: None)
    mgr = _boot_hook_manager()
    assert mgr._config.confirm_tools_unknown
    assert _mcp_call(mgr, "atlassian", "createJiraIssue") == TOOL_ALLOW


def test_boot_on_a_torn_overlay_without_the_key_keeps_operator_grants(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew.config import live

    _home(
        tmp_path,
        monkeypatch,
        base='{"hooks": {"auto_approve_tools": ["@atlassian/*"]}}',
        local='{"skills": {},}',
    )
    monkeypatch.setattr(live, "snapshot", lambda: None)
    mgr = _boot_hook_manager()
    assert not mgr._config.confirm_tools_unknown
    assert _mcp_call(mgr, "atlassian", "createJiraIssue") == TOOL_AUTO_APPROVE
