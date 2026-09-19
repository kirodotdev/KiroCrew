"""TaskRunner steps as taskq rows -- the runner adapter end to end.

Real ``TaskRunner`` + real ``execute_single_task`` + a real SQLite store in
``tmp_path``; only the ACP session provider is faked (the same shape
``test_subagent_stop_reason_consistency`` uses). Fake clock, fake sleeps,
seeded jitter: nothing here waits on wall-clock time.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import Clock, open_task_store, settle_dependency_park, settle_store_writes
from test_taskrunner import _make_mock_sessions

from kiro_crew.acp.types import STOP_REASON_END_TURN, STOP_REASON_TOOL_STALL
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.providers.base import LLMEvent
from kiro_crew.recovery.ladder import RecoveryLadder
from kiro_crew.taskq import model as m
from kiro_crew.taskq.adapters import runner as r
from kiro_crew.taskq.dependency import KIND_RATE_LIMITED, SIGNAL_ATTR, DependencySignal
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner

_STALL_EVIDENCE = "verdict=unknown; idle_secs=5400; tool=execute_bash; evidence=no result frame"


@pytest.fixture
def clock() -> Clock:
    return Clock(5_000.0)


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> TaskStore:
    yield from open_task_store(tmp_path, clock)


def _admission(store: TaskStore, clock: Clock, *, cap: int = 1) -> r.RunnerAdmission:
    async def _sleep(secs: float) -> None:
        clock.advance(secs)
        await asyncio.sleep(0)

    return r.RunnerAdmission(
        store,
        lane=r.RunnerLane(cap),
        clock=clock,
        sleep=_sleep,
        ladder=RecoveryLadder(clock=clock, rng=random.Random(7)),
    )


def _provider(script) -> MagicMock:
    """``script(call_no, message)`` yields the events of one turn (or raises)."""
    provider = MagicMock()
    calls: list[str] = []
    active = {"n": 0, "peak": 0}

    async def _stream(message: str):
        calls.append(message)
        active["n"] += 1
        active["peak"] = max(active["peak"], active["n"])
        try:
            await asyncio.sleep(0)
            for ev in script(len(calls), message):
                yield ev
        finally:
            active["n"] -= 1

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    provider.calls = calls
    provider.active = active
    return provider


def _done(text: str = "whole") -> list[LLMEvent]:
    return [
        LLMEvent(kind="text_chunk", text=text),
        LLMEvent(kind="complete", stop_reason=STOP_REASON_END_TURN),
    ]


def _stalled(text: str = "half") -> list[LLMEvent]:
    return [
        LLMEvent(kind="text_chunk", text=text),
        LLMEvent(kind="complete", stop_reason=STOP_REASON_TOOL_STALL, text=_STALL_EVIDENCE),
    ]


@pytest.fixture(autouse=True)
def _no_self_review(monkeypatch):
    # The post-step self-review is its own turn; these tests count STEP turns.
    monkeypatch.setattr("kiro_crew.task_executor.self_review", AsyncMock(return_value=True))


def _runner(tmp_path: Path, provider: MagicMock, admission: r.RunnerAdmission) -> TaskRunner:
    sessions = _make_mock_sessions()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
    runner.attach_task_admission(admission)
    return runner


def _run(tmp_path: Path, *steps: Step, task_id: str = "r1") -> TaskRun:
    run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    run.task_id = task_id
    run.work_dir = str(tmp_path)
    run.tasks = list(steps)
    return run


def _kinds(store: TaskStore, task_id: str) -> list[str]:
    return [e.kind for e in store.events(task_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "persistent", "incognito", "temporary"])
async def test_taskq_retains_only_persistent_run_and_step_bodies(
    tmp_path: Path, store: TaskStore, clock: Clock, mode
) -> None:
    admission = _admission(store, clock)
    provider = _provider(lambda n, msg: _done("session result sentinel"))
    runner = _runner(tmp_path, provider, admission)
    await runner.adopt_task_rows()
    step = Step(index=1, title="session step title sentinel", description="session instructions")
    run = _run(tmp_path, step)
    run.name = "session task name sentinel"
    if mode is not None:
        run.execution_context = ExecutionContext(
            None, MemoryStoreRef("default"), "template", "kirocrew", mode
        )
    runner._runs[run.task_id] = run

    await runner._taskq_begin_run(run)
    assert await runner._execute_single_task(run, step)
    run.status = "completed"
    await runner._taskq_end_run(run)
    assert provider.calls and step.status == StepStatus.PASSED
    assert admission.lane.running == 0
    assert not runner._run_handles

    persistent = mode in (None, "persistent")
    assert store.count(kind=m.KIND_TASKRUNNER_STEP) == (2 if persistent else 0)
    for row_id in ("taskrunner:r1", "taskrunner:r1:task1"):
        if persistent:
            assert store.get(row_id).state == m.DONE
            assert "claimed" in _kinds(store, row_id)
        else:
            assert store.get(row_id) is None
            assert store.events(row_id) == []

    path = store.path
    store.close()
    reopened = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    try:
        assert reopened.count(kind=m.KIND_TASKRUNNER_STEP) == (2 if persistent else 0)
        report = r.adopt_orphaned_rows(reopened, kinds=(m.KIND_TASKRUNNER_STEP,))
        assert report.examined == 0 and not report.resumed
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_and_persistent_steps_share_the_live_lane(
    tmp_path: Path, store: TaskStore, clock: Clock, mode
) -> None:
    admission = _admission(store, clock, cap=1)
    provider = _provider(lambda n, msg: _done())
    runner = _runner(tmp_path, provider, admission)
    await runner.adopt_task_rows()
    persistent_step = Step(index=1, title="ordinary", description="ordinary instructions")
    restricted_step = Step(index=1, title="restricted", description="restricted instructions")
    persistent = _run(tmp_path, persistent_step, task_id="persistent")
    restricted = _run(tmp_path, restricted_step, task_id="restricted")
    restricted.execution_context = ExecutionContext(
        None, MemoryStoreRef("default"), "template", "kirocrew", mode
    )
    await runner._taskq_begin_run(persistent)
    await runner._taskq_begin_run(restricted)
    assert await asyncio.gather(
        runner._execute_single_task(persistent, persistent_step),
        runner._execute_single_task(restricted, restricted_step),
    ) == [True, True]
    persistent.status = restricted.status = "completed"
    await runner._taskq_end_run(persistent)
    await runner._taskq_end_run(restricted)
    assert len(provider.calls) == 2 and provider.active["peak"] == 1
    assert admission.lane.running == admission.lane.waiting == 0
    assert store.count(kind=m.KIND_TASKRUNNER_STEP) == 2
    assert store.get("taskrunner:persistent").state == m.DONE
    assert store.get("taskrunner:persistent:task1").state == m.DONE
    assert store.get("taskrunner:restricted") is None
    assert store.get("taskrunner:restricted:task1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_step_cancelled_before_lane_admission_leaves_no_row(
    tmp_path: Path, store: TaskStore, clock: Clock, mode
) -> None:
    admission = _admission(store, clock, cap=1)
    provider = _provider(lambda n, msg: _done())
    runner = _runner(tmp_path, provider, admission)
    await runner.adopt_task_rows()
    step = Step(index=1, title="restricted title", description="restricted instructions")
    run = _run(tmp_path, step)
    run.execution_context = ExecutionContext(
        None, MemoryStoreRef("default"), "template", "kirocrew", mode
    )
    await runner._taskq_begin_run(run)
    await admission.lane.acquire("another-step")
    pending = asyncio.create_task(runner._execute_single_task(run, step))
    try:
        for _ in range(20):
            await settle_store_writes(store)
            if admission.lane.waiting == 1:
                break
        assert admission.lane.waiting == 1
        assert not provider.calls
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        admission.lane.release("another-step")
        run.status = "cancelled"
        await runner._taskq_end_run(run)
    assert admission.lane.running == admission.lane.waiting == 0
    assert not runner._run_handles
    assert store.count(kind=m.KIND_TASKRUNNER_STEP) == 0
    assert not store.events("taskrunner:r1")
    assert not store.events("taskrunner:r1:task1")


# ── 1. two steps through admission under cap 1 ──────────────────────────────


@pytest.mark.asyncio
async def test_two_steps_under_cap_one_serialize_and_are_claimed_once(
    tmp_path: Path, store: TaskStore, clock: Clock
) -> None:
    adm = _admission(store, clock, cap=1)
    provider = _provider(lambda n, msg: _done(f"out{n}"))
    runner = _runner(tmp_path, provider, adm)
    runner._run_session_keys["r1"] = "chat:alice"
    s1, s2 = Step(index=1, title="one", description="d1"), Step(
        index=2, title="two", description="d2"
    )
    run = _run(tmp_path, s1, s2)
    await runner._taskq_begin_run(run)

    results = await asyncio.gather(
        runner._execute_single_task(run, s1), runner._execute_single_task(run, s2)
    )

    assert results == [True, True]
    assert provider.active["peak"] == 1  # never two turns at once under cap 1
    assert [s.status for s in (s1, s2)] == [StepStatus.PASSED, StepStatus.PASSED]
    run_row = store.get("taskrunner:r1")
    assert run_row is not None and run_row.state == m.RUNNING
    assert run_row.params[r.PARAM_LANE] == "chat:alice"
    for idx in (1, 2):
        row = store.get(f"taskrunner:r1:task{idx}")
        assert row.state == m.DONE and row.parent_id == "taskrunner:r1"
        assert row.root_id == "taskrunner:r1" and row.params[r.PARAM_LANE] == "chat:alice"
        assert _kinds(store, row.id).count("claimed") == 1
    assert adm.lane.running == 0
    run.status = "completed"
    await runner._taskq_end_run(run)
    assert store.get("taskrunner:r1").state == m.DONE


@pytest.mark.asyncio
async def test_cron_launched_run_queues_in_the_system_lane(
    tmp_path: Path, store: TaskStore, clock: Clock
) -> None:
    adm = _admission(store, clock, cap=2)
    provider = _provider(lambda n, msg: _done())
    runner = _runner(tmp_path, provider, adm)
    s1 = Step(index=1, title="one", description="d1")
    run = _run(tmp_path, s1)
    run.source = "cron"
    runner._run_session_keys["r1"] = "chat:alice"  # a session exists, but cron wins
    await runner._taskq_begin_run(run)
    assert await runner._execute_single_task(run, s1) is True
    assert store.get("taskrunner:r1").params[r.PARAM_LANE] == r.LANE_SYSTEM
    assert store.get("taskrunner:r1:task1").params[r.PARAM_LANE] == r.LANE_SYSTEM


@pytest.mark.asyncio
async def test_a_step_cancelled_while_waiting_for_a_slot_leaves_no_queued_row(
    tmp_path: Path, store: TaskStore, clock: Clock
) -> None:
    """``TaskRunner.cancel`` / ``pause`` / the global timeout cancel the run
    task, and the cancel can land while a step waits for its lane slot. The row
    is already accepted and ``taskrunner_step`` has no dispatcher, so nothing
    would ever claim it again: the admission ends it."""
    adm = _admission(store, clock, cap=1)
    runner = _runner(tmp_path, _provider(lambda n, msg: _done()), adm)
    step = Step(index=1, title="parked", description="d")
    run = _run(tmp_path, step)
    runner._runs["r1"] = run
    await adm.lane.acquire("another-step")  # the only slot is taken

    parked = asyncio.create_task(runner._execute_single_task(run, step))
    for _ in range(20):
        await settle_store_writes(store)
        if adm.lane.waiting == 1:
            break
    assert store.state_of("taskrunner:r1:task1") == m.QUEUED

    parked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked

    row = store.get("taskrunner:r1:task1")
    assert row.state == m.CANCELLED and row.lease_owner is None
    assert store.oldest_wait_secs(kind=m.KIND_TASKRUNNER_STEP) == 0.0
    adm.lane.release()
    assert adm.lane.running == 0 and adm.lane.waiting == 0


@pytest.mark.asyncio
async def test_adoption_ends_the_step_rows_a_dead_incarnation_left(
    tmp_path: Path, clock: Clock
) -> None:
    """Crash after accept (``queued``) and crash after admit (``running``) on the
    runner path, in one boot: every row ends with an explainable terminal state
    and a repeat sweep settles nothing twice."""
    path = tmp_path / "tasks.db"
    store_a = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    adm_a = _admission(store_a, clock, cap=1)
    run_row = adm_a.accept(
        kind=m.KIND_TASKRUNNER_STEP,
        task_id="taskrunner:r7",
        params={"task_id": "r7", r.PARAM_SAFE_RETRY: True},
    )
    step1 = adm_a.accept(
        kind=m.KIND_TASKRUNNER_STEP,
        task_id="taskrunner:r7:task1",
        params={"task_id": "r7", "index": 1, r.PARAM_SAFE_RETRY: True},
        parent_id=run_row.id,
    )
    step2 = adm_a.accept(
        kind=m.KIND_TASKRUNNER_STEP,
        task_id="taskrunner:r7:task2",
        params={"task_id": "r7", "index": 2, r.PARAM_SAFE_RETRY: True},
        parent_id=run_row.id,
    )
    handle = await adm_a.admit(step1.id)
    assert handle.running()
    assert [store_a.state_of(x.id) for x in (run_row, step2)] == [m.QUEUED, m.QUEUED]
    store_a.close()  # -- crash

    store_b = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    try:
        runner = _runner(tmp_path, _provider(lambda n, msg: _done()), _admission(store_b, clock))

        report = await runner.adopt_task_rows()

        assert sorted(report.cancelled) == [run_row.id, step2.id]
        assert report.failed == [step1.id]  # re-run from the checkpoint on resume
        assert store_b.count_by_state() == {m.CANCELLED: 2, m.FAILED: 1}
        assert store_b.oldest_wait_secs() == 0.0
        again = await runner.adopt_task_rows()
        assert again.examined == 0 and again.cancelled == []
    finally:
        store_b.close()


# ── 2. restart mid-step -> resume from the checkpoint, not a re-run ─────────


@pytest.mark.asyncio
async def test_restart_mid_step_resumes_from_checkpoint(
    tmp_path: Path, store: TaskStore, clock: Clock, monkeypatch
) -> None:
    # A previous incarnation: run row + step rows, PASSED step 1, step 2 cut off.
    dead = "dead-incarnation"
    for row_id, parent in (("taskrunner:r1", None), ("taskrunner:r1:task2", "taskrunner:r1")):
        store.accept_one(
            m.TaskRecord(
                id=row_id,
                kind=m.KIND_TASKRUNNER_STEP,
                parent_id=parent,
                params={"task_id": "r1", r.PARAM_SAFE_RETRY: True},
            )
        )
        store.claim(row_id, owner=dead)
        store.transition(row_id, m.STARTING)
        store.transition(row_id, m.RUNNING)
    store.accept_one(
        m.TaskRecord(
            id="taskrunner:r1:task1", kind=m.KIND_TASKRUNNER_STEP, parent_id="taskrunner:r1"
        )
    )
    store.claim("taskrunner:r1:task1", owner=dead)
    store.finish("taskrunner:r1:task1", m.DONE)

    adm = _admission(store, clock, cap=1)
    provider = _provider(lambda n, msg: _done("resumed"))
    runner = _runner(tmp_path, provider, adm)
    s1 = Step(index=1, title="one", description="d1", status=StepStatus.PASSED, result="first")
    s2 = Step(index=2, title="two", description="d2")
    run = _run(tmp_path, s1, s2)
    run.status = "paused"  # what _load_runs assigns to a run cut off by a crash
    run.branch_name = "kirocrew/r1"  # git-coordinated: the checkpoint is real
    runner._runs["r1"] = run
    monkeypatch.setattr(runner, "_ensure_resumable_workspace", AsyncMock(return_value=True))
    monkeypatch.setattr("kiro_crew.taskrunner.git_coord.finalize", AsyncMock())
    monkeypatch.setattr(runner, "_apersist_runs", AsyncMock())

    report = await runner.adopt_task_rows()  # joins the sweep attach started

    assert report is not None and report.resumed == ["taskrunner:r1"]
    assert report.failed == ["taskrunner:r1:task2"]  # the cut-off step, re-run below
    await runner._tasks["r1"]
    assert run.status == "completed"
    assert s1.status == StepStatus.PASSED and s1.result == "first"  # the checkpoint held
    assert provider.calls and len(provider.calls) == 1  # only step 2 ran
    assert "two" in provider.calls[0]
    assert store.get("taskrunner:r1").state == m.DONE
    assert store.get("taskrunner:r1:task2").state == m.FAILED  # the interrupted attempt
    rerun = store.get("taskrunner:r1:task2~2")
    assert rerun is not None and rerun.state == m.DONE and rerun.parent_id == "taskrunner:r1"


@pytest.mark.asyncio
async def test_restart_without_a_checkpoint_is_not_re_run_blind(
    tmp_path: Path, store: TaskStore, clock: Clock
) -> None:
    store.accept_one(
        m.TaskRecord(
            id="taskrunner:r9",
            kind=m.KIND_TASKRUNNER_STEP,
            params={"task_id": "r9", r.PARAM_SAFE_RETRY: False},
        )
    )
    store.claim("taskrunner:r9", owner="dead")
    store.transition("taskrunner:r9", m.STARTING)
    store.transition("taskrunner:r9", m.RUNNING)
    adm = _admission(store, clock)
    notices: list[str] = []

    async def _notify(title, body, *args, **kw):
        notices.append(title)

    runner = _runner(tmp_path, _provider(lambda n, msg: _done()), adm)
    runner._on_notify = _notify
    run = _run(tmp_path, Step(index=1, title="x", description="d"), task_id="r9")
    run.status = "paused"
    runner._runs["r9"] = run

    report = await runner.adopt_task_rows()

    assert report.unknown_side_effect == ["taskrunner:r9"]
    assert run.status == "paused" and "r9" not in runner._tasks
    assert any("not auto-resumed" in n for n in notices)


@pytest.mark.asyncio
async def test_attach_sweeps_once_and_only_over_a_store_that_already_exists(
    tmp_path: Path, store: TaskStore, clock: Clock, monkeypatch
) -> None:
    """The gateway attaches at socket-bind time, when the manager's store may
    still be opening off the loop, and again once it is attached.

    The store getter is LIVE, so a sweep armed on the first attach would run
    over rows it could not see when it was armed -- beside the one the second
    attach arms. Two concurrent sweeps over one set of rows can settle a row the
    other has already handed back to its run.
    """
    store.accept_one(
        m.TaskRecord(
            id="taskrunner:r4",
            kind=m.KIND_TASKRUNNER_STEP,
            params={"task_id": "r4", r.PARAM_SAFE_RETRY: True},
        )
    )
    store.claim("taskrunner:r4", owner="dead")
    store.transition("taskrunner:r4", m.STARTING)
    store.transition("taskrunner:r4", m.RUNNING)

    opening: dict[str, TaskStore | None] = {"store": None}
    adm = r.RunnerAdmission(lambda: opening["store"], lane=r.RunnerLane(1), clock=clock)
    sweeps: list[str] = []
    real_sweep = r.adopt_orphaned_rows

    def _counted(*args, **kw):
        sweeps.append(str(kw.get("kinds")))
        return real_sweep(*args, **kw)

    monkeypatch.setattr(r, "adopt_orphaned_rows", _counted)

    runner = _runner(tmp_path, _provider(lambda n, msg: _done()), adm)
    assert runner.task_admission is adm  # the typed refusal is already in place
    assert runner._adopt_inflight is None and sweeps == []

    opening["store"] = store  # the open worker attached it
    runner.attach_task_admission(adm)
    await runner._adopt_inflight

    assert len(sweeps) == 1
    assert store.get("taskrunner:r4").state == m.FAILED  # no run to resume into


# ── 3. stall -> recovering -> bounded failed with the partial kept ──────────


@pytest.mark.asyncio
async def test_persistent_stall_recovers_through_the_ladder_then_fails_with_partial(
    tmp_path: Path, store: TaskStore, clock: Clock, monkeypatch
) -> None:
    delays: list[float] = []

    async def _fake_delay(secs: float) -> None:
        delays.append(secs)
        clock.advance(secs)
        await asyncio.sleep(0)

    monkeypatch.setattr("kiro_crew.task_executor._recovery_delay", _fake_delay)
    adm = _admission(store, clock)
    provider = _provider(lambda n, msg: _stalled())
    runner = _runner(tmp_path, provider, adm)
    step = Step(index=1, title="Test step", description="desc")
    run = _run(tmp_path, step)

    success = await runner._execute_single_task(run, step)

    assert success is False and step.status == StepStatus.FAILED
    assert step.result == "half"  # the partial is kept
    assert "partial result preserved" in step.error and "stalled" in step.error
    # L3 allows one re-run, then the layer is exhausted: two turns, one delay.
    assert len(provider.calls) == 2 and len(delays) == 1 and delays[0] > 0
    assert "retry attempt" in provider.calls[1] and STOP_REASON_TOOL_STALL in provider.calls[1]
    row = store.get("taskrunner:r1:task1")
    assert row.state == m.FAILED
    transitions = [
        (e.data.get("from"), e.data.get("to"))
        for e in store.events(row.id)
        if e.kind == "transition"
    ]
    assert (m.RUNNING, m.RECOVERING) in transitions  # the wait between turns
    assert transitions[-1][1] == m.FAILED
    assert row.generation == 2  # the re-run was a fresh claim
    assert adm.lane.running == 0


@pytest.mark.asyncio
async def test_stall_then_success_passes_after_one_recovery(
    tmp_path: Path, store: TaskStore, clock: Clock, monkeypatch
) -> None:
    monkeypatch.setattr("kiro_crew.task_executor._recovery_delay", AsyncMock())
    adm = _admission(store, clock)
    provider = _provider(lambda n, msg: _stalled() if n == 1 else _done())
    runner = _runner(tmp_path, provider, adm)
    step = Step(index=1, title="Test step", description="desc")
    run = _run(tmp_path, step)
    assert await runner._execute_single_task(run, step) is True
    assert step.status == StepStatus.PASSED and step.result == "whole"
    assert store.get("taskrunner:r1:task1").state == m.DONE


# ── 4. 429 -> waiting_dependency -> wake -> PASSED ──────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_parks_the_step_and_the_wake_resumes_it(
    tmp_path: Path, store: TaskStore, clock: Clock
) -> None:
    adm = _admission(store, clock)
    raised = asyncio.Event()

    def _script(n: int, msg: str):
        if n == 1:
            exc = RuntimeError("HTTP 429 Too Many Requests")
            setattr(
                exc,
                SIGNAL_ATTR,
                DependencySignal(
                    kind=KIND_RATE_LIMITED,
                    dependency_scope="github:api",
                    source="test",
                    retry_at=clock.t + 30,
                ),
            )
            raised.set()  # the 429 is leaving the turn: the park follows it
            raise exc
        return _done()

    provider = _provider(_script)
    runner = _runner(tmp_path, provider, adm)
    step = Step(index=1, title="Test step", description="desc")
    run = _run(tmp_path, step)

    running = asyncio.create_task(runner._execute_single_task(run, step))
    await settle_dependency_park(adm, store, raised)
    row = store.get("taskrunner:r1:task1")
    assert row.state == m.WAITING_DEPENDENCY and row.wait["dependency_scope"] == "github:api"
    assert adm.lane.running == 0  # the slot is released for the wait
    assert step.attempts == 1 and not running.done()
    clock.advance(31)
    assert adm.tick() == ["taskrunner:r1:task1"]

    assert await running is True
    assert step.status == StepStatus.PASSED and len(provider.calls) == 2
    assert step.attempts == 1  # the wait did not burn an attempt
    assert store.get("taskrunner:r1:task1").state == m.DONE
    assert adm.lane.running == 0


# ── the run cap does not refuse once the lane meters the steps ──────────────


@pytest.mark.asyncio
async def test_run_cap_defers_to_the_lane_when_admission_is_attached(
    tmp_path: Path, store: TaskStore, clock: Clock
) -> None:
    adm = _admission(store, clock)
    runner = _runner(tmp_path, _provider(lambda n, msg: _done()), adm)
    never = asyncio.get_running_loop().create_future()
    for i in range(3):
        runner._tasks[f"busy{i}"] = asyncio.ensure_future(never)
    run = _run(tmp_path, Step(index=1, title="x", description="d"), task_id="r5")
    run.status = "planned"
    runner._runs["r5"] = run
    runner._apersist_runs = AsyncMock()  # type: ignore[method-assign]
    runner._workflow_rebind = AsyncMock()  # type: ignore[method-assign]
    try:
        # Legacy: refused. Attached: accepted (the steps queue in the lane).
        runner._task_admission = None
        with pytest.raises(ValueError, match="Too many concurrent tasks"):
            await runner.execute_plan("r5")
        runner._task_admission = adm
        assert await runner.execute_plan("r5") == "r5"
        runner._tasks["r5"].cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner._tasks["r5"]
    finally:
        never.cancel()
        for i in range(3):
            runner._tasks.pop(f"busy{i}", None)


@pytest.mark.asyncio
async def test_run_and_step_admission_stay_off_the_loop(
    tmp_path: Path, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's own admission writes under the STRICT on-loop guard.

    Covers the container row (``accept`` + ``claim_only`` + ``running``), one
    step's ``accept`` + ``admit``, and both terminal writes. The step BODY
    (``execute_single_task``) makes handle writes of its own and is deliberately
    outside this pin -- what is pinned here is the admission boundary the runner
    owns.
    """
    from kiro_crew.taskq import store as store_mod

    adm = _admission(store, clock, cap=1)
    provider = _provider(lambda n, msg: _done())
    runner = _runner(tmp_path, provider, adm)
    runner._run_session_keys["r1"] = "chat:alice"
    step = Step(index=1, title="one", description="d1")
    run = _run(tmp_path, step)

    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    try:
        await runner._taskq_begin_run(run)
        handle = await runner._taskq_admit_step(run, step)
        assert handle is not None
        assert await handle.running_async({"index": 1}) is True
        assert await handle.done_async() is True
        run.status = "completed"
        await runner._taskq_end_run(run)
    finally:
        monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)

    assert store.loop_thread_calls == before  # the reads below are the test's own
    assert store.get("taskrunner:r1").state == m.DONE
    assert store.get("taskrunner:r1:task1").state == m.DONE
    assert _kinds(store, "taskrunner:r1:task1").count("claimed") == 1


