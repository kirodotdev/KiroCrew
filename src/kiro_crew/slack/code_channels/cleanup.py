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

**Nothing keeps working in a removed tree.** Before the removal, the bound
session, every session filed under the channel's folder, and the folder itself
are pointed back at the bound project; when any of them cannot be moved, the
worktree stays.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
from dataclasses import dataclass, field
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
    # git's registration above is the trust anchor; the project the channel was
    # bound from must also be a checkout of that same repository.
    project = _source_project(binding, record)
    if project and is_sensitive_path(project):
        return "sensitive path"
    root = git_ops.toplevel(project) if project and os.path.isdir(project) else None
    if not root or os.path.realpath(root) != os.path.realpath(root_recorded):
        return "repository is not the bound project's"
    if not _cut_from(root, tree, branch):
        # The tree must sit exactly where a bind from this root puts it, so the
        # record cannot name some other checkout (another channel's tree, say)
        # as the repository and the sessions' home. A tree git adopted at any
        # other path is kept, never removed.
        return "worktree was not cut from the bound project"
    return ""


def _cut_from(root: str, tree: str, branch: str) -> bool:
    """Whether *tree* is the path a bind from *root* on *branch* creates. Blocking."""
    from kiro_crew.dashboard.handlers import worktree as wt

    expected = os.path.join(
        os.path.dirname(root), f"{os.path.basename(root)}-wt-{wt._dir_slug(branch)}"
    )
    # *root* is already physical (git's realpath toplevel), so *expected* is not
    # resolved: a symlink planted at that path must not make it match.
    return os.path.normcase(os.path.normpath(expected)) == os.path.normcase(os.path.realpath(tree))


def _source_project(binding: Binding, record: Any) -> str:
    """The project the channel was bound from, before any worktree. Blocking.

    A git-backed bind stores the WORKTREE (or a directory in it) as
    ``Binding.project_dir``, so on the common path the binding names the tree
    itself. The project it came from is then the one the workspace record kept
    from before the worktree was cut. Otherwise it is the binding's own
    project, or the one the bind resolved (the dashboard default).
    """
    project = binding.project_dir or str(getattr(record, "resolved_project", "") or "")
    if project and record.worktree and _inside(project, str(record.worktree)):
        # The bind kept its own project, else (none given) the session's.
        project = str(
            getattr(record, "project_dir", "") or getattr(record, "resolved_project", "") or ""
        )
    return project


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


def _check_sync(binding: Binding, store: Any) -> tuple[Any, str]:
    """``(record, keep reason)``; ``(None, "")`` when there is no worktree. Blocking."""
    record = store.get(binding.team_id, binding.channel_id)
    if record is None or not record.worktree or not os.path.isdir(record.worktree):
        return None, ""
    return record, provenance_refusal(binding, record) or _keep_reason(record)


def _inside(path: str, tree: str) -> bool:
    """Blocking: whether *path* is *tree* or under it."""
    if not path:
        return False
    real, root = os.path.realpath(path), os.path.realpath(tree)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def _same_path(path: str, other: str) -> bool:
    """Blocking: whether *path* and *other* resolve to the same directory."""
    return bool(path and other) and os.path.realpath(path) == os.path.realpath(other)


def _home_project(binding: Binding, record: Any) -> str:
    """The directory the channel's sessions go back to: the project it was bound from.

    The same project :func:`provenance_refusal` checked against git. Blocking.
    """
    project = _source_project(binding, record)
    if not project or not os.path.isdir(project) or _inside(project, record.worktree):
        return ""
    return project


def _folder_project(state: Any, folder_id: str) -> str | None:
    """The folder's ``project_dir`` (``None`` when there is no such folder)."""
    for folder in list(getattr(state, "_folders", None) or ()):
        if isinstance(folder, dict) and folder.get("id") == folder_id:
            return str(folder.get("project_dir") or "")
    return None


async def _swap_folder_project(state: Any, folder_id: str, expect: str, project: str) -> bool:
    """Set the folder's project to *project* only while it is still *expect*."""
    mutate = getattr(state, "mutate_folders", None)
    if not callable(mutate):
        return False

    def _apply(folders: list[dict[str, Any]]) -> tuple[bool, bool]:
        target = next((f for f in folders if f.get("id") == folder_id), None)
        if target is None:
            return False, True  # gone: nothing points anywhere any more
        if str(target.get("project_dir") or "") != expect:
            # Changed meanwhile: not ours to overwrite, and not moved either, so
            # the caller must not act as if it were (it may still point at the tree).
            return False, False
        target["project_dir"] = project
        return True, True

    try:
        done = bool(await mutate(_apply))
    except Exception:
        logger.warning("code channel folder %s: project not reset", folder_id, exc_info=True)
        return False
    push = getattr(state, "push_slots_update", None)
    if done and callable(push):
        push()
    return done


