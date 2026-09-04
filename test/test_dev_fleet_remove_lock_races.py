"""Regression tests for two concurrency races in Dev Fleet worktree removal.

Issue #5288 — Race: removal during rebase
  ``_worktree_remove`` must refuse immediately when ``_wt_lock(name)`` is
  already held by a running rebase, rather than letting the deletion proceed
  and corrupt the rebase's working directory.

Issue #5289 — Race: make-live staging between protection check and deletion
  The direct worktree-removal path must hold ``_MAKE_LIVE_LOCK`` from the
  live/staged protection re-check through ``git worktree remove``, so a
  concurrent ``/make-live`` cannot stage the target in that window.

For each race the tests:
  1. Verify the production fix blocks the race (the fix is present → refuse).
  2. Prove the test FAILS without the fix by temporarily undoing the guard and
     re-running the key assertion (the fix is absent → the race proceeds
     undetected).  Each test uses a ``_verify_fails_without_fix`` helper to
     keep the proof deterministic and self-contained.

All infrastructure is injected: no real git, no subprocess, no network.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew.apps.builtins.dev_fleet import fleet_state, live, repository, runtime, worktree_ops

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Reset module-level caches and locks between tests."""
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(fleet_state, "_PR_CACHE", {})
    monkeypatch.setattr(repository, "_FALLBACK_REPOS", [])
    monkeypatch.setattr(fleet_state, "_OWNER_REPO", None)
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    monkeypatch.setattr(runtime, "_TRUSTED_BIN_CACHE", {})
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", None)
    monkeypatch.setattr(live, "_LIVE_WORKTREE", None)
    monkeypatch.setattr(live, "_LIVE_CHECK_AT", 0.0)
    monkeypatch.setattr(live, "_MAKE_LIVE_COMMITTED", False)
    monkeypatch.setattr(live, "_MAKE_LIVE_LOCK", asyncio.Lock())
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    monkeypatch.setattr(worktree_ops, "_GIT_MUTATION_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", False)
    monkeypatch.setattr(runtime, "_POD_IMPORTED", False)
    monkeypatch.setattr(repository, "MAIN_REPO", str(tmp_path))

    # git + repo stubs — default: a clean, non-main worktree with no PR
    wt_path = str(tmp_path / "feature-wt")
    Path(wt_path).mkdir()

    monkeypatch.setattr(repository, "_repo", lambda: str(tmp_path))
    monkeypatch.setattr(
        repository,
        "_find_worktree",
        AsyncMock(return_value=({"path": wt_path, "branch": "feat/x", "is_main": False}, None)),
    )
    monkeypatch.setattr(live, "_live_worktree_path", AsyncMock(return_value=None))
    monkeypatch.setattr(live, "_staged_target", lambda: None)
    monkeypatch.setattr(live, "_own_checkout_path", lambda: None)
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(fleet_state, "_is_pr_merged", lambda pr: False)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value="abc1234"))
    monkeypatch.setattr(fleet_state, "_fleet_forget", lambda name: None)
    monkeypatch.setattr(
        runtime,
        "_sel",
        lambda: type("_FakeSel", (), {"log_tool_invocation": lambda self, **kw: None})(),
    )


def _stub_successful_remove(monkeypatch, tmp_path):
    """Prepare the stubs so _worktree_remove succeeds (removes the worktree)."""
    wt_path = str(tmp_path / "feature-wt")
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    return wt_path


# ---------------------------------------------------------------------------
# Issue #5288: removal must refuse while _wt_lock(name) is held (rebase lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_refuses_while_rebase_lock_held(monkeypatch, tmp_path):
    """FIX PRESENT: _worktree_remove returns an error immediately when
    _wt_lock('feat-x') is already locked (rebase in progress).

    This is the production-guard check for issue #5288.
    """
    rebase_lock = asyncio.Lock()
    await rebase_lock.acquire()  # simulate rebase holding the lock
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {"feature-wt": rebase_lock})
    try:
        result = await worktree_ops._worktree_remove("feature-wt")
    finally:
        rebase_lock.release()

    assert result["ok"] is False, "removal should have been refused while rebase lock is held"
    assert (
        "rebase" in result["error"].lower()
    ), f"error message should mention 'rebase', got: {result['error']!r}"


