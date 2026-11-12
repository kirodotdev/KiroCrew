"""Characterization tests for the pre-first-turn reaper coverage gap.

Neither the startup watchdog (:meth:`SubagentManager._is_startup_stalled`,
:data:`_STARTUP_TIMEOUT_SECS`) nor :meth:`SubagentManager._sweep_stuck_waves` covers:

1. a wave member still in ``_queue`` (never in ``_agents``); the sweep also skips its wave;
2. a registered run with ``_exec_started is None`` (e.g. parked on a spawn approval).

Only the wall-clock reaper at ``_default_timeout`` bounds them. Each gap assertion is the
observed behaviour a fix is expected to change, so closing a gap must edit these tests.
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
    """A queued wave member lives only in ``_queue``, which no reaper iterates.

    Expected to change when queued runs get a deadline.
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
    """The stuck-wave sweep skips a lost-submission wave while any member is queued.

    Expected to change when stranded members are reaped or re-queued.
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
    """``_is_startup_stalled`` never fires for a run with ``_exec_started is None``.

    Expected to change when such a run gets a deadline from registration.
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
    """A pre-execution run has no bound shorter than the wall-clock ``_default_timeout``.

    Pins the absence of a shorter bound, not the wall-clock value; expected to change.
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
    info._exec_started = time.time() - (mgr._startup_deadline + 10)
    info.turns = 0
    info._pid = None

    assert mgr._is_startup_stalled(info, time.time()) is True
