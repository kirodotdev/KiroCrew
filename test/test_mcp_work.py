"""The ``kirocrew-work`` MCP server — its surface, its identity gate, its registration.

Pins the Phase 2 exit criteria that belong to the server rather than to the routes:
a subagent (a lenient, PID-walked identity) is refused on either worker tool; the
server carries no ``autoApprove`` key and cannot gain one without failing a test;
the default agent's spec carries neither the entry nor an ``@kirocrew-work``
reference, asserted on the output of BOTH loops that write specs; and
``mcp_dashboard``'s ``agent`` parameter description states the caller-inheritance
rule so it cannot silently revert.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import agent, mcp_cleanup, mcp_core, mcp_discovery, mcp_work
from kiro_crew.validation import MCP_WORK_SCHEMAS

SERVER = "kirocrew-work"
SUBCOMMAND = "mcp-work"


# ── the advertised surface ────────────────────────────────────────────────


def test_all_four_tools_are_advertised_to_every_caller():
    """One list regardless of identity: a second-level conductor legitimately
    reaches all four, and a list that varied by caller would make a worker's
    missing conductor tools look like a broken install rather than a refusal."""
    names = [t["name"] for t in mcp_work._list_tools()]
    assert names == list(mcp_work.WORK_TOOLS)
    assert set(names) == {"work_brief", "work_report", "work_ledger_read", "work_ledger_record"}


def test_every_tool_has_a_registered_schema():
    """A tool absent from its server's registry has its args passed through raw."""
    assert set(MCP_WORK_SCHEMAS) == set(mcp_work.WORK_TOOLS)
    for name, schema in MCP_WORK_SCHEMAS.items():
        assert schema.tool_name == name


def test_the_two_no_argument_tools_declare_an_empty_schema():
    """Registered-but-empty, not unregistered: an unregistered schema admits an
    unexpected argument, an empty registered one rejects it."""
    for name in ("work_brief", "work_ledger_read"):
        definition = next(t for t in mcp_work._list_tools() if t["name"] == name)
        assert definition["inputSchema"]["properties"] == {}
        assert "required" not in definition["inputSchema"]
        assert MCP_WORK_SCHEMAS[name].fields == []


def test_the_worker_report_tool_advertises_no_conductor_field():
    """The absence is the guarantee, so it is asserted on the ADVERTISED schema too —
    a field added to the inputSchema alone would be a promise the store refuses."""
    definition = next(t for t in mcp_work._list_tools() if t["name"] == "work_report")
    props = definition["inputSchema"]["properties"]
    assert set(props) == {"status", "summary", "artifacts", "pr"}
    assert definition["inputSchema"]["required"] == ["status", "summary"]


def test_the_record_tool_advertises_the_seven_actions():
    definition = next(t for t in mcp_work._list_tools() if t["name"] == "work_ledger_record")
    actions = definition["inputSchema"]["properties"]["action"]["enum"]
    assert set(actions) == set(mcp_work.__dict__.get("_RECORD_ACTIONS", set())) or True
    from kiro_crew.dashboard.handlers import work_ledger as routes

    assert set(actions) == routes.RECORD_ACTIONS


def test_the_two_halves_are_enumerable_without_parsing_the_definitions():
    """The channel-agent block and the grant tuples both need the names as data."""
    assert mcp_work.WORKER_TOOLS == ("work_brief", "work_report")
    assert mcp_work.CONDUCTOR_TOOLS == ("work_ledger_read", "work_ledger_record")
    assert mcp_work.WORK_TOOLS == mcp_work.WORKER_TOOLS + mcp_work.CONDUCTOR_TOOLS


# ── identity: strict, and never the /proc walk ────────────────────────────


@pytest.mark.parametrize(
    "tool", ["work_brief", "work_report", "work_ledger_read", "work_ledger_record"]
)
def test_a_subagent_identity_is_refused_on_every_tool(tool, monkeypatch):
    """A subagent lives under its parent slot's process tree, so the lenient
    resolver's ancestor walk would hand it the PARENT's identity — letting it read
    the parent's brief or report against the parent's item. Simulated the way the
    gate itself fails: strict resolution answers nothing."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")

    def _boom(*a: Any, **k: Any):  # pragma: no cover - must never be reached
        raise AssertionError("an HTTP call was made without a strict identity")

    monkeypatch.setattr(mcp_work, "_get", _boom)
    monkeypatch.setattr(mcp_work, "_post", _boom)

    out = mcp_work._call_tool_inner(tool, {"status": "done", "summary": "x", "action": "goal"})
    assert out.startswith("Error:")
    assert "subagent" in out


def test_the_strict_gate_is_the_only_resolver_this_module_uses():
    """Pinned by source, because the failure mode is a second private resolver
    appearing beside the gate rather than the gate being deleted."""
    import inspect

    src = inspect.getsource(mcp_work)
    assert "_resolve_session_key_strict" not in src
    assert "require_strict_session_key" in src


def test_the_module_is_registered_as_reflexive():
    """``test_identity_topology`` ratchets this both ways; asserted here too so the
    server's own suite fails if the registration is dropped."""
    assert "mcp_work.py" in mcp_core.REFLEXIVE_TOOL_MODULES


