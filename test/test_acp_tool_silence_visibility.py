"""A call's silence timer runs only while the session can prove it would see the call's activity.

Where that activity may be out of the session's sight, the call is not judged by its
silence, and the liveness oracle and the hard cap judge it as they judged every call
before the timer (``acp-client.md``, "Per-call silence timer"). Each test here drives
a healthy turn whose call's activity is out of sight for a while and shows the call
is not cut; where the timer may run again, a row shows it acting, so none of them
passes by the timer never acting.

The shapes: a native sub-agent's frame the runtime dropped while the roster route was
closed for less than one timer read; an ask (an approval, an MCP elicitation) waiting
on a person, whether or not the consumer holds the event; an ask naming no call,
whether or not the consumer parks on it; a Kiro Crew call on a handle no keepalive
reaches; a frame of a kind the timer cannot read; a concurrent request reader holding
the session's frames; a KAS hook running, with a consumer park inside it or before it,
and one that outlives its turn. Where a hold and a consumer park cover the same
instant, it is subtracted once, a heartbeat stamped inside them included. Plus the
metric a silence cut emits.

The clock is the tick harness's (``acp_tool_silence_helpers``): every instant is exact.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from acp_tool_silence_helpers import (
    _BOUND,
    _EDIT,
    _START,
    _STDIO_BOUND,
    _SUBAGENT,
    _TICK,
    _WAIT,
    _at,
    _child_text,
    _Clock,
    _co_tenant_roster,
    _permission,
    _tool_call,
    _Turn,
    _update,
    install_tick_clock,
)

from kiro_crew.acp import kas_wire
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    EVENT_ELICITATION_REQUEST,
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    METHOD_KIRO_SESSION_UPDATE,
    STOP_REASON_END_TURN,
    STOP_REASON_TOOL_STALL,
    JsonRpcMessage,
)
from kiro_crew.dashboard.handlers.sessions_parts import live_turn
from kiro_crew.providers.acp import AcpProvider


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    return install_tick_clock(monkeypatch)


def _end_turn(request_id: int = 1) -> JsonRpcMessage:
    return JsonRpcMessage(id=request_id, result={"stopReason": "end_turn"})


def _done(call_id: str, session: str = "sA") -> JsonRpcMessage:
    return _update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": call_id,
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "done"}}],
        },
        session=session,
    )


# ── A native sub-agent's frame lost between two timer reads ──
#
# kiro-cli streams a native sub-agent under its own sessionId, which the runtime
# routes to the parent only while the parent is the one session registered there.
# A co-tenant registered for less than one queue read closes the route and opens
# it again before the next read asks, so the answer the timer samples is open both
# times; the runtime's count of frames that reached no session is what moved.


def _runtime_alone_with_its_child() -> AcpRuntime:
    """A real runtime whose one session ``sA`` owns the roster that names ``child-1``."""
    runtime = AcpRuntime(work_dir="/tmp")
    runtime._session_queues["sA"] = asyncio.Queue()
    runtime._subagent_sessions = {"child-1"}
    runtime._subagent_owner = "sA"
    return runtime


#: The child's own call, a Kiro Crew wait on its own server (sends nothing while it runs).
_CHILD_WAIT = _tool_call(
    "child-wait",
    kind="other",
    kiro={"toolName": "wait", "mcpServerName": "kirocrew-core"},
    raw={"seconds": 600},
)
#: The tick the child dispatches its call, and the tick it finishes.
_CHILD_CALL = 4
_CHILD_FINISHES = _CHILD_CALL + 3 * 16


@pytest.mark.parametrize("child", ["dropped-in-a-blip", "routed", "silent"])
@pytest.mark.asyncio
async def test_a_childs_frame_lost_between_two_reads_retires_its_parents_call(clock, child):
    """The parent dispatches kiro-cli's sub-agent tool at tick 1 and its child talks
    at tick 2. At tick 4, inside ONE queue read, a co-tenant registers, the reader
    drops the child's ``tool_call`` for a long wait, and the co-tenant leaves: both
    timer reads around it find the route open. The child's wait runs three bounds
    and the parent's call completes. Through ``prompt()``.

    * dropped-in-a-blip: the runtime counted the lost frame, so the parent's call
      loses its timer and the turn ends on its own.
    * routed: no co-tenant; the child's ``tool_call`` reaches the parent, its open
      call holds the parent's, and the turn ends on its own too.
    * silent: the child sends nothing after tick 2 and nothing was lost, so the
      timer still acts: one bound after the child's last frame."""
    runtime = _runtime_alone_with_its_child()
    route_open_after_blip: list[bool] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT)]
        if tick == 2:
            return [_child_text()]
        if tick == _CHILD_CALL and child == "dropped-in-a-blip":
            runtime._session_queues["sB"] = asyncio.Queue()
            # What the reader does with the child's frame while sB is registered.
            assert runtime.routes_roster_children_to("sA") is False
            runtime._note_dropped_frame("child-1", "session/update")
            runtime.unregister_session("sB")
            route_open_after_blip.append(runtime.routes_roster_children_to("sA"))
            return []
        if tick == _CHILD_CALL and child == "routed":
            return [_update(_CHILD_WAIT, session="child-1")]
        if tick == _CHILD_FINISHES and child != "silent":
            return [_done("child-wait", session="child-1"), _child_text("CI green")]
        if tick == _CHILD_FINISHES + 2:
            return [_done("tc-sub"), _end_turn()]
        return []

    turn = _Turn(clock, script)
    turn.handle._runtime.routes_roster_children_to = runtime.routes_roster_children_to
    turn.handle._runtime.unrouted_child_frames = runtime.unrouted_child_frames

    events = await turn.prompt()

    if child == "silent":
        assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
        assert events[-1].title == "tc-sub"
        assert turn.cancelled_at == _at(2) + _BOUND + _TICK
        return
    assert events[-1].stop_reason == STOP_REASON_END_TURN
    assert turn.cancelled_at is None
    if child == "dropped-in-a-blip":
        # The sampled answer missed the blip: the route was open at every read.
        assert route_open_after_blip == [True]
        assert runtime.unrouted_child_frames() == 1


