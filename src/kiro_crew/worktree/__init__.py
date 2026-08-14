"""Repo-agnostic git worktree support: sandboxed git, the access barrier, and
inspection / recyclability / removal.

``kiro_crew.dashboard.handlers.worktree`` (creation) and
``kiro_crew.dashboard.handlers.worktree_fleet`` (listing and removal) are the HTTP
surface over this package.
"""

from kiro_crew.worktree.access import allowed_repo_roots, match_allowed_root
from kiro_crew.worktree.git_exec import (
    SANDBOX_REFUSAL,
    SandboxUnavailable,
    git_error,
    git_no_repo_code,
    git_toplevel,
    norm_path,
    repo_lock,
    resolve_base_ref,
    run_git,
)
from kiro_crew.worktree.service import (
    RECYCLABLE_VERDICTS,
    RepoWorktrees,
    WorktreeInfo,
    find_worktree,
    inspect_worktree,
    list_worktrees,
    prune_verdict,
    remove_worktree,
    repo_disk_bytes,
)

__all__ = [
    "RECYCLABLE_VERDICTS",
    "SANDBOX_REFUSAL",
    "RepoWorktrees",
    "SandboxUnavailable",
    "WorktreeInfo",
    "allowed_repo_roots",
    "find_worktree",
    "git_error",
    "git_no_repo_code",
    "git_toplevel",
    "inspect_worktree",
    "list_worktrees",
    "match_allowed_root",
    "norm_path",
    "prune_verdict",
    "remove_worktree",
    "repo_disk_bytes",
    "repo_lock",
    "resolve_base_ref",
    "run_git",
]