@pytest.mark.asyncio
async def test_the_step_body_keeps_its_store_writes_off_the_loop(
    tmp_path: Path, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step BODY under the STRICT guard -- what the pin above excludes.

    ``execute_task`` is a coroutine on the gateway loop and makes four store
    touches of its own: the recovery write, the ``running`` re-entry after the
    reclaim, the persisted-answer read at the top, and the ``input_consumed``
    append once the step has durably passed. Armed, any one of them left on the
    loop raises instead of shipping.
    """
    from kiro_crew.taskq import store as store_mod

    monkeypatch.setattr("kiro_crew.task_executor._recovery_delay", AsyncMock())
    adm = _admission(store, clock)
    # A persisted, not-yet-consumed operator answer on the row the step re-adopts.
    seeded = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r1:task1")
    assert seeded is not None
    store.append_event(seeded.id, "wake", {"answer": "use the v2 endpoint"})
    provider = _provider(lambda n, msg: _stalled() if n == 1 else _done())
    runner = _runner(tmp_path, provider, adm)
    step = Step(index=1, title="one", description="desc")
    run = _run(tmp_path, step)

    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    try:
        assert await runner._execute_single_task(run, step) is True
    finally:
        monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)

    assert store.loop_thread_calls == before  # the reads below are the test's own
    assert step.status == StepStatus.PASSED
    # Each of the four writes actually landed, off-loop.
    assert "use the v2 endpoint" in step.description  # the answer was read back
    assert "input_consumed" in _kinds(store, "taskrunner:r1:task1")
    transitions = [
        (e.data.get("from"), e.data.get("to"))
        for e in store.events("taskrunner:r1:task1")
        if e.kind == "transition"
    ]
    assert (m.RUNNING, m.RECOVERING) in transitions  # the recovery write
    assert (m.STARTING, m.RUNNING) in transitions  # the re-entry after the reclaim
    assert transitions[-1][1] == m.DONE


@pytest.mark.asyncio
async def test_a_dependency_wake_writes_running_off_the_loop(
    tmp_path: Path, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step body's whole RESUMPTION half, under the strict guard.

    The re-admission read, the fresh claim, the ``running`` re-publish and the
    step's terminal write all run while the guard is armed. The body's own
    ``running`` call on this path is a no-op -- ``_resume_after_wait`` has
    already published RUNNING by the time it is reached -- so it is the ONE of
    the five step-body sites the counter cannot catch on its own; it takes the
    awaited seam for consistency with the other four.

    The guard is armed only around the resumption, because the wake itself is
    driven here by ``adm.tick()`` -- the test's own synchronous store I/O, which
    in production runs through the monitoring loop's off-loop tick.
    """
    from kiro_crew.taskq import store as store_mod

    adm = _admission(store, clock)
    raised = asyncio.Event()

    def _script(n: int, msg: str):
        if n == 1:
            exc = RuntimeError("HTTP 429 Too Many Requests")
            setattr(
                exc,
                SIGNAL_ATTR,
                DependencySignal(
                    kind=KIND_RATE_LIMITED,
                    dependency_scope="github:api",
                    source="test",
                    retry_at=clock.t + 30,
                ),
            )
            raised.set()  # the 429 is leaving the turn: the park follows it
            raise exc
        return _done()

    provider = _provider(_script)
    runner = _runner(tmp_path, provider, adm)
    step = Step(index=1, title="one", description="d1")
    run = _run(tmp_path, step)

    running = asyncio.create_task(runner._execute_single_task(run, step))
    await settle_dependency_park(adm, store, raised)
    assert store.get("taskrunner:r1:task1").state == m.WAITING_DEPENDENCY
    clock.advance(31)
    assert adm.tick() == ["taskrunner:r1:task1"]

    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    try:
        assert await running is True
    finally:
        monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)

    assert store.loop_thread_calls == before  # the reads below are the test's own
    assert step.status == StepStatus.PASSED and step.attempts == 1
    assert store.get("taskrunner:r1:task1").state == m.DONE


