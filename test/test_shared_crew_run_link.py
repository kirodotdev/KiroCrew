"""An admitted task run names its run id before the task runner can start it.

The task runner persists a run before the handler's ``attach_child`` records
RUNNING, and resumes a run it finds running after a gateway stop. So the run id
is written on the ADMITTED record first, and restart reconciliation holds a
linked record of a probed operation until its run ends, instead of cancelling
it as "never started" while the task runner runs the work.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_shared_crew_admission import _open, _service
from test_shared_crew_entry_points import _admission_request
from test_shared_crew_entry_points import service as entry_service

from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import admission_run_link as link
from kiro_crew.shared_crew import host_hooks
from kiro_crew.shared_crew.admission import OperationKind, OperationState, ReasonCode
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.testing.clock import ManualClock

S = OperationState
A = Principal.system("alice-run-link").id
TASK = OperationKind.TASK
service = entry_service


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=20_000.0)


async def _task(svc, key="task-key-0001"):
    return await svc.admit(
        budget_key=A,
        session_key="dashboard:alice",
        operation=TASK,
        idempotency_key=key,
        digest=key,
        allow_queue=False,
    )


def _restart(path, clock, probe):
    """A new gateway process: the task runner's probe is installed before reconciling."""
    svc = _service(_open(path, clock), clock)
    svc.register_probe(TASK, probe)
    return svc


@pytest.mark.asyncio
async def test_a_linked_task_run_is_held_through_a_restart_until_its_run_ends(tmp_path, clock):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    svc = _service(store, clock)
    run = await _task(svc)
    assert run.admitted
    assert await link.link_run(run, "spec_1") is True
    durable = store.get_sync(run.work.work_id)
    assert durable.state is S.ADMITTED and durable.extra["execution_id"] == "spec_1"
    # -- the gateway stops after start_background persisted the run, before the attach --
    store.tasks.close()
    status: dict[str, OperationState | None] = {"spec_1": None}  # the runner resumed it
    svc2 = _restart(path, clock, lambda task_id: status.get(task_id, S.UNKNOWN_OUTCOME))
    await svc2.ensure_ready()
    held = svc2._store.get_sync(run.work.work_id)
    assert held.state is S.RECONCILING  # not cancelled: the task runner still runs it
    assert svc2._scheduler.is_active(run.work.work_id)
    await svc2.tick()
    assert svc2._store.get_sync(run.work.work_id).state is S.RECONCILING
    status["spec_1"] = S.COMPLETED
    assert (await svc2.tick())["children"] == 1
    assert svc2._store.get_sync(run.work.work_id).state is S.COMPLETED
    assert not svc2._scheduler.is_active(run.work.work_id)


@pytest.mark.asyncio
async def test_an_unlinked_admitted_task_run_is_still_cancelled_on_restart(tmp_path, clock):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    run = await _task(_service(store, clock))
    store.tasks.close()
    svc2 = _restart(path, clock, lambda _task_id: None)
    await svc2.ensure_ready()
    ended = svc2._store.get_sync(run.work.work_id)
    assert ended.state is S.CANCELLED
    assert ended.extra[ag.RECONCILE_DETAIL_KEY] == ag.RECONCILE_RESTARTED_BEFORE_START


@pytest.mark.asyncio
async def test_the_gateway_installs_the_task_runner_probe_before_reconciling(monkeypatch, clock):
    state = SimpleNamespace(task_runner=None)
    monkeypatch.setattr(ag, "_HOOKS", ag.HostHooks())
    host_hooks.install(state)
    probe = ag._HOOKS.run_probes[TASK]
    assert probe("spec_1") is None  # no runner yet: still running
    state.task_runner = SimpleNamespace(_runs={"spec_1": SimpleNamespace(status="running")})
    assert probe("spec_1") is None
    state.task_runner._runs["spec_1"].status = "completed"
    assert probe("spec_1") is S.COMPLETED
    svc = _service(None, clock)
    ag._apply_hooks(svc)
    assert svc._probes[TASK] is probe


