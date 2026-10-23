"""AutoNudgeService tests: the bounds that end a loop.

``max_cycles``, the runtime budget and its clock, the over-ceiling load path, the recorded
stop reason and revival, and the stop sentinel's finish.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import svc_fixture  # noqa: F401

from kiro_crew import autonudge as _an
from kiro_crew import autonudge_authz as _autonudge_mod
from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    AUTONUDGE_STOP_REASON,
    CYCLE_CAP_REASON,
    MANUAL_STOP_REASON,
    RUNTIME_BUDGET_REASON,
    SESSION_START_FAILURE_REASON,
    STRUCTURAL_TERMINAL_REASON,
    AutoNudgeService,
    NudgeLoop,
    runtime_budget_exceeded,
)
from kiro_crew.monitoring.decision import monitor_budget_reason
from kiro_crew.monitoring.models import MonitorBudgets

#: Lost-run ceiling for synchronization the test itself must release. It is a
#: hang guard whose expiry fails at the awaited line, never a performance
#: assertion, and is at most half the repository's ``--timeout=120``.
_LOST_RUN_SECS = 30.0


@pytest.mark.asyncio
async def test_max_cycles_deactivates(svc, monkeypatch):
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_cycles=2)
    loop.cycle_count = 2  # simulate cap reached
    svc._save()
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    # _timer with cycle_count==max deactivates the loop (doesn't remove it).
    refreshed = svc._loops[loop.id]
    assert not refreshed.active


@pytest.mark.asyncio
async def test_max_cycles_emits_expired_event(svc, monkeypatch):
    """Hitting the cap must emit a distinct signal, not stop silently.

    Reaching ``max_cycles`` is a runaway backstop, not a finish: the loop
    stopped with its goal possibly unmet. Before this, the only trace was a log
    line plus an ``updated`` event indistinguishable from the user pressing
    Stop, so a capped-out loop looked the same as the agent stopping itself.
    """
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_cycles=2)
    loop.cycle_count = 2  # simulate cap reached
    svc._save()
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert ("expired", loop.id) in events, f"no expired event emitted; got {events}"
    # The loop is observed in its FINAL state: expired fires after the
    # deactivating update, so a subscriber never sees a still-active loop.
    assert not svc._loops[loop.id].active


@pytest.mark.asyncio
async def test_no_expired_event_on_manual_deactivate(svc, monkeypatch):
    """A user-initiated stop must NOT masquerade as cap exhaustion.

    ``expired`` drives a user-visible notification, so overloading it onto
    every deactivation would notify the user about their own Stop click.
    """
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_cycles=5)
    await svc.update(loop.id, active=False)  # manual pause, cap not reached
    assert "expired" not in events
    svc.stop()


@pytest.mark.asyncio
async def test_unlimited_loop_never_expires(svc, monkeypatch):
    """max_cycles=0 means unlimited — the cap branch must not fire at all."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    events: list[str] = []

    async def on_fire(_loop):
        return True

    svc._on_fire = on_fire
    svc.subscribe(lambda ev, lp: events.append(ev))
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_cycles=0)
    loop.cycle_count = 9999  # would trip any finite cap
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert "expired" not in events
    assert svc._loops[loop.id].active is True


def test_runtime_budget_exceeded_predicate():
    """Direct contract of the shared predicate: 0 = unlimited, missing
    created_ts never trips (no anchor to measure from), boundary is >=."""
    from kiro_crew.autonudge import runtime_budget_exceeded

    base = NudgeLoop(id="x", slot_key="s", message="m", created_ts=1000.0)
    # No budget → never exceeded, however old the loop is.
    base.max_runtime_secs = 0
    assert runtime_budget_exceeded(base, now=1e12) is False
    # Budget set, not yet elapsed.
    base.max_runtime_secs = 100
    assert runtime_budget_exceeded(base, now=1099.9) is False
    # Boundary: exactly spent counts as exceeded.
    assert runtime_budget_exceeded(base, now=1100.0) is True
    assert runtime_budget_exceeded(base, now=5000.0) is True
    # Malformed/legacy entry with no created_ts must never trip — guessing an
    # anchor could kill a healthy loop on its first post-upgrade cycle.
    orphan = NudgeLoop(id="y", slot_key="s", message="m", created_ts=0.0, max_runtime_secs=1)
    assert runtime_budget_exceeded(orphan, now=1e12) is False


