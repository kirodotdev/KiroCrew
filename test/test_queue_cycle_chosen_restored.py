"""A "Run now" on a restored card that lands while a turn is ending still runs.

The turn's end drain holds every restored card the user has not chosen, and the
click's own start refuses while the ending turn still holds the slot. The cycle
therefore starts a chosen card once it has released the slot, under the same
idle-slot checks the click's own start uses.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from chat_runner_coverage_helpers import _slot, _state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY, RESTORED_SEND_CHOSEN_KEY
from kiro_crew.testing.wait import await_until


def _setup(tmp_path, *, chosen: bool = False):
    state, slot = _state(tmp_path), _slot()
    state._slots[slot.key] = slot
    entry = {"id": "r1", "content": "restored card", RESTORED_QUEUE_KEY: True}
    if chosen:
        entry[RESTORED_SEND_CHOSEN_KEY] = True
    slot._queue[:] = [entry]
    return state, slot, entry


async def _finish(state, slot, *, mark_during_read=None, starts=True, drain=True, during=None):
    started: list[object] = []

    async def done_payload(_st, _sl, **_kwargs):
        if mark_during_read is not None:
            # The click promotes the card and marks it chosen inside this await,
            # while the ending turn still holds the slot.
            mark_during_read[RESTORED_SEND_CHOSEN_KEY] = True
        if during is not None:
            during()
        return {}

    async def record_start(_st, _sl, **kwargs):
        started.append(kwargs.get("required_queue_id"))
        return starts

    with (
        patch.object(chat_runner, "chat_done_payload", new=done_payload),
        patch.object(chat_runner, "_start_next_queued_turn", new=record_start),
    ):
        await chat_runner._finish_queue_cycle(state, slot, drain=drain)
        pending = list(state._background_tasks)
        if pending:
            await await_until(asyncio.gather(*pending), what="the chosen-card start")
    return started


@pytest.mark.asyncio
async def test_a_run_now_during_the_done_read_starts_the_chosen_card(tmp_path):
    state, slot, entry = _setup(tmp_path)
    started = await _finish(state, slot, mark_during_read=entry)
    assert started == ["r1"]


@pytest.mark.asyncio
async def test_without_a_run_now_a_restored_card_waits(tmp_path):
    state, slot, _entry = _setup(tmp_path)
    assert await _finish(state, slot) == []


@pytest.mark.asyncio
async def test_a_stop_in_flight_leaves_the_start_to_the_click(tmp_path):
    """The click's stop is still resolving: the cycle leaves the card to the click,
    whose own start runs once that stop settles (test_stop_handler_idempotent)."""
    state, slot, entry = _setup(tmp_path)

    def _stop_claimed() -> None:
        slot._stop_state = "soft_pending"

    assert await _finish(state, slot, mark_during_read=entry, during=_stop_claimed) == []
    assert slot._queue[0][RESTORED_SEND_CHOSEN_KEY] is True


@pytest.mark.asyncio
async def test_a_teardown_hold_keeps_the_chosen_card_queued(tmp_path):
    state, slot, _entry = _setup(tmp_path, chosen=True)
    slot._pending_discard_conversation_key = slot.key
    assert await _finish(state, slot) == []


@pytest.mark.asyncio
async def test_a_held_queue_does_not_start_the_chosen_card(tmp_path):
    state, slot, _entry = _setup(tmp_path, chosen=True)
    assert await _finish(state, slot, drain=False) == []


@pytest.mark.asyncio
async def test_a_card_that_cannot_start_yet_stays_chosen_for_the_next_turn_end(tmp_path):
    state, slot, entry = _setup(tmp_path)
    started = await _finish(state, slot, mark_during_read=entry, starts=False)
    assert started == ["r1"]
    assert slot._queue[0][RESTORED_SEND_CHOSEN_KEY] is True
    assert slot._turn_admission_reserved is False
