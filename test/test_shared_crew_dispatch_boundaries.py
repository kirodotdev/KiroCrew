"""Admission suspension, restored-card selection and saved-run identity boundaries."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state
from test_shared_crew_queued_admission import (
    BOB,
    _client,
)
from test_shared_crew_queued_admission import clock as _clock
from test_shared_crew_queued_admission import dash_state as _dash_state
from test_shared_crew_queued_admission import installed as _installed
from test_shared_crew_turn_outcome import _service

from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.handlers import workflows
from kiro_crew.dashboard.slot_queue_repository import (
    drain_restored_parked_sends,
    durable_queue_entries,
    sanitize_restored_queue,
)
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import turn_gate
from kiro_crew.shared_crew.admission import OperationKind, OperationState
from kiro_crew.testing.wait import await_until

clock = _clock
dash_state = _dash_state
installed = _installed


@pytest.mark.asyncio
@pytest.mark.parametrize("still_busy", [False, True])
@pytest.mark.parametrize("hold", ["turn", "subagents"])
async def test_queued_send_rechecks_busy_after_admission(
    dash_state, installed, monkeypatch, still_busy, hold
):
    slot = dash_state.get_or_create_slot("s1")
    busy = MagicMock()
    busy.done.return_value = False
    slot.task = busy if hold == "turn" else None
    children = [{}] if hold == "subagents" else []
    dash_state.subagents = MagicMock()
    dash_state.subagents.running_agents_for.return_value = children
    accepted = asyncio.Event()
    release = asyncio.Event()
    admit = entry.admit_queued_send

    async def paused_admit(*args, **kwargs):
        result = await admit(*args, **kwargs)
        accepted.set()
        await await_until(release)
        return result

    calls = []

    async def run(state, target, message, **kwargs):
        async def body():
            calls.append((message, turn_gate.admitted_work()))

        await turn_gate.gate_turn(state, target, message, kwargs, body)

    monkeypatch.setattr(entry, "admit_queued_send", paused_admit)
    monkeypatch.setattr(chat_handlers, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat_runner, "_run_chat", run)
    monkeypatch.setattr(chat_runner, "_parked_queue_drain_admitted", lambda: True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_slack.slot_is_live", lambda *_: True)
    async with _client(dash_state, BOB) as client:
        sending = asyncio.create_task(
            client.post(
                "/api/chat?ws=1",
                json={"slot": "s1", "message": "follow up"},
                headers={"Idempotency-Key": "busy-boundary-send"},
            )
        )
        try:
            await await_until(accepted)
            if not still_busy:
                slot.task = None
                children.clear()
            release.set()
            response = await await_until(sending)
            assert response.status == 200
            receipt = await response.json()
            assert receipt["queued"] is True
            [work] = await installed.list_work(session_key="dashboard:s1")
            if still_busy:
                assert slot.task is (busy if hold == "turn" else None)
                assert calls == []
                assert len(slot._queue) == 1 and work.state is OperationState.QUEUED
            else:
                assert slot.task is not None
                await await_until(slot.task)
                assert slot._queue == []
                assert calls == [("follow up", work.work_id)]
                assert (
                    await installed.current_work(work.work_id)
                ).state is OperationState.COMPLETED
                assert installed.capacity(BOB.id).crew.queued == 0
        finally:
            release.set()
            await await_until(asyncio.gather(sending, return_exceptions=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_pending", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_recovery_leaves_unaccepted_cards_in_the_same_slot(
    tmp_path, monkeypatch, memory_pending, cancelled
):
    before = _service(tmp_path)
    saved = [{"id": "plain-first", "content": "ordinary first"}]
    for index in range(2):
        admission = await before.admit(
            budget_key=BOB.id,
            session_key="dashboard:mixed",
            operation=OperationKind.CHAT_TURN,
            idempotency_key=f"mixed-parked-{index}",
            park=True,
        )
        if cancelled:
            await before.cancel(admission.work.work_id, expected_revision=admission.work.revision)
        saved.append(
            {
                "id": f"parked-{index}",
                "content": f"accepted {index}",
                "meta": {entry.QUEUED_WORK_META_KEY: admission.work.work_id},
            }
        )
        saved.append({"id": f"plain-{index}", "content": f"ordinary {index}"})
    saved = durable_queue_entries(saved)
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
    slot = state.get_or_create_slot("mixed")
    slot._queue[:] = sanitize_restored_queue(saved)
    from kiro_crew.shared_crew.host_hooks import mark_sessions_restored

    mark_sessions_restored(state)
    plain = [dict(item) for item in slot._queue if item["id"].startswith("plain")]
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *_: False)
    monkeypatch.setattr(chat_runner, "_parked_queue_drain_admitted", lambda: True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_slack.slot_is_live", lambda *_: True)
    calls = []
    tasks = []
    follow_queue = True
    finished = asyncio.Event()
    drained = asyncio.Event()
    drain = chat_runner._drain_parked_queues

    async def observe_drain(_state, keys):
        await drain(_state, keys)
        if slot.key in keys:
            drained.set()

    monkeypatch.setattr(chat_runner, "_drain_parked_queues", observe_drain)
    if memory_pending:
        other = state.get_or_create_slot("memory-waiter")
        chat_runner._arm_parked_queue_drain(state, other, notice=False)

    def spawn(_state, _slot, coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    async def run(_state, _slot, message, **kwargs):
        async def body():
            calls.append(message)

        await turn_gate.gate_turn(_state, _slot, message, kwargs, body)
        if not follow_queue or not await chat_runner._start_next_queued_turn(_state, _slot):
            finished.set()

    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", spawn)
    monkeypatch.setattr(chat_runner, "_run_chat", run)
    try:
        await asyncio.wait_for(drain_restored_parked_sends(state), timeout=30)
        if memory_pending:
            assert calls == [] and len(slot._queue) == len(saved)
            drained.clear()
            state.memory_startup_task.set_result(None)
            await await_until(drained)
            await await_until(asyncio.gather(*state._background_tasks))
        if not cancelled:
            await await_until(finished)
        await await_until(asyncio.gather(*tasks))
        assert calls == ([] if cancelled else ["accepted 0", "accepted 1"])
        assert slot._queue == plain
        assert service.capacity(BOB.id).crew.active == 0
        assert service.capacity(BOB.id).crew.queued == 0
        # An explicit dispatch outside the recovery task still owns its chosen card.
        follow_queue = False
        assert await chat_runner._start_next_queued_turn(
            state, slot, required_queue_id="plain-first"
        )
        await await_until(slot.task)
        assert "ordinary first" in calls
    finally:
        if memory_pending and not state.memory_startup_task.done():
            state.memory_startup_task.cancel()
        await await_until(asyncio.gather(*tasks, return_exceptions=True))
        for task in list(state._background_tasks):
            await await_until(task)
        service._store.tasks.close()


@pytest.mark.asyncio
async def test_saved_definition_key_is_bound_to_definition_and_operation(installed, monkeypatch):
    start = AsyncMock(return_value={"run_id": "saved-run"})
    raw_start = AsyncMock(return_value={"run_id": "raw-run"})
    svc = SimpleNamespace(start_definition=start, start=raw_start)
    monkeypatch.setattr(workflows, "_svc", lambda _request: svc)
    monkeypatch.setattr(workflows, "_private_memory_refusal", AsyncMock(return_value=None))
    monkeypatch.setattr(
        workflows, "read_bounded_json", AsyncMock(return_value=({"source": "pass"}, None))
    )

    async def request(ref):
        req = make_mocked_request(
            "POST",
            f"/api/workflows/definitions/{ref}/run",
            match_info={"workflow_ref": ref},
            headers={"X-Session-Key": "same-session", "Idempotency-Key": "same-key"},
        )
        return await workflows.api_workflow_definition_run(req)

    first = await request("definition-a")
    replay = await request("definition-a")
    conflict = await request("definition-b")
    assert first.status == replay.status == 200
    assert json.loads(replay.body)["replayed"] is True
    assert json.loads(replay.body)["run_id"] == "saved-run"
    assert conflict.status == 409
    assert json.loads(conflict.body)["code"] == "idempotency_conflict"
    req = make_mocked_request(
        "POST",
        "/api/workflows/run",
        headers={"X-Session-Key": "same-session", "Idempotency-Key": "same-key"},
    )
    other_operation = await workflows.api_workflow_run(req)
    assert other_operation.status == 409
    assert json.loads(other_operation.body)["code"] == "idempotency_conflict"
    start.assert_awaited_once()
    assert start.call_args.args == ("definition-a",)
    raw_start.assert_not_awaited()
