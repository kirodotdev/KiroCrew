"""Prune verdicts and the candidate scan of the Dev Fleet standalone backend.

This module pins ``worktree_ops._prunable``'s verdict for each worktree state
(merged, closed, fresh, active, dirty, unverifiable head, reused branch, a failed
dirty check) and ``_prune_candidates``'s split into candidate and kept rows, its
bounded concurrent scan and its discovery-order output. The PR lookup is always
stubbed, so nothing reaches gh or the network. The helper, PR-cache, pod and rebase
cases live in ``test_dev_fleet_server_coverage.py``, the HTTP layer in
``test_dev_fleet_server_coverage_http_api.py``, and the untracked-discard path of a
removal in ``test_dev_fleet_server_coverage_untracked_discard.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from dev_fleet_server_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _pin_module_state,
)

from kiro_crew.apps.builtins.dev_fleet import fleet_state, repository, worktree_ops


# --------------------------------------------------------------------------
# prune verdicts
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prunable_dirty_check_failure(monkeypatch):
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=None))
    v = await worktree_ops._prunable("/nope/missing", "feat")
    assert v == {**v, "ok": False, "code": "dirty_check_failed"}
    assert v["age_h"] is None


@pytest.mark.asyncio
async def test_prunable_merged_but_dirty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=1))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=True))
    assert (await worktree_ops._prunable(str(tmp_path), "feat"))["code"] == "merged_dirty"


@pytest.mark.asyncio
async def test_prunable_merged_unverified_when_oid_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value=None))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", AsyncMock(return_value=None))
    assert (await worktree_ops._prunable(str(tmp_path), "feat"))["code"] == "merged_unverified"


@pytest.mark.asyncio
async def test_prunable_merged_with_new_commits(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value="aaa"))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", AsyncMock(return_value="bbb"))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", AsyncMock(return_value=False))
    assert (await worktree_ops._prunable(str(tmp_path), "feat"))["code"] == "merged_new_commits"


@pytest.mark.asyncio
async def test_prunable_merged_clean_is_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value="aaa"))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", AsyncMock(return_value="aaa"))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", AsyncMock(return_value=True))
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is True and v["code"] == "merged"


@pytest.mark.asyncio
async def test_prunable_fresh_empty_worktree_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is False and v["code"] == "fresh"


@pytest.mark.asyncio
async def test_prunable_active_worktree_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "OPEN"}))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=3))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    assert (await worktree_ops._prunable(str(tmp_path), "feat"))["code"] == "active"


def _bind_closed_head(monkeypatch, *, contained=True, pr_oid="pr-head"):
    """Satisfy the closed verdict's head binding.

    The `closed` verdict is bound to the branch head the same way `merged` is:
    a closed PR is looked up by branch NAME, so a reused branch would otherwise
    inherit a stale CLOSED verdict. These two seams are what that guard reads.
    """
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value="local-head"))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", AsyncMock(return_value=pr_oid))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", AsyncMock(return_value=contained))


@pytest.mark.asyncio
async def test_prunable_closed_clean_is_candidate_not_merged(monkeypatch, tmp_path):
    """A CLOSED-PR worktree with a clean tree is a `closed` candidate — a
    distinct class from `merged`, so the manual checklist can group and warn on
    it separately."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is True
    assert v["code"] == "closed"
    # It must NOT be classified as merged — a merged tree's content is on the
    # base branch by definition, a closed one's is not.
    assert v["code"] != "merged"


@pytest.mark.asyncio
async def test_prunable_closed_reused_branch_is_refused(monkeypatch, tmp_path):
    """REGRESSION PIN: a branch REUSED after its PR closed must not inherit the
    stale CLOSED verdict.

    GitHub resolves a closed PR by branch NAME, so a branch that kept moving —
    new commits the closed PR never saw, perhaps a replacement PR not opened yet
    — still returns that CLOSED record. Offering the tree as prunable on that
    basis would destroy work unrelated to the PR that was declined, which is the
    exact data loss this whole class exists to prevent. The local head not being
    contained in the closed PR's head is what detects it."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=5))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch, contained=False)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is False
    assert v["code"] == "closed_new_commits"
    # The ancestry warning still travels with the refusal.
    assert v["unmerged_commits"] is True


@pytest.mark.asyncio
async def test_prunable_closed_unverifiable_head_is_refused(monkeypatch, tmp_path):
    """Fail-closed: when the closed PR's head OID cannot be established the
    verdict withholds the candidate rather than trusting the name-based lookup.
    An unverifiable guard must never read as a pass."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=1))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch, pr_oid=None)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is False
    assert v["code"] == "closed_unverified"


