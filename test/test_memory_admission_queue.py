"""A queued turn refused at memory admission keeps its input.

While the gateway's shared memory preparation is still running, every dashboard
turn waits at ``_run_chat``'s admission seam and, past
``MEMORY_ADMISSION_WAIT_SECONDS``, is refused. The queue drain therefore leaves
the queue untouched until preparation finishes, then starts each parked slot's
next queued turn without the user sending anything.

Each test drives the real handler or drain and the real ``_run_chat`` with
``state.memory_startup_task`` set to a future that does not complete until the
test resolves it, and checks two things: the item is still queued after the
admission grace period, and it runs exactly once when preparation finishes
without anything else being sent.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_handlers import api_chat_slot_continue
from kiro_crew.dashboard.chat_runner import (
    MEMORY_PREPARATION_PARKED_TEXT,
    _run_chat,
    _start_next_queued_turn,
)
from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND
from kiro_crew.dashboard.state import CRON_NOTIFY_PREFIX
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

# Short enough to keep the suite fast, long enough that the "after the grace
# period" checks below really are after it.
_GRACE = 0.2


def _state_with_pending_memory(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.memory_startup.MEMORY_ADMISSION_WAIT_SECONDS", _GRACE)
    state = _make_state(tmp_path)
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False

    delivered: list[str] = []

    async def stream(message):
        delivered.append(message)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ran")
        yield LLMEvent(kind=EVENT_COMPLETE)

    client = MagicMock()
    client.stream = stream
    client.stream_command = stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.record_failure = AsyncMock()
    state.memory_startup_task = asyncio.get_running_loop().create_future()
    return state, delivered


def _app(state):
    app = _make_app(state)
    app.router.add_post("/api/chat/slots/{slot}/continue", api_chat_slot_continue)
    return app


async def _settle(state, slot) -> None:
    """Wait until the slot is idle and no background drain is left running."""
    for _ in range(200):
        await asyncio.sleep(0.01)
        pending = [t for t in state._background_tasks if not t.done()]
        if not slot.running and not pending:
            return
    raise AssertionError("slot never went idle")


def _finish_preparation(state) -> None:
    if not state.memory_startup_task.done():
        state.memory_startup_task.set_result(None)


def _ran(delivered: list[str], needle: str) -> int:
    return sum(needle in message for message in delivered)


def _seeded_slot(state, key: str):
    slot = state.get_or_create_slot(key)
    slot.append("user", "earlier question", "msg msg-u")
    slot.append("assistant", "earlier answer", "msg msg-a")
    return slot


@pytest.mark.asyncio
async def test_continue_pressed_during_preparation_runs_once_it_finishes(tmp_path, monkeypatch):
    state, delivered = _state_with_pending_memory(tmp_path, monkeypatch)
    slot = _seeded_slot(state, "continue-slot")
    try:
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post("/api/chat/slots/continue-slot/continue")
            assert resp.status == 200

            await asyncio.sleep(_GRACE * 2)
            # Not consumed: still a queued card, no refused turn, nothing ran.
            assert len(slot._queue) == 1
            assert not any(m["role"] == "error" for m in slot.messages)
            assert [m["content"] for m in slot.messages if m["role"] == "notice"] == [
                MEMORY_PREPARATION_PARKED_TEXT
            ]
            assert delivered == []
            # The retained continuation is what blocks a duplicate press.
            again = await client.post("/api/chat/slots/continue-slot/continue")
            assert again.status == 409
            assert (await again.json())["code"] == "slot_queue_pending"

            _finish_preparation(state)
            await _settle(state, slot)
    finally:
        _finish_preparation(state)

    assert slot._queue == []
    assert len(delivered) == 1
    assert any(m["role"] == "assistant" and m["content"] == "ran" for m in slot.messages)
    state.sessions.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_queued_behind_a_refused_turn_runs_once_preparation_finishes(
    tmp_path, monkeypatch
):
    state, delivered = _state_with_pending_memory(tmp_path, monkeypatch)
    slot = _seeded_slot(state, "queued-slot")
    first = asyncio.create_task(_run_chat(state, slot, "FIRST-MESSAGE"))
    slot.task = first
    try:
        async with TestClient(TestServer(_app(state))) as client:
            await asyncio.sleep(0)
            resp = await client.post(
                "/api/chat?ws=1", json={"message": "SECOND-MESSAGE", "slot": "queued-slot"}
            )
            assert resp.status == 200
            assert (await resp.json())["queued"] is True

        # The turn in flight is refused at the admission seam; what queued
        # behind it is retained and has not been popped.
        await first
        assert [item["content"] for item in slot._queue] == ["SECOND-MESSAGE"]
        assert not any(
            m["role"] == "user" and m["content"] == "SECOND-MESSAGE" for m in slot.messages
        )
        assert delivered == []

        # Nothing else is sent: preparation finishing alone starts it.
        _finish_preparation(state)
        await _settle(state, slot)
    finally:
        _finish_preparation(state)

    assert slot._queue == []
    assert _ran(delivered, "SECOND-MESSAGE") == 1
    assert _ran(delivered, "FIRST-MESSAGE") == 0


@pytest.mark.asyncio
async def test_cron_delivery_drained_during_preparation_is_not_consumed(tmp_path, monkeypatch):
    state, delivered = _state_with_pending_memory(tmp_path, monkeypatch)
    slot = _seeded_slot(state, "cron-slot")
    cron_text = f'{CRON_NOTIFY_PREFIX}"nightly"] CRON-PAYLOAD'
    slot.queue_append(cron_text, kind=CRON_NOTIFICATION_KIND)
    try:
        # The drain every producer calls after enqueueing.
        await _start_next_queued_turn(state, slot)
        await asyncio.sleep(_GRACE * 2)
        assert [item["content"] for item in slot._queue] == [cron_text]
        assert not any(m["role"] == "error" for m in slot.messages)
        assert delivered == []

        _finish_preparation(state)
        await _settle(state, slot)
    finally:
        _finish_preparation(state)

    assert slot._queue == []
    assert _ran(delivered, "CRON-PAYLOAD") == 1


@pytest.mark.asyncio
async def test_queue_not_parked_by_preparation_is_left_alone(tmp_path, monkeypatch):
    """Only queues this mechanism parked are drained; another idle slot's queue
    keeps waiting for its own user's next send."""
    state, delivered = _state_with_pending_memory(tmp_path, monkeypatch)
    parked = _seeded_slot(state, "parked-slot")
    other = _seeded_slot(state, "other-slot")
    parked.queue_append("PARKED-ITEM")
    other.queue_append("OTHER-ITEM")
    try:
        await _start_next_queued_turn(state, parked)
        _finish_preparation(state)
        await _settle(state, parked)
    finally:
        _finish_preparation(state)

    assert _ran(delivered, "PARKED-ITEM") == 1
    assert _ran(delivered, "OTHER-ITEM") == 0
    assert [item["content"] for item in other._queue] == ["OTHER-ITEM"]