@pytest.mark.asyncio
async def test_a_frame_lost_before_the_call_is_dequeued_counts_against_it(clock):
    """The reader drops the child's frame before this session has dequeued the
    parent's own sub-agent call (the consumer was busy with an earlier event). The
    count is taken at turn start, so the loss still retires the call."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            turn.unrouted += 1
            return [_update(_SUBAGENT)]
        if tick == _CHILD_FINISHES:
            return [_done("tc-sub"), _end_turn()]
        return []

    turn = _Turn(clock, script, routes=True)

    events = await turn.prompt()

    assert events[-1].stop_reason == STOP_REASON_END_TURN
    assert turn.cancelled_at is None


@pytest.mark.parametrize("unread", ["route", "count"])
@pytest.mark.asyncio
async def test_a_runtime_that_cannot_answer_proves_nothing(clock, unread):
    """A runtime that answers nothing readable about the roster route, or about the
    frames it lost, proves nothing about what reached the session: the sub-agent
    call has no timer, and the turn ends on its own."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT)]
        if tick == _CHILD_FINISHES:
            return [_done("tc-sub"), _end_turn()]
        return []

    turn = _Turn(clock, script, routes=True)
    runtime = turn.handle._runtime
    if unread == "route":
        runtime.routes_roster_children_to = MagicMock()
    else:
        runtime.unrouted_child_frames = MagicMock()

    events = await turn.prompt()

    assert events[-1].stop_reason == STOP_REASON_END_TURN
    assert turn.cancelled_at is None


# ── An ask waiting on a person ──
#
# The ask (session/request_permission, or an MCP elicitation: elicitation/create or
# KAS's _kiro/mcp/elicitation) names its call. While it waits the call cannot move
# and nothing says how long the person takes, whether or not the consumer holds the
# event; the answer is the call's last proven activity, and its timer runs again
# from there.

#: A tool on a url-declared server: the network bound.
_NET = _tool_call("tc-net", kind="other", kiro={"toolName": "deploy", "mcpServerName": "remote"})
_ASK_TICK = 12
#: Ticks from the answer to the tool's result: under one bound.
_RESULT_AFTER = 10
#: How long the person takes: four bounds.
_READ_FOR = 4 * _BOUND


def _elicitation(call_id: str = "tc-net") -> JsonRpcMessage:
    params: dict[str, Any] = {
        "sessionId": "sA",
        "message": "Deploy to production?",
        "requestedSchema": {"type": "object", "properties": {"confirm": {"type": "boolean"}}},
    }
    if call_id:
        params["toolCallId"] = call_id
    return JsonRpcMessage(id=950, method="elicitation/create", params=params)


def _ask_turn(clock: _Clock, ask: str, script) -> _Turn:
    turn = _Turn(clock, script)
    turn.handle._mcp_network_servers = frozenset({"remote"})
    turn.handle.accepts_elicitation = ask != "elicitation-declined"
    # The SEL record of an elicitation answer is not what these tests read.
    turn.handle._audit_elicitation_decision = lambda *_a, **_k: None
    return turn


async def _answer(handle: AcpSessionHandle, ask: str) -> None:
    if ask == "permission":
        assert await handle.approve_tool(951)
    elif ask == "rejection":
        await handle.reject_tool(951)
    else:
        assert await handle.answer_elicitation(950, "accept", {"confirm": True})


def _net_permission() -> JsonRpcMessage:
    """The approval the url-server tool asks for, under its own request id."""
    asked = _permission("tc-net")
    return JsonRpcMessage(id=951, method=asked.method, params=asked.params)


