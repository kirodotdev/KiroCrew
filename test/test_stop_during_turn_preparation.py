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
* a runner that already opened its stream is not cancelled by an ``idle``
  answer while it finishes within the soft-stop budget;
* one still running after the budget (its provider turn over) is ended by the
  press, by Stop and by Run now alike.
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


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["stop", "interrupt"])
async def test_an_idle_answer_wakes_priority_after_the_runner_finishes(route: str) -> None:
    from kiro_crew.dashboard import responsive_chat
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

    press = _Press()

    async def _act(ctx: TurnContext) -> Any:
        if route == "stop":
            return await _stop(ctx)
        chosen = responsive_chat.first_priority_id(ctx.slot)
        assert chosen is not None
        response = await api_chat_slot_interrupt(
            _interrupt_request(ctx.state, ctx.slot.key, chosen)
        )
        return json.loads(response.body)

    def setup(ctx: TurnContext) -> None:
        _answer_idle(ctx)
        chosen = ctx.slot.queue_append("responsive follow-up", directive_user_origin=True)
        ctx.slot._queue[-1][responsive_chat.RESPONSIVE_KEY] = True
        assert responsive_chat.first_priority_id(ctx.slot) == chosen
        _press_while_held(ctx, press, _act)

    record = await run_turn(
        TurnScript(
            events=[_REPLY, Do(lambda _ctx: press.entered.set()), Wait(1), _DONE],
            setup=setup,
        )
    )

    expected = (
        {"ok": True, "info": "no active turn"}
        if route == "stop"
        else {"ok": True, "outcome": "idle"}
    )
    assert press.replies == [expected]
    assert press.ended_before_release is True
    assert not press.task.cancelled()
    assert record.stop_reason == STOP_REASON_END_TURN
    assert press.stop_state == "idle"
    assert [call.args[0] for call in record.successors] == ["responsive follow-up"]


#: Virtual seconds a runner stays wedged after its provider turn: past the soft-stop
#: budget's 60 s ceiling, whatever the config says.
_WEDGED = 3600.0


def _stop_card_states(ctx: TurnContext) -> list[str]:
    states = []
    for msg in ctx.slot.messages:
        try:
            data = json.loads(msg.get("cls") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("kind") == "stop_event":
            states.append(data["state"])
    return states


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["stop", "interrupt"])
async def test_a_press_ends_a_runner_that_outlives_its_provider_turn(route: str) -> None:
    """The provider holds no turn, yet the runner never ends. The press
    waits the soft-stop budget, then ends the runner, so the slot goes idle and
    the composer takes the next prompt instead of every press adding a card."""
    press = _Press()
    cards: list[list[str]] = []

    async def _interrupt(ctx: TurnContext) -> Any:
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        chosen = ctx.slot.queue_append("chosen card")
        response = await api_chat_slot_interrupt(
            _interrupt_request(ctx.state, ctx.slot.key, chosen)
        )
        return json.loads(response.body)

    async def _act(ctx: TurnContext) -> Any:
        reply = await (_stop(ctx) if route == "stop" else _interrupt(ctx))
        cards.append(_stop_card_states(ctx))
        return reply

    def setup(ctx: TurnContext) -> None:
        _answer_idle(ctx)
        ctx.slot.queue_append("queued first")
        _press_while_held(ctx, press, _act)

    record = await run_turn(
        TurnScript(
            events=[_REPLY, Do(lambda _ctx: press.entered.set()), Wait(_WEDGED), _DONE],
            setup=setup,
        )
    )

    if route == "stop":
        assert press.replies == [{"ok": True}]
        assert cards == [["stopped"]]
        successors = ["queued first"]
    else:
        assert press.replies == [{"ok": True, "outcome": "idle"}]
        successors = ["chosen card"]
    # The press ended the runner long before the wedge would have let go.
    assert press.ended_before_release is True
    assert press.runner is not None and press.runner.done() and press.task.done()
    assert press.stop_state == "idle"
    assert record.stop_reason != STOP_REASON_END_TURN
    assert [call.args[0] for call in record.successors] == successors


@pytest.mark.asyncio
async def test_a_second_press_during_the_wait_escalates_and_owns_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing again while the first press waits out the budget is the hard
    kill. The first press still ends the runner after the budget, but never labels
    the card "stopped": the escalation owns the card."""
    from kiro_crew.dashboard import turn_dispatch

    press = _Press()
    seen: dict[str, Any] = {}
    # Set when the press reads its budget, right before it parks in the wait.
    waiting = asyncio.Event()
    read_budget = turn_dispatch.native_cancel_ack_timeout_secs

    def _budget() -> float:
        waiting.set()
        return read_budget()

    monkeypatch.setattr(
        turn_dispatch, "native_cancel_ack_timeout_secs", _budget  # flake-ok: spy, real value kept
    )

    async def _two_presses(ctx: TurnContext) -> Any:
        # The turn's own start reads the budget too; only the press's read counts.
        waiting.clear()
        first = asyncio.ensure_future(_stop(ctx))
        await asyncio.wait_for(waiting.wait(), _BOUND)
        seen["second"] = await _stop(ctx)
        seen["first"] = await first
        seen["cards"] = _stop_card_states(ctx)
        return seen["first"]

    def setup(ctx: TurnContext) -> None:
        _answer_idle(ctx)
        _press_while_held(ctx, press, _two_presses)

    await run_turn(
        TurnScript(
            events=[_REPLY, Do(lambda _ctx: press.entered.set()), Wait(_WEDGED), _DONE],
            setup=setup,
        )
    )

    assert seen["second"] == {"ok": True}
    assert press.ended_before_release is True
    assert press.runner is not None and press.runner.done() and press.task.done()
    assert "stopped" not in seen["cards"]
    assert press.stop_state == "killing"
