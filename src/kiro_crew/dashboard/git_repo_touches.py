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

:func:`note_tool_call` retains only bounded candidate strings per proposed call.
The runner's successful terminal result releases them through
:func:`finish_tool_call`; refusals, cancellations and ambiguous failures discard
them. One walker task per slot (:func:`_drain_walks`) resolves executed candidates
in a worker thread. The runner never waits on a ``stat``: a stalled mount holds
the walker, not the turn's event stream. Bookkeeping never raises: noticing a
repository is a display convenience and must never fail a turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Iterable, Mapping
from itertools import islice
from typing import TYPE_CHECKING, Any

from kiro_crew.platform.tool_paths import edit_target_candidates, edit_target_strings, is_edit_call
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

#: Longest path, in characters, this module keeps. Every candidate, root and
#: transcript seed is checked against it before it is retained: a longer one
#: is dropped, never shortened, because a cut path names a different target.
#: 4096 is Linux's PATH_MAX, longer than any path a real repository sits at.
MAX_REPO_PATH_CHARS = 4096

#: Candidate paths taken from one tool call. A real call names one or two; the
#: cap bounds the filesystem walk a pathological call could ask for.
_MAX_CANDIDATES_PER_CALL = 16

#: Unfinished tool calls retained per slot, oldest proposed call dropped first.
#: Each value holds only the bounded candidate strings, never tool parameters.
MAX_PENDING_TOOL_CALLS = 64

#: Command text scanned for directories. The budget accommodates a full batch
#: of maximum-length paths, but never scans a command shortened mid-token.
_MAX_COMMAND_CHARS = MAX_REPO_PATH_CHARS * _MAX_CANDIDATES_PER_CALL


class RootWalkLimitReached(Exception):
    """The ancestor budget ended before discovery established absence."""


class _DiffPath(str):
    """A raw diff-block path needing anchoring validation on the walker thread.

    Keeping the provenance on a string retains only the bounded path, not the
    params mapping or file content. Params paths may be relative to the slot's
    project; a diff-block path must pass the edit extractor's anchoring check.
    """

    __slots__ = ()


#: Distinct candidate paths one slot's walker holds while a walk is in flight.
#: A ``stat`` that hangs on a dead mount holds the walker, and the session keeps
#: calling tools meanwhile, so the backlog is capped: the oldest path gives way
#: first, and a repository only it named is noticed again by the next call that
#: works there. Sized for a burst of edits across a few repositories.
_MAX_PENDING_CANDIDATES_PER_SLOT = 64

#: Depth ceiling for :func:`find_git_root`. A path nested deeper than this
#: below its root is treated as not in a repository rather than paying an
#: unbounded number of stat calls. Also on the ``handlers.files`` facade, where
#: the project branch probe's tests read it.
_GIT_ROOT_WALK_LIMIT = 40

#: Paths per transcript seed. A long session can hold thousands of file-change
#: rows; the newest are the ones worth listing.
_MAX_SEED_PATHS = 256

#: Shell working-directory argument names across the supported agents.
_SHELL_CWD_KEYS = ("working_dir", "cwd", "workdir", "working_directory")

# A directory token: double-quoted, single-quoted, or a bare run of non-space,
# non-separator characters. Only absolute or ``~`` paths are kept afterwards.
_DIR_TOKEN = r"(\"[^\"]+\"|'[^']+'|[^\s;&|()]+)"
# ``cd <dir>`` at the start of a line or after a separator. The gap before
# ``cd`` is spaces and tabs only, and line starts come from ``re.MULTILINE``:
# a gap that could match newlines would let every newline in a long run start
# a match that rescans the rest of the run, quadratic on the event loop.
_CD_RE = re.compile(
    r"(?:^|&&|\|\||;)[ \t]*(?:builtin[ \t]+)?(?:cd|pushd)[ \t]+" + _DIR_TOKEN, re.MULTILINE
)
# ``git -C <dir>`` anywhere in the command.
_GIT_C_RE = re.compile(r"(?:^|[\s;&|(])git\s+-C\s+" + _DIR_TOKEN)


def _path_fits(path: str) -> bool:
    """Bound every retained path by :data:`MAX_REPO_PATH_CHARS`.

    Dropping, not truncating, preserves filesystem identity. This also bounds
    the candidates a stalled walker holds and the roots remembered after eviction.
    """
    return bool(path) and len(path) <= MAX_REPO_PATH_CHARS


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


