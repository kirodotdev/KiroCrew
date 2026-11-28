"""Mutation pins for the load-bearing lines in the dependency coordinator that carried no test.

These lines in ``kiro_crew.taskq.dependency`` (the per-scope retry coordinator) and
one line in ``kiro_crew.taskq.lanes`` carry invariants that no other test observes:
delete any one of them and every test that covers the coordinator and the lane
scheduler stays green. This file pins the reachable ones, whose silent removal
answers an outage as a wait, parks a row another owner holds, loses a give-up,
resurrects a schedule nothing waits on, or hands a tie to the newer lane. Lines
whose deletion an existing test already reds are left to that test, and a line
whose guard is redundant (no writer can reach the case it excludes) is left
unpinned, because a pin on it freezes dead code instead of protecting behaviour.

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
import threading
from pathlib import Path
from typing import Any

import pytest
from overload_fakes import Clock, FixedRng, backoff, open_task_store

from kiro_crew.taskq import lanes, model
from kiro_crew.taskq.dependency import (
    EVENT_WAIT,
    KIND_AUTH_FAILED,
    KIND_PERMANENT_PARAM_ERROR,
    KIND_RATE_LIMITED,
    OUTCOME_DEADLINE,
    OUTCOME_TERMINAL,
    OUTCOME_UNPERSISTED,
    OUTCOME_WAIT,
    PARK_STATE,
    PHASE_PROBE,
    PHASE_RAMP,
    WAIT_STATE,
    DependencyCoordinator,
    DependencySignal,
)
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.testing.wait import default_timeout

T0 = 1_000_000.0
GH = "github:api"
OTHER = "provider:model-x"


def _signal(
    kind: str = KIND_RATE_LIMITED, scope: str = GH, retry_at: float | None = None
) -> DependencySignal:
    return DependencySignal(kind=kind, dependency_scope=scope, source="test", retry_at=retry_at)


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> TaskStore:
    yield from open_task_store(tmp_path, clock, name="tasks/tasks.db", window=64)


def _coord(store: TaskStore | None, clock: Clock, **kw: Any) -> DependencyCoordinator:
    kw.setdefault("rng", FixedRng())
    kw.setdefault("backoff", backoff(2.0, 900.0))
    kw.setdefault("wake_spacing_secs", 5.0)
    return DependencyCoordinator(store, clock=clock, **kw)


def _accept(store: TaskStore, task_id: str) -> None:
    store.accept_one(
        model.TaskRecord(id=task_id, kind=model.KIND_SUBAGENT, session_key="s", params={})
    )


def _claimed(store: TaskStore, task_id: str, *, run: bool) -> int:
    """Claim *task_id* and walk it to ``starting`` (and ``running`` when *run*)."""
    claim = store.claim(task_id, owner="w")
    assert claim is not None
    assert store.transition(task_id, model.STARTING, generation=claim.generation)
    if run:
        assert store.transition(task_id, model.RUNNING, generation=claim.generation)
    return claim.generation


def _running(store: TaskStore, task_id: str) -> int:
    _accept(store, task_id)
    return _claimed(store, task_id, run=True)


def _starting(store: TaskStore, task_id: str) -> int:
    _accept(store, task_id)
    return _claimed(store, task_id, run=False)


def _woken_probe(store: TaskStore, clock: Clock, coord: DependencyCoordinator) -> int:
    """A live run ``t1`` waits on GH and is woken as the probe; answers its new generation.

    The row is then in ``retry_wait``, which is where a woken probe that fails again
    reports from.
    """
    gen = _running(store, "t1")
    assert coord.report("t1", _signal(), generation=gen).state == WAIT_STATE
    clock.advance(2.0)
    assert coord.tick() == ["t1"]
    row = store.get("t1")
    assert row is not None and row.state == model.RETRY_WAIT
    return row.generation


def _wait_events(store: TaskStore, task_id: str) -> list[dict]:
    return [e.data for e in store.events(task_id) if e.kind == EVENT_WAIT]


class TestAParkOutageIsNeverAnsweredWithAPark:
    """``_persist_wait``'s ``if parked == WRITE_UNAVAILABLE: return None``.

    The ``enter`` was refused (the row is not a live run) and the ``retry_wait``
    park could not be written because the store is down. Nothing durable holds the
    wait, so the caller must be told ``unpersisted`` and no ``dependency_wait``
    event may claim one: that event is what ``rebuild`` restores a parked waiter
    from.

    - deleted: the outage falls through and is answered as a park, with an event;
    - loosened: the outage is routed through the refusal re-read
      (``if parked != WRITE_COMMITTED:``), so a row already in ``retry_wait`` is
      answered as parked although the park write never landed;
    - healthy: with the store back, the same report parks the row in ``retry_wait``.
    """

    @staticmethod
    def _park_down(store: TaskStore, monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
        real = store.transition
        outage = {"on": True}

        def _transition(task_id: str, state: str, *a: Any, **kw: Any) -> bool:
            if outage["on"] and state == model.RETRY_WAIT:
                raise TaskStoreUnavailable("store down")
            return real(task_id, state, *a, **kw)

        monkeypatch.setattr(store, "transition", _transition)
        return outage

    def test_a_starting_row_whose_park_is_down_is_unpersisted(
        self, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coord = _coord(store, clock)
        gen = _starting(store, "t1")
        self._park_down(store, monkeypatch)

        verdict = coord.report("t1", _signal(), generation=gen, from_state=model.STARTING)
        assert verdict.outcome == OUTCOME_UNPERSISTED
        assert store.state_of("t1") == model.STARTING
        assert _wait_events(store, "t1") == []
        assert coord.waiters(GH) == []

    def test_a_reporting_probe_whose_park_is_down_is_unpersisted(
        self, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coord = _coord(store, clock)
        gen = _woken_probe(store, clock, coord)
        waits_before = len(_wait_events(store, "t1"))
        self._park_down(store, monkeypatch)

        verdict = coord.report("t1", _signal(), generation=gen, from_state=model.RETRY_WAIT)
        assert verdict.outcome == OUTCOME_UNPERSISTED
        assert len(_wait_events(store, "t1")) == waits_before
        assert coord.waiters(GH) == []

    def test_the_healthy_side_parks_the_row_once_the_store_is_back(
        self, store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coord = _coord(store, clock)
        gen = _starting(store, "t1")
        outage = self._park_down(store, monkeypatch)
        assert coord.report("t1", _signal(), generation=gen).outcome == OUTCOME_UNPERSISTED

        outage["on"] = False
        verdict = coord.report("t1", _signal(), generation=gen, from_state=model.STARTING)
        assert (verdict.outcome, verdict.state) == (OUTCOME_WAIT, PARK_STATE)
        assert store.state_of("t1") == model.RETRY_WAIT
        assert [e["state"] for e in _wait_events(store, "t1")] == [PARK_STATE]
        assert coord.waiters(GH) == ["t1"]


def _give_up_once(coord: DependencyCoordinator, clock: Clock) -> None:
    """Drive a store-less scope with ``max_attempts=1`` to its give-up for ``t1``."""
    assert coord.report("t1", _signal()).outcome == OUTCOME_WAIT
    clock.advance(2.0)
    assert coord.tick() == ["t1"]
    assert coord.report("t1", _signal()).outcome == OUTCOME_DEADLINE


class TestAGiveUpReachesAHookWhoseLoopHasStopped:
    """``_on_hook_loop``'s ``or not home.is_running()`` term.

    The hooks belong to the loop they subscribed from. When that loop has stopped
    but is not closed (a gateway shutting down), ``call_soon_threadsafe`` queues the
    callback where nothing will run it. After a give-up the schedule is already
    cleared, so the hook is the waiter's only releaser: it runs inline instead.

    - deleted: the give-up hook is queued on the stopped loop and never runs;
    - loosened: none weaker than the deletion; ``is_closed`` and ``is_running`` are
      the only loop-state questions asyncio answers, and ``is_closed`` is already
      asked;
    - healthy: with the home loop running on another thread, the hook is handed to
      that loop and runs on its thread.
    """

    def test_a_stopped_loop_still_gets_the_give_up(self, clock: Clock) -> None:
        failed: list[str] = []
        coord = _coord(None, clock, max_attempts=1)
        loop = asyncio.new_event_loop()
        try:

            async def _subscribe() -> None:
                coord.subscribe(on_fail=lambda tid, _reason: failed.append(tid))

            loop.run_until_complete(_subscribe())
            assert not loop.is_running() and not loop.is_closed()

            _give_up_once(coord, clock)
            assert failed == ["t1"]
        finally:
            loop.close()

    def test_the_healthy_side_hands_the_hook_to_its_running_loop(self, clock: Clock) -> None:
        coord = _coord(None, clock, max_attempts=1)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="hook-loop", daemon=True)
        thread.start()
        fired: list[tuple[str, int]] = []
        done = threading.Event()

        def _on_fail(task_id: str, _reason: str) -> None:
            fired.append((task_id, threading.get_ident()))
            done.set()

        async def _subscribe() -> None:
            coord.subscribe(on_fail=_on_fail)

        # The bound guards against a hang; it never decides the verdict.
        bound = default_timeout()
        try:
            asyncio.run_coroutine_threadsafe(_subscribe(), loop).result(timeout=bound)
            _give_up_once(coord, clock)
            assert done.wait(timeout=bound)
            assert fired == [("t1", thread.ident)]
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=bound)
            loop.close()
        assert not thread.is_alive()


class TestAHookCalledOnItsOwnLoopRunsInline:
    """``_on_hook_loop``'s ``home is running`` term.

    Called ON the loop the hooks belong to, the hook runs at once: deferring it with
    ``call_soon_threadsafe`` leaves the released run parked until the loop next
    turns, and the coordinator's caller reads the schedule as already released.

    - deleted: the hook is deferred to the next loop turn;
    - loosened: none weaker than the deletion; ``_remember_hook_loop`` records the
      running loop as the home before the test, so ``home is running`` holds exactly
      when any loop runs on this thread, and ``running is not None`` is the same
      predicate;
    - healthy: the hook runs exactly once, not once inline and again deferred.
    """

    @pytest.mark.asyncio
    async def test_the_give_up_hook_has_run_when_report_returns(self, clock: Clock) -> None:
        failed: list[str] = []
        coord = _coord(None, clock, max_attempts=1, on_fail=lambda t, _r: failed.append(t))
        coord.subscribe()

        _give_up_once(coord, clock)
        assert failed == ["t1"]

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert failed == ["t1"]


class TestADeferredWakeRefusalNeverResurrectsAGoneScope:
    """``_requeue_waiter``'s scope-gone guard (``if sched is None: ...; return``).

    A delegated wake runs on the loop one tick after the worker popped the waiter.
    If the run ended meanwhile (``forget``), the scope was deleted: the last waiter
    left. A refusal arriving then must not re-create the schedule, which would hold
    a waiter nobody is waiting on and keep the pump ticking for it.

    - deleted (replaced with a fresh ``ScopeSchedule`` stored under the scope): the
      gone scope comes back holding the ended task;
    - loosened (``self._scopes.setdefault(scope, ScopeSchedule(...))``): the same;
    - healthy: while the scope still exists, the refused waiter is requeued and the
      next tick wakes it again.
    """

    @staticmethod
    def _delegated(store: TaskStore, clock: Clock) -> tuple[DependencyCoordinator, list[Any], int]:
        coord = _coord(store, clock, wake_through=lambda _t, _g: False)
        gen = _running(store, "t1")
        assert coord.report("t1", _signal(), generation=gen).state == WAIT_STATE
        clock.advance(2.0)
        callbacks: list[Any] = []
        assert coord.tick(callbacks=callbacks, live_waiters=frozenset({("t1", gen)})) == ["t1"]
        assert coord.waiters(GH) == [] and callbacks
        return coord, callbacks, gen

    def test_a_refusal_after_the_scope_left_does_not_bring_it_back(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord, callbacks, _gen = self._delegated(store, clock)
        coord.forget("t1")
        assert coord.scopes() == []

        for callback in callbacks:
            callback()
        assert coord.scopes() == []
        assert coord.next_deadline() is None

    def test_the_healthy_side_requeues_onto_a_live_scope(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord, callbacks, gen = self._delegated(store, clock)

        for callback in callbacks:
            callback()
        assert coord.waiters(GH) == ["t1"]
        due = coord.next_deadline()
        assert due is not None and due >= clock() + 5.0

        clock.advance(due - clock())
        again: list[Any] = []
        assert coord.tick(callbacks=again, live_waiters=frozenset({("t1", gen)})) == ["t1"]


class TestALateProbeFailureCountsAgainstTheFailedScope:
    """``_take_scope_failure``'s ``if not sched.in_flight:`` before the scope is popped.

    A give-up fails the waiters, but woken tasks still in flight have not answered.
    The scope is kept for them so a late failure from one still counts against the
    scope that gave up, instead of opening a FRESH schedule with a full budget.

    - deleted (popped unconditionally): the late report starts a new schedule at
      attempt 1 and is answered as a wait;
    - loosened: none weaker than the deletion; the guard is one emptiness test
      (``if not sched.waiters`` reads the set just cleared, which is the deletion);
    - healthy: with nothing in flight the scope is removed, and a later report starts
      a fresh schedule.
    """

    def test_a_late_report_from_the_wave_is_failed_too(self, clock: Clock) -> None:
        failed: list[str] = []
        coord = _coord(
            None, clock, max_attempts=1, wake_per_tick=2, on_fail=lambda t, _r: failed.append(t)
        )
        for tid in ("t1", "t2", "t3"):
            assert coord.report(tid, _signal()).outcome == OUTCOME_WAIT
        clock.advance(2.0)
        assert coord.tick() == ["t1"]
        clock.advance(5.0)
        assert coord.tick() == ["t2", "t3"]

        assert coord.report("t2", _signal()).outcome == OUTCOME_DEADLINE
        assert failed == ["t2"]

        late = coord.report("t3", _signal())
        assert late.outcome == OUTCOME_DEADLINE
        assert failed == ["t2", "t3"]

    def test_the_healthy_side_a_scope_with_nothing_in_flight_starts_fresh(
        self, clock: Clock
    ) -> None:
        coord = _coord(None, clock, max_attempts=1)
        _give_up_once(coord, clock)
        assert coord.scopes() == []

        fresh = coord.report("t9", _signal())
        assert (fresh.outcome, fresh.attempts) == (OUTCOME_WAIT, 1)


class TestATerminalSignalTakesTheTaskOffEveryScope:
    """``_terminal``'s loop over every scope's ``waiters`` and ``in_flight``.

    A task ended by a terminal signal (auth, a permanent parameter error) is done
    waiting everywhere, whichever scope the signal named. Left on a schedule, a
    later tick spends the scope's one probe on a task that already failed, and a
    woken task left in flight pins the scope.

    - deleted: the ended task stays a waiter and is woken as the next probe;
    - loosened (only the signal's own scope): a task ended on another scope still
      takes this scope's probe;
    - loosened (only ``waiters``, not ``in_flight``): an ended probe keeps the scope
      alive after its last waiter left;
    - healthy: the next probe goes to a task that is still waiting, and a scope with
      no one left is removed.
    """

    def test_a_task_ended_on_another_scope_is_not_this_scopes_probe(self, clock: Clock) -> None:
        coord = _coord(None, clock)
        for tid in ("t1", "t2"):
            assert coord.report(tid, _signal()).outcome == OUTCOME_WAIT

        ended = coord.report("t1", _signal(kind=KIND_AUTH_FAILED, scope=OTHER))
        assert ended.outcome == OUTCOME_TERMINAL
        assert coord.waiters(GH) == ["t2"]

        clock.advance(2.0)
        assert coord.tick() == ["t2"]

    def test_an_ended_probe_does_not_pin_its_scope(self, clock: Clock) -> None:
        coord = _coord(None, clock)
        assert coord.report("t1", _signal()).outcome == OUTCOME_WAIT
        clock.advance(2.0)
        assert coord.tick() == ["t1"]

        ended = coord.report("t1", _signal(kind=KIND_PERMANENT_PARAM_ERROR))
        assert ended.outcome == OUTCOME_TERMINAL
        clock.advance(1.0)
        assert coord.tick() == []
        assert coord.scopes() == []
        assert coord.next_deadline() is None


class TestAnUncountedReportLeavesTheWakeWave:
    """``report``'s unconditional ``sched.in_flight.discard(task_id)`` after joining.

    A woken task whose wave was already counted (another member reported first and
    set the scope back to ``waiting``) rejoins as a plain waiter. It must leave
    ``in_flight`` too: a waiter still marked woken is read as the probe by
    ``forget``, so cancelling it while the real probe is out moves the scope to
    ``ramp`` and wakes the rest before the probe has answered.

    - deleted: cancelling that waiter during the next probe ramps the scope at once;
    - loosened: none weaker than the deletion; it is one unconditional discard, and
      every condition a reader might put on it (the phase, the count) is the branch
      above that already discarded;
    - healthy: the probe's own success is what ramps the scope.
    """

    def test_cancelling_a_rejoined_waiter_is_not_the_probe_succeeding(self, clock: Clock) -> None:
        coord = _coord(None, clock, wake_per_tick=2)
        for tid in ("t1", "t2", "t3", "t4"):
            assert coord.report(tid, _signal()).outcome == OUTCOME_WAIT
        clock.advance(2.0)
        assert coord.tick() == ["t1"]
        coord.forget("t1")
        assert coord.tick() == ["t2", "t3"]

        assert coord.report("t2", _signal()).attempts == 2
        assert coord.report("t3", _signal()).attempts == 2
        clock.advance(4.0)
        assert coord.tick() == ["t4"]

        coord.forget("t3")
        sched = coord.schedule(GH)
        assert sched is not None and sched.phase == PHASE_PROBE
        assert coord.tick() == []

        coord.forget("t4")
        assert coord.tick() == ["t2"]


class TestAProbingScopePastItsDeadlineFinishesItsWake:
    """``tick``'s ``and sched.phase == PHASE_WAITING`` in the deadline check.

    The wall-clock deadline fails a scope that is still WAITING. A scope whose
    retry instant already arrived is mid-wake: it is probing or ramping, and its
    waiters are being released, so it finishes the wake instead of failing them.

    - deleted: a probing scope past its deadline fails the waiters it was about to
      wake;
    - loosened (``sched.phase != PHASE_RAMP``): a probing scope is still failed;
    - healthy: a scope still waiting at its deadline is failed, and its waiters are
      told.
    """

    def test_a_probing_scope_past_its_deadline_ramps(self, clock: Clock) -> None:
        failed: list[str] = []
        coord = _coord(
            None,
            clock,
            wait_deadline_secs=100.0,  # flake-ok: read on the injected Clock; no real time passes
            wake_per_tick=1,
            on_fail=lambda t, _r: failed.append(t),
        )
        for tid in ("t1", "t2", "t3"):
            assert coord.report(tid, _signal(retry_at=T0 + 98.0)).outcome == OUTCOME_WAIT
        clock.advance(98.0)
        assert coord.tick() == ["t1"]

        clock.advance(5.0)
        assert coord.tick() == ["t2"]
        sched = coord.schedule(GH)
        assert sched is not None and sched.phase == PHASE_RAMP
        clock.advance(5.0)
        assert coord.tick() == ["t3"]
        assert failed == []

    def test_the_healthy_side_a_waiting_scope_past_its_deadline_fails(self, clock: Clock) -> None:
        failed: list[str] = []
        coord = _coord(
            None,
            clock,
            wait_deadline_secs=100.0,  # flake-ok: read on the injected Clock; no real time passes
            on_fail=lambda t, _r: failed.append(t),
        )
        assert coord.report("t1", _signal(retry_at=T0 + 150.0)).outcome == OUTCOME_WAIT
        clock.advance(150.0)
        assert coord.tick() == []
        assert failed == ["t1"]
        assert coord.scopes() == []


class TestALaneCreditTieGoesToTheOldestHead:
    """``LaneScheduler.interleave``'s ``if age_of is not None: order.sort(...)``.

    Lanes tied on credit are broken in the order ``pick`` reads them, and the store
    hands the lanes over sorted by lane NAME. ``fetch_dispatchable_fair`` passes
    ``age_of`` so the tie goes to the lane whose head has waited longest.

    - deleted: the tie goes to the lane that sorts first by name, a newer row;
    - loosened (sorted newest-first, or by lane name): the newer head wins the tie;
    - healthy: both lanes are served, oldest head first, one row each.
    """

    @staticmethod
    def _two_lanes(store: TaskStore, clock: Clock) -> None:
        store.accept_one(model.TaskRecord(id="zb0", kind=model.KIND_SUBAGENT, session_key="dash:b"))
        clock.advance(100.0)
        store.accept_one(model.TaskRecord(id="za0", kind=model.KIND_SUBAGENT, session_key="dash:a"))
        store.accept_one(model.TaskRecord(id="za1", kind=model.KIND_SUBAGENT, session_key="dash:a"))
        clock.advance(1.0)

    def test_the_store_breaks_a_tie_on_the_older_head(self, store: TaskStore, clock: Clock) -> None:
        self._two_lanes(store, clock)
        picked = store.fetch_dispatchable_fair(
            model.KIND_SUBAGENT, limit=1, scheduler=lanes.LaneScheduler()
        )
        assert [r.id for r in picked] == ["zb0"]

    def test_the_healthy_side_serves_both_lanes_oldest_head_first(
        self, store: TaskStore, clock: Clock
    ) -> None:
        self._two_lanes(store, clock)
        order = store.fetch_dispatchable_fair(
            model.KIND_SUBAGENT, limit=3, scheduler=lanes.LaneScheduler()
        )
        assert [r.id for r in order] == ["zb0", "za0", "za1"]

    def test_the_scheduler_alone_orders_ties_by_age_not_mapping_order(self) -> None:
        by_lane = {"first": [("new", 5.0)], "second": [("old", 1.0)]}
        order = lanes.LaneScheduler().interleave(by_lane, limit=2, age_of=lambda row: row[1])
        assert [row[0] for row in order] == ["old", "new"]
