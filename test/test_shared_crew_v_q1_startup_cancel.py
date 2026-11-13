"""Admission reservations and restored queues survive startup cancellation boundaries."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from test_shared_crew_turn_outcome import _service

from kiro_crew import memory_startup
from kiro_crew.dashboard import chat_runner, server
from kiro_crew.shared_crew import admission_entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import host_hooks
from kiro_crew.shared_crew.admission import OperationKind, OperationState, RuntimeOwnershipRef
from kiro_crew.testing.wait import async_wait_until

BOUND = 30


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["slice", "accept"])
async def test_cancelled_acceptance_releases_reservation(tmp_path, monkeypatch, cancel_at):
    svc = _service(tmp_path, hard_limits=True)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def pause():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(BOUND), "acceptance worker was not released"

    if cancel_at == "slice":

        def runner(cmd):
            pause()
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(svc._slices, "_runner", runner)
    else:
        accept = svc._store.accept_sync

        def blocked_accept(*args, **kwargs):
            pause()
            return accept(*args, **kwargs)

        monkeypatch.setattr(svc._store, "accept_sync", blocked_accept)

    task = asyncio.create_task(
        svc.admit(
            budget_key="p_unknown",
            session_key="dashboard:cancel",
            operation=OperationKind.CHAT_TURN,
            idempotency_key="cancel-reservation",
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), BOUND)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
        await svc.tick()
        assert svc.capacity("p_unknown").crew.active == 0
        assert svc.capacity("p_unknown").crew.queued == 0
        assert not svc._live
        rows = await svc.list_work()
        assert all(w.state is OperationState.CANCELLED for w in rows)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(svc._store.tasks.close)


@pytest.mark.asyncio
async def test_real_restore_before_memory_task_publication_drains_once(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(server.cautious_boot, "pause_before", AsyncMock())
    before = _service(tmp_path)
    running = await before.admit(
        budget_key="p_unknown",
        session_key="dashboard:crash-run",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="running-on-crash",
    )
    await before._store.advance(
        running.work,
        OperationState.RUNNING,
        runtime=RuntimeOwnershipRef(execution_id=running.work.work_id),
    )
    parked = await before.admit(
        budget_key="p_unknown",
        session_key="dashboard:crash-run",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="parked-on-crash",
        park=True,
    )
    before._store.tasks.close()
    state = _make_state(tmp_path / "sessions")
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state._dynamic_cards = None
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    state.memory_startup_task = None
    log = state.conversation_log
    await asyncio.to_thread(
        log.update_metadata,
        "dashboard:crash-run",
        {
            "title": "crash-run",
            "queued_prompts": [
                {
                    "id": "parked-card",
                    "content": "parked prompt",
                    "meta": {
                        admission_entry.QUEUED_WORK_META_KEY: parked.work.work_id,
                    },
                }
            ],
        },
    )
    await asyncio.to_thread(
        (tmp_path / "open_slots.json").write_text,
        json.dumps({"keys": ["crash-run"], "ts": 0}),
        encoding="utf-8",
    )
    svc = _service(tmp_path)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr(ag, "current_service", AsyncMock(return_value=svc))
    svc.set_reconcile_listener(host_hooks.reconcile_note(state))
    startup = memory_startup.MemoryStartup()
    monkeypatch.setattr(memory_startup, "_active", startup)
    delivered = []

    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    async def stream(message):
        delivered.append(message)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="reply")
        yield LLMEvent(kind=EVENT_COMPLETE)

    client = MagicMock()
    client.stream = stream
    client.stream_command = stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *_: False)
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(
            restore_sessions=False,
            restore_window_minutes=60,
            idle_slot_sweep_days=0,
            surface_channel_sessions=False,
        )
    )
    try:
        await asyncio.wait_for(server._restore_dashboard_sessions(state, cfg), BOUND)
        slot = state._slots["crash-run"]
        assert len(slot._queue) == 1 and delivered == []
        state.memory_startup_task = asyncio.get_running_loop().create_future()
        startup.complete()
        state.memory_startup_task.set_result(None)
        state.ready = True
        await async_wait_until(lambda: bool(delivered), timeout=BOUND)
        await asyncio.wait_for(slot.task, BOUND)
        assert slot._queue == []
        assert sum("parked prompt" in message for message in delivered) == 1
        assert (
            sum(
                row["role"] == "user" and "parked prompt" in row["content"] for row in slot.messages
            )
            == 1
        )
        await asyncio.wait_for(server._restore_dashboard_sessions(state, cfg), BOUND)
        assert len(delivered) == 1
        works = await svc.list_work(session_key="dashboard:crash-run")
        assert {work.state for work in works} == {
            OperationState.UNKNOWN_OUTCOME,
            OperationState.CANCELLED,
            OperationState.COMPLETED,
        }
        assert len(works) == 3
        assert svc.capacity("p_unknown").crew.active == 0
        assert svc.capacity("p_unknown").crew.queued == 0
    finally:
        for task in list(state._background_tasks):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(svc._store.tasks.close)