@dataclass
class _Moved:
    """What :func:`_retarget` moved off the tree, so a kept tree gets it back."""

    sessions: list[tuple[str, str]] = field(default_factory=list)  # (key, its old project)
    folder: tuple[str, str, str] | None = None  # (folder id, old project, new project)
    project: str = ""  # where the sessions were moved to


async def _restore(moved: _Moved, setter: Any, reader: Any = None) -> None:
    """Point what was moved back at the tree it worked in (best effort).

    A session whose project someone changed since the move is left where it is.
    """
    state = surface.dashboard_state()
    if moved.folder is not None and state is not None:
        folder_id, old, new = moved.folder
        await _swap_folder_project(state, folder_id, new, old)
    for key, old in moved.sessions:
        try:
            if reader is not None and moved.project:
                now = str(await reader(key) or "")
                if not await asyncio.to_thread(_same_path, now, moved.project):
                    continue
            if setter is None or not await setter(key, old):
                logger.warning("code channel: session %s not moved back to %s", key, old)
        except Exception:
            logger.warning("code channel: session %s not moved back", key, exc_info=True)


def _project_tools(service: Any) -> tuple[Any, Any]:
    """The project setter and reader: the service's, else the dashboard's own path."""
    setter = getattr(service, "_set_project", None)
    if setter is not None:
        return setter, getattr(service, "_project_of", None)
    from kiro_crew.slack.code_channels.workspace.service import (
        dashboard_project_reader,
        dashboard_project_setter,
    )

    return (
        dashboard_project_setter(surface.dashboard_state),
        dashboard_project_reader(surface.dashboard_state),
    )


def _turn_running(state: Any, key: str, filed: dict[str, tuple[bool, int]]) -> bool:
    """Whether session *key* is mid-turn (the folder scan's answer, else its slot's)."""
    if key in filed:
        return filed[key][0]
    slot = (getattr(state, "_slots", None) or {}).get(key) if state is not None else None
    return bool(getattr(slot, "turn_running", False))


async def _retarget(binding: Binding, record: Any, setter: Any, reader: Any, moved: _Moved) -> str:
    """Move every session and the folder off the worktree; why not ("" when done).

    The bound session and every session filed under the channel's folder (a
    conductor's workers) go back to the bound project before the tree is
    removed, so none keeps working in a deleted directory. Everything is checked
    before anything moves; what did move is recorded in *moved*, so the caller
    moves it back whenever the tree is kept after all.
    """
    from kiro_crew.slack.code_channels.workspace import folders

    tree = str(record.worktree)
    project = await asyncio.to_thread(_home_project, binding, record)
    state = surface.dashboard_state()
    keys = {binding.session_key} if binding.session_key else set()
    filed: dict[str, tuple[bool, int]] = {}
    folder_id = str(getattr(record, "folder_id", "") or "")
    if folder_id and state is not None:
        filed = folders.sessions_in(state, folders.subtree_ids(state, folder_id))
        keys |= set(filed)
    targets: list[tuple[str, str]] = []
    for key in sorted(keys):
        current = tree
        if reader is not None:
            try:
                current = str(await reader(key) or "")
            except Exception:
                current = tree  # unreadable: assume it works in the tree
        # No such session, or one working elsewhere: nothing to move.
        if not await asyncio.to_thread(_inside, current, tree):
            continue
        if _turn_running(state, key, filed):
            return "a session is mid-turn in it"
        targets.append((key, current))
    if targets and (not project or setter is None):
        return "a session still works in it"
    for key, current in targets:
        moved.project = project
        # Recorded before the await: a cancel inside the setter may land after
        # the move took effect. The restore skips a session that never moved.
        moved.sessions.append((key, current))
        if not await setter(key, project):
            moved.sessions.pop()  # refused: it never moved
            return "a session still works in it"
    named = (_folder_project(state, folder_id) if folder_id and state is not None else None) or ""
    folder_in_tree: bool = bool(named) and await asyncio.to_thread(_inside, named, tree)
    if folder_in_tree:
        if not project:
            return "its folder still points at it"
        moved.folder = (folder_id, named, project)  # before the await, as above
        if not await _swap_folder_project(state, folder_id, named, project):
            moved.folder = None  # refused: it never moved
            return "its folder still points at it"
    return ""


