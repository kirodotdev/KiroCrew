"""A steer sent while a turn streams plain text reaches THAT turn.

A text-only generation offers no tool-call boundary, so the turn has one safe
point left for a steer: the end of the text block. kiro-cli 2.30.0 takes the
steer there, inside the SAME ``session/prompt``, on both engines Crew drives:

* kiro (default engine): ``AgentExecutionUserMessageQueued`` on
  ``_kiro.dev/session/update`` when the steer is written, then
  ``AgentExecutionSteeringInjected`` on the same method once the text block ends,
  then the model's reply to the steer, then ``end_turn``;
* KAS (``--agent-engine v3``): ``session_info_update`` frames whose
  ``_meta.kiro.kind`` is ``steering_queued`` and then ``steering_injected``, the
  reply, ``turn_end``, and a ``steering_cleared`` naming the injected steer just
  before the prompt answers.

The frames below keep the order and the shape of those live runs (ids, prose and
the model's reply are this file's own). These tests drive them through the real
reader loop and session handle, then hand the echo to the dashboard's real
settle and teardown, and pin the outcome: the steer settles as consumed by the
turn it was written into, and the teardown requeues nothing, so no second turn
runs the same text. A runtime that sends no injection echo leaves the steer to
the teardown's requeue fallback, and these assertions catch that.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from test_acp_runtime import (
    _await_routed,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
    EVENT_STEER_QUEUED,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_SESSION_UPDATE,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.testing.wait import await_until

SID = "sA"
STEER = "go north instead"
#: What ``steer()`` writes and what both engines echo back: the steer RPC wraps
#: the text, and the live echoes carried the wrapped form verbatim.
WRAPPED = f"<user_message>\n{STEER}\n</user_message>"
ESSAY = ["Rivers ", "shaped ", "the first ", "cities."]
REPLY = ["Heading ", "north."]


def _chunk(text: str) -> dict:
    return {
        "method": METHOD_SESSION_UPDATE,
        "params": {
            "sessionId": SID,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


def _kiro_steer(discriminant: str) -> dict:
    return {
        "method": METHOD_KIRO_SESSION_UPDATE,
        "params": {
            "sessionId": SID,
            "update": {"sessionUpdate": discriminant, "messageId": "steer-1", "content": WRAPPED},
        },
    }


def _kas_info(kiro: dict) -> dict:
    return {
        "method": METHOD_SESSION_UPDATE,
        "params": {
            "sessionId": SID,
            "update": {"sessionUpdate": "session_info_update", "_meta": {"kiro": kiro}},
        },
    }


def _kiro_frames() -> tuple[list[dict], list[dict]]:
    """(frames before the steer is written, frames after) on the default engine."""
    before = [_chunk(ESSAY[0])]
    after = [
        _kiro_steer("AgentExecutionUserMessageQueued"),
        *[_chunk(t) for t in ESSAY[1:]],
        # The live run's context-meter frame between the text block and the
        # injection: the turn has not ended, so nothing may read it as a terminal.
        {
            "method": "_kiro.dev/metadata",
            "params": {"sessionId": SID, "contextUsagePercentage": 25.0},
        },
        _kiro_steer("AgentExecutionSteeringInjected"),
        *[_chunk(t) for t in REPLY],
    ]
    return before, after


def _kas_frames() -> tuple[list[dict], list[dict]]:
    """(frames before the steer is written, frames after) on the KAS engine."""
    before = [
        _kas_info({"turnStart": True, "kind": "turn_start", "messageId": "t-1-turn-start"}),
        _chunk(ESSAY[0]),
    ]
    after = [
        _kas_info({"kind": "steering_queued", "messageId": "steer-1", "content": WRAPPED}),
        *[_chunk(t) for t in ESSAY[1:]],
        _kas_info({"kind": "context_usage", "usagePercentage": 4.7}),
        _kas_info({"kind": "steering_injected", "messageId": "steer-1", "content": WRAPPED}),
        *[_chunk(t) for t in REPLY],
        _kas_info(
            {"turnEnd": {"stopReason": "end_turn"}, "kind": "turn_end", "stopReason": "end_turn"}
        ),
        _kas_info({"kind": "steering_cleared", "messageIds": ["steer-1"]}),
    ]
    return before, after


FRAMES = {ACP_BACKEND_KIRO: _kiro_frames, ACP_BACKEND_KAS: _kas_frames}


async def _text_only_turn(backend: str, on_steer_window=None) -> list:
    """One text-only prompt on the shared runtime with a steer written mid-stream.

    ``on_steer_window`` runs after the first text chunk has been dispatched and
    before the runtime's queued echo, which is where the dashboard writes the
    steer in the live run.
    """
    before, after = FRAMES[backend]()
    rt, reader, _ = _make_runtime()
    rt._acp_backend = backend
    q = _register(rt, SID)
    handle = AcpSessionHandle(SID, q[SID], rt)
    task = await _start_reader(rt)
    events: list = []
    streaming = asyncio.Event()
    driver: asyncio.Future | None = None
    try:

        async def drive():
            async for ev in handle.prompt("write an essay", timeout=600.0):
                events.append(ev)
                if ev.kind == EVENT_TEXT_CHUNK:
                    streaming.set()

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, SID))[SID]
        for frame in before:
            _feed(reader, frame)
        await await_until(streaming, what="the first text chunk reaching the turn")
        if on_steer_window is not None:
            await on_steer_window()
        for frame in after:
            _feed(reader, frame)
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
    finally:
        # A failed barrier leaves the prompt pending; settle it before the reader
        # stops so no task outlives the test.
        if driver is not None and not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        await _stop_reader(task)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS], ids=["kiro", "kas"])
async def test_the_injection_echo_lands_inside_the_text_only_turn(backend):
    events = await _text_only_turn(backend)
    kinds = [e.kind for e in events]

    # Text only: the turn never offered a tool-call boundary.
    assert EVENT_TOOL_CALL not in kinds
    assert EVENT_STEER_QUEUED in kinds
    consumed = [e for e in events if e.kind == EVENT_STEER_CONSUMED]
    assert [e.text for e in consumed] == [WRAPPED]
    # Consumed by THIS turn: the echo comes after the text block and before the
    # turn's terminal, and the reply to the steer streams inside the same turn.
    at = kinds.index(EVENT_STEER_CONSUMED)
    assert kinds.index(EVENT_STEER_QUEUED) < at < kinds.index(EVENT_COMPLETE)
    texts_after = "".join(e.text for e in events[at:] if e.kind == EVENT_TEXT_CHUNK)
    assert texts_after == "".join(REPLY)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS], ids=["kiro", "kas"])
async def test_the_dashboard_settles_the_steer_and_requeues_nothing(tmp_path, monkeypatch, backend):
    """The teardown must not run a steer the turn already consumed a second time."""
    from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
    from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers, _settle_consumed_steers

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("test")
    running = MagicMock()
    running.done.return_value = False
    slot.task = running
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=True)
    slot._acp_client = client

    outcomes: list[str] = []

    async def write_steer():
        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            outcomes.append(await steer_into_running_turn(state, slot, STEER))

    events = await _text_only_turn(backend, on_steer_window=write_steer)
    assert outcomes == [STEER_STEERED]
    assert slot._pending_steers == [STEER]

    # What `_run_chat` does with each event of the turn, then its teardown.
    for ev in events:
        if ev.kind == EVENT_STEER_CONSUMED:
            _settle_consumed_steers(slot, ev.text or "", state)
    _requeue_unconsumed_steers(state, slot)

    assert slot._pending_steers == []
    assert slot._queue == []
    row = next(m for m in slot.messages if (m.get("meta") or {}).get("steer"))
    assert row["meta"].get("steerState") == "consumed"
    assert not any(
        c.args[0] == "queue_push" for c in state.broadcast_ws.call_args_list
    ), "the teardown pushed a queue card for a steer the turn already consumed"
