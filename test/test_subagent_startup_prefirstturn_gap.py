"""Characterization tests for the pre-first-turn reaper coverage gap.

A subagent shown as ``starting`` is registered but has not advanced past
turn 0. Two watchdogs are meant to bound that state:

* the fast startup watchdog (:meth:`SubagentManager._is_startup_stalled`,
  window :data:`_STARTUP_TIMEOUT_SECS`), and
* the stuck-wave sweep (:meth:`SubagentManager._sweep_stuck_waves`).

These tests record two states that neither watchdog covers. Each assertion
that encodes the gap is marked in its docstring as the currently observed
behaviour that a fix is expected to change, so a change closing the gap has
to edit these tests deliberately rather than silently pass them:

1. A wave member still sitting in the spawn queue lives only in ``_queue`` --
   never in ``_agents`` -- so the reaper's per-agent loop and the startup
   watchdog never see it, and the stuck-wave sweep skips any wave holding a
   queued member.
2. A registered run that has not entered ``_run_inner`` (``_exec_started is
   None`` -- e.g. parked on a spawn approval) is invisible to the startup
   watchdog, whose predicate returns ``False`` for it.

In both states the only remaining backstop is the wall-clock reaper at
``_default_timeout``: at any instant short of that deadline the reaper's
per-agent decision leaves the run in place. The tests assert that no bound
shorter than the wall clock exists, not the wall-clock value itself.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from kiro_crew.subagent import (
    _STARTUP_TIMEOUT_SECS,
    _WAVE_STUCK_SECS,
    SubagentInfo,
    SubagentManager,
)


def _make_manager(max_concurrent: int = 1) -> SubagentManager:
    mgr = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        max_concurrent=max_concurrent,
    )
    mgr._on_done = None
    return mgr


def _reaper_would_terminate(mgr: SubagentManager, info: SubagentInfo, now: float) -> bool:
    """Reproduce the reaper's per-agent terminal decision without running the
    async loop: a live run is force-reaped only when the startup watchdog
    fires or when elapsed exceeds the wall-clock ``_default_timeout``. Mirrors
    ``_MonitoringMixin._reaper_loop`` in ``subagent_manager/monitoring.py``.
    """
    if info.done:
        return False
    if mgr._is_startup_stalled(info, now):
        return True
    return (now - info.started) > mgr._default_timeout


# --- Gap 1: a queued wave member is invisible to every reaper ---------


def test_gap_queued_member_absent_from_agents_so_no_reaper_sees_it():
    """A wave member behind the stagger/concurrency gate lives only in
    ``_queue``; it is never registered in ``_agents``, the only collection the
    reaper's per-agent loop and the startup watchdog iterate.

    Currently observed and expected to change: a queued member has no reaper
    coverage of any kind. A fix that gives queued runs a deadline should make
    the queued member reachable (in ``_agents`` or a swept collection), which
    will change this assertion.
    """
    mgr = _make_manager(max_concurrent=1)
    batch_id = "wave"
    mgr._queue.append(
        {
            "task": "t",
            "parent_session_key": "p",
            "agent": "amzn-builder",
            "batch_id": batch_id,
            "batch_total": 2,
            "_preassigned_id": "queued_member",
        }
    )
    assert "queued_member" not in mgr._agents


def test_gap_stuck_wave_sweep_skips_a_wave_with_a_queued_member():
    """The stuck-wave sweep reconciles a wave only when nothing of it is still
    queued. A wave with a lost submission (submitted < expected), all
    registered members terminal, and no progress for the grace window is left
    untouched while any member remains in ``_queue``.

    Currently observed and expected to change: a wave stranded on a
    never-draining queue is closed by neither the sweep nor a completion event.
    A fix that reaps or re-queues stranded members should let the sweep
    reconcile such a wave, which will change these assertions.
    """
    mgr = _make_manager(max_concurrent=1)
    batch_id = "wave"
    terminal = SubagentInfo(
        id="done_member", task="t", agent="", batch_id=batch_id, batch_total=2, done=True
    )
    mgr._agents["done_member"] = terminal
    mgr._queue.append(
        {
            "task": "t",
            "parent_session_key": "p",
            "agent": "amzn-builder",
            "batch_id": batch_id,
            "batch_total": 2,
            "_preassigned_id": "queued_member",
        }
    )
    # Lost-submission shape: 1 of 2 submitted, past the grace window.
    mgr._batch_submitted[batch_id] = [1, 2]
    mgr._batch_progress_ts[batch_id] = time.time() - _WAVE_STUCK_SECS - 100

    before = list(mgr._batch_submitted[batch_id])
    mgr._sweep_stuck_waves(time.time())
    after = list(mgr._batch_submitted[batch_id])

    # Untouched: the queued-member guard short-circuits the sweep.
    assert before == after
    # And the wave still reads as pending, so no digest closes it.
    assert mgr.batch_members_pending(batch_id) is True


# --- Gap 2: a run that never entered execution is invisible to the ----
#           fast startup watchdog; only the wall clock backstops it.


def test_gap_startup_watchdog_ignores_a_run_that_never_entered_execution():
    """``_is_startup_stalled`` keys on ``_exec_started``. A run registered in
    ``_agents`` but parked before ``_run_inner`` (``_exec_started is None`` --
    e.g. awaiting a spawn approval no surface answered) is not seen by the fast
    watchdog, no matter how long it has been registered.

    Currently observed and expected to change: the fast watchdog never fires
    for a pre-execution run. A fix that gives such a run a deadline measured
    from registration should make the watchdog (or an equivalent reaper) fire,
    which will change this assertion.
    """
    mgr = _make_manager(max_concurrent=1)
    info = SubagentInfo(id="parked", task="t", agent="")
    info._exec_started = None
    info._awaiting_approval = True
    info.turns = 0
    info._pid = None
    now = info.started + _STARTUP_TIMEOUT_SECS * 100  # far past the startup window

    assert mgr._is_startup_stalled(info, now) is False


def test_gap_pre_execution_run_has_no_bound_shorter_than_the_wall_clock():
    """For a registered pre-execution run (``_exec_started is None``), the
    reaper's per-agent decision does not terminate it at any instant short of
    the wall-clock ``_default_timeout`` -- the startup watchdog cannot see it,
    so the wall clock is the only bound.

    Currently observed and expected to change: the sole backstop is the wall
    clock. A fix adding a pre-execution deadline should terminate the run
    before ``_default_timeout``, which will change this assertion. The test
    pins the absence of a shorter bound, not the wall-clock value.
    """
    mgr = _make_manager(max_concurrent=1)
    info = SubagentInfo(id="parked", task="t", agent="")
    info._exec_started = None
    info._awaiting_approval = True
    info.turns = 0
    info._pid = None

    # Just before the wall clock: still not terminated.
    just_before = info.started + mgr._default_timeout - 1
    assert _reaper_would_terminate(mgr, info, just_before) is False

    # Only once the wall clock is exceeded does the reaper act.
    past_wall_clock = info.started + mgr._default_timeout + 1
    assert _reaper_would_terminate(mgr, info, past_wall_clock) is True


def test_startup_watchdog_reaps_a_run_wedged_after_entering_execution():
    """Contrast (the covered half): once ``_run_inner`` has set
    ``_exec_started`` and the run is still on turn 0 with no runtime pid past
    the startup window, the fast watchdog fires. This is stable expected
    behaviour, not a gap; the tests above pin the uncovered half.
    """
    mgr = _make_manager(max_concurrent=1)
    info = SubagentInfo(id="wedged", task="t", agent="")
    info._exec_started = time.time() - (_STARTUP_TIMEOUT_SECS + 10)
    info.turns = 0
    info._pid = None

    assert mgr._is_startup_stalled(info, time.time()) is True
