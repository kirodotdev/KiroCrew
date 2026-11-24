"""Who started a sub-agent below the first level, read from the engine's own frames.

The engine names, on each permission request, the sub-agent that raised it and how
deep it runs. A start is itself a request: the one a sub-agent raises to start
another names the starter, and the ``tool_call`` frame of that same tool call
carries the id of the sub-agent it starts. So the agents between a nested sub-agent
and the session's agent are known, and only their ``ask`` rules hold a grant back.
Where a link is missing the gate falls back to every registered agent.

The frames are the ones a real engine sent, in order
(``fixtures/kas_native_subagents/nested-dispatch-chain.jsonl``): the session's
agent starts ``star``, ``star`` starts ``b``, ``b`` runs one shell command.
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
    _granted,
    _handle,
    _own,
    _setting,
    _verdict,
    _write,
)

from kiro_crew.acp import kas_engine_child_grants as grants
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_native_children import (
    NATIVE_CHILD_LABEL_CAP,
    NATIVE_CHILD_ROSTER_CAP,
)
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage
from kiro_crew.hooks import TOOL_AUTO_APPROVE
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "kas_native_subagents" / "nested-dispatch-chain.jsonl"
)
_STEPS = {
    r["label"]: r
    for r in map(json.loads, _FIXTURE.read_text(encoding="utf-8").splitlines())
    if "label" in r
}
_START_STAR = "the session's agent starting star"
_START_B = "star starting b"
_B_RUNS = "b, started by star, running a command"
_SID = _STEPS[_B_RUNS]["request"]["sessionId"]
_ASKS_SHELL = {"capability": "shell", "effect": "ask"}


def _step(handle: AcpSessionHandle, label: str, **requester: Any) -> AcpEvent:
    """One recorded step through *handle*: its ``tool_call`` frame, then its request.

    *requester* replaces fields of the request's ``requester.subagent``.
    """
    record = copy.deepcopy(_STEPS[label])
    request = record["request"]
    request["_meta"]["kiro"]["requester"].get("subagent", {}).update(requester)
    handle._handle_update(
        JsonRpcMessage(
            method="session/update", params={"sessionId": _SID, "update": record["tool_call"]}
        )
    )
    event = handle._build_permission_event(
        JsonRpcMessage(id=9, method="session/request_permission", params=request)
    )
    assert event is not None
    return event


def _parents_own(handle: AcpSessionHandle) -> AcpEvent:
    """The command ``b`` runs, as the session agent's own call: no sub-agent named."""
    record = copy.deepcopy(_STEPS[_B_RUNS])
    call, request = record["tool_call"], record["request"]
    call["toolCallId"] = request["toolCall"]["toolCallId"] = "the-parents-own-call"
    del call["_meta"]["kiro"]["agentSubtaskId"]
    del request["_meta"]["kiro"]["requester"]["subagent"]
    handle._handle_update(
        JsonRpcMessage(method="session/update", params={"sessionId": _SID, "update": call})
    )
    event = handle._build_permission_event(
        JsonRpcMessage(id=8, method="session/request_permission", params=request)
    )
    assert event is not None and not event.native_child_call
    return event


async def _mounted(agents_dir: Path, setting, tmp_path: Path, **others: Any) -> AcpSessionHandle:
    """``star`` and ``b`` as the tester's run had them, and *others* (name -> spec fields)."""
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("star", tools=["*"]))
    _write(agents_dir, _own("b"))
    for name, fields in others.items():
        _write(agents_dir, _own(name, **fields))
    setting("on")
    batch = await _batch()
    assert {"star", "b", *others} <= {a["id"] for a in batch}
    return _handle(batch, tmp_path, sid=_SID)


