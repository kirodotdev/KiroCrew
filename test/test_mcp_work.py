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


def test_all_six_tools_are_advertised_to_every_caller():
    """One list regardless of identity: a second-level conductor legitimately
    reaches all six, and a list that varied by caller would make a worker's
    missing conductor tools look like a broken install rather than a refusal."""
    names = [t["name"] for t in mcp_work._list_tools()]
    assert names == list(mcp_work.WORK_TOOLS)
    assert set(names) == {
        "work_brief",
        "work_report",
        "work_ledger_read",
        "work_ledger_evaluate",
        "work_ledger_record",
        "work_ledger_rebuild",
    }


def test_every_tool_has_a_registered_schema():
    """A tool absent from its server's registry has its args passed through raw."""
    assert set(MCP_WORK_SCHEMAS) == set(mcp_work.WORK_TOOLS)
    for name, schema in MCP_WORK_SCHEMAS.items():
        assert schema.tool_name == name


def test_the_brief_tool_declares_an_empty_schema():
    """Registered-but-empty, not unregistered: an unregistered schema admits an
    unexpected argument, an empty registered one rejects it."""
    definition = next(t for t in mcp_work._list_tools() if t["name"] == "work_brief")
    assert definition["inputSchema"]["properties"] == {}
    assert "required" not in definition["inputSchema"]
    assert MCP_WORK_SCHEMAS["work_brief"].fields == []


def test_work_ledger_evaluate_declares_only_a_required_item_id():
    definition = next(
        tool for tool in mcp_work._list_tools() if tool["name"] == "work_ledger_evaluate"
    )
    assert set(definition["inputSchema"]["properties"]) == {"item_id"}
    assert definition["inputSchema"]["required"] == ["item_id"]
    assert definition["inputSchema"]["additionalProperties"] is False
    fields = MCP_WORK_SCHEMAS["work_ledger_evaluate"].fields
    assert [field.name for field in fields] == ["item_id"]
    assert fields[0].required is True


def test_work_ledger_read_advertises_the_five_optional_filters():
    """Every parameter narrows or shapes the read and none is required, so a
    conductor that passes nothing still gets its whole board — and the advertised
    schema, the validation schema and the forwarded fields name the same five."""
    definition = next(t for t in mcp_work._list_tools() if t["name"] == "work_ledger_read")
    props = definition["inputSchema"]["properties"]
    assert set(props) == {"events", "item_id", "state", "since", "compact"}
    assert "required" not in definition["inputSchema"]
    assert {f.name for f in MCP_WORK_SCHEMAS["work_ledger_read"].fields} == set(props)
    assert set(mcp_work._READ_FIELDS) == set(props)
    assert all(not f.required for f in MCP_WORK_SCHEMAS["work_ledger_read"].fields)
    # The advertised bounds are the validated bounds.
    events = next(f for f in MCP_WORK_SCHEMAS["work_ledger_read"].fields if f.name == "events")
    assert (props["events"]["minimum"], props["events"]["maximum"]) == (
        events.min_val,
        events.max_val,
    )
    state = next(f for f in MCP_WORK_SCHEMAS["work_ledger_read"].fields if f.name == "state")
    assert set(props["state"]["enum"]) == state.allowed


def test_work_ledger_read_description_states_the_shape_a_conductor_relies_on():
    """What an argument-less read still is, how each argument narrows it, and the
    truncation marker are what a conductor plans its patrol read around, so the
    description must say them — and must not claim a changed default."""
    definition = next(t for t in mcp_work._list_tools() if t["name"] == "work_ledger_read")
    text = definition["description"]
    assert "last 20 events" in text
    assert "NARROWS" in text and "events=<n>" in text
    assert "compact=true" in text
    assert "truncated=true" in text
    assert "closed first, then open oldest-created" in text
    assert "NEWEST FIRST" not in text and "last 5 events" not in text
    assert "Takes no arguments" not in text
    events = definition["inputSchema"]["properties"]["events"]["description"]
    assert "default and max 20" in events