class _BudgetClock:
    """Stands in for the model's ``time``: seconds pass only when the test says so."""

    def __init__(self) -> None:
        self._wall = 1_800_000_000.0
        self._mono = 5_000.0

    def time(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance(self, passed: float, *, step: float = 0.0) -> None:
        self._mono += passed
        self._wall += passed + step


@pytest.mark.parametrize(
    ("passed", "step", "spent"),
    [
        (5.0, 0.0, False),
        (5.0, 2 * 3600.0, False),
        (2 * 3600.0, 0.0, True),
        (2 * 3600.0, -3 * 3600.0, True),
    ],
    ids=["fresh", "fresh_forward_step", "spent", "spent_backward_step"],
)
def test_runtime_budget_is_measured_on_the_monotonic_clock(monkeypatch, passed, step, spent):
    """A wall-clock step while the loop runs neither spends its budget nor gives it back.

    Before the fix the budget was ``time.time() - created_ts``: a 2 h forward
    step stopped a loop armed seconds earlier as ``runtime_budget``, and a 3 h
    backward step let a loop 2 h into a 1 h budget buy more unattended turns.
    """
    from kiro_crew.autonudge_service import model

    clock = _BudgetClock()
    monkeypatch.setattr(model, "time", clock)
    loop = NudgeLoop(
        id="x", slot_key="s", message="m", created_ts=clock.time(), max_runtime_secs=3600
    )
    assert runtime_budget_exceeded(loop) is False
    clock.advance(passed, step=step)
    assert runtime_budget_exceeded(loop) is spent


def test_runtime_budget_counts_the_time_spent_before_a_load(monkeypatch):
    """The stored ``created_ts`` still carries the budget across a restart."""
    from kiro_crew.autonudge_service import model

    clock = _BudgetClock()
    monkeypatch.setattr(model, "time", clock)
    loop = NudgeLoop(
        id="x", slot_key="s", message="m", created_ts=clock.time() - 1800, max_runtime_secs=3600
    )
    assert runtime_budget_exceeded(loop) is False
    clock.advance(1801.0)
    assert runtime_budget_exceeded(loop) is True


def test_a_suspend_spends_no_runtime_budget_until_a_restart(monkeypatch):
    """The runtime budget is awake time while the gateway runs, and wall time across a restart.

    On Linux and macOS a host suspend moves the wall clock and not the monotonic one.
    A loop 30 min into a 1 h budget that sleeps 8 h wakes with its 30 min left; a
    restart after the same sleep reads the stored ``created_ts`` on the wall clock
    and finds the budget spent.
    """
    from kiro_crew.autonudge_service import model

    clock = _BudgetClock()
    monkeypatch.setattr(model, "time", clock)
    created = clock.time() - 1800
    loop = NudgeLoop(id="x", slot_key="s", message="m", created_ts=created, max_runtime_secs=3600)
    assert runtime_budget_exceeded(loop) is False
    clock.advance(0.0, step=8 * 3600.0)
    assert runtime_budget_exceeded(loop) is False, "the suspend spent the budget"
    restarted = NudgeLoop(
        id="x", slot_key="s", message="m", created_ts=created, max_runtime_secs=3600
    )
    assert runtime_budget_exceeded(restarted) is True, "the restart lost the time before it"


class _SteppedTime:
    """The ``time`` module with ``time()`` held at *base* plus ``step``; every other name is real.

    Bound as a module's own ``time``, never over the stdlib clock, so the event loop and
    every other module keep the real one. ``time()`` moves only when the test sets
    ``step``, so a test that measures from ``base`` reads no live clock.
    """

    def __init__(self, base: float) -> None:
        self.base = base
        self.step = 0.0

    def time(self) -> float:
        return self.base + self.step

    def __getattr__(self, name: str):
        return getattr(time, name)


def _held_wall_clock(monkeypatch) -> _SteppedTime:
    """Hold the wall clock the budget code reads at one base; the test steps it via ``step``."""
    from kiro_crew.autonudge_service import model, mutations

    clock = _SteppedTime(time.time())
    for module in (model, mutations):
        monkeypatch.setattr(module, "time", clock)
    return clock


@pytest.mark.parametrize("step", [0.0, -1200.0], ids=["no_step", "backward_step"])
@pytest.mark.asyncio
async def test_resume_re_anchors_a_budget_the_timer_reads_as_spent(svc, monkeypatch, step):
    """A Resume press on a loop past its budget gives it a fresh one, whatever the wall clock says.

    The loop was paused 65 minutes into a 60 minute budget. Read as
    ``time.time() - created_ts``, a 20 minute backward step makes that 45 minutes: the
    resume keeps the spent budget, and the timer stops the loop on its first tick.
    """
    loop = await svc.add("s1", "check the PR", idle_secs=600, max_runtime_secs=3600)
    try:
        clock = _held_wall_clock(monkeypatch)
        loop.created_ts = clock.base - 3900
        runtime_budget_exceeded(loop)  # the timer's read in this process
        paused_created_ts = loop.created_ts
        await svc.update(loop.id, active=False)
        clock.step = step

        resumed = await svc.update(loop.id, active=True, fresh_run=True)

        assert resumed is not None and resumed.active is True
        assert resumed.created_ts > paused_created_ts, "the resume kept the spent budget"
        assert runtime_budget_exceeded(resumed) is False
    finally:
        svc.stop()


@pytest.mark.parametrize("step", [0.0, 7200.0], ids=["no_step", "forward_step"])
@pytest.mark.asyncio
async def test_resume_keeps_the_budget_left_at_the_pause(svc, monkeypatch, step):
    """A loop paused with half its budget left resumes with that half, not a fresh budget.

    Read as ``time.time() - created_ts``, a 2 hour forward step makes the 30 minute old
    loop look spent, and the resume re-anchors it to a full hour.
    """
    loop = await svc.add("s1", "check the PR", idle_secs=600, max_runtime_secs=3600)
    try:
        clock = _held_wall_clock(monkeypatch)
        loop.created_ts = clock.base - 1800
        runtime_budget_exceeded(loop)  # the timer's read in this process
        await svc.update(loop.id, active=False)
        clock.step = step

        resumed = await svc.update(loop.id, active=True, fresh_run=True)

        assert resumed is not None and resumed.active is True
        # Measured from the held base, which the step does not move.
        left = resumed.max_runtime_secs - (clock.base - resumed.created_ts)
        assert 1700 < left < 1900
    finally:
        svc.stop()


class _HoldClock:
    """``time`` for the budget and both holds: seconds pass only when the test says so.

    ``monotonic()`` follows the time the test says really passed; ``time()``
    follows it too, plus any wall-clock step. Every other name is the real module's.
    """

    def __init__(self) -> None:
        self._wall = 1_800_000_000.0
        self._mono = 5_000.0

    def time(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance(self, passed: float, *, step: float = 0.0) -> None:
        self._mono += passed
        self._wall += passed + step

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.mark.parametrize("hold", ["approval", "person_wait"])
@pytest.mark.parametrize(
    ("before", "during"),
    [(0.0, 0.0), (2 * 3600.0, 0.0), (0.0, 2 * 3600.0), (0.0, -3 * 3600.0)],
    ids=[
        "no_step",
        "forward_step_before_hold",
        "forward_step_during_hold",
        "backward_step_during_hold",
    ],
)
@pytest.mark.asyncio
async def test_a_released_hold_hands_back_the_time_it_really_held(
    svc, monkeypatch, hold, before, during
):
    """A hold's release gives the budget back the time it held, whatever the wall clock did.

    The loop runs 20 min of a 40 min budget, is held 30 min, and is released: it
    has 20 min left. Read from the wall clock at the release, a 2 h forward step
    before the hold spends the budget, and a 3 h backward step during the hold
    (past its start, so the wall clock credits nothing) charges the 30 min held.
    Crediting the anchor with the wall-clock span instead would hand a forward step
    during the hold back as budget, so the loop must also be spent 20 min later.
    """
    from kiro_crew.autonudge_service import firing, model, timers

    loop = await svc.add("s1", "check the PR", idle_secs=600, max_runtime_secs=2400)
    try:
        clock = _HoldClock()
        for module in (model, timers, firing):
            monkeypatch.setattr(module, "time", clock)
        loop.created_ts = clock.time()
        assert runtime_budget_exceeded(loop) is False  # the timer's read in this process
        clock.advance(1200.0, step=before)
        if hold == "approval":
            await timers._record_approval_hold(svc, loop.slot_key)
            assert loop.approval_stalled is True
        else:
            assert await firing._commit_person_wait(svc, loop, held=True)
        clock.advance(1800.0, step=during)
        if hold == "approval":
            assert await svc.release_approval_hold(loop.slot_key, why="test", arm=False)
        else:
            assert await firing._commit_person_wait(svc, loop, held=False)

        assert runtime_budget_exceeded(loop) is False, "the release left the budget spent"
        clock.advance(1201.0)
        assert runtime_budget_exceeded(loop) is True, "the release handed back more than it held"
    finally:
        svc.stop()


@pytest.mark.parametrize(
    ("stopped", "raised", "age", "passed", "step", "spent"),
    [
        (None, None, 3300.0, 600.0, 0.0, True),
        (None, None, 3300.0, 600.0, -1200.0, True),
        (RUNTIME_BUDGET_REASON, 7200, 3900.0, 300.0, 7200.0, False),
    ],
    ids=["no_step", "backward_step", "forward_step"],
)
@pytest.mark.asyncio
async def test_a_failed_resume_write_leaves_the_budget_clock_as_it_was(
    svc, monkeypatch, stopped, raised, age, passed, step, spent
):
    """A Resume whose write fails leaves the runtime budget reading what it read before.

    The press on a budget the pause ran out, or that the timer stopped, re-anchors it,
    and the rollback of the failed write puts ``created_ts`` back. The budget's anchor
    is keyed to ``created_ts`` and kept outside the fields that rollback restores, so
    one left for the re-anchored value is not the loop's any more: the next read takes
    the spent time from the wall clock, with every step the wall clock took since the
    anchor it replaced. A 20 min backward step then hands back 20 min of a spent
    budget, and a 2 h forward step spends a budget that was raised past the loop's age.
    """
    from kiro_crew.autonudge import nudge_cycle_header
    from kiro_crew.autonudge_service import model, mutations

    loop = await svc.add("s1", "check the PR", idle_secs=600, max_runtime_secs=3600)
    try:
        clock = _HoldClock()
        for module in (model, mutations):
            monkeypatch.setattr(module, "time", clock)
        loop.created_ts = clock.time() - age
        runtime_budget_exceeded(loop)  # the timer's read in this process
        await svc.update(loop.id, active=False, stopped_reason=stopped)
        if raised:
            await svc.update(loop.id, max_runtime_secs=raised)
        paused_created_ts = loop.created_ts
        clock.advance(passed, step=step)
        header = nudge_cycle_header(loop)
        assert runtime_budget_exceeded(loop) is spent, "the setup must leave the case's verdict"

        real_write = svc._write_state

        def _refuse(_payload):
            raise OSError("disk full")

        monkeypatch.setattr(svc, "_write_state", _refuse)
        with pytest.raises(OSError):
            await svc.update(loop.id, active=True, fresh_run=True)

        assert loop.active is False and loop.created_ts == paused_created_ts
        assert nudge_cycle_header(loop) == header, "the failed press moved the clock"
        assert runtime_budget_exceeded(loop) is spent, "the failed press changed the verdict"

        monkeypatch.setattr(svc, "_write_state", real_write)
        resumed = await svc.update(loop.id, active=True, fresh_run=True)
        assert resumed is not None and resumed.active is True
        assert runtime_budget_exceeded(resumed) is False, "the press that landed kept it spent"
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_runtime_budget_deactivates_and_emits_expired(svc, monkeypatch):
    """A spent wall-clock budget stops the loop BEFORE it buys another turn,
    with the same terminal treatment as the cycle cap: deactivate (not
    remove) + ``expired`` so the user-visible notification fires."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))
    await svc.start()
    # _on_fire stays None during add() so the initially-armed (no-op sleep)
    # timer delivers nothing; drain it before wiring the counting callback.
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_runtime_secs=60)
    await svc._timers[loop.id]
    svc._on_fire = on_fire
    loop.created_ts = loop.created_ts - 120  # backdate: budget already spent
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert ("expired", loop.id) in events, f"no expired event emitted; got {events}"
    refreshed = svc._loops[loop.id]
    assert not refreshed.active
    assert fired == [], "a spent budget must not buy one more unattended turn"


@pytest.mark.asyncio
async def test_runtime_budget_unspent_fires_normally(svc, monkeypatch):
    """A loop within its budget behaves exactly like an unbudgeted one."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    svc._on_fire = on_fire
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_runtime_secs=86400)
    await svc._timers[loop.id]
    assert len(fired) == 1
    assert "expired" not in events
    assert svc._loops[loop.id].active is True


@pytest.mark.asyncio
async def test_runtime_budget_zero_is_unlimited(svc, monkeypatch):
    """max_runtime_secs=0 means unlimited — an arbitrarily old loop still fires."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    await svc.start()
    # _on_fire stays None during add() so the initially-armed (no-op sleep)
    # timer delivers nothing; drain it before wiring the counting callback.
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_runtime_secs=0)
    await svc._timers[loop.id]
    svc._on_fire = on_fire
    loop.created_ts = 1.0  # epoch-old loop
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert len(fired) == 1
    assert svc._loops[loop.id].active is True


@pytest.mark.asyncio
async def test_runtime_budget_persists_across_restart(tmp_path):
    """The budget must survive a gateway restart WITHOUT resetting its clock:
    both max_runtime_secs and the created_ts anchor round-trip the store."""
    svc1 = AutoNudgeService(base_dir=tmp_path)
    await svc1.start()
    loop = await svc1.add(slot_key="chat-1-123", message="go", idle_secs=15, max_runtime_secs=3600)
    created = svc1._loops[loop.id].created_ts
    svc1.stop()

    svc2 = AutoNudgeService(base_dir=tmp_path)
    await svc2.start()
    restored = svc2._loops[loop.id]
    assert restored.max_runtime_secs == 3600
    assert restored.created_ts == created
    svc2.stop()


@pytest.mark.asyncio
async def test_stopped_reason_records_why_and_clears_on_revival(svc, monkeypatch):
    """The store records WHY a loop deactivated: _timer's terminal bounds tag
    'cycle_cap'/'runtime_budget', a plain update(active=False) tags 'manual',
    and any revival clears the tag. This is what lets revival logic refuse to
    resume a manual pause whose budget has since elapsed."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    # runtime_budget: backdated loop trips the budget in _timer.
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_runtime_secs=60)
    await svc._timers[loop.id]
    svc._on_fire = None
    loop.created_ts -= 120
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert svc._loops[loop.id].stopped_reason == "runtime_budget"
    # Revival clears the tag (budget lifted in the same update so the re-armed
    # timer does not immediately re-trip under the no-op sleep).
    await svc.update(loop.id, active=True, max_runtime_secs=0)
    assert svc._loops[loop.id].stopped_reason == ""
    # Manual pause tags 'manual'.
    await svc.update(loop.id, active=False)
    assert svc._loops[loop.id].stopped_reason == "manual"
    # cycle_cap: cap-stopped loop tags 'cycle_cap'.
    loop2 = await svc.add(slot_key="chat-2-456", message="go", idle_secs=15, max_cycles=1)
    loop2.cycle_count = 1
    svc._cancel_timer(loop2.id)
    await svc._timer(loop2)
    assert svc._loops[loop2.id].stopped_reason == "cycle_cap"
    svc.stop()


