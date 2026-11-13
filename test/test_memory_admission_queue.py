"""A queued turn refused at memory admission keeps its input.

Past ``MEMORY_ADMISSION_WAIT_SECONDS`` a turn is refused at ``_run_chat``'s admission seam,
so the drain waits for preparation, then starts each parked slot's next turn by itself.

``state.memory_startup_task`` is a future the test resolves; each test checks the item is
still queued after the grace period and runs exactly once afterwards.
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
from kiro_crew.testing.wait import async_wait_until, await_until

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


def _hold_first_admission(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Park the first turn to reach the memory admission seam until the test releases it.

    The real ``wait_for_memory_preparation`` still decides; holding it only starts the
    grace period when the test chooses, so a slow host cannot run it out. Returns
    ``(at_seam, release)``.
    """
    import kiro_crew.memory_startup as memory_startup

    real = memory_startup.wait_for_memory_preparation
    at_seam = asyncio.Event()
    release = asyncio.Event()

    async def held(task, **kw):
        if not at_seam.is_set():
            at_seam.set()
            await release.wait()
        return await real(task, **kw)

    monkeypatch.setattr(memory_startup, "wait_for_memory_preparation", held)
    return at_seam, release


async def _settle(state, slot) -> None:
    """Wait until the slot is idle and no background drain is left running."""
    # The drain is armed by a done-callback on the preparation task, and callbacks
    # fire in registration order on a later loop step: once this one has fired, a
    # drain that was admitted is already among the background tasks.
    callbacks_ran = asyncio.get_running_loop().create_future()
    state.memory_startup_task.add_done_callback(lambda _task: callbacks_ran.set_result(None))
    await await_until(callbacks_ran, what="the preparation task's done-callbacks")

    def _pending() -> list:
        return [t for t in state._background_tasks if not t.done()]

    await async_wait_until(
        lambda: not slot.running and not _pending(),
        describe=lambda: f"running={slot.running} pending={_pending()}",
    )


async def _yield_loop(times: int = 50) -> None:
    """Give every ready task a turn; deterministic, unlike sleeping a duration."""
    for _ in range(times):
        await asyncio.sleep(0)


