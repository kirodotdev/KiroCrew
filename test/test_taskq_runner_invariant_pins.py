"""Mutation pins for the load-bearing lines in the runner adapters that carried no test.

These lines in ``kiro_crew.taskq.adapters.runner`` and its ``runner_lane`` and
``runner_handle`` owners carry invariants that no other test observes: delete any
one of them and every test that covers the adapters stays green. This file pins the
reachable ones, whose silent removal loses a terminal write, over-admits the lane,
refuses work it should park, or lets a later arrival take a slot first.

``AUTOSDE.yaml``'s ``an-invariant-line-needs-a-mutation-pin`` requires each pin to
red under three mutations, not one:

1. the line DELETED,
2. the line LOOSENED to the next-weakest thing a reader might write,
3. the HEALTHY side asserted -- under a satisfied fence the write COMMITS and the
   row moves, or the waiter is served.

Every class below carries all three, says why its line has no weaker spelling than
its deletion, or names the existing test that already holds one side. Each is
verified by applying the mutations its docstring names to its line and observing
this file red; the counts are in the PR description.
"""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace

import pytest
from overload_fakes import Clock
from taskq_runner_adapter_helpers import (  # noqa: F401 -- fixtures are requested by name
    _admission,
    _Sleeps,
    clock_fixture,
    store_fixture,
)

from kiro_crew.taskq import model as m
from kiro_crew.taskq.adapters import runner as r
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.testing.wait import async_wait_until, await_until


def _step(adm: r.RunnerAdmission, n: int) -> str:
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id=f"taskrunner:r:task{n}")
    assert rec is not None
    return rec.id


