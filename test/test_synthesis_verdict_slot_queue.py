"""The synthesis fire gate counts sub-agent results still waiting in the slot's queue.

A turn that ends with sign-in required hands its queue off held (``drain`` False)
while ``allow_automatic_successor`` stays True, so ``_finish_queue_cycle`` still
asks :func:`synthesis_fire_verdict` whether to fire. A completion parked in
``slot._queue`` has not been processed, so the synthesis prompt's claim that each
result was handled above would be false; the gate must hold until that entry runs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.chat_utils import (
    SYNTHESIS_CLEAR,
    SYNTHESIS_HELD,
    synthesis_fire_verdict,
)
from kiro_crew.dashboard.chat_utils_injected_rows import (
    SUBAGENT_COMPLETION_KIND,
    SYNTHETIC_RECOVERY_KIND,
)
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture(autouse=True)
def _no_cycle_background_tasks(_floor_monkeypatch):
    """Keep ``_finish_queue_cycle``'s title and summary tasks off executor threads."""
    _floor_monkeypatch.setattr(cr, "title_then_refresh", AsyncMock())
    _floor_monkeypatch.setattr(cr, "generate_session_summary", AsyncMock())


def _idle_subagents() -> MagicMock:
    """A registry with no running, pending or store-queued children."""
    return MagicMock(
        running_agents_for=MagicMock(return_value=[]),
        has_in_memory_pending_work_for=MagicMock(return_value=False),
        queued_count_or_none_async=AsyncMock(return_value=0),
    )


def _slot_with_queue(tmp_path, entries):
    state = _make_state(tmp_path)
    state.subagents = _idle_subagents()
    slot = state.get_or_create_slot("chat-synthesis-queue")
    slot._queue = list(entries)
    return state, slot


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [SUBAGENT_COMPLETION_KIND, SYNTHETIC_RECOVERY_KIND])
async def test_queued_delivery_holds_the_verdict(tmp_path, kind):
    state, slot = _slot_with_queue(tmp_path, [{"message": "result", "kind": kind}])
    assert await synthesis_fire_verdict(state, slot) == SYNTHESIS_HELD


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival", ["queued", "delivering"])
async def test_delivery_arriving_during_the_store_read_holds_the_verdict(tmp_path, arrival):
    """A child that finishes while the store read is awaited is seen after it."""
    state, slot = _slot_with_queue(tmp_path, [])

    async def _store_read(_key):
        if arrival == "queued":
            slot._queue.append({"message": "result", "kind": SUBAGENT_COMPLETION_KIND})
        else:
            slot._subagent_deliveries_inflight += 1
        return 0

    state.subagents.queued_count_or_none_async = _store_read
    assert await synthesis_fire_verdict(state, slot) == SYNTHESIS_HELD


@pytest.mark.asyncio
async def test_queued_user_message_does_not_hold_the_verdict(tmp_path):
    state, slot = _slot_with_queue(tmp_path, [{"message": "hello"}])
    assert await synthesis_fire_verdict(state, slot) == SYNTHESIS_CLEAR


@pytest.mark.asyncio
async def test_sign_in_held_queue_with_completion_does_not_fire_synthesis(tmp_path, monkeypatch):
    """The sign-in hold: queue kept (``drain`` False), successor still allowed."""
    fired = MagicMock()

    async def _synthesis(_state, _slot):
        fired()

    monkeypatch.setattr(cr, "_run_pending_synthesis", _synthesis)
    entry = {"message": "[Subagent completion event] ...", "kind": SUBAGENT_COMPLETION_KIND}
    state, slot = _slot_with_queue(tmp_path, [entry])
    slot._titled = True
    slot._pending_synthesis = True
    slot._last_turn_auth_required = True
    state._slots[slot.key] = slot
    task = MagicMock()
    task.done.return_value = False
    slot.task = task

    await cr._finish_queue_cycle(state, slot, allow_automatic_successor=True, drain=False)
    pending = list(state._background_tasks)
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), timeout=FALLBACK_TIMEOUT_SECS)

    fired.assert_not_called()
    assert slot._pending_synthesis is True, "synthesis stays armed for after the queue runs"
    assert slot._queue == [entry], "the held completion stays queued"
