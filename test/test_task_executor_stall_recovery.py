"""``execute_task``'s stalled-turn arm, over a REAL store, adapter and ladder.

Two properties, one per defect this file exists for:

* a stall the recovery ladder APPROVED is not charged to the logic-retry
  budget. A logic retry is a fresh attempt at work that was attempted and came
  back wrong; a recoverable stall is the SAME attempt, not finished being
  attempted once, and the ladder is what bounds it. Sharing one counter let two
  ordinary failures spend the budget a later stall needed;
* the ``recovering`` write is the PRECONDITION for the wait-and-reclaim, so a
  refused one refuses the recovery. Reclaiming a row the store still calls
  ``running`` raises out of the step and takes the accepted run with it.

The in-place ceiling is also pinned, because excluding a retry from one budget
without naming the budget that holds it is how a stall becomes a hang: the
ladder's L3 count is per-unit and DECAYS after its cooldown, so
``STOP_RECOVERY_MAX_RETRIES`` -- the same in-place budget the chat slot's
``_tool_stall_retries`` and the sub-agent's ``_stop_recovery_used`` spend -- is
what bounds one step's re-runs whatever the ladder forgets.

Fake clock, a real SQLite store in ``tmp_path``, no kiro-cli: the only waits are
the ladder's, and the recovery seam advances the clock instead of sleeping.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import Clock

from kiro_crew import task_executor
from kiro_crew.acp.types import (
    STOP_REASON_END_TURN,
    STOP_REASON_TOOL_STALL,
    STOP_RECOVERY_MAX_RETRIES,
    TurnUsage,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.recovery.ladder import L3_ACP_RUNTIME, RecoveryLadder
from kiro_crew.task_models import MAX_RETRIES, SESSION_PREFIX, Project, Task, TaskStatus
from kiro_crew.taskq import model as m
from kiro_crew.taskq.adapters import runner as r
from kiro_crew.taskq.store import TaskStore

_RUN_TASK_ID = "task-stall"
_SESSION_KEY = f"{SESSION_PREFIX}:{_RUN_TASK_ID}:task1"
_ROW_ID = "taskrunner:stall:task1"
#: One ordinary retryable failure per spelling, so the repeated-error loop
#: detector never fires and the budget under test is the only one spending.
_PIPE_DIED = "error: pipe died"
_TRANSPORT_GONE = "error: transport closed"


class _Unbounded(BaseException):
    """More turns than any budget allows. A ``BaseException`` on purpose: it must
    reach the test instead of being retried as one more failed attempt."""


class _Client:
    """Fake ACP client: one scripted stop reason per turn, the last repeating."""

    def __init__(self, *stops: str, ceiling: int) -> None:
        self.stops = list(stops)
        self.turns = 0
        self.prompts: list[str] = []
        self._ceiling = int(ceiling)

    async def stream(self, prompt: str) -> AsyncIterator[LLMEvent]:
        self.turns += 1
        self.prompts.append(prompt)
        if self.turns > self._ceiling:
            raise _Unbounded(f"turn {self.turns}: the in-place recovery is not bounded")
        stop = self.stops[min(self.turns, len(self.stops)) - 1]
        await asyncio.sleep(0)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"partial from turn {self.turns}")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop, usage=TurnUsage(duration_ms=0))


def _sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.record_success = MagicMock()
    sessions.check_context_usage = MagicMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_failure = AsyncMock()
    return sessions


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_executor, "check_context", AsyncMock())
    monkeypatch.setattr(
        task_executor,
        "build_task_prompt",
        AsyncMock(side_effect=lambda run, task, *a, **k: task.description),
    )
    fake_config = MagicMock()
    fake_config.load.return_value = SimpleNamespace(agent=SimpleNamespace(provider="acp"))
    monkeypatch.setattr(task_executor, "KiroCrewConfig", fake_config)


async def _step(client: _Client, task: Task, handle: r.Admitted) -> bool:
    run = Project(spec_path="spec.md", spec_content="body")
    run.task_id = _RUN_TASK_ID
    run.tasks = [task]
    run.branch_name = ""
    run.work_dir = ""
    sessions = _sessions()
    sessions.open_task_session = AsyncMock(return_value=(client, True, False))
    return await task_executor.execute_task(
        run,
        task,
        sessions,
        None,
        "agentX",
        None,
        False,
        None,
        "",
        AsyncMock(),
        _SESSION_KEY,
        taskq=handle,
    )


class _Recovery:
    """The ladder's wait between a stalled turn and its re-run, on the fake clock.

    Production passes real seconds here; this passes the SAME seconds to the
    clock the store and the ladder read, so ``next_run_at`` is due when the
    reclaim asks and nothing sleeps. ``extra`` is what the decay case adds: a
    cooldown's worth of wall time between stalls, which is the ONLY way the
    ladder's own count stops bounding the re-runs.
    """

    def __init__(self, clock: Clock, *, extra: float = 0.0) -> None:
        self.clock = clock
        self.extra = float(extra)
        self.delays: list[float] = []

    async def __call__(self, secs: float) -> None:
        self.delays.append(secs)
        self.clock.advance(secs + self.extra)
        await asyncio.sleep(0)


class _Sleeps:
    """The admission's own sleep: advances the clock instead of waiting."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, secs: float) -> None:
        self.calls.append(secs)
        self.clock.advance(secs)
        await asyncio.sleep(0)


