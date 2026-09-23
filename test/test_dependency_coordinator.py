"""DependencyCoordinator: one schedule per scope, staged wake by capacity, bounded waits."""

from __future__ import annotations

import asyncio
import random
import threading
from pathlib import Path

import pytest
from overload_fakes import Clock, FixedRng, backoff, open_task_store

from kiro_crew.taskq import model
from kiro_crew.taskq.dependency import (
    EVENT_FAILED,
    EVENT_WAIT,
    EVENT_WAKE,
    KIND_AUTH_FAILED,
    KIND_DEPENDENCY_UNAVAILABLE,
    KIND_PERMANENT_PARAM_ERROR,
    KIND_QUOTA_EXHAUSTED,
    KIND_RATE_LIMITED,
    PHASE_PROBE,
    PHASE_RAMP,
    PHASE_WAITING,
    DependencyCoordinator,
    DependencySignal,
    coordinator_from_config,
    register_coordinator,
    shared_retry_at,
)
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.taskq.waits import WaitRecord

T0 = 1_000_000.0
GH = "github:api"
BEDROCK = "provider:model-x"


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


def _running(store: TaskStore, task_id: str, *, session: str = "s") -> int:
    """Accept, claim and start *task_id* so it is a live ``running`` row; returns its generation."""
    rec = model.TaskRecord(
        id=task_id, kind=model.KIND_SUBAGENT, session_key=session, params={"t": task_id}
    )
    store.accept_one(rec)
    claim = store.claim(task_id, owner="w")
    assert claim is not None
    store.transition(task_id, model.STARTING, generation=claim.generation)
    store.transition(task_id, model.RUNNING, generation=claim.generation)
    return claim.generation


def _rewoken(store: TaskStore, task_id: str) -> int:
    """Re-claim a woken (``retry_wait``) row as a live run; returns its generation."""
    claim = store.claim(task_id, owner="w")
    assert claim is not None
    store.transition(task_id, model.STARTING, generation=claim.generation)
    store.transition(task_id, model.RUNNING, generation=claim.generation)
    return claim.generation


def _starting(store: TaskStore, task_id: str) -> int:
    rec = model.TaskRecord(
        id=task_id, kind=model.KIND_SUBAGENT, session_key="s", params={"t": task_id}
    )
    store.accept_one(rec)
    claim = store.claim(task_id, owner="w")
    assert claim is not None
    store.transition(task_id, model.STARTING, generation=claim.generation)
    return claim.generation


def _coord(store: TaskStore | None, clock: Clock, **kw) -> DependencyCoordinator:
    kw.setdefault("rng", FixedRng())
    kw.setdefault("backoff", backoff(2.0, 900.0))
    kw.setdefault("wake_spacing_secs", 5.0)
    return DependencyCoordinator(store, clock=clock, **kw)


def _events(store: TaskStore, task_id: str, kind: str) -> list[dict]:
    return [e.data for e in store.events(task_id) if e.kind == kind]


# ── one schedule per scope ────────────────────────────────────────────────────


