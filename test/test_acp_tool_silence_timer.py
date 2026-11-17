"""Each non-shell tool call has its own silence timer, restarted only by its own frames.

The shared runtime's lost-call shape (ticket V2424286497): a built-in file edit is
dispatched beside a subagent launch, auto-approved, and never runs. Nothing more
arrives for it, while the co-tenant subagent's roster frames, fanned out to every
session, keep arriving. Those frames keep the queue from ever being empty for a
poll, so the oracle arm never runs, and the edit waited for the turn's own
deadline. The per-call timer is read on every pass and counts only frames that
name the call, so the edit is cancelled at its class's bound and the model is told
it timed out.

The three exceptions are pinned beside it: a Kiro Crew tool that heartbeats is not
cancelled while it beats, a sub-agent call is not cancelled while its sub-agent
works, and a call waiting on a person's approval is not cancelled while the person
reads the card. Each test also shows the timer still acting once the activity
stops, so none of them passes by the timer never acting.

The handle's clock is a fake that each queue read advances by one exact tick, so
every instant below is computed, not measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# The tick harness, shared with ``test_acp_tool_silence_visibility``. Every name is
# also read off this module by the review probes in ``compat/repros3``.
from acp_tool_silence_helpers import (  # noqa: F401  (names the probes read too)
    _BOUND,
    _EDIT,
    _START,
    _STDIO_BOUND,
    _SUBAGENT,
    _TICK,
    _TURN,
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

from kiro_crew.acp._dispatch import MAX_ACTIVE_TOOL_CALLS
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.tool_silence import (
    SILENCE_CLASS_BUILTIN,
    SILENCE_CLASS_MCP_NETWORK,
    SILENCE_CLASS_MCP_STDIO,
    SILENCE_CLASS_NONE,
    SILENCE_CLASS_SUBAGENT,
    classify_tool_silence,
    silence_bound,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_SUBAGENT_LIST_UPDATE,
    STOP_REASON_END_TURN,
    STOP_REASON_TOOL_STALL,
    JsonRpcMessage,
)
from kiro_crew.timed_out_call import is_silence_timeout


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    return install_tick_clock(monkeypatch)


@pytest.mark.asyncio
async def test_a_lost_builtin_edit_beside_a_talking_subagent_is_cancelled_at_its_bound(clock):
    """The ticket's shape: the edit is dispatched at tick 1 and a co-tenant roster
    lands on every tick after it, so no poll ever finds the queue empty. The edit
    is cancelled one tick past its bound and the turn ends in tool-stall
    recovery naming the edit, with evidence the recovery prompt reads as a
    timeout."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT)]
        return [_co_tenant_roster()]

    turn = _Turn(clock, script)

    events = await turn.run()

    end = events[-1]
    assert end.kind == EVENT_COMPLETE
    assert end.stop_reason == STOP_REASON_TOOL_STALL
    assert turn.cancelled_at == _at(1) + _BOUND + _TICK
    assert end.title == "tc-edit"
    assert "silence_timer=builtin" in end.text
    assert "processes_terminated=0" in end.text
    assert is_silence_timeout(end.text)


@pytest.mark.asyncio
async def test_frames_of_a_sibling_call_do_not_restart_the_silent_call(clock):
    """A second call streams output on every tick; the lost edit beside it is
    still cancelled on its own clock."""
    sibling = _tool_call("tc-read", kind="read", kiro={"toolName": "read"}, raw={"path": "b"})

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT), _update(sibling)]
        return [
            _update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-read",
                    "content": [{"type": "content", "content": {"type": "text", "text": "x"}}],
                }
            )
        ]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(1) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_a_frame_naming_the_call_restarts_its_timer(clock):
    """The edit's own progress frame at tick 10 restarts its clock there."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT)]
        if tick == 10:
            return [
                _update(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc-edit",
                        "status": "in_progress",
                    }
                )
            ]
        return [_co_tenant_roster()]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert turn.cancelled_at == _at(10) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_a_result_still_queued_when_the_bound_runs_out_is_handled_first(clock):
    """The edit's bound runs out on the pass that dequeues a co-tenant roster
    while the edit's own result waits behind it in the queue. The timer is read
    only with nothing queued, so the result is handled first and the edit is
    never cancelled: the turn runs on to its own deadline."""
    done = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-edit",
        "status": "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
    }
    # The pass that dequeues tick 18's first frame is the first one past the
    # bound (dispatch at tick 1, bound 16 ticks).
    expires = 1 + 16 + 1

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT)]
        if tick == expires:
            return [_co_tenant_roster(), _update(done)]
        return [_co_tenant_roster()]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == "timeout"
    assert turn.cancelled_at is None


@pytest.mark.asyncio
async def test_a_shell_call_keeps_the_oracle_rules(clock):
    """A shell call has no silence class: with the oracle arm out of the way the
    turn runs to its own deadline."""
    shell = _tool_call(
        "tc-sh", kind="execute", kiro={"toolName": "shell"}, raw={"command": "make test"}
    )

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        return [_update(shell)] if tick == 1 else [_co_tenant_roster()]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == "timeout"
    assert turn.cancelled_at is None


@pytest.mark.asyncio
async def test_a_harness_outside_the_set_keeps_the_oracle_rules(clock):
    """No positive identity on the frame, no timer: a claude-acp turn is unchanged."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        return [_update(_EDIT)] if tick == 1 else [_co_tenant_roster()]

    turn = _Turn(clock, script, backend="claude")

    events = await turn.run()

    assert events[-1].stop_reason == "timeout"


# ── Exception 1: Kiro Crew's own long tools heartbeat ──


