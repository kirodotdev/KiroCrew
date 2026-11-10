"""Name the cause when a worktree is invisible because it is in scratch.

Dev Fleet runs its git in the agent sandbox, which masks the whole scratch root
(``sandbox_leaves``: one session must not read another session's scratch). So a
worktree created under any session's ``$KIROCREW_SCRATCH`` reads as ``prunable``
to that git and discovery drops it, which on its own reads as a bare "worktree
not found". The gateway process is not sandboxed, so it can still see where the
recorded path is and say so.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from kiro_crew.agent_scratch import scratch_root


def scratch_worktree_entry(entries: list[dict], name: str) -> dict | None:
    """The record of a prunable worktree named *name* under the scratch root.

    Blocking (resolves paths): run it on the executor.
    """
    root = os.path.realpath(scratch_root())
    for entry in entries:
        path = entry.get("path") or ""
        if entry.get("is_main") or not entry.get("prunable") or Path(path).name != name:
            continue
        try:
            inside = os.path.commonpath([root, os.path.realpath(path)]) == root
        except ValueError:  # different drives on Windows
            inside = False
        if inside:
            return entry
    return None


def scratch_worktree_error(name: str, entry: dict, repo: str) -> str:
    """The refusal: the cause, and the commands that recreate it under ``.worktrees``.

    Remove-then-add, not ``git worktree move``: in an agent's sandboxed shell the
    scratch dir is its own mount, so the move's ``rename`` fails with
    ``Invalid cross-device link``. ``worktree remove`` without ``--force`` refuses
    a tree with uncommitted or untracked work, so nothing is lost silently.

    A detached tree first gets a branch named after it: removing it deletes its
    HEAD, so a commit made after this refusal would otherwise be left on no ref.

    The chain opens with ``test -d``: the match is by name across the whole
    scratch root, and in any other session's shell the path is masked, where
    ``worktree remove`` on a missing path exits 0 and drops the owner's record
    even if its tree is dirty, so the ``add`` would then take its branch.
    """
    path = entry.get("path") or ""
    q_path = shlex.quote(path)
    branch = entry.get("branch")
    switch = ""
    if not branch:
        branch = name
        switch = f"git -C {q_path} switch -c {shlex.quote(branch)} && "
    q_repo = shlex.quote(repo)
    target = shlex.quote(f"{repo}/.worktrees/{name}")
    return (
        f"worktree {name!r} is in a session's private scratch directory ({path}), "
        "which Dev Fleet cannot see, so it cannot start a pod from it. Recreate it "
        f"under {repo}/.worktrees/. Only the session that owns that scratch directory "
        "can run this: commit your changes first, then run "
        f"test -d {q_path} && {switch}git -C {q_repo} worktree remove {q_path} && "
        f"git -C {q_repo} worktree add {target} {shlex.quote(branch)}"
    )
