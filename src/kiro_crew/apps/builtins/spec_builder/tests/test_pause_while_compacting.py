"""Spec Builder's Pause halts its worker even when a compaction declines the Stop.

Core's ``test_compaction_stop_declined_surfaces.py`` pins the host's Stop surfaces:
a Stop a compaction declines leaves the queue, the pending approvals and the staged
files in place. The Pause is the app's own surface. Its worker task is this app's run
loop, not the compaction turn, so the Pause cancels it and drops its queued work
whatever the cooperative stop answers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.apps.builtins.spec_builder.backend import runtime


class _SpecSlot:
    """The spec-builder worker slot as ``_halt_active_turn`` reads it."""

    key = "spec-builder-live"
    _app = "spec-builder"
    running = True

    def __init__(self):
        self.task = MagicMock()
        self.task.done.return_value = False
        self._queue = [{"id": "q1", "content": "next prompt"}]
        self._pending_steers = ["steer-1"]
        self._pending_synthesis = True


def _spec_state(slot, *, compacting: bool, stop_outcome: str):
    state = MagicMock()
    state.get_slot = lambda key: slot if key == slot.key else None
    state.sessions.is_compacting = MagicMock(return_value=compacting)
    state.sessions.stop_turn = AsyncMock(return_value=stop_outcome)
    return state


@pytest.mark.asyncio
async def test_a_pause_while_compacting_skips_the_stop_but_still_halts_the_worker():
    """The cooperative stop would be declined, so it is not sent; the WORKER task
    (this app's own run loop, not the compaction turn) is still cancelled, so a
    Pause the route reports as halted has halted, and its queued work is dropped
    as on any Pause."""
    slot = _SpecSlot()
    state = _spec_state(slot, compacting=True, stop_outcome="soft")
    assert await runtime._halt_active_turn(state, "live") is True
    state.sessions.stop_turn.assert_not_awaited()
    slot.task.cancel.assert_called_once()
    assert slot._queue == [] and slot._pending_steers == []
    assert slot._pending_synthesis is False


@pytest.mark.asyncio
async def test_a_pause_declined_in_the_race_still_halts_the_worker():
    """The race: the probe passed, the compaction committed, ``stop_turn`` declined.
    The worker task is ours to cancel regardless, so the Pause still halts it."""
    slot = _SpecSlot()
    state = _spec_state(slot, compacting=False, stop_outcome="compacting")
    assert await runtime._halt_active_turn(state, "live") is True
    state.sessions.stop_turn.assert_awaited_once()
    slot.task.cancel.assert_called_once()
    assert slot._queue == [] and slot._pending_steers == []


@pytest.mark.asyncio
async def test_a_pause_that_goes_through_still_discards_and_cancels():
    slot = _SpecSlot()
    state = _spec_state(slot, compacting=False, stop_outcome="soft")
    assert await runtime._halt_active_turn(state, "live") is True
    slot.task.cancel.assert_called_once()
    assert slot._queue == [] and slot._pending_steers == []
    assert slot._pending_synthesis is False
