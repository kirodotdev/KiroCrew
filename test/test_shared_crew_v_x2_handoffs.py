"""Admission ownership across pre-dispatch exits and background start retries."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks
from test_handlers_taskrunner_coverage import _request, _runner, _state
from test_shared_crew_admission import _open, _service

from kiro_crew.dashboard import chat_handlers as chat
from kiro_crew.dashboard.handlers import taskrunner, workflows
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew.admission import OperationState
from kiro_crew.testing.clock import ManualClock

BOUND = 30


@pytest.fixture
def service(tmp_path, monkeypatch):
    clock = ManualClock(start=10000)
    store = _open(tmp_path / "admission.db", clock)
    svc = _service(store, clock, principal_concurrency=1)
    monkeypatch.setattr(ag, "current_service", AsyncMock(return_value=svc))
    monkeypatch.setattr(ag, "_SERVICE", svc)
    return svc


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["stop", "close", "cancel", "error", "assignment"])
async def test_chat_predispatch_exit_releases_admission(tmp_path, monkeypatch, service, exit_kind):
    state = _make_state(tmp_path)
    state.push_slots_update = MagicMock()
    slot = state.get_or_create_slot("s1") if exit_kind != "assignment" else None
    run = AsyncMock()
    monkeypatch.setattr(chat, "_run_chat", run)
    monkeypatch.setattr(chat, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat, "maybe_auto_tag", AsyncMock())
    accepted = []
    real_admit = entry.admit_chat_turn

    async def admit(*args, **kwargs):
        result = await real_admit(*args, **kwargs)
        accepted.append(result)
        return result

    async def hold(st, sl):
        assert accepted and service._scheduler.is_active(accepted[0].work.work_id)
        if exit_kind == "stop":
            sl._stop_generation += 1
        elif exit_kind == "close":
            st._slots.pop(sl.key)
        elif exit_kind == "cancel":
            raise asyncio.CancelledError()
        elif exit_kind == "error":
            raise RuntimeError("link failed")

    monkeypatch.setattr(entry, "admit_chat_turn", admit)
    monkeypatch.setattr(chat, "maybe_auto_link_slack", hold)
    if exit_kind == "assignment":
        monkeypatch.setattr(
            chat, "pin_private_agent_store", AsyncMock(side_effect=OSError("store"))
        )
    request = _request(state, path="/api/chat?ws=1", json_body={"slot": "s1", "message": "hello"})
    try:
        if exit_kind in {"cancel", "error"}:
            error = asyncio.CancelledError if exit_kind == "cancel" else RuntimeError
            with pytest.raises(error):
                await asyncio.wait_for(chat.api_chat(request), BOUND)
        else:
            response = await asyncio.wait_for(chat.api_chat(request), BOUND)
            expected_status = {"stop": 200, "close": 409, "assignment": 503}
            assert response.status == expected_status[exit_kind]
        assert len(accepted) == 1
        work_id = accepted[0].work.work_id
        current = await service.current_work(work_id)
        assert current.state is OperationState.FAILED
        assert not service._scheduler.is_active(work_id)
        run.assert_not_awaited()
        if slot is not None:
            assert slot._has_reader is False
    finally:
        await asyncio.wait_for(drain_background_tasks(state), BOUND)


@pytest.mark.asyncio
async def test_chat_dispatch_transfers_admission_to_runner(tmp_path, monkeypatch, service):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    state.push_slots_update = MagicMock()
    monkeypatch.setattr(chat, "_run_chat", AsyncMock())
    monkeypatch.setattr(chat, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat, "maybe_auto_tag", AsyncMock())
    monkeypatch.setattr(chat, "maybe_auto_link_slack", AsyncMock())
    request = _request(state, path="/api/chat?ws=1", json_body={"slot": "s1", "message": "hello"})
    try:
        response = await asyncio.wait_for(chat.api_chat(request), BOUND)
        assert response.status == 200
        work_id = json.loads(response.body)["admission"]["work"]["work_id"]
        await asyncio.wait_for(slot.task, BOUND)
        assert (await service.current_work(work_id)).state is OperationState.COMPLETED
    finally:
        await asyncio.wait_for(drain_background_tasks(state), BOUND)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["helper", "run"])
@pytest.mark.parametrize("attached", [False, True])
async def test_workflow_replay_never_starts_or_abandons(
    tmp_path, monkeypatch, service, path, attached
):
    request = _request(_state(None), json_body={"source": "async def workflow(ctx): pass"})
    first = await workflows._admit_workflow_run(request, SimpleNamespace(), {})
    if attached:
        await first.attach_child("original-run")
    replay = ag.Admission(
        service, "admitted", work=await service.current_work(first.work.work_id), replayed=True
    )
    monkeypatch.setattr(workflows, "_admit_workflow_run", AsyncMock(return_value=replay))
    start = AsyncMock(return_value={"run_id": "duplicate-run"})
    svc = SimpleNamespace(start=start)
    monkeypatch.setattr(workflows, "_svc", lambda request: svc)
    monkeypatch.setattr(workflows, "_private_memory_refusal", AsyncMock(return_value=None))
    if path == "helper":
        response = await workflows._start_admitted(request, svc, {}, start)
    else:
        response = await workflows.api_workflow_run(request)
    start.assert_not_awaited()
    body = json.loads(response.body)
    assert body["replayed"] is True
    assert body["admission"]["work"]["work_id"] == first.work.work_id
    assert body.get("run_id") == ("original-run" if attached else None)
    assert response.status == (200 if attached else 202)
    assert service._scheduler.is_active(first.work.work_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["start", "retry", "execute"])
async def test_taskrunner_replay_never_starts_or_abandons(tmp_path, monkeypatch, service, path):
    runner = _runner(tmp_path)
    request = _request(
        _state(runner),
        json_body={"spec": "__inline__:# task", "from_index": 0},
        match_info={"task_id": "original-task"},
    )
    first = await entry.admit_background_run(
        request, operation=ag.OperationKind.TASK, session_key="taskrunner:original-task", probe=None
    )
    await first.attach_child("original-task")
    replay = ag.Admission(
        service, "admitted", work=await service.current_work(first.work.work_id), replayed=True
    )
    monkeypatch.setattr(entry, "admit_background_run", AsyncMock(return_value=replay))
    monkeypatch.setattr(taskrunner, "_admit_existing_run", AsyncMock(return_value=replay))
    monkeypatch.setattr(taskrunner, "app_disabled", lambda app: False)
    materialize = AsyncMock(return_value=tmp_path / "spec.md")
    monkeypatch.setattr(taskrunner, "_materialize_inline_spec", materialize)
    handlers = {
        "start": taskrunner.api_taskrunner_start,
        "retry": taskrunner.api_taskrunner_retry,
        "execute": taskrunner.api_taskrunner_execute_plan,
    }
    response = await handlers[path](request)
    runner.start_background.assert_not_awaited()
    runner.retry_from_task.assert_not_awaited()
    runner.execute_plan.assert_not_awaited()
    materialize.assert_not_awaited()
    body = json.loads(response.body)
    assert response.status == 200 and body["replayed"] is True
    assert body["task_id"] == "original-task"
    assert service._scheduler.is_active(first.work.work_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "write", "cancel"])
async def test_taskrunner_inline_failure_releases_admission(
    tmp_path, monkeypatch, service, failure
):
    runner = _runner(tmp_path)
    spec = "__inline__:" if failure == "empty" else "__inline__:# task"
    request = _request(_state(runner), json_body={"spec": spec})
    monkeypatch.setattr(taskrunner, "app_disabled", lambda app: False)
    error = asyncio.CancelledError() if failure == "cancel" else OSError("write failed")
    monkeypatch.setattr(taskrunner, "_materialize_inline_spec", AsyncMock(side_effect=error))
    try:
        await asyncio.wait_for(taskrunner.api_taskrunner_start(request), BOUND)
    except (OSError, asyncio.CancelledError):
        pass
    runner.start_background.assert_not_awaited()
    assert service.capacity(ag.actor_budget(request)).principal.active == 0


@pytest.mark.asyncio
async def test_stopped_chat_releases_capacity_before_queue_drain(tmp_path, monkeypatch, service):
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")
    state.push_slots_update = MagicMock()
    monkeypatch.setattr(chat, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat, "maybe_auto_tag", AsyncMock())
    request = _request(state, path="/api/chat?ws=1", json_body={"slot": "s1", "message": "hello"})

    async def hold(st, sl):
        sl._stop_generation += 1
        sl.queue_append("next")

    async def drain(st, sl):
        assert sl._turn_admission_reserved
        assert service.capacity(ag.actor_budget(request)).principal.active == 0

    drained = AsyncMock(side_effect=drain)
    monkeypatch.setattr(chat, "maybe_auto_link_slack", hold)
    monkeypatch.setattr(chat, "_start_next_queued_turn", drained)
    try:
        response = await asyncio.wait_for(chat.api_chat(request), BOUND)
        assert json.loads(response.body)["stopped"] is True
        drained.assert_awaited_once()
    finally:
        await asyncio.wait_for(drain_background_tasks(state), BOUND)


@pytest.mark.asyncio
async def test_workflow_same_key_returns_original_execution(monkeypatch, service):
    request = _request(_state(None))
    monkeypatch.setattr(ag, "idempotency_key_of", lambda request: "workflow-retry-key")
    start = AsyncMock(return_value={"run_id": "original-run"})
    first = await workflows._start_admitted(request, SimpleNamespace(), {"source": "a"}, start)
    second = await workflows._start_admitted(request, SimpleNamespace(), {"source": "a"}, start)
    assert first["run_id"] == json.loads(second.body)["run_id"] == "original-run"
    start.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_same_key_with_a_changed_body_is_a_conflict(monkeypatch, service):
    request = _request(_state(None))
    monkeypatch.setattr(ag, "idempotency_key_of", lambda request: "workflow-retry-key")
    start = AsyncMock(return_value={"run_id": "original-run"})
    await workflows._start_admitted(request, SimpleNamespace(), {"source": "a"}, start)
    changed = await workflows._start_admitted(request, SimpleNamespace(), {"source": "b"}, start)
    assert changed.status == 409
    assert json.loads(changed.body)["code"] == "idempotency_conflict"
    start.assert_awaited_once()
