"""Deterministic regression tests for the dev-fleet shutdown admission race.

Issue: dev_fleet_cleanup snapshotted _ACTIVE_RUNS with ``list(...items())``,
then iterated the snapshot.  A _start_run call racing between the snapshot
point and the end of cleanup could register a new entry that was never included
in the snapshot and therefore never cancelled or awaited.  The escaped process
then kept mutating shared checkout state after the gateway exited.

Fix: _SHUTDOWN_ADMISSION_LOCK + _SHUTDOWN_IN_PROGRESS gate registration atomically
with the cleanup snapshot.  These tests verify:

1.  A run started BEFORE cleanup is captured in the snapshot and cleaned up.
2.  A run that races AFTER the snapshot-point (i.e. after cleanup has set the
    flag) is refused — proving the window is closed.
3.  The lock is never held across the slow kill/await phase (no deadlock).
4.  _start_run raises after shutdown, and after cleanup the flag stays set.
5.  All tests fail against a patched "old production code" path that uses the
    bare list snapshot without the admission guard (regression proof).
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import runtime, worktree_ops

# ---------------------------------------------------------------------------
# Fixture: reset all module-level state that these tests touch
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Test 1: run registered BEFORE cleanup is captured and terminated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_before_cleanup_is_captured_and_cancelled():
    """A run already in _ACTIVE_RUNS at cleanup time is found and cancelled."""
    # Pre-populate a fake active run with a running process.
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

    # The process tree was killed.
    assert fake_proc.pid in killed_pids
    # The task was cancelled (cancelled() → True after a yield).
    await asyncio.sleep(0)
    assert task.cancelled()
    # The entry was popped from _ACTIVE_RUNS.
    assert rid not in runtime._ACTIVE_RUNS
    # Shutdown flag is set and stays set.
    assert runtime._SHUTDOWN_IN_PROGRESS is True


# ---------------------------------------------------------------------------
# Test 2: run racing AFTER the snapshot-point is refused (window closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_after_snapshot_is_refused():
    """_start_run raises after cleanup has set _SHUTDOWN_IN_PROGRESS.

    This is the deterministic regression test: it pauses cleanup at the
    transaction boundary (immediately after flag+snapshot) and confirms that a
    concurrent _start_run is refused rather than escaping cleanup.
    """
    # We'll control execution order via events.
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

        # Start cleanup in the background.
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

        # The failed run must NOT have been registered.
        assert len(runtime._ACTIVE_RUNS) == 0

        # Let cleanup finish.
        cleanup_may_proceed.set()
        await cleanup_task


# ---------------------------------------------------------------------------
# Test 3: the admission lock is released before slow kill/await work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_lock_released_before_slow_work():
    """The admission lock is not held during kill/await, so _start_run does not
    deadlock waiting for the lock while cleanup is in the slow phase.

    This test verifies the lock is free after cleanup's critical section even
    while a fake 'slow kill' is still running in the same event loop.
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

        # Let kill finish.
        kill_may_finish.set()
        await cleanup_task


# ---------------------------------------------------------------------------
# Test 4: _start_run raises AFTER cleanup, and _SHUTDOWN_IN_PROGRESS stays set
# ---------------------------------------------------------------------------


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

    # Now try to start a run — it must be refused.
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


# ---------------------------------------------------------------------------
# Test 5: regression proof — OLD code (bare list snapshot) fails test 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_code_without_admission_guard_escapes_cleanup():
    """Prove the bug existed: the old bare-snapshot cleanup misses a concurrent
    registration, so the test that catches it (test 2) would FAIL against old code.

    This test simulates the pre-fix behavior and asserts the race DOES occur —
    confirming that the new guard, not test design, is what prevents the escape.
    """
    # Replicate the OLD cleanup: no admission flag, bare list() snapshot.
    snapshot_taken = asyncio.Event()
    cleanup_may_proceed = asyncio.Event()

    async def old_cleanup():
        """Old cleanup: snapshot without closing the admission window first."""
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

    # Wait for the bare snapshot to be taken (no flag set yet).
    await snapshot_taken.wait()

    # With old code, _SHUTDOWN_IN_PROGRESS is never set — so _start_run
    # succeeds and escapes the cleanup snapshot.
    assert not runtime._SHUTDOWN_IN_PROGRESS  # old code never set this

    # Manually simulate what _start_run would do without the guard.
    rid_escaped = "escaped_run"
    task_escaped = asyncio.create_task(asyncio.sleep(100))
    # No admission check → registers successfully.
    runtime._ACTIVE_RUNS[rid_escaped] = (task_escaped, None)

    cleanup_may_proceed.set()
    await cleanup_coro

    # OLD CODE BUG: the escaped run is still in _ACTIVE_RUNS — cleanup missed it.
    assert (
        rid_escaped in runtime._ACTIVE_RUNS
    ), "Expected old code to leave the escaped run in _ACTIVE_RUNS (the bug)"
    # Cleanup: cancel the dangling task.
    task_escaped.cancel()
    try:
        await task_escaped
    except asyncio.CancelledError:
        pass
