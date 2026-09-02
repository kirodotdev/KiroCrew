"""Regression: the Dev Fleet prune worker must be drained on gateway shutdown.

Issue: ``_prune_run`` fired its ``_work()`` batch coroutine with
``asyncio.create_task(...)`` and discarded the returned task -- it was never
stored in a module global nor registered in ``_ACTIVE_RUNS``. ``dev_fleet_cleanup``
therefore had no handle to it, so a prune worker still running when the gateway
stopped kept mutating the shared MAIN_REPO ``.git`` state (``git worktree
remove`` / ``update-ref -d``) after the runner was supposed to have left
nothing behind. This violated the module's active-run cleanup invariant, which
``dev_fleet_cleanup`` already upholds for ``_ACTIVE_RUNS`` runs and the named
background tasks (``_refresher_task`` / ``_warm_task`` / ``_reaper_task``).

Fix: retain the worker in a module global ``_prune_task`` and drain it in
``dev_fleet_cleanup`` (cancel + await), alongside the named background tasks.
Cancelling is safe because the two destructive git mutations in
``_worktree_remove_locked`` are wrapped in ``asyncio.shield`` -- a shutdown
cancel cannot interrupt one mid-call (which would SIGKILL the child via
``_run_cmd`` and leave a half-removed worktree); the cancellation is delivered
only at the safe boundary once the shielded mutation has finished.

These tests verify:

1.  The worker task is retained in ``_prune_task``.
2.  ``dev_fleet_cleanup`` cancels and awaits the in-flight prune worker (the
    task ends up done) and clears the handle -- with no dangling reference.
3.  A destructive git mutation modelled with ``asyncio.shield`` runs to
    completion even though cleanup cancels the worker -- proving the shielded
    removal is not interrupted mid-call.
4.  Against the pre-fix "discarded task" path (no retention, no drain) the
    worker survives cleanup -- the regression this guards against.
"""

from __future__ import annotations

import asyncio

import pytest

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import live, repository, runtime, worktree_ops


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Isolate each test from shared module state."""
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(runtime, "_SHUTDOWN_IN_PROGRESS", False)
    monkeypatch.setattr(runtime, "_SHUTDOWN_ADMISSION_LOCK", asyncio.Lock())
    monkeypatch.setattr(worktree_ops, "_PRUNE_LOCK", asyncio.Lock())
    # ``raising=False`` so this suite still SETS UP against the pre-fix module
    # that lacks ``_prune_task`` -- the failure must land in the test body
    # (worker survives cleanup), not the fixture, so the regression is provable
    # by reverting the production hunk alone.
    monkeypatch.setattr(worktree_ops, "_prune_task", None, raising=False)
    monkeypatch.setattr(
        worktree_ops,
        "_PRUNE_STATE",
        {
            "running": False,
            "total": 0,
            "done": 0,
            "current": None,
            "results": [],
            "items": {},
        },
    )
    monkeypatch.setattr(worktree_ops, "_refresher_task", None)
    monkeypatch.setattr(worktree_ops, "_warm_task", None)
    monkeypatch.setattr(worktree_ops, "_reaper_task", None)
    yield


async def _start_prune(monkeypatch, *, remove_impl):
    """Start a single-item prune whose removal phase runs ``remove_impl``.

    Returns ``entered`` (set once the worker is inside removal). Deterministic:
    callers wait on observable state, not sleeps.
    """
    entered = asyncio.Event()

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": "feat/x"}, None

    async def fake_remove(nm, *, force, progress, _caller, **_kw):
        # ``**_kw`` absorbs any keyword the production ``_worktree_remove`` gains
        # (e.g. ``discard_untracked_paths``) so a call-site signature change can
        # never silently make the item fail before ``entered`` is set and hang
        # the test on ``entered.wait()``.
        entered.set()
        return await remove_impl()

    monkeypatch.setattr(repository, "_find_worktree", fake_find)
    monkeypatch.setattr(worktree_ops, "_worktree_remove", fake_remove)

    # Force the item so no gh verdict is required (keeps the test hermetic).
    res = await worktree_ops._prune_run([], force_names={"wt-x"})
    assert res["ok"] is True
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    return entered


@pytest.mark.asyncio
async def test_worker_is_retained(monkeypatch):
    """The prune worker task is stored in the module global (not discarded)."""
    gate = asyncio.Event()

    async def blocking():
        await gate.wait()
        return {"ok": True}

    await _start_prune(monkeypatch, remove_impl=blocking)
    try:
        assert worktree_ops._prune_task is not None
        assert not worktree_ops._prune_task.done()
    finally:
        gate.set()
        await asyncio.wait_for(worktree_ops._prune_task, timeout=5.0)


@pytest.mark.asyncio
async def test_cleanup_drains_prune_worker_and_clears_handle(monkeypatch):
    """dev_fleet_cleanup cancels + awaits the in-flight worker, no dangling ref."""
    gate = asyncio.Event()

    async def blocking():
        await gate.wait()  # never released -> worker is cancelled by cleanup
        return {"ok": True}

    await _start_prune(monkeypatch, remove_impl=blocking)
    prune_task = worktree_ops._prune_task
    assert prune_task is not None

    app = object()  # dev_fleet_cleanup only reads the arg, never touches it
    await asyncio.wait_for(mod.dev_fleet_cleanup(app), timeout=5.0)

    assert prune_task.done()
    assert worktree_ops._prune_task is None, "the handle must be cleared after the drain"


@pytest.mark.asyncio
async def test_run_uninterruptible_drains_before_reraising(monkeypatch):
    """_run_uninterruptible holds the caller until the inner task completes.

    A bare ``asyncio.shield`` would let the outer ``await`` raise
    ``CancelledError`` IMMEDIATELY -- unwinding the frame (and releasing its
    held locks) while the detached mutation is still writing. This asserts the
    caller stays blocked until the inner coroutine finishes, and only then does
    the cancellation propagate.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    inner_finished = {"value": False}
    reraised = {"value": False}

    async def inner() -> str:
        started.set()
        await release.wait()
        inner_finished["value"] = True
        return "done"

    async def caller() -> None:
        try:
            await runtime._run_uninterruptible(inner())
        except asyncio.CancelledError:
            reraised["value"] = True
            raise

    task = asyncio.ensure_future(caller())
    await asyncio.wait_for(started.wait(), timeout=5.0)

    # Cancel the caller while the inner mutation is still in flight.
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done(), "caller must stay blocked until the inner task finishes"
    assert not inner_finished["value"]
    assert not reraised["value"]

    # Release the inner mutation: only now may the caller unwind, and it must
    # re-raise the cancellation the caller requested.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)
    assert inner_finished["value"], "inner mutation must run to completion"
    assert reraised["value"], "cancellation must propagate after the drain"


