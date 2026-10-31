"""Release-channel pin: the fleet-wide surfaces the row touches.

Sibling of ``test_dev_fleet_release_channel_ops.py`` (which holds the Create mutation
and the row's classification) carrying the three places the channel reaches beyond
its own row: the ``_drop_worktrees`` reconciliation a tombstoned removal gets, the
prune verdict that must never preselect the pin, and the background refresher's
fetch shape. Split out for the file line cap; one subject per module.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    release_channel_pin,
    repository,
    runtime,
    worktree_ops,
)
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import await_until


def _async_return(value):
    async def _call(*_args, **_kw):
        return value

    return _call


# --------------------------------------------------------------------------
# tombstoned removal
# --------------------------------------------------------------------------
def test_dropping_the_channels_worktree_demotes_the_channel_to_its_placeholder():
    """A tombstoned removal must take the channel's adopted binding with the row.

    `_drop_worktrees` is the one reconciliation a removal mid-build gets; leaving
    `release_channel.worktree` set would publish a snapshot with no release row and no
    Create either, since the page offers Create only when `worktree` is null.
    """
    name = release_channel_pin.WORKTREE_NAME
    data = {
        "worktrees": [{"name": "main"}, {"name": name}],
        "release_channel": {
            "name": name,
            "worktree": name,
            "version": "0.4.9",
            "tip_version": "0.5.0",
            "at_tip": False,
            "name_taken_by_branch": False,
        },
    }
    out = fleet_state._drop_worktrees(data, {name})
    assert [w["name"] for w in out["worktrees"]] == ["main"]
    assert out["release_channel"]["worktree"] is None
    assert out["release_channel"]["at_tip"] is None
    assert out["release_channel"]["version"] == "0.5.0"  # back to what Create would check out
    assert out["release_channel"]["tip_version"] == "0.5.0"
    # Copied, never mutated: a response in flight may still hold the old dict.
    assert data["release_channel"]["worktree"] == name
    # Dropping an unrelated row leaves the channel alone.
    same = fleet_state._drop_worktrees(data, {"main"})
    assert same["release_channel"] is data["release_channel"]


def test_dropping_the_branch_checkout_holding_the_reserved_name_frees_it():
    """The OTHER holder: a branch checkout under the reserved name, recorded as
    `name_taken_by_branch` with no `worktree`. One directory is one row, so dropping
    the row that carries the name drops the occupant; a flag left set would hide
    Create behind a checkout that is gone until a later rebuild."""
    name = release_channel_pin.WORKTREE_NAME
    data = {
        "worktrees": [{"name": "main"}, {"name": name, "branch": "feat/borrowed"}],
        "release_channel": {
            "name": name,
            "worktree": None,
            "version": "0.5.0",
            "tip_version": "0.5.0",
            "at_tip": None,
            "name_taken_by_branch": True,
        },
    }
    out = fleet_state._drop_worktrees(data, {name})
    assert [w["name"] for w in out["worktrees"]] == ["main"]
    assert out["release_channel"]["name_taken_by_branch"] is False
    assert out["release_channel"]["worktree"] is None
    assert data["release_channel"]["name_taken_by_branch"] is True  # copied, not mutated
    same = fleet_state._drop_worktrees(data, {"main"})
    assert same["release_channel"] is data["release_channel"]


# --------------------------------------------------------------------------
# prune
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_never_offers_the_detached_pin(tmp_path, monkeypatch):
    """`empty` reads clean-and-old as ABANDONMENT, which does not hold for the pin.

    A detached tree at the reserved name has no branch and therefore no PR, and every
    commit it holds is reachable from the base branch -- so `own == 0` is the steady
    state rather than neglect, and it is exactly what the operator asked for.
    """
    tree = tmp_path / release_channel_pin.WORKTREE_NAME
    tree.mkdir()
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((False, [])))
    monkeypatch.setattr(repository, "_git", _async_return("abc123"))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", _async_return(None))
    # Old enough that `empty` would be offered for a branch checkout.
    ManualClock(start=Path(tree).stat().st_ctime + 400_000).install(monkeypatch, worktree_ops)

    verdict = await worktree_ops._prunable(str(tree), None)
    assert verdict["ok"] is False
    assert verdict["code"] == "fresh"


@pytest.mark.asyncio
async def test_prune_still_offers_a_hand_detached_tree_under_any_other_name(tmp_path, monkeypatch):
    """The exemption reaches only what this feature creates.

    A tree the operator detached by hand -- at a release tag or anywhere else -- under
    its own name keeps today's `empty` verdict on age, so Prune merged behaves for every
    install exactly as it did before the channel row existed.
    """
    tree = tmp_path / "kirocrew-wt-experiment"
    tree.mkdir()
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((False, [])))
    monkeypatch.setattr(repository, "_git", _async_return("abc123"))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", _async_return(None))
    ManualClock(start=Path(tree).stat().st_ctime + 400_000).install(monkeypatch, worktree_ops)

    verdict = await worktree_ops._prunable(str(tree), None)
    assert verdict["ok"] is True
    assert verdict["code"] == "empty"


@pytest.mark.asyncio
async def test_prune_still_offers_a_branch_checkout_holding_the_reserved_name(
    tmp_path, monkeypatch
):
    """Both halves are needed: a BRANCH tree is an ordinary feature tree whatever its name."""
    tree = tmp_path / release_channel_pin.WORKTREE_NAME
    tree.mkdir()
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((False, [])))
    monkeypatch.setattr(repository, "_git", _async_return("abc123"))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", _async_return(None))
    ManualClock(start=Path(tree).stat().st_ctime + 400_000).install(monkeypatch, worktree_ops)

    verdict = await worktree_ops._prunable(str(tree), "feat/borrowed-the-name")
    assert verdict["ok"] is True
    assert verdict["code"] == "empty"


# --------------------------------------------------------------------------
# refresher
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_refresher_fetches_the_base_branch_and_the_mirror_in_separate_spawns(
    monkeypatch, tmp_path
):
    """Two fetches per cycle, each its own argv, so their failures stay apart.

    The base branch is what every row's Behind is measured against and is fetched as
    it was before the channel existed; the mirror refspec is the channel's. A mirror
    the remote cannot serve must not leave Behind stale, and here it does not: the base
    fetch runs first and on its own, the mirror fetch fails, and the fleet is still
    rebuilt.
    """
    calls: list[list[str]] = []
    rebuilt = asyncio.Event()

    async def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if release_channel_pin._TAG_REFSPEC in cmd:
            return 128, "", "fatal: could not read from remote repository"
        return 0, "", ""

    async def fake_refresh():
        rebuilt.set()
        return {}

    monkeypatch.setattr(repository, "_repo", lambda: str(tmp_path))
    monkeypatch.setattr(repository, "_upstream_remote", _async_return("origin"))
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    monkeypatch.setattr(fleet_state, "_fleet_refresh", fake_refresh)
    task = asyncio.create_task(worktree_ops._status_refresher())
    try:
        await await_until(rebuilt, timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    fetches = [cmd for cmd in calls if "fetch" in cmd]
    assert len(fetches) == 2, fetches
    base, mirror = fetches
    assert repository.BASE_BRANCH in base
    assert release_channel_pin._TAG_REFSPEC not in base
    assert release_channel_pin._TAG_REFSPEC in mirror
    assert repository.BASE_BRANCH not in mirror
    assert "--prune" not in base, "pruning is the mirror fetch's job, scoped to its namespace"