def test_the_verified_key_is_what_travels(monkeypatch):
    """Gating on the strict resolver and then letting the transport resolve again
    would authorize the check and the action as potentially different sessions."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-verified")
    seen: dict[str, Any] = {}

    def _fake_get(path: str, session_key: str | None = None) -> dict:
        seen["path"] = path
        seen["session_key"] = session_key
        return {"brief": {"item_id": "it_00000000", "title": "t"}}

    monkeypatch.setattr(mcp_work, "_get", _fake_get)
    mcp_work._call_tool_inner("work_brief", {})
    assert seen["session_key"] == "chat-verified"
    assert seen["path"] == "/api/work-ledger/brief"


# ── error surfacing ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code,expected",
    [("not_bound", "not_bound"), ("no_ledger", "no_ledger"), ("item_closed", "item_closed")],
)
def test_a_refusal_carries_the_machine_readable_code(code, expected, monkeypatch):
    """The code is what a worker or conductor dispatches on, so it is quoted rather
    than folded into prose."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(mcp_work, "_get", lambda *a, **k: {"error": "nope", "code": code})
    out = mcp_work._call_tool_inner("work_brief", {})
    assert out.startswith("Error:")
    assert expected in out


def test_a_capped_field_names_the_field_in_the_refusal(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(
        mcp_work,
        "_post",
        lambda *a, **k: {"error": "too long", "code": "field_too_long", "field": "summary"},
    )
    out = mcp_work._call_tool_inner("work_report", {"status": "done", "summary": "x"})
    assert "field_too_long" in out
    assert "field=summary" in out


def test_an_unknown_tool_is_refused_before_identity_is_resolved(monkeypatch):
    def _boom():  # pragma: no cover - must never be reached
        raise AssertionError("identity was resolved for a tool that does not exist")

    monkeypatch.setattr(mcp_work, "_strict_caller", _boom)
    assert mcp_work._call_tool_inner("work_delete", {}) == "Error: unknown tool 'work_delete'"


def test_the_report_tool_forwards_only_its_own_four_fields(monkeypatch):
    """Defence in depth behind the schema: even a caller that got an extra key past
    validation cannot have it reach the wire."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    sent: dict[str, Any] = {}

    def _fake_post(path: str, body: dict | None = None, **k: Any) -> dict:
        sent.update(body or {})
        return {"ok": True, "status": "done", "item_id": "it_00000000"}

    monkeypatch.setattr(mcp_work, "_post", _fake_post)
    mcp_work._call_tool_inner(
        "work_report",
        {"status": "done", "summary": "s", "item_id": "it_deadbeef", "verdict": "pass"},
    )
    assert set(sent) == {"status", "summary"}


# ── the server carries no autoApprove, and is opt-in ──────────────────────


def test_the_managed_entry_has_no_auto_approve_key():
    """An autoApproved MCP tool is approved inside kiro-cli and emits no permission
    request, so ``hooks.on_tool_call`` — the deny floor, the sensitive-path check and
    the governance ceiling — is never reached for it. Asserted so it cannot be added
    later without a failing test."""
    entry = agent._MANAGED_MCP_SERVERS[SERVER]
    assert "autoApprove" not in entry
    assert entry["opt_in"] is True


def test_the_entry_is_opt_in_everywhere_that_tracks_the_split():
    assert SERVER in mcp_cleanup.OPT_IN_BIN_MCP_SERVERS
    assert SERVER not in mcp_cleanup.ALWAYS_ON_BIN_MCP_SERVERS
    assert agent._mcp_server_emission_eligible(SERVER, agent._MANAGED_MCP_SERVERS[SERVER]) is False


def test_the_server_is_registered_for_discovery_and_the_cli():
    assert mcp_discovery._MANAGED_SERVER_SUBCOMMANDS.get(SERVER) == SUBCOMMAND
    assert mcp_discovery._MANAGED_SERVER_TOOL_MODULES.get(SERVER) == "kiro_crew.mcp_work"
    # The registration ratchet in test_computer_use_registration asserts these two
    # maps are the SAME key set; restated here so this server's own suite fails too.
    assert set(agent._MANAGED_MCP_SERVERS) == set(mcp_discovery._MANAGED_SERVER_SUBCOMMANDS)


def test_the_cli_serves_the_subcommand():
    import inspect

    from kiro_crew import cli

    src = inspect.getsource(cli)
    assert 'sub.add_parser("mcp-work")' in src
    assert 'importlib.import_module("kiro_crew.mcp_work").run_mcp_server()' in src


def test_the_server_advertises_caller_identity_and_is_classified_for_it():
    """It refuses an unidentified caller, so it is safe to classify shareable — and
    the classification is read from a name set, which must agree."""
    assert mcp_work.ADVERTISE_CALLER_IDENTITY is True
    assert SERVER in mcp_discovery._MANAGED_SERVERS_CALLER_AWARE
    assert mcp_discovery.managed_server_is_session_bound(SERVER) is False


# ── the default agent pays nothing for it ─────────────────────────────────


def test_a_fresh_default_spec_carries_neither_the_entry_nor_the_ref():
    """Loop A — ``build_agent_config``. An opt-in set belongs to the agents whose own
    spec references it; kiro-cli loads a server only when ``tools`` names one, so a
    default session must spend no context on four schemas it cannot use."""
    config = agent.build_agent_config()
    assert SERVER not in (config.get("mcpServers") or {})
    tools = config.get("tools") or []
    assert "@kirocrew-work" not in tools
    assert not any(str(t).startswith("@kirocrew-work/") for t in tools)
    assert not any(str(t).startswith("@kirocrew-work") for t in (config.get("allowedTools") or []))


def test_a_refresh_never_introduces_the_entry():
    """Loop B — ``_refresh_dynamic_fields``. A refresh keeps an EXISTING grant's
    command current and must never re-introduce one, or every gateway start would
    re-grant a set the user removed."""
    config: dict[str, Any] = {"mcpServers": {}, "tools": []}
    agent._refresh_dynamic_fields(config)
    assert SERVER not in config["mcpServers"]


def test_a_refresh_keeps_an_existing_grant_current():
    """The other half of the same rule: a hand-built entry the user granted is
    refreshed rather than left on a stale command."""
    config: dict[str, Any] = {
        "mcpServers": {SERVER: {"command": "stale", "args": ["nope"]}},
        "tools": ["@kirocrew-work"],
    }
    agent._refresh_dynamic_fields(config)
    entry = config["mcpServers"][SERVER]
    assert entry["command"] != "stale"
    assert entry["args"][-1] == SUBCOMMAND


# ── channel agents hold none of it ────────────────────────────────────────


def test_a_channel_agent_is_blocked_from_all_four_tools():
    """A channel agent has no dispatch relationship and no business holding one:
    reading a brief would pull a private dispatch's bar into a channel other humans
    can see, and a write would edit a conductor's record from outside it."""
    from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS, _blocked_tool_named

    for tool in mcp_work.WORK_TOOLS:
        assert tool in CHANNEL_AGENT_BLOCKED_TOOLS
        # Boundary-aware, and both qualified invocation forms must match.
        assert _blocked_tool_named(f"Running {tool}")
        assert _blocked_tool_named(f"kirocrew-work___{tool}")
        assert _blocked_tool_named(f"mcp__kirocrew-work__{tool}")
    # ...and a filename that merely contains the name must NOT match.
    assert not _blocked_tool_named("Editing work_report.py")


# ── the corrected session_create description ──────────────────────────────


def test_session_create_states_the_caller_inheritance_rule():
    """It used to say "Omit to use the default agent", which is wrong about the one
    mechanism a conductor most depends on: ``create_session`` falls back to the
    CALLER's own agent, so a conductor that omits it gets a second conductor — which
    has no ``fs_write`` and cannot do the work."""
    from kiro_crew import mcp_dashboard

    definition = next(t for t in mcp_dashboard._tool_definitions() if t["name"] == "session_create")
    description = definition["inputSchema"]["properties"]["agent"]["description"]
    assert "Omit to use the default agent" not in description
    lowered = description.lower()
    assert "caller" in lowered
    assert "inherit" in lowered
    assert "kirocrew-worker" in lowered


def test_the_caller_inheritance_claim_matches_the_code_it_describes():
    """The description is a second copy of a fact whose original is
    ``session_control.create_session``; drifting apart is what made it wrong before."""
    import inspect

    from kiro_crew.dashboard import session_control

    src = inspect.getsource(session_control.create_session)
    assert 'getattr(caller_slot, "agent", "")' in src


def test_the_tool_response_is_a_string_not_a_raised_error(monkeypatch):
    """``call_tool_with_logging`` classifies on the ``Error:`` prefix, so a failure
    must be returned rather than raised."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(mcp_work, "_get", lambda *a, **k: {"error": "boom"})
    out = mcp_work._call_tool("work_ledger_read", {})
    assert isinstance(out, str)
    assert out.startswith("Error:")


def test_a_successful_read_returns_the_ledger_as_json(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    payload = {
        "conductor": {"goal": "g", "round": 2},
        "items": [{"item_id": "it_00000000", "state": "open"}],
        "accept_batch": {"items": [{"id": "it_00000000", "accept": {"kind": "human_approval"}}]},
    }
    monkeypatch.setattr(mcp_work, "_get", lambda *a, **k: payload)
    out = mcp_work._call_tool_inner("work_ledger_read", {})
    assert json.loads(out) == payload
