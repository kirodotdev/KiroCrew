"""Deterministic tests for the dev-fleet shutdown admission race.

A _start_run racing dev_fleet_cleanup's snapshot of _ACTIVE_RUNS would escape it,
never cancelled or awaited, and keep mutating the checkout after the gateway exits.
_SHUTDOWN_ADMISSION_LOCK + _SHUTDOWN_IN_PROGRESS gate registration atomically with
the snapshot; the lock is not held across the slow kill/await phase.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import runtime, worktree_ops


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Isolate each test from shared module state."""
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_SHUTDOWN_IN_PROGRESS", False)
    # Give each test a fresh lock so prior acquisitions do not bleed over.
    monkeypatch.setattr(runtime, "_SHUTDOWN_ADMISSION_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_RUNS_LOCK", asyncio.Lock())
    # Disable background tasks so startup/cleanup don't spawn real subtasks.
    monkeypatch.setenv(worktree_ops._DISABLE_BACKGROUND_ENV, "1")
    yield


# Helpers


def _make_fake_proc(*, running: bool = True):
    """A fake asyncio subprocess-like object."""
    proc = MagicMock()
    proc.returncode = None if running else 0
    proc.pid = 99999
    proc.stdout = AsyncMock()
    proc.stdout.readline = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


async def _noop_run(label, cmd, *, cwd=None, env=None, cleanup_paths=None):
    """Replacement _start_run that registers immediately without spawning."""
    rid = uuid.uuid4().hex[:12]
    async with runtime._RUNS_LOCK:
        runtime._RUNS[rid] = {
            "status": "running",
            "exit_code": None,
            "label": label,
            "output": [],
            "started": 0.0,
        }
    task = asyncio.create_task(asyncio.sleep(10))  # long-lived stand-in
    async with runtime._SHUTDOWN_ADMISSION_LOCK:
        if runtime._SHUTDOWN_IN_PROGRESS:
            task.cancel()
            raise RuntimeError("dev-fleet shutdown in progress: run refused")
        runtime._ACTIVE_RUNS[rid] = (task, None)
    task.add_done_callback(lambda _t: runtime._ACTIVE_RUNS.pop(rid, None))
    return rid


@pytest.mark.asyncio
async def test_run_before_cleanup_is_captured_and_cancelled():
    """A run already in _ACTIVE_RUNS at cleanup time is found and cancelled."""
    fake_proc = _make_fake_proc(running=True)
    task = asyncio.create_task(asyncio.sleep(10))
    rid = "before_run"
    runtime._ACTIVE_RUNS[rid] = (task, fake_proc)

    killed_pids = []

    async def fake_kill_tree(pid):
        killed_pids.append(pid)

    with (
        patch.object(runtime, "_kill_tree", side_effect=fake_kill_tree),
        patch.object(worktree_ops, "_refresher_task", None),
        patch.object(worktree_ops, "_warm_task", None),
        patch.object(worktree_ops, "_reaper_task", None),
    ):
        await mod.dev_fleet_cleanup(app=None)

    assert fake_proc.pid in killed_pids
    # cancelled() turns True only after a yield.
    await asyncio.sleep(0)
    assert task.cancelled()
    assert rid not in runtime._ACTIVE_RUNS
    assert runtime._SHUTDOWN_IN_PROGRESS is True


@pytest.mark.asyncio
async def test_run_after_snapshot_is_refused():
    """_start_run raises after cleanup has set _SHUTDOWN_IN_PROGRESS.

    Cleanup pauses right after flag+snapshot, so a concurrent _start_run lands
    inside the race window and must be refused.
    """
    snapshot_taken = asyncio.Event()
    cleanup_may_proceed = asyncio.Event()

    async def instrumented_cleanup(app):
        """Cleanup that signals after the critical section, then waits."""
        global _cleanup_inner_called  # noqa: F821
        async with runtime._SHUTDOWN_ADMISSION_LOCK:
            runtime._SHUTDOWN_IN_PROGRESS = True
            _snapshot = list(runtime._ACTIVE_RUNS.items())
        # Signal: the admission window is now closed.
        snapshot_taken.set()
        # Wait for the test to attempt a registration before we continue.
        await cleanup_may_proceed.wait()
        # Now do the (empty) cleanup loop.
        for rid, (task, proc) in _snapshot:
            if proc is not None and proc.returncode is None:
                await runtime._kill_tree(proc.pid)
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            runtime._ACTIVE_RUNS.pop(rid, None)

    with (
        patch.object(worktree_ops, "_refresher_task", None),
        patch.object(worktree_ops, "_warm_task", None),
        patch.object(worktree_ops, "_reaper_task", None),
    ):

        cleanup_task = asyncio.create_task(instrumented_cleanup(app=None))

        # Wait until the critical section has completed.
        await snapshot_taken.wait()

        # At this point _SHUTDOWN_IN_PROGRESS is True. Attempt to start a run.
        # Patch the internals that _start_run needs so it doesn't actually spawn.
        fake_proc = _make_fake_proc()

        async def fake_create_subprocess(*args, **kwargs):
            return fake_proc

        with patch(
            "kiro_crew.apps.builtins.dev_fleet.runtime.create_subprocess_limited",
            side_effect=fake_create_subprocess,
        ):
            with pytest.raises(RuntimeError, match="shutdown in progress"):
                await runtime._start_run(
                    "test_label",
                    ["/bin/echo", "hi"],
                    cwd=None,
                    env={},
                )

        assert len(runtime._ACTIVE_RUNS) == 0

        cleanup_may_proceed.set()
        await cleanup_task


