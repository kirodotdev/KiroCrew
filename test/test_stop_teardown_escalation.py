"""An explicit second Stop discards work held behind preparation teardown."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.testing.wait import await_until, finished


@pytest.mark.asyncio
@pytest.mark.parametrize("released_task", [False, True])
async def test_second_stop_discards_queue_during_shielded_teardown(
    tmp_path, monkeypatch, released_task
):
    state = _make_state(tmp_path)
    state.push_slots_update = MagicMock()
    state._background_tasks = set()
    state.sessions.stop_turn = AsyncMock(return_value="idle")
    slot = state.get_or_create_slot("stop-teardown")
    slot.queue_append("must not dispatch")
    slot._pending_steers.append("must not requeue")
    started = asyncio.Event()
    tearing_down = asyncio.Event()
    recancelled = asyncio.Event()
    release_teardown = asyncio.Event()
    first_expiry = asyncio.Event()
    cleanup = asyncio.create_task(release_teardown.wait())
    settle_waits = []
    drained = AsyncMock()

    async def prepare():
        try:
            started.set()
            await await_until(release_teardown)
        finally:
            slot._preparing_task = None
            if released_task:
                slot.task = None
            tearing_down.set()
            try:
                while not cleanup.done():
                    try:
                        await await_until(asyncio.shield(cleanup))
                    except asyncio.CancelledError:
                        recancelled.set()
            finally:
                if slot.task is turn:
                    slot.task = None
                slot._stop_state = "idle"

    async def settle_wait(awaitable, timeout):
        if timeout != 2.0:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        settle_waits.append(awaitable)
        if len(settle_waits) == 1:
            await await_until(first_expiry)
        else:
            await await_until(recancelled)
        awaitable.cancel()
        raise asyncio.TimeoutError

    monkeypatch.setattr(chat_handlers, "_stop_settle_wait", settle_wait)
    monkeypatch.setattr(chat_handlers, "sel", MagicMock())
    monkeypatch.setattr(chat_handlers, "_drain_parked_queues", drained)
    turn = asyncio.create_task(prepare())
    slot.task = slot._preparing_task = turn
    first_stop = None
    try:
        await await_until(started)
        first_stop = asyncio.create_task(chat_handlers.stop_slot_turn(state, slot))
        await await_until(tearing_down)
        assert slot._stop_state == "soft_pending"
        assert slot._stop_teardown_task is turn
        assert slot.task is (None if released_task else turn)

        retry = await finished(
            chat_handlers.stop_slot_turn(state, slot, escalate=False), "non-escalating retry"
        )
        assert retry["already_stopping"] is True
        assert not recancelled.is_set()
        assert len(slot._queue) == 1
        assert slot._pending_steers == ["must not requeue"]
        assert state.sessions.stop_turn.await_count == 1

        result = await finished(chat_handlers.stop_slot_turn(state, slot), "second Stop")
        assert result == {"ok": True}
        assert state.sessions.stop_turn.await_count == 2
        assert state.sessions.stop_turn.await_args.kwargs["force"] is True
        assert recancelled.is_set()
        assert len(settle_waits) == 2
        assert not turn.done()
        assert not slot._queue
        assert not slot._pending_steers

        first_expiry.set()
        await await_until(first_stop)
        release_teardown.set()
        await await_until(asyncio.gather(turn, cleanup, return_exceptions=True))
        await await_until(asyncio.gather(*state._background_tasks))
        assert slot._stop_teardown_task is None
        assert not slot.running
        drained.assert_not_awaited()
    finally:
        first_expiry.set()
        release_teardown.set()
        tasks = [turn, cleanup]
        if first_stop is not None:
            tasks.append(first_stop)
        await await_until(asyncio.gather(*tasks, return_exceptions=True))


@pytest.mark.asyncio
async def test_idle_interrupt_defers_to_hard_stop_during_preparing_settlement(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    state.push_slots_update = MagicMock()
    state._background_tasks = set()
    slot = state.get_or_create_slot("interrupt-escalation")
    slot.queue_append("must not dispatch")
    preparing = asyncio.Event()
    tearing_down = asyncio.Event()
    release_settle = asyncio.Event()
    hard_started = asyncio.Event()
    release_hard = asyncio.Event()
    dispatched = AsyncMock()

    async def prepare():
        try:
            preparing.set()
            await await_until(release_settle)
        finally:
            tearing_down.set()
            await await_until(release_settle)
            slot.task = slot._preparing_task = None

    async def settle_wait(awaitable, timeout):
        await await_until(tearing_down)
        await await_until(release_settle)
        return await await_until(awaitable)

    async def stop_turn(key, *, force, **kwargs):
        if not force:
            return "idle"
        hard_started.set()
        await await_until(release_hard)
        await kwargs["on_hard"]()
        return "hard"

    state.sessions.stop_turn = AsyncMock(side_effect=stop_turn)
    monkeypatch.setattr(chat_handlers, "_stop_settle_wait", settle_wait)
    monkeypatch.setattr(chat_handlers, "sel", MagicMock())
    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", dispatched)
    turn = asyncio.create_task(prepare())
    slot.task = slot._preparing_task = turn
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/interrupt", chat_handlers.api_chat_slot_interrupt)
    interrupt = hard_stop = None
    try:
        await await_until(preparing)
        async with TestClient(TestServer(app)) as client:
            interrupt = asyncio.ensure_future(client.post(f"/api/chat/slots/{slot.key}/interrupt"))
            try:
                await await_until(tearing_down)
                stop_id = slot._stop_event_id
                assert stop_id is not None
                hard_stop = asyncio.create_task(chat_handlers.stop_slot_turn(state, slot))
                await await_until(hard_started)
                assert slot._stop_state == "killing"

                release_settle.set()
                response = await await_until(interrupt)
                assert response.status == 200
                assert (await response.json())["outcome"] == "idle"
                assert not hard_stop.done()
                assert slot._stop_state == "killing"
                assert slot._stop_event_id == stop_id
                card = json.loads(slot.messages[-1]["cls"])
                assert card["state"] == "interrupting"

                release_hard.set()
                await await_until(hard_stop)
                card = json.loads(slot.messages[-1]["cls"])
                assert card["id"] == stop_id
                assert card["state"] == "stop_failed_reset"
                assert card["outcome"] == "hard"
                assert slot._stop_state == "idle"
                assert slot._stop_event_id is None
                assert not slot._queue
                dispatched.assert_not_awaited()
            finally:
                release_settle.set()
                release_hard.set()
                await await_until(
                    asyncio.gather(
                        *(task for task in (interrupt, hard_stop) if task is not None),
                        return_exceptions=True,
                    )
                )
    finally:
        release_settle.set()
        release_hard.set()
        await await_until(asyncio.gather(turn, return_exceptions=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "interrupt"])
@pytest.mark.parametrize("outcome", ["idle", "soft"])
async def test_retained_teardown_does_not_swallow_successor_stop(
    tmp_path, monkeypatch, action, outcome
):
    state = _make_state(tmp_path)
    state.push_slots_update = MagicMock()
    state._background_tasks = set()
    state.sessions.stop_turn = AsyncMock(return_value=outcome)
    slot = state.get_or_create_slot("successor-stop")
    release = asyncio.Event()
    predecessor = asyncio.create_task(release.wait())
    successor = asyncio.create_task(release.wait())
    slot._stop_teardown_task = predecessor
    slot.task = slot._preparing_task = successor
    slot.queue_append("queued after successor")
    monkeypatch.setattr(chat_handlers, "sel", MagicMock())
    monkeypatch.setattr(chat_handlers, "_drain_parked_queues", AsyncMock())
    app = web.Application()
    app["state"] = state
    handler = (
        chat_handlers.api_chat_slot_stop
        if action == "stop"
        else chat_handlers.api_chat_slot_interrupt
    )
    app.router.add_post("/api/chat/slots/{slot}/" + action, handler)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                f"/api/chat/slots/{slot.key}/{action}",
                json={"queue_id": slot._queue[0]["id"]},
            )
            assert response.status == 200
            state.sessions.stop_turn.assert_awaited_once()
            assert state.sessions.stop_turn.await_args.args == (effective_session_key(slot),)
            assert state.sessions.stop_turn.await_args.kwargs["force"] is False
            assert not predecessor.done()
            assert predecessor.cancelling() == 0
            if outcome == "idle":
                assert successor.cancelled()
            else:
                assert not successor.done()
    finally:
        release.set()
        await await_until(asyncio.gather(predecessor, successor, return_exceptions=True))
        await await_until(asyncio.gather(*state._background_tasks))