@pytest.mark.parametrize(
    ("ask", "consumer"),
    [
        ("permission", "holds-the-card"),
        ("elicitation", "holds-the-card"),
        ("permission", "answers-later"),
        ("elicitation", "answers-later"),
        ("elicitation-declined", "never-asked"),
    ],
)
@pytest.mark.asyncio
async def test_a_call_that_asks_the_person_and_then_finishes_is_not_cut(clock, ask, consumer):
    """A tool on a url-declared server is dispatched at tick 1 and asks the person
    at tick 12. The person takes four bounds, and the tool sends its result ten
    ticks after the answer. Through ``prompt()``.

    * holds-the-card: the consumer holds the event while the person reads, the
      park every clock subtracts, and answers through the handle.
    * answers-later: the consumer hands the card on and keeps pulling, so no park
      covers the wait; the answer arrives four bounds later.
    * never-asked: this consumer cannot put an elicitation to a person, so the
      handle declines it at once; the ask still names its call."""
    answer_tick = _ASK_TICK + 4 * 16
    tasks: list[asyncio.Task] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_NET)]
        if tick == _ASK_TICK:
            return [_net_permission() if ask == "permission" else _elicitation()]
        if consumer == "answers-later" and tick == answer_tick:
            tasks.append(asyncio.ensure_future(_answer(handle, ask)))
            return []
        result_tick = (answer_tick if consumer == "answers-later" else _ASK_TICK) + _RESULT_AFTER
        if tick == result_tick:
            return [_done("tc-net"), _end_turn()]
        return []

    turn = _ask_turn(clock, ask, script)
    held: list[str] = []

    async def on_event(ev) -> None:
        if ev.kind in (EVENT_PERMISSION_REQUEST, EVENT_ELICITATION_REQUEST):
            held.append(ev.kind)
            if consumer == "holds-the-card":
                clock.now += _READ_FOR
                await _answer(turn.handle, ask)

    turn.on_event = on_event

    events = await turn.prompt()
    await asyncio.gather(*tasks)

    assert len(held) == (0 if consumer == "never-asked" else 1)
    assert events[-1].stop_reason == STOP_REASON_END_TURN, (events[-1].title, turn.cancelled_at)
    assert turn.cancelled_at is None


@pytest.mark.parametrize("ask", ["permission", "rejection", "elicitation"])
@pytest.mark.parametrize("consumer", ["holds-the-card", "answers-later"])
@pytest.mark.asyncio
async def test_a_call_lost_after_its_answer_is_cut_one_bound_after_it(clock, ask, consumer):
    """The same ask, and the tool is lost after the answer: the answer restarts the
    call's timer, so it is cut one bound after the answer, never during the wait."""
    answer_tick = _ASK_TICK + 4 * 16
    tasks: list[asyncio.Task] = []
    answered_at: list[float] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_NET)]
        if tick == _ASK_TICK:
            return [_elicitation() if ask == "elicitation" else _net_permission()]
        if consumer == "answers-later" and tick == answer_tick:
            answered_at.append(clock.now - _START)
            tasks.append(asyncio.ensure_future(_answer(handle, ask)))
        return []

    turn = _ask_turn(clock, ask, script)

    async def on_event(ev) -> None:
        if consumer == "holds-the-card" and ev.kind in (
            EVENT_PERMISSION_REQUEST,
            EVENT_ELICITATION_REQUEST,
        ):
            clock.now += _READ_FOR
            answered_at.append(clock.now - _START)
            await _answer(turn.handle, ask)

    turn.on_event = on_event

    events = await turn.prompt()
    await asyncio.gather(*tasks)

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-net"
    assert turn.cancelled_at == answered_at[0] + _BOUND + _TICK


def _unnamed_permission() -> JsonRpcMessage:
    """An approval whose ``toolCall`` names no ``toolCallId``: the handle yields it."""
    asked = _permission()
    params = dict(asked.params)
    params["toolCall"] = {"title": "Editing a.txt", "kind": "edit"}
    return JsonRpcMessage(id=901, method=asked.method, params=params)


#: A concurrent request reader is in flight over these ticks (``_awaited_responses``),
#: so no timer read happens and only an ask's own start and answer move the hold.
_READER_FROM = 4


@pytest.mark.parametrize("ask", ["elicitation", "permission"])
@pytest.mark.asyncio
async def test_an_ask_naming_no_call_pauses_every_call(clock, ask):
    """An ask that names no call waits on the person for four bounds, and the
    consumer answers it later. Any call could be the one waiting, so the lost edit
    dispatched before it is not judged while it waits, and the wait is not charged
    to it after: it is cut one bound of its own silence later. A concurrent request
    reader keeps every timer read off from before the ask to past the answer, so
    the hold is opened by the ask and closed by the answer themselves."""
    ask_tick = 5
    answer_tick = ask_tick + 4 * 16
    reader_until = answer_tick + 6
    tasks: list[asyncio.Task] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT)]
        if tick == _READER_FROM:
            handle._awaited_responses.add(77)
        if tick == reader_until:
            handle._awaited_responses.discard(77)
        if tick == ask_tick:
            return [_elicitation(call_id="") if ask == "elicitation" else _unnamed_permission()]
        if tick == answer_tick:
            answer = (
                handle.answer_elicitation(950, "decline", None)
                if ask == "elicitation"
                else handle.reject_tool(901)
            )
            tasks.append(asyncio.ensure_future(answer))
        return []

    turn = _ask_turn(clock, "elicitation", script)

    events = await turn.prompt()
    await asyncio.gather(*tasks)

    assert events[-1].title == "tc-edit"
    held = _at(answer_tick) - _at(ask_tick)
    assert turn.handle._silence_held_total == held
    assert turn.cancelled_at == _at(1) + held + _BOUND + _TICK