class TouchCandidates(list[str]):
    """Bounded strings with a loss bit, including an empty but incomplete scan."""

    def __init__(self, values: Iterable[str] = (), *, truncated: bool = False) -> None:
        super().__init__(values)
        self.truncated = truncated


def _command_texts(command: Any) -> TouchCandidates:
    """Bounded strings from a shell command or argv; never retain a whole argv."""
    parts = [command] if isinstance(command, str) else command
    found = TouchCandidates()
    if not isinstance(parts, (list, tuple)):
        return found
    found.truncated = len(parts) > _MAX_CANDIDATES_PER_CALL
    for part in islice(parts, _MAX_CANDIDATES_PER_CALL):
        if isinstance(part, str) and part:
            if len(part) > _MAX_COMMAND_CHARS:
                found.truncated = True
            else:
                found.append(part)
    return found


def _command_dirs(command: str) -> TouchCandidates:
    """Directories a shell command explicitly enters or points git at."""
    found = TouchCandidates()
    if len(command) > _MAX_COMMAND_CHARS:
        found.truncated = True
        return found
    seen: set[str] = set()
    for regex in (_CD_RE, _GIT_C_RE):
        for match in regex.finditer(command):
            path = _unquote(match.group(1))
            # ``os.path.isabs`` is string-only, and on Windows it is what
            # accepts ``C:\repo`` and UNC paths; a leading ``/`` is accepted
            # on its own because Python 3.13 on Windows does not count a
            # rooted path as absolute.
            if not (path.startswith(("/", "~")) or os.path.isabs(path)):
                continue
            if not _path_fits(path):
                found.truncated = True
                continue
            if path not in seen:
                if len(found) >= _MAX_CANDIDATES_PER_CALL:
                    found.truncated = True
                    return found
                seen.add(path)
                found.append(path)
    return found


def touch_candidates(
    raw_params: Mapping | None,
    *,
    tool_kind: str = "",
    diff_path: str = "",
    is_shell: bool = False,
) -> TouchCandidates:
    """Bounded raw paths one call worked in; string/container work only.

    Edit calls contribute target spellings without account/path expansion;
    diff-only paths keep their provenance for worker-thread anchoring checks.
    Shell calls contribute cwd and explicit command directories. No params
    mapping, command text or document body survives in the returned snapshot.
    """
    out = TouchCandidates()
    kind = tool_kind if isinstance(tool_kind, str) and _path_fits(tool_kind) else ""
    if is_edit_call(kind, diff_path):
        paths = edit_target_strings(raw_params)
        out.truncated = paths.truncated
        for path in paths:
            if _path_fits(path):
                out.append(path)
            elif path:
                out.truncated = True
        if diff_path and not _path_fits(diff_path):
            out.truncated = True
        if (
            not paths.truncated
            and not paths.unanchored
            and _path_fits(diff_path)
            and diff_path not in out
        ):
            out.append(_DiffPath(diff_path))
    if is_shell and isinstance(raw_params, Mapping):
        for key in _SHELL_CWD_KEYS:
            value = raw_params.get(key)
            if isinstance(value, str) and value:
                if _path_fits(value):
                    out.append(value)
                else:
                    out.truncated = True
        commands = _command_texts(raw_params.get("command"))
        out.truncated |= commands.truncated
        for command in commands:
            dirs = _command_dirs(command)
            out.extend(dirs)
            out.truncated |= dirs.truncated
    seen: set[str] = set()
    unique = TouchCandidates(truncated=out.truncated)
    for path in out:
        if path not in seen:
            if len(unique) >= _MAX_CANDIDATES_PER_CALL:
                unique.truncated = True
                break
            seen.add(path)
            unique.append(path)
    return unique


def _ceiling_dirs() -> set[str]:
    """``GIT_CEILING_DIRECTORIES`` as git reads it, so the walk stops where git's does."""
    raw = os.environ.get("GIT_CEILING_DIRECTORIES", "")
    return {os.path.normpath(p) for p in raw.split(os.pathsep) if p}


