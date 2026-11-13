"""A wave's two in-memory deadlines are aged on the monotonic clock, stamp to sweep.

The gateway's completion consumer (``_subagent_done``) holds a finished wave member's
result for the wave digest and stamps ``_digest_held_at``. The reaper's hold-deadline
sweep force-flushes a partial digest once the oldest hold is ``DIGEST_HOLD_SECS`` old.
Every submission of a wave also stamps ``_batch_progress_ts`` (a refused spawn, a
lost submission, an execution record that cannot be read), and the reaper's
stuck-wave sweep reconciles a lost submission once ``_WAVE_STUCK_SECS`` pass with no
progress. Each pair reads one clock at both ends; this pins that a wall-clock step
moves neither.

The hold is driven through the real closure, each progress stamp through the real
spawn or lost-submission method, and each sweep inside the real reaper loop. A flush
is read through a recording completion consumer and a reconcile through the wave's
submitted count; every kill, release, prune and store call is a recorder. All state is
under tmp_path (the conftest isolates the data home).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_cron_thread_routing import _capture_subagent_done, _make_gateway

from kiro_crew import subagent as sa
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_WAVE = "wave-1"
_PARENT = "dashboard:chat-7"


class _SteppedClock:
    """The ``time`` module with its wall clock and its monotonic clock offset."""

    def __init__(self) -> None:
        self.wall = 0.0
        self.mono = 0.0

    def time(self) -> float:
        return time.time() + self.wall

    def monotonic(self) -> float:
        return time.monotonic() + self.mono

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


def _member(agent_id: str, *, done: bool) -> SubagentInfo:
    info = SubagentInfo(
        id=agent_id,
        task=f"task {agent_id}",
        parent_session_key=_PARENT,
        batch_id=_WAVE,
        batch_total=3,
    )
    info.done = done
    info.result = "finished" if done else ""
    return info


async def _hold_one_member(clock: _SteppedClock) -> tuple[SubagentManager, SubagentInfo]:
    """Finish one member of a three-member wave through the gateway's closure."""
    gateway = _make_gateway()
    gateway._batch_progress = {}
    done_cb = _capture_subagent_done(gateway)
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_done=done_cb)
    gateway.subagent_mgr = mgr
    finished = _member("m1", done=True)
    for info in (finished, _member("m2", done=False), _member("m3", done=False)):
        mgr._agents[info.id] = info
    with patch("kiro_crew.slack.gateway.time", clock):
        await done_cb(finished)
    assert finished._digest_held, "the closure did not hold the finished member for the digest"
    return mgr, finished


async def _reaper_tick(
    mgr: SubagentManager, clock: _SteppedClock, *, stuck_waves: bool = False
) -> list[SubagentInfo]:
    """Run the real reaper loop on *clock* until one tick has run.

    Only the sweep under test runs; the other one is a no-op. The digest-hold sweep
    announces through a recording completion consumer that stands in for the gateway's,
    and the flush-only records it received are returned. With *stuck_waves* the
    stuck-wave sweep runs instead, and its outcome is the wave's submitted count.
    """
    announced: list[SubagentInfo] = []

    async def _record(info: SubagentInfo) -> None:
        announced.append(info)

    pump = MagicMock()
    with contextlib.ExitStack() as stack:
        for patcher in (
            patch.object(sa, "_REAPER_INTERVAL", 0),
            patch.object(sa, "compact_cost_log"),
            patch.object(sa, "prune_stale_tombstones", MagicMock(return_value=0)),
            patch.object(sa, "time", clock),
            patch.object(mgr, "_sample_live_costs"),
            patch.object(mgr, "_sweep_conversations_async", new=AsyncMock()),
            patch.object(mgr, "retry_owed_teardown_sweeps", new=AsyncMock()),
            patch.object(mgr, "_maybe_flag_stall", new=AsyncMock()),
            patch.object(mgr, "_taskq_pump", pump),
            patch.object(type(mgr._admission), "taskq_schedule_owed_replay", MagicMock()),
            patch.object(mgr, "_force_reap", AsyncMock()),
            patch.object(mgr, "release_conversation_async", AsyncMock(return_value=(True, "x"))),
        ):
            stack.enter_context(patcher)
        stack.enter_context(patch.object(sa, "sel"))
        if stuck_waves:
            stack.enter_context(patch.object(mgr, "_sweep_digest_holds_async", new=AsyncMock()))
        else:
            stack.enter_context(patch.object(mgr, "_sweep_stuck_waves_async", new=AsyncMock()))
            stack.enter_context(
                patch.object(mgr, "batch_members_pending_async", new=AsyncMock(return_value=True))
            )
            stack.enter_context(patch.object(mgr, "_on_done", _record))
        task = asyncio.ensure_future(mgr._reaper_loop())
        try:
            await async_wait_until(
                lambda: pump.call_count >= 2 or task.done(),
                describe=lambda: f"reaper ticks: {pump.call_count}, reaper done: {task.done()}",
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=30.0)
        await asyncio.wait_for(asyncio.gather(*mgr._tasks.values()), timeout=30.0)
    assert pump.call_count >= 2, "the reaper loop did not complete a tick"
    return [info for info in announced if info._digest_flush_only]