@pytest.mark.parametrize("consumer", ["holds-the-card", "answers-at-once"])
@pytest.mark.parametrize("ask", ["permission", "elicitation"])
@pytest.mark.asyncio
async def test_an_unnamed_ask_the_consumer_holds_is_subtracted_once(clock, ask, consumer):
    """The lost edit is dispatched at tick 1 and an ask naming no call comes at tick
    2, beside a co-tenant's frames. The dispatch loop opens the hold before it
    yields the ask, and the consumer then parks on that same event while the
    person reads for four bounds and answers through the handle: one stretch,
    both held and parked, subtracted once. The edit is cut one bound of its own
    silence after the answer, not a second wait later.

    * answers-at-once: the control; the consumer answers as the card arrives, so
      neither a park nor a hold covers any time and the edit is cut one bound
      after its dispatch."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT)]
        if tick == 2:
            return [_elicitation(call_id="") if ask == "elicitation" else _unnamed_permission()]
        return [_co_tenant_roster()]

    turn = _ask_turn(clock, "elicitation", script)
    answered_at: list[float] = []

    async def on_event(ev) -> None:
        if ev.kind not in (EVENT_PERMISSION_REQUEST, EVENT_ELICITATION_REQUEST):
            return
        if consumer == "holds-the-card":
            clock.now += _READ_FOR
        answered_at.append(clock.now - _START)
        if ask == "elicitation":
            assert await turn.handle.answer_elicitation(950, "accept", {"confirm": True})
        else:
            assert await turn.handle.approve_tool(901)

    turn.on_event = on_event

    events = await turn.prompt()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-edit"
    wait = _READ_FOR if consumer == "holds-the-card" else 0.0
    assert answered_at == [_at(2) + wait]
    assert turn.cancelled_at == _at(1) + wait + _BOUND + _TICK
    # Every held instant was parked too: the hold banked none of it.
    assert turn.handle._silence_held_total == 0.0


@pytest.mark.parametrize("ask", ["unnamed-ask", "no-ask"])
@pytest.mark.asyncio
async def test_a_heartbeat_inside_a_held_park_is_charged_the_rest_of_that_park_once(clock, ask):
    """Kiro Crew's ``wait`` is dispatched at tick 1 and an ask naming no call comes
    at tick 2. The consumer parks on that ask while the person reads for four bounds,
    and the wait's keepalive lands half way through: it arrives over HTTP whenever
    it is due. The stretch after the beat is both held and parked, so the beat's
    baseline counts the hold and the park still open as one: the wait is charged
    neither half, and is cut one stdio bound after the person answers.

    * no-ask: the control; the same park and beat on the wait's own event, no hold."""
    read_for = 4 * _STDIO_BOUND
    beat_at: list[float] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_WAIT)]
        if tick == 2 and ask == "unnamed-ask":
            return [_unnamed_permission()]
        return []

    turn = _Turn(clock, script)

    async def on_event(ev) -> None:
        on_ask = ask == "unnamed-ask" and ev.kind == EVENT_PERMISSION_REQUEST
        on_wait = ask == "no-ask" and ev.kind == EVENT_TOOL_CALL and ev.tool_call_id == "tc-wait"
        if not (on_ask or on_wait):
            return
        clock.now += read_for / 2
        beat_at.append(clock.now - _START)
        assert turn.handle.note_tool_heartbeat("tc-wait") == 1
        clock.now += read_for / 2
        if on_ask:
            assert await turn.handle.approve_tool(901)

    turn.on_event = on_event

    events = await turn.prompt()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-wait"
    assert beat_at == [_at(2 if ask == "unnamed-ask" else 1) + read_for / 2]
    assert turn.cancelled_at == beat_at[0] + read_for / 2 + _STDIO_BOUND + _TICK


@pytest.mark.asyncio
async def test_an_ask_the_last_turn_left_unanswered_pauses_nothing_in_the_next(clock):
    """Turn 1 yields an ask naming no call and ends before anyone answers it, so
    its hold is still open when the turn ends. Turn 2 drops the ask, and the hold
    with it: a lost edit dispatched while a concurrent request reader keeps every
    timer read off is cut at the first read after the reader leaves, its whole
    silence charged to it, none of it held for the last turn's ask."""
    turn2_start: list[int] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if not turn2_start:
            if tick == 1:
                return [_unnamed_permission()]
            return [_end_turn(1)] if tick == 3 else []
        # Turn 2, counted from its first read.
        at = tick - turn2_start[0]
        if at == 0:
            handle._awaited_responses.add(77)
        if at == 2:
            return [_update(_EDIT)]
        if at == 40:
            handle._awaited_responses.discard(77)
        return []

    turn = _Turn(clock, script)

    first = await turn.prompt()

    assert first[-1].stop_reason == STOP_REASON_END_TURN
    assert turn.handle._silence_held_since is not None
    turn2_start.append(turn.queue.tick + 1)
    turn2_from = clock.now - _START

    events = await turn.prompt()

    assert events[-1].title == "tc-edit"
    # Released on turn 2's 41st read, the first that reads a timer.
    assert turn.cancelled_at == turn2_from + _at(41)


# ── A handle no keepalive reaches ──
#
# A call on Kiro Crew's own servers proves it is alive by its heartbeat, which only
# ``/api/session-keepalive`` delivers, and only to a provider the gateway's own
# registry holds (``kiro_crew.keepalive_route``). ``kirocrew chat`` holds its
# provider in its own process, so no beat of its calls ever arrives.