def _admitted(
    store: TaskStore, clock: Clock, *, seed: int = 1
) -> tuple[r.RunnerAdmission, RecoveryLadder]:
    ladder = RecoveryLadder(clock=clock, rng=random.Random(seed))
    adm = r.RunnerAdmission(
        store, lane=r.RunnerLane(1), clock=clock, sleep=_Sleeps(clock), ladder=ladder
    )
    return adm, ladder


async def _row(adm: r.RunnerAdmission) -> r.Admitted:
    rec = await adm.accept_async(
        kind=m.KIND_TASKRUNNER_STEP, task_id=_ROW_ID, side_effect_class=m.SIDE_EFFECT_NONE
    )
    assert rec is not None
    handle = await adm.admit(rec.id)
    assert await handle.running_async({"index": 1}) is True
    return handle


def _open(tmp_path: Path, clock: Clock, cls: type[TaskStore] = TaskStore) -> TaskStore:
    return cls(
        tmp_path / "tasks.db", window=8, clock=clock, network_fs=False, busy_timeout_secs=0.05
    ).open()


# ── the two budgets are different in kind ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stall_the_ladder_approved_does_not_spend_the_logic_retry_budget(
    tmp_path: Path, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ordinary failures, then a stall on the last attempt the logic budget
    has: the ladder approves the recovery, so the step re-runs instead of being
    failed with an approved recovery in hand."""
    clock = Clock()
    store = _open(tmp_path, clock)
    recovery = _Recovery(clock)
    monkeypatch.setattr(task_executor, "_recovery_delay", recovery)
    try:
        adm, ladder = _admitted(store, clock)
        handle = await _row(adm)
        client = _Client(
            _PIPE_DIED, _TRANSPORT_GONE, STOP_REASON_TOOL_STALL, STOP_REASON_END_TURN, ceiling=8
        )
        task = Task(index=1, title="stall", description="desc")

        ok = await _step(client, task, handle)
        assert ok is True, f"the ladder approved a recovery; the step failed: {task.error}"
        assert client.turns == MAX_RETRIES + 1, "the stall was charged to the logic budget"
        assert task.status is TaskStatus.PASSED
        assert recovery.delays and recovery.delays[0] > 0, "the ladder's L3 delay was not taken"
        # One L3 failure recorded, and the row went recovering -> re-claimed
        # under a new generation rather than being failed.
        assert ladder.attempts(L3_ACP_RUNTIME, _SESSION_KEY) == 1
        kinds = [e.kind for e in store.events(handle.task_id)]
        assert kinds.count("claimed") == 2
        assert store.state_of(handle.task_id) == m.RUNNING
        assert handle.state == m.RUNNING
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_in_place_budget_bounds_the_re_runs_the_ladder_count_forgets(
    tmp_path: Path, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stall once per ladder cooldown is approved by the ladder every time --
    its per-unit count decays -- so the bound that holds the re-runs is the
    step's own in-place budget, not the ladder's."""
    clock = Clock()
    store = _open(tmp_path, clock)
    cooldown = RecoveryLadder(clock=clock).layer_policy(L3_ACP_RUNTIME).cooldown_secs
    recovery = _Recovery(clock, extra=cooldown + 1.0)
    monkeypatch.setattr(task_executor, "_recovery_delay", recovery)
    try:
        adm, ladder = _admitted(store, clock)
        handle = await _row(adm)
        client = _Client(STOP_REASON_TOOL_STALL, ceiling=STOP_RECOVERY_MAX_RETRIES + 3)
        task = Task(index=1, title="stall", description="desc")

        assert await _step(client, task, handle) is False
        assert client.turns == STOP_RECOVERY_MAX_RETRIES + 1
        assert task.status is TaskStatus.FAILED
        assert "in-place recovery exhausted" in task.error
        assert task.result.startswith("partial from turn"), "the partial was dropped"
        # Every stall started a fresh run of failures at L3: the ladder never
        # refused one, which is precisely why the step needs its own ceiling.
        assert ladder.attempts(L3_ACP_RUNTIME, _SESSION_KEY) == 1
        assert len(recovery.delays) == STOP_RECOVERY_MAX_RETRIES
    finally:
        store.close()


