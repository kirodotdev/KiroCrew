"""Tests for the two startup-pressure guards (subagent.py, admission/pump.py,
monitoring.py).

A fan-out is bounded by three different quantities. ``_max_concurrent`` bounds
how many agents RUN; ``_spawn_stagger_secs`` bounds the RATE of starts; and --
new here -- ``_startup_cap`` bounds how many admitted agents are IN STARTUP at
once (``_exec_started`` set, no runtime PID, no answer on its own session, no
turn -- plus a ``ClaimPoint`` reservation not yet registered; an agent parked at
the spawn-approval prompt is NOT counted, its release is metered by the pump).
Without the third bound one start was admitted per interval however long each
took, and under slow starts a wide fan-out piled dozens of agents into startup
together; the fixed 120s startup watchdog then reaped healthy starts as
``Failed to start within 120s`` (measured ~50% loss on a 120-item wave against
~2% at 24-45 items).

Guard A: ``_should_stagger_queue`` and the drain pump hold further spawns in the
EXISTING queue while the in-startup population is at ``_startup_cap``; the
queue wakes on a PID / first answer (``_note_startup_progress``) and on the
slot-release drain of a terminal, including the watchdog's reap of a wedged
start, so a wedged population cannot hold the queue past its reap.

The watchdog's deadline itself stays the fixed ``_startup_deadline`` whatever the
crowd; what moves is the clock, reset at ``SessionStartGate`` exit on both start
paths (``_gate_exit_reset``). The deadline is not pressure-aware -- see
``TestWatchdogIgnoresTheCrowd`` for the invariant -- which is also what keeps
the single-agent contract of ``test_subagent_startup_watchdog.py`` intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import mock_ctx, mock_sessions

# One spelling across the suite for a pid no platform can allocate: a fake pid a
# live process could own reaches whatever holds it on the runner when a cleanup
# path signals it (under pytest-xdist, a sibling worker).
from test_update_provider import _UNALLOCATABLE_PID

import kiro_crew.subagent as subagent_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.subagent import _STARTUP_CAP_GATE_ROUNDS, SubagentInfo, SubagentManager

# The end-to-end tests drive ``SubagentManager.spawn``, which refuses on a
# memory-pressured host; pin the host reading so the verdict is the test's.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# ── helpers ───────────────────────────────────────────────────────────────


def _manager(
    *, max_concurrent: int = 8, startup_timeout: int = 120, gate_width: int = 2
) -> SubagentManager:
    """A manager whose in-startup bound is ``2 x gate_width`` (clamped to the cap).

    The bound has no config key -- it is derived from the session-start gate's
    width alone -- so a test that wants a bound of 2 asks for a gate of 1.
    """
    mgr = SubagentManager(
        sessions=mock_sessions(),
        ctx_builder=mock_ctx(),
        max_concurrent=max_concurrent,
        startup_timeout=startup_timeout,
    )
    mgr._session_start_concurrency = gate_width
    return mgr


def _info(agent_id: str = "a1b2c3d4", **overrides) -> SubagentInfo:
    info = SubagentInfo(id=agent_id, task="t", agent="")
    for k, v in overrides.items():
        setattr(info, k, v)
    return info


def _starting(agent_id: str, exec_started: float | None = 100.0, **overrides) -> SubagentInfo:
    """An agent in startup: executing, but nothing to show for it yet."""
    return _info(agent_id, **{"_exec_started": exec_started, "_pid": None, "turns": 0, **overrides})


def _register(mgr: SubagentManager, *infos: SubagentInfo) -> None:
    for info in infos:
        mgr._agents[info.id] = info


# ── _in_startup: admitted with nothing to show yet ────────────────────────


class TestInStartupPredicate:
    def test_executing_with_nothing_to_show_is_in_startup(self) -> None:
        assert SubagentManager._in_startup(_starting("s1")) is True

    def test_queued_or_approval_parked_is_not_in_startup(self) -> None:
        """``_exec_started`` None: never entered ``_run_inner``. A queued spawn
        and an agent parked at the spawn-approval prompt are both starting
        nothing, so neither consumes the bound (a handful of unanswered
        prompts must not hold every other spawn on the host); the parked one is
        metered into startup by the pump when its prompt resolves instead."""
        assert SubagentManager._in_startup(_info(_exec_started=None, turns=0)) is False
        parked = _info(_exec_started=None, turns=0, _awaiting_approval=True)
        assert SubagentManager._in_startup(parked) is False
        assert _manager()._is_startup_stalled(parked, now=1e9) is False

    def test_a_reservation_counts_until_its_info_is_registered(self) -> None:
        """A durable-store spawn reserves its slot before its claim is awaited
        and registers only on re-entry, which skips the gate; the reservation
        stands in for the missing info so admissions decided in between see it."""
        mgr = _manager(max_concurrent=8, gate_width=1)  # bound 2
        mgr._spawn_stagger_secs = 0.0
        _register(mgr, _starting("a"))
        assert mgr._should_stagger_queue(time.monotonic())[0] is False
        mgr._startup_reservations = 1
        assert mgr._startup_population() == 2
        assert mgr._should_stagger_queue(time.monotonic())[0] is True
        mgr._admission.release_reservation("r")
        assert mgr._startup_reservations == 0
        assert mgr._should_stagger_queue(time.monotonic())[0] is False

    @pytest.mark.parametrize(
        "leaving",
        [
            {"_pid": _UNALLOCATABLE_PID},
            {"_first_stream_started": 101.0},
            # A mid-run TOOL prompt sets the same flag; the stream/turn it
            # arrived on keeps it out of the population.
            {"_first_stream_started": 101.0, "turns": 1, "_awaiting_approval": True},
            {"turns": 1},
            {"done": True},
            {"_reap_started": True},
        ],
    )
    def test_every_way_out_of_startup_leaves_the_population(self, leaving: dict) -> None:
        assert SubagentManager._in_startup(_starting("s1", **leaving)) is False

    def test_population_counts_only_registered_agents_in_startup(self) -> None:
        mgr = _manager()
        a, b, c = _starting("a"), _starting("b"), _starting("c", 50.0)
        c._pid = _UNALLOCATABLE_PID
        _register(mgr, a, b, c)
        assert mgr._startup_population() == 2
        assert mgr._startup_population(exclude=a) == 1
        # An unregistered info excludes nothing.
        assert mgr._startup_population(exclude=_starting("x")) == 2


# ── _startup_cap: configured, or derived from the running cap ─────────────


class TestStartupCap:
    @pytest.mark.parametrize(
        ("cap", "ssc", "expected"),
        [
            (8, 2, 4),  # 2 x gate width; the cap does not enter
            (40, 2, 4),
            (64, 2, 4),  # a cap-derived ceil(cap / 4) would give 16: 7 gate rounds queued
            (64, 4, 8),  # a wider gate admits a wider round
            (8, 4, 8),  # 2 x 4 = 8, clamped to the cap
            (3, 2, 3),  # 2 x 2 = 4 clamped to the cap of 3
            (1, 2, 1),
            (0, 2, 1),  # an adaptive squeeze to 0: the running cap pauses admission, not this
        ],
    )
    def test_derives_two_gate_rounds_clamped_to_the_cap(
        self, cap: int, ssc: int, expected: int
    ) -> None:
        mgr = _manager(max_concurrent=max(cap, 1), gate_width=ssc)
        mgr._max_concurrent = cap
        assert _STARTUP_CAP_GATE_ROUNDS == 2
        assert mgr._startup_cap() == expected

    def test_derived_bound_does_not_grow_with_the_cap(self) -> None:
        """The gate is the resource the bound rations, so the bound is a
        function of the gate's width alone: raising the cap must not queue more
        rounds of idle admitted starts behind the same permits."""
        mgr = _manager(max_concurrent=8, gate_width=2)
        bounds = []
        for cap in (8, 16, 40, 64, 128):
            mgr._max_concurrent = cap
            bounds.append(mgr._startup_cap())
        assert bounds == [4, 4, 4, 4, 4]

    def test_there_is_no_config_key_for_the_bound(self) -> None:
        """``2 x gate width`` is both floor and ceiling of the useful range, so
        an override could only make the bound worse; the operator's lever is
        ``agent.session_start_concurrency``, which the bound tracks. No
        ``agent.subagent_max_concurrent_startups`` key exists in the schema,
        the live-config list or the manager."""
        assert not hasattr(KiroCrewConfig().agent, "subagent_max_concurrent_startups")
        assert "agent.subagent_max_concurrent_startups" not in SubagentManager.LIVE_CONFIG_PATHS
        mgr = _manager(max_concurrent=40, gate_width=2)
        assert not hasattr(mgr, "_max_concurrent_startups_setting")
        # apply_limits leaves the derivation alone: a cap change re-clamps, nothing else.
        cfg = KiroCrewConfig()
        cfg.agent.max_subagents = 40
        mgr.apply_limits(cfg, max_concurrent=40)
        assert mgr._startup_cap() == 4
        mgr.apply_limits(cfg, max_concurrent=3)
        assert mgr._startup_cap() == 3


# ── Guard A at the gate: the third clause of _should_stagger_queue ────────


class TestGateHoldsAtTheStartupCap:
    def test_a_free_slot_is_still_held_while_startup_is_full(self) -> None:
        mgr = _manager(max_concurrent=8, gate_width=1)  # bound 2
        mgr._spawn_stagger_secs = 0.0
        _register(mgr, _starting("a"), _starting("b"))
        should_queue, slot_free = mgr._should_stagger_queue(time.monotonic())
        # Held -- and ``slot_free`` still tells the truth about the cap, so
        # the caller knows no running agent's exit is what will release this.
        assert (should_queue, slot_free) == (True, True)

    def test_one_agent_leaving_startup_opens_the_gate(self) -> None:
        mgr = _manager(max_concurrent=8, gate_width=1)  # bound 2
        mgr._spawn_stagger_secs = 0.0
        a, b = _starting("a"), _starting("b")
        _register(mgr, a, b)
        a._pid = _UNALLOCATABLE_PID
        should_queue, slot_free = mgr._should_stagger_queue(time.monotonic())
        assert (should_queue, slot_free) == (False, True)

    def test_a_wedged_agent_that_was_reaped_no_longer_counts(self) -> None:
        mgr = _manager(max_concurrent=8, gate_width=1)  # bound 2
        mgr._spawn_stagger_secs = 0.0
        wedged = _starting("w")
        _register(mgr, wedged, _starting("peer"))
        assert mgr._should_stagger_queue(time.monotonic())[0] is True
        wedged._reap_started = True  # the reaper's first write, before any await
        assert mgr._should_stagger_queue(time.monotonic())[0] is False


# ── Guard A end to end: the queue holds, wakes on progress, drains on a reap ─
#
# Every path from admission to ``_run`` is exercised here: the auto-approved
# ones (yolo / approval_mode="auto" / parent trust / hooks) start inside
# ``spawn()`` right after the gate, and the interactive one parks in
# ``_spawn_with_approval`` and, when its prompt resolves, re-enters through
# the pump (``_admit_released_start``) to be metered into startup -- the path
# a bulk trust/yolo grant releases all at once.


class _StartupRuns:
    """Patch ``_run`` with a run that ENTERS startup and then waits for the test.

    Each run marks ``_exec_started`` -- the real ``_run_inner``'s first
    statement -- and parks on a future; the test moves it out of startup
    (``progress``: a PID, the way a real start does) or ends it (``finish``).
    """

    def __init__(self) -> None:
        self.parked: dict[str, asyncio.Future] = {}
        self.started: list[str] = []
        runs = self

        async def _run(mgr: SubagentManager, info: SubagentInfo) -> None:
            info._exec_started = time.time()
            runs.started.append(info.id)
            fut = runs.parked.setdefault(info.id, asyncio.get_event_loop().create_future())
            await fut
            info.done = True
            info.result = "ok"
            mgr._claim_finalize(info)
            if mgr._release_slot(info):
                mgr._running_count -= 1
                mgr._drain_queue()

        self._patches = [patch.object(SubagentManager, "_run", new=_run)]

    def __enter__(self) -> "_StartupRuns":
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in self._patches:
            p.stop()

    def progress(
        self, mgr: SubagentManager, info: SubagentInfo, pid: int = _UNALLOCATABLE_PID
    ) -> None:
        live = mgr._agents[info.id]
        live._pid = pid
        mgr._note_startup_progress(live)

    async def finish(self, mgr: SubagentManager, info: SubagentInfo) -> None:
        fut = self.parked.setdefault(info.id, asyncio.get_event_loop().create_future())
        if not fut.done():
            fut.set_result("ok")
        await _settle()


async def _settle(rounds: int = 25) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def _spawn(mgr: SubagentManager, task: str) -> SubagentInfo:
    # One tick between admissions, as the stagger timer guarantees in
    # production: the admitted run's first step (``_exec_started``) lands
    # before the next spawn reads the population.
    info = mgr.spawn(task, parent_session_key="dash:pressure")
    assert info is not None
    await _settle()
    return info


async def _close(mgr: SubagentManager, runs: _StartupRuns) -> None:
    mgr._shutting_down = True
    for fut in runs.parked.values():
        if not fut.done():
            fut.set_result("ok")
    tasks = [t for t in mgr._tasks.values() if not t.done()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    mgr._taskq.close()


@pytest.mark.asyncio
async def test_queue_holds_at_the_startup_cap_and_resumes_on_progress() -> None:
    """Cap 8, gate width 1 (bound 2), four spawns: two start, two wait in the
    EXISTING queue -- no second queue -- and each leaves as one agent in
    startup records a PID."""
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        await mgr.wait_taskq_ready()
        try:
            first = await _spawn(mgr, "one")
            second = await _spawn(mgr, "two")
            third = await _spawn(mgr, "three")
            fourth = await _spawn(mgr, "four")

            assert runs.started == [first.id, second.id]
            assert third.queued and not third.done
            assert fourth.queued and not fourth.done
            assert mgr._startup_population() == 2
            assert mgr._running_count == 2  # the cap had six free slots; startup held them
            assert len(mgr._queue) == 2

            runs.progress(mgr, first)  # a runtime PID: out of startup
            await _settle()
            assert runs.started == [first.id, second.id, third.id]
            assert mgr._startup_population() == 2  # second + third
            assert len(mgr._queue) == 1

            runs.progress(mgr, second)
            await _settle()
            assert runs.started == [first.id, second.id, third.id, fourth.id]
            assert not mgr._queue
        finally:
            await _close(mgr, runs)


def _interactive(mgr: SubagentManager, pending: dict, trusted: list) -> None:
    """Route ``mgr`` through the REAL ``_spawn_with_approval``: no yolo, no
    parent trust, no hook auto-approve, and an ``on_spawn_approval`` that parks
    every prompt on a future in *pending* until ``trusted`` holds a truthy flag."""

    async def _prompt(request_id: str, description: str, parent_session_key: str = "") -> bool:
        if trusted:
            return True
        fut = asyncio.get_event_loop().create_future()
        pending[request_id] = fut
        return await fut

    mgr._sessions.get_approval_policy = MagicMock(return_value="")
    mgr._ctx_builder.hooks.auto_approve_subagent_spawn = False
    mgr._on_spawn_approval = _prompt


@pytest.mark.asyncio
async def test_ignored_prompts_do_not_block_an_unrelated_auto_approved_spawn() -> None:
    """Cap 8, gate width 1 (bound 2), FOUR prompted spawns nobody answers, then
    an auto-approved spawn (``approval_mode="auto"``) from another parent: it
    starts at once. A parked agent is starting nothing, so it consumes no
    startup slot and queues nobody -- the converse of the bulk-grant hole, and
    the failure a bound that counted parked agents self-inflicted: four
    unanswered prompts would have stalled every spawn on the host."""
    pending: dict[str, asyncio.Future] = {}
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        _interactive(mgr, pending, trusted=[])
        await mgr.wait_taskq_ready()
        try:
            parked = [await _spawn(mgr, f"prompted-{i}") for i in range(4)]
            assert len(pending) == 4  # all four admitted and parked, none queued
            assert all(mgr._agents[p.id]._awaiting_approval for p in parked)
            assert mgr._startup_population() == 0
            assert not mgr._queue

            auto = mgr.spawn("auto", parent_session_key="dash:other", approval_mode="auto")
            assert auto is not None
            await _settle()
            assert not auto.queued
            assert runs.started == [auto.id]
            assert mgr._startup_population() == 1
            # And the bound still meters the auto path itself.
            second = mgr.spawn("auto-2", parent_session_key="dash:other", approval_mode="auto")
            await _settle()  # one tick between admissions, as the stagger guarantees
            third = mgr.spawn("auto-3", parent_session_key="dash:other", approval_mode="auto")
            await _settle()
            assert runs.started == [auto.id, second.id]
            assert third.queued and not third.done
        finally:
            for fut in pending.values():
                if not fut.done():
                    fut.set_result(False)
            await _close(mgr, runs)


@pytest.mark.asyncio
async def test_bulk_approval_cannot_release_more_than_the_startup_cap() -> None:
    """The bulk-approval path, driven end to end through ``spawn()`` and the
    REAL ``_spawn_with_approval``: cap 8, gate width 1 (bound 2), four spawns
    that each need a human prompt. All four are admitted and park (a parked
    agent consumes no startup slot). Then every pending prompt is resolved in
    one pass, exactly as ``tool_approval:bulk_trust`` / ``bulk_yolo`` does:
    the four released starts re-enter through the pump and are METERED into
    startup -- two enter ``_run``, two wait in the existing queue, and each
    waiter is released as one agent in startup records a PID. Before the
    release was metered, all four entered ``_run`` together on the grant -- the
    wide-fan-out pile-up under bulk approval that the measurement came from."""
    pending: dict[str, asyncio.Future] = {}
    trusted: list = []
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        _interactive(mgr, pending, trusted)
        await mgr.wait_taskq_ready()
        try:
            infos = [await _spawn(mgr, f"prompted-{i}") for i in range(4)]
            first, second, third, fourth = infos
            assert runs.started == []  # nobody past the prompt yet
            assert sorted(pending) == sorted(f"spawn:{i.id}" for i in infos)
            assert all(mgr._agents[i.id]._awaiting_approval for i in infos)
            assert mgr._startup_population() == 0  # parked agents start nothing
            assert not mgr._queue
            assert mgr._running_count == 4  # they do hold their slots

            # The bulk grant: trust flips on and every pending prompt resolves at once.
            trusted.append(True)
            for fut in list(pending.values()):
                fut.set_result(True)
            await _settle()
            assert runs.started == [first.id, second.id]  # not four
            assert mgr._startup_population() == 2
            assert len(mgr._queue) == 2  # the released starts, waiting on the bound
            assert all(p.get("_startup_release") for p in mgr._queue)
            assert mgr._agents[third.id]._start_release is not None
            assert mgr._agents[third.id]._exec_started is None

            runs.progress(mgr, first)  # a runtime PID: out of startup, one slot opens
            await _settle()
            assert runs.started == [first.id, second.id, third.id]
            assert mgr._startup_population() == 2  # second + third
            assert len(mgr._queue) == 1
            runs.progress(mgr, second)
            await _settle()
            assert runs.started == [first.id, second.id, third.id, fourth.id]
            assert not mgr._queue
            assert all(mgr._agents[i.id]._start_release is None for i in infos)
        finally:
            await _close(mgr, runs)


@pytest.mark.asyncio
async def test_a_held_release_does_not_starve_a_queued_resume() -> None:
    """Gate width 1 (bound 2). Two starts fill the bound; a third, released
    from its prompt, is HELD by the pump. A running agent (past startup) then
    yields its lane slot and asks to resume: a resume waits on a slot, never
    on the startup bound, so the pump must grant it on the same pass that
    holds the release -- a phase that ended the pass on its hold would leave
    the resume parked for as long as the bound stays full."""
    from kiro_crew.taskq import WaitRecord
    from kiro_crew.taskq import model as taskq_model

    pending: dict[str, asyncio.Future] = {}
    trusted: list = []
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        _interactive(mgr, pending, trusted)
        mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
        await mgr.wait_taskq_ready()
        try:
            worker = await _spawn(mgr, "worker")
            pending.pop(f"spawn:{worker.id}").set_result(True)
            await _settle()
            assert runs.started == [worker.id]
            runs.progress(mgr, worker)  # a PID: running, out of startup
            live = mgr._agents[worker.id]
            assert mgr._startup_population() == 0

            a, b, c = [await _spawn(mgr, f"p-{i}") for i in range(3)]
            assert len(pending) == 3 and not mgr._queue  # all parked, none counted
            trusted.append(True)  # the bulk grant
            for fut in list(pending.values()):
                fut.set_result(True)
            await _settle()
            assert runs.started == [worker.id, a.id, b.id]
            assert mgr._startup_population() == 2  # the bound is full
            assert [p.get("_startup_release") for p in mgr._queue] == [True]  # c is held

            # The worker yields its lane slot for a wait, then its wake condition is met.
            store = mgr._admission.taskq_store()
            await store.run(store.transition, worker.id, taskq_model.RUNNING)
            record = WaitRecord.children([a.id], since=store.now())
            assert mgr._admission.yield_slot(live, record) is True
            await _settle()
            assert live._slot_released is True
            running_before = mgr._running_count
            assert mgr._admission.request_resume(live) is True
            await _settle()

            assert live._slot_released is False, "the resume was not granted"
            assert live._resume_pending is False
            assert mgr._running_count == running_before + 1
            # The release is still held, exactly where it was.
            assert [p.get("_startup_release") for p in mgr._queue] == [True]
            assert runs.started == [worker.id, a.id, b.id]
        finally:
            await _close(mgr, runs)


@pytest.mark.asyncio
async def test_a_released_start_that_is_stopped_while_waiting_never_runs() -> None:
    """Gate width 1 (bound 2), three prompted spawns, bulk grant: the third
    waits for the bound; a stop while it waits ends it without a run and drops
    its entry, so the pump never meters a run that already ended."""
    pending: dict[str, asyncio.Future] = {}
    trusted: list = []
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        _interactive(mgr, pending, trusted)
        mgr._sessions.reset = AsyncMock()
        mgr._sigkill_session = AsyncMock()  # type: ignore[method-assign]
        mgr._write_tombstone = MagicMock()  # type: ignore[method-assign]
        mgr._record_cost = MagicMock()  # type: ignore[method-assign]
        mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
        await mgr.wait_taskq_ready()
        try:
            first, second, third = [await _spawn(mgr, f"p-{i}") for i in range(3)]
            trusted.append(True)
            for fut in list(pending.values()):
                fut.set_result(True)
            await _settle()
            assert runs.started == [first.id, second.id]
            live = mgr._agents[third.id]
            assert live._start_release is not None and len(mgr._queue) == 1

            live.user_stopped = True
            await mgr._force_reap(third.id, live, 1.0, reason="user_stop")
            await _settle()
            assert live.done is True
            # A stop is a stop, not a rejection: the reap owns the record and
            # the release path writes nothing over it.
            assert live.user_stopped is True
            assert "spawn rejected" not in live.error
            assert not mgr._queue  # the entry went with it
            runs.progress(mgr, first)
            await _settle()
            assert runs.started == [first.id, second.id]  # never metered in
        finally:
            await _close(mgr, runs)


@pytest.mark.asyncio
async def test_a_reap_while_waiting_is_announced_as_a_reap_not_a_rejection() -> None:
    """Same wait, ended by the watchdog's reap instead of a stop: the error
    names the interrupted wait (the reap's own vocabulary), the spawn is never
    recorded as started, and no ``rejected`` audit is written."""
    pending: dict[str, asyncio.Future] = {}
    trusted: list = []
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        _interactive(mgr, pending, trusted)
        mgr._sessions.reset = AsyncMock()
        mgr._sigkill_session = AsyncMock()  # type: ignore[method-assign]
        mgr._write_tombstone = MagicMock()  # type: ignore[method-assign]
        mgr._record_cost = MagicMock()  # type: ignore[method-assign]
        mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
        mgr._log_spawned = MagicMock()  # type: ignore[method-assign]
        await mgr.wait_taskq_ready()
        try:
            first, second, third = [await _spawn(mgr, f"p-{i}") for i in range(3)]
            trusted.append(True)
            for fut in list(pending.values()):
                fut.set_result(True)
            await _settle()
            assert runs.started == [first.id, second.id]
            live = mgr._agents[third.id]
            assert live._start_release is not None
            # Only the two starts that actually ran are recorded as spawned.
            assert [c.args[0].id for c in mgr._log_spawned.call_args_list] == [first.id, second.id]

            with patch("kiro_crew.subagent.sel") as sel_mock:
                await mgr._force_reap(third.id, live, 300.0, reason="timeout")
                await _settle()
                audited = [
                    c.kwargs.get("outcome")
                    for c in sel_mock.return_value.log_tool_invocation.call_args_list
                ]
            assert live.done is True and live.reaped is True
            assert "waiting to be admitted into startup after spawn approval" in live.error
            assert "spawn rejected" not in live.error
            assert "rejected" not in audited
            assert [c.args[0].id for c in mgr._log_spawned.call_args_list] == [first.id, second.id]
        finally:
            await _close(mgr, runs)


@pytest.mark.asyncio
async def test_a_reap_names_the_admission_wait_even_when_the_pump_ends_it_mid_reap() -> None:
    """The reap's session teardown really yields (``sessions.reset`` waits on
    the registry lock under fan-out), and a pump pass inside that window ends
    the admission wait and clears ``_start_release``. The reap's record still
    names the interrupted wait, because what it interrupts is read before its
    first await."""
    pending: dict[str, asyncio.Future] = {}
    trusted: list = []
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        _interactive(mgr, pending, trusted)
        mgr._sigkill_session = AsyncMock()  # type: ignore[method-assign]
        mgr._write_tombstone = MagicMock()  # type: ignore[method-assign]
        mgr._record_cost = MagicMock()  # type: ignore[method-assign]
        mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
        mgr._log_spawned = MagicMock()  # type: ignore[method-assign]
        await mgr.wait_taskq_ready()
        try:
            first, second, third = [await _spawn(mgr, f"q-{i}") for i in range(3)]
            trusted.append(True)
            for fut in list(pending.values()):
                fut.set_result(True)
            await _settle()
            assert runs.started == [first.id, second.id]
            live = mgr._agents[third.id]
            assert live._start_release is not None
            during: dict[str, object] = {}

            async def _reset(_key: str) -> None:
                mgr._drain_queue()
                await _settle()
                during["start_release"] = live._start_release

            mgr._sessions.reset = _reset
            await mgr._force_reap(third.id, live, 300.0, reason="timeout")
            await _settle()
            # The window this test is about: the pump ended the wait mid-reap.
            assert during["start_release"] is None
            assert live.done is True and live.reaped is True
            assert "waiting to be admitted into startup after spawn approval" in live.error
            assert [c.args[0].id for c in mgr._log_spawned.call_args_list] == [first.id, second.id]
        finally:
            await _close(mgr, runs)


@pytest.mark.asyncio
async def test_a_stop_during_the_prompt_is_not_turned_into_a_rejection() -> None:
    """A user stop lands while the spawn prompt is open; inside the stop's
    session teardown the gateway closes admission and the prompt resolves. The
    stop owns the record: the release path answers ``"ended"`` before it looks
    at the admission gate, so no rejection is written over a neutral stop."""
    pending: dict[str, asyncio.Future] = {}
    trusted: list = []
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        _interactive(mgr, pending, trusted)
        mgr._sigkill_session = AsyncMock()  # type: ignore[method-assign]
        mgr._write_tombstone = MagicMock()  # type: ignore[method-assign]
        mgr._record_cost = MagicMock()  # type: ignore[method-assign]
        mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
        mgr._log_spawned = MagicMock()  # type: ignore[method-assign]
        await mgr.wait_taskq_ready()
        try:
            only = await _spawn(mgr, "prompted")
            live = mgr._agents[only.id]
            prompt = pending.pop(f"spawn:{only.id}")

            async def _reset(_key: str) -> None:
                mgr._sessions.admission_closed = True
                prompt.set_result(True)
                await _settle()

            mgr._sessions.reset = _reset
            with patch("kiro_crew.subagent.sel") as sel_mock:
                live.user_stopped = True
                await mgr._force_reap(only.id, live, 1.0, reason="user_stop")
                await _settle()
                outcomes = [
                    c.kwargs.get("outcome")
                    for c in sel_mock.return_value.log_tool_invocation.call_args_list
                ]
            assert live.done is True
            assert "rejected" not in outcomes
            assert "spawn rejected" not in (live.error or "")
            assert runs.started == []
            mgr._log_spawned.assert_not_called()
        finally:
            mgr._sessions.admission_closed = False
            await _close(mgr, runs)


@pytest.mark.asyncio
async def test_a_temporary_start_reaped_while_waiting_leaves_nothing_on_disk() -> None:
    """A non-persistent spawn is recorded (its live-run state seeded) only once
    it is admitted, so a reap while it waits for the bound finds neither that
    state nor a ``state.json``: the run's own mode is what keeps its tombstone
    off the disk. The real ``_write_tombstone`` runs here."""
    from kiro_crew.subagent_persistence import _agent_dir

    pending: dict[str, asyncio.Future] = {}
    trusted: list = []
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        _interactive(mgr, pending, trusted)
        mgr._memory_mode_for_session = lambda _key: "temporary"
        mgr._sessions.reset = AsyncMock()
        mgr._sigkill_session = AsyncMock()  # type: ignore[method-assign]
        mgr._record_cost = MagicMock()  # type: ignore[method-assign]
        mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
        await mgr.wait_taskq_ready()
        try:
            first, second, third = [await _spawn(mgr, f"t-{i}") for i in range(3)]
            trusted.append(True)
            for fut in list(pending.values()):
                fut.set_result(True)
            await _settle()
            assert runs.started == [first.id, second.id]
            live = mgr._agents[third.id]
            assert live.memory_mode == "temporary"
            assert live._start_release is not None
            await mgr._force_reap(third.id, live, 300.0, reason="timeout")
            await _settle()
            assert live.done is True and live.reaped is True
            assert not (_agent_dir(third.id) / "tombstone.json").exists()
            assert not _agent_dir(third.id).exists()
        finally:
            await _close(mgr, runs)


@pytest.mark.asyncio
async def test_admission_closed_at_release_refuses_the_approved_start() -> None:
    """The prompt resolves after the updater closed gateway admission: the
    start is refused with its own rejection (slot released, ``rejected`` /
    ``admission_closed`` audited, parent announced), is never recorded as
    spawned, and never runs."""
    pending: dict[str, asyncio.Future] = {}
    trusted: list = []
    announced: list = []

    async def _on_done(info):  # noqa: ANN001
        announced.append(info)

    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1)
        mgr._spawn_stagger_secs = 0.0
        _interactive(mgr, pending, trusted)
        mgr._on_done = _on_done
        mgr._log_spawned = MagicMock()  # type: ignore[method-assign]
        await mgr.wait_taskq_ready()
        try:
            only = await _spawn(mgr, "late")
            assert mgr._running_count == 1
            mgr._sessions.admission_closed = True
            with patch("kiro_crew.subagent.sel") as sel_mock:
                pending.pop(f"spawn:{only.id}").set_result(True)
                await _settle()
                reasons = [
                    c.kwargs.get("metadata", {}).get("reason")
                    for c in sel_mock.return_value.log_tool_invocation.call_args_list
                    if c.kwargs.get("outcome") == "rejected"
                ]
            live = mgr._agents[only.id]
            assert runs.started == []
            assert live.done is True and "gateway closed admission" in live.error
            assert reasons == ["admission_closed"]
            assert mgr._running_count == 0
            assert [a.id for a in announced] == [only.id]
            mgr._log_spawned.assert_not_called()
        finally:
            mgr._sessions.admission_closed = False
            await _close(mgr, runs)


@pytest.mark.asyncio
async def test_queue_drains_after_the_watchdog_reaps_a_wedged_start() -> None:
    """Gate width 1 (bound 2), two wedged starts and a waiter: the wedged
    pair holds the queue only until the watchdog reaps one of them -- the
    reap's slot-release drain admits the waiter, so the in-startup count
    cannot stick and deadlock the queue."""
    with _StartupRuns() as runs:
        mgr = _manager(max_concurrent=8, gate_width=1, startup_timeout=120)
        mgr._spawn_stagger_secs = 0.0
        await mgr.wait_taskq_ready()
        # Neuter the reap's process/tombstone collaborators, keep its queue drain.
        mgr._sessions.reset = AsyncMock()
        mgr._sigkill_session = AsyncMock()  # type: ignore[method-assign]
        mgr._write_tombstone = MagicMock()  # type: ignore[method-assign]
        mgr._record_cost = MagicMock()  # type: ignore[method-assign]
        mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
        try:
            wedged = await _spawn(mgr, "wedged")
            other = await _spawn(mgr, "other")
            waiter = await _spawn(mgr, "waiter")
            assert runs.started == [wedged.id, other.id]
            assert waiter.queued and not waiter.done

            live = mgr._agents[wedged.id]
            now = live._exec_started + 121.0
            assert mgr._is_startup_stalled(live, now) is True
            await mgr._force_reap(wedged.id, live, 121.0, reason="startup_timeout")
            await _settle()

            assert live.done is True
            assert "Failed to start within 120s" in live.error
            assert mgr._startup_population() == 2  # other + the waiter, now starting
            assert runs.started == [wedged.id, other.id, waiter.id]
            assert not mgr._queue
        finally:
            await _close(mgr, runs)


# ── The watchdog deadline is fixed; the crowd is diagnostics only ─────────
#
# The deadline is not pressure-aware (no term per OTHER agent in startup).
# With queue time uncharged (gate-exit reset) and the in-startup population
# bounded, there is no evidence that a healthy start misses the BASE deadline;
# and a term sampled at sweep time against ``now - _exec_started``, which spans
# the whole crowded period, would not be monotonic -- it would shrink as the
# crowd drained, so an agent inside its window at one sweep could be reaped at
# the next. These tests pin that invariant.


class TestWatchdogIgnoresTheCrowd:
    def test_lone_wedged_agent_is_reaped_at_the_base_deadline(self) -> None:
        mgr = _manager(startup_timeout=120)
        lone = _starting("lone", exec_started=1_000.0)
        _register(mgr, lone)
        assert mgr._is_startup_stalled(lone, now=1_000.0 + 120.0) is False
        assert mgr._is_startup_stalled(lone, now=1_000.0 + 120.5) is True

    def test_a_crowd_buys_a_wedged_agent_no_extra_time(self) -> None:
        """Four peers in startup: the deadline is still the base, not more."""
        mgr = _manager(startup_timeout=120)
        wedged = _starting("wedged", exec_started=1_000.0)
        _register(mgr, wedged, *[_starting(f"p{i}", 1_000.0) for i in range(4)])
        assert mgr._is_startup_stalled(wedged, now=1_000.0 + 120.0) is False
        assert mgr._is_startup_stalled(wedged, now=1_000.0 + 120.5) is True

    def test_a_draining_crowd_cannot_shorten_a_window_already_granted(self) -> None:
        """Monotonicity: inside the window at one sweep with peers present, the
        same agent must still be inside it at the next sweep after every peer
        has left -- what a crowd-sized deadline would violate."""
        mgr = _manager(startup_timeout=120)
        me = _starting("me", exec_started=1_000.0)
        peers = [_starting(f"p{i}", 1_000.0) for i in range(4)]
        _register(mgr, me, *peers)
        assert mgr._is_startup_stalled(me, now=1_000.0 + 119.0) is False
        for peer in peers:
            peer._pid = _UNALLOCATABLE_PID  # every peer leaves startup
        assert mgr._startup_population(exclude=me) == 0
        assert mgr._is_startup_stalled(me, now=1_000.0 + 119.5) is False
        assert mgr._is_startup_stalled(me, now=1_000.0 + 120.5) is True


@pytest.mark.asyncio
async def test_reaper_sweep_reaps_at_the_base_deadline_under_a_crowd(monkeypatch) -> None:
    """One real sweep of ``_reaper_loop`` with two agents in startup: the one
    past the base deadline is reaped under ``startup_timeout``; the one inside
    it -- under the same crowd -- is left alone. The reaper's warning names the
    fixed deadline and the in-startup population, and the error names the fixed
    deadline."""
    mgr = _manager(startup_timeout=120)
    wedged = _starting("wedged", exec_started=1_000.0)
    fresh = _starting("fresh", exec_started=1_000.0 + 100.0)
    _register(mgr, wedged, fresh)
    reaped: list[tuple[str, str]] = []
    swept = asyncio.Event()

    async def _force_reap(agent_id, info, elapsed, *, reason=""):
        reaped.append((agent_id, reason))
        info.done = True
        swept.set()

    mgr._force_reap = _force_reap  # type: ignore[method-assign]
    mgr._rebuild_conversation_registry = AsyncMock()  # type: ignore[method-assign]
    mgr._sample_live_costs = MagicMock()  # type: ignore[method-assign]
    mgr._sweep_stuck_waves_async = AsyncMock()  # type: ignore[method-assign]
    mgr._sweep_digest_holds_async = AsyncMock()  # type: ignore[method-assign]
    mgr._sweep_conversations = MagicMock()  # type: ignore[method-assign]
    mgr._taskq_pump = MagicMock()  # type: ignore[method-assign]
    mgr._maybe_flag_stall = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(subagent_mod, "_REAPER_INTERVAL", 0)
    monkeypatch.setattr(subagent_mod, "compact_cost_log", lambda: None)
    monkeypatch.setattr(subagent_mod, "prune_stale_tombstones", lambda *a, **k: 0)
    # +125s: past wedged's base deadline (a base/8-per-peer term would have
    # bought it 135s here), inside fresh's own window.
    monkeypatch.setattr(
        subagent_mod,
        "time",
        SimpleNamespace(time=lambda: 1_000.0 + 125.0, monotonic=time.monotonic),
    )

    loop_task = asyncio.ensure_future(mgr._reaper_loop())
    try:
        await asyncio.wait_for(swept.wait(), 5.0)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        mgr._taskq.close()

    assert reaped == [("wedged", "startup_timeout")]
    assert fresh.done is False


@pytest.mark.asyncio
async def test_reaper_sweep_reaps_a_runtime_that_never_answers_its_first_prompt(
    monkeypatch,
) -> None:
    """One real sweep: a start whose runtime is up (PID recorded) but whose
    first prompt got no frame for longer than ``_FIRST_PROMPT_SILENT_SECS`` is
    reaped under ``startup_timeout``; one still inside that window, and one
    whose stream already answered, are left alone."""
    from kiro_crew.subagent_manager.monitoring import _FIRST_PROMPT_SILENT_SECS

    now = 10_000.0
    mgr = _manager(startup_timeout=120)
    silent = _starting("silent", exec_started=now - 900.0, _pid=4242)
    silent.last_activity = now - _FIRST_PROMPT_SILENT_SECS - 1.0
    fresh = _starting("fresh", exec_started=now - 900.0, _pid=4243)
    fresh.last_activity = now - _FIRST_PROMPT_SILENT_SECS + 30.0
    answered = _starting(
        "answered", exec_started=now - 900.0, _pid=4244, _first_stream_started=now - 890.0
    )
    answered.last_activity = now - 890.0
    _register(mgr, silent, fresh, answered)
    reaped: list[tuple[str, str]] = []
    swept = asyncio.Event()

    async def _force_reap(agent_id, info, elapsed, *, reason=""):
        reaped.append((agent_id, reason))
        info.done = True
        swept.set()

    mgr._force_reap = _force_reap  # type: ignore[method-assign]
    mgr._rebuild_conversation_registry = AsyncMock()  # type: ignore[method-assign]
    mgr._sample_live_costs = MagicMock()  # type: ignore[method-assign]
    mgr._sweep_stuck_waves_async = AsyncMock()  # type: ignore[method-assign]
    mgr._sweep_digest_holds_async = AsyncMock()  # type: ignore[method-assign]
    mgr._sweep_conversations = MagicMock()  # type: ignore[method-assign]
    mgr._taskq_pump = MagicMock()  # type: ignore[method-assign]
    mgr._maybe_flag_stall = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(subagent_mod, "_REAPER_INTERVAL", 0)
    monkeypatch.setattr(subagent_mod, "compact_cost_log", lambda: None)
    monkeypatch.setattr(subagent_mod, "prune_stale_tombstones", lambda *a, **k: 0)
    monkeypatch.setattr(
        subagent_mod, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
    )

    loop_task = asyncio.ensure_future(mgr._reaper_loop())
    try:
        await asyncio.wait_for(swept.wait(), 5.0)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        mgr._taskq.close()

    assert reaped == [("silent", "startup_timeout")]
    assert fresh.done is False
    assert answered.done is False


# ── Leaving startup takes a produced event, not an opened stream ───────────


class TestStreamOpenIsNotProgress:
    @staticmethod
    def _sessions_with_stream(stream_factory) -> MagicMock:
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.context_used_tokens = MagicMock(return_value=0)
        provider.context_window_tokens = MagicMock(return_value=0)
        # Synchronous on the provider contract: as an AsyncMock child it would
        # hand back a coroutine nobody awaits.
        provider.mcp_session_report = MagicMock(return_value=None)
        provider.stream = MagicMock(side_effect=stream_factory)
        sessions = mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.record_success = MagicMock()
        return sessions

    @pytest.mark.asyncio
    async def test_a_stream_that_opens_and_yields_nothing_is_still_reaped(self) -> None:
        """A provider whose ``stream()`` is entered but never yields is a start
        that is not starting: it stays in startup (``_in_startup`` True, no
        first-stream stamp, no pump wake) and the watchdog reaps it at the base
        deadline. Stamping at stream open would let exactly this hang evade
        both."""
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.subagent_persistence import create_agent_folder

        opened = asyncio.Event()
        never = asyncio.get_event_loop().create_future()

        async def _hung_stream(*_a, **_k):
            opened.set()
            await never
            yield  # pragma: no cover -- unreachable

        sessions = self._sessions_with_stream(_hung_stream)
        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("message", None))
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx, startup_timeout=120)
        mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
        mgr._note_startup_progress = MagicMock()  # type: ignore[method-assign]
        info = _info("f00d1234", execution_context=execution_for_store(""))
        _register(mgr, info)
        await asyncio.to_thread(
            create_agent_folder, info.id, execution_context=info.execution_context
        )
        task = asyncio.ensure_future(mgr._run_inner(info, "subagent:f00d1234"))
        try:
            await asyncio.wait_for(opened.wait(), 5.0)
            await _settle()
            assert info._exec_started is not None
            assert info._first_stream_started is None
            assert SubagentManager._in_startup(info) is True
            mgr._note_startup_progress.assert_not_called()
            assert mgr._is_startup_stalled(info, now=info._exec_started + 120.5) is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_the_first_event_received_leaves_startup(self) -> None:
        """The counterpart: one event out of the stream stamps the marker,
        takes the run out of startup and wakes the pump once."""
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.subagent_persistence import create_agent_folder

        async def _one_event(*_a, **_k):
            yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

        sessions = self._sessions_with_stream(_one_event)
        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("message", None))
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx, startup_timeout=120)
        mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
        mgr._note_startup_progress = MagicMock()  # type: ignore[method-assign]
        info = _info("f00d5678", execution_context=execution_for_store(""))
        _register(mgr, info)
        await asyncio.to_thread(
            create_agent_folder, info.id, execution_context=info.execution_context
        )
        await mgr._run_inner(info, "subagent:f00d5678")
        assert info._first_stream_started is not None
        mgr._note_startup_progress.assert_called_once_with(info)

    @pytest.mark.asyncio
    async def test_a_completion_withheld_for_recovery_is_an_answer(self) -> None:
        """A recoverable completion as the stream's FIRST frame never reaches the
        loop that consumes the stream (it is withheld for in-place recovery),
        but it is the backend answering: the run leaves startup on it, before
        the recovery wait, not after."""
        from kiro_crew.acp.types import STOP_REASON_TOOL_STALL
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.subagent_persistence import create_agent_folder

        seen: dict[str, object] = {}
        calls: list[str] = []

        async def _stream(msg, *_a, **_k):
            calls.append(msg)
            if len(calls) == 1:
                yield SimpleNamespace(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_TOOL_STALL,
                    text="",
                    title="",
                    tool_input="",
                    runtime_global=False,
                )
                return
            yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

        sessions = self._sessions_with_stream(_stream)
        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("message", None))
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx, startup_timeout=120)
        mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
        mgr._note_startup_progress = MagicMock()  # type: ignore[method-assign]
        info = _info("f00dcafe", execution_context=execution_for_store(""))
        _register(mgr, info)

        async def _recovery(live, _event):
            seen["marker"] = live._first_stream_started
            seen["in_startup"] = SubagentManager._in_startup(live)
            return "continue"

        mgr._yield_for_stop_recovery = _recovery  # type: ignore[method-assign]
        await asyncio.to_thread(
            create_agent_folder, info.id, execution_context=info.execution_context
        )
        await mgr._run_inner(info, "subagent:f00dcafe")
        assert calls[1] == "continue"
        assert seen["marker"] is not None
        assert seen["in_startup"] is False
        mgr._note_startup_progress.assert_called_once_with(info)


# ── A PID ends startup before any stream, on every runtime-backed start ────
#
# The first-answer marker is the startup exit only for a start that publishes
# no runtime PID. Both AcpRuntime-backed paths record one before the stream
# opens -- the dedicated path from ``get_pid`` after ``get_or_create``, the
# shared path from ``runtime.pid`` at ``_bind_shared_handle`` -- so for them the
# startup window closes at the PID and never reaches the first frame of the
# turn. That ordering is what keeps the window a start budget rather than a
# first-token budget on these paths; pinned here so moving the PID record past
# the stream cannot pass silently.


class TestAPidEndsStartupBeforeTheStream:
    @pytest.mark.asyncio
    async def test_dedicated_start_is_out_of_startup_when_its_stream_opens(self) -> None:
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.subagent_persistence import create_agent_folder

        at_open: dict[str, object] = {}
        holder: dict[str, SubagentInfo] = {}

        async def _stream(*_a, **_k):
            live = holder["info"]
            at_open.update(
                pid=live._pid,
                marker=live._first_stream_started,
                in_startup=SubagentManager._in_startup(live),
            )
            yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

        sessions = TestStreamOpenIsNotProgress._sessions_with_stream(_stream)
        sessions.get_pid = MagicMock(return_value=_UNALLOCATABLE_PID)
        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("message", None))
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx, startup_timeout=120)
        mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
        mgr._note_startup_progress = MagicMock()  # type: ignore[method-assign]
        info = _info("f00d0001", execution_context=execution_for_store(""))
        holder["info"] = info
        _register(mgr, info)
        await asyncio.to_thread(
            create_agent_folder, info.id, execution_context=info.execution_context
        )
        await mgr._run_inner(info, "subagent:f00d0001")
        assert at_open == {"pid": _UNALLOCATABLE_PID, "marker": None, "in_startup": False}

    @pytest.mark.asyncio
    async def test_shared_start_is_out_of_startup_at_bind(self) -> None:
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.subagent_persistence import create_agent_folder

        mgr = _manager()
        mgr._note_startup_progress = MagicMock()  # type: ignore[method-assign]
        info = _starting("f00d0002", execution_context=execution_for_store(""))
        _register(mgr, info)
        await asyncio.to_thread(
            create_agent_folder, info.id, execution_context=info.execution_context
        )
        runtime = MagicMock()
        runtime.pid = _UNALLOCATABLE_PID
        handle = MagicMock()
        handle.session_id = "shared-session-1"
        handle.memory_mode = "persistent"
        assert SubagentManager._in_startup(info) is True
        await mgr._bind_shared_handle(info, "subagent:f00d0002", runtime, handle)
        assert info._pid == _UNALLOCATABLE_PID
        assert info._first_stream_started is None
        assert SubagentManager._in_startup(info) is False
        mgr._note_startup_progress.assert_called_once_with(info)


# ── A co-tenant's fanned-out frame is not this start's answer ─────────────


def _shared_runtime(work_dir) -> tuple[object, asyncio.StreamReader, list[dict]]:
    """A REAL ``AcpRuntime`` over in-memory pipes.

    A line fed to the returned reader goes through the real ``_reader_loop`` --
    routing by ``sessionId``, the ownerless broadcast and its ``fanout_no_owner``
    mark -- and every request the runtime writes is parsed into the list.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    runtime = AcpRuntime(work_dir=str(work_dir))
    reader = asyncio.StreamReader()
    written: list[dict] = []
    stdin = MagicMock()
    stdin.write = MagicMock(side_effect=lambda data: written.append(json.loads(data)))
    stdin.drain = AsyncMock()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = stdin
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    runtime._process = proc
    runtime._pid = _UNALLOCATABLE_PID
    runtime._initialized = True
    return runtime, reader, written