_SPAWN = _tool_call(
    "tc-ssa",
    kind="other",
    kiro={"toolName": "spawn_sub_agents", "mcpServerName": "kirocrew-core"},
    raw={"agents": [{"task": "build"}]},
)
#: A call on an opt-in server of Kiro Crew's own (``KIROCREW_BIN_MCP_SERVERS``), not the core one.
_PANEL = _tool_call(
    "tc-panel",
    kind="other",
    kiro={"toolName": "panel_show", "mcpServerName": "kirocrew-panel"},
    raw={"panel": "status"},
)
#: Each tool's call and the bound it gets where its keepalive reaches the handle.
_CREW_CALLS = {
    "spawn_sub_agents": (_SPAWN, _BOUND),
    "wait": (_WAIT, _STDIO_BOUND),
    "non-core-server": (_PANEL, _STDIO_BOUND),
}
#: The children (or the wait) finish here, long past either bound.
_CREW_CALL_DONE = 100


@pytest.mark.parametrize(
    "surface", ["gateway-beats", "gateway-silent", "unreached", "reached-mid-call"]
)
@pytest.mark.parametrize("tool", list(_CREW_CALLS))
@pytest.mark.asyncio
async def test_a_kiro_crew_call_is_timed_only_where_its_keepalive_reaches(clock, surface, tool):
    """A Kiro Crew call that works for 100 ticks, past its bound, sending nothing
    the harness forwards.

    * gateway-beats: the registry the keepalive route reads holds the handle's
      provider, and a heartbeat naming the call arrives every 8 ticks: not cut.
    * gateway-silent: the same handle with no heartbeat: cut one bound after the
      dispatch, so the timer acts where the beat could reach it.
    * unreached: no keepalive can reach the handle (``kirocrew chat``): the call
      is not judged by its silence, and the turn ends as the call does.
    * reached-mid-call: marked reachable after the call was dispatched; the beats
      sent before are gone, so the call stays untimed for the turn."""
    frame, bound = _CREW_CALLS[tool]
    call_id = frame["toolCallId"]

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(frame)]
        if surface == "gateway-beats" and tick % 8 == 0 and tick < _CREW_CALL_DONE:
            assert handle.note_tool_heartbeat(call_id) == 1
        if surface == "reached-mid-call" and tick == 30:
            handle.keepalive_routed = True
        if tick == _CREW_CALL_DONE:
            return [_done(call_id)]
        if tick == _CREW_CALL_DONE + 1:
            return [_end_turn()]
        return []

    turn = _Turn(clock, script, keepalive_routed=surface.startswith("gateway"))

    events = await turn.prompt()

    if surface == "gateway-silent":
        assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
        assert events[-1].title == call_id
        assert turn.cancelled_at == _at(1) + bound + _TICK
    else:
        assert events[-1].stop_reason == STOP_REASON_END_TURN
        assert turn.cancelled_at is None