def _remove_sync(binding: Binding, store: Any, record: Any, reason: str) -> tuple[str, str]:
    from kiro_crew.slack.code_channels.workspace import git_ops

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
    # The tree is gone: from here on nothing may report it kept, or the caller
    # would point sessions and the folder back at a deleted directory.
    try:
        tip = _safe_tip(record.repo_root, record.branch)
        if tip:
            # Compare-and-delete at the checked sha: a branch that moved since stays.
            git_ops.run_git(
                ["update-ref", "-d", f"refs/heads/{record.branch}", tip], record.repo_root
            )
    except Exception:
        logger.warning(
            "code channel %s: worktree removed, branch kept", binding.channel_id, exc_info=True
        )
    try:
        record.worktree = ""
        store.put(record)
    except Exception:
        logger.warning(
            "code channel %s: worktree removed, record not updated",
            binding.channel_id,
            exc_info=True,
        )
    return REMOVED, ""


def _still_there(tree: str) -> bool:
    """Whether *tree* is still a usable worktree (unknown counts as there). Blocking.

    A tree git deleted part of, leaving the directory without its ``.git``
    link, is gone too: no session may be pointed back at it.
    """
    for path in (tree, os.path.join(tree, ".git")):
        try:
            os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except Exception:
            return True  # EACCES, ELOOP...: can't tell, so it stays
    return True


async def _tree_present(tree: str) -> tuple[bool, bool]:
    """``(present, cancelled)``: :func:`_still_there` off the event loop.

    Awaited through any cancels (reported, not lost), so the answer is always
    used: nothing awaits between it and the outcome it decides.
    """
    probe = asyncio.get_running_loop().run_in_executor(None, _still_there, tree)
    cancelled = False
    while not probe.done():
        try:
            await asyncio.wait({probe})
        except asyncio.CancelledError:
            cancelled = True
    try:
        return bool(probe.result()), cancelled
    except Exception:
        return True, cancelled


async def release_channel_workspace(binding: Binding, *, caller: str) -> tuple[str, str]:
    """Clean up *binding*'s worktree after its channel was archived.

    Returns ``(outcome, reason)``: ``removed``, ``kept`` (with why), or
    ``no_worktree``. A failure keeps everything and never raises; a
    cancellation still moves the sessions back and is then passed on.
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
    moved = _Moved()
    setter: Any = None
    reader: Any = None
    cancelled = False
    tree = ""  # read before the removal thread can clear record.worktree
    attempted = False  # whether OUR removal ran (a tree gone for other reasons isn't ours)
    try:
        setter, reader = _project_tools(service)
        async with quiet:
            record, reason = await asyncio.to_thread(_check_sync, binding, store)
            if record is None:
                outcome, reason = NO_WORKTREE, ""
            else:
                tree = str(record.worktree)
                # Only a tree that is about to go moves its sessions out.
                if not reason:
                    reason = await _retarget(binding, record, setter, reader, moved)
                attempted = not reason
                # A plain executor Future, not a Task: shutdown's cancel-all
                # never cancels it, and nothing else holds it. So however often
                # THIS coroutine is cancelled, the loop below waits (without
                # blocking the event loop) for git's real result, and the
                # restore below always matches the tree that is left.
                removal = asyncio.get_running_loop().run_in_executor(
                    None, functools.partial(_remove_sync, binding, store, record, reason)
                )
                while not removal.done():
                    try:
                        await asyncio.wait({removal})
                    except asyncio.CancelledError:
                        cancelled = True
                outcome, reason = removal.result()
    except asyncio.CancelledError:
        # Cancelled before the removal started: the tree stays, so whatever was
        # moved off it goes back below before the cancellation carries on.
        cancelled = True
        outcome, reason = KEPT, "cleanup cancelled"
    except Exception:
        logger.warning(
            "code channel %s: worktree cleanup failed", binding.channel_id, exc_info=True
        )
        outcome, reason = KEPT, "cleanup failed"
    if outcome == KEPT and attempted and tree:
        present, probe_cancelled = await _tree_present(tree)
        cancelled = cancelled or probe_cancelled
        if not present:
            # Whatever failed, git did remove the tree: never point anything
            # back at a deleted directory, and never report it kept.
            outcome, reason = REMOVED, ""
    if outcome == KEPT and (moved.sessions or moved.folder is not None):
        # The tree stays, so whatever was moved off it goes back.
        try:
            await _restore(moved, setter, reader)
        except Exception:
            logger.warning("code channel %s: restore failed", binding.channel_id, exc_info=True)
    surface.audit(
        caller,
        "slack.code_channel.worktree_release",
        "denied" if outcome == KEPT else "allowed",
        f"{where} result={outcome}",
        reason,
    )
    if cancelled:
        raise asyncio.CancelledError
    return outcome, reason
