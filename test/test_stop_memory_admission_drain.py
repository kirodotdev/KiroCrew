"""Terminal Stop retries ordinary queued work after releasing its posture."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import memory_startup
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.testing.wait import await_until, finished


@pytest.mark.asyncio
async def test_stop_drains_ordinary_queue_when_memory_finishes_during_teardown(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    state.push_slots_update = MagicMock()
    state.broadcast_ws = MagicMock()
    state._background_tasks = set()
    state.sessions.stop_turn = AsyncMock(return_value="idle")
    state.sessions.release = MagicMock()
    slot = state.get_or_create_slot("stop-memory")
    slot.append("user", "waiting for memory", "msg msg-u")
    slot.queue_append("ordinary queued message")
    preparation = asyncio.get_running_loop().create_future()
    state.memory_startup_task = preparation
    entered = asyncio.Event()
    dispatched = AsyncMock()
    run_chat = chat_runner._run_chat
    wait_for_memory = memory_startup.wait_for_memory_preparation

    async def wait_for_preparation(task):
        entered.set()
        await wait_for_memory(task)

    async def settle_wait(awaitable, timeout):
        try:
            await await_until(awaitable)
        except asyncio.CancelledError:
            pass
        assert slot._stop_state == "soft_pending"
        assert not preparation.done()
        preparation.set_result(None)
        await chat_handlers._drain_parked_queues(state, [slot.key])
        await await_until(asyncio.gather(*state._background_tasks))
        assert [entry["content"] for entry in slot._queue] == ["ordinary queued message"]
        dispatched.assert_not_awaited()

    monkeypatch.setattr(memory_startup, "_active", None)
    monkeypatch.setattr(memory_startup, "wait_for_memory_preparation", wait_for_preparation)
    monkeypatch.setattr(chat_handlers, "_stop_settle_wait", settle_wait)
    monkeypatch.setattr(chat_handlers, "sel", MagicMock())
    monkeypatch.setattr(chat_runner, "_run_chat", dispatched)
    turn = asyncio.create_task(run_chat(state, slot, "waiting for memory"))
    slot.task = turn
    try:
        await await_until(entered)
        await finished(chat_handlers.stop_slot_turn(state, slot), "terminal Stop")
        await await_until(asyncio.gather(*state._background_tasks))
        assert slot._stop_state == "idle"
        assert not slot._queue
        dispatched.assert_awaited_once()
        assert dispatched.await_args.args[2] == "ordinary queued message"
    finally:
        if not preparation.done():
            preparation.set_result(None)
        turn.cancel()
        await await_until(asyncio.gather(turn, return_exceptions=True))