@pytest.mark.asyncio
async def test_a_forward_wall_clock_step_does_not_flush_a_fresh_hold():
    """Red before the fix: the hold stamp and the sweep read the wall clock, so a
    10-minute forward step made a hold seconds old look 600 s old and forced a partial
    digest out of a wave whose other members were still running."""
    clock = _SteppedClock()
    mgr, _held = await _hold_one_member(clock)
    clock.wall += 600.0
    flushes = await _reaper_tick(mgr, clock)
    assert flushes == [], f"a fresh hold was force-flushed after a wall-clock step: {flushes}"


@pytest.mark.asyncio
async def test_a_backward_wall_clock_step_does_not_keep_an_expired_hold():
    """Red before the fix: the hold stamp and the sweep read the wall clock, so an hour-long
    backward step made a hold that really is older than the deadline look negative-aged and
    kept its finished result withheld past ``DIGEST_HOLD_SECS``."""
    clock = _SteppedClock()
    mgr, _held = await _hold_one_member(clock)
    clock.wall -= 3600.0
    clock.mono += sa.DIGEST_HOLD_SECS + 30
    flushes = await _reaper_tick(mgr, clock)
    assert {(f.batch_id, f.parent_session_key) for f in flushes} == {
        (_WAVE, _PARENT)
    }, "a hold older than the deadline was kept after a wall-clock step back"


@pytest.mark.asyncio
async def test_a_hold_past_its_deadline_still_flushes():
    """Control: a hold that really is older than the deadline flushes, as today."""
    clock = _SteppedClock()
    mgr, _held = await _hold_one_member(clock)
    clock.wall += sa.DIGEST_HOLD_SECS + 30
    clock.mono += sa.DIGEST_HOLD_SECS + 30
    flushes = await _reaper_tick(mgr, clock)
    assert {(f.batch_id, f.parent_session_key) for f in flushes} == {
        (_WAVE, _PARENT)
    }, "a hold older than the deadline was not flushed"


@pytest.mark.asyncio
async def test_a_fresh_hold_with_no_step_is_not_flushed():
    """Control: with no step and no time passed, the deadline is not reached."""
    clock = _SteppedClock()
    mgr, _held = await _hold_one_member(clock)
    assert await _reaper_tick(mgr, clock) == []


# ── the stuck-wave sweep ──────────────────────────────────────────────────────
#
# A wave whose spawn submissions were counted but never all arrived (a refused spawn,
# a lost submission) is force-reconciled by the reaper once ``_WAVE_STUCK_SECS`` pass
# with no progress. The three places that count a submission each stamp the progress
# time, so each is driven here; the sweep is the real one inside the real reaper loop.

_PROGRESS_WRITERS = ["lost-submission", "refused-spawn", "unreadable-execution-record"]


