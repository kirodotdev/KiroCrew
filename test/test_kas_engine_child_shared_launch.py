"""A server a person's own agent sets up as the default agent does is the same server.

The agent-spec rebuild rewrites the default agent's server declaration and no other
agent's: a bare ``command`` becomes the absolute path it resolved to, beside the
rebuild's record of both values. So the default agent's entry never reads byte for
byte like an own agent's that was written the same way. The two are compared like
with like (as stored, and as written), and a match makes the own agent's entry
declare the default agent's own: both launch one thing, its ask on every MCP call
spares that server, and the session agent's grant on its tool reaches the call.
Every other declaration of the name stays another server, and stays asked.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from kas_engine_child_helpers import (
    _DEFAULTS,
    _agents_dir,
    _batch,
    _default_spec,
    _handle,
    _own,
    _setting,
    _write,
)

from kiro_crew.acp import kas_engine_child_grants as grants
from kiro_crew.acp import kas_subagent_targets as targets
from kiro_crew.acp._dispatch import build_session_new_params
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.mcp_provenance import DERIVED_KEY

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

_MCP_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "kas_native_subagents"
    / "child-mcp-permission-request.jsonl"
)
[_MCP_CALL] = [
    r
    for r in map(json.loads, _MCP_FIXTURE.read_text(encoding="utf-8").splitlines())
    if "label" in r
]
_ASK = {"capability": "mcp", "effect": "ask"}
_ARGS = ["/srv/own.py", "--port", "0"]
#: What a person writes, and what the rebuild leaves in the default agent's file for it.
_WRITTEN = {"command": "own-srv", "args": _ARGS}
_RESOLVED = "/opt/tools/bin/own-srv"
_REBUILT = {
    "command": _RESOLVED,
    "args": _ARGS,
    DERIVED_KEY: {"from": "own-srv", "emitted": _RESOLVED},
}


def _rules(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return (entry.get("permissions") or {}).get("rules") or []


def _entry(batch: list[dict[str, Any]], name: str) -> dict[str, Any]:
    [entry] = [a for a in batch if a["id"] == name]
    return entry


async def _mounted(
    agents_dir: Path,
    setting,
    theirs: dict[str, Any],
    mine: dict[str, Any],
    *,
    name: str = "own-srv",
    **parent: Any,
) -> list[dict[str, Any]]:
    """The default agent declaring *name* as *theirs*, ``own-server`` ``own-srv`` as *mine*."""
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    _write(
        agents_dir,
        _default_spec(
            tools=[*shipped["tools"], f"@{name}"],
            allowedTools=[*shipped["allowedTools"], "execute_bash", f"@{name}"],
            mcpServers={name: theirs},
            **parent,
        ),
    )
    _write(
        agents_dir,
        _own("own-server", tools=["fs_read", "@own-srv"], mcpServers={"own-srv": mine}),
    )
    setting("on")
    batch = await _batch()
    assert batch.helper_parent == "kirocrew" and "own-server" in {a["id"] for a in batch}
    return batch


def _shared(batch: list[dict[str, Any]]) -> frozenset[str]:
    return grants.shared_servers_of(_entry(batch, "own-server"), _entry(batch, "kirocrew"))


# ── set up alike: one server ──────────────────────────────────────────────────

_ALIKE: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "a bare command the rebuild resolved": (_REBUILT, _WRITTEN),
    "the absolute command the rebuild resolved to": (
        _REBUILT,
        {"command": _RESOLVED, "args": _ARGS},
    ),
    "an absolute command in both files": (
        {"command": _RESOLVED, "args": _ARGS},
        {"command": _RESOLVED, "args": _ARGS},
    ),
    "the default agent's entry copied whole, record included": (_REBUILT, _REBUILT),
    "a url with no headers in both files": (
        {"url": "https://mcp.example.com/own"},
        {"url": "https://mcp.example.com/own"},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_ALIKE))
async def test_a_server_both_files_set_up_alike_is_one_server(agents_dir, setting, case):
    theirs, mine = _ALIKE[case]
    batch = await _mounted(agents_dir, setting, theirs, mine)
    child, parent = _entry(batch, "own-server"), _entry(batch, "kirocrew")

    assert _shared(batch) == frozenset({"own-srv"})
    # As ``session/new`` carries them: one declaration, so one process.
    params = build_session_new_params("/w", mcp_servers=[], kas_custom_agents=list(batch))
    wire = {a["id"]: a["mcpServers"] for a in params["_meta"]["kiro"]["customAgents"]}
    assert json.dumps(wire["own-server"]["own-srv"], sort_keys=True) == json.dumps(
        wire["kirocrew"]["own-srv"], sort_keys=True
    )
    assert child["mcpServers"]["own-srv"] is not parent["mcpServers"]["own-srv"]
    asks = [r for r in _rules(child) if r.get("capability") == "mcp" and r.get("effect") == "ask"]
    assert {**_ASK, "exclude": ["own-srv/*"]} in asks and _ASK not in asks


@pytest.mark.asyncio
async def test_the_sessions_grant_on_its_tool_reaches_the_own_agents_call(
    agents_dir, setting, tmp_path
):
    """The request a real engine sent for ``@own-srv/p6_lookup``, with the default
    agent's server as the rebuild leaves it and the own agent's as it was written."""
    batch = await _mounted(agents_dir, setting, _REBUILT, _WRITTEN)
    handle = _handle(batch, tmp_path, sid=_MCP_CALL["request"]["sessionId"])
    ref = "@own-srv/p6_lookup"
    handle.kas_crew_answered = frozenset({*handle.kas_crew_answered, ref})
    handle.kas_hook_held = frozenset({ref})
    record = copy.deepcopy(_MCP_CALL)
    handle._handle_update(
        JsonRpcMessage(
            method="session/update",
            params={"sessionId": record["request"]["sessionId"], "update": record["tool_call"]},
        )
    )

    event = handle._build_permission_event(
        JsonRpcMessage(id=11, method="session/request_permission", params=record["request"])
    )

    assert event is not None and event.native_child_call
    assert ref in event.spec_granted_builtins and ref in event.spec_hook_held


# ── set up another way: another server, still asked ───────────────────────────

_HEADERS = {"X-Mode": "x"}
_APART: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "another bare command": (_REBUILT, {"command": "other-srv", "args": _ARGS}),
    "another absolute command": (_REBUILT, {"command": "/elsewhere/own-srv", "args": _ARGS}),
    "other arguments": (_REBUILT, {"command": "own-srv", "args": ["/srv/mine.py"]}),
    "no arguments": (_REBUILT, {"command": "own-srv"}),
    "an environment of its own": (_REBUILT, {**_WRITTEN, "env": {"MODE": "x"}}),
    "another timeout": (_REBUILT, {**_WRITTEN, "timeout": 5000}),
    "a url where the default agent runs a command": (_REBUILT, {"url": "https://x.example.com"}),
    "a record of its own naming the same source": (
        _REBUILT,
        {
            "command": "/elsewhere/own-srv",
            "args": _ARGS,
            DERIVED_KEY: {"from": "own-srv", "emitted": "/elsewhere/own-srv"},
        },
    ),
    "a command the person changed after the rebuild": (
        {**_REBUILT, "command": "/hand/edited/own-srv"},
        _WRITTEN,
    ),
    "a record that is not readable": ({**_REBUILT, DERIVED_KEY: {"from": "own-srv"}}, _WRITTEN),
    "the same environment in both files": (
        {**_REBUILT, "env": {"MODE": "x"}},
        {**_WRITTEN, "env": {"MODE": "x"}},
    ),
    "a url with the same headers in both files": (
        {"url": "https://mcp.example.com/own", "headers": _HEADERS},
        {"url": "https://mcp.example.com/own", "headers": _HEADERS},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_APART))
async def test_a_server_set_up_another_way_stays_asked(agents_dir, setting, case):
    theirs, mine = _APART[case]
    batch = await _mounted(agents_dir, setting, theirs, mine)
    child = _entry(batch, "own-server")

    assert _shared(batch) == frozenset()
    assert _ASK in _rules(child)


@pytest.mark.asyncio
async def test_a_server_only_the_own_agent_declares_stays_asked(agents_dir, setting):
    """The default agent sets the same thing up under another name: the name is the
    own agent's alone, and what it launches is never looked for under another."""
    batch = await _mounted(agents_dir, setting, _REBUILT, _WRITTEN, name="another")
    child = _entry(batch, "own-server")

    assert "own-srv" not in _entry(batch, "kirocrew")["mcpServers"]
    assert _shared(batch) == frozenset()
    assert child["mcpServers"]["own-srv"] == _WRITTEN and _ASK in _rules(child)


