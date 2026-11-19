"""A Stop pressed while a dashboard turn is still being prepared.

Before a turn opens its provider stream it can wait a long time: in Shared
Crew fair admission, in a cold session start, in its context build. A Stop
pressed then finds no provider turn, so ``SessionManager.stop_turn`` answers
``"idle"`` and the stop card settles. These tests run the real ``_run_chat``
through ``turn_harness.run_turn`` with the press made from another task while
preparation is held on an event, and fake only the provider edge (the claim,
admission and the ``stop_turn`` answer):

* the press ends the preparing runner at once, so a preparation that never
  finishes can still be stopped, and the runner's own tail hands the queue
  behind it off exactly once;
* the press cancels the record's own runner, the inner task a bounded turn
  runs under ``asyncio.shield``, and that still ends the outer ``slot.task``;
* a second press while that runner unwinds cancels nothing;
* a runner that already opened its stream is never cancelled by an ``idle``
  answer.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request
from turn_harness import Do, TurnContext, TurnScript, Wait, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
#: Bound on every wait a test releases itself, in the harness's virtual seconds.
_BOUND = 30.0


@pytest.fixture(autouse=True)
def _quiet_audit(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    # The stop handlers write their audit line through the facade's ``sel``.
    from kiro_crew.dashboard import chat_handlers

    _floor_monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())


class _Press:
    """What a press made from outside the turn saw."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.replies: list[Any] = []
        self.ended_before_release: bool | None = None
        self.stop_state = ""
        self.task: Any = None
        self.runner: Any = None
        self.second_press_cancelled: bool | None = None
        self.unwinding_at_second_press: bool | None = None


def _answer_idle(ctx: TurnContext) -> None:
    async def _stop_turn(_key: str, **_kwargs: Any) -> str:
        return "idle"

    ctx.state.sessions.stop_turn.side_effect = _stop_turn
    ctx.state.sessions.is_compacting.side_effect = lambda *_a, **_k: False
    ctx.state._slots[ctx.slot.key] = ctx.slot


def _hold_the_claim(ctx: TurnContext, press: _Press) -> None:
    """The cold session start waits on *press.release*."""
    answer = ctx.state.sessions.get_or_create.side_effect

    async def _claim(*args: Any, **kwargs: Any) -> Any:
        press.entered.set()
        await asyncio.wait_for(press.release.wait(), _BOUND)
        return await answer(*args, **kwargs)

    ctx.state.sessions.get_or_create.side_effect = _claim


def _hold_admission(monkeypatch: pytest.MonkeyPatch, press: _Press) -> None:
    """Fair admission waits on *press.release*, then lets the turn run."""
    from kiro_crew.shared_crew import turn_gate

    async def _admit(**_kwargs: Any) -> None:
        press.entered.set()
        await asyncio.wait_for(press.release.wait(), _BOUND)
        return None

    monkeypatch.setattr(turn_gate, "admit_turn", _admit)


def _press_while_held(
    ctx: TurnContext,
    press: _Press,
    act: Callable[[TurnContext], Any],
) -> None:
    """From another task: once preparation holds, *act*, then see whether the
    runner ended on its own before preparation is released."""

    async def _presser() -> None:
        try:
            await asyncio.wait_for(press.entered.wait(), _BOUND)
            press.task = ctx.slot.task
            preparing = getattr(ctx.slot, "_preparing_turn", None)
            press.runner = getattr(preparing, "runner", None)
            press.replies.append(await act(ctx))
            press.stop_state = ctx.slot._stop_state
            if preparing is not None and press.runner is not None:
                # A second press while the runner unwinds: the record was
                # cleared before the first cancel, so it finds nothing.
                from kiro_crew.dashboard.chat_handlers import _cancel_preparing_runner

                press.unwinding_at_second_press = not press.runner.done()
                press.second_press_cancelled = _cancel_preparing_runner(ctx.slot, preparing)
            done, _ = await asyncio.wait({press.task}, timeout=5.0)
            press.ended_before_release = bool(done)
        finally:
            press.release.set()

    ctx.state._background_tasks.add(asyncio.ensure_future(_presser()))