@pytest.mark.asyncio
async def test_a_run_ended_while_its_linked_start_ran_is_stopped_on_attach(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    stopped: list[str] = []
    svc.register_cancel(TASK, lambda work: stopped.append(work.extra["execution_id"]))
    run = await _task(svc)
    assert await link.link_run(run, "spec_late")
    await svc.cancel(run.work.work_id, expected_revision=svc._live[run.work.work_id].work.revision)
    await run.attach_child("spec_late", session_key="taskrunner:spec_late")
    assert stopped == ["spec_late"]


@pytest.mark.asyncio
async def test_a_refused_link_write_fails_closed(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    run = await _task(svc)

    async def locked(*_a, **_kw):
        raise RuntimeError("database is locked")

    with patch.object(svc._store, "annotate", side_effect=locked):
        assert await link.link_run(run, "spec_x") is False
    assert svc._live[run.work.work_id].unpersisted is not None
    await svc.tick()
    assert svc._store.get_sync(run.work.work_id).state is S.FAILED


@pytest.mark.asyncio
async def test_a_cancel_racing_the_link_write_still_ends_the_run(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    run = await _task(svc)
    work_id = run.work.work_id
    real = svc._store.annotate

    async def racing(work, extra):
        written = await real(work, extra)
        # A stop sent at the revision it saw loses its compare-and-set to the link.
        await svc.cancel(work_id, expected_revision=work.revision)
        return written

    with patch.object(svc._store, "annotate", side_effect=racing):
        assert await link.link_run(run, "spec_r") is False
    durable = svc._store.get_sync(work_id)
    assert durable.state is S.CANCELLED and durable.reason is ReasonCode.CANCELLED_BY_USER
    assert work_id not in svc._live and not svc._scheduler.is_active(work_id)


@pytest.mark.asyncio
async def test_nothing_is_linked_when_admission_is_off_or_the_work_ended(tmp_path, clock):
    assert await link.link_run(ag.Admission(None, "passthrough"), "spec_1") is True
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    run = await _task(svc)
    await run.abandon(reason=ReasonCode.RUNTIME_ERROR)
    assert await link.link_run(run, "spec_2") is False


def _record(service):
    [work] = [live.work for live in service._live.values()]
    return work


@pytest.mark.asyncio
async def test_start_names_the_run_id_before_start_background(service, monkeypatch, tmp_path):
    from handlers_taskrunner_coverage_helpers import _runner, _state

    from kiro_crew.dashboard.handlers import taskrunner

    runner = _runner(tmp_path)
    runner.mint_task_id = lambda spec: f"{Path(spec).stem}_7"
    seen = []

    async def start(*_a, **kw):
        work = _record(service)
        seen.append((kw.get("task_id"), work.state, work.extra.get("execution_id")))
        return kw["task_id"]

    runner.start_background = AsyncMock(side_effect=start)
    monkeypatch.setattr(taskrunner, "app_disabled", lambda _app: False)
    monkeypatch.setattr(taskrunner, "_sel", MagicMock())
    body = {"spec": "__inline__:# Work", "source": "dashboard"}
    response = await taskrunner.api_taskrunner_start(_admission_request(_state(runner), body))
    assert response.status == 200, response.body
    [(run_id, linked_state, linked_id)] = seen
    assert run_id.startswith("TASK_") and (linked_state, linked_id) == (S.ADMITTED, run_id)
    assert json.loads(response.body)["task_id"] == run_id
    work = await service._store.get(_record(service).work_id)
    assert work.state is S.RUNNING and work.extra["execution_id"] == run_id


@pytest.mark.asyncio
async def test_start_refuses_and_removes_its_spec_when_the_link_fails(
    service, monkeypatch, tmp_path
):
    from handlers_taskrunner_coverage_helpers import _runner, _state

    from kiro_crew.dashboard.handlers import taskrunner

    runner = _runner(tmp_path)
    runner.mint_task_id = lambda spec: "TASK_x_1"
    monkeypatch.setattr(taskrunner, "app_disabled", lambda _app: False)
    monkeypatch.setattr(taskrunner, "_sel", MagicMock())

    async def locked(*_a, **_kw):
        raise RuntimeError("database is locked")

    body = {"spec": "__inline__:# Work", "source": "dashboard"}
    with patch.object(service._store, "annotate", side_effect=locked):
        response = await taskrunner.api_taskrunner_start(_admission_request(_state(runner), body))
    assert response.status == 503
    runner.start_background.assert_not_awaited()
    assert list(runner._work_dir.glob("TASK_*.md")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint,method", [("retry", "retry_from_task"), ("execute_plan", "execute_plan")]
)
async def test_a_resumed_run_is_linked_before_it_is_set_running(
    service, monkeypatch, tmp_path, endpoint, method
):
    from handlers_taskrunner_coverage_helpers import _runner, _state

    from kiro_crew.dashboard.handlers import taskrunner

    runner = _runner(tmp_path)
    runner._runs = {"t-1": SimpleNamespace(status="planned", name="nightly", task_id="t-1")}
    seen = []

    async def resume(*_a, **_kw):
        work = _record(service)
        seen.append((work.state, work.extra.get("execution_id")))

    setattr(runner, method, AsyncMock(side_effect=resume))
    monkeypatch.setattr(taskrunner, "_sel", MagicMock())
    request = _admission_request(_state(runner), {}, path=f"/api/taskrunner/t-1/{endpoint}")
    response = await getattr(taskrunner, f"api_taskrunner_{endpoint}")(request)
    assert response.status == 200, response.body
    assert seen == [(S.ADMITTED, "t-1")]
    assert _record(service).state is S.RUNNING


@pytest.mark.asyncio
async def test_start_background_runs_under_the_id_it_was_given(tmp_path):
    from test_taskrunner_spec_prefix import _runner

    runner = _runner(tmp_path / "work")
    runner.run = AsyncMock()
    spec = tmp_path / "job.md"
    spec.write_text("# Job", encoding="utf-8")
    minted = runner.mint_task_id(spec)
    assert minted.startswith("job_")
    assert await runner.start_background(spec, task_id=minted) == minted
    await asyncio.wait_for(runner._tasks.get(minted) or asyncio.sleep(0), timeout=30)
    with pytest.raises(ValueError, match="already in use"):
        await runner.start_background(spec, task_id=minted)
    assert list(runner._runs) == [minted]


class _Done:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _probe_runner(status, *, task=None, pending=(), starting=()):
    return SimpleNamespace(
        _runs={"r1": SimpleNamespace(status=status)},
        _tasks={} if task is None else {"r1": task},
        _recovery_pending=set(pending),
        _start_ids_in_flight=set(starting),
    )


@pytest.mark.parametrize(
    "runner,expected",
    [
        (_probe_runner("planned"), S.CANCELLED),  # execute cut before it set the run running
        (_probe_runner("planned", task=_Done(False)), None),  # its start is in flight
        (_probe_runner("paused"), S.CANCELLED),
        (_probe_runner("paused", pending=["r1"]), None),  # adoption may still resume it
        # The sweep's execute_plan sets ``planned`` before it installs the run task.
        (_probe_runner("planned", pending=["r1"], starting=["r1"]), None),
        (_probe_runner("planned", starting=["r1"]), None),  # an execute or retry handoff
        (_probe_runner("paused", task=_Done(True)), S.CANCELLED),
        (_probe_runner("running"), None),
        (_probe_runner("completed"), S.COMPLETED),
    ],
    ids=[
        "planned-idle",
        "planned-starting",
        "paused",
        "paused-recovering",
        "resume-handoff",
        "start-handoff",
        "paused-task-done",
        "running",
        "completed",
    ],
)
def test_the_task_runner_probe_settles_only_a_run_that_cannot_be_resumed(runner, expected):
    from kiro_crew.shared_crew.admission_entry import taskrunner_probe

    assert taskrunner_probe(runner)("r1") is expected


@pytest.mark.asyncio
async def test_a_linked_run_left_planned_by_a_restart_is_not_held_forever(tmp_path, clock):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    run = await _task(_service(store, clock))
    assert await link.link_run(run, "r1")
    store.tasks.close()  # -- stopped before execute_plan set the run running
    from kiro_crew.shared_crew.admission_entry import taskrunner_probe

    svc2 = _restart(path, clock, taskrunner_probe(_probe_runner("planned")))
    await svc2.ensure_ready()
    assert (await svc2.tick())["children"] == 1
    assert svc2._store.get_sync(run.work.work_id).state is S.CANCELLED
    assert not svc2._scheduler.is_active(run.work.work_id)


def test_a_run_recovered_as_paused_waits_for_the_adoption_sweep(tmp_path):
    from test_taskrunner_coverage import _registry_item
    from test_taskrunner_coverage import _runner as _coverage_runner

    (tmp_path / "runs.json").write_text(json.dumps([_registry_item()]), encoding="utf-8")
    runner = _coverage_runner(tmp_path)
    assert runner._runs["r1"].status == "paused"
    assert runner._recovery_pending == {"r1"}


def test_a_planned_run_also_waits_for_the_adoption_sweep(tmp_path):
    from test_taskrunner_coverage import _registry_item
    from test_taskrunner_coverage import _runner as _coverage_runner

    # A stop after the sweep's execute_plan persisted ``planned``: its row still resumes it.
    item = _registry_item(status="planned")
    (tmp_path / "runs.json").write_text(json.dumps([item]), encoding="utf-8")
    runner = _coverage_runner(tmp_path)
    assert runner._runs["r1"].status == "planned"
    assert runner._recovery_pending == {"r1"}


def test_ids_minted_before_either_run_is_registered_differ(tmp_path, monkeypatch):
    from test_taskrunner_spec_prefix import _runner

    from kiro_crew.taskrunner_parts import run_lifecycle

    runner = _runner(tmp_path / "work")
    ManualClock(start=1.0).install(monkeypatch, run_lifecycle)  # a clock that does not move
    first, second = runner.mint_task_id("job.md"), runner.mint_task_id("job.md")
    assert (first, second) == ("job_1000000000", "job_1000000001")


@pytest.mark.asyncio
async def test_the_adoption_sweep_ends_the_recovery_wait(tmp_path, monkeypatch):
    from test_taskrunner_coverage import _runner as _coverage_runner

    from kiro_crew.taskq.adapters import runner as runner_adapter

    runner = _coverage_runner(tmp_path)
    runner._recovery_pending = {"r1"}
    await runner.adopt_task_rows()  # no queue store yet: nothing was decided
    assert runner._recovery_pending == {"r1"}
    monkeypatch.setattr(
        runner_adapter, "adopt_orphaned_rows", lambda *_a, **_kw: runner_adapter.AdoptReport()
    )
    runner._task_admission = SimpleNamespace(store=object())
    await runner.adopt_task_rows()
    assert runner._recovery_pending == set()


def test_detaching_the_queue_ends_the_recovery_wait(tmp_path):
    from test_taskrunner_coverage import _runner as _coverage_runner

    runner = _coverage_runner(tmp_path)
    runner._recovery_pending = {"r1"}
    runner.attach_task_admission(SimpleNamespace(store=None))  # the store opens later
    assert runner._recovery_pending == {"r1"}
    runner.attach_task_admission(None)  # no queue: no sweep will decide
    assert runner._recovery_pending == set()


@pytest.mark.asyncio
async def test_a_failed_adoption_sweep_still_ends_the_recovery_wait(tmp_path, monkeypatch):
    from test_taskrunner_coverage import _runner as _coverage_runner

    from kiro_crew.taskq.adapters import runner as runner_adapter

    def broken(*_a, **_kw):
        raise RuntimeError("row write refused")

    runner = _coverage_runner(tmp_path)
    runner._recovery_pending = {"r1"}
    runner._task_admission = SimpleNamespace(store=object())
    monkeypatch.setattr(runner_adapter, "adopt_orphaned_rows", broken)
    with pytest.raises(RuntimeError):
        await runner.adopt_task_rows()
    assert runner._recovery_pending == set()