@pytest.mark.parametrize("named", [True, False])
@pytest.mark.asyncio
async def test_a_heartbeating_kirocrew_tool_is_not_cancelled_until_it_stops(clock, named):
    """``wait`` on ``kirocrew-core`` heartbeats every 8 ticks until tick 200, far past
    its stdio bound; it is cancelled one bound after the last beat. A beat that
    names no call stamps every call on Kiro Crew's own servers, which is the same
    call here."""
    last_beat = 200

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_WAIT)]
        if tick % 8 == 0 and tick <= last_beat:
            stamped = handle.note_tool_heartbeat("tc-wait" if named else "")
            assert stamped == 1
        return []

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-wait"
    assert "silence_timer=mcp_stdio" in events[-1].text
    assert turn.cancelled_at == _at(last_beat) + _STDIO_BOUND + _TICK


@pytest.mark.asyncio
async def test_a_heartbeat_for_a_kirocrew_tool_never_stands_in_for_a_lost_edit(clock):
    """A heartbeat that names no call stamps only calls on Kiro Crew's own
    servers: the lost edit beside the beating ``wait`` is still cancelled."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT), _update(_WAIT)]
        if tick % 4 == 0:
            handle.note_tool_heartbeat("")
        return []

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(1) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_a_heartbeats_activity_time_is_never_read_past_its_arrival(clock):
    """A beat at tick 10 reports activity later than the clock reads: it restarts
    the ``wait`` at the beat, so the wait is still cut one bound after it."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_WAIT)]
        if tick == 10:
            assert handle.note_tool_heartbeat("tc-wait", activity_at=clock.now + _TURN) == 1
        return []

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].title == "tc-wait"
    assert turn.cancelled_at == _at(10) + _STDIO_BOUND + _TICK


# ── Exception 2: a sub-agent call counts its sub-agent's frames ──


def _owned_roster() -> JsonRpcMessage:
    """This session's own sub-agent roster: routed here alone, so not marked."""
    return JsonRpcMessage(
        method=METHOD_SUBAGENT_LIST_UPDATE,
        params={"subagents": [{"sessionId": "child-1", "status": "running"}]},
    )


@pytest.mark.parametrize("frame", [_child_text, _owned_roster], ids=["child-text", "owned-roster"])
@pytest.mark.asyncio
async def test_an_active_subagent_is_not_cancelled_until_it_goes_silent(clock, frame):
    """The sub-agent streams text, or its owned roster updates, every 6 ticks until
    tick 150, on a runtime that routes its frames here. The call is cancelled one
    sub-agent bound after the last of them."""
    last_frame = 150

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT)]
        if tick % 6 == 0 and tick <= last_frame:
            return [frame()]
        return []

    turn = _Turn(clock, script, routes=True)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-sub"
    assert "silence_timer=subagent" in events[-1].text
    assert turn.cancelled_at == _at(last_frame) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_a_subagent_running_a_shell_call_is_not_cancelled(clock):
    """The child starts a shell call at tick 4 that prints nothing; the parent's
    sub-agent call is not judged while it runs. The call's terminal at tick 120
    hands the bound back, counted from that frame."""
    child_shell = {
        "sessionUpdate": "tool_call",
        "toolCallId": "child-sh",
        "title": "npm test",
        "kind": "execute",
        "status": "pending",
        "rawInput": {"command": "npm test"},
        "_meta": {"kiro": {"toolName": "shell"}},
    }
    child_done = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "child-sh",
        "status": "completed",
        "kind": "execute",
        "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
    }

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT)]
        if tick == 4:
            return [_update(child_shell, session="child-1")]
        if tick == 120:
            return [_update(child_done, session="child-1")]
        return []

    turn = _Turn(clock, script, routes=True)

    events = await turn.run()

    assert events[-1].title == "tc-sub"
    assert turn.cancelled_at == _at(120) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_a_subagents_frames_never_stand_in_for_a_lost_edit(clock):
    """The child's frames restart only the sub-agent call: the edit dispatched
    beside it is cancelled on its own bound."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT), _update(_EDIT)]
        return [_child_text()]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(1) + _BOUND + _TICK


# ── A native sub-agent's own calls hold its parent's sub-agent call ──
#
# No Kiro Crew watchdog times a native child's calls, and a call the child has
# running sends nothing while it runs: a Kiro Crew tool's heartbeat names the
# child's call, not the parent's, and a person answering the child's question is a
# wait no frame shows. So any call the child has open keeps the parent's sub-agent
# call from being judged; each test then shows the timer acting again, one bound
# after the frame that closed the child's call.

#: The tick the child dispatches its call, and the tick its result arrives.
_CHILD_START = 4
_CHILD_END = 200


def _child_call(call_id: str, *, kind: str | None, kiro: dict, raw: dict | None = None) -> dict:
    return _tool_call(call_id, kind=kind, kiro=kiro, raw=raw)


def _child_done(call_id: str) -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": "done"}}],
    }


#: Every kind of call a native sub-agent can have running, by what it is.
_CHILD_CALLS: dict[str, dict] = {
    "kirocrew-wait": _child_call(
        "child-wait",
        kind="other",
        kiro={"toolName": "wait", "mcpServerName": "kirocrew-core"},
        raw={"seconds": 900},
    ),
    "kirocrew-ask-question": _child_call(
        "child-ask",
        kind="other",
        kiro={"toolName": "ask_question", "mcpServerName": "kirocrew-core"},
        raw={"question": "ok?"},
    ),
    "builtin-edit": _child_call(
        "child-edit", kind="edit", kiro={"toolName": "write"}, raw={"path": "a.txt"}
    ),
    "third-party-mcp": _child_call(
        "child-mcp", kind="other", kiro={"toolName": "search", "mcpServerName": "local"}
    ),
    # Only a built-in the harness stamps is one it never terminates; an MCP tool
    # that shares the name is an ordinary call.
    "mcp-tool-named-introspect": _child_call(
        "child-mcp-introspect",
        kind="other",
        kiro={"toolName": "introspect", "mcpServerName": "local"},
    ),
    "nested-subagent": _child_call(
        "child-sub", kind=None, kiro={"toolName": "subagent"}, raw={"task": "y"}
    ),
    "shell": _child_call(
        "child-sh", kind="execute", kiro={"toolName": "shell"}, raw={"command": "make"}
    ),
}


def _child_update(update: dict, spelling: str) -> JsonRpcMessage:
    """A child's frame under either session-update spelling kiro-cli sends."""
    if spelling == "extension":
        return JsonRpcMessage(
            method=METHOD_KIRO_SESSION_UPDATE,
            params={"sessionId": "child-1", "update": update},
        )
    return _update(update, session="child-1")