async def _settle_task(task: asyncio.Future, what: str) -> None:
    """Cancel *task* if it is still running and wait, bounded, for it to end."""
    task.cancel()
    await await_until(asyncio.gather(task, return_exceptions=True), what=what)


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
async def test_turn_waits_past_the_grace_and_queued_message_runs_after_it(tmp_path, monkeypatch):
    state, delivered = _state_with_pending_memory(tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.memory_startup.MEMORY_ADMISSION_WAIT_SECONDS", 0.0)
    slot = _seeded_slot(state, "queued-slot")
    first = asyncio.create_task(_run_chat(state, slot, "FIRST-MESSAGE"))
    slot.task = first
    try:
        async with TestClient(TestServer(_app(state))) as client:
            await async_wait_until(
                lambda: slot.turn_running, describe=lambda: "first turn admitted"
            )
            resp = await client.post(
                "/api/chat?ws=1", json={"message": "SECOND-MESSAGE", "slot": "queued-slot"}
            )
            assert (await resp.json())["queued"] is True

        # The grace period is zero: the first turn is still waiting, not refused.
        await _yield_loop()
        assert not first.done()
        assert not any(m["role"] == "error" for m in slot.messages)
        assert delivered == []

        # Preparation finishing alone runs both, in order, with no resend.
        _finish_preparation(state)
        await await_until(first, what="the first turn after preparation")
        await _settle(state, slot)
    finally:
        _finish_preparation(state)
        await _settle_task(first, "the first turn")

    assert slot._queue == []
    assert [("F" if "FIRST-MESSAGE" in m else "S") for m in delivered] == [
        "F",
        "S",
    ]


@pytest.mark.asyncio
async def test_turn_is_refused_when_preparation_failed(tmp_path, monkeypatch):
    from kiro_crew import memory_startup

    state, delivered = _state_with_pending_memory(tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.memory_startup.MEMORY_ADMISSION_WAIT_SECONDS", 0.0)
    slot = _seeded_slot(state, "failed-slot")
    startup = memory_startup.MemoryStartup.begin()
    turn = asyncio.create_task(_run_chat(state, slot, "FAILED-MESSAGE"))
    slot.task = turn
    try:
        await _yield_loop()
        assert not turn.done()
        # The supervisor records the failure; the wedged worker never returns.
        startup.fail(RuntimeError("boom"))
        monkeypatch.setattr("kiro_crew.memory_startup._FENCE_RECHECK_SECONDS", 0.01)
        await await_until(turn, what="the turn refused on the recorded failure")
        assert not state.memory_startup_task.done()
    finally:
        await _settle_task(turn, "the failed-preparation turn")
        startup.stop()
        startup.release()
        _finish_preparation(state)

    assert delivered == []
    assert any(m["role"] == "error" and "boom" in m["content"] for m in slot.messages)


@pytest.mark.asyncio
async def test_wedged_preparation_ends_the_wait_with_a_refusal(tmp_path, monkeypatch):
    """No supervisor failure and a task that never finishes: the turn's own finite
    limit refuses it instead of waiting forever. The limit is driven to zero; the
    test asserts the outcome, not a duration."""
    state, delivered = _state_with_pending_memory(tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.memory_startup.MEMORY_PREPARATION_DEADLINE_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.memory_startup._UNBOUNDED_WAIT_MARGIN_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.memory_startup._FENCE_RECHECK_SECONDS", 0.01)
    slot = _seeded_slot(state, "wedged-slot")
    turn = asyncio.create_task(_run_chat(state, slot, "WEDGED-MESSAGE"))
    slot.task = turn
    try:
        await await_until(turn, what="the turn refused at its own limit")
        assert not state.memory_startup_task.done()
    finally:
        await _settle_task(turn, "the wedged-preparation turn")
        _finish_preparation(state)

    assert delivered == []
    errors = [m for m in slot.messages if m["role"] == "error"]
    assert [m.get("meta", {}).get("code") for m in errors] == ["memory_unavailable"]
    assert "Memory preparation is still running" in errors[0]["content"]


@pytest.mark.asyncio
async def test_wedged_publication_ends_the_wait_with_a_refusal(monkeypatch):
    """The task is never published while startup is preparing: the same finite limit."""
    from kiro_crew import memory_startup

    monkeypatch.setattr(memory_startup, "MEMORY_PREPARATION_DEADLINE_SECONDS", 0.0)
    monkeypatch.setattr(memory_startup, "_UNBOUNDED_WAIT_MARGIN_SECONDS", 0.0)
    startup = memory_startup.MemoryStartup.begin()
    wait = asyncio.ensure_future(
        memory_startup.wait_for_memory_preparation(lambda: None, bounded=False)
    )
    try:
        with pytest.raises(memory_startup.MemoryStartupUnavailable, match="still starting"):
            await await_until(wait, what="the unpublished-task wait at its own limit")
    finally:
        await _settle_task(wait, "the unpublished-task wait")
        startup.stop()
        startup.release()


@pytest.mark.asyncio
async def test_stop_during_the_wait_leaves_preparation_running(tmp_path, monkeypatch):
    state, delivered = _state_with_pending_memory(tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.memory_startup.MEMORY_ADMISSION_WAIT_SECONDS", 0.0)
    slot = _seeded_slot(state, "stop-slot")
    turn = asyncio.create_task(_run_chat(state, slot, "STOPPED-MESSAGE"))
    slot.task = turn
    try:
        await _yield_loop()
        await _settle_task(turn, "the stopped turn")
        assert turn.cancelled() or turn.done()
        assert not state.memory_startup_task.done()
    finally:
        await _settle_task(turn, "the stopped turn")
        _finish_preparation(state)

    assert delivered == []


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
