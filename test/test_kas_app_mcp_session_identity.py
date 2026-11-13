"""Builtin app MCP servers get the session token on the KAS backend too.

The reported defect: on KAS every ``mochi:mochi`` tool refused as
``identity_unattestable``. The kiro-cli path re-declares the server per session
with the signed token (``session_mcp.kiro_control_plane_servers``), but KAS never
reaches it, and ``kas_agents.hoist_servers_for_session`` tokened only the managed
``kirocrew-*`` names, so the app server launched with no token at all.
"""

from __future__ import annotations

from kiro_crew.acp import kas_app_identity
from kiro_crew.acp.kas_agents import MANAGED_MCP_SERVER_NAMES, hoist_servers_for_session
from kiro_crew.mcp_gateway.session_servers import STUB_SESSION_TOKEN_ENV

APP = "mochi:mochi"
PINNED = {
    "command": "/crew/python3.12",
    "args": ["-s", "-P", "-m", "kiro_crew", "app", "mcp", "mochi"],
    "env": {"PYTHONPATH": "/crew/site-packages", "KIROCREW_HOME": "/crew/home"},
}


def _agents(entry=None):
    return [
        {
            "id": "mochi--mochi",
            "mcpServers": {APP: entry or {"command": "kirocrew", "args": ["app", "mcp", "mochi"]}},
        }
    ]


def _env(element):
    return {pair["name"]: pair["value"] for pair in element.get("env") or []}


def _hoist(agents, launches):
    return hoist_servers_for_session(
        agents, "mochi--mochi", [], session_token="tok-1", app_launches=launches
    )


def test_app_server_is_hoisted_with_the_token_and_the_shipped_launch(monkeypatch):
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "8642")
    out_agents, array = _hoist(_agents(), {APP: PINNED})

    (element,) = array
    assert element["name"] == APP
    assert (element["command"], element["args"]) == (PINNED["command"], PINNED["args"])
    env = _env(element)
    assert env[STUB_SESSION_TOKEN_ENV] == "tok-1"
    assert env["KIROCREW_PORT"] == "8642"
    assert env["PYTHONPATH"] == "/crew/site-packages"
    assert env["KIROCREW_HOME"] == "/crew/home"
    # Moved out of the block: the session-level element is the one launch.
    assert "mcpServers" not in out_agents[0]


def test_a_hand_edited_launch_never_reaches_the_token():
    spec_entry = {"command": "/tmp/evil", "args": ["x"], "env": {"LEAK": "1"}}
    _, array = _hoist(_agents(spec_entry), {APP: PINNED})

    (element,) = array
    assert element["command"] == PINNED["command"]
    assert element["args"] == PINNED["args"]
    assert "LEAK" not in _env(element)


def test_an_app_server_without_a_shipped_launch_is_left_alone():
    agents = _agents()
    out_agents, array = _hoist(agents, {})

    assert array == []
    assert out_agents is agents


def test_resolver_returns_the_launch_for_a_declared_app_server(monkeypatch, tmp_path):
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(session_mcp, "builtin_app_identity_launches", lambda: {APP: PINNED})
    monkeypatch.setattr(session_mcp, "native_settings_sources", lambda _wd: [])
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)

    got = kas_app_identity.resolve_app_identity_launches(
        _agents(), "mochi--mochi", tmp_path, MANAGED_MCP_SERVER_NAMES
    )
    assert got == {APP: PINNED}
    # Another agent's block does not count.
    assert (
        kas_app_identity.resolve_app_identity_launches(
            _agents(), "other", tmp_path, MANAGED_MCP_SERVER_NAMES
        )
        == {}
    )


def test_resolver_withholds_every_app_server_on_unreadable_settings(monkeypatch, tmp_path):
    from kiro_crew.acp import session_mcp

    def unreadable(_wd):
        raise session_mcp.NativeSettingsUnreadable("global", tmp_path / "mcp.json", "bad")

    monkeypatch.setattr(session_mcp, "builtin_app_identity_launches", lambda: {APP: PINNED})
    monkeypatch.setattr(session_mcp, "native_settings_sources", unreadable)

    got = kas_app_identity.resolve_app_identity_launches(
        _agents(), "mochi--mochi", tmp_path, MANAGED_MCP_SERVER_NAMES
    )
    assert got == {}


def test_resolver_honours_a_settings_restriction(monkeypatch, tmp_path):
    from kiro_crew.acp import session_mcp

    muted = session_mcp.NativeSettingsSource(
        session_mcp.NATIVE_SOURCE_GLOBAL, tmp_path / "mcp.json", {APP: {"disabled": True}}
    )
    monkeypatch.setattr(session_mcp, "builtin_app_identity_launches", lambda: {APP: PINNED})
    monkeypatch.setattr(session_mcp, "native_settings_sources", lambda _wd: [muted])
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)

    got = kas_app_identity.resolve_app_identity_launches(
        _agents(), "mochi--mochi", tmp_path, MANAGED_MCP_SERVER_NAMES
    )
    assert got == {}


def test_the_shipped_disabled_false_still_hoists_and_tokens():
    # Mochi's manifest declares ``disabled: false`` and registration copies it into
    # the agent's entry, so the hoist must not read that no-op as a restriction.
    entry = {"command": "kirocrew", "args": ["app", "mcp", "mochi"], "disabled": False}
    _, array = _hoist(_agents(entry), {APP: PINNED})

    assert [e["name"] for e in array] == [APP]
    assert _env(array[0])[STUB_SESSION_TOKEN_ENV] == "tok-1"


def test_the_shipped_manifest_entry_shape_is_hoistable():
    import json
    from pathlib import Path

    import kiro_crew

    manifest = Path(kiro_crew.__file__).parent / "apps" / "builtins" / "mochi" / "app.json"
    entry = json.loads(manifest.read_text(encoding="utf-8"))["mcpServers"]["mochi"]
    _, array = _hoist(_agents(dict(entry)), {APP: PINNED})

    assert [e["name"] for e in array] == [APP]


def test_a_real_restriction_on_the_entry_still_blocks_the_hoist():
    entry = {"command": "kirocrew", "args": ["app", "mcp", "mochi"], "disabledTools": ["x"]}
    agents = _agents(entry)
    out_agents, array = _hoist(agents, {APP: PINNED})

    assert array == []
    assert out_agents is agents