@pytest.mark.parametrize("spelling", ["plain", "extension"])
@pytest.mark.parametrize(
    ("call", "heartbeat"),
    [
        ("kirocrew-wait", "names-call"),
        ("kirocrew-wait", "names-none"),
        ("kirocrew-ask-question", "names-call"),
        ("kirocrew-ask-question", "names-none"),
        ("builtin-edit", "none"),
        ("third-party-mcp", "none"),
        ("mcp-tool-named-introspect", "none"),
        ("nested-subagent", "none"),
        ("shell", "none"),
    ],
)
@pytest.mark.asyncio
async def test_a_native_subagent_running_any_call_is_not_cancelled(
    clock, call, heartbeat, spelling
):
    """The parent's sub-agent call is dispatched at tick 1 and the child starts a
    call at tick 4 that runs, sending nothing, far past the sub-agent bound. A
    Kiro Crew tool beats every 8 ticks the way ``/api/session-keepalive`` hands
    it to the provider: naming the child's own call, or naming none. The parent's
    call is held while the child's call is open, and cut one bound after the
    child's result frame."""
    child = _CHILD_CALLS[call]
    call_id = child["toolCallId"]

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT)]
        if tick == _CHILD_START:
            return [_child_update(child, spelling)]
        if heartbeat != "none" and _CHILD_START < tick < _CHILD_END and tick % 8 == 0:
            handle.note_tool_heartbeat(call_id if heartbeat == "names-call" else "")
        if tick == _CHILD_END:
            return [_child_update(_child_done(call_id), spelling)]
        return []

    turn = _Turn(clock, script, routes=True)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-sub"
    assert "silence_timer=subagent" in events[-1].text
    assert turn.cancelled_at == _at(_CHILD_END) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_a_heartbeat_naming_a_childs_call_never_stands_in_for_the_parents_lost_wait(clock):
    """The parent's own ``wait`` is lost beside its sub-agent call while the child's
    ``wait`` beats under the child's call id. That beat is the sub-agent's activity:
    it restarts the sub-agent call, never the parent's own call on the same server,
    which is cut at its stdio bound."""
    child = _CHILD_CALLS["kirocrew-wait"]

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT), _update(_WAIT)]
        if tick == _CHILD_START:
            return [_child_update(child, "plain")]
        if tick > _CHILD_START and tick % 8 == 0:
            assert handle.note_tool_heartbeat("child-wait") == 1
        return []

    turn = _Turn(clock, script, routes=True)

    events = await turn.run()

    assert events[-1].title == "tc-wait"
    assert "silence_timer=mcp_stdio" in events[-1].text
    assert turn.cancelled_at == _at(1) + _STDIO_BOUND + _TICK


@pytest.mark.parametrize(
    "child",
    [
        pytest.param(
            {**_CHILD_CALLS["builtin-edit"], "status": "failed"}, id="opened-already-terminal"
        ),
        pytest.param(None, id="introspect-the-harness-never-terminates"),
    ],
)
@pytest.mark.asyncio
async def test_a_child_call_that_cannot_be_running_holds_nothing(clock, child):
    """A child's call that arrives already refused, and the child's ``introspect``,
    which kiro-cli never terminates, are not calls the child is waiting on: the
    parent's sub-agent call is cut one bound after that frame."""
    frame = child if child is not None else _captured_introspect()

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT)]
        if tick == _CHILD_START:
            return [_child_update(frame, "plain")]
        return []

    turn = _Turn(clock, script, routes=True)

    events = await turn.run()

    assert events[-1].title == "tc-sub"
    assert turn.cancelled_at == _at(_CHILD_START) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_past_the_call_map_bound_a_childs_calls_hold_the_call_for_the_turn(clock):
    """The child opens one call more than the call map holds and closes every one.
    The call it could not record may still be running, so the parent's sub-agent
    call stays held: the turn runs to its own deadline."""
    opened = MAX_ACTIVE_TOOL_CALLS + 1
    calls = [
        _child_call(f"child-{n}", kind="read", kiro={"toolName": "read"}) for n in range(opened)
    ]

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT)]
        if tick == _CHILD_START:
            return [_child_update(c, "plain") for c in calls]
        if tick == _CHILD_START + 1:
            return [_child_update(_child_done(c["toolCallId"]), "plain") for c in calls]
        return []

    turn = _Turn(clock, script, routes=True)

    events = await turn.run()

    assert events[-1].stop_reason == "timeout"
    assert turn.cancelled_at is None