@pytest.mark.parametrize(
    "args",
    [
        {"events": 21},
        {"events": -1},
        {"events": True},
        {"item_id": "it_zz"},
        {"item_id": "../etc"},
        {"state": "closed"},
        {"since": "x" * 41},
        {"compact": "yes"},
        {"status": "done"},
    ],
)
def test_work_ledger_read_refuses_an_out_of_shape_filter(args):
    """Refused at the schema, before any identity is resolved or wire is touched."""
    from kiro_crew.validation import ValidationError

    with pytest.raises(ValidationError):
        mcp_work._validate_args("work_ledger_read", args)


def test_work_ledger_read_forwards_only_known_filters_as_query(monkeypatch):
    """The filters travel as a query string on the same GET; an unknown key that
    got past validation still never reaches the wire, and a bool is spelled the
    way the route reads it back."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    seen: dict[str, Any] = {}

    def _fake_get(path: str, session_key: str | None = None, **k: Any) -> dict:
        seen["path"] = path
        seen["session_key"] = session_key
        return {"conductor": {}, "items": []}

    monkeypatch.setattr(mcp_work, "_get", _fake_get)
    mcp_work._call_tool_inner(
        "work_ledger_read",
        {
            "events": 3,
            "item_id": "it_0000abcd",
            "state": "open",
            "since": "2026-01-02T03:04:05+00:00",
            "compact": True,
            "verdict": "pass",
        },
    )
    from urllib.parse import parse_qs, urlsplit

    split = urlsplit(seen["path"])
    assert split.path == mcp_work._READ_PATH
    assert parse_qs(split.query, keep_blank_values=True) == {
        "events": ["3"],
        "item_id": ["it_0000abcd"],
        "state": ["open"],
        "since": ["2026-01-02T03:04:05+00:00"],
        "compact": ["true"],
    }
    assert seen["session_key"] == "chat-x"


def test_work_ledger_read_with_no_filters_hits_the_bare_path(monkeypatch):
    """No arguments is still the common call, and it must not grow a trailing ``?``."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    seen: dict[str, Any] = {}

    def _fake_get(path: str, session_key: str | None = None, **k: Any) -> dict:
        seen["path"] = path
        return {"conductor": {}, "items": []}

    monkeypatch.setattr(mcp_work, "_get", _fake_get)
    mcp_work._call_tool_inner("work_ledger_read", {})
    assert seen["path"] == mcp_work._READ_PATH
    mcp_work._call_tool_inner("work_ledger_read", {"compact": False})
    assert seen["path"] == f"{mcp_work._READ_PATH}?compact=false"