@pytest.mark.asyncio
async def test_a_server_the_default_agent_withholds_a_tool_of_keeps_its_own_declaration(
    agents_dir, setting
):
    batch = await _mounted(agents_dir, setting, _REBUILT, _WRITTEN, excludedTools=["@own-srv/drop"])
    child = _entry(batch, "own-server")

    assert _shared(batch) == frozenset()
    assert child["mcpServers"]["own-srv"] == _WRITTEN and _ASK in _rules(child)


def test_like_is_compared_with_like():
    assert grants.set_up_alike(_WRITTEN, _REBUILT)
    assert grants.set_up_alike({"command": _RESOLVED, "args": _ARGS}, _REBUILT)
    assert grants.set_up_alike(copy.deepcopy(_REBUILT), _REBUILT)
    # The name alone, and anything that is not a declaration, never match.
    assert not grants.set_up_alike({}, _REBUILT)
    assert not grants.set_up_alike(None, _REBUILT) and not grants.set_up_alike(_WRITTEN, None)
    assert not grants.set_up_alike(_WRITTEN, "own-srv")
    # The other side's record is the only one read.
    forged = {
        "command": "/evil",
        "args": _ARGS,
        DERIVED_KEY: {"from": "own-srv", "emitted": "/evil"},
    }
    assert not grants.set_up_alike(forged, _REBUILT)
    assert not grants.set_up_alike(forged, _WRITTEN)