def find_git_root(path: str) -> str | None:
    """The nearest ancestor of *path* (inclusive) holding a ``.git`` entry.

    The one repository-discovery walk the dashboard does in-process: the Git
    tab's touched-repository list and the project chip's branch probe
    (``file_api.project_dirs._project_git_branch``) both go through it, so a
    project reads as a repository on both or on neither. ``.git`` is probed for
    existence, not ``isdir``: a linked worktree's ``.git`` is a file. A path that
    does not exist yet (a file the call is creating, in a directory it is
    creating) still finds its repository through its parents. Like git, the walk
    never enters a ``GIT_CEILING_DIRECTORIES`` entry above the starting
    directory -- the same rule the status and log routes inherit from ``git
    rev-parse --git-dir``, which owns discovery there. Exhausting the ancestor
    budget raises ``RootWalkLimitReached`` rather than claiming absence; the
    branch chip degrades to no label and repository tracking records the loss.
    Pure path arithmetic plus ``exists`` probes: no realpath, no home or
    sensitivity judgement; callers apply those where they mean something.
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
    raise RootWalkLimitReached


def resolve_repo_roots(candidates: Iterable[str], anchor: str = "") -> TouchCandidates:
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
    roots = TouchCandidates(truncated=getattr(candidates, "truncated", False))
    for raw in candidates:
        if not _path_fits(raw):
            roots.truncated |= bool(raw)
            continue
        if isinstance(raw, _DiffPath) and not edit_target_candidates({}, raw):
            continue
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            if not anchor:
                continue
            path = os.path.join(os.path.expanduser(anchor), path)
        if not _path_fits(path):
            roots.truncated = True
            continue
        try:
            found = find_git_root(os.path.normpath(path))
        except RootWalkLimitReached:
            roots.truncated = True
            continue
        if found is None:
            continue
        root = os.path.realpath(found)
        is_fs_root = os.path.dirname(root) == root
        if root == home or is_fs_root:
            continue
        if root in roots:
            # Candidates arrive oldest to newest, so a repeated root takes its
            # latest position: A, B, A lists B before A, and A is the newest.
            roots.remove(root)
            roots.append(root)
            continue
        if is_sensitive_path(root):
            continue
        if not _path_fits(root):
            roots.truncated = True
            continue
        roots.append(root)
    return roots


def record_repo_roots(slot: _ChatSlot, roots: Iterable[str], *, newest: bool = True) -> bool:
    """Merge *roots* into the slot's list. Event loop only; returns whether it changed.

    The list and *roots* are both ordered oldest to newest touch. ``newest=True``
    (a live tool call) moves each root to the newest end; ``newest=False`` (a
    transcript seed, which is older than anything noticed live) adds only
    unknown roots, keeping their order, ahead of the held ones, so a seed never
    reorders live activity and the cap drops the oldest seeded root first.

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
    before = (list(held), list(evicted), overflow, slot._git_repos_incomplete)
    slot._git_repos_incomplete |= getattr(roots, "truncated", False)
    batches: list[list[str]] = []
    seeded: list[str] = []
    for root in roots:
        if not _path_fits(root):
            slot._git_repos_incomplete |= bool(root)
        elif newest:
            batches.append([root])
        elif root not in held and root not in seeded:
            seeded.append(root)
    if seeded:
        batches.append(seeded)
    for batch in batches:
        if newest:
            if batch[0] in held:
                held.remove(batch[0])
            held.extend(batch)
        else:
            held[:0] = batch
        members = set(batch)
        evicted = [root for root in evicted if root not in members]
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
    return (held, evicted, overflow, slot._git_repos_incomplete) != before


def omitted_repo_count(slot: _ChatSlot) -> int:
    """Repositories this slot worked in that its capped list leaves out."""
    evicted = getattr(slot, "_git_repos_evicted", None) or []
    return len(evicted) + getattr(slot, "_git_repos_evicted_overflow", 0)


