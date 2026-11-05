"""An admitted child names its run id before its spawn can dispatch it.

The run id is written on the ADMITTED record and its run key is bound to the
admitted budget before the spawn call, so a restart between the spawn and the
attach does not cancel a child the task queue still runs, a start the queue
releases later lands in the admitted person's slice, and the gate keeps the
capacity a failed attach still owes until the child has stopped.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_shared_crew_admission import _open, _service
from test_shared_crew_entry_points import service as entry_service

from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import admission_link as link
from kiro_crew.shared_crew import containment
from kiro_crew.shared_crew.admission import OperationState, ReasonCode
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import await_until

S = OperationState
A = Principal.system("alice-link").id
service = entry_service


class _Minter:
    """A subagent manager that only draws run ids."""

    def __init__(self, *ids: str) -> None:
        self._ids = list(ids)

    def _mint_agent_id(self) -> str:
        return self._ids.pop(0)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=10_000.0)


async def _child(svc, key="child-key-0001"):
    return await svc.admit_child(
        parent_session_key="", fallback_budget=A, idempotency_key=key, digest=key
    )


@pytest.mark.asyncio
async def test_a_linked_child_is_held_through_a_restart_until_its_run_ends(tmp_path, clock):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    svc = _service(store, clock)
    child = await _child(svc)
    assert child.admitted
    assert await link.link_spawn(child, _Minter("run-abc")) == "run-abc"
    durable = store.get_sync(child.work.work_id)
    assert durable.state is S.ADMITTED and durable.extra["execution_id"] == "run-abc"
    # -- the gateway stops after the spawn committed its row, before the attach --
    store.tasks.close()
    store2 = _open(path, clock)
    states: dict[str, OperationState] = {}
    svc2 = _service(store2, clock, child_probe=lambda eid: states.get(eid))
    await svc2.ensure_ready()
    held = store2.get_sync(child.work.work_id)
    assert held.state is S.RECONCILING  # not cancelled: the queue may still run it
    # A recovered queue dispatch of the parentless run still finds its person.
    assert svc2.budget_for_session("subagent:run-abc") == A
    # Its child slot stays held until the probe says the run ended.
    assert svc2._scheduler.is_active(child.work.work_id)
    await svc2.tick()
    assert store2.get_sync(child.work.work_id).state is S.RECONCILING
    states["run-abc"] = S.COMPLETED
    assert (await svc2.tick())["children"] == 1
    assert store2.get_sync(child.work.work_id).state is S.COMPLETED
    assert child.work.work_id not in svc2._live
    assert not svc2._scheduler.is_active(child.work.work_id)


@pytest.mark.asyncio
async def test_the_boot_reconciles_admission_before_it_opens_subagent_dispatch(tmp_path, clock):
    """An API-only gateway opens the queue with no chat send to reconcile first."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    path = tmp_path / "t.db"
    store = _open(path, clock)
    child = await _child(_service(store, clock))
    assert await link.link_spawn(child, _Minter("run-boot")) == "run-boot"
    store.tasks.close()
    svc2 = _service(_open(path, clock), clock, child_probe=lambda eid: None)
    seen: list[str | None] = []
    gateway = object.__new__(GatewayOrchestrator)
    gateway._memory_startup = None
    gateway._background_tasks = set()
    gateway.subagent_mgr = SimpleNamespace(
        release_queue_dispatch=lambda: seen.append(svc2.budget_for_session("subagent:run-boot"))
    )
    attempts: list[int] = []

    async def _current():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("store unreadable")
        return svc2

    with (
        patch.object(ag, "current_service", _current),
        patch.object(link, "DISPATCH_RETRY_SECONDS", 0),
    ):
        await gateway._start_subagent_dispatch_after_memory_ready()
        # A failed reconcile keeps recovered dispatch held: nothing is released.
        assert seen == []
        [retry] = gateway._background_tasks
        await await_until(retry, what="the dispatch retry")
    # The retry released it only once the run key named its person again.
    assert seen == [A]
    assert len(attempts) == 2 and not gateway._background_tasks


async def _restarted_with_linked_child(tmp_path, clock, run_id, **cfg):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    child = await _child(_service(store, clock, **cfg))
    assert await link.link_spawn(child, _Minter(run_id)) == run_id
    store.tasks.close()
    store2 = _open(path, clock)
    return child, store2, _service(store2, clock, child_probe=lambda eid: None, **cfg)