@pytest.mark.asyncio
async def test_bound_deactivation_never_overwrites_a_manual_pause(svc):
    """RACE: user pauses right after the timer detects
    expiry — the timer's in-flight bound-tagged update must degrade to a
    no-op, not stamp 'runtime_budget' over the user's 'manual' (which would
    make the paused loop budget-revivable)."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    # User pause lands first.
    await svc.update(loop.id, active=False)
    assert svc._loops[loop.id].stopped_reason == "manual"
    # The timer's shielded update arrives second with the bound tag.
    await svc.update(loop.id, active=False, stopped_reason="runtime_budget")
    assert (
        svc._loops[loop.id].stopped_reason == "manual"
    ), "a terminal bound must never overwrite an existing deactivation"
    assert svc._loops[loop.id].active is False
    svc.stop()


@pytest.mark.asyncio
async def test_a_pause_landing_after_a_bound_keeps_the_bound(svc):
    """A reasonless ``active=False`` reaching a loop a bound already stopped is
    a repeat of an inactive state, not a new stop: "manual" over the bound
    would read as resumable and lose why the loop ended."""
    await svc.start()
    for slot, bound in (("chat-1-201", "runtime_budget"), ("chat-1-202", "cycle_cap")):
        loop = await svc.add(slot_key=slot, message="go", idle_secs=15)
        await svc.update(loop.id, active=False, stopped_reason=bound)
        paused = await svc.update(loop.id, active=False)
        assert svc._loops[loop.id].stopped_reason == bound
        assert svc._loops[loop.id].active is False
        assert paused is not None and paused.stopped_reason == bound
    running = await svc.add(slot_key="chat-1-203", message="go", idle_secs=15)
    await svc.update(running.id, active=False)
    assert svc._loops[running.id].stopped_reason == "manual"
    revived = await svc.update(loop.id, active=True)
    assert revived is not None and revived.active is True and revived.stopped_reason == ""
    svc.stop()


@pytest.mark.asyncio
async def test_revival_runs_a_fresh_budget_and_a_running_save_keeps_the_old_one(svc, monkeypatch):
    """The user's resume (a revival flagged ``fresh_run``) runs a bound-stopped
    loop again on a FRESH budget: the count restarts and the budget clock
    re-anchors on the revival, so the next tick fires instead of re-stopping on
    the still-spent bound and nothing has to be raised first. A save on a
    RUNNING loop is not a revival."""
    fired: list[str] = []

    async def on_fire(loop):
        fired.append(loop.id)
        return True

    async def _nosleep(_secs):
        return None

    svc._on_fire = on_fire
    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()

    async def settle(loop_id: str) -> None:
        # A loop the timer re-stops cancels its own task; the count below is the verdict.
        await asyncio.wait_for(
            asyncio.gather(svc._timers[loop_id], return_exceptions=True), timeout=_LOST_RUN_SECS
        )

    capped = await svc.add(slot_key="chat-1-301", message="go", idle_secs=15, max_cycles=1)
    await settle(capped.id)
    svc._cancel_timer(capped.id)
    await svc._timer(capped)
    assert svc._loops[capped.id].stopped_reason == "cycle_cap"
    assert fired.count(capped.id) == 1
    assert (await svc.update(capped.id, active=True, fresh_run=True)) is not None
    await settle(capped.id)
    assert fired.count(capped.id) == 2, "the resumed loop must fire again under the same cap"
    assert svc._loops[capped.id].active is True
    assert svc._loops[capped.id].cycle_count == 1

    budgeted = await svc.add(slot_key="chat-1-302", message="go", idle_secs=15, max_runtime_secs=60)
    await settle(budgeted.id)
    budgeted.created_ts -= 120
    svc._cancel_timer(budgeted.id)
    await svc._timer(budgeted)
    assert svc._loops[budgeted.id].stopped_reason == "runtime_budget"
    before_revival = _an.time.time()
    assert (await svc.update(budgeted.id, active=True, fresh_run=True)) is not None
    await settle(budgeted.id)
    assert fired.count(budgeted.id) == 2, "the resumed loop must fire again under the same budget"
    assert svc._loops[budgeted.id].active is True
    assert svc._loops[budgeted.id].created_ts >= before_revival

    running = await svc.add(slot_key="chat-1-303", message="go", idle_secs=15, max_cycles=3)
    await settle(running.id)
    created = svc._loops[running.id].created_ts
    saved = await svc.update(running.id, active=True, fresh_run=True, message="go on")
    assert saved is not None and saved.cycle_count == 1 and saved.created_ts == created
    svc.stop()


@pytest.mark.asyncio
async def test_a_bare_revival_keeps_the_count_so_a_reconciler_cannot_mint_a_fresh_allowance(
    svc, monkeypatch
):
    """A revival WITHOUT ``fresh_run`` -- the Research Lab and Issue Radar
    reconcilers re-arm any inactive loop of a live campaign or crew every few
    seconds, and a ``monitor_update`` bound raise asks for its increment -- keeps
    the loop's count and clock, so a cap-stopped loop is re-stopped unfired on
    its first tick exactly as before the resume reset existed: the user's cap
    stays a bound on unattended turns rather than becoming a per-run allowance."""
    fired: list[str] = []

    async def on_fire(loop):
        fired.append(loop.id)
        return True

    async def _nosleep(_secs):
        return None

    svc._on_fire = on_fire
    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()

    async def settle(loop_id: str) -> None:
        await asyncio.wait_for(
            asyncio.gather(svc._timers[loop_id], return_exceptions=True), timeout=_LOST_RUN_SECS
        )

    capped = await svc.add(slot_key="research-c1", message="go", idle_secs=15, max_cycles=1)
    await settle(capped.id)
    svc._cancel_timer(capped.id)
    await svc._timer(capped)
    assert svc._loops[capped.id].stopped_reason == "cycle_cap"
    created = svc._loops[capped.id].created_ts
    revived = await svc.update(capped.id, active=True)
    assert revived is not None and revived.cycle_count == 1 and revived.created_ts == created
    assert revived.stopped_reason == "", "a revival still clears the reason it is undoing"
    await settle(capped.id)
    assert fired.count(capped.id) == 1, "the still-spent cap must re-stop the loop unfired"
    assert svc._loops[capped.id].active is False
    assert svc._loops[capped.id].stopped_reason == "cycle_cap"
    svc.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "cycles", "budget", "count_zeroed", "clock_reanchored"),
    [
        (MANUAL_STOP_REASON, 7, 3600, False, False),
        (AUTONUDGE_STOP_REASON, 7, 0, False, False),
        (APPROVAL_STALL_REASON, 7, 0, False, False),
        (SESSION_START_FAILURE_REASON, 7, 0, False, False),
        (STRUCTURAL_TERMINAL_REASON, 7, 0, False, False),
        (CYCLE_CAP_REASON, 7, 0, True, False),
        (RUNTIME_BUDGET_REASON, 7, 0, False, True),
        # An agent-written row: the loader copies the field as stored, whatever its shape.
        pytest.param([CYCLE_CAP_REASON], 7, 0, False, False, id="malformed-list"),
        pytest.param(MANUAL_STOP_REASON, 7, "300", False, False, id="malformed-bounds"),
        # Paused by hand with a bound spent: the first tick would refuse it anyway, so
        # that bound's counter resets while the other keeps the breakpoint.
        pytest.param(MANUAL_STOP_REASON, 24, 0, True, False, id="paused-at-cap"),
        pytest.param(MANUAL_STOP_REASON, 7, 300, False, True, id="budget-elapsed-during-pause"),
        pytest.param(MANUAL_STOP_REASON, 24, 300, True, True, id="both-spent"),
    ],
)
async def test_the_users_resume_resets_only_the_counter_behind_a_spent_bound(
    svc, monkeypatch, reason, cycles, budget, count_zeroed, clock_reanchored
):
    """Play on a paused loop (the dashboard route's ``fresh_run`` revival through
    ``authorize_and_update_nudge``) picks the loop up where it stopped: a loop a
    person paused at cycle 7 of 24 comes back at cycle 7, not cycle 1, because
    the cap is a lifetime limit and a pause must not quietly mint a fresh
    allowance. Only a spent bound resets, and only its own counter: a spent cap
    (stopped on it, or the count reached it) zeroes the count; a spent time
    budget (stopped on it, or the clock ran it out while the loop sat paused)
    re-anchors the clock -- Play there is otherwise a dead press, re-stopped on
    its first tick unless the bound is raised first -- so an overnight pause at
    7 of 24 with an hour of budget comes back at 7 of 24 on a fresh clock. A
    reason or a bound that is not even the right type reads as "not spent"
    rather than raising after ``active`` has flipped. The loop was armed 600s
    ago in every row."""
    monkeypatch.setattr(
        _autonudge_mod, "sel", lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None)
    )
    well_typed = isinstance(budget, int)
    loop = await svc.add(
        slot_key="chat-1-304",
        message="go",
        idle_secs=15,
        max_cycles=24,
        max_runtime_secs=budget if well_typed else 0,
    )
    if not well_typed:
        loop.max_cycles, loop.max_runtime_secs = "24", budget
    loop.cycle_count = cycles
    loop.created_ts -= 600
    created = loop.created_ts
    if isinstance(reason, str):
        await svc.update(loop.id, active=False, stopped_reason=reason)
    else:
        await svc.update(loop.id, active=False)
        loop.stopped_reason = reason
    assert svc._loops[loop.id].stopped_reason == reason
    revived, error, status = await _autonudge_mod.authorize_and_update_nudge(
        svc=svc, loop_id=loop.id, active=True, fresh_run=True, source="dashboard"
    )
    assert error is None and status == 200 and revived is not None
    assert revived.active is True and revived.stopped_reason == ""
    assert revived.cycle_count == (0 if count_zeroed else cycles)
    if clock_reanchored:
        assert revived.created_ts > created
    else:
        assert revived.created_ts == created
    svc.stop()


@pytest.mark.asyncio
async def test_budget_expiring_mid_turn_deactivates_post_delivery(svc, monkeypatch):
    """The budget gates turn STARTS and must not cancel an
    in-flight turn — but once a slow turn ENDS with the budget spent, the loop
    deactivates immediately (tagged runtime_budget, expired emitted) instead
    of arming another idle cycle. Channel loops must not self-re-arm."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)

    async def on_fire(loop):
        # Simulate a turn so slow the budget expires while it runs.
        loop.created_ts -= 120
        return True

    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    await svc.start()
    # Channel-bound loop: exercises the self-re-arm path, which must be
    # skipped after the post-delivery deactivation.
    loop = await svc.add(
        slot_key="slack:1700000000.1", message="go", idle_secs=15, max_runtime_secs=60
    )
    svc._on_fire = on_fire
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    refreshed = svc._loops[loop.id]
    assert refreshed.cycle_count == 1, "the in-flight turn itself is never cancelled"
    assert refreshed.active is False, "spent budget takes effect the moment the turn ends"
    assert refreshed.stopped_reason == "runtime_budget"
    assert "expired" in events
    assert loop.id not in svc._timers, "no further cycle may be armed"
    svc.stop()


