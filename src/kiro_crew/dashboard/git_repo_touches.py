"""Which git repositories a chat session has worked in, for the Git tab.

Most sessions start in the shared workspace, which is not itself a repository,
and then edit files or run commands in repositories elsewhere on disk, where a
Git tab reading only the session's project directory has nothing to show. This
module notices the repositories a session's tool calls
touch -- a file the agent writes, a shell command's working directory, a leading
``cd <dir>`` or ``git -C <dir>`` -- and keeps their roots on the slot.

The roots are server-derived from the session's own tool calls, never taken from
a request, so the Git routes can admit them alongside the project directories
they already allow (``project_dirs._slot_git_repo_snapshot``).

Three halves, split by where they may run:

* :func:`touch_candidates` -- pure string work on one tool call's params.
* :func:`resolve_repo_roots` -- filesystem walk, worker thread only.
* :func:`record_repo_roots` -- slot mutation, event loop only.

:func:`note_tool_call` chains them for the chat runner and never raises: noticing
a repository is a display convenience and must never fail a turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from kiro_crew.platform.tool_paths import edit_target_candidates, is_edit_call
from kiro_crew.security import is_sensitive_path

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot

logger = logging.getLogger(__name__)

#: Repositories one slot remembers. The Git tab polls each one it shows, so the
#: list stays short; the least recently touched root is dropped first.
MAX_SLOT_GIT_REPOS = 24

#: Evicted roots one slot remembers by path, so the Git tab can say how many
#: repositories the capped list leaves out without double-counting a revisit.
#: Older evicted roots are kept only as a count.
MAX_SLOT_EVICTED_GIT_REPOS = 256

#: Candidate paths taken from one tool call. A real call names one or two; the
#: cap bounds the filesystem walk a pathological call could ask for.
_MAX_CANDIDATES_PER_CALL = 16

#: Depth ceiling for a walk-up looking for a repository root (``.git``). A path
#: nested deeper than this below its root is treated as not in a repository
#: rather than paying an unbounded number of stat calls. Shared with the project
#: branch probe (``file_api.project_dirs._project_git_branch``), which reads it
#: off the ``handlers.files`` facade.
_GIT_ROOT_WALK_LIMIT = 40

#: Paths per transcript seed. A long session can hold thousands of file-change
#: rows; the newest are the ones worth listing.
_MAX_SEED_PATHS = 256

#: Shell working-directory argument names across the supported agents.
_SHELL_CWD_KEYS = ("working_dir", "cwd", "workdir", "working_directory")

# A directory token: double-quoted, single-quoted, or a bare run of non-space,
# non-separator characters. Only absolute or ``~`` paths are kept afterwards.
_DIR_TOKEN = r"(\"[^\"]+\"|'[^']+'|[^\s;&|()]+)"
# ``cd <dir>`` at the start of the command or after a separator.
_CD_RE = re.compile(r"(?:^|&&|\|\||;|\n)\s*(?:builtin\s+)?(?:cd|pushd)\s+" + _DIR_TOKEN)
# ``git -C <dir>`` anywhere in the command.
_GIT_C_RE = re.compile(r"(?:^|[\s;&|(])git\s+-C\s+" + _DIR_TOKEN)


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _command_texts(command: Any) -> list[str]:
    """A shell command's scannable strings. Agents send a string or an argv
    list; each argv element is scanned on its own, since the script of a
    ``bash -lc <script>`` call is one element and its ``cd`` opens that string.
    """
    if isinstance(command, str):
        return [command] if command else []
    if isinstance(command, (list, tuple)):
        return [part for part in command if isinstance(part, str) and part]
    return []


def _command_dirs(command: str) -> list[str]:
    """Directories a shell command explicitly enters or points git at."""
    found: list[str] = []
    for regex in (_CD_RE, _GIT_C_RE):
        for match in regex.finditer(command):
            path = _unquote(match.group(1))
            if path.startswith(("/", "~")):
                found.append(path)
    return found


def touch_candidates(
    raw_params: Mapping | None,
    *,
    tool_kind: str = "",
    diff_path: str = "",
    is_shell: bool = False,
) -> list[str]:
    """Paths one tool call worked in, in the order the call names them.

    Pure string work, no filesystem access, so it is safe on the event loop.
    Edit calls contribute their target files; shell calls their working
    directory and any ``cd``/``git -C`` directory in the command. Reads are not
    counted: the tab tracks where changes are being made.
    """
    out: list[str] = []
    if is_edit_call(tool_kind, diff_path):
        out.extend(edit_target_candidates(raw_params, diff_path))
    if is_shell and isinstance(raw_params, Mapping):
        for key in _SHELL_CWD_KEYS:
            value = raw_params.get(key)
            if isinstance(value, str) and value:
                out.append(value)
        for command in _command_texts(raw_params.get("command")):
            out.extend(_command_dirs(command))
    seen: set[str] = set()
    unique: list[str] = []
    for path in out:
        if isinstance(path, str) and path and path not in seen:
            seen.add(path)
            unique.append(path)
        if len(unique) >= _MAX_CANDIDATES_PER_CALL:
            break
    return unique


def _ceiling_dirs() -> set[str]:
    """``GIT_CEILING_DIRECTORIES`` as git reads it, so the walk stops where git's does."""
    raw = os.environ.get("GIT_CEILING_DIRECTORIES", "")
    return {os.path.normpath(p) for p in raw.split(os.pathsep) if p}