class TestOneSchedulePerScope:
    def test_five_sessions_one_scope_one_schedule_one_probe(
        self, store: TaskStore, clock: Clock
    ) -> None:
        woken: list[str] = []
        coord = _coord(store, clock, capacity=lambda: 8, on_wake=woken.append)
        gens = {f"t{i}": _running(store, f"t{i}", session=f"session-{i}") for i in range(5)}
        verdicts = [coord.report(tid, _signal(), generation=gen) for tid, gen in gens.items()]

        assert coord.scopes() == [GH]
        sched = coord.schedule(GH)
        assert sched is not None
        assert sched.attempts == 1
        assert len(sched.waiters) == 5
        # Every waiter shares the ONE retry instant: base 2s (FixedRng = ceiling).
        assert {v.retry_at for v in verdicts} == {T0 + 2.0}
        assert all(v.outcome == "wait" and v.state == model.WAITING_DEPENDENCY for v in verdicts)
        for tid in gens:
            row = store.get(tid)
            assert row is not None and row.state == model.WAITING_DEPENDENCY
            record = WaitRecord.from_dict(row.wait)
            assert record is not None and record.dependency_scope == GH
            assert record.resume_condition.at == T0 + 2.0

        # Nothing wakes before retry_at.
        clock.advance(1.0)
        assert coord.tick() == []
        # At retry_at exactly ONE probe is released, not five.
        clock.advance(1.0)
        assert coord.tick() == ["t0"]
        assert woken == ["t0"]
        # A woken PARKED row (no live run to hand the slot to) is claimable
        # ``retry_wait`` -- the dispatcher writes ``running`` on claim.
        assert store.state_of("t0") == model.RETRY_WAIT
        assert all(store.state_of(f"t{i}") == model.WAITING_DEPENDENCY for i in range(1, 5))
        sched = coord.schedule(GH)
        assert sched is not None and sched.phase == PHASE_PROBE and sched.in_flight == {"t0"}

    def test_probe_failing_costs_the_scope_one_attempt_not_five(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock, capacity=lambda: 8)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(5)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(), generation=gen)
        clock.advance(2.0)
        assert coord.tick() == ["t0"]
        # The probe hits the wall again (new generation after the wake).
        new_gen = store.get("t0").generation  # type: ignore[union-attr]
        verdict = coord.report("t0", _signal(), generation=new_gen)
        sched = coord.schedule(GH)
        assert sched is not None
        assert sched.attempts == 2
        assert sched.phase == PHASE_WAITING
        assert verdict.retry_at == T0 + 2.0 + 4.0  # base·2^(2-1)
        assert len(sched.waiters) == 5 and not sched.in_flight
        # Nobody else was woken by the failed probe.
        assert len(_events(store, "t1", EVENT_WAKE)) == 0

    def test_newcomers_arriving_mid_wake_spend_none_of_the_probe_budget(
        self, store: TaskStore, clock: Clock
    ) -> None:
        """``dependency_max_attempts`` bounds PROBE cycles, never arrival volume.

        A brand-new task hitting the same down dependency is a joiner, exactly
        like the five above -- its failure is its OWN first call, not this
        scope's probe answering. Charging it to the budget makes ``attempts``
        climb with load instead of with probes: here 24 arrivals against a cap
        of 20 fail every waiter in the scope without one woken probe having
        answered at all.
        """
        coord = _coord(store, clock, capacity=lambda: 8, max_attempts=20, wait_deadline_secs=0.0)
        coord.report("seed", _signal(), generation=_running(store, "seed"))
        seen: list[int] = []
        for i in range(24):
            sched = coord.schedule(GH)
            assert sched is not None
            clock.t = max(clock.t, sched.retry_at)
            assert coord.tick(), "the scope was due but woke nobody"
            assert sched.phase != PHASE_WAITING  # a wake is in flight and still running
            newcomer = f"n{i}"
            verdict = coord.report(newcomer, _signal(), generation=_running(store, newcomer))
            assert verdict.outcome == "wait", f"arrival {i} was failed: {verdict.reason}"
            seen.append(verdict.attempts)
        assert seen == [1] * 24
        assert coord.schedule(GH) is not None  # the scope kept every probe it had

    def test_a_woken_waiters_failure_costs_the_wave_one_probe_and_still_runs_out(
        self, store: TaskStore, clock: Clock
    ) -> None:
        """The other half: a waiter the scope WOKE reporting again IS the probe answering.

        One attempt per wake wave -- and the budget still runs out, so a scope
        against a dependency that never comes back ends its waiters instead of
        riding the ladder forever.
        """
        coord = _coord(store, clock, capacity=lambda: 8, max_attempts=4, wait_deadline_secs=0.0)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(2)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(), generation=gen)
        seen: list[int] = []
        verdict = None
        for _ in range(4):
            sched = coord.schedule(GH)
            assert sched is not None
            clock.t = max(clock.t, sched.retry_at)
            woken = coord.tick()
            assert woken, "the scope was due but woke nobody"
            verdict = coord.report(woken[0], _signal(), generation=_rewoken(store, woken[0]))
            seen.append(verdict.attempts)
        assert seen == [2, 3, 4, 5]  # one per wave, off the ladder each time
        assert verdict is not None and verdict.outcome == "deadline"
        assert "after 5 attempts" in verdict.reason
        assert coord.scopes() == []
        # Every waiter on the scope ends, not just the one that reported last.
        assert [store.state_of(tid) for tid in gens] == [model.FAILED, model.FAILED]

    def test_one_ramp_wave_costs_the_scope_one_attempt_not_one_per_member(
        self, store: TaskStore, clock: Clock
    ) -> None:
        """A ramp wakes by CAPACITY, and its members fail one by one.

        The first failure is the wave's answer and moves the scope back to
        ``waiting``; the rest are the same outage reported again, so they join
        the schedule the first one set. Counting each of them would spend a
        capacity-sized slice of the budget on a single retry round.
        """
        coord = _coord(store, clock, capacity=lambda: 4, wake_spacing_secs=1.0)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(6)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(), generation=gen)
        clock.advance(2.0)
        assert coord.tick() == ["t0"]  # the probe
        clock.advance(1.0)
        wave = coord.tick()
        assert len(wave) == 4 and coord.schedule(GH).phase == PHASE_RAMP  # type: ignore[union-attr]
        for tid in wave:
            coord.report(tid, _signal(), generation=_rewoken(store, tid))
        sched = coord.schedule(GH)
        assert sched is not None
        assert sched.attempts == 2 and sched.phase == PHASE_WAITING
        assert set(sched.waiters) == set(wave) | {"t5"}

    def test_later_joiner_does_not_start_a_second_timer(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock)
        g1 = _running(store, "a")
        coord.report("a", _signal(), generation=g1)
        clock.advance(1.0)
        g2 = _running(store, "b")
        v = coord.report("b", _signal(), generation=g2)
        sched = coord.schedule(GH)
        assert sched is not None and sched.attempts == 1
        assert v.retry_at == T0 + 2.0  # the existing instant, not now + backoff

    def test_server_retry_at_is_honoured_exactly_and_only_extends(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock, backoff=backoff(2.0, 10.0))
        g1 = _running(store, "a")
        v1 = coord.report("a", _signal(retry_at=T0 + 600.0), generation=g1)
        assert v1.retry_at == T0 + 600.0  # not clamped by the 10s backoff cap
        g2 = _running(store, "b")
        v2 = coord.report("b", _signal(retry_at=T0 + 300.0), generation=g2)
        assert v2.retry_at == T0 + 600.0  # an earlier reset never shortens
        g3 = _running(store, "c")
        v3 = coord.report("c", _signal(retry_at=T0 + 900.0), generation=g3)
        assert v3.retry_at == T0 + 900.0  # a later reset extends
        clock.advance(899.0)
        assert coord.tick() == []
        clock.advance(1.0)
        assert coord.tick() == ["a"]

    def test_a_headerless_failure_never_shortens_an_unexpired_server_retry_at(
        self, store: TaskStore, clock: Clock
    ) -> None:
        """A ramp batch fails item by item and not every failure carries a header:
        one member's stated reset is a floor the next member's headerless failure
        cannot pull back, or the scope retries against a refused dependency."""
        coord = _coord(store, clock, capacity=lambda: 3, backoff=backoff(2.0, 900.0))
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(4)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(), generation=gen)
        clock.advance(2.0)
        assert coord.tick() == ["t0"]  # the probe
        clock.advance(5.0)  # one wake_spacing later: the ramp batch
        assert coord.tick() == ["t1", "t2", "t3"]
        sched = coord.schedule(GH)
        assert sched is not None
        assert sched.phase == PHASE_RAMP and sched.in_flight == {"t0", "t1", "t2", "t3"}

        reset_at = clock.t + 500.0
        v_server = coord.report("t1", _signal(retry_at=reset_at), generation=_rewoken(store, "t1"))
        assert v_server.retry_at == reset_at
        assert sched.retry_at == reset_at and sched.server_retry_at == reset_at

        v_headerless = coord.report(
            "t2", _signal(kind=KIND_DEPENDENCY_UNAVAILABLE), generation=_rewoken(store, "t2")
        )
        assert v_headerless.retry_at == reset_at
        assert sched.retry_at == reset_at and sched.server_retry_at == reset_at
        # One wake wave, one attempt: the headerless member joins the instant the
        # first one set instead of spending a second probe on the same round.
        assert sched.attempts == 2

        # The instant the main chat and the GitHub monitors read off the schedule.
        register_coordinator(coord)
        try:
            assert shared_retry_at(GH) == reset_at
        finally:
            register_coordinator(None)

        clock.advance(499.0)
        assert coord.tick() == []
        clock.advance(1.0)
        assert coord.tick() == ["t1"]

    def test_an_expired_server_retry_at_stops_being_a_floor(
        self, store: TaskStore, clock: Clock
    ) -> None:
        """Expiry is the floor's implicit exit: once the stated reset has passed the
        ladder resumes, so a floor can never freeze a scope."""
        coord = _coord(store, clock)
        coord.report("a", _signal(retry_at=T0 + 50.0), generation=_running(store, "a"))
        sched = coord.schedule(GH)
        assert sched is not None and sched.server_retry_at == T0 + 50.0
        clock.advance(50.0)
        assert coord.tick() == ["a"]
        clock.advance(1.0)
        coord.report("a", _signal(), generation=_rewoken(store, "a"))
        assert sched.server_retry_at is None
        assert sched.retry_at == clock.t + coord.backoff_ceiling(sched.attempts)

    def test_recovered_clears_the_server_floor(self, store: TaskStore, clock: Clock) -> None:
        """``recovered()`` is the floor's explicit exit: the scope is due now and a
        later failure lands on the ladder, not back on the discarded reset."""
        coord = _coord(store, clock)
        coord.report("a", _signal(retry_at=T0 + 600.0), generation=_running(store, "a"))
        sched = coord.schedule(GH)
        assert sched is not None and sched.server_retry_at == T0 + 600.0
        assert coord.recovered(GH)
        assert sched.retry_at == T0 and sched.server_retry_at is None
        assert coord.tick() == ["a"]
        clock.advance(1.0)
        coord.report("a", _signal(), generation=_rewoken(store, "a"))
        assert sched.server_retry_at is None
        assert sched.retry_at == clock.t + coord.backoff_ceiling(sched.attempts)

    def test_a_held_floor_does_not_fail_the_scope_before_the_stated_reset(
        self, store: TaskStore, clock: Clock
    ) -> None:
        """On the shipped ladder (base 2s, cap 120s) a 3000s reset outlives more
        retries than the attempts cap allows: a lost floor spends every attempt on
        calls the dependency already refused and fails the waiters well inside the
        wall-clock budget they were given."""
        coord = _coord(store, clock, capacity=lambda: 2, backoff=backoff(2.0, 120.0))
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(3)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(), generation=gen)
        clock.advance(2.0)
        assert coord.tick() == ["t0"]  # the probe, now in flight
        reset_at = clock.t + 3000.0
        coord.report("t1", _signal(retry_at=reset_at), generation=gens["t1"])
        coord.report(
            "t0", _signal(kind=KIND_DEPENDENCY_UNAVAILABLE), generation=_rewoken(store, "t0")
        )
        sched = coord.schedule(GH)
        assert sched is not None and sched.retry_at == reset_at
        spent = sched.attempts

        while clock.t < reset_at:
            assert coord.next_deadline() == reset_at
            clock.advance(min(coord.backoff_max_secs, reset_at - clock.t))
            if clock.t < reset_at:
                assert coord.tick() == []
        assert sched.attempts == spent
        assert all(store.state_of(tid) != model.FAILED for tid in gens)
        assert len(coord.tick()) == 1