async def _release_with(svc, monkeypatch):
    released: list[int] = []

    async def _current():
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    monkeypatch.setattr(link, "DISPATCH_RETRY_SECONDS", 0)
    retry = await link.release_after_reconcile(lambda: released.append(1))
    return released, retry


@pytest.mark.asyncio
async def test_a_child_whose_reconcile_failed_holds_dispatch_until_it_is_reconciled(
    tmp_path, clock, monkeypatch
):
    child, store2, svc2 = await _restarted_with_linked_child(tmp_path, clock, "run-lock")
    real = svc2._reconcile_one
    calls: list[str] = []

    async def _locked_twice(work):
        calls.append(work.work_id)
        if len(calls) <= 2:  # the boot reconcile, then the check right after it
            raise RuntimeError("database is locked")
        await real(work)

    monkeypatch.setattr(svc2, "_reconcile_one", _locked_twice)
    released, retry = await _release_with(svc2, monkeypatch)
    # ensure_ready logged the row and marked itself ready; dispatch stays held.
    assert svc2._ready and released == [] and retry is not None
    await await_until(retry, what="the dispatch retry")
    assert released == [1] and len(calls) == 3
    assert store2.get_sync(child.work.work_id).state is S.RECONCILING
    assert svc2._scheduler.is_active(child.work.work_id)
    assert svc2.budget_for_session("subagent:run-lock") == A


@pytest.mark.asyncio
async def test_under_hard_limits_dispatch_waits_for_the_recovered_childs_slice(
    tmp_path, clock, monkeypatch, caplog
):
    child, _, svc2 = await _restarted_with_linked_child(tmp_path, clock, "run-hard")
    svc2._limits = replace(svc2._limits, hard_limits=True)
    # Not applied at the boot reconcile nor at the check after it; applied on retry.
    units = iter(["", "", "kirocrew-p.slice"])

    async def _prepare(budget_key):
        return next(units)

    monkeypatch.setattr(svc2, "prepare_principal_slice", _prepare)
    released, retry = await _release_with(svc2, monkeypatch)
    assert released == [] and retry is not None  # the slice was not applied
    # The hold names the child it waits on, so an operator can find it.
    assert any(child.work.work_id in r.getMessage() for r in caplog.records)
    await await_until(retry, what="the dispatch retry")
    assert released == [1]


@pytest.mark.asyncio
async def test_without_hard_limits_an_unapplied_slice_does_not_hold_dispatch(
    tmp_path, clock, monkeypatch
):
    _, _, svc2 = await _restarted_with_linked_child(tmp_path, clock, "run-soft")
    assert not svc2._limits.hard_limits

    async def _prepare(budget_key):
        return ""

    monkeypatch.setattr(svc2, "prepare_principal_slice", _prepare)
    released, retry = await _release_with(svc2, monkeypatch)
    assert released == [1] and retry is None


@pytest.mark.asyncio
async def test_an_unlinked_admitted_child_is_still_cancelled_on_restart(tmp_path, clock):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    svc = _service(store, clock)
    child = await _child(svc)
    store.tasks.close()
    store2 = _open(path, clock)
    svc2 = _service(store2, clock, child_probe=lambda eid: None)
    await svc2.ensure_ready()
    ended = store2.get_sync(child.work.work_id)
    assert ended.state is S.CANCELLED
    assert ended.extra[ag.RECONCILE_DETAIL_KEY] == ag.RECONCILE_RESTARTED_BEFORE_START