def _walk_to_git_root(path: str) -> str | None:
    """The nearest ancestor of *path* (inclusive) holding a ``.git`` entry.

    ``.git`` is probed for existence, not ``isdir``: a linked worktree's ``.git``
    is a file. A path that does not exist yet (a file the call is creating, in a
    directory it is creating) still finds its repository through its parents.
    Like git, the walk never enters a ``GIT_CEILING_DIRECTORIES`` entry above
    the starting directory.
    """
    ceilings = _ceiling_dirs()
    cur = path
    for _ in range(_GIT_ROOT_WALK_LIMIT):
        if cur != path and cur in ceilings:
            return None
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def resolve_repo_roots(candidates: Iterable[str], anchor: str = "") -> list[str]:
    """Distinct real repository roots for *candidates*. Worker thread only.

    A relative candidate resolves against *anchor* (the session's project) and
    is dropped when there is none: the gateway's own working directory is not
    the agent's, so guessing would list an unrelated repository. The home
    directory and the filesystem root are never listed even when they are
    repositories (a dotfiles repo): a status over all of home is not what the
    user means by "this chat's changes", and it would scan the whole tree on
    every poll. Sensitive roots are dropped too.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    roots: list[str] = []
    for raw in candidates:
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            if not anchor:
                continue
            path = os.path.join(os.path.expanduser(anchor), path)
        found = _walk_to_git_root(os.path.normpath(path))
        if found is None:
            continue
        root = os.path.realpath(found)
        is_fs_root = os.path.dirname(root) == root
        if root == home or is_fs_root or root in roots:
            continue
        if is_sensitive_path(root):
            continue
        roots.append(root)
    return roots


def record_repo_roots(slot: _ChatSlot, roots: Iterable[str], *, newest: bool = True) -> bool:
    """Merge *roots* into the slot's list. Event loop only; returns whether it changed.

    The list is ordered oldest to newest touch. ``newest=True`` (a live tool
    call) moves each root to the newest end; ``newest=False`` (a transcript
    seed, which is older than anything noticed live) adds only unknown roots,
    at the oldest end, so a seed never reorders live activity.

    Roots the cap drops go to ``_git_repos_evicted`` (distinct, never also
    held, oldest first), and a root recorded again leaves it. Past
    ``MAX_SLOT_EVICTED_GIT_REPOS`` the oldest evicted roots are forgotten into
    ``_git_repos_evicted_overflow``, so :func:`omitted_repo_count` is exact
    until then and an upper estimate after: a forgotten root that returns and
    is evicted again is counted twice.
    """
    held: list[str] = list(getattr(slot, "_git_repos", None) or [])
    evicted: list[str] = list(getattr(slot, "_git_repos_evicted", None) or [])
    overflow: int = getattr(slot, "_git_repos_evicted_overflow", 0)
    before = (list(held), list(evicted), overflow)
    for root in roots:
        if newest:
            if root in held:
                held.remove(root)
            held.append(root)
        elif root not in held:
            held.insert(0, root)
        if root in evicted:
            evicted.remove(root)
        if len(held) > MAX_SLOT_GIT_REPOS:
            evicted.extend(held[:-MAX_SLOT_GIT_REPOS])
            held = held[-MAX_SLOT_GIT_REPOS:]
        if len(evicted) > MAX_SLOT_EVICTED_GIT_REPOS:
            forgotten = len(evicted) - MAX_SLOT_EVICTED_GIT_REPOS
            overflow += forgotten
            evicted = evicted[forgotten:]
    slot._git_repos = held
    slot._git_repos_evicted = evicted
    slot._git_repos_evicted_overflow = overflow
    return (held, evicted, overflow) != before


def omitted_repo_count(slot: _ChatSlot) -> int:
    """Repositories this slot worked in that its capped list leaves out."""
    evicted = getattr(slot, "_git_repos_evicted", None) or []
    return len(evicted) + getattr(slot, "_git_repos_evicted_overflow", 0)


def transcript_change_paths(messages: Iterable[Mapping[str, Any]]) -> list[str]:
    """File paths the transcript's file-change rows name, newest first. Pure.

    Seeds the list for a slot restored after a gateway restart, when the live
    tool calls that found its repositories are gone. Only edits survive in the
    transcript this way; a repository the agent only ran commands in reappears
    the next time it works there.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for message in reversed(list(messages)):
        meta = message.get("meta") if isinstance(message, Mapping) else None
        changes = meta.get("file_changes") if isinstance(meta, Mapping) else None
        if not isinstance(changes, list):
            continue
        for change in changes:
            path = change.get("path") if isinstance(change, Mapping) else None
            if isinstance(path, str) and path and path not in seen:
                seen.add(path)
                paths.append(path)
                if len(paths) >= _MAX_SEED_PATHS:
                    return paths
    return paths


async def note_tool_call(
    slot: _ChatSlot,
    raw_params: Mapping | None,
    *,
    tool_kind: str = "",
    diff_path: str = "",
    is_shell: bool = False,
) -> None:
    """Record the repositories one tool call worked in. Never raises."""
    try:
        candidates = touch_candidates(
            raw_params, tool_kind=tool_kind, diff_path=diff_path, is_shell=is_shell
        )
        if not candidates:
            return
        roots = await asyncio.to_thread(
            resolve_repo_roots, candidates, getattr(slot, "project", "") or ""
        )
        if roots:
            record_repo_roots(slot, roots)
    except Exception:  # noqa: BLE001 - display bookkeeping must never fail a turn
        logger.debug("could not note git repositories for a tool call", exc_info=True)
