"""A worktree under the scratch root is refused with its cause, not "not found".

Dev Fleet's git runs in the agent sandbox, which masks the whole scratch root, so
such a worktree comes back ``prunable`` and discovery drops it.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import scratch_worktrees

NAME = "wt-in-scratch"
HEAD = "0123456789abcdef0123456789abcdef01234567"


def _entries(
    main: Path, worktree: Path, *, prunable: bool, branch: str | None = None
) -> list[dict]:
    entry = {"path": str(worktree), "head": HEAD, "branch": branch, "is_main": False}
    if prunable:
        entry["prunable"] = "gitdir file points to non-existent location"
    return [{"path": str(main), "branch": "main", "is_main": True}, entry]


async def _find(tmp_path: Path, worktree: Path, *, prunable: bool, branch: str | None = None):
    main = tmp_path / "repo"
    with (
        patch.object(
            repository_mod,
            "_worktree_porcelain_entries",
            new_callable=AsyncMock,
            return_value=_entries(main, worktree, prunable=prunable, branch=branch),
        ),
        patch.object(scratch_worktrees, "scratch_root", return_value=tmp_path / "scratch"),
        patch.object(repository_mod, "_repo", return_value=str(main)),
    ):
        return await repository_mod._find_worktree(NAME)


@pytest.mark.asyncio
async def test_a_worktree_hidden_by_the_scratch_mask_names_its_cause(tmp_path):
    hidden = tmp_path / "scratch" / "runtime-abc" / NAME
    hidden.mkdir(parents=True)

    target, err = await _find(tmp_path, hidden, prunable=True)

    assert target is None
    assert err is not None and err != f"worktree not found: {NAME}"
    assert "scratch" in err and str(hidden) in err
    repo = shlex.quote(str(tmp_path / "repo"))
    dest = shlex.quote(f"{tmp_path / 'repo'}/.worktrees/{NAME}")
    # A detached tree gets a branch first, so a commit made after the refusal
    # survives the remove, and is recreated on that branch, not the stale head.
    # The chain opens with `test -d`: in another session's shell the path is
    # masked, and `worktree remove` on a missing path would drop the owner's
    # record and let the `add` take its branch.
    assert err.endswith(
        f"run test -d {shlex.quote(str(hidden))} && "
        f"git -C {shlex.quote(str(hidden))} switch -c {NAME} && "
        f"git -C {repo} worktree remove {shlex.quote(str(hidden))} && "
        f"git -C {repo} worktree add {dest} {NAME}"
    )
    assert "Only the session that owns that scratch directory can run this" in err
    assert HEAD not in err and "--detach" not in err


@pytest.mark.asyncio
async def test_a_scratch_worktree_on_a_branch_is_recreated_on_that_branch(tmp_path):
    hidden = tmp_path / "scratch" / "runtime-abc" / NAME
    hidden.mkdir(parents=True)

    _, err = await _find(tmp_path, hidden, prunable=True, branch="crew/me/fix")

    repo = shlex.quote(str(tmp_path / "repo"))
    dest = shlex.quote(f"{tmp_path / 'repo'}/.worktrees/{NAME}")
    assert err is not None
    assert err.endswith(
        f"run test -d {shlex.quote(str(hidden))} && "
        f"git -C {repo} worktree remove {shlex.quote(str(hidden))} && "
        f"git -C {repo} worktree add {dest} crew/me/fix"
    )
    assert "Only the session that owns that scratch directory can run this" in err
    assert "switch -c" not in err
    # Never `worktree move`: it fails across the sandbox's scratch mount.
    assert "worktree move" not in err


@pytest.mark.asyncio
async def test_a_really_missing_worktree_outside_scratch_stays_not_found(tmp_path):
    gone = tmp_path / "elsewhere" / NAME

    target, err = await _find(tmp_path, gone, prunable=True)

    assert (target, err) == (None, f"worktree not found: {NAME}")


@pytest.mark.asyncio
async def test_a_visible_worktree_is_still_found(tmp_path):
    visible = tmp_path / "repo" / ".worktrees" / NAME
    visible.mkdir(parents=True)

    target, err = await _find(tmp_path, visible, prunable=False)

    assert err is None and target is not None and target["path"] == str(visible)
