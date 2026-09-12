"""Tests for :mod:`kiro_crew.git_worktree_scope`.

The shared decision all four filter-driver guards feed their probe results
into. Only a genuinely ABSENT ``config.worktree`` reads as an inactive scope;
every present entry — regular or not — and every other stat outcome keeps the
scope in so the caller's probe runs and fails closed.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew.git_worktree_scope import worktree_scope_active


def _gitdir(tmp_path):
    d = tmp_path / "repo" / ".git"
    d.mkdir(parents=True)
    return d


def test_absent_file_is_inactive(tmp_path):
    d = _gitdir(tmp_path)
    assert worktree_scope_active(str(d), str(tmp_path)) is False


def test_regular_file_is_active(tmp_path):
    d = _gitdir(tmp_path)
    (d / "config.worktree").write_text("")
    assert worktree_scope_active(str(d), str(tmp_path)) is True


def test_relative_gitdir_joins_onto_base(tmp_path):
    d = _gitdir(tmp_path)
    (d / "config.worktree").write_text("")
    rel = os.path.join("repo", ".git")
    assert worktree_scope_active(rel, str(tmp_path)) is True


def test_empty_gitdir_stays_active_and_fails_closed(tmp_path):
    assert worktree_scope_active("", str(tmp_path)) is True


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is POSIX-only")
def test_fifo_keeps_the_scope_probed(tmp_path):
    """A present-but-non-regular entry must NOT read as an empty scope: git
    still loads the path, so dropping the scope would skip the filter-driver
    probe on exactly the entry an evader would plant. ``os.path.isfile``
    answers False for a FIFO; the lstat-based check keeps the scope in."""
    d = _gitdir(tmp_path)
    os.mkfifo(d / "config.worktree")
    assert worktree_scope_active(str(d), str(tmp_path)) is True


def test_broken_symlink_keeps_the_scope_probed(tmp_path):
    d = _gitdir(tmp_path)
    try:
        (d / "config.worktree").symlink_to(d / "nowhere")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert worktree_scope_active(str(d), str(tmp_path)) is True