def _store_down(store: TaskStore, monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Make ``finish`` raise ``TaskStoreUnavailable`` until the returned flag is cleared."""
    real_finish = store.finish
    outage = {"on": True}

    def _finish(*a, **kw):
        if outage["on"]:
            raise TaskStoreUnavailable("store down")
        return real_finish(*a, **kw)

    monkeypatch.setattr(store, "finish", _finish)
    return outage


async def _parked(coro) -> asyncio.Task:
    """Start *coro* as a task and let it reach its park (or its refusal)."""
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(0)
    return task


class _ForgetSpy:
    """The one coordinator method the settle path calls."""

    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def forget(self, task_id: str) -> None:
        self.forgotten.append(task_id)


class TestARetryWhileTheStoreIsStillDownKeepsTheWrite:
    """``RunnerAdmission.retry_terminal_writes``'s ``continue`` on ``TaskStoreUnavailable``.

    A replay that meets the store still down must keep owning the write: it is the
    only copy of a finished step's outcome. Dropped there, the row stays ``running``
    under this incarnation's lease until the next boot reconciles it as an
    interrupted run.

    - deleted: the arm falls through and the write counts as settled;
    - loosened: the write is dropped (``_pending_finish.pop``) before the ``continue``;
    - healthy: once the store is back, the same replay commits ``done``.
    """

    @pytest.mark.asyncio
    async def test_a_failed_replay_keeps_the_write_and_a_later_one_commits_it(
        self, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adm, _ = _admission(store, clock, cap=1)
        task_id = _step(adm, 1)
        handle = await adm.admit(task_id)
        assert handle.running()
        outage = _store_down(store, monkeypatch)
        assert handle.done() is False
        assert adm.stats()["pending_terminal_writes"] == 1

        assert adm.retry_terminal_writes() == 0
        assert adm.stats()["pending_terminal_writes"] == 1
        assert store.state_of(task_id) == m.RUNNING

        outage["on"] = False
        assert adm.retry_terminal_writes() == 1
        assert store.state_of(task_id) == m.DONE
        assert adm.stats()["pending_terminal_writes"] == 0


class TestASecondSlotReleaseGivesBackNothing:
    """``Admitted.release_slot``'s ``if self._slot_held:``.

    ``recovering()`` gives the slot back, and a ``fail()`` that follows it (the
    ``recovering`` write did not commit, or the re-claim was refused) settles through
    ``release_slot`` again. That second call must
    free nothing: the slot it would free belongs to another row, and the lane then
    admits one more body than its cap.

    - deleted: the second release frees the other row's slot;
    - loosened: the guard reads the lane (``lane.running > 0``) instead of the handle;
    - healthy: the first release frees exactly one slot, and the lane refills to its cap.
    """

    @pytest.mark.asyncio
    async def test_fail_after_recovering_leaves_the_other_rows_slot_held(
        self, store: TaskStore, clock: Clock
    ) -> None:
        adm, _ = _admission(store, clock, cap=2)
        first = await adm.admit(_step(adm, 1))
        second = await adm.admit(_step(adm, 2))
        assert first.running() and second.running()

        assert first.recovering(reason="stalled", delay_secs=5.0)
        assert adm.lane.running == 1 and adm.lane.holders == frozenset({second.task_id})
        first.fail("recovery exhausted")
        assert adm.lane.running == 1 and adm.lane.holders == frozenset({second.task_id})

        third = await adm.admit(_step(adm, 3))
        assert adm.lane.running == 2 and adm.lane.saturated
        assert adm.lane.holders == frozenset({second.task_id, third.task_id})


class TestAPausedLaneParksADescendant:
    """``RunnerLane.self_blocking_holders``'s ``or self.effective <= 0``.

    A bound of ``0`` is a pause that grants nothing to anyone, and the raise that
    ends it is a ``pump``. A descendant that arrives during the pause, while its
    ancestor holds the only occupied slot, is parked for that raise, never refused.

    - deleted: the descendant is refused as self-blocked;
    - loosened: the guard reads ``self.ceiling`` and misses the lane-local pause;
    - healthy: the raise serves the parked descendant.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pause", ["owner cap", "lane override"])
    async def test_the_descendant_parks_through_the_pause_and_is_served_after(
        self, pause: str
    ) -> None:
        cap = {"v": 2}
        lane = r.RunnerLane(lambda: cap["v"], name="paused")
        await lane.acquire("parent")
        if pause == "owner cap":
            cap["v"] = 0
        else:
            assert lane.set_effective_cap(0) == 0

        child = await _parked(lane.acquire("child", ancestors=["parent"]))
        assert not child.done(), "a paused lane refused a descendant instead of parking it"
        assert lane.waiting == 1

        if pause == "owner cap":
            cap["v"] = 2
            assert lane.pump() == 1
        else:
            assert lane.set_effective_cap(None) == 2
        await await_until(child)
        assert lane.running == 2 and lane.holders == frozenset({"parent", "child"})


class TestSpareCapacityParksADescendant:
    """``RunnerLane.self_blocking_holders``'s ``if self._running < self.effective``.

    The lane reads its cap live, and the raise edge is the owner's separate ``pump``.
    Between the two, a slot is free while a waiter is still parked, so a descendant
    queues behind it (FIFO). With a slot free, no wait is self-blocking, whoever holds
    the others.

    - deleted: the descendant is refused while a slot is free;
    - loosened: ``<=`` stops refusing at the bound, so a lane full of ancestors parks for
      ever; ``test_runner_lane_refuses_a_wait_only_its_own_ancestor_could_end`` in
      ``test_taskq_runner_adapter.py`` holds that refusal side;
    - healthy: the parked descendant is served.
    """

    @pytest.mark.asyncio
    async def test_a_descendant_queued_behind_a_waiter_with_a_slot_free_parks(self) -> None:
        cap = {"v": 1}
        lane = r.RunnerLane(lambda: cap["v"], name="spare")
        await lane.acquire("parent")
        root = await _parked(lane.acquire("root"))
        cap["v"] = 2  # raised; the owner has not rung pump yet

        child = await _parked(lane.acquire("child", ancestors=["parent"]))
        assert not child.done(), "a descendant was refused while a slot was free"
        assert lane.waiting == 2

        assert lane.pump() == 1
        await await_until(root)
        lane.release("root")
        await await_until(child)
        assert lane.holders == frozenset({"parent", "child"})


class TestALateArrivalQueuesBehindAParkedWaiter:
    """``RunnerLane.acquire``'s ``not self._waiters and`` fast-path guard.

    The lane is FIFO. When the owner's cap rises before it rings ``pump``, a slot is
    free while a waiter is still parked, and a late arrival must queue behind that
    waiter rather than take the slot.

    - deleted: the late arrival is granted at once and the earlier waiter stays parked;
    - loosened: none weaker than the deletion; the guard is one emptiness test;
    - healthy: the pump serves the earlier waiter first, the next release the late one.
    """

    @pytest.mark.asyncio
    async def test_the_parked_waiter_is_served_before_the_late_arrival(self) -> None:
        cap = {"v": 1}
        lane = r.RunnerLane(lambda: cap["v"], name="fifo")
        await lane.acquire("holder")
        first = await _parked(lane.acquire("first"))
        cap["v"] = 2  # raised; the owner has not rung pump yet

        late = await _parked(lane.acquire("late"))
        assert not late.done(), "a late arrival overtook a parked waiter"
        assert lane.waiting == 2 and lane.running == 1

        assert lane.pump() == 1
        await await_until(first)
        assert not late.done() and lane.holders == frozenset({"holder", "first"})
        lane.release("holder")
        await await_until(late)
        assert lane.holders == frozenset({"first", "late"})


class TestAnAdmitSleepIsBoundedSoTheRowIsReread:
    """The ``min(..., _MAX_ADMIT_SLEEP_SECS)`` bounds in ``_defer_for_pressure`` and ``admit``.

    ``agent.admit_wait_secs`` goes up to 3600, and a backoff can be longer than any
    wait a caller would sit through blind. Each sleep inside ``admit`` is bounded so
    the row is re-read at least every 30 s and a cancel that lands meanwhile is seen.

    - deleted: one bound gone, the sleep is the whole wait (3600 s, or the 600 s backoff);
    - loosened: bounded by the configured admit wait instead of the constant: the same sleeps;
    - healthy: an uncancelled row is admitted once its wait has passed.
    """

    @pytest.mark.asyncio
    async def test_a_pressure_deferral_sleeps_at_most_the_bound(
        self, store: TaskStore, clock: Clock
    ) -> None:
        refused = SimpleNamespace(admitted=False, reason="memory critical")
        sleeps = _Sleeps(clock)
        cancelled: list[str] = []

        async def _sleep_then_cancel(secs: float) -> None:
            await sleeps(secs)
            cancelled.append(store.cancel(task_id, reason="user") or "")

        adm = r.RunnerAdmission(
            store,
            lane=r.RunnerLane(1),
            clock=clock,
            sleep=_sleep_then_cancel,
            pressure=lambda: refused,
            admit_wait_secs=3600.0,
        )
        task_id = _step(adm, 1)
        with pytest.raises(r.RunnerTaskCancelled):
            await await_until(asyncio.ensure_future(adm.admit(task_id)))
        assert sleeps.calls == [30.0] and cancelled == [m.QUEUED]

    @pytest.mark.asyncio
    async def test_the_healthy_side_admits_once_the_deferral_has_passed(
        self, store: TaskStore, clock: Clock
    ) -> None:
        verdicts = itertools.chain(
            [SimpleNamespace(admitted=False, reason="memory critical")],
            itertools.repeat(SimpleNamespace(admitted=True, reason="")),
        )
        adm, sleeps = _admission(
            store, clock, pressure=lambda: next(verdicts), admit_wait_secs=3600.0
        )
        handle = await await_until(asyncio.ensure_future(adm.admit(_step(adm, 1))))
        assert handle.state == m.STARTING
        assert sum(sleeps.calls) >= 3600.0

    @pytest.mark.asyncio
    async def test_a_backoff_is_slept_in_bounded_steps_and_then_admitted(
        self, store: TaskStore, clock: Clock
    ) -> None:
        adm, sleeps = _admission(store, clock, admit_wait_secs=3600.0)
        task_id = _step(adm, 1)
        assert store.defer(task_id, wait=600.0, reason="backoff")

        handle = await await_until(asyncio.ensure_future(adm.admit(task_id)))
        assert handle.state == m.STARTING
        assert max(sleeps.calls) == 30.0
        assert sum(sleeps.calls) >= 600.0


class TestAnIdleLaneGrantsWithoutReadingTheAncestry:
    """``RunnerAdmission.admit``'s ``and parks`` gate on the ancestor read.

    The ancestor read is a store round trip, an ``await`` between the lane's
    saturation check and its grant. Made only on a saturated lane, it leaves a step
    admitted onto a free slot to take that slot in the same step; made always, any
    other acquirer can take the slot while the read is in flight, and the step parks
    behind a later arrival.

    - deleted: every admission reads the ancestry, onto an idle lane too;
    - loosened: gated on ``lane.running > 0``, a lane with a slot still free reads it;
    - healthy: onto a saturated lane the ancestry is read, and the step parks and runs.
    """

    @staticmethod
    def _reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        real = r.RunnerAdmission._ancestor_ids
        reads: list[str] = []

        def _spy(store: TaskStore, task_id: str, *rest: int) -> frozenset[str]:
            reads.append(task_id)
            return real(store, task_id, *rest)

        monkeypatch.setattr(r.RunnerAdmission, "_ancestor_ids", staticmethod(_spy))
        return reads

    @pytest.mark.asyncio
    async def test_a_lane_with_a_free_slot_reads_no_ancestry(
        self, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads = self._reads(monkeypatch)
        adm, _ = _admission(store, clock, cap=2)
        first = await adm.admit(_step(adm, 1))
        second = await adm.admit(_step(adm, 2))
        assert first.slot_held and second.slot_held
        assert reads == []

    @pytest.mark.asyncio
    async def test_the_healthy_side_reads_it_on_a_saturated_lane(
        self, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads = self._reads(monkeypatch)
        adm, _ = _admission(store, clock, cap=1)
        await adm.lane.acquire("unrelated")
        task_id = _step(adm, 1)
        admitting = asyncio.ensure_future(adm.admit(task_id))
        await async_wait_until(lambda: adm.lane.waiting == 1)
        assert reads == [task_id]

        adm.lane.release("unrelated")
        handle = await await_until(admitting)
        assert handle.state == m.STARTING and handle.slot_held


class TestATerminalWriteStillOwedIsNotForgotten:
    """``Admitted._settle_apply``'s ``if self.task_id not in self.admission._pending_finish:``.

    ``forget`` is the run-ended signal: it drops the row's wake state and takes it out
    of the dependency coordinator's schedule. While the terminal write is owed the
    store still holds the row ``running``, so the settle forgets nothing. A write
    that committed, or one a newer generation fenced (another owner ended the row),
    leaves nothing owed and is forgotten.

    - deleted: a settle whose write was deferred forgets the row;
    - loosened: ``if ok:`` keeps a fenced settle's row known for ever;
    - healthy: a committed and a fenced settle are both forgotten.
    """

    @pytest.mark.asyncio
    async def test_a_deferred_settle_forgets_nothing(
        self, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _ForgetSpy()
        adm, _ = _admission(store, clock, cap=1, coordinator=spy)
        handle = await adm.admit(_step(adm, 1))
        assert handle.running()
        _store_down(store, monkeypatch)

        assert handle.done() is False
        assert adm.stats()["pending_terminal_writes"] == 1
        assert spy.forgotten == []

    @pytest.mark.asyncio
    async def test_the_healthy_side_forgets_a_committed_and_a_fenced_settle(
        self, store: TaskStore, clock: Clock
    ) -> None:
        spy = _ForgetSpy()
        adm, _ = _admission(store, clock, cap=2, coordinator=spy)
        committed = await adm.admit(_step(adm, 1))
        fenced = await adm.admit(_step(adm, 2))
        assert committed.running() and fenced.running()
        assert store.cancel(fenced.task_id, reason="operator") == m.RUNNING

        assert committed.done() is True
        assert fenced.done() is False
        assert adm.stats()["pending_terminal_writes"] == 0
        assert spy.forgotten == [committed.task_id, fenced.task_id]
