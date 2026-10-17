"""Shared fakes for the overload-resilience tests (taskq, admission, fairness, adaptive).

A plain module, imported explicitly (``from overload_fakes import Clock``) like
``terminal_fakes``: no autouse magic, every test file names what it takes.

* :class:`Clock` -- a callable virtual clock (``clock()`` returns ``t``); the
  starting instant is a constructor argument so each suite keeps its own epoch.
* :class:`FixedRng` -- every draw answers the top of its range so jittered instants are exact.
* :func:`backoff` -- the coordinator's recovery-ladder schedule with a test-sized base and cap.
* :func:`task_record` -- a minimal ``TaskRecord`` for one id.
* :func:`open_task_store` -- the store a fixture yields, closed on teardown.
* :func:`wait_taskq_open` -- a manager's off-loop store open, attached or failed by name.
* :func:`settle_store_writes` / :func:`settle_dependency_park` -- the two
  barriers: the store's writer thread, and a step reaching its dependency wait.
* :func:`settle_depth_emits` -- wait out every in-flight queued-depth emit.
* :func:`memory_below_floor` -- the memory floor's reading of a host short of memory, for deferring a spawn.
* :class:`ManagerHarness` -- a real ``SubagentManager`` whose runs finish when
  the test says so, with the mocks it needs (:func:`mock_sessions`, :func:`mock_ctx`).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.recovery.policy import LayerPolicy, RecoveryPolicy
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import FairnessSettings
from kiro_crew.taskq import model
from kiro_crew.taskq.adapters.runner import RunnerAdmission
from kiro_crew.taskq.dependency import dependency_backoff
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitLedger

#: The tool a parent blocks in while it waits for its children.
BLOCKING_TOOL = "@kirocrew-core/spawn_sub_agents"


class Clock:
    """Callable virtual clock: ``clock()`` is ``t``; tests move it explicitly."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> float:
        self.t += secs
        return self.t


class FixedRng(random.Random):
    """Every draw answers the top of its range so retry instants are exact."""

    def uniform(self, a: float, b: float) -> float:  # type: ignore[override]
        return b

    def random(self) -> float:
        return 1.0


def backoff(base_secs: float, max_secs: float) -> LayerPolicy:
    """The dependency coordinator's schedule with a test-sized base and cap."""
    return dependency_backoff(RecoveryPolicy(base_secs=base_secs, max_secs=max_secs))


def task_record(task_id: str, **kw: Any) -> model.TaskRecord:
    kw.setdefault("kind", model.KIND_SUBAGENT)
    kw.setdefault("params", {"task": task_id})
    return model.TaskRecord(id=task_id, **kw)


def open_task_store(
    tmp_path: Path, clock: Any, *, name: str = "tasks.db", window: int = 8, **kw: Any
) -> Iterator[TaskStore]:
    """Yield an opened store under *tmp_path* and close it after the test.

    A fixture body: ``yield from open_task_store(tmp_path, clock)``. The
    defaults are the ones most suites use; a suite with its own window or file
    name passes them through.
    """
    s = TaskStore(tmp_path / name, window=window, clock=clock, network_fs=False, **kw).open()
    try:
        yield s
    finally:
        s.close()


#: How long :func:`wait_taskq_open` waits for a manager's off-loop store open.
#: A guard against an open that never returns, never a pass condition. The open
#: is a config load, SQLite create, schema and boot reconcile on a worker thread:
#: about 15 ms on an idle Linux host, and inside a test call of about 0.3 s on a
#: hosted Windows runner. That runner stalls for several seconds at a time,
#: sometimes every xdist worker at once and sometimes one worker alone, and calls
#: that take 0.3 s there have been measured at 5.5 to 6.9 s. The ceiling is more
#: than twice that and under the ``timeout(30)`` these tests carry, so a wedged
#: open fails here, by name, instead of taking the xdist worker down with the
#: pytest-timeout kill.
STORE_OPEN_CEILING_SECS = 20.0