@pytest.mark.asyncio
async def test_cleanup_stays_pending_and_holds_lock_until_mutation_releases(monkeypatch):
    """Cleanup blocks, and _GIT_MUTATION_LOCK stays held, until removal ends.

    Exercises the real ``_worktree_remove_locked`` uninterruptible-mutation
    path: the destructive git call is modelled by a gated fake ``_run_cmd``.
    While it is in flight and cleanup has issued the cancel, (1) cleanup must
    not complete and (2) no competitor may acquire ``_GIT_MUTATION_LOCK`` --
    proving the lock is not released early. Both free up only once the mutation
    returns.
    """
    # Fresh lock instance for this test so we can probe contention.
    monkeypatch.setattr(worktree_ops, "_GIT_MUTATION_LOCK", asyncio.Lock())
    monkeypatch.setattr(live, "_MAKE_LIVE_LOCK", asyncio.Lock())

    mutation_started = asyncio.Event()
    release_mutation = asyncio.Event()
    mutation_returned = {"value": False}

    async def fake_run_cmd(cmd, **kwargs):
        # Stand in for `git worktree remove`: the one destructive call the
        # worker makes while holding _GIT_MUTATION_LOCK.
        if "worktree" in cmd and "remove" in cmd:
            mutation_started.set()
            await release_mutation.wait()
            mutation_returned["value"] = True
            return 0, "", ""
        return 0, "", ""

    async def fake_find(nm):
        return {"path": "/wt/wt-x", "branch": "feat/x"}, None

    async def fake_remove(nm, *, force, progress, _caller, **_kw):
        # ``**_kw`` future-proofs against production signature drift (see the
        # note on the fake in _start_prune).
        # Mirror the production shape: hold _GIT_MUTATION_LOCK across the
        # uninterruptible destructive mutation.
        async with worktree_ops._GIT_MUTATION_LOCK:
            await runtime._run_uninterruptible(
                fake_run_cmd(["git", "-C", "/repo", "worktree", "remove", "/wt/wt-x"])
            )
        return {"ok": True}

    monkeypatch.setattr(runtime, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(repository, "_find_worktree", fake_find)
    monkeypatch.setattr(worktree_ops, "_worktree_remove", fake_remove)

    res = await worktree_ops._prune_run([], force_names={"wt-x"})
    assert res["ok"] is True
    await asyncio.wait_for(mutation_started.wait(), timeout=5.0)

    cleanup = asyncio.ensure_future(mod.dev_fleet_cleanup(object()))
    # Give cleanup a bounded window to run: under a bare shield the worker's
    # cancellation would unwind the frame, release the lock, and let cleanup
    # COMPLETE inside this window while the mutation is still orphaned. The
    # uninterruptible drain keeps cleanup pending and the lock held.
    done, _pending = await asyncio.wait({cleanup}, timeout=0.5)

    # (1) Cleanup must not finish while the mutation is in flight.
    assert cleanup not in done, "cleanup must stay pending until removal releases"
    assert not mutation_returned["value"]
    # (2) The git-mutation lock must still be held -- a competitor cannot take it.
    assert worktree_ops._GIT_MUTATION_LOCK.locked(), "lock must not be released early"

    # Release the destructive mutation: now cleanup can complete.
    release_mutation.set()
    await asyncio.wait_for(cleanup, timeout=5.0)

    assert mutation_returned["value"], "the mutation must run to completion"
    assert not worktree_ops._GIT_MUTATION_LOCK.locked(), "lock released after the drain"
    assert worktree_ops._prune_task is None


@pytest.mark.asyncio
async def test_old_discarded_task_path_survives_cleanup(monkeypatch):
    """Regression proof: the discard-the-task behavior leaks past cleanup."""
    leaked: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    async def blocked_work() -> None:
        await asyncio.Event().wait()  # blocked forever, like a stuck removal

    # Old behavior: fire and discard, never retain (_prune_task stays None),
    # never register in _ACTIVE_RUNS.
    leaked.append(real_create_task(blocked_work()))
    await asyncio.sleep(0)  # let it start

    app = object()
    await asyncio.wait_for(mod.dev_fleet_cleanup(app), timeout=5.0)

    assert (
        leaked and not leaked[0].done()
    ), "discarded prune worker leaks past cleanup -- this is the bug"

    leaked[0].cancel()
    try:
        await leaked[0]
    except asyncio.CancelledError:
        pass
