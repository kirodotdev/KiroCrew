"""Archive deletes a channel's branch only when nobody can still need it.

The branch goes only when no worktree has it checked out and every commit on it
is on an independent ref (``origin/HEAD``, else the recorded base), never on
``HEAD``, which in the source checkout can be the branch itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    BRANCH,
    _bind,
    _click,
    _git,
    _passthrough_spawn,
    _register_workspace,
    _wrap,
    env_fixture,
    worktree_fixture,
)
from slack_code_channels_helpers import OWNER

from kiro_crew.dashboard.handlers import worktree as wt
from kiro_crew.slack.code_channels import cleanup, wrapup
from kiro_crew.slack.code_channels.workspace import git_ops

pytestmark = pytest.mark.usefixtures("unsandboxed_git")


@pytest.fixture(name="unsandboxed_git")
def unsandboxed_git_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run cleanup's git without an OS sandbox, as ``env`` does (CI runners have none)."""
    monkeypatch.setattr(wt, "sandboxed_spawn_argv", _passthrough_spawn)
    monkeypatch.setattr(git_ops, "sandboxed_spawn_argv", _passthrough_spawn)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        _git("config", key, value, cwd=root)
    (root / "a.txt").write_text("a\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    return root


def test_a_branch_checked_out_in_the_source_without_origin_head_is_kept(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git("checkout", "-q", "-b", "work", cwd=root)
    (root / "a.txt").write_text("b\n")
    _git("commit", "-q", "-am", "work", cwd=root)
    tip, why = cleanup._safe_tip(str(root), "work")
    assert tip == "" and "checked out" in why


def test_without_origin_head_or_a_recorded_base_the_branch_is_kept(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git("branch", "work", cwd=root)  # merged into HEAD, but HEAD proves nothing
    tip, why = cleanup._safe_tip(str(root), "work")
    assert tip == "" and "origin/HEAD" in why


def _origin_head(root: Path) -> str:
    """Point ``origin/HEAD`` at the current main, as a clone would."""
    sha = _git("rev-parse", "main", cwd=root)
    _git("update-ref", "refs/remotes/origin/main", sha, cwd=root)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=root)
    return sha


def test_a_branch_with_nothing_past_origin_head_may_go(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    base = _origin_head(root)
    _git("branch", "work", cwd=root)
    tip, why = cleanup._safe_tip(str(root), "work")
    assert tip == base and why == ""


def test_a_branch_with_commits_past_origin_head_is_kept(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _origin_head(root)
    _git("checkout", "-q", "-b", "work", cwd=root)
    (root / "a.txt").write_text("b\n")
    _git("commit", "-q", "-am", "work", cwd=root)
    _git("checkout", "-q", "main", cwd=root)
    tip, why = cleanup._safe_tip(str(root), "work")
    assert tip == "" and "commits" in why


def test_a_branch_checked_out_in_the_source_is_kept_even_with_origin_head(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _origin_head(root)
    _git("checkout", "-q", "-b", "work", cwd=root)  # merged, but checked out here
    tip, why = cleanup._safe_tip(str(root), "work")
    assert tip == "" and "checked out" in why


def test_untracked_files_hidden_by_config_still_keep_the_tree(env: Any, worktree: Any) -> None:
    root, tree = worktree
    _git("config", "status.showUntrackedFiles", "no", cwd=root)
    (tree / "notes.txt").write_text("not committed\n")
    record = type("R", (), {"worktree": str(tree), "branch": BRANCH})()
    assert cleanup._keep_reason(record) == "uncommitted changes"


def test_archive_keeps_a_branch_with_its_own_commits_and_says_so(
    env: Any, worktree: Any, monkeypatch: Any
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    base = _git("rev-parse", "HEAD", cwd=root)
    _register_workspace(root, tree, base)
    (tree / "README.md").write_text("work\n")
    _git("commit", "-q", "-am", "channel work", cwd=tree)
    notes: list[str] = []
    monkeypatch.setattr(wrapup, "_notify_branch_kept", lambda _b, reason: notes.append(reason))
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists()
    assert _git("branch", "--list", BRANCH, cwd=root) != ""  # the commits survive
    assert notes and BRANCH in notes[0]