def test_work_ledger_evaluate_uses_its_route_deadline(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    seen: dict[str, Any] = {}

    def _fake_post(path: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        seen.update(path=path, body=body, **kwargs)
        return {"item_id": "it_0000abcd", "verdict": "pass", "evaluation": {}}

    monkeypatch.setattr(mcp_work, "_post", _fake_post)
    mcp_work._call_tool_inner("work_ledger_evaluate", {"item_id": "it_0000abcd"})
    assert seen == {
        "path": mcp_work._EVALUATE_PATH,
        "body": {"item_id": "it_0000abcd"},
        "timeout": mcp_work._EVALUATE_TIMEOUT_SECS,
        "session_key": "chat-x",
    }
    assert seen["timeout"] > 30


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
    assert mcp_work.CONDUCTOR_TOOLS == (
        "work_ledger_read",
        "work_ledger_evaluate",
        "work_ledger_rebuild",
        "work_ledger_record",
    )
    assert mcp_work.WORK_TOOLS == mcp_work.WORKER_TOOLS + mcp_work.CONDUCTOR_TOOLS


# ── identity: strict, and never the /proc walk ────────────────────────────


@pytest.mark.parametrize("tool", list(mcp_work.WORK_TOOLS))
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


def test_a_channel_agent_is_blocked_from_all_work_tools():
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
    """The description must state the caller-inheritance rule, not "Omit to use the
    default agent": ``create_session`` falls back to the CALLER's own agent, so a
    conductor that omits it gets a second conductor — which has no ``fs_write``
    and cannot do the work."""
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


# ── the two conductor writes and the rebuild, as the wire sees them ───────


def test_the_rebuild_tool_posts_an_empty_body_and_reports_the_counts(monkeypatch):
    """A rebuild carries no arguments: the route folds the caller's own record."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    seen: dict[str, Any] = {}

    def _fake_post(path: str, body: dict | None = None, **k: Any) -> dict:
        seen["path"] = path
        seen["body"] = body
        seen["session_key"] = k.get("session_key")
        return {"ok": True, "items": 3, "events": 7, "removed": 1}

    monkeypatch.setattr(mcp_work, "_post", _fake_post)
    out = mcp_work._call_tool_inner("work_ledger_rebuild", {})
    assert seen == {"path": mcp_work._REBUILD_PATH, "body": {}, "session_key": "chat-x"}
    assert out == "Rebuilt the work ledger from the crew log. items=3 events=7 removed=1"


def test_a_refused_rebuild_quotes_the_store_code(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(
        mcp_work,
        "_post",
        lambda *a, **k: {"error": "the log is missing a unit", "code": "crew_log_incomplete"},
    )
    out = mcp_work._call_tool_inner("work_ledger_rebuild", {})
    assert out == (
        "Error: could not rebuild the work ledger: the log is missing a unit "
        "[crew_log_incomplete]"
    )


def test_the_record_tool_forwards_only_the_registered_fields(monkeypatch):
    """Same defence as the worker report: a stray key never reaches the wire."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    sent: dict[str, Any] = {}

    def _fake_post(path: str, body: dict | None = None, **k: Any) -> dict:
        sent["path"] = path
        sent.update(body or {})
        return {"ok": True, "action": "create", "item": {"item_id": "it_00000001"}}

    monkeypatch.setattr(mcp_work, "_post", _fake_post)
    mcp_work._call_tool_inner(
        "work_ledger_record",
        {"action": "create", "title": "t", "acceptance": "a", "status": "done", "pr": "x"},
    )
    assert sent.pop("path") == mcp_work._RECORD_PATH
    assert set(sent) == {"action", "title", "acceptance"}


def test_an_item_write_reports_the_committed_item(monkeypatch):
    """The reply quotes the store's committed fields, not the caller's request."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(
        mcp_work,
        "_post",
        lambda *a, **k: {
            "ok": True,
            "action": "decide",
            "item": {"item_id": "it_00000002", "state": "closed", "verdict": "pass"},
        },
    )
    out = mcp_work._call_tool_inner(
        "work_ledger_record", {"action": "decide", "item_id": "it_00000002", "verdict": "pass"}
    )
    assert out == "Recorded decide. item=it_00000002 state=closed verdict=pass"


def test_an_item_write_with_unset_fields_says_so(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(mcp_work, "_post", lambda *a, **k: {"ok": True, "item": {}})
    # An empty item dict is falsy: the reply falls through to the conductor line.
    out = mcp_work._call_tool_inner("work_ledger_record", {"action": "create", "title": "t"})
    assert out == "Recorded create. round=(unset)"
    monkeypatch.setattr(
        mcp_work, "_post", lambda *a, **k: {"ok": True, "action": "create", "item": {"x": 1}}
    )
    out = mcp_work._call_tool_inner("work_ledger_record", {"action": "create", "title": "t"})
    assert out == "Recorded create. item=(unknown) state=(unset) verdict=(none)"


def test_a_conductor_write_reports_the_round(monkeypatch):
    """``goal`` and ``round`` write the conductor record and return no item."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(
        mcp_work,
        "_post",
        lambda *a, **k: {"ok": True, "action": "round", "conductor": {"round": 4}},
    )
    out = mcp_work._call_tool_inner("work_ledger_record", {"action": "round", "round": 4})
    assert out == "Recorded round. round=4"


def test_a_refused_record_carries_the_code_and_field(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(
        mcp_work,
        "_post",
        lambda *a, **k: {"error": "too long", "code": "field_too_long", "field": "title"},
    )
    out = mcp_work._call_tool_inner("work_ledger_record", {"action": "create", "title": "t"})
    assert out == "Error: could not write the work ledger: too long [field_too_long, field=title]"


def test_arguments_for_an_unregistered_schema_pass_through_unchanged(monkeypatch):
    """The registry is complete today; the fallback is what keeps a future tool
    added without a schema from being silently stripped to nothing."""
    monkeypatch.setattr(mcp_work, "MCP_WORK_SCHEMAS", {})
    args = {"anything": 1}
    assert mcp_work._validate_args("work_brief", args) is args


def test_run_mcp_server_serves_this_module_and_advertises_identity(monkeypatch):
    seen: dict[str, Any] = {}

    def _fake_loop(name: str, version: str, list_tools: Any, call_tool: Any, **kw: Any) -> None:
        seen.update(name=name, version=version, list_tools=list_tools, call_tool=call_tool, **kw)

    monkeypatch.setattr(mcp_work, "run_mcp_stdio_loop", _fake_loop)
    mcp_work.run_mcp_server()
    assert seen["name"] == SERVER
    assert seen["version"] == mcp_work.SERVER_VERSION
    assert seen["list_tools"] is mcp_work._list_tools
    assert seen["call_tool"] is mcp_work._call_tool
    assert seen["advertise_caller_identity"] is True


# ── the read budget, measured on what the tool layer delivers ─────────────


def _fat_board(n: int = 8, *, chars: int = 13_000) -> dict[str, Any]:
    """A board of *n* open items whose events make the whole read ~``n * chars``."""
    items = []
    for i in range(n):
        items.append(
            {
                "item_id": f"it_{i:08x}",
                "state": "open",
                "status": "progress",
                "title": f"item {i}",
                "created_at": f"2026-09-{i + 1:02d}T10:00:00+00:00",
                "acceptance": {"kind": "pr_checks", "pr": 100 + i, "repo": "o/r"},
                "events": [{"kind": "report", "summary": "x" * (chars // 20)} for _ in range(20)],
            }
        )
    batch = {
        "items": [
            {"id": r["item_id"], "accept": r["acceptance"], "status": r["status"]} for r in items
        ]
    }
    return {"conductor": {"goal": "g", "round": 1}, "items": items, "accept_batch": batch}


def _delivered(monkeypatch: pytest.MonkeyPatch, board: dict[str, Any]) -> str:
    """The text the model receives: the real tool call, then the real egress frame."""
    from kiro_crew.validation import build_tool_response

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(mcp_work, "_get", lambda *a, **k: json.loads(json.dumps(board)))
    out = mcp_work._call_tool("work_ledger_read", {})
    return build_tool_response(out)["content"][0]["text"]


def test_a_110kb_board_delivers_the_newest_item_as_valid_json(monkeypatch):
    """The bug: ~110 KB came back whole, the runtime cut it at 100,000 chars, and the
    newest item -- serialized last -- was lost inside torn JSON."""
    from kiro_crew.validation import MAX_RESPONSE_LEN

    board = _fat_board()
    assert len(json.dumps(board, indent=2)) > 105_000
    text = _delivered(monkeypatch, board)
    assert len(text) <= mcp_work._READ_BUDGET_CHARS < MAX_RESPONSE_LEN
    doc = json.loads(text)
    assert doc["truncated"] is True
    ids = [r["item_id"] for r in doc["items"]]
    assert "it_00000007" in ids
    assert {e["id"] for e in doc["accept_batch"]["items"]} >= {"it_00000007"}


def test_an_under_budget_read_is_untouched(monkeypatch):
    board = _fat_board(2, chars=1_000)
    doc = json.loads(_delivered(monkeypatch, board))
    assert doc == board


def test_closed_rows_are_dropped_before_open_ones():
    board = _fat_board(6, chars=1_000)
    # The two OLDEST rows stay open; the newer ones close. Age alone would drop the
    # open ones first.
    for row in board["items"][2:5]:
        row["state"] = "accepted"
    for row in board["items"]:
        row["title"] = "t" * 6_000
    doc = json.loads(mcp_work._fit_ledger(board, budget=22_000))
    assert len(json.dumps(doc, indent=2, ensure_ascii=False)) <= 22_000
    kept = [r["item_id"] for r in doc["items"]]
    assert set(doc["omitted_items"]) == {"it_00000002", "it_00000003", "it_00000004"}
    assert kept == ["it_00000000", "it_00000001", "it_00000005"]


def test_open_rows_go_oldest_created_first_and_the_newest_stays():
    board = _fat_board(4, chars=1_000)
    for row in board["items"]:
        row["title"] = "t" * 8_000
    board["items"].reverse()  # the store's order is not the drop order
    doc = json.loads(mcp_work._fit_ledger(board, budget=12_000))
    assert [r["item_id"] for r in doc["items"]] == ["it_00000003"]
    assert doc["omitted_items"] == ["it_00000000", "it_00000001", "it_00000002"]


def test_a_board_at_the_item_ceiling_trims_in_few_renders(monkeypatch):
    """Dropping rows bisects the count: a 256-row board must not re-render once
    per dropped row, each render running the whole egress scrub."""
    board = _fat_board(256, chars=1_000)
    for i, row in enumerate(board["items"]):
        row["created_at"] = f"2026-09-01T{i // 60:02d}:{i % 60:02d}:00+00:00"
    calls = 0
    real = mcp_work._render

    def _counting(doc: Any) -> str:
        nonlocal calls
        calls += 1
        return real(doc)

    monkeypatch.setattr(mcp_work, "_render", _counting)
    doc = json.loads(mcp_work._fit_ledger(board, budget=20_000))
    assert calls <= 16
    assert doc["items"][-1]["item_id"] == "it_000000ff"
    assert len(doc["items"]) + len(doc["omitted_items"]) == 256


def test_one_oversized_acceptance_does_not_evict_the_other_rows():
    """Elision runs before any row is dropped, so only the bloated bar pays."""
    board = _fat_board(8, chars=1_000)
    huge = {"kind": "pr_checks", "pr": 1, "repo": "o/r", "notes": "n" * 60_000}
    board["items"][0]["acceptance"] = huge
    board["accept_batch"]["items"][0]["accept"] = huge
    doc = json.loads(mcp_work._fit_ledger(board, budget=30_000))
    assert len(doc["items"]) == 8
    assert "omitted_items" not in doc
    assert doc["elided_acceptance_for"] == ["it_00000000"]
    assert doc["items"][0]["acceptance"]["elided"] is True
    assert doc["accept_batch"]["items"][0]["accept"]["elided"] is True
    assert doc["items"][1]["acceptance"] == board["items"][1]["acceptance"]


def test_the_measured_size_is_the_delivered_size(monkeypatch):
    """The fitter measures with the same redact + sanitize the egress applies, so
    what it counted is what the model gets -- byte for byte, not an estimate."""
    board = _fat_board()
    # Content that redaction and NFC normalization both change the length of.
    board["items"][-1]["title"] = "token=ghp_" + "a" * 36 + " e\u0301"
    fitted = mcp_work._fit_ledger(json.loads(json.dumps(board)))
    assert _delivered(monkeypatch, board) == fitted
    assert len(fitted) <= mcp_work._READ_BUDGET_CHARS


def test_a_board_nothing_can_trim_falls_back_to_one_envelope():
    board = _fat_board(1, chars=0)
    board["conductor"]["goal"] = "g" * 50_000
    doc = json.loads(mcp_work._fit_ledger(board, budget=10_000))
    assert doc["unfittable"] is True
    assert doc["item_count"] == 1
    assert doc["omitted_items"] == ["it_00000000"]