@pytest.mark.asyncio
async def test_a_handle_no_keepalive_reaches_still_times_its_other_calls(clock):
    """The rule takes the timer from Kiro Crew's own calls alone: on ``kirocrew
    chat``'s handle a lost edit dispatched beside a working ``spawn_sub_agents`` is
    still cut one bound of its own silence after its dispatch."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SPAWN)]
        if tick == 2:
            return [_update(_EDIT)]
        return []

    turn = _Turn(clock, script, keepalive_routed=False)

    events = await turn.prompt()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(2) + _BOUND + _TICK


_PARENT_KEY = "dashboard:main"


@pytest.mark.parametrize("children", ["quiet", "moving"])
@pytest.mark.asyncio
async def test_a_spawn_beat_carries_its_childrens_activity_through_the_provider(
    clock, children, monkeypatch
):
    """``spawn_sub_agents`` polls and beats every 8 ticks naming its one child, through
    the route (``live_turn._note_tool_heartbeat``) into the ``AcpProvider`` the gateway's
    registry holds, which forwards the child's own latest activity to the live
    ``AcpSessionProvider`` and its handle. The child last moved at tick 1.

    * quiet: the child never moves again: the call is cut one sub-agent bound after the
      child's last activity, however often the tool beats.
    * moving: the child moves at every beat: the call is never cut and the turn ends."""
    # The child's stamps are on the fake clock, not the reader's, so no think
    # window may excuse them: a negative infinite one excuses nothing.
    monkeypatch.setattr(live_turn, "_model_think_window", lambda _provider: -math.inf)
    child = SimpleNamespace(
        parent_session_key=_PARENT_KEY,
        done=False,
        _tool_tracker=None,
        _awaiting_approval=False,
        _exec_started=1.0,
        _last_activity_mono=None,
    )
    state = SimpleNamespace(subagents=SimpleNamespace(get=lambda agent_id: child))
    holder: list[AcpProvider] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            child._last_activity_mono = clock.now
            return [_update(_SPAWN)]
        if tick % 8 == 0 and tick < _CREW_CALL_DONE:
            if children == "moving":
                child._last_activity_mono = clock.now
            body = {"tool_call_id": "tc-ssa", "phase": "poll", "subagent_ids": ["a1"]}
            live_turn._note_tool_heartbeat(state, _PARENT_KEY, holder[0], body)
        if tick == _CREW_CALL_DONE:
            return [_done("tc-ssa")]
        if tick == _CREW_CALL_DONE + 1:
            return [_end_turn()]
        return []

    turn = _Turn(clock, script)
    outer = AcpProvider.__new__(AcpProvider)
    outer._client = AcpSessionProvider(turn.handle, turn.handle._runtime, session_key=_PARENT_KEY)
    holder.append(outer)

    events = await turn.prompt()

    if children == "quiet":
        assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
        assert events[-1].title == "tc-ssa"
        assert turn.cancelled_at == _at(1) + _BOUND + _TICK
    else:
        assert events[-1].stop_reason == STOP_REASON_END_TURN
        assert turn.cancelled_at is None


# ── A frame for the call of a kind the timer cannot read ──


def _unknown_update(call_id: str) -> JsonRpcMessage:
    return _update({"sessionUpdate": "tool_call_waiting", "toolCallId": call_id})


def _unknown_request(call_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=777, method="_kiro/tool/confirm", params={"sessionId": "sA", "toolCallId": call_id}
    )


def _unknown_notification(call_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="_kiro/tool/progress", params={"sessionId": "sA", "toolCallId": call_id}
    )


def _unknown_request_naming_a_tool_call(call_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=778,
        method="_kiro/tool/confirm",
        params={"sessionId": "sA", "toolCall": {"toolCallId": call_id}},
    )


def _unknown_notification_carrying_an_update(call_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="_kiro/tool/progress",
        params={"sessionId": "sA", "update": {"toolCallId": call_id, "phase": "waiting"}},
    )


@pytest.mark.parametrize(
    "frame",
    [
        _unknown_update,
        _unknown_request,
        _unknown_notification,
        _unknown_request_naming_a_tool_call,
        _unknown_notification_carrying_an_update,
    ],
    ids=[
        "update-kind",
        "server-request",
        "notification",
        "server-request-tool-call",
        "notification-update",
    ],
)
@pytest.mark.parametrize("names", ["the-call", "another-call"])
@pytest.mark.asyncio
async def test_a_frame_of_a_kind_the_timer_cannot_read_retires_the_call_it_names(
    clock, frame, names
):
    """At tick 5 a frame of a kind the timer does not read names the edit: it may
    say the edit waits on something, so its silence proves nothing and it loses its
    timer (the turn runs to its own deadline). Naming another call retires nothing:
    the edit is still cut at its bound."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT)]
        if tick == 5:
            return [frame("tc-edit" if names == "the-call" else "tc-other")]
        return [_co_tenant_roster()]

    turn = _Turn(clock, script)

    events = await turn.run()

    if names == "another-call":
        assert events[-1].title == "tc-edit"
        assert turn.cancelled_at == _at(1) + _BOUND + _TICK
        return
    assert events[-1].stop_reason == "timeout"
    assert turn.cancelled_at is None
    if frame in (_unknown_request, _unknown_request_naming_a_tool_call):
        turn.handle._runtime.send_error.assert_awaited()