def transcript_change_paths(messages: Iterable[Mapping[str, Any]]) -> TouchCandidates:
    """File paths the transcript's file-change rows name, newest first. Pure.

    Seeds the list for a slot restored after a gateway restart, when the live
    tool calls that found its repositories are gone. Only edits survive in the
    transcript this way; a repository the agent only ran commands in reappears
    the next time it works there. The runner attaches file changes only to
    assistant rows; caller-supplied metadata on other roles is not agent work.
    """
    paths = TouchCandidates()
    seen: set[str] = set()
    for message in reversed(list(messages)):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        meta = message.get("meta")
        changes = meta.get("file_changes") if isinstance(meta, Mapping) else None
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            # A failed or refused write whose outcome is unprovable keeps only
            # its path (file_changes._flush_file_changes). Live capture records
            # nothing for such a call, so the seed skips it too.
            if change.get("omitted_reason") == "unverified_write":
                continue
            path = change.get("path")
            if isinstance(path, str) and path:
                if not _path_fits(path):
                    paths.truncated = True
                    continue
                if path not in seen:
                    if len(paths) >= _MAX_SEED_PATHS:
                        paths.truncated = True
                        return paths
                    seen.add(path)
                    paths.append(path)
    return paths


def note_tool_call(
    slot: _ChatSlot,
    raw_params: Mapping | None,
    *,
    tool_call_id: str,
    tool_kind: str = "",
    diff_path: str = "",
    is_shell: bool = False,
    background_tasks: set[asyncio.Task[Any]] | None = None,
) -> None:
    """Snapshot a proposal/refinement without admitting a repository. Loop only.

    A call's candidates are raw bounded strings until its successful terminal
    result reaches :func:`finish_tool_call`. Refinements may supply the first
    path (Claude streams empty input first), or more paths; their union spends
    the same per-call budget. Updating a call keeps its original insertion order.
    Missing/oversized ids retain nothing. Turn exit drops unfinished entries.
    """
    try:
        if not isinstance(tool_call_id, str) or not _path_fits(tool_call_id):
            return
        candidates = touch_candidates(
            raw_params, tool_kind=tool_kind, diff_path=diff_path, is_shell=is_shell
        )
        if not candidates and not candidates.truncated:
            return
        # ACP emits result before refinement when one wire update carries both.
        # A bounded terminal receipt admits its first paths only after execution,
        # and prevents a refused result's trailing refinement from re-staging it.
        terminal = slot._git_repo_touch_terminal.get(tool_call_id)
        if terminal is not None:
            if terminal == "completed":
                _queue_walk(slot, candidates, background_tasks=background_tasks)
            return
        pending = slot._git_repo_touch_pending
        previous = pending.get(tool_call_id)
        merged = TouchCandidates(
            previous or (), truncated=candidates.truncated or getattr(previous, "truncated", False)
        )
        for path in candidates:
            if path in merged:
                index = merged.index(path)
                if not isinstance(path, _DiffPath):
                    merged[index] = path
            elif len(merged) < _MAX_CANDIDATES_PER_CALL:
                merged.append(path)
            else:
                merged.truncated = True
        pending[tool_call_id] = merged
        if len(pending) > MAX_PENDING_TOOL_CALLS:
            del pending[next(iter(pending))]
            # Forgetting ids must not require an unbounded tombstone set. A
            # successful result missing its proposal is conservatively cut for
            # this turn; refusals and turn cleanup never publish this loss bit.
            slot._git_repo_touch_overflow = True
    except Exception:  # noqa: BLE001 - display bookkeeping must never fail a turn
        logger.debug("could not stage git repositories for a tool call", exc_info=True)


def finish_tool_call(
    slot: _ChatSlot,
    tool_call_id: str,
    *,
    executed: bool,
    background_tasks: set[asyncio.Task[Any]] | None = None,
) -> None:
    """Release one terminal call; only confirmed execution queues its paths.

    The runner uses the same successful completion signal as transcript file
    changes, never a proposal or an approval that might not reach execution.
    Failed/refused/cancelled calls pop their candidates without starting a walk.
    All work here is bounded string/container work; filesystem resolution stays
    on the single walker task, whose queue preserves completion order.
    """
    try:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return
        if not _path_fits(tool_call_id):
            slot._git_repos_incomplete |= executed
            return
        candidates = slot._git_repo_touch_pending.pop(tool_call_id, None)
        terminal = slot._git_repo_touch_terminal
        terminal[tool_call_id] = "completed" if executed else "refused"
        if len(terminal) > MAX_PENDING_TOOL_CALLS:
            # Dropping the oldest receipt loses no path: the refinement a
            # receipt waits for is read from the same wire update as its result
            # (``acp/client_turn.py`` yields the refinement right after it), so
            # it has arrived long before 64 later results are recorded. Most
            # receipts are for calls that name no path at all, so counting this
            # drop as a loss would flag every long turn as incomplete.
            del terminal[next(iter(terminal))]
        if not executed:
            return
        if candidates is None:
            slot._git_repos_incomplete |= slot._git_repo_touch_overflow
            return
        _queue_walk(slot, candidates, background_tasks=background_tasks)
    except Exception:  # noqa: BLE001 - display bookkeeping must never fail a turn
        logger.debug("could not finish git repositories for a tool call", exc_info=True)


