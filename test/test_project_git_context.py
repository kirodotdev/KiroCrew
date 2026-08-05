"""Unit tests for `_project_git_context_line` — the agent's git worktree
awareness line injected into the `[PROJECT]` block (issue #1607).

The helper is deliberately PURE FILESYSTEM (no git subprocess), so these tests
fabricate the `.git` layouts git itself would write — a `.git` DIRECTORY with a
`HEAD` for the main worktree, and a `.git` FILE holding a `gitdir:` pointer for
a linked worktree — and assert the branch/kind sentence without needing git or
a sandbox on the host.
"""

from __future__ import annotations

from kiro_crew.context import _project_git_context_line


def _make_main_repo(root, branch="main"):
    """A main worktree: `.git/` directory with a symbolic-ref HEAD."""
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")


def test_main_worktree_reports_branch(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _make_main_repo(repo, "main")
    line = _project_git_context_line(str(repo))
    assert "on branch `main`" in line
    assert "the main worktree" in line
    assert str(repo) in line
    # No cross-session warning for the main worktree.
    assert "sibling worktrees" not in line


def test_linked_worktree_reports_branch_and_warns(tmp_path):
    # Main repo whose $GIT_DIR holds the linked worktree's administrative dir.
    main = tmp_path / "proj"
    main.mkdir()
    _make_main_repo(main, "main")
    wt_admin = main / ".git" / "worktrees" / "feature"
    wt_admin.mkdir(parents=True)
    (wt_admin / "HEAD").write_text("ref: refs/heads/feat/upload-limit\n")

    # Linked worktree: sibling dir whose `.git` is a FILE pointing at wt_admin.
    linked = tmp_path / "proj-wt-upload-limit"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {wt_admin}\n")

    line = _project_git_context_line(str(linked))
    assert "on branch `feat/upload-limit`" in line
    assert "a linked worktree" in line
    assert "sibling worktrees" in line  # the keep-on-branch warning


def test_linked_worktree_relative_gitdir_pointer(tmp_path):
    """git may write a RELATIVE gitdir pointer; it resolves against the tree."""
    main = tmp_path / "proj"
    main.mkdir()
    _make_main_repo(main, "main")
    wt_admin = main / ".git" / "worktrees" / "rel"
    wt_admin.mkdir(parents=True)
    (wt_admin / "HEAD").write_text("ref: refs/heads/feat/rel\n")

    linked = tmp_path / "proj-wt-rel"
    linked.mkdir()
    # Relative pointer from the linked tree up to the admin dir.
    import os

    rel = os.path.relpath(wt_admin, linked)
    (linked / ".git").write_text(f"gitdir: {rel}\n")

    line = _project_git_context_line(str(linked))
    assert "on branch `feat/rel`" in line
    assert "a linked worktree" in line


def test_detached_head(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
    line = _project_git_context_line(str(repo))
    assert "detached HEAD at `0123456789ab`" in line
    assert "the main worktree" in line


def test_finds_repo_from_subdirectory(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _make_main_repo(repo, "dev")
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    line = _project_git_context_line(str(nested))
    assert "on branch `dev`" in line


def test_non_repo_returns_empty(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _project_git_context_line(str(plain)) == ""


def test_missing_path_returns_empty(tmp_path):
    assert _project_git_context_line(str(tmp_path / "does-not-exist")) == ""


def test_linked_worktree_without_gitdir_pointer_returns_empty(tmp_path):
    # A `.git` file that is not a valid `gitdir:` pointer must not crash.
    linked = tmp_path / "weird"
    linked.mkdir()
    (linked / ".git").write_text("not a gitdir pointer\n")
    assert _project_git_context_line(str(linked)) == ""