@pytest.mark.asyncio
async def test_update_changes_runtime_budget(svc):
    """update() validates the budget and leaves it untouched when omitted."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    assert loop.max_runtime_secs == 0
    updated = await svc.update(loop.id, max_runtime_secs=7200)
    assert updated is not None and updated.max_runtime_secs == 7200
    # Omitted → unchanged.
    updated = await svc.update(loop.id, message="still going")
    assert updated is not None and updated.max_runtime_secs == 7200
    # A malformed finite bound must not become unlimited.
    with pytest.raises(ValueError):
        await svc.update(loop.id, max_runtime_secs=-5)
    assert loop.max_runtime_secs == 7200
    svc.stop()


@pytest.mark.asyncio
async def test_load_leaves_an_inactive_loop_with_an_over_ceiling_budget_untouched(
    svc, monkeypatch, caplog
):
    """A paused legacy row with NO stop reason and a budget above the current
    ceiling is read back exactly as stored: still inactive, reason still empty,
    budget and deadline as written, and the store is not marked dirty for it.
    The ceiling is a write-time bound; load rewrites nothing against it."""
    loop = await svc.add(slot_key="chat-1-123", message="go", max_runtime_secs=7200)
    svc._loops[loop.id].active = False
    svc._loops[loop.id].stopped_reason = ""
    svc._loops[loop.id].next_due_ts = 0.0
    svc._save()
    svc.stop()
    monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: 3600)
    restored = AutoNudgeService(base_dir=svc._base_dir)
    with caplog.at_level(logging.INFO, logger="kiro_crew.autonudge"):
        restored._load()
    try:
        row = restored.get_by_id(loop.id)
        assert row is not None
        assert row.active is False
        assert row.stopped_reason == ""
        assert row.max_runtime_secs == 7200
        assert row.next_due_ts == 0.0
        assert restored._store_dirty is False
        assert not any("runtime" in r.getMessage() for r in caplog.records)
    finally:
        restored.stop()


@pytest.mark.asyncio
async def test_load_keeps_an_active_loop_with_an_over_ceiling_budget_running(svc, monkeypatch):
    """Lowering ``monitoring.max_runtime_secs`` below a running loop's stored
    budget does not stop it: the row stays active with its budget as written,
    so its deadline is still the stored ``created_ts + max_runtime_secs``."""
    loop = await svc.add(slot_key="chat-1-123", message="go", max_runtime_secs=7200)
    created_ts = loop.created_ts
    svc.stop()
    monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: 3600)
    restored = AutoNudgeService(base_dir=svc._base_dir)
    restored._load()
    try:
        row = restored.get_by_id(loop.id)
        assert row is not None and row.active is True
        assert row.stopped_reason == ""
        assert row.max_runtime_secs == 7200
        assert row.created_ts == created_ts
        assert not runtime_budget_exceeded(row, created_ts + 7199)
        assert runtime_budget_exceeded(row, created_ts + 7200)
        # An update that omits the budget is not re-checked and keeps it.
        updated = await restored.update(loop.id, message="still going")
        assert updated is not None and updated.max_runtime_secs == 7200
        # An update that supplies a budget over the ceiling is refused.
        with pytest.raises(ValueError):
            await restored.update(loop.id, max_runtime_secs=7200)
        assert restored.get_by_id(loop.id).max_runtime_secs == 7200
    finally:
        restored.stop()


@pytest.mark.asyncio
async def test_load_keeps_an_active_structured_monitor_with_an_over_ceiling_budget(
    svc, monkeypatch, caplog
):
    """A well-formed structured record above a lowered ceiling is neither held
    nor quarantined: it loads active with its budget, target and deadline as
    stored. Only a budget the next write supplies meets the ceiling."""
    loop = await svc.add_monitor(
        slot_key="chat-1-123",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=7200),
    )
    created_ts = loop.monitor.created_ts
    svc.stop()
    monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: 3600)
    restored = AutoNudgeService(base_dir=svc._base_dir)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        restored._load()
    try:
        row = restored.get_by_id(loop.id)
        assert row is not None and row.active is True
        assert row.stopped_reason == ""
        assert row.monitor is not None and row.monitor.outcome is None
        assert row.monitor.budgets.max_runtime_secs == 7200
        assert row.monitor.target == "owner/repo#123"
        assert row.monitor.created_ts == created_ts
        assert monitor_budget_reason(row.monitor, now=created_ts + 7199) == ""
        assert monitor_budget_reason(row.monitor, now=created_ts + 7200) == "runtime_budget"
        assert not any("quarantined malformed" in r.getMessage() for r in caplog.records)
        # A budget-neutral update does not revalidate the stored runtime budget.
        updated = await restored.update_monitor(loop.id, cadence_secs=120)
        assert updated is not None and updated.monitor.cadence_secs == 120
        assert updated.monitor.budgets.max_runtime_secs == 7200
        # A supplied budget over the ceiling is refused; one inside it lands.
        with pytest.raises(ValueError):
            await restored.update_monitor(loop.id, budget_patch={"max_runtime_secs": 7200})
        updated = await restored.update_monitor(loop.id, budget_patch={"max_runtime_secs": 1800})
        assert updated is not None and updated.monitor.budgets.max_runtime_secs == 1800
    finally:
        restored.stop()


@pytest.mark.asyncio
async def test_stop_sentinel_keeps_the_record_finished(svc, tmp_path, monkeypatch, caplog):
    """The stop file FINISHES the loop: the record stays, inactive, under its own
    reason, so the goal popover can say the goal was met instead of falling back
    to its empty form. Every other effect of the old removal is kept: the timer
    is gone, the stop line names the reason, the file is left where the agent
    wrote it, and nothing announces a stop-short (``expired``)."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    await svc.start()
    sentinel = tmp_path / "STOP"
    loop = await svc.add(
        slot_key="chat-1-123", message="go", idle_secs=15, stop_sentinel_path=str(sentinel)
    )
    sentinel.write_text("halt")
    svc._cancel_timer(loop.id)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        await svc._timer(loop)
    kept = svc.get_by_slot("chat-1-123")
    assert kept is not None and kept.id == loop.id
    assert kept.active is False
    assert kept.stopped_reason == _an.STOP_SENTINEL_REASON
    assert kept.next_due_ts == 0.0
    assert loop.id not in svc._timers
    assert sentinel.exists(), "the stop file is the agent's; the service leaves it"
    assert "expired" not in events, "a finish is not a stop-short"
    assert "updated" in events
    rows = json.loads((tmp_path / _an._NUDGES_FILE).read_text(encoding="utf-8"))["loops"]
    assert [(r["active"], r["stopped_reason"]) for r in rows] == [(False, "stop_sentinel")]
    stop_lines = [r.getMessage() for r in caplog.records if "stop_sentinel" in r.getMessage()]
    assert stop_lines, "the WARNING stop line must still name the reason"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop_sentinel", "monitor_terminal"])