@pytest.mark.asyncio
async def test_kiro_clis_own_preview_of_a_call_restarts_it(clock):
    """kiro-cli sends ``tool_call_chunk`` for this session's own call under the
    extension spelling, naming the call: that is the call's frame."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT)]
        if tick == 10:
            return [
                JsonRpcMessage(
                    method=METHOD_KIRO_SESSION_UPDATE,
                    params={
                        "sessionId": "sA",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-edit",
                            "title": "write",
                            "kind": "edit",
                        },
                    },
                )
            ]
        return []

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(10) + _BOUND + _TICK


# ── A concurrent request reader holding this session's frames ──


@pytest.mark.parametrize("result", ["buffered", "never"])
@pytest.mark.asyncio
async def test_no_call_is_judged_while_a_concurrent_reader_holds_the_queue(clock, result):
    """From tick 5 a concurrent ``_wait_for_response`` (a model switch, a command)
    reads this session's queue and holds what it takes until its own answer comes
    at tick 40: the edit's result is in its buffer, out of the dispatch loop's
    sight, when the edit's bound runs out. It re-injects the result on its way out,
    so the turn ends on its own. With no result in it, the edit is cut at the
    first read after the reader leaves."""
    waiter_id = 77

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT)]
        if tick == 5:
            handle._awaited_responses.add(waiter_id)
        if tick == 40:
            handle._awaited_responses.discard(waiter_id)
            if result == "buffered":
                return [_done("tc-edit"), _end_turn()]
        return []

    turn = _Turn(clock, script)

    events = await turn.prompt()

    if result == "buffered":
        assert events[-1].stop_reason == STOP_REASON_END_TURN
        assert turn.cancelled_at is None
        return
    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(40)


# ── A KAS hook running ──


_KAS_EDIT = _tool_call("tc-edit", kind="edit", kiro={}, raw={"path": "a.txt"})


@pytest.mark.parametrize("edit", ["finishes", "lost", "streams-then-lost", "dispatched-after"])
@pytest.mark.asyncio
async def test_every_call_is_paused_while_a_kas_hook_runs(clock, monkeypatch, edit):
    """A KAS built-in edit is dispatched at tick 1 and a hook execution starts at
    tick 3 and runs four bounds: it can gate the edit, and nothing names which call
    it gates. No call is judged while it runs and its run is not charged to a call
    after. A concurrent request reader keeps every timer read off from tick 2 to
    past the hook, so the hold is opened and closed by the hook's own start and end.

    * finishes: the edit's result comes ten ticks after the hook; the turn ends.
    * lost: the edit is cut one bound of its own silence later.
    * streams-then-lost: the edit's output lands half way through the hook; only
      the hook's stretch after it is subtracted.
    * dispatched-after: the edit is dispatched only after the hook; none of the
      hook's stretch is subtracted from it."""
    monkeypatch.setattr(kas_wire, "hooks_announced", lambda: True)
    hook_tick = 3
    hook_ends = hook_tick + 4 * 16
    reader_until = hook_ends + 3
    output_tick = hook_tick + 2 * 16
    dispatch_tick = reader_until + 1 if edit == "dispatched-after" else 1
    release = asyncio.Event()
    finished: list[float] = []
    output = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-edit",
        "content": [{"type": "content", "content": {"type": "text", "text": "half"}}],
    }

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == dispatch_tick:
            return [_update(_KAS_EDIT)]
        if tick == 2:
            handle._awaited_responses.add(77)
        if tick == reader_until:
            handle._awaited_responses.discard(77)
        if tick == hook_tick:
            return [
                JsonRpcMessage(
                    id=601,
                    method=kas_wire.METHOD_HOOKS_EXECUTE,
                    params={"sessionId": "sA", "hookId": "h1"},
                )
            ]
        if edit == "streams-then-lost" and tick == output_tick:
            return [_update(output)]
        if tick == hook_ends:
            release.set()
        if edit == "finishes" and tick == hook_ends + _RESULT_AFTER:
            return [_done("tc-edit"), _end_turn()]
        return []

    turn = _Turn(clock, script, backend=ACP_BACKEND_KAS)

    async def run_hook(_request_id: Any, _params: dict) -> None:
        await release.wait()
        finished.append(clock.now - _START)

    turn.handle._answer_kas_hook_execute = run_hook

    events = await turn.prompt()

    if edit == "finishes":
        assert events[-1].stop_reason == STOP_REASON_END_TURN
        assert turn.cancelled_at is None
        return
    assert events[-1].title == "tc-edit"
    held = turn.handle._silence_held_total
    # Closed by the hook's own end, not by a later read: the reader is still in.
    assert finished == [_at(hook_ends)]
    assert _at(hook_ends) - _at(hook_tick) <= held <= _at(hook_ends + 2) - _at(hook_tick)
    if edit == "lost":
        assert turn.cancelled_at == _at(1) + held + _BOUND + _TICK
    elif edit == "streams-then-lost":
        after_output = held - (_at(output_tick) - _at(hook_tick))
        assert turn.cancelled_at == _at(output_tick) + after_output + _BOUND + _TICK
    else:
        assert turn.cancelled_at == _at(dispatch_tick) + _BOUND + _TICK


def _hook_execute(request_id: int, hook_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=request_id,
        method=kas_wire.METHOD_HOOKS_EXECUTE,
        params={"sessionId": "sA", "hookId": hook_id},
    )


#: A KAS built-in read beside the lost edit, which finishes: the consumer may hold
#: its ``tool_call`` event (a slow channel send) while a hook runs.
_KAS_READ = _tool_call("tc-read", kind="read", kiro={}, raw={"path": "b.txt"})


def _holds_the_read(clock: _Clock, call_id: str, holds: bool):
    """A consumer that holds *call_id*'s ``tool_call`` event for two bounds when *holds*."""

    def on_event(ev) -> None:
        if holds and ev.kind == EVENT_TOOL_CALL and ev.tool_call_id == call_id:
            clock.now += 2 * _BOUND

    return on_event


@pytest.mark.parametrize(
    "consumer",
    [
        "holds-an-event-during-the-hook",
        "holds-an-event-before-the-hook",
        "holds-an-event-then-the-edit-streams",
        "holds-nothing",
    ],
)
@pytest.mark.asyncio
async def test_a_consumer_park_inside_a_kas_hook_is_subtracted_once(clock, monkeypatch, consumer):
    """A KAS built-in edit is lost at tick 1 and a hook runs from tick 3 for four
    bounds. A read dispatched at tick 4, which finishes at tick 5, has its event
    held by the consumer for two bounds while the hook still runs: that park lies
    inside the hold, so the stretch is subtracted once and the edit is cut one
    bound of its own silence after the hook ends, however long the park was.

    * holds-an-event-before-the-hook: the read comes at tick 2 and its park ends
      before the hook starts, so the park and the hook are each subtracted whole.
    * holds-an-event-then-the-edit-streams: the same park inside the hook, then the
      edit's output at tick 10, still inside it: the stamp's baseline already
      counts that park once, so none of the hook is charged to the edit after it.
    * holds-nothing: the control; no park, the same hook."""
    monkeypatch.setattr(kas_wire, "hooks_announced", lambda: True)
    hook_tick = 3
    hook_ends = hook_tick + 4 * 16
    before = consumer == "holds-an-event-before-the-hook"
    streams = consumer == "holds-an-event-then-the-edit-streams"
    output = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-edit",
        "content": [{"type": "content", "content": {"type": "text", "text": "half"}}],
    }
    frames = {
        1: [_update(_KAS_EDIT)],
        2 if before else 4: [_update(_KAS_READ)],
        hook_tick: [_hook_execute(601, "h1")],
        5: [_done("tc-read")],
    }
    if streams:
        frames[10] = [_update(output)]
    release = asyncio.Event()
    ran: list[float] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == hook_ends:
            release.set()
        return frames.get(tick, [])

    turn = _Turn(clock, script, backend=ACP_BACKEND_KAS)

    async def run_hook(_request_id: Any, _params: dict) -> None:
        ran.append(clock.now - _START)
        await release.wait()
        ran.append(clock.now - _START)

    turn.handle._answer_kas_hook_execute = run_hook
    turn.on_event = _holds_the_read(clock, "tc-read", consumer != "holds-nothing")

    events = await turn.prompt()

    assert events[-1].title == "tc-edit"
    park_before = 2 * _BOUND if before else 0.0
    park_during = 0.0 if before or consumer == "holds-nothing" else 2 * _BOUND
    # A park runs on the clock, so the hook starts or ends that much later.
    assert ran == [_at(hook_tick) + park_before, _at(hook_ends) + park_before + park_during]
    if streams:
        assert turn.cancelled_at == ran[1] + _BOUND + _TICK
    else:
        assert turn.cancelled_at == _at(1) + park_before + (ran[1] - ran[0]) + _BOUND + _TICK
    assert turn.handle._silence_held_total == ran[1] - ran[0] - park_during


