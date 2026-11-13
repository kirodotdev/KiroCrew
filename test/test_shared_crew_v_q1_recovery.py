"""Parked sends resume through admission; a stopped turn records cancellation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from test_shared_crew_turn_outcome import ALICE, _service, _Slot

from kiro_crew.dashboard import chat_runner, server
from kiro_crew.dashboard.slot_queue_repository import durable_queue_entries, sanitize_restored_queue
from kiro_crew.shared_crew import admission_entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import turn_gate
from kiro_crew.shared_crew.admission import OperationKind, OperationState, ReasonCode
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.testing.wait import async_wait_until

BOUND = 30
S = OperationState


@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    monkeypatch.setattr(ag, "current_service", AsyncMock(return_value=svc))
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr("kiro_crew.dashboard.chat_slack.slot_is_live", lambda *_: True)
    yield svc
    svc._store.tasks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_admitted", [False, True])
async def test_cancelled_terminal_event_settles_cancelled(service, handler_admitted):
    slot = _Slot("stop-me")
    state = SimpleNamespace(push_slots_update=lambda: None)

    async def run():
        slot.append("assistant", "partial reply")

    async def turn():
        await turn_gate.gate_turn(state, slot, "question", {}, run, stop_reason=lambda: "cancelled")

    if handler_admitted:
        admission = await service.admit(
            budget_key=ALICE.id,
            session_key="dashboard:stop-me",
            operation=OperationKind.CHAT_TURN,
            idempotency_key="cancelled-turn-1",
        )
        await admission.run(turn())
    else:
        await turn()
    [work] = await service.list_work(session_key="dashboard:stop-me")
    assert work.state is S.CANCELLED
    assert work.reason is ReasonCode.CANCELLED_BY_USER
    assert service.capacity(work.budget_key).crew.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_pending,cancelled", [(False, False), (True, False), (False, True)])
async def test_startup_drains_only_restored_parked_sends(
    tmp_path, monkeypatch, memory_pending, cancelled
):
    before = _service(tmp_path)
    parked = await before.admit(
        budget_key=ALICE.id,
        session_key="dashboard:parked",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="parked-restart-1",
        park=True,
    )
    if cancelled:
        await before.cancel(parked.work.work_id, expected_revision=parked.work.revision)
    saved = durable_queue_entries(
        [
            {
                "id": "queued-one",
                "content": "parked prompt",
                "meta": {
                    admission_entry.QUEUED_WORK_META_KEY: parked.work.work_id,
                },
            }
        ]
    )
    before._store.tasks.close()
    service = _service(tmp_path)
    monkeypatch.setattr(ag, "_SERVICE", service)
    monkeypatch.setattr(ag, "current_service", AsyncMock(return_value=service))
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state._dynamic_cards = None
    state.memory_startup_task = (
        asyncio.get_running_loop().create_future() if memory_pending else None
    )
    slot = state.get_or_create_slot("parked")
    ordinary = state.get_or_create_slot("ordinary")

    async def restore(_state):
        slot._queue[:] = sanitize_restored_queue(saved)
        ordinary._queue[:] = sanitize_restored_queue([{"id": "plain", "content": "wait for me"}])

    monkeypatch.setattr(
        server,
        "chat",
        SimpleNamespace(
            restore_open_slots_async=restore,
            restore_recent_sessions_async=AsyncMock(return_value=0),
        ),
    )
    monkeypatch.setattr(server.cautious_boot, "pause_before", AsyncMock())
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *_: False)
    monkeypatch.setattr(chat_runner, "_parked_queue_drain_admitted", lambda: True)
    monkeypatch.setattr(
        chat_runner, "spawn_guarded_turn", lambda _s, _slot, coro: asyncio.create_task(coro)
    )
    calls = []

    async def run(_state, _slot, message, **kwargs):
        async def body():
            calls.append(message)

        await turn_gate.gate_turn(
            _state, _slot, message, kwargs, body, stop_reason=lambda: "end_turn"
        )

    monkeypatch.setattr(chat_runner, "_run_chat", run)
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(
            restore_sessions=True,
            restore_window_minutes=60,
            idle_slot_sweep_days=0,
            surface_channel_sessions=False,
        )
    )
    try:
        await asyncio.wait_for(server._restore_dashboard_sessions(state, cfg), BOUND)
        if memory_pending:
            assert calls == [] and len(slot._queue) == 1
            state.memory_startup_task.set_result(None)
        if not cancelled:
            await async_wait_until(lambda: slot.task is not None, timeout=BOUND)
            await asyncio.wait_for(slot.task, BOUND)
        assert slot._queue == []
        assert calls == ([] if cancelled else ["parked prompt"])
        assert [e["content"] for e in ordinary._queue] == ["wait for me"]
        works = await service.list_work(session_key="dashboard:parked")
        completed = [w for w in works if w.state is S.COMPLETED]
        assert len(completed) == (0 if cancelled else 1)
        if completed:
            assert completed[0].budget_key == Principal.unknown().id
        assert service.capacity(ALICE.id).crew.active == 0
        assert service.capacity(ALICE.id).crew.queued == 0
    finally:
        if memory_pending and not state.memory_startup_task.done():
            state.memory_startup_task.cancel()
        for task in list(state._background_tasks):
            await asyncio.wait_for(task, BOUND)
        service._store.tasks.close()