@pytest.mark.asyncio
async def test_admission_lock_released_before_slow_work():
    """The admission lock is not held during kill/await, so _start_run does not
    deadlock waiting for the lock while cleanup is in the slow phase.
    """
    kill_started = asyncio.Event()
    kill_may_finish = asyncio.Event()

    async def slow_kill_tree(pid):
        kill_started.set()
        await kill_may_finish.wait()

    fake_proc = _make_fake_proc(running=True)
    task = asyncio.create_task(asyncio.sleep(100))
    rid = "slow_run"
    runtime._ACTIVE_RUNS[rid] = (task, fake_proc)

    with (
        patch.object(runtime, "_kill_tree", side_effect=slow_kill_tree),
        patch.object(worktree_ops, "_refresher_task", None),
        patch.object(worktree_ops, "_warm_task", None),
        patch.object(worktree_ops, "_reaper_task", None),
    ):

        cleanup_task = asyncio.create_task(mod.dev_fleet_cleanup(app=None))

        # Wait for kill to start (cleanup is past critical section, in slow phase).
        await kill_started.wait()

        # The admission lock must be FREE — acquire and release immediately.
        acquired = False
        try:
            acquired = runtime._SHUTDOWN_ADMISSION_LOCK.locked()
        except Exception:
            pass
        assert not acquired, "Admission lock should be released before the slow kill phase"

        kill_may_finish.set()
        await cleanup_task


@pytest.mark.asyncio
async def test_start_run_refused_after_cleanup():
    """After a full dev_fleet_cleanup cycle, _start_run raises on any new call."""
    with (
        patch.object(worktree_ops, "_refresher_task", None),
        patch.object(worktree_ops, "_warm_task", None),
        patch.object(worktree_ops, "_reaper_task", None),
    ):
        await mod.dev_fleet_cleanup(app=None)

    assert runtime._SHUTDOWN_IN_PROGRESS is True

    fake_proc = _make_fake_proc()

    async def fake_create(*args, **kwargs):
        return fake_proc

    with patch(
        "kiro_crew.apps.builtins.dev_fleet.runtime.create_subprocess_limited",
        side_effect=fake_create,
    ):
        with pytest.raises(RuntimeError, match="shutdown in progress"):
            await runtime._start_run("after_shutdown", ["/bin/echo"])

    assert len(runtime._ACTIVE_RUNS) == 0


@pytest.mark.asyncio
async def test_old_code_without_admission_guard_escapes_cleanup():
    """A bare-snapshot cleanup with no admission guard misses a concurrent registration.

    Shows the race is real, so it is the guard, not the test design, that makes
    test_run_after_snapshot_is_refused pass.
    """
    snapshot_taken = asyncio.Event()
    cleanup_may_proceed = asyncio.Event()

    async def old_cleanup():
        """Snapshot without closing the admission window first."""
        _snapshot = list(runtime._ACTIVE_RUNS.items())  # bare snapshot, no flag
        snapshot_taken.set()
        await cleanup_may_proceed.wait()
        for rid, (task, proc) in _snapshot:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            runtime._ACTIVE_RUNS.pop(rid, None)

    cleanup_coro = asyncio.create_task(old_cleanup())

    await snapshot_taken.wait()

    # Without the guard _SHUTDOWN_IN_PROGRESS is never set, so a registration
    # after the snapshot succeeds.
    assert not runtime._SHUTDOWN_IN_PROGRESS

    # What _start_run does without the admission check.
    rid_escaped = "escaped_run"
    task_escaped = asyncio.create_task(asyncio.sleep(100))
    runtime._ACTIVE_RUNS[rid_escaped] = (task_escaped, None)

    cleanup_may_proceed.set()
    await cleanup_coro

    # Cleanup missed the escaped run.
    assert (
        rid_escaped in runtime._ACTIVE_RUNS
    ), "Expected old code to leave the escaped run in _ACTIVE_RUNS (the bug)"
    task_escaped.cancel()
    try:
        await task_escaped
    except asyncio.CancelledError:
        pass
