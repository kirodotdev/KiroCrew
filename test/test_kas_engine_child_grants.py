"""A person's own agent, run as an engine sub-agent, is governed as kiro-cli governs one.

Its parent's grants answer its calls and its own answer nothing, where the engine
names it on the request as a direct child of the session's agent. A grant on an
MCP tool reaches it only for a server it declares byte for byte as its parent
does; its ``ask`` on every MCP call spares only those. An engine that names no
one (0.66.117) leaves it asked. The requests are the ones a real engine sent
(``kas_engine_child_helpers`` and ``child-mcp-permission-request.jsonl``).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from kas_engine_child_helpers import (
    _DEFAULTS,
    _DIRECT_HELPER,
    _DIRECT_OWN,
    _HELPER,
    _RECORDED,
    _STAMPED,
    _agents_dir,
    _batch,
    _default_spec,
    _granted,
    _handle,
    _mounted_with_own_agents,
    _own,
    _own_request,
    _replay,
    _setting,
    _sid,
    _verdict,
    _write,
)

from kiro_crew.acp import kas_engine_child_grants as grants
from kiro_crew.acp._dispatch import build_session_new_params
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_files import OWNED_KIRO_AGENT_NAMES
from kiro_crew.hooks import TOOL_AUTO_APPROVE

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
_SAME = {"command": sys.executable, "args": ["/srv/same.py"]}


def _rules(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return (entry.get("permissions") or {}).get("rules") or []


def _entry(batch: list[dict[str, Any]], name: str) -> dict[str, Any]:
    [entry] = [a for a in batch if a["id"] == name]
    return entry


# ── the parent's grants, and not its own ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_persons_agent_the_engine_names_is_judged_as_its_parent(
    agents_dir, setting, tmp_path
):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_OWN))

    own = _own_request(handle, _DIRECT_OWN)
    person = _replay(handle, _DIRECT_OWN)

    assert person.native_child_call
    # Every stamped field its parent's own request carries, less the grants on MCP
    # tools: it shares no server with its parent.
    for name in _STAMPED:
        expected = getattr(own, name)
        if name in ("spec_granted_builtins", "spec_hook_held"):
            expected = frozenset(ref for ref in expected if not ref.startswith("@"))
        assert getattr(person, name) == expected, name
    assert "execute_bash" in person.spec_granted_builtins
    assert _verdict(own)[0] == TOOL_AUTO_APPROVE and _verdict(person) == _verdict(own)


@pytest.mark.asyncio
async def test_its_own_grant_answers_nothing(agents_dir, setting, tmp_path):
    """The parent does not grant the shell and the agent's own file does: asked,
    exactly as its parent is."""
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    _write(agents_dir, _default_spec(allowedTools=shipped["allowedTools"]))
    _write(agents_dir, _own("own-model", allowedTools=["execute_bash"]))
    setting("on")
    batch = await _batch()
    assert batch.helper_parent == "kirocrew" and "own-model" in {a["id"] for a in batch}
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_OWN))

    own = _own_request(handle, _DIRECT_OWN)
    person = _replay(handle, _DIRECT_OWN)

    assert "execute_bash" not in person.spec_granted_builtins
    assert _verdict(own)[0] != TOOL_AUTO_APPROVE and _verdict(person) == _verdict(own)


@pytest.mark.asyncio
async def test_its_own_ask_is_not_answered_by_its_parents_grant(agents_dir, setting, tmp_path):
    """The agent's own file says "ask me" about the shell: its parent's shell grant
    does not answer that, and its parent's other grants still reach it."""
    _write(agents_dir, _default_spec())
    ask = {"capability": "shell", "effect": "ask", "match": ["git push*"]}
    _write(agents_dir, _own("own-model", permissions={"rules": [ask]}))
    setting("on")
    batch = await _batch()
    assert ask in _rules(_entry(batch, "own-model"))
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_OWN))

    own = _own_request(handle, _DIRECT_OWN)
    person = _replay(handle, _DIRECT_OWN)

    assert person.spec_granted_builtins == own.spec_granted_builtins - {"execute_bash"} - {
        ref for ref in own.spec_granted_builtins if ref.startswith("@")
    }
    assert "fs_read" in person.spec_granted_builtins
    assert person.spec_granted_commands == ()
    assert _verdict(own)[0] == TOOL_AUTO_APPROVE and _verdict(person)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_its_own_deny_leaves_the_grant_since_the_engine_refuses_first(
    agents_dir, setting, tmp_path
):
    _write(agents_dir, _default_spec())
    deny = {"capability": "shell", "effect": "deny", "match": ["rm *"]}
    _write(agents_dir, _own("own-model", permissions={"rules": [deny]}))
    setting("on")
    batch = await _batch()
    assert deny in _rules(_entry(batch, "own-model"))
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_OWN))

    assert "execute_bash" in _replay(handle, _DIRECT_OWN).spec_granted_builtins