@pytest.mark.parametrize("left", ["child-call-open", "overflow-mark"])
@pytest.mark.asyncio
async def test_a_new_turn_forgets_the_last_turns_child_calls(clock, left):
    """Turn 1 ends ``end_turn`` with its child's ``wait`` still open (no terminal
    frame came for it), or with only the overflow mark set (the child opened one
    call more than the map holds and closed them all). Turn 2, through the same
    ``prompt()`` turn start, dispatches a sub-agent call that is lost: turn start
    forgot both, so it is cut one bound after its dispatch."""
    many = [
        _child_call(f"child-{n}", kind="read", kiro={"toolName": "read"})
        for n in range(MAX_ACTIVE_TOOL_CALLS + 1)
    ]
    if left == "child-call-open":
        child_frames = [[_child_update(_CHILD_CALLS["kirocrew-wait"], "plain")], []]
    else:
        child_frames = [
            [_child_update(c, "plain") for c in many],
            [_child_update(_child_done(c["toolCallId"]), "plain") for c in many],
        ]
    turn_1 = [
        [_update(_SUBAGENT)],
        *child_frames,
        [_update(_child_done("tc-sub")), JsonRpcMessage(id=1, result={"stopReason": "end_turn"})],
    ]
    lost_at: list[float] = []

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if handle._prompt_starts == 1:
            return turn_1[tick - 1] if tick <= len(turn_1) else []
        if not lost_at:
            lost_at.append(clock.now - _START)
            return [_update({**_SUBAGENT, "toolCallId": "tc-sub-2", "title": "tc-sub-2"})]
        return []

    turn = _Turn(clock, script, routes=True)

    first = await turn.prompt()
    assert first[-1].stop_reason == STOP_REASON_END_TURN
    assert turn.handle._child_open_calls or turn.handle._child_calls_overflow
    second = await turn.prompt()

    assert second[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert second[-1].title == "tc-sub-2"
    assert turn.cancelled_at == lost_at[0] + _BOUND + _TICK


# ── A KAS sub-agent streams on this session, every frame naming its subtask ──

#: A KAS sub-agent call that reaches the tracker: a plain ``tool_call`` stamped
#: with the sub-agent tool's name. KAS's own spawn frame is taken for the roster
#: before the tracker (``test_a_kas_spawn_frame_gets_no_timer``).
_KAS_SUBAGENT = _tool_call(
    "tc-kas-sub", kind="other", kiro={"toolName": "invoke_sub_agent"}, raw={"task": "x"}
)


def _kas_subtask(update: dict) -> JsonRpcMessage:
    """*update* as a KAS sub-agent's frame: on this session, naming subtask st-1."""
    meta = dict(update.get("_meta") or {})
    meta["kiro"] = {**(meta.get("kiro") or {}), "agentSubtaskId": "st-1"}
    return _update({**update, "_meta": meta})


_KAS_SUBTASK_FRAMES: dict[str, dict] = {
    "thinking": {
        "sessionUpdate": "agent_thought_chunk",
        "content": {"type": "text", "text": "child thinking"},
    },
    "usage": {"sessionUpdate": "usage_update", "used": 10, "size": 1000},
    "text": {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "child text"},
    },
}


def _kas_pipeline() -> JsonRpcMessage:
    """KAS's pipeline frame: it names its stages' subtasks, never one of its own."""
    stage = {"name": "research", "status": "in_progress", "agentSubtaskId": "st-1"}
    return _update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-pipe",
            "_meta": {"kiro": {"pipeline": {"groupId": "g1", "stages": [stage]}}},
        }
    )


@pytest.mark.parametrize("kind", [*sorted(_KAS_SUBTASK_FRAMES), "pipeline"])
@pytest.mark.asyncio
async def test_a_kas_subagents_frames_of_every_kind_restart_its_call(clock, kind):
    """A KAS sub-agent's thinking, usage or text, or the pipeline frame naming its
    stage, arrives every 6 ticks until tick 150, while co-tenant rosters fan out
    on a runtime that cannot route a kiro-cli child here. KAS frames ride this
    session, so the call keeps its timer and is cut one bound after the last."""
    last_frame = 150

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_KAS_SUBAGENT)]
        if tick % 6 == 0 and tick <= last_frame and kind == "pipeline":
            return [_kas_pipeline()]
        if tick % 6 == 0 and tick <= last_frame:
            return [_kas_subtask(_KAS_SUBTASK_FRAMES[kind])]
        return [_co_tenant_roster()]

    turn = _Turn(clock, script, backend=ACP_BACKEND_KAS, routes=False)

    events = await turn.run()

    assert events[-1].title == "tc-kas-sub"
    assert "silence_timer=subagent" in events[-1].text
    assert turn.cancelled_at == _at(last_frame) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_a_kas_subagent_running_a_long_call_is_not_cancelled(clock):
    """The KAS sub-agent dispatches ``wait`` on ``kirocrew-core`` at tick 4 and its
    result comes at tick 200: the parent's call is held while it runs."""
    child_wait = _tool_call(
        "child-wait", kind="other", kiro={"serverName": "kirocrew-core"}, raw={"seconds": 900}
    )

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_KAS_SUBAGENT)]
        if tick == _CHILD_START:
            return [_kas_subtask(child_wait)]
        if tick == _CHILD_END:
            return [_kas_subtask(_child_done("child-wait"))]
        return []

    turn = _Turn(clock, script, backend=ACP_BACKEND_KAS)

    events = await turn.run()

    assert events[-1].title == "tc-kas-sub"
    assert turn.cancelled_at == _at(_CHILD_END) + _BOUND + _TICK


@pytest.mark.parametrize("beside", [False, True], ids=["alone", "beside-a-lost-edit"])
@pytest.mark.asyncio
async def test_a_kas_spawn_frame_gets_no_timer(clock, beside):
    """KAS's own sub-agent spawn (``kind: agent-subtask``) feeds the roster and never
    reaches the tracker: alone, nothing times it and the turn runs to its deadline;
    a lost edit beside it is still cut at its own bound."""
    spawn = {
        "sessionUpdate": "tool_call",
        "toolCallId": "invoke_subagent_st-1",
        "title": "Sub-agent: researcher",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "st-1"}},
    }
    edit = _tool_call("tc-edit", kind="edit", kiro={}, raw={"path": "a.txt"})

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(spawn), _update(edit)] if beside else [_update(spawn)]
        return []

    turn = _Turn(clock, script, backend=ACP_BACKEND_KAS)

    events = await turn.run()

    if beside:
        assert events[-1].title == "tc-edit"
        assert turn.cancelled_at == _at(1) + _BOUND + _TICK
    else:
        assert events[-1].stop_reason == "timeout"
        assert turn.cancelled_at is None


