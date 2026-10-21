from __future__ import annotations

import asyncio

import pytest

from kiro_crew.dashboard.slot_preparing import is_preparing, preparing_message


class _State:
    def __init__(self, slot) -> None:
        self.slot = slot
        self.pushes: list[tuple[bool, bool]] = []

    def push_slots_update(self) -> None:
        payload = self.slot.to_dict()
        self.pushes.append((payload["preparing"], payload["running"]))


def _slot():
    from kiro_crew.dashboard.state import _ChatSlot

    return _ChatSlot("thread-p")


def test_a_fresh_slot_is_not_preparing():
    slot = _slot()
    assert is_preparing(slot) is False
    assert slot.to_dict()["preparing"] is False


def test_preparing_rides_the_slots_frame_and_never_marks_a_turn():
    slot = _slot()
    state = _State(slot)
    with preparing_message(state, slot):
        assert slot.to_dict()["preparing"] is True
        assert slot.running is False
        assert slot.turn_running is False
    assert state.pushes == [(True, False), (False, False)]


def test_overlapping_producers_do_not_clear_each_other():
    slot = _slot()
    state = _State(slot)
    with preparing_message(state, slot):
        with preparing_message(state, slot):
            pass
        assert is_preparing(slot) is True
    assert is_preparing(slot) is False


def test_an_error_inside_the_span_still_clears_the_flag():
    slot = _slot()
    state = _State(slot)
    with pytest.raises(RuntimeError):
        with preparing_message(state, slot):
            raise RuntimeError("boom")
    assert is_preparing(slot) is False
    assert state.pushes[-1] == (False, False)


@pytest.mark.asyncio
async def test_cancellation_inside_the_span_clears_the_flag():
    slot = _slot()
    state = _State(slot)
    entered = asyncio.Event()

    async def work() -> None:
        with preparing_message(state, slot):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), timeout=30)
    assert is_preparing(slot) is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=30)
    assert is_preparing(slot) is False


def test_a_new_slot_after_a_gateway_restart_reads_not_preparing():
    # The flag is memory-only: the replacement slot a restarted gateway builds
    # cannot inherit a spinner from work that died with the old process.
    slot = _slot()
    with preparing_message(_State(slot), slot):
        reborn = _slot()
        assert reborn.to_dict()["preparing"] is False