async def _until(predicate, what: str, ceiling: float = 10.0) -> None:
    """Wait on observable state, never on a guessed sleep."""
    deadline = time.monotonic() + ceiling
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"never observed: {what}")
        await asyncio.sleep(0.01)


class TestOnlyThisSessionsFrameIsItsAnswer:
    @pytest.mark.asyncio
    async def test_a_routed_roster_frame_is_the_sessions_own_answer(self) -> None:
        """Provenance, not event kind: the SAME roster kind reached through a
        routed frame (the KAS sub-agent lifecycle path) belongs to this session,
        so it takes the run out of startup and issues the ``running`` mark. An
        event-kind exclusion would keep this run in startup on its own
        progress."""
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_SUBAGENT_LIST
        from kiro_crew.subagent_persistence import create_agent_folder

        seen: dict[str, object] = {}
        holder: dict[str, SubagentInfo] = {}

        async def _stream(*_a, **_k):
            live = holder["info"]
            yield SimpleNamespace(kind=EVENT_SUBAGENT_LIST, subagents=[], runtime_global=False)
            seen.update(
                marker=live._first_stream_started,
                marked=live._taskq_running_marked,
                in_startup=SubagentManager._in_startup(live),
            )
            yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

        sessions = TestStreamOpenIsNotProgress._sessions_with_stream(_stream)
        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("message", None))
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx, startup_timeout=120)
        mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
        mgr._note_startup_progress = MagicMock()  # type: ignore[method-assign]
        info = _info("f00d0004", execution_context=execution_for_store(""))
        holder["info"] = info
        _register(mgr, info)
        await asyncio.to_thread(
            create_agent_folder, info.id, execution_context=info.execution_context
        )
        await mgr._run_inner(info, "subagent:f00d0004")
        assert seen["marker"] is not None
        assert seen["marked"] is True
        assert seen["in_startup"] is False
        mgr._note_startup_progress.assert_called_once_with(info)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fanned_out", ["roster", "mcp_registration", "compaction"])
    async def test_a_co_tenants_fanned_out_frame_neither_starts_nor_marks_the_run(
        self, tmp_path, fanned_out: str
    ) -> None:
        """Two sessions on ONE real runtime. The runtime fans an ownerless frame
        out to both -- the roster (``_kiro.dev/subagent/list_update``), an MCP
        registration naming no session (``_kiro.dev/mcp/server_initialized``) or
        a compaction notice naming none (``_kiro.dev/compaction/status``);
        the subagent's real ``AcpSessionHandle`` turns it into a
        ``runtime_global`` event of that kind and the real ``AcpSessionProvider``
        hands it to ``_run_inner``. That is another tenant's traffic: the run
        keeps its startup marker clear and issues no ``running`` mark on it. The
        first frame addressed to its own session does both. Both kinds are
        driven so a test on one event kind cannot stand in for the provenance
        test."""
        from kiro_crew.acp.session_handle import AcpSessionHandle
        from kiro_crew.acp.types import (
            METHOD_COMPACTION_STATUS,
            METHOD_MCP_SERVER_INITIALIZED,
            METHOD_SUBAGENT_LIST_UPDATE,
        )
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.subagent_persistence import create_agent_folder

        frame = {
            "roster": {"method": METHOD_SUBAGENT_LIST_UPDATE, "params": {"subagents": []}},
            "mcp_registration": {
                "method": METHOD_MCP_SERVER_INITIALIZED,
                "params": {"serverName": "other"},
            },
            "compaction": {
                "method": METHOD_COMPACTION_STATUS,
                "params": {"status": {"type": "completed"}},
            },
        }[fanned_out]

        runtime, reader, written = _shared_runtime(tmp_path)
        parent_q: asyncio.Queue = asyncio.Queue()
        sub_q: asyncio.Queue = asyncio.Queue()
        runtime._session_queues.update({"parent-sid": parent_q, "sub-sid": sub_q})
        handle = AcpSessionHandle("sub-sid", sub_q, runtime)

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("message", None))
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=ctx, startup_timeout=120)
        mgr._should_use_session_sharing = MagicMock(return_value=True)  # type: ignore[method-assign]
        mgr._note_startup_progress = MagicMock()  # type: ignore[method-assign]

        async def _create_shared(info, session_key, _agent):
            return await mgr._bind_shared_handle(info, session_key, runtime, handle)

        mgr._create_shared_session = _create_shared  # type: ignore[method-assign]
        info = _info("f00d0003", execution_context=execution_for_store(""))
        _register(mgr, info)
        await asyncio.to_thread(
            create_agent_folder, info.id, execution_context=info.execution_context
        )

        def _feed(frame: dict) -> None:
            reader.feed_data((json.dumps(frame) + "\n").encode())

        pump = asyncio.ensure_future(runtime._reader_loop())
        run = asyncio.ensure_future(mgr._run_inner(info, "subagent:f00d0003"))
        try:
            await _until(
                lambda: any(r.get("method") == "session/prompt" for r in written),
                "the subagent's session/prompt",
            )
            prompt_id = next(r["id"] for r in written if r.get("method") == "session/prompt")
            _feed(frame)
            # Delivered to BOTH sessions, and consumed off the subagent's queue.
            await _until(
                lambda: parent_q.qsize() == 1 and sub_q.empty(),
                "the frame fanned out to both sessions",
            )
            await asyncio.sleep(0)
            assert parent_q.get_nowait().fanout_no_owner is True
            assert info._first_stream_started is None
            assert info._taskq_running_marked is False
            # Counted for the startup reap's record.
            assert info._startup_cotenant_frames == 1
            # The one wake so far is the runtime PID recorded at bind.
            mgr._note_startup_progress.assert_called_once_with(info)

            _feed(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "sub-sid",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "working"},
                        },
                    },
                }
            )
            await _until(
                lambda: info._first_stream_started is not None,
                "the run's own frame taking it out of startup",
            )
            # Out of startup, frames are not counted.
            assert info._startup_cotenant_frames == 1
            assert info._taskq_running_marked is True
            _feed({"id": prompt_id, "result": {"stopReason": "end_turn"}})
            await asyncio.wait_for(run, timeout=10.0)
        finally:
            for task in (run, pump):
                task.cancel()
            await asyncio.gather(run, pump, return_exceptions=True)
        assert info.streaming_text == "working"


