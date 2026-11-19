"""A dispatch that ends before its turn starts hands the queue off
(``turn_dispatch.mark_turn_unstarted``), and that hand-off never starts a session a
code channel's Stop holds (``stop_hold``).

The counterpart of ``test_unstarted_turn_queue_handoff``: the same cancelled-before-it-runs
dispatch, on a held slot. A queued entry no person wrote stays queued; a person's entry
stamped after the Stop releases the hold and runs.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chat_mode_security_helpers import (  # noqa: F401 -- fixtures are requested by name
    _hermetic_home,
    _isolate_safety_override,
    state_fixture,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_delivery import queue_for_next_turn
from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn
from kiro_crew.slack.code_channels import stop_hold
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture(autouse=True)
def _no_pending() -> Any:
    stop_hold._keys.clear()
    stop_hold._channels.clear()
    yield
    stop_hold._keys.clear()
    stop_hold._channels.clear()


class _TurnRecorder:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.ran = asyncio.Event()

    async def __call__(self, _state: Any, _slot: Any, message: str, **_kwargs: Any) -> None:
        self.messages.append(message)
        self.ran.set()


def _held(slot: Any) -> None:
    stop_hold.Pressed(next(stop_hold._presses)).hold(slot)
    stop_hold._last = slot._stop_hold_seq


@pytest.mark.asyncio
@pytest.mark.parametrize("person", [False, True], ids=["unstamped", "persons-stamp"])
async def test_a_cancelled_unstarted_dispatch_never_restarts_a_held_queue(
    state: Any, monkeypatch: pytest.MonkeyPatch, person: bool
) -> None:
    slot = state.get_or_create_slot("w-held", app="crew-keyboard")
    _held(slot)
    successor = _TurnRecorder()
    monkeypatch.setattr(chat_runner, "_run_chat", successor)

    slot.task = spawn_guarded_turn(
        state,
        slot,
        state.run_background_turn(slot, chat_runner._run_chat(state, slot, "cancelled")),
    )
    assert slot.running is True
    stamp = stop_hold.stamp(stop_hold.seen(), True) if person else ""  # after the Stop
    queue_for_next_turn(
        state, slot, "queued behind the cancelled turn", turn_actor="app", stop_hold_stamp=stamp
    )
    holder = slot.task
    holder.cancel()

    if person:
        await await_until(successor.ran, what="the person's queued turn")
        assert successor.messages == ["queued behind the cancelled turn"]
        return
    # The cancelled holder's done callback hands the floor to the hand-off's own task;
    # the slot is free only once that task ended (a drained queue would hold it with
    # the successor's turn first).
    await async_wait_until(
        lambda: slot.task is not holder and slot.task is None,
        describe=lambda: f"the hand-off to end: {slot.task!r}",
    )
    assert successor.messages == [], "a held queue was started with no person's message"
    assert len(slot._queue) == 1 and "queued behind the cancelled turn" in str(slot._queue[0])
    assert stop_hold.decide(slot, "") == stop_hold.HELD