async def wait_taskq_open(mgr: SubagentManager, ceiling: float = STORE_OPEN_CEILING_SECS) -> None:
    """Wait for *mgr*'s startup store open to attach, or fail by name.

    ``SubagentManager`` opens its task store on a worker thread when it is
    built on a running loop, and every spawn answers ``task_store_unavailable``
    until that open attaches. This awaits the open itself (a signal, not a
    poll); *ceiling* only bounds an open that never returns.
    """
    try:
        await asyncio.wait_for(mgr.wait_taskq_ready(), ceiling)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"the task store did not open within {ceiling:.1f}s "
            f"(state: {mgr._taskq_unavailable!r})"
        ) from None


def _noop() -> None:
    return None


async def settle_store_writes(store: TaskStore, rounds: int = 1) -> None:
    """Barrier for the store's off-loop writes -- a SIGNAL, never a sleep.

    ``TaskStore.run`` submits to an executor with exactly ONE worker, so a job
    queued here cannot start before every job queued earlier has finished; the
    loop then resumes the waiters in the order their futures completed. One
    ``sleep(0)`` first, so a task that was only just created reaches its own
    submission before this one is queued behind it.

    *rounds* repeats the barrier for a chain of hops: work a waiter queues only
    after its own write returned (a report task's settle after its terminal, a
    depth emit's re-read) needs one barrier per hop.
    """
    for _ in range(rounds):
        await asyncio.sleep(0)
        await asyncio.wait_for(store.run(_noop), 10)