# ── the recovering write is the precondition, not advice ──────────────────────


class _LockedOnRecovering(TaskStore):
    """A REAL store whose FILE another connection locks for the ``recovering``
    write only, so that ONE transition gets SQLite's own ``database is locked``.

    The lock clears with the refusal, which is the transient case: the store is
    there again by the time the caller decides what to do, so what the step does
    next is a decision about the REFUSAL and not about an outage.
    """

    def transition(self, task_id, state, **kw):  # type: ignore[no-untyped-def]
        if state != m.RECOVERING:
            return super().transition(task_id, state, **kw)
        # ``transition`` runs on the store's writer thread and ``close`` on the
        # test's, so the blocker is not pinned to either.
        blocker = sqlite3.connect(str(self.path), timeout=0, check_same_thread=False)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            return super().transition(task_id, state, **kw)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()


@pytest.mark.asyncio
async def test_a_refused_recovering_write_refuses_the_recovery(
    tmp_path: Path, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row never left ``running``, so there is nothing to re-claim: the step
    ends with its partial preserved instead of reclaiming a row this incarnation
    already owns and raising the run down."""
    clock = Clock()
    store = _open(tmp_path, clock, _LockedOnRecovering)
    recovery = _Recovery(clock)
    monkeypatch.setattr(task_executor, "_recovery_delay", recovery)
    try:
        adm, _ = _admitted(store, clock)
        handle = await _row(adm)
        generation = handle.generation
        client = _Client(STOP_REASON_TOOL_STALL, STOP_REASON_END_TURN, ceiling=8)
        task = Task(index=1, title="stall", description="desc")

        assert await _step(client, task, handle) is False
        assert client.turns == 1, "the turn re-ran under a row the store never moved"
        assert recovery.delays == [], "the recovery wait was taken for a refused write"
        assert task.status is TaskStatus.FAILED
        assert "recovering" in task.error
        assert task.result.startswith("partial from turn 1"), "the partial was dropped"
        # The row is exactly where the store left it, under THIS incarnation's
        # generation, so the caller's terminal write still commits.
        assert store.state_of(handle.task_id) == m.RUNNING
        assert handle.state == m.RUNNING
        assert handle.generation == generation
        assert handle.fail(task.error) is True
        assert store.state_of(handle.task_id) == m.FAILED
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_reclaim_refused_by_a_cancel_ends_the_step_not_the_run(
    tmp_path: Path, patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery write commits and the operator cancels the row during the
    backoff: the reclaim has nothing to take. That is this step's outcome, not
    an exception the run unwinds on."""
    clock = Clock()
    store = _open(tmp_path, clock)
    recovery = _Recovery(clock)

    async def _cancel_then_wait(secs: float) -> None:
        store.cancel(_ROW_ID, reason="operator")
        await recovery(secs)

    monkeypatch.setattr(task_executor, "_recovery_delay", _cancel_then_wait)
    try:
        adm, _ = _admitted(store, clock)
        handle = await _row(adm)
        client = _Client(STOP_REASON_TOOL_STALL, STOP_REASON_END_TURN, ceiling=8)
        task = Task(index=1, title="stall", description="desc")

        assert await _step(client, task, handle) is False
        assert client.turns == 1
        assert task.status is TaskStatus.FAILED
        assert store.state_of(handle.task_id) == m.CANCELLED, "the cancel was overwritten"
    finally:
        store.close()