# ── fault isolation ───────────────────────────────────────────────────────────


class TestFaultIsolation:
    def test_throttled_scope_does_not_block_another_scope(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock, capacity=lambda: 4)
        g_gh = _running(store, "gh-task")
        g_br = _running(store, "bedrock-task")
        coord.report("gh-task", _signal(retry_at=T0 + 3600.0), generation=g_gh)
        coord.report("bedrock-task", _signal(scope=BEDROCK), generation=g_br)
        assert coord.scopes() == [GH, BEDROCK]
        clock.advance(2.0)
        assert coord.tick() == ["bedrock-task"]
        assert store.state_of("bedrock-task") == model.RETRY_WAIT  # claimable, re-dispatched
        assert store.state_of("gh-task") == model.WAITING_DEPENDENCY
        # The bedrock task completes: its scope is gone, GitHub's schedule untouched.
        coord.forget("bedrock-task")
        assert coord.scopes() == [GH]
        gh = coord.schedule(GH)
        assert gh is not None and gh.retry_at == T0 + 3600.0

    def test_failing_one_scope_fails_only_its_waiters(self, store: TaskStore, clock: Clock) -> None:
        coord = _coord(store, clock, wait_deadline_secs=100.0)
        g_gh = _running(store, "gh-task")
        g_br = _running(store, "bedrock-task")
        coord.report("gh-task", _signal(retry_at=T0 + 50.0), generation=g_gh)
        coord.report("bedrock-task", _signal(scope=BEDROCK, retry_at=T0 + 500.0), generation=g_br)
        # GitHub's probe keeps failing until its deadline passes.
        clock.advance(50.0)
        assert coord.tick() == ["gh-task"]
        clock.advance(51.0)
        gen = store.get("gh-task").generation  # type: ignore[union-attr]
        v = coord.report("gh-task", _signal(), generation=gen)
        assert v.outcome == "deadline"
        assert store.state_of("gh-task") == model.FAILED
        assert store.state_of("bedrock-task") == model.WAITING_DEPENDENCY
        assert coord.scopes() == [BEDROCK]


# ── staged wake ───────────────────────────────────────────────────────────────