# ── 8. the step's ``running`` mark is the fence, not a notification ──────────


class _LockedOnRunning(TaskStore):
    """A REAL store whose FILE another connection locks for the ``running`` write
    only, so that ONE transition gets SQLite's own ``database is locked``.

    The lock clears with the refusal: what the caller does next is a decision
    about the REFUSAL, not about an outage that is still going on.

    ``lock_only_id`` narrows the lock to one row, so a test can refuse the run
    container's mark while the steps under it take theirs normally; unset, every
    ``running`` write is locked.
    """

    lock_only_id: str | None = None

    def transition(self, task_id, state, **kw):  # type: ignore[no-untyped-def]
        if state != m.RUNNING or (self.lock_only_id is not None and task_id != self.lock_only_id):
            return super().transition(task_id, state, **kw)
        # ``transition`` runs on the store's writer thread and ``close`` on the
        # test's, so the blocker is pinned to neither.
        blocker = sqlite3.connect(str(self.path), timeout=0, check_same_thread=False)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            return super().transition(task_id, state, **kw)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()


@pytest.mark.asyncio
async def test_a_step_whose_running_mark_is_refused_never_runs_its_turn(
    tmp_path: Path, clock: Clock
) -> None:
    """``starting`` reaches no WAITING state and may belong to a newer owner, so a
    refused ``starting -> running`` ends the step before its turn instead of
    executing under a row the store did not move."""
    store = _LockedOnRunning(
        tmp_path / "tasks.db", clock=clock, network_fs=False, busy_timeout_secs=0.05
    ).open()
    try:
        adm = _admission(store, clock)
        provider = _provider(lambda n, msg: _done())
        runner = _runner(tmp_path, provider, adm)
        step = Step(index=1, title="one", description="d1")
        run = _run(tmp_path, step)

        assert await runner._execute_single_task(run, step) is False
        assert provider.calls == [], "the turn ran under a row the store left starting"
        assert step.status == StepStatus.FAILED and "running mark" in step.error
        # Explicitly terminal, never a live-looking row nothing is executing.
        assert store.get("taskrunner:r1:task1").state == m.FAILED
        assert adm.lane.running == 0
    finally:
        store.close()