@pytest.mark.asyncio
async def test_prunable_closed_ahead_of_base_flags_unmerged_commits(monkeypatch, tmp_path):
    """A clean closed tree whose branch is ahead of base carries the ancestry
    warning (`unmerged_commits`) — a stronger signal than a merged candidate."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=4))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is True and v["code"] == "closed"
    assert v["unmerged_commits"] is True


@pytest.mark.asyncio
async def test_prunable_closed_clean_no_own_commits_no_unmerged_warning(monkeypatch, tmp_path):
    """A clean closed tree with no commits ahead of base is still a candidate,
    but the unmerged-commits alarm is off."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    _bind_closed_head(monkeypatch)
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["code"] == "closed"
    assert v["unmerged_commits"] is False


@pytest.mark.asyncio
async def test_prunable_closed_dirty_is_refused_with_loss_summary(monkeypatch, tmp_path):
    """A CLOSED-PR worktree with a dirty tree is REFUSED without force
    (code `closed_dirty`), and the verdict names what a removal would lose:
    the tracked-modification flag and the untracked-file count."""
    monkeypatch.setattr(
        fleet_state, "_pr_status_cached", AsyncMock(return_value={"state": "CLOSED"})
    )
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=2))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=True))
    # One modified tracked file plus three untracked files — the loss summary.
    monkeypatch.setattr(
        repository,
        "_dirty_split",
        AsyncMock(return_value=(True, ["a.py", "b.py", "c.py"])),
    )
    v = await worktree_ops._prunable(str(tmp_path), "feat")
    assert v["ok"] is False
    assert v["code"] == "closed_dirty"
    # Loss summary: modified tracked files + untracked count are surfaced so the
    # operator decides informed instead of blind-confirming.
    assert v["dirty"] is True
    assert v["dirty_tracked"] is True
    assert v["dirty_untracked"] == 3
    # And the ancestry warning still rides along on the refusal.
    assert v["unmerged_commits"] is True


@pytest.mark.asyncio
async def test_prune_candidates_surfaces_closed_unmerged_flag(monkeypatch):
    """`_prune_candidates` carries the closed candidate's `unmerged_commits`
    flag onto its row (and only onto closed rows, not merged ones)."""
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(
            return_value=[
                {"path": "/r", "is_main": True},
                {"path": "/r/wt-closed", "branch": "c"},
                {"path": "/r/wt-merged", "branch": "m"},
            ]
        ),
    )

    async def fake_prunable(path, branch):
        if branch == "c":
            return {"ok": True, "code": "closed", "unmerged_commits": True}
        return {"ok": True, "code": "merged"}

    monkeypatch.setattr(worktree_ops, "_prunable", fake_prunable)
    out = await worktree_ops._prune_candidates()
    by_name = {c["name"]: c for c in out["candidates"]}
    assert by_name["wt-closed"]["code"] == "closed"
    assert by_name["wt-closed"]["unmerged_commits"] is True
    # A merged row carries no unmerged_commits key (frontend reads its absence
    # as "not applicable").
    assert "unmerged_commits" not in by_name["wt-merged"]


@pytest.mark.asyncio
async def test_prune_candidates_closed_dirty_lands_in_kept_with_loss_counts(monkeypatch):
    """A `closed_dirty` worktree is a KEPT row (refused by default), and its
    dirt breakdown is surfaced so the checklist can show the loss summary."""
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(
            return_value=[
                {"path": "/r", "is_main": True},
                {"path": "/r/wt-cd", "branch": "cd"},
            ]
        ),
    )

    async def fake_prunable(path, branch):
        return {
            "ok": False,
            "code": "closed_dirty",
            "dirty": True,
            "dirty_tracked": True,
            "dirty_untracked": 2,
            "dirty_untracked_paths": ["x", "y"],
            "unmerged_commits": True,
        }

    monkeypatch.setattr(worktree_ops, "_prunable", fake_prunable)
    out = await worktree_ops._prune_candidates()
    assert [c["name"] for c in out["candidates"]] == []
    kept = {k["name"]: k for k in out["kept"]}
    assert kept["wt-cd"]["code"] == "closed_dirty"
    assert kept["wt-cd"]["dirty"] is True
    assert kept["wt-cd"]["dirty_tracked"] is True
    assert kept["wt-cd"]["dirty_untracked"] == 2


@pytest.mark.asyncio
async def test_prunable_passes_full_head_oid_to_pr_status_cached(monkeypatch, tmp_path):
    """Prune cache invalidation uses full commit identity for branch reuse."""
    full_head = "a" * 40
    cache = AsyncMock(return_value=None)
    monkeypatch.setattr(fleet_state, "_pr_status_cached", cache)
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    git = AsyncMock(return_value=full_head)
    monkeypatch.setattr(repository, "_git", git)

    await worktree_ops._prunable(str(tmp_path), "feat")

    git.assert_awaited_once_with(str(tmp_path), "rev-parse", "HEAD")
    cache.assert_awaited_once_with("feat", full_head)