class TestStagedWake:
    def test_probe_then_capacity_sized_batches_with_spacing(
        self, store: TaskStore, clock: Clock
    ) -> None:
        woken: list[str] = []
        coord = _coord(
            store, clock, capacity=lambda: 3, wake_spacing_secs=5.0, on_wake=woken.append
        )
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(8)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(retry_at=T0 + 10.0), generation=gen)
        clock.advance(10.0)
        assert coord.tick() == ["t0"]  # probe
        assert coord.tick() == []  # spacing not elapsed
        clock.advance(4.0)
        assert coord.tick() == []
        clock.advance(1.0)
        assert coord.tick() == ["t1", "t2", "t3"]  # ramp: capacity 3
        sched = coord.schedule(GH)
        assert sched is not None and sched.phase == PHASE_RAMP
        clock.advance(5.0)
        assert coord.tick() == ["t4", "t5", "t6"]
        clock.advance(5.0)
        assert coord.tick() == ["t7"]
        assert woken == [f"t{i}" for i in range(8)]
        # Every woken row is claimable again (``retry_wait``, due now) under a
        # NEW generation; ``running`` is written by whoever claims it.
        for tid, gen in gens.items():
            row = store.get(tid)
            assert row is not None and row.state == model.RETRY_WAIT and row.generation == gen + 1
            assert row.next_run_at is not None and row.next_run_at <= clock.t
            assert row.lease_owner is None
            assert len(_events(store, tid, EVENT_WAKE)) == 1

    def test_probe_completion_starts_the_ramp_early(self, store: TaskStore, clock: Clock) -> None:
        coord = _coord(store, clock, capacity=lambda: 2, wake_spacing_secs=60.0)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(4)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(retry_at=T0 + 1.0), generation=gen)
        clock.advance(1.0)
        assert coord.tick() == ["t0"]
        coord.forget("t0")  # the probe finished its work: the dependency is back
        assert coord.tick() == ["t1", "t2"]
        clock.advance(60.0)
        assert coord.tick() == ["t3"]
        # Nothing left to wake; the scope lingers only to count a late probe
        # failure and is dropped once every woken task has finished.
        assert coord.waiters(GH) == []
        for tid in ("t1", "t2", "t3"):
            coord.forget(tid)
        assert coord.scopes() == []

    def test_scope_with_only_in_flight_tasks_ages_out(self, store: TaskStore, clock: Clock) -> None:
        coord = _coord(store, clock, backoff=backoff(2.0, 100.0))
        gen = _running(store, "a")
        coord.report("a", _signal(retry_at=T0 + 1.0), generation=gen)
        clock.advance(1.0)
        assert coord.tick() == ["a"]
        assert coord.scopes() == [GH]  # a silent finisher is kept until the cap passes
        clock.advance(100.0)
        coord.tick()
        assert coord.scopes() == [GH]
        clock.advance(1.0)
        coord.tick()
        assert coord.scopes() == []

    def test_wake_per_tick_overrides_capacity(self, store: TaskStore, clock: Clock) -> None:
        coord = _coord(store, clock, capacity=lambda: 100, wake_per_tick=2, wake_spacing_secs=1.0)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(5)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(retry_at=T0 + 1.0), generation=gen)
        clock.advance(1.0)
        assert coord.tick() == ["t0"]
        clock.advance(1.0)
        assert coord.tick() == ["t1", "t2"]
        clock.advance(1.0)
        assert coord.tick() == ["t3", "t4"]

    def test_recovered_signal_wakes_now_but_still_staged(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock, capacity=lambda: 2)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(3)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(retry_at=T0 + 3600.0), generation=gen)
        assert coord.recovered(GH)
        assert not coord.recovered("nope")
        assert coord.tick() == ["t0"]
        assert coord.next_deadline() == T0 + 5.0

    def test_reports_during_ramp_reset_the_scope(self, store: TaskStore, clock: Clock) -> None:
        coord = _coord(store, clock, capacity=lambda: 2, wake_spacing_secs=1.0)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(4)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(retry_at=T0 + 1.0), generation=gen)
        clock.advance(1.0)
        coord.tick()
        clock.advance(1.0)
        assert coord.tick() == ["t1", "t2"]
        gen = store.get("t2").generation  # type: ignore[union-attr]
        # t2 was re-dispatched (claimed -> running) before its probe failed again.
        reclaim = store.claim("t2", owner="w")
        assert reclaim is not None
        store.transition("t2", model.STARTING, generation=reclaim.generation)
        store.transition("t2", model.RUNNING, generation=reclaim.generation)
        gen = reclaim.generation
        coord.report("t2", _signal(), generation=gen)
        sched = coord.schedule(GH)
        assert sched is not None
        assert sched.phase == PHASE_WAITING and sched.attempts == 2
        assert set(sched.waiters) == {"t2", "t3"}
        assert store.state_of("t2") == model.WAITING_DEPENDENCY


# ── backoff and bounds ────────────────────────────────────────────────────────


class TestBackoffAndBounds:
    def test_jitter_is_bounded_by_the_doubling_ceiling_and_the_cap(self, clock: Clock) -> None:
        """The schedule is the recovery ladder's: equal jitter in ``[ceiling/2, ceiling]``,
        never a near-zero draw that would hot-loop a scope still down."""
        coord = DependencyCoordinator(
            None, clock=clock, rng=random.Random(7), backoff=backoff(2.0, 100.0)
        )
        for attempts in range(1, 12):
            ceiling = min(100.0, 2.0 * 2 ** (attempts - 1))
            assert coord.backoff_ceiling(attempts) == ceiling
            for _ in range(50):
                delay = coord.backoff_delay(attempts)
                assert ceiling / 2 <= delay <= ceiling
        assert coord.backoff_ceiling(1_000) == 100.0

    def test_default_schedule_is_the_recovery_ladders(self, clock: Clock) -> None:
        from kiro_crew.recovery.policy import DEFAULT_BACKOFF_BASE_SECS, DEFAULT_BACKOFF_MAX_SECS

        coord = DependencyCoordinator(None, clock=clock)
        assert coord.backoff_ceiling(1) == DEFAULT_BACKOFF_BASE_SECS
        assert coord.backoff_max_secs == DEFAULT_BACKOFF_MAX_SECS

    def test_attempts_cap_fails_every_waiter_with_the_reason(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock, max_attempts=2, wake_spacing_secs=0.0)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(3)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(), generation=gen)
        for _ in range(2):  # two failed probes exhaust max_attempts=2
            sched = coord.schedule(GH)
            assert sched is not None
            clock.t = sched.retry_at
            probe = coord.tick()
            assert len(probe) == 1
            gen = store.get(probe[0]).generation  # type: ignore[union-attr]
            v = coord.report(probe[0], _signal(), generation=gen)
        assert v.outcome == "deadline" and "attempts" in v.reason
        for tid in gens:
            assert store.state_of(tid) == model.FAILED
            failed = _events(store, tid, EVENT_FAILED)
            assert failed and failed[-1]["reason"] == v.reason
        assert coord.scopes() == []

    def test_wall_clock_deadline_fails_waiters_on_tick(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock, wait_deadline_secs=30.0)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(2)}
        for tid, gen in gens.items():
            coord.report(tid, _signal(retry_at=T0 + 60.0), generation=gen)
        # The WaitRecord carries the same deadline the coordinator enforces.
        record = WaitRecord.from_dict(store.get("t0").wait)  # type: ignore[union-attr]
        assert record is not None and record.deadline_at == T0 + 30.0
        clock.advance(60.0)
        assert coord.tick() == []
        for tid in gens:
            assert store.state_of(tid) == model.FAILED
        assert coord.scopes() == []

    def test_deadline_zero_means_attempts_cap_only(self, store: TaskStore, clock: Clock) -> None:
        coord = _coord(store, clock, wait_deadline_secs=0.0)
        gen = _running(store, "a")
        coord.report("a", _signal(retry_at=T0 + 10.0), generation=gen)
        record = WaitRecord.from_dict(store.get("a").wait)  # type: ignore[union-attr]
        assert record is not None and record.deadline_at is None
        clock.advance(1_000_000.0)
        assert coord.tick() == ["a"]


# ── terminal signals ──────────────────────────────────────────────────────────