async def _count_one_submission(mgr: SubagentManager, clock: _SteppedClock, writer: str) -> None:
    """Count one submission of a three-member wave through *writer*, stamping *clock*."""
    with patch.object(sa, "time", clock), patch.object(sa, "sel"):
        if writer == "lost-submission":
            mgr.record_lost_submission(_WAVE, 3, "never arrived")
        elif writer == "refused-spawn":
            mgr.spawn("", parent_session_key=_PARENT, batch_id=_WAVE, batch_total=3)
        else:
            await mgr.spawn_async(
                "a task",
                parent_session_key=_PARENT,
                batch_id=_WAVE,
                batch_total=3,
                _memory_mode="not-a-memory-mode",
            )
    assert mgr._batch_submitted[_WAVE] == [1, 3], f"{writer} did not count its submission"
    assert _WAVE in mgr._batch_progress_ts, f"{writer} did not stamp the wave's progress"


def _wave_manager() -> SubagentManager:
    return SubagentManager(sessions=MagicMock(), ctx_builder=None)


# ``_count_one_submission`` leaves a wave at one of three submitted; one reconcile
# counts one more lost member, and the fresh progress stamp keeps later ticks off it.
_NOT_RECONCILED = [1, 3]
_RECONCILED = [2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", _PROGRESS_WRITERS)
async def test_a_forward_wall_clock_step_does_not_reconcile_a_wave_that_just_progressed(writer):
    """Red before the fix: the stamp and the sweep read the wall clock, so a step of
    ``_WAVE_STUCK_SECS`` and a minute made a wave that progressed seconds ago look stuck
    and force-reconciled a submission that may still be in flight."""
    clock = _SteppedClock()
    mgr = _wave_manager()
    await _count_one_submission(mgr, clock, writer)
    clock.wall += sa._WAVE_STUCK_SECS + 60
    await _reaper_tick(mgr, clock, stuck_waves=True)
    assert (
        mgr._batch_submitted[_WAVE] == _NOT_RECONCILED
    ), "a wave that just progressed was reconciled"


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", _PROGRESS_WRITERS)
async def test_a_wave_stuck_past_the_window_is_reconciled_through_a_wall_clock_step_back(writer):
    """Red before the fix: an hour's step back of the wall clock made a wave with no
    progress for longer than the window read as having progressed in the future, so the
    sweep never reconciled it. Also fails when a writer stamps another clock than the
    sweep reads (a wall-clock stamp is far ahead of any monotonic reading)."""
    clock = _SteppedClock()
    mgr = _wave_manager()
    await _count_one_submission(mgr, clock, writer)
    clock.wall -= 3600.0
    clock.mono += sa._WAVE_STUCK_SECS + 60
    await _reaper_tick(mgr, clock, stuck_waves=True)
    assert mgr._batch_submitted[_WAVE] == _RECONCILED, f"a stuck wave was not reconciled ({writer})"


@pytest.mark.asyncio
async def test_a_wave_stuck_past_the_window_is_reconciled_without_a_step():
    """Control: both clocks pass the window, so the wave is reconciled, as today."""
    clock = _SteppedClock()
    mgr = _wave_manager()
    await _count_one_submission(mgr, clock, "lost-submission")
    clock.wall += sa._WAVE_STUCK_SECS + 60
    clock.mono += sa._WAVE_STUCK_SECS + 60
    await _reaper_tick(mgr, clock, stuck_waves=True)
    assert mgr._batch_submitted[_WAVE] == _RECONCILED


@pytest.mark.asyncio
async def test_a_wave_that_just_progressed_is_not_reconciled_without_a_step():
    """Control: with no step and no time passed, the window is not reached."""
    clock = _SteppedClock()
    mgr = _wave_manager()
    await _count_one_submission(mgr, clock, "lost-submission")
    await _reaper_tick(mgr, clock, stuck_waves=True)
    assert mgr._batch_submitted[_WAVE] == _NOT_RECONCILED