def test_holding_the_parents_servers_changes_only_what_is_set_up_alike():
    parent = {"mcpServers": {"same": _REBUILT, "apart": _REBUILT, "kept": _REBUILT}}
    child = {
        "id": "c",
        "mcpServers": {
            "same": _WRITTEN,
            "apart": {"command": "own-srv"},
            "kept": copy.deepcopy(_REBUILT),
            "mine": _WRITTEN,
        },
    }
    held = grants.holding_parents_servers(child, parent)

    assert held["mcpServers"] == {**child["mcpServers"], "same": _REBUILT}
    assert held["mcpServers"]["same"] is not _REBUILT and child["mcpServers"]["same"] is _WRITTEN
    # Nothing to change: the entry itself comes back.
    assert grants.holding_parents_servers(held, parent) is held
    assert grants.holding_parents_servers({"id": "c"}, parent) == {"id": "c"}
    assert grants.holding_parents_servers(child, {"id": "p"}) is child


# ── a pooled server: the same comparison decides whether the agent registers ──


@pytest.mark.parametrize(
    "mine, elsewhere",
    [
        (_WRITTEN, []),
        ({"command": _RESOLVED, "args": _ARGS}, []),
        ({**_WRITTEN, "disabledTools": ["x"], "type": "stdio"}, []),
        ({"command": "other-srv", "args": _ARGS}, ["own-srv"]),
        ({**_WRITTEN, "env": {"MODE": "x"}}, ["own-srv"]),
        (
            {
                "command": "/evil",
                "args": _ARGS,
                DERIVED_KEY: {"from": "own-srv", "emitted": "/evil"},
            },
            ["own-srv"],
        ),
    ],
    ids=[
        "as written",
        "the resolved command",
        "keys that launch nothing",
        "another command",
        "an environment of its own",
        "a record of its own",
    ],
)
def test_a_pooled_server_set_up_alike_does_not_refuse_the_agent(mine, elsewhere):
    spec = {"mcpServers": {"own-srv": mine}}
    stubs = frozenset({"own-srv"})

    assert targets._pooled_elsewhere(spec, {"own-srv": _REBUILT}, stubs) == elsewhere
    # A stub the session's own spec does not declare still refuses it.
    assert targets._pooled_elsewhere(spec, {}, stubs) == ["own-srv"]