class TestTerminalSignals:
    def test_auth_failed_goes_to_waiting_input_and_is_never_scheduled(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock)
        gen = _running(store, "a")
        v = coord.report("a", _signal(KIND_AUTH_FAILED), generation=gen)
        assert v.outcome == "terminal" and v.state == model.WAITING_INPUT
        row = store.get("a")
        assert row is not None and row.state == model.WAITING_INPUT
        record = WaitRecord.from_dict(row.wait)
        assert record is not None and record.resume_condition.key == f"auth:{GH}"
        assert coord.scopes() == []
        assert _events(store, "a", EVENT_FAILED)
        clock.advance(10_000.0)
        assert coord.tick() == []

    def test_auth_failed_on_a_non_running_row_fails(self, store: TaskStore, clock: Clock) -> None:
        coord = _coord(store, clock)
        gen = _starting(store, "a")
        v = coord.report("a", _signal(KIND_AUTH_FAILED), generation=gen)
        assert v.state == model.FAILED and store.state_of("a") == model.FAILED

    def test_permanent_param_error_and_quota_fail_at_once(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock)
        for tid, kind in (("p", KIND_PERMANENT_PARAM_ERROR), ("q", KIND_QUOTA_EXHAUSTED)):
            gen = _running(store, tid)
            v = coord.report(tid, _signal(kind), generation=gen)
            assert v.outcome == "terminal" and v.state == model.FAILED
            assert store.state_of(tid) == model.FAILED
        assert coord.scopes() == []

    def test_quota_with_reset_is_scheduled_at_the_reset(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock)
        gen = _running(store, "q")
        v = coord.report("q", _signal(KIND_QUOTA_EXHAUSTED, retry_at=T0 + 86_400.0), generation=gen)
        assert v.outcome == "wait" and v.retry_at == T0 + 86_400.0


# ── parking a row that is not running ────────────────────────────────────────


class TestParkedRows:
    def test_starting_row_parks_in_retry_wait_until_the_deadline(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock, wait_deadline_secs=600.0)
        gen = _starting(store, "a")
        v = coord.report("a", _signal(retry_at=T0 + 30.0), generation=gen)
        assert v.outcome == "wait"
        row = store.get("a")
        assert row is not None and row.state == model.RETRY_WAIT
        # Eligible only at the scope DEADLINE: the dispatcher cannot pre-empt the staged wake.
        assert row.next_run_at == T0 + 600.0
        assert store.fetch_dispatchable(model.KIND_SUBAGENT, limit=10) == []
        clock.advance(30.0)
        assert coord.tick() == ["a"]
        row = store.get("a")
        assert row is not None and row.state == model.QUEUED and row.next_run_at == T0 + 30.0
        assert [r.id for r in store.fetch_dispatchable(model.KIND_SUBAGENT, limit=10)] == ["a"]
        wake = _events(store, "a", EVENT_WAKE)
        assert wake and wake[-1]["to"] == model.QUEUED

    def test_a_parked_row_is_reported_as_parked_and_a_live_one_as_a_wait(
        self, store: TaskStore, clock: Clock
    ) -> None:
        """The verdict names WHERE the wait landed, because the two places behave
        differently: a ``waiting_dependency`` row keeps its runtime resident and is
        resumed, a ``retry_wait`` row holds nothing and is re-claimed."""
        coord = _coord(store, clock, wait_deadline_secs=600.0)
        parked = coord.report("p", _signal(), generation=_starting(store, "p"))
        assert parked.outcome == "wait" and parked.state == model.RETRY_WAIT
        assert store.state_of("p") == model.RETRY_WAIT
        live = coord.report("l", _signal(), generation=_running(store, "l"))
        assert live.outcome == "wait" and live.state == model.WAITING_DEPENDENCY
        assert store.state_of("l") == model.WAITING_DEPENDENCY


# ── restart ───────────────────────────────────────────────────────────────────


