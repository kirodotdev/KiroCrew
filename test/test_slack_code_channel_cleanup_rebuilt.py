"""Archive still cleans up after the channel's workspace record was rebuilt.

A rebuild (an unarchive, a lost record) runs while the session already works in
the channel's worktree, so the record's own project fields name the tree; the
checkout it was cut from survives only as ``repo_root``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    BRANCH,
    CH,
    SLOT,
    _bind,
    _click,
    _git,
    _wrap,
    env_fixture,
    worktree_fixture,
)
from slack_code_channels_helpers import OWNER

from kiro_crew.slack.code_channels import cleanup, wrapup
from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord, WorkspaceStore


def _rebuilt(root: Any, tree: Any, subdir: str = "") -> None:
    WorkspaceStore().put(
        WorkspaceRecord(
            team_id="T1",
            channel_id=CH,
            session_key=SLOT,
            project_dir=str(tree),
            resolved_project=str(tree),
            repo_root=str(root),
            subdir=subdir,
            worktree=str(tree),
            branch=BRANCH,
            base_sha=_git("rev-parse", "HEAD", cwd=root),
        )
    )


def test_archive_removes_a_clean_worktree_whose_record_was_rebuilt(env: Any, worktree: Any) -> None:
    root, tree = worktree
    _bind(env, project=str(tree))
    _rebuilt(root, tree)
    main = _git("rev-parse", "main", cwd=root)
    _git("update-ref", "refs/remotes/origin/main", main, cwd=root)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=root)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists()
    assert any(
        "result=removed" in r["resources"]
        for r in env.sel.ops("slack.code_channel.worktree_release")
    )


def test_the_source_of_a_rebuilt_record_is_its_repo_root_and_subdir(worktree: Any) -> None:
    root, tree = worktree
    (root / "pkg").mkdir()
    binding = type("B", (), {"project_dir": str(tree / "pkg")})()
    record = WorkspaceRecord(
        team_id="T1",
        channel_id=CH,
        project_dir=str(tree / "pkg"),
        resolved_project=str(tree / "pkg"),
        repo_root=str(root),
        subdir="pkg",
        worktree=str(tree),
    )
    assert cleanup._source_project(binding, record) == str(root / "pkg")