async def _stop(ctx: TurnContext) -> Any:
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    return await stop_slot_turn(ctx.state, ctx.slot)


def _interrupt_request(state: Any, slot_key: str, queue_id: str) -> web.Request:
    app = web.Application()
    app["state"] = state
    body = json.dumps({"queue_id": queue_id}).encode()
    payload = streams.StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    payload.feed_data(body)
    payload.feed_eof()
    return make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot_key}/interrupt",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        match_info={"slot": slot_key},
        app=app,
        payload=payload,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("held_in", ["session_claim", "admission"])
async def test_stop_ends_a_preparing_runner_and_its_tail_hands_the_queue_off(
    held_in: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    press = _Press()
    if held_in == "admission":
        _hold_admission(monkeypatch, press)

    def setup(ctx: TurnContext) -> None:
        _answer_idle(ctx)
        if held_in == "session_claim":
            _hold_the_claim(ctx, press)
        ctx.slot.queue_append("sent during preparation")
        _press_while_held(ctx, press, _stop)

    record = await run_turn(TurnScript(events=[_REPLY, _DONE], setup=setup))

    # The press ended the runner itself: a preparation that never finishes
    # cannot keep the slot running behind a settled stop card.
    assert press.ended_before_release is True
    # The cancelled task is the record's own runner, the inner task the bounded
    # turn runs under ``asyncio.shield``, and the outer ``slot.task`` ended too.
    # (``_run_chat`` may absorb the cancel and return, so "done", not "cancelled").
    assert press.runner is not None and press.runner is not press.task
    assert press.runner.done() and press.task.done()
    # A second press while that runner unwinds cancels nothing.
    assert press.unwinding_at_second_press is True
    assert press.second_press_cancelled is False
    assert record.calls("stream") == []
    assert press.replies == [{"ok": True}]
    assert press.stop_state == "idle"
    # The runner's own tail handed the queue off, once.
    assert [call.args[0] for call in record.successors] == ["sent during preparation"]


@pytest.mark.asyncio
async def test_interrupt_ends_a_preparing_runner_and_the_chosen_card_runs_once() -> None:
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

    press = _Press()

    async def _interrupt(ctx: TurnContext) -> Any:
        chosen = ctx.slot.queue_append("chosen card")
        response = await api_chat_slot_interrupt(
            _interrupt_request(ctx.state, ctx.slot.key, chosen)
        )
        return json.loads(response.body)

    def setup(ctx: TurnContext) -> None:
        _answer_idle(ctx)
        _hold_the_claim(ctx, press)
        ctx.slot.queue_append("queued first")
        _press_while_held(ctx, press, _interrupt)

    record = await run_turn(TurnScript(events=[_REPLY, _DONE], setup=setup))

    assert press.replies == [{"ok": True, "outcome": "idle"}]
    assert press.ended_before_release is True
    assert press.runner is not press.task and press.runner.done()
    assert press.second_press_cancelled is False
    assert record.calls("stream") == []
    assert [call.args[0] for call in record.successors] == ["chosen card"]


@pytest.mark.asyncio
async def test_an_idle_answer_never_cancels_a_runner_whose_stream_is_open() -> None:
    press = _Press()

    def _press_mid_stream(ctx: TurnContext) -> None:
        press.entered.set()

    def setup(ctx: TurnContext) -> None:
        _answer_idle(ctx)
        _press_while_held(ctx, press, _stop)

    record = await run_turn(
        TurnScript(events=[_REPLY, Do(_press_mid_stream), Wait(1), _DONE], setup=setup)
    )

    # The provider held no turn while the slot still read running: named, not
    # reported as a stop, and the reply finishes.
    assert press.replies == [{"ok": True, "info": "no active turn"}]
    assert not press.task.cancelled()
    assert [row["content"] for row in record.rows("assistant")] == ["the reply"]
    assert record.stop_reason == STOP_REASON_END_TURN