class TestRestart:
    def test_rebuild_restores_scopes_waiters_and_backoff(
        self, store: TaskStore, clock: Clock
    ) -> None:
        first = _coord(store, clock, capacity=lambda: 2)
        gens = {f"t{i}": _running(store, f"t{i}") for i in range(3)}
        for tid, gen in gens.items():
            first.report(tid, _signal(retry_at=T0 + 120.0), generation=gen)
        g_br = _running(store, "b")
        first.report("b", _signal(scope=BEDROCK), generation=g_br)
        g_p = _starting(store, "parked")
        first.report("parked", _signal(retry_at=T0 + 120.0), generation=g_p)
        # A second failed probe on bedrock raises its attempts before the crash.
        clock.advance(2.0)
        assert first.tick() == ["b"]
        first.report("b", _signal(scope=BEDROCK), generation=store.get("b").generation)  # type: ignore[union-attr]
        assert first.schedule(BEDROCK).attempts == 2  # type: ignore[union-attr]

        # "Restart": a fresh coordinator over the same store.
        second = _coord(store, clock, capacity=lambda: 2)
        assert second.scopes() == []
        assert second.rebuild() == 5
        assert second.scopes() == [GH, BEDROCK]
        gh = second.schedule(GH)
        assert gh is not None
        assert set(gh.waiters) == {"t0", "t1", "t2", "parked"}
        assert gh.retry_at == T0 + 120.0 and gh.since == T0 and gh.attempts == 1
        br = second.schedule(BEDROCK)
        assert br is not None and br.attempts == 2 and set(br.waiters) == {"b"}
        # The rebuilt schedule resumes the backoff rather than retrying at once.
        assert second.tick() == []
        clock.t = T0 + 120.0
        # Both scopes are due: each releases its own single probe.
        assert second.tick() == ["t0", "b"]
        assert store.state_of("t0") == model.RETRY_WAIT  # claimable; no live run owns it
        # A rebuild after the wake does not re-add the woken rows.
        third = _coord(store, clock)
        assert third.rebuild() == 3
        assert "t0" not in third.waiters(GH) and third.waiters(BEDROCK) == []

    def test_rebuild_restores_the_server_floor_with_the_scope(
        self, store: TaskStore, clock: Clock
    ) -> None:
        """The stated reset outlives the process that heard it: the floor rides on
        the rows with the instant, so a restart mid-throttle cannot turn an
        authoritative deadline back into a ladder delay."""
        reset_at = T0 + 500.0
        first = _coord(store, clock, capacity=lambda: 2)
        first.report(
            "a", _signal(kind=KIND_DEPENDENCY_UNAVAILABLE), generation=_running(store, "a")
        )
        first.report("b", _signal(retry_at=reset_at), generation=_running(store, "b"))
        sched = first.schedule(GH)
        assert sched is not None
        assert sched.retry_at == reset_at and sched.server_retry_at == reset_at
        # Each entry records the SCHEDULE's instants as of that report: a's
        # predates the reset, b's carries it, and the restore takes the latest.
        assert _events(store, "a", EVENT_WAIT)[-1]["server_retry_at"] is None
        assert _events(store, "b", EVENT_WAIT)[-1]["server_retry_at"] == reset_at

        second = _coord(store, clock, capacity=lambda: 2)
        assert second.rebuild() == 2
        restored = second.schedule(GH)
        assert restored is not None
        assert restored.retry_at == reset_at and restored.server_retry_at == reset_at
        assert second.public()[0]["server_retry_at"] == reset_at

        # A headerless failure on the far side of the restart still lands on the
        # floor, and the scope wakes nobody before the server said it would accept
        # work again.
        clock.advance(1.0)
        v = second.report(
            "c", _signal(kind=KIND_DEPENDENCY_UNAVAILABLE), generation=_running(store, "c")
        )
        assert v.retry_at == reset_at
        assert restored.retry_at == reset_at and restored.server_retry_at == reset_at
        clock.t = reset_at - 1.0
        assert second.tick() == []
        clock.advance(1.0)
        assert second.tick() == ["a"]
        # Expiry is still the floor's implicit exit, restored or not: the probe
        # failing at the stated reset resumes the ladder instead of freezing.
        second.report(
            "a", _signal(kind=KIND_DEPENDENCY_UNAVAILABLE), generation=_rewoken(store, "a")
        )
        assert restored.server_retry_at is None
        assert restored.retry_at == clock.t + second.backoff_ceiling(restored.attempts)

    def test_rebuild_of_a_row_written_without_the_floor_key_has_no_floor(
        self, store: TaskStore, clock: Clock
    ) -> None:
        """A wait entry from an incarnation that never wrote the key rebuilds with no
        floor at all -- the ladder is then the whole schedule."""
        first = _coord(store, clock)
        first.report("a", _signal(retry_at=T0 + 500.0), generation=_running(store, "a"))
        # The entry shape an incarnation without the floor key left on the row.
        store.append_event(
            "a",
            EVENT_WAIT,
            {
                **_signal(retry_at=T0 + 500.0).to_dict(),
                "retry_at": T0 + 500.0,
                "attempts": 1,
                "since": T0,
                "state": model.WAITING_DEPENDENCY,
            },
        )

        second = _coord(store, clock)
        assert second.rebuild() == 1
        sched = second.schedule(GH)
        assert sched is not None
        assert sched.retry_at == T0 + 500.0 and sched.server_retry_at is None
        clock.t = T0 + 500.0
        assert second.tick() == ["a"]
        clock.advance(1.0)
        second.report("a", _signal(), generation=_rewoken(store, "a"))
        assert sched.server_retry_at is None
        assert sched.retry_at == clock.t + second.backoff_ceiling(sched.attempts)

    def test_rebuild_ignores_rows_that_were_woken_or_failed(
        self, store: TaskStore, clock: Clock
    ) -> None:
        coord = _coord(store, clock, max_attempts=1)
        gen = _running(store, "a")
        coord.report("a", _signal(), generation=gen)
        clock.advance(2.0)
        assert coord.tick() == ["a"]
        coord.report("a", _signal(), generation=store.get("a").generation)  # type: ignore[union-attr]
        assert store.state_of("a") == model.FAILED
        fresh = _coord(store, clock)
        assert fresh.rebuild() == 0


# ── misc ──────────────────────────────────────────────────────────────────────


class TestMisc:
    def test_store_less_coordinator_keeps_a_schedule(self, clock: Clock) -> None:
        coord = DependencyCoordinator(
            None, clock=clock, rng=FixedRng(), backoff=backoff(1.0, 900.0)
        )
        v = coord.report("x", _signal())
        assert v.outcome == "wait" and v.retry_at == T0 + 1.0
        clock.advance(1.0)
        assert coord.tick() == ["x"]

    def test_events_carry_the_signal_and_schedule(self, store: TaskStore, clock: Clock) -> None:
        coord = _coord(store, clock)
        gen = _running(store, "a")
        coord.report("a", _signal(retry_at=T0 + 9.0), generation=gen)
        wait = _events(store, "a", EVENT_WAIT)
        assert wait and wait[-1]["kind"] == KIND_RATE_LIMITED
        assert wait[-1]["dependency_scope"] == GH and wait[-1]["retry_at"] == T0 + 9.0
        assert wait[-1]["attempts"] == 1 and wait[-1]["state"] == model.WAITING_DEPENDENCY
        assert coord.public()[0]["waiters"] == 1

    def test_coordinator_from_config_reads_agent_keys(self, store: TaskStore, clock: Clock) -> None:
        class Agent:
            recovery_backoff_base_secs = 3.0
            recovery_backoff_max_secs = 30
            dependency_max_attempts = 4
            dependency_wait_deadline_secs = 0
            dependency_wake_per_tick = 7
            dependency_wake_spacing_secs = 0.5

        coord = coordinator_from_config(store, Agent(), clock=clock)
        assert coord.backoff_ceiling(1) == 3.0
        assert coord.backoff_ceiling(100) == 30.0
        assert coord._wake_batch() == 7

    def test_default_agent_config_has_the_keys(self) -> None:
        from kiro_crew.config.sections import AgentConfig

        cfg = AgentConfig()
        # The dependency schedule is the recovery ladder's: no keys of its own.
        assert not hasattr(cfg, "dependency_backoff_base_secs")
        assert not hasattr(cfg, "dependency_backoff_max_secs")
        assert cfg.dependency_max_attempts == 20
        assert cfg.dependency_wait_deadline_secs == 3600
        assert cfg.dependency_wake_per_tick == 0
        assert cfg.dependency_wake_spacing_secs == 1.0


# ── D2 (overload experiment): infrastructure scopes are deadline-bounded ─────


