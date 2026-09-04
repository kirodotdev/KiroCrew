"""Deterministic regression test for the dev-fleet proc-stamp orphan window.

Context
-------
``#5297`` closed the shutdown *admission* race: a run whose ``(task, proc)``
tuple is registered in ``_ACTIVE_RUNS`` AFTER ``dev_fleet_cleanup`` took its
snapshot is now refused, and a run registered before is cancelled + its process
tree killed (see ``test_devfleet_active_runs_shutdown.py``).

This is a DIFFERENT, narrower window that ``#5297`` does not close.

``_start_run`` registers the run under the admission lock as ``(task, None)``
— the process handle is NOT known yet.  The worker coroutine stamps the real
process into ``_ACTIVE_RUNS[rid] = (task, proc)`` LATER, right after
``await create_subprocess_limited(...)`` returns, and it does so WITHOUT the
admission lock and WITHOUT re-checking ``_SHUTDOWN_IN_PROGRESS``.

So the following interleaving escapes cleanup:

1. ``_start_run`` registers ``(task, None)`` (shutdown not yet in progress).
2. The worker reaches ``await create_subprocess_limited(...)`` — the child is
   spawned by the OS, but the coroutine has not returned, so ``proc`` is still
   ``None`` in ``_ACTIVE_RUNS``.
3. ``dev_fleet_cleanup`` runs: it snapshots ``(task, None)``, sees ``proc is
   None`` and therefore SKIPS ``_kill_tree``, then merely ``task.cancel()``s.
4. The spawned child is never killed — it keeps running (a pip/npm/git build
   mutating the shared checkout) after the gateway exits.  This is exactly the
   "escaped process keeps mutating shared checkout state" harm the shutdown
   path exists to prevent.

This test drives that interleaving deterministically and asserts the child's
process tree IS killed.  It FAILS against current production code (the child is
orphaned) and passes once the worker stamps/kills under the admission guard.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import runtime, worktree_ops


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_SHUTDOWN_IN_PROGRESS", False)
    monkeypatch.setattr(runtime, "_SHUTDOWN_ADMISSION_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_RUNS_LOCK", asyncio.Lock())
    monkeypatch.setenv(worktree_ops._DISABLE_BACKGROUND_ENV, "1")
    yield


def _make_fake_proc():
    """A fake asyncio subprocess-like object that never yields any output."""
    proc = MagicMock()
    proc.returncode = None  # still running
    proc.pid = 424242
    proc.stdout = AsyncMock()
    # readline blocks forever so the worker parks in its read loop rather than
    # finishing the run before cleanup fires.
    proc.stdout.readline = AsyncMock(side_effect=lambda: asyncio.Future())
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


@pytest.mark.asyncio
async def test_proc_spawned_during_shutdown_is_not_orphaned():
    """A run whose process is spawned in the proc-stamp window must still be
    killed by cleanup, not orphaned.
    """
    spawn_entered = asyncio.Event()
    spawn_may_return = asyncio.Event()
    fake_proc = _make_fake_proc()

    async def blocking_create_subprocess(*args, **kwargs):
        # The OS child is "spawned" the moment we enter here; the coroutine
        # then blocks BEFORE returning, so _start_run's worker has registered
        # (task, None) but has not yet stamped `proc`.
        spawn_entered.set()
        await spawn_may_return.wait()
        return fake_proc

    killed_pids: list[int] = []

    async def fake_kill_tree(pid):
        killed_pids.append(pid)

    with (
        patch.object(
            runtime,
            "create_subprocess_limited",
            side_effect=blocking_create_subprocess,
        ),
        patch.object(runtime, "_kill_tree", side_effect=fake_kill_tree),
        patch.object(runtime, "platform_compat") as pc,
        patch.object(worktree_ops, "_refresher_task", None),
        patch.object(worktree_ops, "_warm_task", None),
        patch.object(worktree_ops, "_reaper_task", None),
    ):
        pc.IS_POSIX = True
        pc.IS_WINDOWS = False
        pc.kill_and_reap = AsyncMock()

        # 1. Start the run. It registers (task, None) and the worker begins;
        #    the worker parks inside blocking_create_subprocess (child spawned,
        #    proc not yet stamped).
        rid = await runtime._start_run("build", ["/bin/echo", "hi"], cwd=None, env={})
        await spawn_entered.wait()

        # Precondition: the run is registered but with proc STILL None — this
        # is the window under test.
        assert rid in runtime._ACTIVE_RUNS
        assert runtime._ACTIVE_RUNS[rid][1] is None

        # 2. Shutdown fires now, while the child is live but unstamped.
        cleanup = asyncio.create_task(mod.dev_fleet_cleanup(app=None))

        # Let cleanup take its snapshot and cancel the worker. Release the
        # spawn so the (now-cancelled) worker unwinds through its except/finally.
        await asyncio.sleep(0)
        spawn_may_return.set()
        await cleanup

    # The spawned child MUST have been killed. Current production code orphans
    # it (proc was None in the snapshot, so _kill_tree was skipped and the
    # worker's CancelledError never reaped it), so this assertion fails today.
    assert fake_proc.pid in killed_pids, (
        "child spawned in the proc-stamp window was orphaned: cleanup never "
        "killed its process tree"
    )


@pytest.mark.asyncio
async def test_repeat_cancellation_during_drain_still_reaps():
    """A SECOND cancellation arriving while the cancel handler drains the spawn
    task must not abandon the child.

    The cancel handler recovers the process handle by draining ``spawn_task``
    (the spawn keeps running detached after the first cancel). If a second
    cancellation — e.g. a shutdown hard-timeout following the graceful cancel —
    lands on the drain await and is swallowed into ``proc = None``, the child is
    orphaned again. The drain therefore loops until the spawn task is actually
    done, absorbing repeat cancellations, and this test proves it: it cancels
    the worker twice while the spawn is still parked, then releases the spawn
    and asserts the child's tree was killed.
    """
    spawn_entered = asyncio.Event()
    spawn_may_return = asyncio.Event()
    fake_proc = _make_fake_proc()

    async def blocking_create_subprocess(*args, **kwargs):
        spawn_entered.set()
        await spawn_may_return.wait()
        return fake_proc

    killed_pids: list[int] = []

    async def fake_kill_tree(pid):
        killed_pids.append(pid)

    with (
        patch.object(
            runtime,
            "create_subprocess_limited",
            side_effect=blocking_create_subprocess,
        ),
        patch.object(runtime, "_kill_tree", side_effect=fake_kill_tree),
        patch.object(runtime, "platform_compat") as pc,
        patch.object(worktree_ops, "_refresher_task", None),
        patch.object(worktree_ops, "_warm_task", None),
        patch.object(worktree_ops, "_reaper_task", None),
    ):
        pc.IS_POSIX = True
        pc.IS_WINDOWS = False
        pc.kill_and_reap = AsyncMock()

        rid = await runtime._start_run("build", ["/bin/echo", "hi"], cwd=None, env={})
        await spawn_entered.wait()
        worker_task = runtime._ACTIVE_RUNS[rid][0]

        # First cancellation: worker enters the CancelledError handler and
        # begins draining spawn_task (still parked).
        worker_task.cancel()
        await asyncio.sleep(0)
        # Second cancellation lands while the drain await is pending — the loop
        # must absorb it and keep draining rather than dropping the handle.
        worker_task.cancel()
        await asyncio.sleep(0)

        # Now let the spawn finish; the drain recovers the handle and reaps.
        spawn_may_return.set()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    assert fake_proc.pid in killed_pids, (
        "repeat cancellation during the drain abandoned the child: its process "
        "tree was never killed"
    )