@pytest.mark.asyncio
async def test_an_ask_of_an_agent_that_is_not_between_holds_nothing_back(
    agents_dir, setting, tmp_path
):
    """The tester's run: ``star9`` asks about the shell and is registered beside
    ``star``. ``star`` started ``b``, so ``star9`` is not above ``b``."""
    handle = await _mounted(
        agents_dir,
        setting,
        tmp_path,
        star9={"tools": ["*"], "permissions": {"rules": [_ASKS_SHELL]}},
    )
    own = _parents_own(handle)
    _step(handle, _START_STAR)
    _step(handle, _START_B)

    nested = _step(handle, _B_RUNS)

    assert _STEPS[_B_RUNS]["request"]["_meta"]["kiro"]["requester"]["subagent"]["depth"] == 2
    assert nested.native_child_call
    assert "execute_bash" in nested.spec_granted_builtins
    assert _verdict(own)[0] == TOOL_AUTO_APPROVE and _verdict(nested) == _verdict(own)


@pytest.mark.asyncio
async def test_with_no_other_agent_registered_the_nested_agent_is_judged_as_the_sessions_agent(
    agents_dir, setting, tmp_path
):
    """The same frames with only ``star`` and ``b`` registered."""
    handle = await _mounted(agents_dir, setting, tmp_path)
    own = _parents_own(handle)
    _step(handle, _START_STAR)
    _step(handle, _START_B)

    nested = _step(handle, _B_RUNS)

    assert _verdict(own)[0] == TOOL_AUTO_APPROVE and _verdict(nested) == _verdict(own)


# ── other chains, built from the recorded frames with ids, names and depth rewritten ──
#
# Only the two-level chain above was run on an engine. Below, the recorded start and
# the recorded command are replayed under other ids, names and depths.

_STAR9 = {"star9": {"tools": ["*"], "permissions": {"rules": [_ASKS_SHELL]}}}


def _start(
    handle: AcpSessionHandle,
    *,
    call: str,
    starts: str,
    name: str,
    by: tuple[str, str, int] | None = None,
    frame: bool = True,
    request: bool = True,
    asks_for: str | None = None,
) -> None:
    """A start: the tool call *call* starts sub-agent *starts*, an agent named *name*.

    *by* is ``(name, id, depth)`` of the sub-agent that asks for it; ``None`` is the
    session's own agent. *frame* and *request* leave one of the two out; *asks_for*
    makes the request name another agent than *name*.
    """
    record = copy.deepcopy(_STEPS[_START_B])
    update, params = record["tool_call"], record["request"]
    update["toolCallId"] = params["toolCall"]["toolCallId"] = call
    update["_meta"]["kiro"]["agentSubtaskId"] = starts
    update["title"] = params["toolCall"]["title"] = f"Sub-agent: {name}"
    params["_meta"]["kiro"]["consent"]["resource"] = name if asks_for is None else asks_for
    if by is None:
        del params["_meta"]["kiro"]["requester"]["subagent"]
    else:
        params["_meta"]["kiro"]["requester"]["subagent"] = {
            "agentName": by[0],
            "agentSubtaskId": by[1],
            "depth": by[2],
        }
    if frame:
        handle._handle_update(
            JsonRpcMessage(method="session/update", params={"sessionId": _SID, "update": update})
        )
    if request:
        assert handle._build_permission_event(
            JsonRpcMessage(id=5, method="session/request_permission", params=params)
        )


def _runs(handle: AcpSessionHandle, *, name: str, subtask: str, depth: int) -> AcpEvent:
    """The recorded command, raised by sub-agent *subtask* (an agent *name*) at *depth*."""
    record = copy.deepcopy(_STEPS[_B_RUNS])
    record["tool_call"]["_meta"]["kiro"]["agentSubtaskId"] = subtask
    handle._handle_update(
        JsonRpcMessage(
            method="session/update", params={"sessionId": _SID, "update": record["tool_call"]}
        )
    )
    request = record["request"]
    request["_meta"]["kiro"]["requester"]["subagent"] = {
        "agentName": name,
        "agentSubtaskId": subtask,
        "depth": depth,
    }
    event = handle._build_permission_event(
        JsonRpcMessage(id=9, method="session/request_permission", params=request)
    )
    assert event is not None and event.native_child_call
    return event


