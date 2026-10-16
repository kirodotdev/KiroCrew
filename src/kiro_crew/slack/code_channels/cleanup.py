"""The archive's worktree cleanup: remove a channel's worktree only when nothing is lost.

Called by the wrap-up's Archive (:mod:`.wrapup`) after Slack archived the channel.
It runs under the workspace service's channel lock with no bind in flight
(:meth:`WorkspaceService.channel_quiet`), so a refresh or a bind finishes first,
and every git call goes through the workspace's sandboxed runner.

**The record is checked before it is trusted.** The workspace record is a file
under the data home; before anything is removed, the record's worktree, branch
and base are checked against what the gateway itself holds
(:func:`provenance_refusal`): the branch must be this channel's own and git must
register the recorded worktree for it (``git_ops.is_channel_worktree``), the
repository must be the one the sealed binding's project lives in, and neither
path may be sensitive.

**Nothing that could be lost is removed.** The worktree stays when it has
uncommitted or untracked changes, ignored files (``worktree remove`` would delete
those too), is not on its channel branch (a detached HEAD may hold commits no ref
does), or git cannot answer. The removal is ``git worktree remove`` without
``--force``. The branch is deleted only when its tip is already on the
repository's default branch (resolved from the repository, never from the
record), so commits always stay reachable. Every decision is SEL-audited.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from kiro_crew.security import is_sensitive_path
from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.store import Binding

logger = logging.getLogger(__name__)

REMOVED = "removed"
KEPT = "kept"
NO_WORKTREE = "no_worktree"


def provenance_refusal(binding: Binding, record: Any) -> str:
    """Why *record* must not drive a destructive cleanup ("" when it may). Blocking."""
    from kiro_crew.slack.code_channels.workspace import git_ops

    tree, root_recorded, branch = str(record.worktree), str(record.repo_root), str(record.branch)
    if is_sensitive_path(tree) or is_sensitive_path(root_recorded):
        return "sensitive path"
    if not git_ops.is_channel_worktree(
        root_recorded, tree, branch, binding.team_id, binding.channel_id
    ):
        return "record does not name this channel's registered worktree"
    # The binding's project when it named one; else the project the bind resolved
    # (the dashboard default). git's registration above is the trust anchor.
    project = binding.project_dir or str(getattr(record, "resolved_project", "") or "")
    root = git_ops.toplevel(project) if project and os.path.isdir(project) else None
    if not root or os.path.realpath(root) != os.path.realpath(root_recorded):
        return "repository is not the bound project's"
    return ""


def _keep_reason(record: Any) -> str:
    """Why removing the worktree would lose something ("" when it would not). Blocking."""
    from kiro_crew.slack.code_channels.workspace import git_ops

    tree = str(record.worktree)
    status = git_ops.run_git(
        ["--no-optional-locks", "status", "--porcelain", "--ignore-submodules=all"], tree
    )
    if status.returncode != 0:
        return "git status unavailable"
    if status.stdout.strip():
        return "uncommitted changes"
    ignored = git_ops.run_git(
        ["--no-optional-locks", "status", "--porcelain", "--ignored=matching"], tree
    )
    if ignored.returncode != 0:
        return "ignored-file listing unavailable"
    if any(line.startswith("!!") for line in ignored.stdout.splitlines()):
        return "ignored files present"
    if git_ops.current_branch(tree) != record.branch:
        return "worktree is not on its channel branch"
    return ""


def _safe_tip(root: str, branch: str) -> str:
    """The tip of *branch* when every commit on it is on the default branch, else "".

    The caller deletes the branch only at THIS sha (compare-and-delete), so a
    branch that moved after the check keeps its new commits. Blocking.
    """
    from kiro_crew.slack.code_channels.workspace import git_ops

    tip = git_ops.resolve_commit(root, f"refs/heads/{branch}")
    trusted = git_ops.resolve_commit(root, git_ops._wt._resolve_base_ref(root))
    if tip and trusted and git_ops.merge_base(root, tip, trusted) == tip:
        return tip
    return ""


def _release_sync(binding: Binding, store: Any) -> tuple[str, str]:
    from kiro_crew.slack.code_channels.workspace import git_ops

    record = store.get(binding.team_id, binding.channel_id)
    if record is None or not record.worktree or not os.path.isdir(record.worktree):
        return NO_WORKTREE, ""
    reason = provenance_refusal(binding, record) or _keep_reason(record)
    if not reason:
        removed = git_ops.run_git(["worktree", "remove", record.worktree], record.repo_root)
        if removed.returncode != 0:
            reason = "git refused to remove the worktree"
    if reason:
        logger.warning(
            "code channel %s archived: worktree %s kept (%s)",
            binding.channel_id,
            record.worktree,
            reason,
        )
        return KEPT, reason
    tip = _safe_tip(record.repo_root, record.branch)
    if tip:
        # Compare-and-delete at the checked sha: a branch that moved since stays.
        git_ops.run_git(["update-ref", "-d", f"refs/heads/{record.branch}", tip], record.repo_root)
    record.worktree = ""
    store.put(record)
    return REMOVED, ""


async def release_channel_workspace(binding: Binding, *, caller: str) -> tuple[str, str]:
    """Clean up *binding*'s worktree after its channel was archived.

    Returns ``(outcome, reason)``: ``removed``, ``kept`` (with why), or
    ``no_worktree``. Never raises; a failure keeps everything.
    """
    from kiro_crew.slack.code_channels.workspace import core as workspace

    from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

    service = getattr(workspace, "_SERVICE", None)
    # The service's own store, so this write and its binds share one lock.
    store = service.store if service is not None else WorkspaceStore()
    quiet = (
        service.channel_quiet(binding.team_id, binding.channel_id)
        if service is not None
        else contextlib.nullcontext()
    )
    where = f"channel={binding.channel_id}"
    try:
        async with quiet:
            outcome, reason = await asyncio.to_thread(_release_sync, binding, store)
    except Exception:
        logger.warning(
            "code channel %s: worktree cleanup failed", binding.channel_id, exc_info=True
        )
        outcome, reason = KEPT, "cleanup failed"
    surface.audit(
        caller,
        "slack.code_channel.worktree_release",
        "denied" if outcome == KEPT else "allowed",
        f"{where} result={outcome}",
        reason,
    )
    return outcome, reason