@pytest.mark.asyncio
async def test_prune_candidates_splits_and_skips_main(monkeypatch):
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(
            return_value=[
                {"path": "/r", "is_main": True},
                {"path": "/r/wt-a", "branch": "a"},
                {"path": "/r/wt-b", "branch": "b"},
            ]
        ),
    )

    async def fake_prunable(path, branch):
        ok = branch == "a"
        return {"ok": ok, "code": "merged" if ok else "active"}

    monkeypatch.setattr(worktree_ops, "_prunable", fake_prunable)
    out = await worktree_ops._prune_candidates()
    assert out["scanned"] == 2
    assert [c["name"] for c in out["candidates"]] == ["wt-a"]
    assert [k["name"] for k in out["kept"]] == ["wt-b"]


# --------------------------------------------------------------------------
# _prune_candidates -- concurrent bounded scan, deterministic output (Defect 1)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_candidates_scans_concurrently(monkeypatch):
    """The per-worktree verdicts must run CONCURRENTLY, not one-at-a-time.

    A serial loop over 111 worktrees at ~0.5s each floors past the gateway's
    30s proxy cap and 504s. This pins that more than one _prunable is in flight
    at once (so the scan is issued concurrently) and that it stays within the
    _PRUNE_CONCURRENCY bound.
    """
    n = worktree_ops._PRUNE_CONCURRENCY * 3
    wts = [{"path": f"/repo/wt-{i}", "branch": f"feat/{i}", "is_main": False} for i in range(n)]
    wts.insert(0, {"path": "/repo/main", "branch": "main", "is_main": True})

    in_flight = 0
    peak = 0
    started = asyncio.Event()

    async def fake_prunable(path, branch):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        started.set()
        try:
            # Yield so siblings queued in the same gather can start before this
            # one finishes -- a serial loop would resolve each before the next.
            # flake-ok: pre-existing two one-turn yields that let queued siblings start, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return {"ok": True, "code": "merged"}
        finally:
            in_flight -= 1

    monkeypatch.setattr(repository, "_discover_worktrees", AsyncMock(return_value=wts))
    monkeypatch.setattr(worktree_ops, "_prunable", fake_prunable)

    out = await worktree_ops._prune_candidates()

    assert out["ok"] is True
    # More than one verdict was in flight at the same time -> concurrent, and
    # never more than the configured bound.
    assert peak > 1
    assert peak <= worktree_ops._PRUNE_CONCURRENCY
    # main is excluded; scanned counts only non-main worktrees.
    assert out["scanned"] == n
    assert len(out["candidates"]) == n


@pytest.mark.asyncio
async def test_prune_candidates_output_order_is_deterministic(monkeypatch):
    """Candidate/kept order follows DISCOVERY order, not completion order.

    Verdicts finish out of order (later worktrees resolve first), but the
    result lists must still be in the input worktree order so the checklist is
    stable across previews.
    """
    wts = [
        {"path": "/repo/main", "branch": "main", "is_main": True},
        {"path": "/repo/wt-a", "branch": "feat/a", "is_main": False},
        {"path": "/repo/wt-b", "branch": "feat/b", "is_main": False},
        {"path": "/repo/wt-c", "branch": "feat/c", "is_main": False},
        {"path": "/repo/wt-d", "branch": "feat/d", "is_main": False},
    ]
    # Finish delay is INVERTED to path order: wt-d resolves first, wt-a last.
    delays = {"/repo/wt-a": 0.04, "/repo/wt-b": 0.03, "/repo/wt-c": 0.02, "/repo/wt-d": 0.01}
    # Alternate candidate / kept so both lists are exercised for ordering.
    verdicts = {
        "/repo/wt-a": {"ok": True, "code": "merged"},
        "/repo/wt-b": {"ok": False, "code": "active", "dirty": False},
        "/repo/wt-c": {"ok": True, "code": "merged"},
        "/repo/wt-d": {"ok": False, "code": "active", "dirty": False},
    }

    async def fake_prunable(path, branch):
        # flake-ok: pre-existing real-time delays that scramble completion order, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(delays[path])
        return verdicts[path]

    monkeypatch.setattr(repository, "_discover_worktrees", AsyncMock(return_value=wts))
    monkeypatch.setattr(worktree_ops, "_prunable", fake_prunable)

    out = await worktree_ops._prune_candidates()

    assert [c["name"] for c in out["candidates"]] == ["wt-a", "wt-c"]
    assert [k["name"] for k in out["kept"]] == ["wt-b", "wt-d"]
    assert out["scanned"] == 4