@pytest.mark.asyncio
async def test_an_ask_of_the_agent_between_holds_the_grant_back(agents_dir, setting, tmp_path):
    """``star9`` asks about the shell and is the one that started ``b``."""
    handle = await _mounted(agents_dir, setting, tmp_path, **_STAR9)
    _start(handle, call="c1", starts="s1", name="star9")
    _start(handle, call="c2", starts="s2", name="b", by=("star9", "s1", 1))

    nested = _runs(handle, name="b", subtask="s2", depth=2)

    assert "execute_bash" not in nested.spec_granted_builtins
    assert "fs_read" in nested.spec_granted_builtins
    assert _verdict(nested)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_grant_in_the_file_of_the_agent_between_adds_nothing(agents_dir, setting, tmp_path):
    """The session's agent does not grant the shell; ``star``'s own file does."""
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    _write(agents_dir, _default_spec(allowedTools=shipped["allowedTools"]))
    _write(agents_dir, _own("star", tools=["*"], allowedTools=["execute_bash"]))
    _write(agents_dir, _own("b", allowedTools=["execute_bash"]))
    setting("on")
    handle = _handle(await _batch(), tmp_path, sid=_SID)
    own = _parents_own(handle)
    _step(handle, _START_STAR)
    _step(handle, _START_B)

    nested = _step(handle, _B_RUNS)

    assert "execute_bash" not in nested.spec_granted_builtins
    assert _verdict(own)[0] != TOOL_AUTO_APPROVE and _verdict(nested) == _verdict(own)


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["mid", "star9"], ids=["no ask between", "an ask between"])
async def test_three_levels_down_only_the_agents_between_count(
    agents_dir, setting, tmp_path, second
):
    """``star`` starts *second*, which starts ``b``. ``star9`` asks about the shell."""
    handle = await _mounted(agents_dir, setting, tmp_path, mid={"tools": ["*"]}, **_STAR9)
    own = _parents_own(handle)
    _start(handle, call="c1", starts="s1", name="star")
    _start(handle, call="c2", starts="s2", name=second, by=("star", "s1", 1))
    _start(handle, call="c3", starts="s3", name="b", by=(second, "s2", 2))

    nested = _runs(handle, name="b", subtask="s3", depth=3)

    if second == "mid":
        assert _verdict(own)[0] == TOOL_AUTO_APPROVE and _verdict(nested) == _verdict(own)
    else:
        assert "execute_bash" not in nested.spec_granted_builtins
        assert _verdict(nested)[0] != TOOL_AUTO_APPROVE


def _no_word_of_the_start(handle: AcpSessionHandle) -> AcpEvent:
    return _runs(handle, name="b", subtask="s2", depth=2)


def _a_start_nobody_was_asked_about(handle: AcpSessionHandle) -> AcpEvent:
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", 1), request=False)
    return _runs(handle, name="b", subtask="s2", depth=2)


def _a_start_with_no_frame(handle: AcpSessionHandle) -> AcpEvent:
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", 1), frame=False)
    return _runs(handle, name="b", subtask="s2", depth=2)


def _a_start_that_asked_for_another_agent(handle: AcpSessionHandle) -> AcpEvent:
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", 1), asks_for="mid")
    return _runs(handle, name="b", subtask="s2", depth=2)


def _a_starter_at_the_wrong_depth(handle: AcpSessionHandle) -> AcpEvent:
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", 1))
    return _runs(handle, name="b", subtask="s2", depth=3)


def _a_missing_link_higher_up(handle: AcpSessionHandle) -> AcpEvent:
    _start(handle, call="c3", starts="s3", name="b", by=("mid", "s2", 2))
    return _runs(handle, name="b", subtask="s3", depth=3)