# ── Leaving startup wakes the queue: the held spawn starts on that edge alone ─
#
# ``_note_startup_progress`` is the only edge that announces a NON-terminal
# exit from the in-startup population: the slot-release drain fires on a
# terminal and the pump does not poll, so a spawn held by ``_startup_cap`` with
# free running slots has nothing else to move it. Losing one of its call sites
# is a silent hang at any fan-out wider than the bound, and no other test in
# this file would notice: the hold half of the bound is pinned above, and the
# resume tests pump the queue themselves. Each case here drives ONE of the
# three transitions through its real code path with a spawn queued behind a
# full bound and asserts the queued spawn starts with no pump call of the
# test's own -- observed while the driven start is still mid-execution, so a
# terminal's drain cannot be what moved it. One case per call site: removing
# that call must turn its case red while the other two stay green.


class TestLeavingStartupStartsTheHeldSpawn:
    """Gate width 1 (bound 2), cap 8: a peer fills one place in startup and the
    driven start the other; ``held`` waits in the queue with six free slots."""

    @staticmethod
    def _ctx() -> MagicMock:
        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("message", None))
        ctx.hooks.auto_approve_subagent_tools = False
        ctx.hooks.auto_approve_subagent_spawn = True
        return ctx

    @staticmethod
    async def _hold_one_behind_the_bound(
        mgr: SubagentManager, driven: SubagentInfo
    ) -> SubagentInfo:
        """Fill the bound with ``driven`` and a peer, then spawn one more: it is
        held by the bound -- the cap has room -- and nothing but a start
        leaving startup can release it."""
        from kiro_crew.subagent_persistence import create_agent_folder

        mgr._spawn_stagger_secs = 0.0
        mgr._session_start_concurrency = 1  # bound 2
        _register(mgr, driven, _starting("peer0001"))
        await asyncio.to_thread(
            create_agent_folder, driven.id, execution_context=driven.execution_context
        )
        await mgr.wait_taskq_ready()
        held = await _spawn(mgr, "held")
        assert held.queued and not held.done
        assert mgr._startup_population() == 2 and len(mgr._queue) == 1
        assert mgr._admission.capacity_view().any_slot  # held by the bound, not the cap
        return held

    @pytest.mark.asyncio
    async def test_the_dedicated_pid_record_starts_the_held_spawn(self) -> None:
        """The dedicated path records the child's PID before its stream opens,
        and that record alone starts ``held``: observed while the driven start's
        stream is open and has produced nothing, so neither a first frame nor a
        terminal can be what woke the pump."""
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.providers.base import EVENT_COMPLETE

        opened = asyncio.Event()
        release = asyncio.Event()

        async def _stream(*_a, **_k):
            opened.set()
            await release.wait()
            yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

        sessions = TestStreamOpenIsNotProgress._sessions_with_stream(_stream)
        sessions.get_pid = MagicMock(return_value=_UNALLOCATABLE_PID)
        with _StartupRuns() as runs:
            mgr = SubagentManager(
                sessions=sessions, ctx_builder=self._ctx(), max_concurrent=8, startup_timeout=120
            )
            mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
            driven = _starting("f00dd1c0", execution_context=execution_for_store(""))
            held = await self._hold_one_behind_the_bound(mgr, driven)
            run = asyncio.ensure_future(mgr._run_inner(driven, f"subagent:{driven.id}"))
            try:
                await asyncio.wait_for(opened.wait(), 5.0)
                assert driven._pid == _UNALLOCATABLE_PID
                assert driven._first_stream_started is None
                await _until(
                    lambda: runs.started == [held.id], "the held spawn starting on the PID record"
                )
                assert not mgr._queue
                assert mgr._startup_population() == 2  # the peer and ``held``
                release.set()
                await asyncio.wait_for(run, 10.0)
            finally:
                release.set()
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
                await _close(mgr, runs)

    @pytest.mark.asyncio
    async def test_the_first_frame_of_its_own_turn_starts_the_held_spawn(self) -> None:
        """A start that publishes no runtime PID (``get_pid`` answers nothing: a
        provider that creates its child lazily from ``stream()``) leaves startup
        on the first frame addressed to its session, and that frame alone starts
        ``held``. An open, silent stream starts nothing; the frame is observed
        with the stream still open, before the completion that ends the run."""
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_SUBAGENT_LIST

        opened = asyncio.Event()
        send_first = asyncio.Event()
        first_sent = asyncio.Event()
        release = asyncio.Event()

        async def _stream(*_a, **_k):
            opened.set()
            await send_first.wait()
            yield SimpleNamespace(kind=EVENT_SUBAGENT_LIST, subagents=[], runtime_global=False)
            first_sent.set()
            await release.wait()
            yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

        sessions = TestStreamOpenIsNotProgress._sessions_with_stream(_stream)
        assert sessions.get_pid() is None
        with _StartupRuns() as runs:
            mgr = SubagentManager(
                sessions=sessions, ctx_builder=self._ctx(), max_concurrent=8, startup_timeout=120
            )
            mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
            driven = _starting("f00dd1c1", execution_context=execution_for_store(""))
            held = await self._hold_one_behind_the_bound(mgr, driven)
            run = asyncio.ensure_future(mgr._run_inner(driven, f"subagent:{driven.id}"))
            try:
                await asyncio.wait_for(opened.wait(), 5.0)
                await _settle()
                assert driven._pid is None
                assert SubagentManager._in_startup(driven) is True
                assert (
                    runs.started == [] and len(mgr._queue) == 1
                )  # an opened stream is not progress
                send_first.set()
                await asyncio.wait_for(first_sent.wait(), 5.0)
                assert driven._first_stream_started is not None
                await _until(
                    lambda: runs.started == [held.id], "the held spawn starting on the first frame"
                )
                assert not mgr._queue
                release.set()
                await asyncio.wait_for(run, 10.0)
            finally:
                send_first.set()
                release.set()
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
                await _close(mgr, runs)

    @pytest.mark.asyncio
    async def test_the_shared_runtime_pid_record_starts_the_held_spawn(self) -> None:
        """The shared path records the runtime's PID as the handle is bound,
        before any prompt is sent, and that record alone starts ``held``."""
        from kiro_crew.execution_context import execution_for_store

        with _StartupRuns() as runs:
            mgr = _manager(max_concurrent=8, gate_width=1)
            driven = _starting("f00dd1c2", execution_context=execution_for_store(""))
            held = await self._hold_one_behind_the_bound(mgr, driven)
            runtime = MagicMock()
            runtime.pid = _UNALLOCATABLE_PID
            handle = MagicMock()
            handle.session_id = "shared-session-1"
            handle.memory_mode = "persistent"
            try:
                await mgr._bind_shared_handle(driven, f"subagent:{driven.id}", runtime, handle)
                assert driven._pid == _UNALLOCATABLE_PID
                assert driven._first_stream_started is None
                await _until(
                    lambda: runs.started == [held.id],
                    "the held spawn starting on the shared runtime's PID record",
                )
                assert not mgr._queue
            finally:
                await _close(mgr, runs)