# ── A kiro-cli sub-agent this session cannot see has no timer ──
#
# kiro-cli streams a native sub-agent under the child's own sessionId, which the
# runtime routes to its parent only while the parent is the one session registered
# on it. On a shared runtime the child's frames are dropped, so its silence cannot
# be read here: the call keeps only the oracle and the hard cap.


@pytest.mark.asyncio
async def test_a_native_subagent_call_the_runtime_cannot_route_to_has_no_timer(clock):
    """The runtime says a child's frames do not reach this session, and nothing
    arrives: the busy sub-agent's call is not cut, the turn runs to its deadline."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        return [_update(_SUBAGENT)] if tick == 1 else []

    turn = _Turn(clock, script, routes=False)

    events = await turn.run()

    assert events[-1].stop_reason == "timeout"
    assert turn.cancelled_at is None


@pytest.mark.parametrize("co_tenant", [True, False], ids=["roster-fanned-out", "none"])
@pytest.mark.asyncio
async def test_a_frame_fanned_out_to_several_sessions_retires_a_native_subagent_call(
    clock, co_tenant
):
    """One co-tenant roster at tick 3 proves the runtime was shared then, whatever
    it says later: the child's frames of that time were dropped, so the call loses
    its timer. With no such frame the call is cut at its bound."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT)]
        if tick == 3 and co_tenant:
            return [_co_tenant_roster()]
        return []

    turn = _Turn(clock, script, routes=True)

    events = await turn.run()

    if co_tenant:
        assert events[-1].stop_reason == "timeout"
        assert turn.cancelled_at is None
    else:
        assert events[-1].title == "tc-sub"
        assert turn.cancelled_at == _at(1) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_a_lost_edit_beside_an_unseen_subagent_is_still_cut(clock):
    """Only the sub-agent call loses its timer: the edit dispatched beside it on the
    shared runtime is cut at its own bound."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_SUBAGENT), _update(_EDIT)]
        return [_co_tenant_roster()]

    turn = _Turn(clock, script, routes=False)

    events = await turn.run()

    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(1) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_kirocrews_own_subagent_call_keeps_its_timer_on_a_shared_runtime(clock):
    """``spawn_sub_agents`` is judged by its heartbeats, which reach this session on
    any runtime: with co-tenant rosters on every tick and no beat, it is cut at its
    bound."""
    spawn = _tool_call(
        "tc-ssa",
        kind="other",
        kiro={"toolName": "spawn_sub_agents", "mcpServerName": "kirocrew-core"},
        raw={"tasks": []},
    )

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        return [_update(spawn)] if tick == 1 else [_co_tenant_roster()]

    turn = _Turn(clock, script, routes=False)

    events = await turn.run()

    assert events[-1].title == "tc-ssa"
    assert turn.cancelled_at == _at(1) + _BOUND + _TICK


# ── Exception 3: a wait on a person pauses the timer ──

#: kiro-cli 2.21.4 asking for an approval: one turn's frames, in wire order.
_APPROVAL_CAPTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "kiro" / "session.jsonl"


@pytest.mark.asyncio
async def test_an_approval_wait_is_not_cancelled(clock):
    """The edit asks for approval at tick 2 and the person reads the card for
    four bounds. The consumer holds the permission event that whole time, which
    the handle banks as park time, so the edit's silence excludes it: the edit is
    cancelled one bound after the approval, not during the wait."""
    read_for = 4 * _BOUND

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(_EDIT)]
        if tick == 2:
            return [_permission()]
        return [_co_tenant_roster()]

    turn = _Turn(clock, script)
    approved_at: list[float] = []

    async def on_event(ev) -> None:
        if ev.kind == EVENT_PERMISSION_REQUEST:
            # The consumer parks on the card: what prompt() times around its
            # yield, banked when the person answers.
            turn.handle._parked_since = clock.now
            clock.now += read_for
            assert await turn.handle.approve_tool(ev.request_id)
            turn.handle._parked_since = None
            approved_at.append(clock.now - _START)

    turn.on_event = on_event

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert approved_at == [_at(2) + read_for]
    # The request restarts the edit's clock at tick 2, and the time the card
    # was read is not counted: one bound from the approval, plus the tick.
    assert turn.cancelled_at == _at(2) + read_for + _BOUND + _TICK
    assert turn.cancelled_at > approved_at[0]


def _captured_approval_order() -> list[str]:
    """What the capture sends naming the call it asks to approve, in order."""
    lines = _APPROVAL_CAPTURE.read_text(encoding="utf-8").splitlines()[1:]
    frames = [json.loads(line) for line in lines if line.strip()]
    asked = next(f for f in frames if f.get("method") == "session/request_permission")
    call_id = asked["params"]["toolCall"]["toolCallId"]
    order = []
    for frame in frames:
        update = (frame.get("params") or {}).get("update") or {}
        if update.get("sessionUpdate") in ("tool_call", "tool_call_update"):
            if update.get("toolCallId") == call_id:
                order.append(update["sessionUpdate"])
        elif frame is asked:
            order.append(frame["method"])
    return order


@pytest.mark.asyncio
async def test_a_call_lost_after_its_approval_is_cut_one_bound_after_its_last_frame(clock):
    """kiro-cli's approval order: the call, the permission request, then an update
    for the approved call. Through ``prompt()``, the consumer holds the request
    while the person reads the card for four bounds, which banks that park, and
    the update at tick 3 restarts the edit's clock after it. The edit is lost
    after that update: the park is subtracted once, so it is cut one bound after
    the update, not one bound plus the whole approval wait."""
    assert _captured_approval_order()[:3] == [
        "tool_call",
        "session/request_permission",
        "tool_call_update",
    ]
    read_for = 4 * _BOUND
    output = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-edit",
        "content": [{"type": "content", "content": {"type": "text", "text": "x"}}],
    }

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        frames = {1: [_update(_EDIT)], 2: [_permission()], 3: [_update(output)]}
        return frames.get(tick, [_co_tenant_roster()])

    turn = _Turn(clock, script)

    async def on_event(ev) -> None:
        if ev.kind == EVENT_PERMISSION_REQUEST:
            clock.now += read_for
            assert await turn.handle.approve_tool(ev.request_id)

    turn.on_event = on_event

    events = await turn.prompt()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(3) + read_for + _BOUND + _TICK


@pytest.mark.asyncio
async def test_a_heartbeat_while_the_consumer_holds_an_event_counts_only_the_park_after_it(clock):
    """Through ``prompt()``, the consumer holds the ``wait``'s tool_call event for
    four bounds (a slow channel send), and the wait's keepalive lands half way
    through, while that park is still open. The beat's park baseline takes the
    half already parked, so only the half after it is subtracted: the wait is cut
    one stdio bound after the hold ends."""
    hold = 4 * _BOUND

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        return [_update(_WAIT)] if tick == 1 else []

    turn = _Turn(clock, script)

    def on_event(ev) -> None:
        if ev.kind == EVENT_TOOL_CALL and ev.tool_call_id == "tc-wait":
            clock.now += hold / 2
            assert turn.handle.note_tool_heartbeat("tc-wait") == 1
            clock.now += hold / 2

    turn.on_event = on_event

    events = await turn.prompt()

    assert events[-1].title == "tc-wait"
    assert "silence_timer=mcp_stdio" in events[-1].text
    assert turn.cancelled_at == _at(1) + hold + _STDIO_BOUND + _TICK


# ── A call the harness never terminates ──

#: kiro-cli 3.1.0 through ``--agent-engine v2``, the argv Kiro Crew launches.
_CAPTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "kiro" / "tool-classes-a-live.jsonl"


def _captured_frames() -> list[dict]:
    lines = _CAPTURE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _captured_introspect() -> dict:
    """The ``tool_call`` the capture sends for ``introspect``, as the wire carried it."""
    for frame in _captured_frames():
        update = (frame.get("params") or {}).get("update") or {}
        kiro = (update.get("_meta") or {}).get("kiro") or {}
        if update.get("sessionUpdate") == "tool_call" and kiro.get("toolName") == "introspect":
            return update
    raise AssertionError("the capture carries no introspect tool_call")


def _own(kind: str, text: str = "working ") -> JsonRpcMessage:
    """This session's own model text or thinking."""
    return _update({"sessionUpdate": kind, "content": {"type": "text", "text": text}})


