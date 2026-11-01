"""Admission completion and refusal remain terminal across delayed store operations."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest
import test_shared_crew_turn_gate as turn_tests

from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import turn_gate
from kiro_crew.shared_crew.admission import Liveness, OperationKind, OperationState, StaleRevision
from kiro_crew.taskq.store import TaskStoreUnavailable
from kiro_crew.testing.wait import async_wait_until, await_until

S = OperationState
service = turn_tests.service
native_slack = turn_tests.native_slack


@pytest.mark.asyncio
async def test_completion_retries_cancellation_committed_before_live_update(service, monkeypatch):
    admission = await service.admit(
        budget_key="p_person",
        session_key="chat",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="race-cancel-complete",
    )
    started, finish, committed, return_cancel = (asyncio.Event() for _ in range(4))
    advance = service._store.advance

    async def delayed_cancel(work, state, **kwargs):
        result = await advance(work, state, **kwargs)
        if state is S.CANCELLING:
            committed.set()
            await await_until(return_cancel)
        return result

    monkeypatch.setattr(service._store, "advance", delayed_cancel)

    async def body():
        started.set()
        await await_until(finish)
        return "finished"

    running = asyncio.create_task(admission.run(body()))
    cancelling = None
    try:
        await await_until(started)
        live = service._live[admission.work.work_id]
        cancelling = asyncio.create_task(
            service.cancel(live.work.work_id, expected_revision=live.work.revision)
        )
        await await_until(committed)
        assert live.work.state is S.RUNNING
        finish.set()
        assert await await_until(running) == "finished"
        current = await service._store.get(live.work.work_id)
        assert current.state is S.CANCELLED
        assert current.runtime.liveness is Liveness.STOPPED
        assert live.ended.is_set()
        assert live.work.work_id not in service._live
        assert service.scheduler.usage("p_person")[1].active == 0
    finally:
        finish.set()
        return_cancel.set()
        await await_until(
            asyncio.gather(running, *([cancelling] if cancelling else []), return_exceptions=True)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["terminal", "read-unavailable", "keeps-changing"])
async def test_settle_adopts_terminal_or_fails_closed(service, monkeypatch, race):
    admission = await service.admit(
        budget_key="p_person",
        session_key="chat",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="race-terminal",
    )
    advance = service._store.advance
    get = service._store.get
    attempts = []

    async def competing_write(work, state, **kwargs):
        if state is not S.COMPLETED:
            return await advance(work, state, **kwargs)
        attempts.append(work.revision)
        if race == "terminal":
            await advance(work, S.FAILED, **kwargs)
        raise StaleRevision(work.revision, work.revision + 1)

    monkeypatch.setattr(service._store, "advance", competing_write)
    if race == "read-unavailable":
        monkeypatch.setattr(
            service._store, "get", AsyncMock(side_effect=TaskStoreUnavailable("locked"))
        )
    assert await admission.run(asyncio.sleep(0, result=7)) == 7
    assert len(attempts) == (3 if race == "keeps-changing" else 1)
    work_id = admission.work.work_id
    if race == "terminal":
        assert work_id not in service._live
        assert (await service._store.get(work_id)).state is S.FAILED
    else:
        # Failed closed: ended in memory, slot held until the terminal write lands.
        assert service._live[work_id].unpersisted is not None
        assert service.scheduler.usage("p_person")[1].active == 1
        monkeypatch.setattr(service._store, "advance", advance)
        monkeypatch.setattr(service._store, "get", get)
        await service.tick()
        assert work_id not in service._live
        assert (await service._store.get(work_id)).state is S.FAILED
    assert service.scheduler.usage("p_person")[1].active == 0


@pytest.fixture(params=["lookup", "refused"])
def unavailable(service, monkeypatch, request):
    if request.param == "lookup":

        def failed_lookup(_scope):
            raise TaskStoreUnavailable("locked")

        monkeypatch.setattr(service._store.tasks, "work_by_scope", failed_lookup)
    else:
        monkeypatch.setattr(
            service,
            "admit",
            AsyncMock(return_value=ag.Admission(service, "rejected", store_unavailable=True)),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["direct", "raising", "channel"])
async def test_unavailable_admission_closes_work_and_notifies(unavailable, wrapper):
    body = AsyncMock()
    work = body()
    told = []

    async def refused(reason):
        told.append(reason)
        return "refused"

    if wrapper == "direct":
        assert await turn_gate.run_admitted(work, session_key="side", refused=refused) is None
    elif wrapper == "raising":
        with pytest.raises(turn_gate.TurnRefused) as error:
            await turn_gate.run_admitted_or_raise(work, session_key="side")
        told.append(error.value.reason)
    else:
        # The factory owns its coroutine until the admitted wrapper calls it.
        work.close()
        assert (
            await turn_gate.run_channel_turn(
                session_key="channel",
                text="hi",
                automation=False,
                run=body,
                refused=refused,
            )
            == "refused"
        )
    assert inspect.getcoroutinestate(work) == inspect.CORO_CLOSED
    body.assert_not_awaited()
    assert told == [ag.TASK_STORE_UNAVAILABLE_CODE]


@pytest.mark.asyncio
async def test_unavailable_store_cleans_up_thread_and_side(unavailable, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_threads
    from kiro_crew.dashboard.handlers import side

    frames = []
    monkeypatch.setattr(chat_threads, "_thread_turn_body", AsyncMock())
    monkeypatch.setattr(chat_threads, "broadcast_thread_reply", lambda _s, **kw: frames.append(kw))
    monkeypatch.setattr(chat_threads, "_in_flight", {"tab:m1"})
    await chat_threads._run_thread_turn(
        SimpleNamespace(), SimpleNamespace(key="tab"), "m1", "run-1", "hi", {}, [], "tab:m1", None
    )
    assert not chat_threads._in_flight
    chat_threads._thread_turn_body.assert_not_awaited()
    assert frames[-1]["final"] and frames[-1]["is_error"]
    assert "task store unavailable" in frames[-1]["content"]

    monkeypatch.setattr(side, "_run_side_turn", AsyncMock())
    monkeypatch.setattr(side, "broadcast_side_result", lambda _s, **kw: frames.append(kw))
    sidecar = SimpleNamespace(
        messages=[], last_run_id="", is_complete=True, task=None, append_user=lambda _q, **_kw: None
    )
    slot = SimpleNamespace(key="tab", _side=sidecar)
    state = SimpleNamespace(_background_tasks=set())
    run_id = side._dispatch_side_turn(state, slot, "why?")
    await await_until(asyncio.gather(*list(state._background_tasks)))
    assert sidecar.is_complete and sidecar.last_run_id == run_id and sidecar.task is None
    side._run_side_turn.assert_not_awaited()
    assert frames[-1]["final"] and frames[-1]["is_error"]
    assert "task store unavailable" in frames[-1]["content"]


@pytest.mark.asyncio
async def test_remote_crew_store_failure_keeps_unavailable_code(unavailable, monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard import handlers_crew_turn, handlers_instances
    from kiro_crew.dashboard.handlers import source_providers

    monkeypatch.setattr(handlers_instances, "_guard", lambda _r, _op: None)
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: True)
    monkeypatch.setattr(handlers_crew_turn, "_crew_turn", AsyncMock())
    request = make_mocked_request("POST", "/api/instances/i/crew-turn", match_info={"id": "i"})
    response = await handlers_crew_turn.api_crew_turn(request)
    assert response.status == 503
    assert b'"task_store_unavailable"' in response.body
    handlers_crew_turn._crew_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["please answer", "!incognito please answer"])
async def test_capacity_control_waits_for_acceptance_publication(
    service, native_slack, monkeypatch, text
):
    reserved, release = asyncio.Event(), asyncio.Event()
    accept, tick = service._store.accept, service.tick

    async def delayed_accept(request, **kwargs):
        if kwargs["state"] is S.QUEUED:
            reserved.set()
            await await_until(release)
        return await accept(request, **kwargs)

    async def checked_tick(*args, **kwargs):
        assert any(live.work.state is S.QUEUED for live in service._live.values())
        return await tick(*args, **kwargs)

    monkeypatch.setattr(service._store, "accept", delayed_accept)
    monkeypatch.setattr(service, "tick", checked_tick)
    check = asyncio.create_task(
        turn_tests.test_native_slack_controls_do_not_wait_for_model_capacity(
            service, native_slack, text, "UOWNER", False, True
        )
    )
    try:
        await await_until(reserved)
        await async_wait_until(lambda: check.done() or bool(service._lock._waiters))
    finally:
        release.set()
        await await_until(check)