@pytest.mark.parametrize("consumer", ["holds-an-event-during-the-hook", "holds-nothing"])
@pytest.mark.asyncio
async def test_a_hook_that_outlives_its_turn_is_held_against_the_next_turns_park(
    clock, monkeypatch, consumer
):
    """Turn 1 parks two bounds on a read's event, runs a short hook to its end, then
    starts a second hook and ends while that one still runs. Turn 2 starts with
    neither the first turn's held total nor its open hold, so the second hook's
    hold is kept against turn 2's own park: turn 2 dispatches a lost edit, then a
    read whose event the consumer holds for two bounds while the hook runs, and
    the edit is cut one bound of its own silence after the hook ends, the park
    subtracted once.

    * holds-nothing: the control; turn 2 parks on nothing."""
    monkeypatch.setattr(kas_wire, "hooks_announced", lambda: True)
    short_release = asyncio.Event()
    release = asyncio.Event()
    turn2_start: list[int] = []
    held_at_turn2: list[float] = []
    ran: list[float] = []
    read2 = _tool_call("tc-read2", kind="read", kiro={}, raw={"path": "c.txt"})

    def first_turn(tick: int) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_KAS_READ)]
        if tick == 2:
            return [_done("tc-read")]
        if tick == 3:
            return [_hook_execute(601, "h0")]
        if tick == 5:
            short_release.set()
        if tick == 8:
            return [_hook_execute(602, "h1")]
        if tick == 10:
            return [_end_turn(1)]
        return []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if not turn2_start:
            return first_turn(tick)
        # Turn 2, counted from its first read.
        at = tick - turn2_start[0]
        if at == 0:
            held_at_turn2.append(handle._silence_held_total)
        if at == 2:
            return [_update(_KAS_EDIT)]
        if at == 4:
            return [_update(read2)]
        if at == 5:
            return [_done("tc-read2")]
        if at == 20:
            release.set()
        return []

    turn = _Turn(clock, script, backend=ACP_BACKEND_KAS)

    async def run_hook(_request_id: Any, params: dict) -> None:
        if params["hookId"] == "h0":
            await short_release.wait()
            return
        await release.wait()
        ran.append(clock.now - _START)

    turn.handle._answer_kas_hook_execute = run_hook
    turn.on_event = _holds_the_read(clock, "tc-read", True)
    try:
        first = await turn.prompt()

        assert first[-1].stop_reason == STOP_REASON_END_TURN
        assert turn.handle._silence_held_total > 0.0
        assert turn.handle._silence_held_since is not None
        turn2_start.append(turn.queue.tick + 1)
        turn2_from = clock.now - _START
        holds = consumer == "holds-an-event-during-the-hook"
        turn.on_event = _holds_the_read(clock, "tc-read2", holds)

        events = await turn.prompt()
    finally:
        short_release.set()
        release.set()

    assert events[-1].title == "tc-edit"
    assert held_at_turn2 == [0.0]
    park = 2 * _BOUND if holds else 0.0
    # Released at turn 2's 21st read, the park having run on the clock meanwhile.
    assert ran == [turn2_from + _at(21) + park]
    assert turn.cancelled_at == ran[0] + _BOUND + _TICK


# ── What a silence cut reports ──


@pytest.mark.asyncio
async def test_a_silence_cut_is_counted_as_its_own_evidence_class_and_window(clock, monkeypatch):
    """``metrics.md``: a per-call silence cut is ``evidence_class=silence`` in the
    ``silence`` window, apart from the oracle's windows."""
    actions: list[dict] = []

    class _Recorder:
        def counter(self, name: str, attrs: dict | None = None, **_kw: Any) -> None:
            if name == "kirocrew.watchdog.action":
                actions.append(dict(attrs or {}))

        def __getattr__(self, _name: str) -> Any:
            return lambda *_a, **_kw: None

    monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: _Recorder())

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        return [_update(_EDIT)] if tick == 1 else []

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].title == "tc-edit"
    cuts = [a for a in actions if a.get("action") == "cancel"]
    assert cuts and {(a["evidence_class"], a["window"]) for a in cuts} == {("silence", "silence")}