def _two_calls_said_to_start_it(handle: AcpSessionHandle) -> AcpEvent:
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", 1))
    _start(handle, call="c9", starts="s2", name="b", by=("star", "s1", 1), request=False)
    return _runs(handle, name="b", subtask="s2", depth=2)


def _two_askers_for_one_call(handle: AcpSessionHandle) -> AcpEvent:
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", 1))
    _start(handle, call="c2", starts="s2", name="b", by=("mid", "s7", 1), frame=False)
    return _runs(handle, name="b", subtask="s2", depth=2)


_UNKNOWN_CHAINS = [
    _no_word_of_the_start,
    _a_start_nobody_was_asked_about,
    _a_start_with_no_frame,
    _a_start_that_asked_for_another_agent,
    _a_starter_at_the_wrong_depth,
    _a_missing_link_higher_up,
    _two_calls_said_to_start_it,
    _two_askers_for_one_call,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("feed", _UNKNOWN_CHAINS, ids=lambda f: f.__name__.strip("_"))
async def test_a_chain_with_a_link_missing_counts_every_registered_agent(
    agents_dir, setting, tmp_path, feed
):
    """``star9`` asks about the shell and started nothing; who started ``b`` is not
    known, so its ask holds the grant back."""
    handle = await _mounted(agents_dir, setting, tmp_path, mid={"tools": ["*"]}, **_STAR9)

    nested = feed(handle)

    assert "execute_bash" not in nested.spec_granted_builtins
    assert "fs_read" in nested.spec_granted_builtins
    assert _verdict(nested)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_starter_the_mount_does_not_spare_leaves_no_grant(agents_dir, setting, tmp_path):
    handle = await _mounted(agents_dir, setting, tmp_path)
    _start(handle, call="c2", starts="s2", name="b", by=("stranger", "s1", 1))

    nested = _runs(handle, name="b", subtask="s2", depth=2)

    assert not _granted(nested) and _verdict(nested)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_request_that_names_no_one_is_asked(agents_dir, setting, tmp_path):
    handle = await _mounted(agents_dir, setting, tmp_path)
    _step(handle, _START_STAR)
    _step(handle, _START_B)
    record = copy.deepcopy(_STEPS[_B_RUNS])
    del record["request"]["_meta"]["kiro"]["requester"]
    handle._handle_update(
        JsonRpcMessage(
            method="session/update", params={"sessionId": _SID, "update": record["tool_call"]}
        )
    )

    event = handle._build_permission_event(
        JsonRpcMessage(id=9, method="session/request_permission", params=record["request"])
    )

    assert event is not None and event.native_child_call and not _granted(event)
    assert _verdict(event)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_what_one_session_heard_is_not_read_by_another(agents_dir, setting, tmp_path):
    first = await _mounted(agents_dir, setting, tmp_path, **_STAR9)
    second = _handle(await _batch(), tmp_path, sid=_SID)
    _start(first, call="c1", starts="s1", name="star")
    _start(first, call="c2", starts="s2", name="b", by=("star", "s1", 1))

    assert "execute_bash" in _runs(first, name="b", subtask="s2", depth=2).spec_granted_builtins
    assert (
        "execute_bash" not in _runs(second, name="b", subtask="s2", depth=2).spec_granted_builtins
    )


# ── what a session keeps of each start is bounded as its roster of sub-agents is ──

_LONG = "x" * (NATIVE_CHILD_LABEL_CAP + 1)
_LONG_ID = "s" * (MAX_ACP_SESSION_ID_LEN + 1)


def _kept(handle: AcpSessionHandle) -> list[object]:
    """Every value the session holds of the starts it heard of, keys included."""
    held = getattr(handle, grants._STARTS_ATTR, None)
    if held is None:
        return []
    out: list[object] = [*held.started, *held.started.values(), *held.asked]
    for value in held.asked.values():
        out.extend(value or ())
    return out


def _b_under_star(handle: AcpSessionHandle, **start: Any) -> AcpEvent:
    """``star`` starts ``b`` and ``b`` runs the command; *start* replaces fields of
    that one start."""
    _start(handle, call="c1", starts="s1", name="star")
    said = {"call": "c2", "starts": "s2", "name": "b", "by": ("star", "s1", 1), **start}
    _start(handle, **said)
    return _runs(handle, name=said["name"], subtask=said["starts"], depth=2)


_OVER_A_BOUND = {
    "the-started-id": {"starts": _LONG_ID},
    "the-tool-call-id": {"call": _LONG},
    "the-askers-id": {"by": ("star", _LONG_ID, 1)},
    "the-askers-name": {"by": (_LONG, "s1", 1)},
    "the-started-name": {"name": _LONG},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_OVER_A_BOUND))