def test_the_capture_never_terminates_introspect():
    """The evidence: kiro-cli sends introspect's tool_call and no update for it ever
    follows; the model's text and the turn's end come next."""
    call_id = _captured_introspect()["toolCallId"]
    frames = _captured_frames()
    named = [
        f
        for f in frames
        if ((f.get("params") or {}).get("update") or {}).get("toolCallId") == call_id
    ]
    assert [f["params"]["update"]["sessionUpdate"] for f in named] == ["tool_call"]
    after = frames[frames.index(named[0]) + 1]
    assert after["params"]["update"]["sessionUpdate"] == "agent_message_chunk"


#: A built-in whose terminal frame never comes although the harness has moved past
#: it. Unlike ``introspect`` nothing names it as such, so it keeps its timer and
#: only the model's next step retires it.
_UNANSWERED_READ = _tool_call(
    "tc-read", kind="read", kiro={"toolName": "read"}, raw={"path": "a.txt"}
)


def _unterminated(name: str) -> dict:
    """The call the harness never terminates: the captured introspect, or a read."""
    return _captured_introspect() if name == "introspect" else _UNANSWERED_READ


def _build_call() -> dict:
    return _tool_call(
        "tc-build", kind="execute", kiro={"toolName": "shell"}, raw={"command": "make build"}
    )


def _build_output() -> JsonRpcMessage:
    """One line the running build prints, on its own call."""
    return _update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-build",
            "content": [{"type": "content", "content": {"type": "text", "text": "cc x.o\n"}}],
        }
    )


@pytest.mark.parametrize("own_kind", ["agent_message_chunk", "agent_thought_chunk"])
@pytest.mark.parametrize("call", ["introspect", "unanswered-read"])
@pytest.mark.asyncio
async def test_an_unterminated_call_does_not_cut_the_turn_the_model_goes_on_with(
    clock, call, own_kind
):
    """A call with no terminal frame, then the model writes or thinks on every tick:
    the healthy turn runs to its own deadline instead of ending in a tool stall at
    the built-in bound. introspect has no timer; the read's is retired by the
    model's next step."""
    unterminated = _unterminated(call)

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(unterminated)]
        return [_own(own_kind)]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == "timeout"
    assert turn.cancelled_at is None


@pytest.mark.parametrize("call", ["introspect", "unanswered-read"])
@pytest.mark.asyncio
async def test_an_unterminated_call_does_not_cut_a_build_streaming_after_it(clock, call):
    """A call with no terminal frame, then the model's next step: a line of text and
    a shell build that prints on every tick. The build is not cancelled for the
    first call's silence, and the shell call keeps the oracle's rules."""
    unterminated = _unterminated(call)

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(unterminated)]
        if tick == 2:
            return [_own("agent_message_chunk", "now building "), _update(_build_call())]
        return [_build_output()]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == "timeout"
    assert turn.cancelled_at is None