# ── 8b. the run CONTAINER row's mark: the row's state decides, not the refusal ─


class _ReconciledUnderTheRunMark(TaskStore):
    """A REAL store that lands the boot reconciler's OWN write on the container row
    in the window between ``claim_only``'s ``starting`` and the ``running`` mark.

    ``reconcile._settle_one`` writes ``unknown_side_effect`` UNFENCED (no
    generation) for an active row of unknown side-effect class that this
    incarnation does not lease, which is what a second gateway booting beside a
    live run does to that run's container row. Placed here, the mark that follows
    is a ``rejected_transition`` off a terminal row.
    """

    def transition(self, task_id, state, **kw):  # type: ignore[no-untyped-def]
        if task_id == "taskrunner:r1" and state == m.RUNNING:
            super().transition(task_id, m.UNKNOWN_SIDE_EFFECT, detail={"reconciled": "lost_owner"})
        return super().transition(task_id, state, **kw)


@pytest.mark.asyncio
async def test_a_run_whose_container_row_already_ended_does_not_start(
    tmp_path: Path, clock: Clock
) -> None:
    """A plan never executes under a container row that already carries an outcome.

    The refusal alone does not say that: the same False covers a store that could
    not take the write. So the state is read, and only a TERMINAL one ends the
    start -- with no handle kept, so the run's exit path writes nothing over the
    outcome another incarnation recorded.
    """
    store = _ReconciledUnderTheRunMark(
        tmp_path / "tasks.db", window=8, clock=clock, network_fs=False
    ).open()
    try:
        adm = _admission(store, clock)
        provider = _provider(lambda n, msg: _done())
        runner = _runner(tmp_path, provider, adm)
        run = _run(tmp_path, Step(index=1, title="one", description="d1"))

        with pytest.raises(r.RunnerTaskCancelled) as caught:
            await runner._taskq_begin_run(run)

        assert m.UNKNOWN_SIDE_EFFECT in str(caught.value)
        assert "r1" not in runner._run_handles, "a handle on an ended row was kept"
        assert store.get("taskrunner:r1").state == m.UNKNOWN_SIDE_EFFECT
        # The exit path has no handle, so it never writes over the outcome.
        run.status = "completed"
        await runner._taskq_end_run(run)
        assert store.get("taskrunner:r1").state == m.UNKNOWN_SIDE_EFFECT
        assert _kinds(store, "taskrunner:r1").count("rejected_transition") == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_lost_run_row_mark_keeps_the_run_and_still_settles_the_row(
    tmp_path: Path, clock: Clock, caplog
) -> None:
    """A container mark the STORE could not take never refuses an accepted run.

    The row holds no lane slot and enters no wait, and ``TRANSITIONS[STARTING]``
    carries every active terminal, so the run's own terminal write commits from
    ``starting``: what the lost mark costs is the progress marker, and refusing
    the run over it would end work the queue exists to keep. The warning is the
    record that the row is live under a mark that did not commit.
    """
    store = _LockedOnRunning(
        tmp_path / "tasks.db", window=8, clock=clock, network_fs=False, busy_timeout_secs=0.05
    ).open()
    store.lock_only_id = "taskrunner:r1"
    try:
        adm = _admission(store, clock)
        provider = _provider(lambda n, msg: _done())
        runner = _runner(tmp_path, provider, adm)
        step = Step(index=1, title="one", description="d1")
        run = _run(tmp_path, step)

        with caplog.at_level("WARNING", logger="kiro_crew.taskrunner"):
            await runner._taskq_begin_run(run)

        assert "r1" in runner._run_handles
        assert store.state_of("taskrunner:r1") == m.STARTING
        assert any("did not take the running mark" in rec.message for rec in caplog.records)

        assert await runner._execute_single_task(run, step) is True
        assert store.state_of("taskrunner:r1:task1") == m.DONE
        run.status = "completed"
        await runner._taskq_end_run(run)
        assert store.state_of("taskrunner:r1") == m.DONE
    finally:
        store.close()