async def test_a_start_with_a_field_over_the_rosters_bound_is_not_kept(
    agents_dir, setting, tmp_path, field
):
    """And reads as a start nobody heard of: every registered agent counts as
    between, so ``star9``'s ask holds the grant back."""
    handle = await _mounted(agents_dir, setting, tmp_path, mid={"tools": ["*"]}, **_STAR9)

    nested = _b_under_star(handle, **_OVER_A_BOUND[field])

    longest = max(len(v) for v in _kept(handle) if isinstance(v, str))
    assert longest <= NATIVE_CHILD_LABEL_CAP
    assert "execute_bash" not in nested.spec_granted_builtins
    assert _verdict(nested)[0] != TOOL_AUTO_APPROVE


def _granted_by_field(event: AcpEvent) -> dict[str, set[Any]]:
    return {name: set(getattr(event, name) or ()) for name in _GRANT_SETS}


_GRANT_SETS = (
    "spec_granted_builtins",
    "spec_hook_held",
    "spec_granted_commands",
    "spec_granted_paths",
    "spec_granted_read_paths",
)


def _evicted(handle: AcpSessionHandle) -> AcpEvent:
    _start(handle, call="c1", starts="s1", name="star")
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", 1))
    held = getattr(handle, grants._STARTS_ATTR)
    for n in range(NATIVE_CHILD_ROSTER_CAP):
        grants._remember(held.started, f"later-{n}", f"call-{n}")
    assert "s2" not in held.started and len(held.started) == NATIVE_CHILD_ROSTER_CAP
    assert grants._remember(held.started, "one-more", "call") is True
    return _runs(handle, name="b", subtask="s2", depth=2)


def _refused_by_a_full_roster(handle: AcpSessionHandle) -> AcpEvent:
    _start(handle, call="c1", starts="s1", name="star")
    handle._native_child_sids.update(f"other-{n}" for n in range(NATIVE_CHILD_ROSTER_CAP))
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", 1))
    assert "s2" not in getattr(handle, grants._STARTS_ATTR).started
    return _runs(handle, name="b", subtask="s2", depth=2)


def _a_depth_past_the_bound(handle: AcpSessionHandle) -> AcpEvent:
    deep = 10**400
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", deep))
    assert deep not in _kept(handle)
    return _runs(handle, name="b", subtask="s2", depth=deep + 1)