@pytest.mark.asyncio
async def test_introspect_does_not_cut_a_build_the_model_starts_with_no_text_between(clock):
    """introspect, verbatim from the capture, then the model's next step is the build
    itself, with no text or thinking first. Nothing retires introspect here, so it
    must carry no timer: kiro-cli's own stamp names it, and the build streaming on
    every tick runs to the turn's deadline."""
    introspect = _captured_introspect()

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(introspect)]
        if tick == 2:
            return [_update(_build_call())]
        return [_build_output()]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == "timeout"
    assert turn.cancelled_at is None


@pytest.mark.asyncio
async def test_introspect_does_not_cut_a_build_dispatched_in_its_batch(clock):
    """introspect and a build dispatched in the same step, the build then printing
    on every tick: no text ever follows introspect, and the turn is not cut."""
    introspect = _captured_introspect()

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(introspect), _update(_build_call())]
        return [_build_output()]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == "timeout"
    assert turn.cancelled_at is None


@pytest.mark.asyncio
async def test_a_lost_edit_in_a_batch_with_introspect_is_still_cut(clock):
    """introspect carries no timer, and nothing else about the batch changes: a lost
    edit dispatched beside it, with only a co-tenant talking after, is cut at its
    own bound and the stall names the edit."""
    introspect = _captured_introspect()

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(introspect), _update(_EDIT)]
        return [_co_tenant_roster()]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(1) + _BOUND + _TICK


@pytest.mark.asyncio
async def test_the_models_text_before_a_call_does_not_retire_it(clock):
    """The ticket's lost edit has no text of its own after it: text before the
    dispatch retires nothing of the call, which is still cut at its bound."""

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_own("agent_message_chunk", "editing it now "), _update(_EDIT)]
        return [_co_tenant_roster()]

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(1) + _BOUND + _TICK


def _ownerless_text() -> JsonRpcMessage:
    """Model text that names no session, fanned out to every co-tenant and marked."""
    return JsonRpcMessage(
        method="session/update",
        params={
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "someone's"},
            }
        },
        fanout_no_owner=True,
    )


def _kas_subtask_thought() -> JsonRpcMessage:
    """A KAS sub-agent's thinking, which names its subtask on this session's frame."""
    return _update(
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "child thinking"},
            "_meta": {"kiro": {"agentSubtaskId": "st-1"}},
        }
    )


@pytest.mark.parametrize(
    ("backend", "frame"),
    [
        (ACP_BACKEND_KIRO, _child_text),
        (ACP_BACKEND_KIRO, _ownerless_text),
        (ACP_BACKEND_KAS, _kas_subtask_thought),
    ],
    ids=["child-routed", "fanned-out-ownerless", "kas-subtask"],
)
@pytest.mark.asyncio
async def test_text_that_is_not_this_models_own_retires_nothing(clock, backend, frame):
    """A child's text, an ownerless fanned-out chunk and a KAS sub-agent's thought
    are not this model's next step: the lost edit is still cut at its bound."""
    edit = _tool_call("tc-edit", kind="edit", kiro={}, raw={"path": "a.txt"})

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        if tick == 1:
            return [_update(edit if backend == ACP_BACKEND_KAS else _EDIT)]
        return [frame()]

    turn = _Turn(clock, script, backend=backend)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert events[-1].title == "tc-edit"
    assert turn.cancelled_at == _at(1) + _BOUND + _TICK


# ── Which bound a call gets ──


@pytest.mark.parametrize(
    ("backend", "is_shell", "server", "tool", "kind", "expected"),
    [
        (ACP_BACKEND_KIRO, False, "", "write", "edit", SILENCE_CLASS_BUILTIN),
        (ACP_BACKEND_KIRO, False, "", "web_fetch", "read", SILENCE_CLASS_BUILTIN),
        (ACP_BACKEND_KAS, False, "", "", "edit", SILENCE_CLASS_BUILTIN),
        (ACP_BACKEND_KAS, False, "", "", "other", SILENCE_CLASS_NONE),
        (ACP_BACKEND_KIRO, True, "", "shell", "execute", SILENCE_CLASS_NONE),
        (ACP_BACKEND_KIRO, False, "", "subagent", "", SILENCE_CLASS_SUBAGENT),
        (ACP_BACKEND_KAS, False, "", "invoke_sub_agent", "other", SILENCE_CLASS_SUBAGENT),
        (ACP_BACKEND_KIRO, False, "kirocrew-core", "spawn_sub_agents", "", SILENCE_CLASS_SUBAGENT),
        (ACP_BACKEND_KAS, False, "kirocrew-core", "spawn_run", "", SILENCE_CLASS_SUBAGENT),
        (ACP_BACKEND_KIRO, False, "kirocrew-core", "spawn_continue", "", SILENCE_CLASS_SUBAGENT),
        (ACP_BACKEND_KIRO, False, "kirocrew-core", "wait", "", SILENCE_CLASS_MCP_STDIO),
        (ACP_BACKEND_KIRO, False, "remote", "search", "", SILENCE_CLASS_MCP_NETWORK),
        (ACP_BACKEND_KIRO, False, "local", "search", "", SILENCE_CLASS_MCP_STDIO),
        # A server that only borrows a sub-agent tool's name is an ordinary MCP call.
        (ACP_BACKEND_KIRO, False, "remote", "spawn_run", "", SILENCE_CLASS_MCP_NETWORK),
        # kiro-cli never terminates its introspect, so its stamp gets no timer; a
        # server's tool of that name and another harness's call keep theirs.
        (ACP_BACKEND_KIRO, False, "", "introspect", "read", SILENCE_CLASS_NONE),
        (ACP_BACKEND_KIRO, False, "local", "introspect", "", SILENCE_CLASS_MCP_STDIO),
        (ACP_BACKEND_KAS, False, "", "introspect", "read", SILENCE_CLASS_BUILTIN),
        ("claude", False, "", "Edit", "edit", SILENCE_CLASS_NONE),
    ],
)
def test_a_call_gets_the_bound_its_harness_identity_names(
    backend, is_shell, server, tool, kind, expected
):
    assert (
        classify_tool_silence(
            backend=backend,
            is_shell=is_shell,
            server=server,
            tool_name=tool,
            kind=kind,
            network_servers=frozenset({"remote"}),
        )
        == expected
    )