def test_infra_scope_survives_more_probes_than_max_attempts() -> None:
    """An ``mcp_gateway:*`` scope keeps its waiters past ``max_attempts`` probes:
    its budget is the wall-clock deadline (a 20-minute gatewayd outage must be
    survived), one probe per backoff step, whole-scope. A provider scope with
    the same probe count still fails at the cap."""
    from kiro_crew.taskq import dependency as dep

    clock = [1000.0]
    coordinator = dep.DependencyCoordinator(
        None,
        clock=lambda: clock[0],
        max_attempts=3,
        wait_deadline_secs=3600.0,
        backoff=backoff(2.0, 2.0),
    )

    def _probe_cycle(scope: str, task_id: str) -> str:
        # Every report carries a ~2 s "server" retry_at, the pre-fix run.py shape.
        verdict = coordinator.report(
            task_id,
            dep.DependencySignal(
                kind=dep.KIND_DEPENDENCY_UNAVAILABLE,
                dependency_scope=scope,
                source="mcp_gateway",
                retry_at=clock[0] + 2.0,
            ),
        )
        clock[0] += 2.5
        coordinator.tick()  # wakes the probe (in_flight); the next report fails it
        return verdict.outcome

    assert dep.is_infra_scope("mcp_gateway:capacity")
    assert not dep.is_infra_scope("provider:acp")
    outcomes = [_probe_cycle("mcp_gateway:capacity", "run-1") for _ in range(12)]
    assert set(outcomes) == {"wait"}
    sched = coordinator.schedule("mcp_gateway:capacity")
    assert sched is not None and sched.attempts > 3
    # The deadline still ends it: same scope, 3600 s later.
    clock[0] += 3600.0
    late = coordinator.report(
        "run-1",
        dep.DependencySignal(
            kind=dep.KIND_DEPENDENCY_UNAVAILABLE,
            dependency_scope="mcp_gateway:capacity",
            source="mcp_gateway",
        ),
    )
    assert late.outcome == "deadline"

    provider_outcomes = [_probe_cycle("provider:acp", "run-2") for _ in range(6)]
    assert provider_outcomes[:3] == ["wait", "wait", "wait"]
    assert "deadline" in provider_outcomes


def test_a_refused_delegated_wake_puts_the_waiter_back_on_its_scope(
    store: TaskStore, clock: Clock
) -> None:
    """The worker path commits to delegation before the seam can answer.

    ``tick`` pops the waiter and hands ``_wake_through`` to the caller as a
    callback, so a refusal arrives a tick later on a waiter no schedule holds. If
    that refusal is dropped the row is never woken and nothing owns it: the live
    run waits on a resume event until its deadline cancels it. The inline arm gets
    this for free by testing the boolean; the deferred arm has to requeue.
    """
    generation = _running(store, "sa-1")
    answers = [False, True]
    asked: list[tuple[str, int | None]] = []

    def _wake_through(task_id: str, gen: int | None) -> bool:
        asked.append((task_id, gen))
        return answers.pop(0)

    coordinator = DependencyCoordinator(
        store,
        clock=clock,
        backoff=backoff(0.5, 0.5),
        rng=FixedRng(),
        wake_per_tick=1,
        wake_spacing_secs=0.5,
        wake_through=_wake_through,
    )
    coordinator.report("sa-1", _signal(), generation=generation)
    assert coordinator.schedule(GH) is not None

    # First due tick: the wake is delegated, then REFUSED when the callback runs.
    clock.t += 1.0
    callbacks: list = []
    assert coordinator.tick(
        callbacks=callbacks, live_waiters=frozenset({("sa-1", generation)})
    ) == ["sa-1"]
    for call in callbacks:
        call()
    assert asked == [("sa-1", generation)]

    sched = coordinator.schedule(GH)
    assert sched is not None, "the refusal dropped the scope with a waiter still owed a wake"
    assert "sa-1" in sched.waiters, "a refused delegated wake left the waiter held by nobody"
    assert "sa-1" not in sched.in_flight
    assert store.state_of("sa-1") == model.WAITING_DEPENDENCY

    # The next due tick wakes it again, and this time the seam takes it.
    clock.t += 2.0
    callbacks = []
    assert coordinator.tick(
        callbacks=callbacks, live_waiters=frozenset({("sa-1", generation)})
    ) == ["sa-1"]
    for call in callbacks:
        call()
    assert asked == [("sa-1", generation), ("sa-1", generation)]


# ── a write the store could not take ─────────────────────────────────────────


