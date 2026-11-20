"""Stage 2 of binding a standing MCP tool grant to its server launch: enforce bound grants.

A grant the approval card or the MCP tool list writes is bound to the launch ids of
its server (``hooks.auto_approve_tool_identity_launches``). A bound grant approves
only those launches and is matched case-sensitively; an unbound grant keeps the
stage-1 behaviour (``test_grant_launch_audit.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import tool_grant_launch_audit as audit
from kiro_crew.dashboard import always_allow_tool
from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, HooksConfig, ToolCall

REF = "@ops/list"
NPX = {"command": "npx", "args": ["-y", "ops-mcp"]}
OTHER = {"command": "npx", "args": ["-y", "other-mcp"]}


@pytest.fixture
def agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import kiro_crew.config.paths as paths_mod

    agents = tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: agents)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    audit._reset_for_tests()

    class _Sel:
        def log_api_access(self, **_kw):
            pass

    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    yield agents
    audit._reset_for_tests()


def _spec(agents: Path, entry: dict, server: str = "ops") -> None:
    (agents / "a.json").write_text(
        json.dumps({"name": "a", "tools": ["*"], "mcpServers": {server: entry}})
    )


def _user_mcp(agents: Path, entry: dict) -> None:
    settings = agents.parent / "settings"
    settings.mkdir(exist_ok=True)
    (settings / "mcp.json").write_text(json.dumps({"mcpServers": {"ops": entry}}))


def _id(entry: dict) -> str:
    found = audit.launch_id(entry, "")
    assert found
    return found


def _manager(bindings, grants=(REF,), **extra) -> HookManager:
    return HookManager(
        HooksConfig.from_dict(
            {
                "auto_approve_tool_identities": list(grants),
                "auto_approve_tool_identity_launches": bindings,
                **extra,
            }
        )
    )


def _call(server: str = "ops", tool: str = "list") -> ToolCall:
    return ToolCall(
        title="Look up the fleet", mcp_server=server, mcp_tool=tool, identity_trusted=True
    )


def _approved(manager: HookManager, call: ToolCall | None = None) -> bool:
    verdict = manager.judge(call or _call(), agent="a")
    audit._wait_for_tests()
    return verdict.action == TOOL_AUTO_APPROVE


def test_a_bound_grant_approves_the_launch_it_was_given_for(agents):
    _spec(agents, NPX)
    assert _approved(_manager({REF: [_id(NPX)]}))


def test_a_bound_grant_asks_when_the_launch_changes(agents):
    _spec(agents, OTHER)
    assert not _approved(_manager({REF: [_id(NPX)]}))
    # Granting again from that session adds its launch.
    assert _approved(_manager({REF: [_id(NPX), _id(OTHER)]}))


def test_an_unbound_grant_keeps_approving_a_changed_launch(agents):
    _spec(agents, NPX)
    manager = _manager({})
    assert _approved(manager)
    _spec(agents, OTHER)
    assert _approved(manager)


def test_a_bound_grant_asks_when_no_definition_is_seen(agents):
    assert not _approved(_manager({REF: [_id(NPX)]}))


@pytest.mark.parametrize("bindings", [{REF: "not-a-list"}, {REF: [1]}, "not-a-map"])
def test_a_malformed_binding_never_approves(agents, bindings):
    _spec(agents, NPX)
    assert not _approved(_manager(bindings))


def test_a_bound_grant_is_matched_case_sensitively(agents):
    _spec(agents, NPX, server="OPS")
    assert not _approved(_manager({REF: [_id(NPX)]}), _call(server="OPS"))


def test_an_unbound_case_variant_cannot_step_around_a_binding(agents):
    _spec(agents, OTHER)
    # "@OPS/List" alone would approve "@ops/list" case-insensitively.
    manager = _manager({REF: [_id(NPX)]}, grants=(REF, "@OPS/List"))
    assert not _approved(manager)


def test_every_definition_of_the_server_must_be_bound(agents):
    _spec(agents, NPX)
    _user_mcp(agents, OTHER)
    assert not _approved(_manager({REF: [_id(NPX)]}))
    assert _approved(_manager({REF: [_id(NPX), _id(OTHER)]}))


def test_a_url_server_is_bound_to_its_url(agents):
    http = {"type": "http", "url": "https://ops.example/mcp"}
    _spec(agents, http)
    assert _approved(_manager({REF: [_id(http)]}))
    _spec(agents, {"type": "sse", "url": "https://elsewhere.example/sse"})
    assert not _approved(_manager({REF: [_id(http)]}))


def test_confirm_tools_outranks_a_bound_grant(agents):
    _spec(agents, NPX)
    assert not _approved(_manager({REF: [_id(NPX)]}, confirm_tools=["@ops/*"]))


def test_heartbeat_hooks_drop_a_bound_grant(agents):
    from kiro_crew.slack.gateway import _build_heartbeat_hooks

    _spec(agents, NPX)
    assert not _approved(_build_heartbeat_hooks(_manager({REF: [_id(NPX)]})))


# -- the grant writer -------------------------------------------------------


def _hooks(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "crew" / "config.json").read_text()).get("hooks", {})


def test_the_grant_and_its_binding_are_written_together(agents, tmp_path):
    always_allow_tool.write_grant(REF, add=True, launches=("b" * 64,))
    hooks = _hooks(tmp_path)
    assert hooks["auto_approve_tool_identities"] == [REF]
    assert hooks["auto_approve_tool_identity_launches"] == {REF: ["b" * 64]}
    always_allow_tool.write_grant(REF, add=True, launches=("a" * 64,))
    assert _hooks(tmp_path)["auto_approve_tool_identity_launches"] == {REF: ["a" * 64, "b" * 64]}
    always_allow_tool.write_grant("@OPS/List", add=False)
    hooks = _hooks(tmp_path)
    assert hooks["auto_approve_tool_identities"] == []
    assert "auto_approve_tool_identity_launches" not in hooks


def test_a_grant_with_no_readable_launch_is_written_unbound(agents, tmp_path):
    always_allow_tool.write_grant(REF, add=True)
    assert "auto_approve_tool_identity_launches" not in _hooks(tmp_path)


def test_the_card_carries_the_launches_its_session_sees(agents, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: False
    )
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: False)
    _spec(agents, NPX)
    event = SimpleNamespace(
        mcp_identity_trusted=True,
        mcp_server_name="ops",
        tool_name="list",
        is_shell=False,
        mcp_identity_unreadable=False,
        connection_mcp_identity=None,
        child_low_fidelity=False,
        child_mcp_identity_trusted=False,
        spec_write_cwd="",
    )
    fields = always_allow_tool.card_fields(event, agent="a")
    assert fields[always_allow_tool.FIELD_LAUNCHES] == _id(NPX)
    assert always_allow_tool._card_launches(fields[always_allow_tool.FIELD_LAUNCHES]) == (_id(NPX),)
    assert always_allow_tool._card_launches("not-an-id") == ()