class _SetEvent:
    def is_set(self) -> bool:
        return True


def _stopping() -> None:
    from kiro_crew.memory_startup import MemoryStartupUnavailable

    raise MemoryStartupUnavailable("Memory startup is stopping. Retry after restart.")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["shutdown", "startup_stopped"])
async def test_preparation_ending_without_ready_memory_keeps_the_queue(
    tmp_path, monkeypatch, ending
):
    """A preparation task that ends because the gateway is stopping leaves the
    parked queue in place for the history save, instead of popping an item
    that `_run_chat` would then refuse."""
    state, delivered = _state_with_pending_memory(tmp_path, monkeypatch)
    slot = _seeded_slot(state, "stopping-slot")
    slot.queue_append("KEEP-ITEM")
    try:
        await _start_next_queued_turn(state, slot)
        if ending == "shutdown":
            monkeypatch.setattr("kiro_crew.dashboard.chat_runner.shutdown_event", _SetEvent())
        else:
            monkeypatch.setattr("kiro_crew.memory_startup.require_memory_prepared", _stopping)
        _finish_preparation(state)
        await _settle(state, slot)
    finally:
        _finish_preparation(state)

    assert [item["content"] for item in slot._queue] == ["KEEP-ITEM"]
    assert not any(m["role"] == "user" and m["content"] == "KEEP-ITEM" for m in slot.messages)
    assert delivered == []