# ── The start clock pauses while queued, on both start paths ───────────────
#
# ``_gate_wait_mark`` (queue entry) and ``_gate_exit_reset`` (permit granted)
# bracket every start-queue wait: the shared path's ``session/new`` gate, and the
# dedicated path's cold-start semaphore, spawn admission and gate (riding
# ``get_or_create`` -> provider factory -> ``AcpProvider``). The waits accumulate
# into ``_start_queue_wait_ms``, which the watchdog subtracts. ONE definition
# serves both paths.


class TestGateExitResetIsOneDefinition:
    def test_acquisition_pauses_the_clock_and_accumulates_the_wait(self, monkeypatch) -> None:
        mgr = _manager()
        info = _starting("a1", exec_started=100.0)
        info.last_activity = 100.0
        mark, reset = mgr._gate_wait_mark(info), mgr._gate_exit_reset(info)
        with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 100.0)):
            mark("cold-start")
        monkeypatch.setattr(subagent_mod, "time", SimpleNamespace(time=lambda: 250.0))
        reset(1.0, "cold-start")  # marked at 100.0, granted at 250.0 -> 150s
        reset(5_000.0)  # never marked: the queue's own measurement is the fallback
        assert info._exec_started == 100.0, "the clock pauses; it does not restart"
        assert info.last_activity == 250.0
        assert info._start_queue_wait_ms == 155_000.0

    def test_the_watchdog_does_not_count_queued_time(self) -> None:
        """Queue wait is admission's cost: a start that queued 200s is judged on
        the time it spent starting."""
        mgr = _manager(startup_timeout=120)
        info = _starting("a1", exec_started=1_000.0)
        _register(mgr, info)
        # Without the queued time, 200s past _exec_started reaps it.
        assert mgr._is_startup_stalled(info, now=1_000.0 + 200.0) is True
        with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 1_200.0)):
            mgr._gate_exit_reset(info)(200_000.0)
        assert mgr._is_startup_stalled(info, now=1_200.0 + 100.0) is False
        # A start wedged AFTER it holds its permit is still reaped at the base
        # deadline of time spent starting.
        assert mgr._is_startup_stalled(info, now=1_200.0 + 120.5) is True

    def test_the_clock_does_not_run_while_queued_for_a_permit(self) -> None:
        """A run 30s into its start that begins waiting for a permit reads 30s for
        as long as it waits, and resumes from 30s -- not from zero -- when served."""
        mgr = _manager(startup_timeout=120)
        info = _starting("q1", exec_started=1_000.0)
        _register(mgr, info)
        with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 1_030.0)):
            mgr._gate_wait_mark(info)()
        assert info._gate_wait_started == 1_030.0
        # 400s into the queue: the clock still reads 30s.
        assert mgr._is_startup_stalled(info, now=1_030.0 + 400.0) is False
        with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 1_430.0)):
            mgr._gate_exit_reset(info)(400_000.0)
        assert info._gate_wait_started is None
        assert mgr._is_startup_stalled(info, now=1_430.0 + 89.0) is False
        assert mgr._is_startup_stalled(info, now=1_430.0 + 90.5) is True

    def test_waits_at_three_queues_then_a_stall_is_reaped_at_the_right_total(self) -> None:
        """The dedicated path queues at the cold-start semaphore, the spawn
        admission and the gate; only the time between them counts."""
        mgr = _manager(startup_timeout=120)
        info = _starting("t3", exec_started=0.0)
        _register(mgr, info)
        clock = 0.0
        for queue, queued_for, worked_after in (
            ("cold-start", 300.0, 10.0),
            ("spawn admission", 200.0, 20.0),
            ("session/new", 100.0, 30.0),
        ):
            with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: clock)):
                mgr._gate_wait_mark(info)(queue)
            clock += queued_for
            with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: clock)):
                mgr._gate_exit_reset(info)(queued_for * 1000.0, queue)
            clock += worked_after
        # 600s queued, 60s worked: 60s more of a stalled initialize is the deadline.
        assert mgr._is_startup_stalled(info, now=clock + 59.0) is False
        assert mgr._is_startup_stalled(info, now=clock + 60.5) is True

    def test_a_start_wedged_before_the_gate_keeps_its_running_clock(self) -> None:
        """The pause covers exactly the wait for a permit: a start that has not
        reached a queue (a hung process spawn, say) is on a running clock and is
        reaped at the base deadline."""
        mgr = _manager(startup_timeout=120)
        info = _starting("w1", exec_started=1_000.0)
        _register(mgr, info)
        assert info._gate_wait_started is None
        assert mgr._is_startup_stalled(info, now=1_000.0 + 120.5) is True

    def test_a_start_parked_in_the_queues_past_the_cap_is_reaped_as_never_started(self) -> None:
        """The paused clock is bounded: a start parked behind holders no watchdog
        bounds (wedged cron runs, say) is not left waiting forever."""
        from kiro_crew.subagent_manager.monitoring import _START_QUEUE_MAX_SECS

        mgr = _manager(startup_timeout=120)
        info = _starting("p1", exec_started=1_000.0)
        _register(mgr, info)
        with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 1_010.0)):
            mgr._gate_wait_mark(info)("cold-start")
        monitor = mgr._monitor
        assert monitor._start_queue_saturated_secs(info, 1_010.0 + _START_QUEUE_MAX_SECS) == 0.0
        assert mgr._is_startup_stalled(info, now=1_010.0 + _START_QUEUE_MAX_SECS + 1) is False
        saturated = monitor._start_queue_saturated_secs(info, 1_010.0 + _START_QUEUE_MAX_SECS + 1)
        assert saturated > _START_QUEUE_MAX_SECS
        # A start that got going is not this case.
        info.turns = 1
        assert (
            monitor._start_queue_saturated_secs(info, 1_010.0 + 10 * _START_QUEUE_MAX_SECS) == 0.0
        )

    @pytest.mark.asyncio
    async def test_shared_path_uses_the_same_reset(self, tmp_path) -> None:
        """The shared path's callback IS ``_gate_exit_reset``'s: one convention."""
        mgr = _manager()
        runtime = MagicMock()
        runtime.create_session = AsyncMock()
        mgr._sessions.get_subagent_runtime = AsyncMock(return_value=runtime)
        mgr._get_parent_runtime = MagicMock(return_value=None)  # type: ignore[method-assign]
        mgr._bind_shared_handle = AsyncMock()  # type: ignore[method-assign]
        info = _starting("shared", exec_started=100.0)
        info.parent_session_key = "dash:p"
        _register(mgr, info)
        await mgr._create_shared_session(info, "subagent:shared", "")
        # The companion acquisition above paused the clock too, for a few
        # microseconds of real time.
        before = info._start_queue_wait_ms
        kwargs = runtime.create_session.await_args.kwargs
        with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 700.0)):
            kwargs["on_gate_queued"]()
        assert info._gate_wait_started == 700.0
        with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 777.0)):
            kwargs["on_gate_acquired"](5.0)
        # The pause is the span on the WATCHDOG's clock (mark -> grant), not the
        # queue's monotonic measurement, which a suspended host leaves short.
        assert info._exec_started == 100.0
        assert info._start_queue_wait_ms - before == 77_000.0
        assert info._gate_wait_started is None

    @pytest.mark.asyncio
    async def test_the_companion_acquisition_gets_this_starts_clock_pair(self) -> None:
        """A shared start with no parent runtime hands ``get_subagent_runtime`` its
        own clock pair, so that call pauses the clock at its WAITS only; the
        caller itself pauses nothing around the call."""
        mgr = _manager(startup_timeout=120)
        runtime = MagicMock()
        runtime.create_session = AsyncMock()
        info = _starting("companion", exec_started=1_000.0)
        info.parent_session_key = "dash:p"
        paused_during_call: list[bool] = []

        async def _companion(_parent, **kwargs):
            paused_during_call.append(info._gate_wait_started is not None)
            with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 1_010.0)):
                kwargs["on_gate_queued"]("companion runtime")
            assert info._gate_wait_started == 1_010.0
            with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 1_410.0)):
                kwargs["on_gate_acquired"](400_000.0, "companion runtime")
            return runtime

        mgr._sessions.get_subagent_runtime = AsyncMock(side_effect=_companion)
        mgr._get_parent_runtime = MagicMock(return_value=None)  # type: ignore[method-assign]
        mgr._bind_shared_handle = AsyncMock()  # type: ignore[method-assign]
        _register(mgr, info)
        await mgr._create_shared_session(info, "subagent:companion", "")
        assert paused_during_call == [False]
        assert info._gate_wait_started is None
        assert info._start_queue_wait_ms == 400_000.0

    @pytest.mark.asyncio
    async def test_a_hung_companion_spawn_is_reaped_at_the_startup_deadline(self) -> None:
        """A companion spawn wedged in its own work (an untimed step before
        ``initialize``) fires no queue callback, so the start clock keeps running
        and the watchdog reaps the start at its deadline -- not at the queue cap,
        whose text would blame other starts."""
        mgr = _manager(startup_timeout=120)
        info = _starting("hung", exec_started=1_000.0)
        info.parent_session_key = "dash:p"
        entered = asyncio.Event()

        async def _wedged(_parent, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        mgr._sessions.get_subagent_runtime = AsyncMock(side_effect=_wedged)
        mgr._get_parent_runtime = MagicMock(return_value=None)  # type: ignore[method-assign]
        _register(mgr, info)
        task = asyncio.ensure_future(mgr._create_shared_session(info, "subagent:hung", ""))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert info._gate_wait_started is None
            assert mgr._is_startup_stalled(info, now=1_000.0 + 120.5) is True
            assert mgr._monitor._start_queue_saturated_secs(info, 1_000.0 + 120.5) == 0.0
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


class TestDedicatedPathGateExitReset:
    """The dedicated path threads the reset through ``get_or_create``."""

    @staticmethod
    def _dedicated_sessions(captured: dict) -> MagicMock:
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.context_used_tokens = MagicMock(return_value=0)
        provider.context_window_tokens = MagicMock(return_value=0)

        async def stream(*_a, **_k):
            from kiro_crew.providers.base import EVENT_COMPLETE

            yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

        provider.stream = MagicMock(side_effect=stream)

        async def get_or_create(*_args, **kwargs):
            captured.update(kwargs)
            return provider, True, False

        sessions = mock_sessions()
        sessions.get_or_create = AsyncMock(side_effect=get_or_create)
        sessions.record_success = MagicMock()
        return sessions

    @pytest.mark.asyncio
    async def test_run_inner_hands_get_or_create_the_gate_exit_reset(self, monkeypatch) -> None:
        from kiro_crew.execution_context import execution_for_store
        from kiro_crew.subagent_persistence import create_agent_folder

        captured: dict = {}
        sessions = self._dedicated_sessions(captured)
        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("message", None))
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
        mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
        info = _info("d1d2d3d4", execution_context=execution_for_store(""))
        info.model = "gpt-5.6-sol"  # a model pin: the dedicated path by decision
        await asyncio.to_thread(
            create_agent_folder, info.id, execution_context=info.execution_context
        )

        await mgr._run_inner(info, "subagent:d1d2d3d4")

        reset = captured.get("on_gate_acquired")
        mark = captured.get("on_gate_queued")
        assert callable(reset) and callable(mark), captured.keys()
        with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 4_300.0)):
            mark()
        assert info._gate_wait_started == 4_300.0
        started = info._exec_started
        with patch.object(subagent_mod, "time", SimpleNamespace(time=lambda: 4_321.0)):
            reset(90_000.0, "spawn admission")
        assert info._exec_started == started
        # mark at 4300.0, grant at 4321.0: the watchdog's own 21s, not the 90s the
        # queue measured on a clock that stops when the host suspends.
        assert info._start_queue_wait_ms == 21_000.0
        assert info._gate_wait_started is None

    def test_provider_factory_names_the_kwarg_and_forwards_it(self, monkeypatch) -> None:
        """Named in ``_acp``, never swallowed by its ``**_kwargs`` catch-all."""
        import kiro_crew.providers.acp as acp_mod

        captured: list[dict] = []

        class _FakeProvider:
            def __init__(self, **kwargs: object) -> None:
                captured.append(kwargs)

        monkeypatch.setattr(acp_mod, "AcpProvider", _FakeProvider)
        factory = KiroCrewConfig().create_provider_factory()
        marker = lambda _ms: None  # noqa: E731
        queued = lambda: None  # noqa: E731
        factory("subagent:x", agent=None, on_gate_acquired=marker, on_gate_queued=queued)
        factory("dash:y", agent=None)
        assert captured[0]["on_gate_acquired"] is marker
        assert captured[0]["on_gate_queued"] is queued
        assert captured[1]["on_gate_acquired"] is None
        assert captured[1]["on_gate_queued"] is None

    @pytest.mark.asyncio
    async def test_acp_provider_forwards_the_reset_to_its_own_create_session(self) -> None:
        """The dedicated process's ``session/new`` gets the callback; a
        ``session/load`` resume takes no gate permit and gets none."""
        from kiro_crew.providers.acp import AcpProvider

        marker = lambda _ms: None  # noqa: E731
        queued = lambda: None  # noqa: E731
        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend="", on_gate_acquired=marker, on_gate_queued=queued)
        provider._client = MagicMock()
        provider._client.backend = ""
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_socket = None
        provider._client._model = "auto"
        provider._client._resume_session_id = ""
        handle = MagicMock()
        handle.session_id = "kiro-sess-1"
        handle.store_session_config = MagicMock()
        handle.set_model = AsyncMock()
        runtime = MagicMock()
        runtime.pid = _UNALLOCATABLE_PID
        runtime.spawn = AsyncMock()
        runtime.create_session = AsyncMock(return_value=handle)
        runtime.load_session = AsyncMock(return_value=handle)
        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda h, r, **kw: MagicMock(_handle=h, _runtime=r, resumed=False),
            ),
        ):
            await provider._start_kiro_runtime()
        runtime.create_session.assert_awaited_once()
        assert runtime.create_session.await_args.kwargs["on_gate_acquired"] is marker
        assert runtime.create_session.await_args.kwargs["on_gate_queued"] is queued
        runtime.load_session.assert_not_awaited()

    def test_a_provider_built_without_the_callback_passes_none(self) -> None:
        from kiro_crew.providers.acp import AcpProvider

        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend="")
        assert provider._on_gate_acquired is None
        assert provider._on_gate_queued is None