async def settle_depth_emits(mgr: SubagentManager, timeout: float = 5.0) -> None:
    """Wait until no ``subagent_queued`` emit is in flight -- a signal, never a sleep.

    An emit reads when its own task runs and may hand off to a fresh read
    task, so this waits on the tasks themselves until the per-parent table is
    empty, and fails by name rather than hanging when one never finishes.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while mgr._queue_depth_emits:
        left = deadline - loop.time()
        if left <= 0:
            raise AssertionError(f"depth emits never finished: {sorted(mgr._queue_depth_emits)}")
        await asyncio.wait([e.task for e in mgr._queue_depth_emits.values()], timeout=left)
        await asyncio.sleep(0)  # the tasks' done-callbacks drop their entries


async def settle_dependency_park(
    admission: RunnerAdmission,
    store: TaskStore,
    raised: asyncio.Event,
    *,
    timeout: float = 30.0,
) -> None:
    """Barrier for a step reaching its dependency wait -- SIGNALS, never a poll.

    Two production signals in series, so no number of passes is involved:

    * *raised* is set by the fake at the instant the dependency error leaves the
      provider. The step still HOLDS its lane slot there, so the probe queued
      next is granted exactly when ``yield_dependency`` releases it -- however
      many awaits sit between the two -- and is granted straight away when the
      park got there first.
    * ``yield_dependency`` posts the wait write to the store's single writer
      thread before its first suspension, so a flush queued behind that grant
      runs after it: when this returns the row IS in the wait.

    The probe hands the slot straight back, leaving the lane as the wait left
    it. *timeout* is a lost-run guard for a park that never comes, never the
    barrier -- nothing here waits on wall-clock time.
    """

    async def _handover() -> None:
        await raised.wait()
        await admission.lane.acquire("test:park-probe")
        admission.lane.release()
        await settle_store_writes(store)

    await asyncio.wait_for(_handover(), timeout)


def mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def memory_below_floor(*_args: Any, **_kwargs: Any) -> tuple[bool, float]:
    """The memory floor's reading of a host with 0.5 GB free: below any floor.

    Patch ``kiro_crew.subagent.check_memory_available`` with it inside
    ``monkeypatch.context()`` (or ``patch.object``), never with a bare
    ``monkeypatch.setattr`` followed by ``monkeypatch.undo()``: the test and
    ``healthy_host_memory`` share ONE ``monkeypatch``, so a blanket undo also
    reverts the fixture's pins and later spawns read the runner's real memory.
    """
    return False, 0.5


class ManagerHarness:
    """A real manager whose runs finish when the test says so.

    ``end(info, outcome)`` resolves the run: ``"ok"`` (default) completes it,
    ``"fail"`` sets ``error``, ``"cancel"`` sets ``user_stopped``. The manager's
    store is opened by its constructor; a loop caller awaits
    ``mgr.wait_taskq_ready()`` before spawning.
    """

    def __init__(self, max_concurrent: int, settings: FairnessSettings | None = None) -> None:
        self.mgr = SubagentManager(
            sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=max_concurrent
        )
        self.mgr._spawn_stagger_secs = 0.0
        self.mgr._last_spawn_ts = 0.0
        if settings is not None:
            self.mgr._admission.set_fairness_settings(settings)
        self.finish: dict[str, asyncio.Future] = {}
        self.started: list[str] = []
        harness = self

        async def _run(_self, info: SubagentInfo) -> None:
            harness.started.append(info.id)
            _self._admission.taskq_mark(info, "running")
            fut = harness.finish.setdefault(info.id, asyncio.get_event_loop().create_future())
            outcome = await fut
            info.done = True
            if outcome == "fail":
                info.error = "boom"
            elif outcome == "cancel":
                info.user_stopped = True
            info.result = "ok"
            _self._claim_finalize(info)
            if _self._release_slot(info):
                _self._running_count -= 1
                _self._drain_queue()

        self._patch = patch.object(SubagentManager, "_run", new=_run)
        self._patch.start()
        # Pin the host-memory reading ``SubagentManager.spawn`` consults, the
        # same way conftest's ``healthy_host_memory`` fixture does: an 8 GB
        # host that honours the floor it is asked about. The harness is the
        # fake host, so a memory-pressured runner must not turn a spawn into a
        # deferral that surfaces as a bare KeyError one line on; and it answers
        # the sync gate (which reads the boolean) exactly as the off-loop one
        # (which compares the figure against its own bar).
        import kiro_crew.subagent as subagent_mod
        from kiro_crew.constants import DEFAULT_SPAWN_MIN_MEMORY_GB

        def _host(min_gb: float | None = None, **_kw: Any) -> tuple[bool, float]:
            floor = DEFAULT_SPAWN_MIN_MEMORY_GB if min_gb is None else min_gb
            return 8.0 >= floor, 8.0

        self._memory_patches = [
            patch.object(subagent_mod, "check_memory_available", _host),
        ]
        for mp in self._memory_patches:
            mp.start()

    def close(self) -> None:
        for mp in self._memory_patches:
            mp.stop()
        self._patch.stop()

    @property
    def store(self) -> TaskStore:
        return self.mgr._taskq

    @property
    def ledger(self) -> WaitLedger:
        return WaitLedger(self.store)

    def spawn(self, task: str, parent: str = "dash:1", **kw: Any) -> SubagentInfo:
        info = self.mgr.spawn(task, parent_session_key=parent, **kw)
        assert info is not None
        return info

    def block_in_spawn_sub_agents(self, info: SubagentInfo, call_id: str = "call") -> None:
        self.mgr._agents[info.id]._inflight_tool = SimpleNamespace(
            tool_name=BLOCKING_TOOL, title=call_id
        )

    def child_of(self, parent: SubagentInfo, task: str, **kw: Any) -> SubagentInfo:
        return self.spawn(task, parent=f"subagent:{parent.id}", **kw)

    async def end(self, info: SubagentInfo, outcome: str = "ok") -> None:
        fut = self.finish.setdefault(info.id, asyncio.get_event_loop().create_future())
        if not fut.done():
            fut.set_result(outcome)
        await self.settle()

    async def settle(self, rounds: int = 25) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)

    def state(self, info: SubagentInfo) -> str | None:
        return self.store.state_of(info.id)

    def live(self, info: SubagentInfo) -> SubagentInfo:
        return self.mgr._agents[info.id]