# ── no name, no grant ─────────────────────────────────────────────────────────

_NOT_AN_OWN_AGENT: dict[str, Any] = {
    "no requester (an engine that names no one)": ...,
    "no sub-agent on the requester": {"sessionId": "s"},
    "no name": {"subagent": {"agentSubtaskId": "st-1", "depth": 1}},
    "a name the batch does not hold": {"subagent": {"agentName": "stranger", "depth": 1}},
    "another case": {"subagent": {"agentName": "OWN-MODEL", "depth": 1}},
    "a name in a list": {"subagent": {"agentName": ["own-model"], "depth": 1}},
    "the session's own agent": {"subagent": {"agentName": "kirocrew", "depth": 1}},
    "one of Kiro Crew's own agents": {
        "subagent": {"agentName": sorted(OWNED_KIRO_AGENT_NAMES - {"kirocrew"})[0], "depth": 1}
    },
    "an agent of the engine's own": {
        "subagent": {"agentName": "general-task-execution", "depth": 1}
    },
    "no depth": {"subagent": {"agentName": "own-model"}},
    "depth 0": {"subagent": {"agentName": "own-model", "depth": 0}},
    "depth as text": {"subagent": {"agentName": "own-model", "depth": "1"}},
    "depth as a float": {"subagent": {"agentName": "own-model", "depth": 1.0}},
    "depth as a boolean": {"subagent": {"agentName": "own-model", "depth": True}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_NOT_AN_OWN_AGENT))
async def test_a_request_that_names_no_registered_agent_as_a_direct_child_is_asked(
    agents_dir, setting, tmp_path, case
):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    request = copy.deepcopy(_RECORDED[_DIRECT_OWN]["request"])
    if _NOT_AN_OWN_AGENT[case] is ...:
        del request["_meta"]["kiro"]["requester"]
    else:
        request["_meta"]["kiro"]["requester"] = _NOT_AN_OWN_AGENT[case]

    event = _replay(_handle(batch, tmp_path, sid=_sid(_DIRECT_OWN)), _DIRECT_OWN, request)

    assert event.native_child_call and not _granted(event)
    assert _verdict(event)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_registration_that_recorded_no_mount_gives_a_persons_agent_no_grant(
    agents_dir, setting, tmp_path
):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    handle = _handle(list(batch), tmp_path, sid=_sid(_DIRECT_OWN))
    assert handle.kas_shipped_helper_parent == ""
    assert not _granted(_replay(handle, _DIRECT_OWN))


@pytest.mark.asyncio
async def test_a_custom_parents_listed_agent_keeps_no_grant(agents_dir, setting, tmp_path):
    """A custom parent's ``availableAgents`` target is not the default agent's mount."""
    _write(
        agents_dir,
        {
            "name": "delegator",
            "prompt": "Delegate.",
            "tools": ["fs_read", "execute_bash", "use_subagent"],
            "allowedTools": ["fs_read", "execute_bash"],
            "toolsSettings": {"subagent": {"availableAgents": ["own-model"]}},
        },
    )
    _write(agents_dir, _own("own-model"))
    setting("on")
    batch = await _batch("delegator")
    assert {a["id"] for a in batch} == {"delegator", "own-model"} and batch.helper_parent == ""
    handle = _handle(batch, tmp_path, agent="delegator", sid=_sid(_DIRECT_OWN))

    assert not _granted(_replay(handle, _DIRECT_OWN))


@pytest.mark.asyncio
async def test_the_helper_keeps_every_grant_of_its_parent(agents_dir, setting, tmp_path):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_HELPER))
    handle.kas_crew_answered = frozenset({"execute_bash", "@srv/write"})

    assert _replay(handle, _DIRECT_HELPER).spec_granted_builtins == handle.kas_crew_answered


# ── MCP: only a server held exactly as the parent holds it ────────────────────