def test_a_bound_is_its_setting_capped_by_the_hard_cap_and_zero_turns_it_off():
    settings = WatchdogSettings(
        builtin_tool_silence_secs=300.0,
        mcp_network_silence_secs=0.0,
        mcp_stdio_silence_secs=9000.0,
        subagent_silence_secs=600.0,
        tool_stall_hard_cap_secs=7200.0,
    )
    assert silence_bound(settings, SILENCE_CLASS_BUILTIN) == 300.0
    assert silence_bound(settings, SILENCE_CLASS_MCP_NETWORK) == 0.0
    assert silence_bound(settings, SILENCE_CLASS_MCP_STDIO) == 7200.0
    assert silence_bound(settings, SILENCE_CLASS_SUBAGENT) == 600.0
    assert silence_bound(settings, SILENCE_CLASS_NONE) == 0.0


def test_the_defaults_are_the_decided_bounds():
    settings = WatchdogSettings()
    assert settings.builtin_tool_silence_secs == 300.0
    assert settings.mcp_network_silence_secs == 300.0
    assert settings.mcp_stdio_silence_secs == 1800.0
    assert settings.subagent_silence_secs == 600.0


def test_a_network_server_is_one_declared_with_a_url_and_no_command():
    from kiro_crew.acp.session_handle_mcp import mcp_network_servers

    spec = {
        "mcpServers": {
            "remote": {"url": "https://mcp.example.com/mcp"},
            "local": {"command": "uvx", "args": ["thing"]},
            "both": {"url": "https://mcp.example.com/b"},
        }
    }
    wire = [
        {"name": "both", "command": "node", "args": ["b.js"]},
        {"name": "w", "url": "https://w"},
    ]
    assert mcp_network_servers(spec, wire) == frozenset({"remote", "w"})
    assert mcp_network_servers(None, "garbage") == frozenset()


def test_the_bounds_are_settings_the_handle_snapshot_carries():
    from kiro_crew.acp.session_handle_watchdog import _load_watchdog_settings
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.section_builders import _build_watchdog_config

    section = _build_watchdog_config({"builtin_tool_silence_secs": 120, "subagent_silence_secs": 0})
    assert section.builtin_tool_silence_secs == 120.0
    assert section.subagent_silence_secs == 0.0
    assert section.mcp_network_silence_secs == 300.0
    assert section.mcp_stdio_silence_secs == 1800.0
    cfg = KiroCrewConfig()
    cfg.watchdog = section
    settings = _load_watchdog_settings(cfg=cfg)
    assert settings.builtin_tool_silence_secs == 120.0
    assert settings.subagent_silence_secs == 0.0
    assert settings.mcp_stdio_silence_secs == 1800.0


def test_the_recovery_prompt_hands_a_timed_out_call_back_as_an_error_to_retry():
    from kiro_crew.dashboard.state import build_tool_stall_recovery_prompt

    body = build_tool_stall_recovery_prompt("Editing a.txt", 300, timed_out=True)
    assert "cancelled Editing a.txt with a timeout error" in body
    assert "~5 minute(s)" in body
    assert "then retry it" in body
    assert "timeout error" not in build_tool_stall_recovery_prompt("Editing a.txt", 300)


def test_only_a_silence_expiry_reads_as_a_timeout():
    text = "verdict=unknown; idle_secs=301; processes_terminated=0; silence_timer=builtin: x"
    assert is_silence_timeout(text)
    assert not is_silence_timeout("verdict=dead; idle_secs=40; processes_terminated=1; shell child")
    assert not is_silence_timeout("")


def test_a_calls_server_name_is_kept_to_the_retained_length():
    """The server name the keepalive matches on is retained like the call's other names."""
    from kiro_crew.acp.liveness_command import MAX_RETAINED_NAME_CHARS
    from kiro_crew.acp.liveness_tool_state import ToolCallState

    state = ToolCallState(silence_server="s" * (MAX_RETAINED_NAME_CHARS + 9))
    assert state.silence_server == "s" * MAX_RETAINED_NAME_CHARS


@pytest.mark.asyncio
async def test_a_delayed_heartbeat_does_not_count_the_park_before_its_activity_as_silence(clock):
    """The consumer holds the sub-agent call's event for four bounds, and a beat lands
    half way through reporting the sub-agent last moved a tick into the hold. Every
    instant of the hold is a park, so the call has no silence to charge at the end of
    it: it is cut one sub-agent bound after the hold ends, not sooner."""
    hold = 4 * _BOUND

    def script(tick: int, handle: AcpSessionHandle) -> list[JsonRpcMessage]:
        return [_update(_SUBAGENT)] if tick == 1 else []

    turn = _Turn(clock, script, routes=True)

    def on_event(ev) -> None:
        if ev.kind == EVENT_TOOL_CALL and ev.tool_call_id == "tc-sub":
            clock.now += _TICK
            moved = clock.now
            clock.now += hold / 2
            assert turn.handle.note_tool_heartbeat("tc-sub", activity_at=moved) == 1
            clock.now += hold / 2

    turn.on_event = on_event

    events = await turn.prompt()

    assert events[-1].title == "tc-sub"
    assert turn.cancelled_at is not None
    assert turn.cancelled_at >= _at(1) + _TICK + hold + _BOUND