_NOT_KEPT = [
    *(
        pytest.param(lambda handle, said=said: _b_under_star(handle, **said), id=field)
        for field, said in sorted(_OVER_A_BOUND.items())
    ),
    pytest.param(_evicted, id="evicted"),
    pytest.param(_refused_by_a_full_roster, id="refused-by-a-full-roster"),
    pytest.param(_a_depth_past_the_bound, id="a-depth-past-the-bound"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("feed", _NOT_KEPT)
async def test_a_start_that_is_not_kept_never_grants_more_than_a_kept_one(
    agents_dir, setting, tmp_path, feed
):
    """A start the session did not keep, or let go to make room, leaves the sub-agent
    with no more than the same chain read whole gives it, and with the ask of an
    agent that may sit above it."""
    kept = _b_under_star(await _mounted(agents_dir, setting, tmp_path, **_STAR9))
    assert "execute_bash" in kept.spec_granted_builtins

    unkept = feed(await _mounted(agents_dir, setting, tmp_path, **_STAR9))

    whole, less = _granted_by_field(kept), _granted_by_field(unkept)
    assert all(less[name] <= whole[name] for name in _GRANT_SETS)
    assert "execute_bash" not in unkept.spec_granted_builtins
    assert _verdict(unkept)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_nested_sub_agent_keeps_its_chain_past_many_later_starts(
    agents_dir, setting, tmp_path
):
    """The roster's own bound is the one bound: a start the roster still tracks is
    still read after hundreds of later ones."""
    handle = await _mounted(agents_dir, setting, tmp_path, **_STAR9)
    _start(handle, call="c1", starts="s1", name="star")
    _start(handle, call="c2", starts="s2", name="b", by=("star", "s1", 1))
    for n in range(300):
        _start(handle, call=f"later-call-{n}", starts=f"later-{n}", name="star")

    nested = _runs(handle, name="b", subtask="s2", depth=2)

    assert {"s1", "s2"} <= handle._native_child_sids
    assert "execute_bash" in nested.spec_granted_builtins


@pytest.mark.asyncio
async def test_a_start_that_is_not_kept_is_counted_and_said_once(
    agents_dir, setting, tmp_path, caplog
):
    handle = await _mounted(agents_dir, setting, tmp_path, **_STAR9)

    with caplog.at_level("WARNING", logger=grants.__name__):
        _b_under_star(handle, call=_LONG)
        _start(handle, call="c3", starts=_LONG_ID, name="b", by=("star", "s1", 1))

    assert getattr(handle, grants._STARTS_ATTR).dropped == 3
    said = [r.getMessage() for r in caplog.records if r.name == grants.__name__]
    assert len(said) == 1 and "was not kept" in said[0] and _LONG not in said[0]


def test_a_start_of_a_sub_agent_the_roster_already_tracks_is_kept_in_a_full_roster():
    class _Handle:
        _session_id = "parent"
        _native_child_sids = {f"other-{n}" for n in range(NATIVE_CHILD_ROSTER_CAP - 1)} | {"s2"}

    frame = copy.deepcopy(_STEPS[_START_B]["tool_call"])
    frame["_meta"]["kiro"]["agentSubtaskId"] = "s2"
    grants.note_start_frame(_Handle, frame)
    assert getattr(_Handle, grants._STARTS_ATTR).started == {"s2": frame["toolCallId"]}

    own = copy.deepcopy(frame)
    own["_meta"]["kiro"]["agentSubtaskId"] = "parent"
    grants.note_start_frame(_Handle, own)
    assert "parent" not in getattr(_Handle, grants._STARTS_ATTR).started


def test_the_starts_one_session_remembers_are_bounded():
    held: dict[str, object] = {}
    for n in range(NATIVE_CHILD_ROSTER_CAP + 40):
        grants._remember(held, f"id-{n}", n)

    assert len(held) == NATIVE_CHILD_ROSTER_CAP and "id-0" not in held and "id-40" in held
    # The first word is kept; a second, different word leaves nothing to read.
    grants._remember(held, "id-40", 40)
    assert held["id-40"] == 40
    grants._remember(held, "id-40", 41)
    assert held["id-40"] is None


def test_the_chain_of_a_sub_agent_the_sessions_agent_started_is_empty():
    class _Handle:
        pass

    request = copy.deepcopy(_STEPS[_START_B]["request"])
    assert grants.started_by(_Handle(), request) == ()
    assert grants.started_by(_Handle(), _STEPS[_START_STAR]["request"]) is None
    assert grants.started_by(_Handle(), _STEPS[_B_RUNS]["request"]) is None
    assert grants.started_by(_Handle(), None) is None