async def _sharing(agents_dir: Path, setting, **child: Any) -> list[dict[str, Any]]:
    """The default agent with a server ``same``, and ``own-server`` as *child* says."""
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    _write(
        agents_dir,
        _default_spec(
            tools=[*shipped["tools"], "@same"],
            allowedTools=[*shipped["allowedTools"], "execute_bash", "@same"],
            mcpServers={"same": _SAME},
        ),
    )
    _write(agents_dir, _own("own-server", **child))
    setting("on")
    batch = await _batch()
    assert batch.helper_parent == "kirocrew" and "own-server" in {a["id"] for a in batch}
    return batch


@pytest.mark.asyncio
async def test_the_ask_spares_only_a_server_declared_as_the_parent_declares_it(agents_dir, setting):
    batch = await _sharing(
        agents_dir,
        setting,
        tools=["fs_read", "@same", "@mine"],
        mcpServers={"same": _SAME, "mine": {"command": sys.executable, "args": ["/srv/mine.py"]}},
    )
    child, parent = _entry(batch, "own-server"), _entry(batch, "kirocrew")

    assert grants.shared_servers_of(child, parent) == frozenset({"same"})
    # As ``session/new`` carries them: the engine hands an equal declaration the
    # process its parent's started.
    params = build_session_new_params("/w", mcp_servers=[], kas_custom_agents=list(batch))
    wire = {a["id"]: a["mcpServers"] for a in params["_meta"]["kiro"]["customAgents"]}
    assert json.dumps(wire["own-server"]["same"], sort_keys=True) == json.dumps(
        wire["kirocrew"]["same"], sort_keys=True
    )
    assert wire["own-server"]["mine"] != wire["kirocrew"]["same"]
    asks = [r for r in _rules(child) if r.get("capability") == "mcp" and r.get("effect") == "ask"]
    assert {**_ASK, "exclude": ["same/*"]} in asks and _ASK not in asks
    # The helper's entry and the parent's are untouched by it.
    assert all(
        "exclude" not in r for r in _rules(_entry(batch, _HELPER)) if r.get("effect") == "ask"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declaration",
    [
        {"command": sys.executable, "args": ["/srv/other.py"]},
        {**_SAME, "env": {"TOKEN": "x"}},
        {**_SAME, "timeout": 5000},
    ],
    ids=["another program", "another environment", "another timeout"],
)
async def test_a_server_of_the_same_name_set_up_another_way_stays_asked(
    agents_dir, setting, declaration
):
    batch = await _sharing(
        agents_dir, setting, tools=["fs_read", "@same"], mcpServers={"same": declaration}
    )
    child = _entry(batch, "own-server")

    assert grants.shared_servers_of(child, _entry(batch, "kirocrew")) == frozenset()
    assert _ASK in _rules(child)


def test_a_server_the_parent_withholds_a_tool_of_is_not_shared():
    child = {"mcpServers": {"same": _SAME, "open": _SAME}}
    parent = {"mcpServers": {"same": _SAME, "open": _SAME}, "excludedTools": ["@same/drop"]}
    assert grants.shared_servers_of(child, parent) == frozenset({"open"})
    assert grants.shared_servers_of(child, {**parent, "excludedTools": ["@same", "@open"]}) == set()
    assert grants.shared_servers_of({"mcpServers": None}, parent) == frozenset()
    assert grants.shared_servers_of(child, {"id": "p"}) == frozenset()


def test_only_the_projections_own_ask_is_narrowed():
    """An equal ask the agent's own file wrote sits before it and keeps asking."""
    parent = {"mcpServers": {"same": _SAME}}
    child = {"mcpServers": {"same": _SAME}, "permissions": {"rules": [_ASK, _ASK]}}
    spared = {**_ASK, "exclude": ["same/*"]}

    assert grants.asking_outside(child, parent)["permissions"]["rules"] == [_ASK, spared]
    # No ask of the projection's last: nothing is narrowed.
    other = {"capability": "shell", "effect": "ask"}
    authored = {"mcpServers": {"same": _SAME}, "permissions": {"rules": [_ASK, other]}}
    assert grants.asking_outside(authored, parent) is authored
    # Nothing shared: returned as it is.
    apart = {"mcpServers": {"same": {"command": "x"}}, "permissions": {"rules": [_ASK]}}
    assert grants.asking_outside(apart, parent) is apart


def test_a_grant_on_an_mcp_tool_survives_only_for_a_shared_server():
    refs = frozenset({"execute_bash", "@same/write", "@same", "@mine/write", "@samething/x"})
    assert grants.within(refs, frozenset({"same"})) == {"@same/write", "@same"}
    assert grants.within(refs, frozenset()) == frozenset()
    assert grants.within(None, frozenset({"same"})) == frozenset()