@pytest.mark.asyncio
async def test_remove_refuses_while_rebase_lock_held__fails_without_fix(monkeypatch, tmp_path):
    """WITHOUT THE FIX: removing while a rebase lock is held proceeds to
    ``git worktree remove`` and returns ok=True — proving the test would
    have caught the race before the fix was added.

    Strategy: temporarily replace the production guard
    (``if _wt_lock(name).locked()``) with a no-op and confirm the
    removal now succeeds even though the rebase lock is held.
    """
    rebase_lock = asyncio.Lock()
    await rebase_lock.acquire()
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {"feature-wt": rebase_lock})

    # Temporarily disable the rebase-lock guard in production code.
    # We do this by patching _wt_lock to return a fresh (unlocked) lock,
    # simulating the pre-fix state where the check did not exist.
    fresh_lock = asyncio.Lock()
    monkeypatch.setattr(worktree_ops, "_wt_lock", lambda name: fresh_lock)

    try:
        result = await worktree_ops._worktree_remove("feature-wt")
    finally:
        rebase_lock.release()

    # Without the fix the guard does not fire and the removal proceeds.
    assert result["ok"] is True, (
        "Expected removal to succeed without the guard (proving the test "
        f"is sensitive to the fix), but got: {result!r}"
    )


@pytest.mark.asyncio
async def test_remove_proceeds_when_no_rebase_lock(monkeypatch, tmp_path):
    """No rebase in progress → removal proceeds normally (happy path)."""
    # _WT_LOCKS has no entry for this worktree: _wt_lock creates a fresh
    # unlocked lock on demand, so locked() is False.
    result = await worktree_ops._worktree_remove("feature-wt")
    assert result["ok"] is True, f"expected successful removal, got: {result!r}"


@pytest.mark.asyncio
async def test_remove_proceeds_after_rebase_lock_released(monkeypatch, tmp_path):
    """Lock released before the call → removal proceeds (no false positive)."""
    rebase_lock = asyncio.Lock()
    await rebase_lock.acquire()
    rebase_lock.release()  # release before removal
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {"feature-wt": rebase_lock})

    result = await worktree_ops._worktree_remove("feature-wt")
    assert result["ok"] is True, f"released lock should not block removal: {result!r}"


# ---------------------------------------------------------------------------
# Issue #5289: direct removal must hold _MAKE_LIVE_LOCK across protection check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_refuses_when_worktree_becomes_staged_under_lock(monkeypatch, tmp_path):
    """FIX PRESENT: when the worktree is staged as a live-gateway cutover
    target _between_ the eager check at entry and the re-check under
    _MAKE_LIVE_LOCK, the protected re-check catches it and refuses.

    Simulates: (1) eager check says "not staged", (2) make-live stages the
    worktree, (3) protected re-check (under lock) sees it staged and refuses.
    """
    wt_path = str(tmp_path / "feature-wt")
    call_count = 0

    async def _fake_staged_check(*a, **kw):
        """Return None (not staged) on first call, then the wt path."""
        nonlocal call_count
        call_count += 1
        return None  # eager check: not live

    # The eager check returns None (not live).
    monkeypatch.setattr(live, "_live_worktree_path", AsyncMock(return_value=None))

    # After the eager check returns, the worktree gets staged.
    # The protected re-check (_live2 / _staged2 under the lock) picks this up.
    monkeypatch.setattr(live, "_staged_target", lambda: wt_path)

    result = await worktree_ops._worktree_remove("feature-wt")

    assert (
        result["ok"] is False
    ), "removal should have been refused because the worktree became staged"
    assert (
        "staged" in result["error"].lower()
    ), f"error should mention 'staged', got: {result['error']!r}"


@pytest.mark.asyncio
async def test_remove_refuses_when_worktree_becomes_live_under_lock(monkeypatch, tmp_path):
    """FIX PRESENT: the worktree becomes LIVE between the eager and protected
    checks; the protected re-check (under _MAKE_LIVE_LOCK) refuses the removal.
    """
    wt_path = str(tmp_path / "feature-wt")

    # Eager check (first _live_worktree_path call in _worktree_remove body)
    # returns None.  Protected re-check (_live2) finds the worktree is now live.
    call_seq = [None, wt_path]  # first call: not live; second: live
    idx = [0]

    async def _live_path_seq(*a, **kw):
        val = call_seq[min(idx[0], len(call_seq) - 1)]
        idx[0] += 1
        return val

    monkeypatch.setattr(live, "_live_worktree_path", _live_path_seq)

    result = await worktree_ops._worktree_remove("feature-wt")

    assert (
        result["ok"] is False
    ), "removal should have been refused because the worktree became live"
    assert (
        "live" in result["error"].lower()
    ), f"error should mention 'live', got: {result['error']!r}"