def _queue_walk(
    slot: _ChatSlot,
    candidates: list[str],
    *,
    background_tasks: set[asyncio.Task[Any]] | None = None,
) -> None:
    """Queue the repositories one tool call worked in. Never raises, never waits.

    Event loop only. The string work happens here; the filesystem walk does
    not: the call's candidates join the slot's pending list and the slot's one
    walker task (:func:`_drain_walks`) is started when none is running, so the
    runner moves on to its next event as soon as this returns. One walker per
    slot, draining in call order, keeps :func:`record_repo_roots`'s
    most-recent-last order the order the agent worked in: two concurrent walks
    could land out of order and list an older touch as the newest.

    The pending list is bounded (``_MAX_PENDING_CANDIDATES_PER_SLOT``), oldest
    dropped first, so a hung mount cannot grow it without limit: a candidate
    queued twice moves to the newest end instead of being counted twice, and
    one walk resolves everything queued while the previous one ran.

    *background_tasks* is the owner's task set (``DashboardState
    ._background_tasks``): the loop holds tasks weakly, so the walker is held
    there the way every other fire-and-forget task on the dashboard is. The
    slot's own handle (``_git_repo_walk_task``) is the "is one running" check.
    A slot closed while a walk is pending just receives its roots on an object
    nothing reads any more; a loop shut down while one is pending cancels the
    task, and the thread's late result is dropped on the closed loop by
    ``asyncio`` itself. Neither raises or warns.
    """
    try:
        slot._git_repos_incomplete |= getattr(candidates, "truncated", False)
        if not candidates:
            return
        pending: list[str] = list(getattr(slot, "_git_repo_walk_pending", None) or [])
        for path in candidates:
            if path in pending:
                previous = pending[pending.index(path)]
                pending.remove(path)
                # A params spelling remains admissible even when a diff-only
                # spelling of that same path cannot pass its anchoring check.
                if isinstance(path, _DiffPath) and not isinstance(previous, _DiffPath):
                    path = previous
            pending.append(path)
        if len(pending) > _MAX_PENDING_CANDIDATES_PER_SLOT:
            slot._git_repos_incomplete = True
            pending = pending[-_MAX_PENDING_CANDIDATES_PER_SLOT:]
        slot._git_repo_walk_pending = pending
        task = getattr(slot, "_git_repo_walk_task", None)
        if task is not None and not task.done():
            return  # the running walker loops until the pending list is empty
        task = asyncio.create_task(_drain_walks(slot))
        slot._git_repo_walk_task = task
        if isinstance(background_tasks, set):
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
    except Exception:  # noqa: BLE001 - display bookkeeping must never fail a turn
        logger.debug("could not note git repositories for a tool call", exc_info=True)


async def _drain_walks(slot: _ChatSlot) -> None:
    """One slot's walker: resolve what is pending, record it, repeat until empty.

    Takes the whole pending list in one go (one thread hop for a burst of
    calls), clears it before the walk so calls arriving meanwhile queue a
    further round, and exits only when a round finds nothing queued. A walk
    that raises costs that round its roots and nothing else. ``Task.done()``
    is true the moment this returns, with no loop tick in between, so
    :func:`_queue_walk` cannot observe a finished walker as still running.
    """
    while True:
        pending: list[str] = list(getattr(slot, "_git_repo_walk_pending", None) or [])
        if not pending:
            return
        slot._git_repo_walk_pending = []
        try:
            roots = await asyncio.to_thread(
                resolve_repo_roots, pending, getattr(slot, "project", "") or ""
            )
            record_repo_roots(slot, roots)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - display bookkeeping must never fail a turn
            logger.debug("could not note git repositories for a tool call", exc_info=True)