def _mcp_call(handle, *, agent: str = "own-server"):
    """The recorded MCP request of a person's agent, through *handle*."""
    record = copy.deepcopy(_MCP_CALL)
    request = record["request"]
    request["_meta"]["kiro"]["requester"]["subagent"]["agentName"] = agent
    handle._handle_update(
        JsonRpcMessage(
            method="session/update",
            params={"sessionId": request["sessionId"], "update": record["tool_call"]},
        )
    )
    event = handle._build_permission_event(
        JsonRpcMessage(id=11, method="session/request_permission", params=request)
    )
    assert event is not None and event.native_child_call
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False], ids=["declared alike", "declared another way"])
async def test_the_gates_grant_on_a_server_tool_follows_the_declaration(
    agents_dir, setting, tmp_path, shared
):
    """The recorded request is for ``@own-srv/p6_lookup``; the parent's gate-answered
    grants name that tool, as a write the batch guard holds and as a hook-held one."""
    theirs = {"command": sys.executable, "args": ["/srv/own.py"]}
    mine = theirs if shared else {"command": sys.executable, "args": ["/srv/mine.py"]}
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    _write(
        agents_dir,
        _default_spec(tools=[*shipped["tools"], "@own-srv"], mcpServers={"own-srv": theirs}),
    )
    _write(agents_dir, _own("own-server", tools=["@own-srv"], mcpServers={"own-srv": mine}))
    setting("on")
    batch = await _batch()
    handle = _handle(batch, tmp_path, sid=_MCP_CALL["request"]["sessionId"])
    ref = "@own-srv/p6_lookup"
    handle.kas_crew_answered = frozenset({*handle.kas_crew_answered, ref})
    handle.kas_hook_held = frozenset({ref})

    event = _mcp_call(handle)

    assert event.mcp_server_name == "own-srv" and event.tool_name == "p6_lookup"
    assert (ref in event.spec_granted_builtins) is shared
    assert (ref in event.spec_hook_held) is shared
    # The built-in grants reach it either way.
    assert "execute_bash" in event.spec_granted_builtins


_ASKS_OF_ITS_OWN = {
    "names-the-tool": (
        {"capability": "mcp", "effect": "ask", "match": ["own-srv/p6_lookup"]},
        False,
    ),
    "names-the-server": ({"capability": "mcp", "effect": "ask", "match": ["own-srv/*"]}, False),
    "every-mcp-call": ({"capability": "mcp", "effect": "ask"}, False),
    "a-server-pattern": ({"capability": "mcp", "effect": "ask", "match": ["own-*/x"]}, False),
    "every-capability": ({"capability": "all", "effect": "ask"}, False),
    # Controls: an ask that names no shared server leaves the grant.
    "another-server": ({"capability": "mcp", "effect": "ask", "match": ["other-srv/*"]}, True),
    "the-shell": ({"capability": "shell", "effect": "ask"}, True),
    "none": (None, True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("which", sorted(_ASKS_OF_ITS_OWN))
async def test_its_own_mcp_ask_is_not_answered_by_its_parents_server_grant(
    agents_dir, setting, tmp_path, which
):
    """Its own "ask me" on an MCP call holds against the session agent's gate-answered
    grant on a server both declare alike, as a write the batch guard holds and as a
    hook-held one; an ask that names no shared server leaves the grant."""
    ask, kept = _ASKS_OF_ITS_OWN[which]
    srv = {"command": sys.executable, "args": ["/srv/own.py"]}
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    _write(
        agents_dir,
        _default_spec(tools=[*shipped["tools"], "@own-srv"], mcpServers={"own-srv": srv}),
    )
    extra = {"permissions": {"rules": [ask]}} if ask else {}
    _write(agents_dir, _own("own-server", tools=["@own-srv"], mcpServers={"own-srv": srv}, **extra))
    setting("on")
    batch = await _batch()
    assert "own-server" in {a["id"] for a in batch}
    handle = _handle(batch, tmp_path, sid=_MCP_CALL["request"]["sessionId"])
    ref = "@own-srv/p6_lookup"
    handle.kas_crew_answered = frozenset({*handle.kas_crew_answered, ref})
    handle.kas_hook_held = frozenset({ref})

    event = _mcp_call(handle)

    assert (ref in event.spec_granted_builtins) is kept
    assert (ref in event.spec_hook_held) is kept
    if not kept:
        assert _verdict(event)[0] != TOOL_AUTO_APPROVE