@pytest.mark.asyncio
async def test_a_linked_run_key_names_its_principal_before_the_attach(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    child = await _child(svc)
    with patch.object(ag, "_SERVICE", svc):
        info = SimpleNamespace(id="run-q", parent_session_key="")
        assert containment.run_principal(info) is None
        assert await link.link_spawn(child, _Minter("run-q")) == "run-q"
        # A queue release or an approval starts it outside the spawn's scope.
        assert containment.run_principal(info) == A


@pytest.mark.asyncio
async def test_a_child_ended_while_its_linked_spawn_ran_is_stopped_on_attach(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    stopped: list[str] = []
    svc.set_child_cancel(lambda work: stopped.append(work.extra["execution_id"]))
    child = await _child(svc)
    assert await link.link_spawn(child, _Minter("run-late")) == "run-late"
    await svc.cancel(
        child.work.work_id, expected_revision=svc._live[child.work.work_id].work.revision
    )
    await child.attach_child("run-late", session_key="subagent:run-late")
    assert stopped == ["run-late"]


@pytest.mark.asyncio
async def test_a_refused_link_write_fails_closed_and_refuses_the_spawn(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    child = await _child(svc)

    async def locked(*_a, **_kw):
        raise RuntimeError("database is locked")

    with patch.object(svc._store, "annotate", side_effect=locked):
        assert await link.link_spawn(child, _Minter("run-x")) is None
    live = svc._live[child.work.work_id]
    assert live.work.state in ag.TERMINAL and live.unpersisted is not None
    await svc.tick()
    assert child.work.work_id not in svc._live
    assert svc._store.get_sync(child.work.work_id).state is S.FAILED


@pytest.mark.asyncio
async def test_nothing_is_linked_when_admission_is_off_or_the_work_ended(tmp_path, clock):
    off = ag.Admission(None, "passthrough")
    assert await link.link_spawn(off, _Minter("run-1")) == ""
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    child = await _child(svc)
    assert await link.link_spawn(child, object()) == ""  # a manager that mints nothing
    await child.abandon(reason=ReasonCode.RUNTIME_ERROR)
    assert await link.link_spawn(child, _Minter("run-2")) is None


@pytest.mark.asyncio
async def test_an_app_spawn_runs_under_the_run_id_it_linked(service, monkeypatch):
    from kiro_crew.apps import spawn_sdk

    monkeypatch.setattr(
        spawn_sdk,
        "list_agents",
        lambda: [SimpleNamespace(name="demo--bg", filename="demo--bg.json")],
    )
    monkeypatch.setattr(spawn_sdk, "is_internal_agent_spec", lambda _a: False)
    monkeypatch.setattr(spawn_sdk, "_app_spawn_model", lambda _app, _agent: "")
    seen: dict[str, object] = {}

    class _Manager(_Minter):
        _agents: dict = {}

        async def spawn_async(self, _task, **kw):
            run_id = kw["_preassigned_id"]
            [work] = await service.list_work(session_key="app:demo")
            seen["linked"] = work.state is S.ADMITTED and work.extra["execution_id"] == run_id
            # The queue pump starts it later, with no parent session and no scope.
            pump = SimpleNamespace(id=run_id, parent_session_key="")
            seen["principal"] = await asyncio.to_thread(lambda: containment.run_principal(pump))
            return SimpleNamespace(id=run_id, error="", done=False)

        def get(self, _id):
            return None

    impl = spawn_sdk.build_spawn_impl(_Manager("run-app"))
    assert await impl("do it", "demo--bg", True, "", "demo") == "run-app"
    assert seen == {"linked": True, "principal": Principal.system("app:demo").id}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["spawn", "continue", "retry"])
async def test_each_spawn_route_starts_the_run_id_it_linked(service, monkeypatch, route):
    from handlers_messaging_coverage_helpers import _Req, _state
    from test_handlers_messaging_coverage import _info, _mgr

    import kiro_crew.dashboard.handlers.messaging as mod
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.shared_crew import admission_entry

    admitted: list[ag.Admission] = []

    async def admit(*_a, **_kw):
        child = await service.admit_child(parent_session_key="", fallback_budget=A)
        admitted.append(child)
        return child

    monkeypatch.setattr(admission_entry, "admit_subagent", admit)
    mgr = _mgr()
    mgr._mint_agent_id = lambda: "run-h"
    seen: list[object] = []

    def start(*_a, **kw):
        live = service._live[admitted[0].work.work_id]
        seen.append((kw.get("_preassigned_id"), live.work.state, live.work.extra["execution_id"]))
        return _info(id=kw["_preassigned_id"], parent_session_key="")

    match = {"agent_id": "conv1" if route == "continue" else "a1"}
    if route == "retry":
        old = _info(done=True, outcome="failed", _raw_task="t")
        old.execution_context = ExecutionContext(
            None, MemoryStoreRef("default"), "template", "kirocrew"
        )
        mgr.get.return_value = old
        mgr.claim_retry.return_value = ""
        mgr.spawn.side_effect = start
        handler, body = mod.api_spawn_retry, None
    elif route == "continue":
        mgr.continue_conversation.side_effect = start
        handler, body = mod.api_spawn_continue, {"task": "x"}
    else:
        mgr.spawn.side_effect = start
        handler, body = mod.api_spawn, {"task": "x"}
    response = await handler(_Req(_state(subagents=mgr), body, match_info=match))
    assert response.status == 200
    assert seen == [("run-h", S.ADMITTED, "run-h")]
    work = await service._store.get(admitted[0].work.work_id)
    assert work.state is S.RUNNING and work.extra["execution_id"] == "run-h"


@pytest.mark.asyncio
async def test_a_cancel_racing_the_link_write_still_ends_the_child(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    child = await _child(svc)
    work_id = child.work.work_id
    real = svc._store.annotate
    cancel: list[asyncio.Task] = []

    async def racing(work, extra):
        written = await real(work, extra)
        # The user's stop, sent at the revision it saw, runs before the link
        # write's result is back: its terminal write loses the compare-and-set.
        cancel.append(asyncio.ensure_future(svc.cancel(work_id, expected_revision=work.revision)))
        await cancel[0]
        return written

    with patch.object(svc._store, "annotate", side_effect=racing):
        assert await link.link_spawn(child, _Minter("run-r")) is None
    durable = svc._store.get_sync(work_id)
    assert durable.state is S.CANCELLED and durable.reason is ReasonCode.CANCELLED_BY_USER
    assert work_id not in svc._live and not svc._scheduler.is_active(work_id)


@pytest.mark.asyncio
async def test_a_spawn_cancelled_during_the_link_write_frees_its_slot(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    child = await _child(svc)
    work_id = child.work.work_id
    entered, release = asyncio.Event(), asyncio.Event()
    real = svc._store.annotate

    async def slow(work, extra):
        entered.set()
        await release.wait()
        return await real(work, extra)

    with patch.object(svc._store, "annotate", side_effect=slow):
        task = asyncio.ensure_future(link.link_spawn(child, _Minter("run-c")))
        await asyncio.wait_for(entered.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await svc.tick()
    assert work_id not in svc._live and not svc._scheduler.is_active(work_id)
    assert svc._store.get_sync(work_id).state is S.FAILED


def test_the_child_probe_waits_while_the_queue_store_is_still_opening():
    """A restart reconciles before the queue store opens: absence is not proof yet."""
    from kiro_crew.shared_crew.admission_entry import subagent_probe

    opening = SimpleNamespace(done=lambda: False)
    manager = SimpleNamespace(_agents={}, _taskq_init_task=opening, taskq_store=lambda: None)
    probe = subagent_probe(manager)
    assert probe("run-early") is None
    manager._taskq_init_task = SimpleNamespace(done=lambda: True)
    assert probe("run-early") is S.UNKNOWN_OUTCOME


def test_the_child_probe_decides_absence_only_from_a_successful_store_read():
    """A failed open or a failed read may still hold a queued run: never settle it."""
    from kiro_crew.shared_crew.admission_entry import subagent_probe

    done = SimpleNamespace(done=lambda: True)

    def _raise(*_a):
        raise RuntimeError("database is locked")

    failed_open = SimpleNamespace(
        _agents={},
        _taskq_init_task=done,
        _taskq_unavailable="taskq_open_failed",
        taskq_store=lambda: None,
    )
    assert subagent_probe(failed_open)("run-x") is None
    store_raises = SimpleNamespace(_agents={}, _taskq_init_task=done, taskq_store=_raise)
    assert subagent_probe(store_raises)("run-x") is None
    read_raises = SimpleNamespace(
        _agents={}, _taskq_init_task=done, taskq_store=lambda: SimpleNamespace(get=_raise)
    )
    assert subagent_probe(read_raises)("run-x") is None
    # A successful read that finds nothing, or a queue that is off, is real absence.
    empty = SimpleNamespace(
        _agents={}, _taskq_init_task=done, taskq_store=lambda: SimpleNamespace(get=lambda i: None)
    )
    assert subagent_probe(empty)("run-x") is S.UNKNOWN_OUTCOME
    off = SimpleNamespace(_agents={}, _taskq_init_task=None, taskq_store=lambda: None)
    assert subagent_probe(off)("run-x") is S.UNKNOWN_OUTCOME