async def test_a_finished_loop_is_not_revived_and_keeps_its_reason(svc, reason):
    """Done is a dead end. The popover's Play (``fresh_run``), ``monitor_update``
    and the app reconcilers all reach ``update(active=True)``, and every one is
    declined: there is nothing to resume. A later reasonless pause or a bound
    landing on the finished row keeps the reason too, because the reason is what
    refuses the revival."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, max_cycles=5)
    loop.cycle_count = 3
    created = loop.created_ts
    await svc.update(loop.id, active=False, stopped_reason=reason)
    assert svc._loops[loop.id].stopped_reason == reason

    writes: list[dict] = []
    real_write = svc._write_state
    svc._write_state = lambda payload: (writes.append(payload), real_write(payload))[1]
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    revived = await svc.update(loop.id, active=True, fresh_run=True)
    assert revived is not None and revived.active is False
    assert revived.stopped_reason == reason
    assert revived.cycle_count == 3 and revived.created_ts == created, "no fresh budget either"
    assert loop.id not in svc._timers
    # Declined with nothing else asked: no store write and no frame, so a
    # reconciler polling the finished row every few seconds costs nothing.
    assert writes == [] and events == []
    # A revival asked WITH a field change still applies the change (and persists).
    patched = await svc.update(loop.id, active=True, max_cycles=9)
    assert patched is not None and patched.active is False and patched.max_cycles == 9
    assert len(writes) == 1 and events == ["updated"]

    await svc.update(loop.id, active=False)  # the popover's Pause off a stale reading
    assert svc._loops[loop.id].stopped_reason == reason
    await svc.update(loop.id, active=False, stopped_reason="cycle_cap")
    assert svc._loops[loop.id].stopped_reason == reason


@pytest.mark.asyncio
async def test_a_stop_file_finish_is_revived_only_once_the_file_is_gone(svc, tmp_path, monkeypatch):
    """The operator's kill switch. Issue Radar and Research Lab arm the stop
    file as a brake and their reconcilers re-arm every inactive loop with
    ``update(active=True)``: while the file stands the row stays Done and the
    re-arm is declined (and writes nothing); once the operator deletes the file
    the same re-arm runs the loop again. A finished watch has no file to lift
    and stays finished."""
    import kiro_crew.autonudge as _an

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    sentinel = tmp_path / "STOP"
    loop = await svc.add(
        slot_key="chat-1-123", message="go", idle_secs=15, stop_sentinel_path=str(sentinel)
    )
    sentinel.write_text("halt")
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert svc._loops[loop.id].stopped_reason == _an.STOP_SENTINEL_REASON
    refused = await svc.update(loop.id, active=True)
    assert refused is not None and refused.active is False
    assert refused.stopped_reason == _an.STOP_SENTINEL_REASON
    sentinel.unlink()
    revived = await svc.update(loop.id, active=True)
    assert revived is not None and revived.active is True and revived.stopped_reason == ""
    assert loop.id in svc._timers
    svc._cancel_timer(loop.id)
    # A finished watch is not a kill switch: nothing to lift, so nothing revives it.
    await svc.update(loop.id, active=False, stopped_reason=_an.MONITOR_TERMINAL_REASON)
    still = await svc.update(loop.id, active=True)
    assert still is not None and still.active is False
    assert still.stopped_reason == _an.MONITOR_TERMINAL_REASON


@pytest.mark.asyncio
async def test_a_finished_loop_is_displaced_by_a_new_arm_not_by_a_create(svc, tmp_path):
    """The two ways on from Done. A directive re-arm (``monitor_start``, which opts
    into ``replace_stopped``) may displace the finished row -- the agent that
    stopped one goal through its stop file can arm the next, as it could when
    the row was removed. The popover's create-only POST still meets the row and
    refuses, so Clear stays the person's step."""
    import kiro_crew.autonudge as _an

    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    await svc.update(loop.id, active=False, stopped_reason=_an.STOP_SENTINEL_REASON)
    assert _an._stopped_row_is_replaceable(svc._loops[loop.id])
    with pytest.raises(_an.MonitorUpdateConflict):
        await svc.add(slot_key="chat-1-123", message="again", idle_secs=15, replace_existing=False)
    replacement = await svc.add(
        slot_key="chat-1-123",
        message="next goal",
        idle_secs=15,
        replace_existing=False,
        replace_stopped=True,
    )
    assert replacement.id != loop.id and replacement.active
    assert [lp.id for lp in svc.list_all()] == [replacement.id]