# ── 9. a refused admission is a failed STEP, in both branches ────────────────


class _LockedOnStarting(TaskStore):
    """A REAL store whose FILE another connection locks for the ``starting``
    write only, so ``admit``'s fenced start write refuses.

    The lock clears with the refusal, so everything the refusal path does next
    (``_requeue_unstarted``, the terminal write) sees a working store: what is
    being pinned is the caller's answer to a REFUSAL, not an ongoing outage.
    """

    def transition(self, task_id, state, **kw):  # type: ignore[no-untyped-def]
        if state != m.STARTING:
            return super().transition(task_id, state, **kw)
        blocker = sqlite3.connect(str(self.path), timeout=0, check_same_thread=False)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            return super().transition(task_id, state, **kw)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()


@pytest.mark.parametrize("steps", [1, 2], ids=["sequential", "parallel"])
@pytest.mark.asyncio
async def test_a_refused_admission_fails_the_step_and_reaches_replan(
    tmp_path: Path, clock: Clock, steps: int
) -> None:
    """A refused admission ends the STEP and the run gets the runner's answer.

    ``_try_replan`` is what a step that did not land is worth to a run, and it
    only runs for a step that came back False. A refusal that propagates instead
    unwinds the whole accepted run past it -- and the two branches of
    ``_execute_tasks`` must not differ on that: one step is the SEQUENTIAL arm,
    two independent steps are one parallel group, where
    ``gather(return_exceptions=True)`` already absorbs the same raise.
    """
    store = _LockedOnStarting(
        tmp_path / "tasks.db", clock=clock, network_fs=False, busy_timeout_secs=0.05
    ).open()
    try:
        adm = _admission(store, clock, cap=2)
        provider = _provider(lambda n, msg: _done())
        runner = _runner(tmp_path, provider, adm)
        plan = [Step(index=i, title=f"s{i}", description=f"d{i}") for i in range(1, steps + 1)]
        run = _run(tmp_path, *plan)
        replanned: list[int] = []

        async def _replan(_run_arg, task):  # type: ignore[no-untyped-def]
            replanned.append(task.index)
            return False

        runner._try_replan = _replan  # type: ignore[assignment]

        await runner._execute_tasks(run, "hist")  # must not raise

        assert replanned == [1], replanned
        assert provider.calls == [], "a step whose row never started ran its turn"
        assert plan[0].status == StepStatus.FAILED
        assert "refused the step" in (plan[0].error or ""), plan[0].error
        assert run.status == "failed"
        assert run.error and "Task 1 failed" in run.error and "refused" in run.error
        # The row is queued for a later dispatch, never left mid-start.
        assert store.get("taskrunner:r1:task1").state == m.QUEUED
        assert adm.lane.running == 0
    finally:
        store.close()