def test_a_wake_the_store_could_not_take_keeps_the_waiter_on_its_scope(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``TaskStoreUnavailable`` from the wake is an OUTAGE, never a statement
    about the row.

    Read as "the row was parked in ``retry_wait``" it draws a
    ``waiting_dependency -> queued`` write the transition table forbids, drops the
    refusal, and appends ``dependency_wake`` claiming the wake happened -- for a
    waiter ``tick`` has already popped and reported woken. The live row is then in
    a wait no schedule holds and nothing in this process will wake it. The waiter
    keeps its place instead, one spacing out.
    """
    generation = _running(store, "sa-1")
    coord = _coord(store, clock)
    coord.report("sa-1", _signal(), generation=generation)

    def _down(task_id: str, **kw: object) -> int | None:
        raise TaskStoreUnavailable("the store could not be reached")

    monkeypatch.setattr(store, "wake_wait", _down)
    clock.advance(2.0)
    assert coord.tick() == [], "an unwritten wake was reported as a wake"
    monkeypatch.undo()

    assert _events(store, "sa-1", EVENT_WAKE) == []
    kinds = [e.kind for e in store.events("sa-1")]
    assert (
        "rejected_transition" not in kinds
    ), "the forbidden waiting_dependency -> queued was tried"
    assert store.state_of("sa-1") == model.WAITING_DEPENDENCY
    sched = coord.schedule(GH)
    assert sched is not None and sched.waiters == {"sa-1": generation}
    assert not sched.in_flight
    assert sched.retry_at == clock.t + 5.0  # one wake_spacing out, as a refused wake is
    # The store comes back and the same waiter is woken for real.
    clock.advance(5.0)
    assert coord.tick() == ["sa-1"]
    assert store.state_of("sa-1") == model.RETRY_WAIT


def test_a_wake_the_store_refuses_drops_the_waiter_and_records_no_wake(
    store: TaskStore, clock: Clock
) -> None:
    """The store answering NO is not an outage: the row is gone, terminal, or held
    under another generation, so no tick of ours can wake it. Requeueing would
    retry a write that cannot land, and a ``dependency_wake`` event would tell the
    trail a wake happened.
    """
    generation = _running(store, "sa-2")
    coord = _coord(store, clock)
    coord.report("sa-2", _signal(), generation=generation)
    # The run is cancelled while parked: the wake and the retry_wait fallback are
    # both refused, and the store is entirely healthy while it refuses them.
    assert store.cancel("sa-2", reason="user stopped it") == model.WAITING_DEPENDENCY

    clock.advance(2.0)
    assert coord.tick() == []
    assert _events(store, "sa-2", EVENT_WAKE) == []
    assert store.state_of("sa-2") == model.CANCELLED
    assert coord.schedule(GH) is None, "a scope kept a waiter no wake can ever reach"


def test_a_wait_that_did_not_persist_is_not_reported_as_a_wait(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Verdict(outcome=wait)`` with no row behind it parks a live run on a wait
    nothing records: ``rebuild`` cannot restore it and no boot sweep sees it, while
    the row stays ``running`` with no runtime under it.

    The three outcomes of the wait write are answered apart -- entered, parked,
    nowhere -- so the caller can act on the one that is not a wait.
    """
    generation = _running(store, "sa-3")
    coord = _coord(store, clock)

    def _down(*_a: object, **_kw: object) -> object:
        raise TaskStoreUnavailable("the store could not be reached")

    monkeypatch.setattr(store, "enter_wait", _down)
    monkeypatch.setattr(store, "transition", _down)
    verdict = coord.report("sa-3", _signal(), generation=generation, from_state=model.RUNNING)
    monkeypatch.undo()

    assert verdict.outcome == "unpersisted"
    assert verdict.state == model.RUNNING, "the row is still where the caller reported from"
    assert verdict.retry_at is not None  # the caller may still honour the instant locally
    assert store.state_of("sa-3") == model.RUNNING
    assert _events(store, "sa-3", EVENT_WAIT) == [], "an event claimed a wait no row is in"
    assert coord.schedule(GH) is None, "the schedule held a waiter that is not waiting"
    assert _coord(store, clock).rebuild() == 0


def test_the_wake_event_names_the_state_the_row_actually_reached(
    store: TaskStore, clock: Clock
) -> None:
    """``WaitLedger.wake`` without a resident slot lands the row in ``retry_wait``,
    claimable under a new generation; only admission's own ``resume_grant`` writes
    ``running``. The event is the audit trail of the wake, so it names the state
    the row is in.
    """
    generation = _running(store, "sa-4")
    coord = _coord(store, clock)
    coord.report("sa-4", _signal(), generation=generation)
    clock.advance(2.0)
    assert coord.tick() == ["sa-4"]
    wake = _events(store, "sa-4", EVENT_WAKE)
    assert wake and wake[-1]["to"] == store.state_of("sa-4") == model.RETRY_WAIT


# ── the writer thread and the loop ───────────────────────────────────────────


def test_the_schedule_lock_is_never_held_across_a_store_write(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``report`` and ``tick`` run on the store's writer thread while the gateway
    loop edits and reads the same schedule (``forget``, ``recovered``, ``waiters``,
    ``next_deadline``, ``shared_retry_at``).

    One ``BEGIN IMMEDIATE`` waits up to ``BUSY_TIMEOUT_SECS`` for a competing
    writer, so a batch of writes under the schedule lock stalls the loop for that
    many timeouts -- and ``store.loop_thread_calls`` cannot show it, because the
    loop thread never reaches the store on this path. Probed rather than timed:
    the question is whether the lock is FREE during each write, which is an answer,
    not a duration.
    """
    coord = _coord(store, clock, capacity=lambda: 8, max_attempts=1)
    gens = {f"t{i}": _running(store, f"t{i}") for i in range(3)}
    seen: list[tuple[str, bool]] = []

    def _probing(name: str, real: object) -> object:
        def _wrapper(*a: object, **kw: object) -> object:
            answer: list[bool] = []

            def _ask() -> None:
                got = coord._lock.acquire(blocking=False)
                answer.append(got)
                if got:
                    coord._lock.release()

            asker = threading.Thread(target=_ask, name="loop-side-reader")
            asker.start()
            asker.join()
            seen.append((name, answer[0]))
            return real(*a, **kw)  # type: ignore[operator]

        return _wrapper

    for method in ("append_event", "enter_wait", "transition", "wake_wait", "finish"):
        monkeypatch.setattr(store, method, _probing(method, getattr(store, method)))

    for tid, gen in gens.items():
        coord.report(tid, _signal(), generation=gen)  # the wait writes
    clock.advance(2.0)
    assert coord.tick() == ["t0"]  # the wake write
    coord.report("t0", _signal(), generation=store.get("t0").generation)  # type: ignore[union-attr]
    monkeypatch.undo()

    assert store.state_of("t1") == model.FAILED  # the give-up writes really ran
    assert {name for name, _free in seen} >= {"append_event", "enter_wait", "wake_wait", "finish"}
    held = [name for name, free in seen if not free]
    assert held == [], f"the schedule lock was held across {held}"


@pytest.mark.asyncio
async def test_a_report_driven_give_up_releases_a_parked_run_on_the_loop(
    store: TaskStore, clock: Clock
) -> None:
    """A give-up reached through ``report`` hands its hooks to the LOOP.

    ``report`` runs on the store's writer thread (``store.run``), and after
    ``_fail_scope`` clears the schedule the give-up hooks are the ONLY releaser
    left for the scope's other waiters: they set loop-affine ``asyncio.Event``s (a
    sub-agent's ``_resume_event``, the runner admission's ``_wakes`` entry). Set
    from the writer thread, the loop is not woken to see it and the parked run
    waits for a grant nothing will hand it until its own timeout ends the turn.
    The thread is the pin, because a loop that happens to wake for another reason
    hides the lost set entirely.
    """
    loop = asyncio.get_running_loop()
    released = asyncio.Event()
    where: list[tuple[str, bool]] = []

    def on_fail(task_id: str, reason: str) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None  # type: ignore[assignment]
        where.append((task_id, running is loop))
        released.set()  # exactly what taskq_on_wait_failed and on_wake do

    coord = _coord(store, clock, wait_deadline_secs=30.0, on_fail=on_fail)
    victim = _running(store, "victim")
    reporter = _running(store, "reporter")
    coord.report("victim", _signal(retry_at=T0 + 600.0), generation=victim)
    coord.report("reporter", _signal(retry_at=T0 + 600.0), generation=reporter)
    assert sorted(coord.waiters(GH)) == ["reporter", "victim"]

    clock.advance(31.0)  # past the scope's wall-clock deadline
    parked = asyncio.create_task(asyncio.wait_for(released.wait(), timeout=10.0))
    verdict = await store.run(coord.report, "reporter", _signal(), generation=reporter)
    # Handed to the loop DURING the call, so it is queued ahead of this
    # coroutine's own resumption: the awaiting caller never sees a give-up whose
    # hooks are still owed.
    assert len(where) == 2, f"the give-up hooks had not run when report() returned: {where}"
    await parked

    assert verdict.outcome == "deadline"
    assert sorted(task_id for task_id, _on_loop in where) == ["reporter", "victim"]
    assert all(on_loop for _task_id, on_loop in where), (
        "a give-up hook ran off the loop its asyncio.Event belongs to: " f"{where}"
    )
    assert store.state_of("victim") == model.FAILED
    assert coord.scopes() == []