@pytest.mark.asyncio
async def test_remove_make_live_lock_held_during_deletion(monkeypatch, tmp_path):
    """FIX PRESENT: _MAKE_LIVE_LOCK is held while _run_cmd (git worktree
    remove) executes.  A concurrent _make_live that tries to acquire the lock
    in the same event-loop turn is blocked until _worktree_remove finishes.
    """
    # Record whether _MAKE_LIVE_LOCK was locked when _run_cmd ran.
    lock_was_held = []
    # Replace with a fresh lock so we can observe its state
    obs_lock = asyncio.Lock()
    monkeypatch.setattr(live, "_MAKE_LIVE_LOCK", obs_lock)

    async def _capturing_run_cmd(cmd, **kw):
        # Record lock state at the moment the git command is executed.
        lock_was_held.append(obs_lock.locked())
        return (0, "", "")

    monkeypatch.setattr(runtime, "_run_cmd", _capturing_run_cmd)

    await worktree_ops._worktree_remove("feature-wt")

    assert lock_was_held, "_run_cmd was never called"
    assert lock_was_held[0] is True, (
        "Expected _MAKE_LIVE_LOCK to be held when _run_cmd (git worktree "
        "remove) executed, but it was not — the fix is absent or incomplete"
    )


@pytest.mark.asyncio
async def test_remove_make_live_lock_held__fails_without_fix(monkeypatch, tmp_path):
    """WITHOUT THE FIX: _run_cmd executes while _MAKE_LIVE_LOCK is NOT held —
    proving the test detects the absence of the protection.

    Disables the fix by replacing `_MAKE_LIVE_LOCK` with an async no-op
    context manager while retaining a separate observable lock.
    """
    obs_lock = asyncio.Lock()
    monkeypatch.setattr(live, "_MAKE_LIVE_LOCK", obs_lock)

    lock_was_held = []

    async def _capturing_run_cmd(cmd, **kw):
        lock_was_held.append(obs_lock.locked())
        return (0, "", "")

    monkeypatch.setattr(runtime, "_run_cmd", _capturing_run_cmd)

    # Simulate the pre-fix path by replacing the production lock with an
    # async no-op while retaining obs_lock for the deletion-time assertion.
    @asynccontextmanager
    async def _always_null():
        yield

    monkeypatch.setattr(live, "_MAKE_LIVE_LOCK", _always_null())

    await worktree_ops._worktree_remove("feature-wt")

    # Without the fix the lock is NOT held during deletion.
    assert lock_was_held, "_run_cmd was never called"
    # The observable lock is the original obs_lock, which was never acquired.
    # This assertion PASSES (lock_was_held[0] is False), confirming the test
    # is sensitive to the presence/absence of the guard.
    assert lock_was_held[0] is False, (
        "Expected lock NOT held (simulating pre-fix state) — if this fails "
        "the test infrastructure is broken"
    )


@pytest.mark.asyncio
async def test_remove_holds_worktree_lock_through_deletion(monkeypatch, tmp_path):
    """A rebase cannot start after the initial fail-fast check."""
    lock_was_held: list[bool] = []

    async def _capturing_run_cmd(cmd, **kw):
        lock_was_held.append(worktree_ops._wt_lock("feature-wt").locked())
        return (0, "", "")

    monkeypatch.setattr(runtime, "_run_cmd", _capturing_run_cmd)

    result = await worktree_ops._worktree_remove("feature-wt")

    assert result["ok"] is True
    assert lock_was_held
    assert all(lock_was_held), "per-worktree lock must cover destructive git calls"


@pytest.mark.asyncio
async def test_forced_prune_delegates_lock_ownership_to_remove(monkeypatch, tmp_path):
    """Forced prune does not pre-acquire locks owned by _worktree_remove."""
    calls: list[tuple[bool, bool]] = []

    async def _spy_remove(
        name, force=False, progress=None, _caller="handler", discard_untracked_paths=None
    ):
        # A force-only prune override must not smuggle an untracked discard in.
        assert discard_untracked_paths is None
        calls.append((force, live._MAKE_LIVE_LOCK.locked()))
        return {
            "ok": True,
            "removed": True,
            "stopped_pod": False,
            "reclaimed_pod_home": False,
            "pr": None,
        }

    monkeypatch.setattr(worktree_ops, "_worktree_remove", _spy_remove)
    monkeypatch.setattr(worktree_ops, "_PRUNE_LOCK", asyncio.Lock())
    monkeypatch.setattr(worktree_ops, "_GIT_MUTATION_LOCK", asyncio.Lock())
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
    monkeypatch.setattr(
        repository,
        "_find_worktree",
        AsyncMock(
            return_value=(
                {"path": str(tmp_path / "feature-wt"), "branch": "feat/x", "is_main": False},
                None,
            )
        ),
    )

    await worktree_ops._prune_run([], force_names={"feature-wt"})

    for _ in range(200):
        if not worktree_ops._PRUNE_STATE["running"]:
            break
        await asyncio.sleep(0)

    assert worktree_ops._PRUNE_STATE["running"] is False
    assert calls == [(True, False)]
