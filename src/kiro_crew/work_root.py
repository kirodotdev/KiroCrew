"""Durable work root for state that must OUTLIVE the process that created it.

Per-process scratch (:mod:`kiro_crew.agent_scratch`) is reclaimed once its owner's
process group is dead. A job whose directory is created by one run and advanced by
a later, different run needs a directory that survives its creator; without one,
authors fall back to the system temp dir. Where that is a tmpfs, its pages are
charged to the agent cgroup as unreclaimable ``shmem``, so the slice sits at
``memory.high`` and throttles every deadline in it (ACP handshakes, script crons,
credential checks) while ``oom_kill`` stays 0.

Layout: ``<data home>/work/<key>/``, on the same real disk as the scratch root,
never tmpfs, because this residue can be large.

* The *key* is caller-supplied and STABLE, so a later run finds the directory by
  computing it again. Keys are validated, not sanitized (:data:`_KEY_RE`): rewriting
  could map two keys onto one directory. A per-run key (a fingerprint, a timestamp)
  is never rejoined and leaves a lock file behind that is never reclaimed.
* No owner pid and no liveness gate: outliving the creator is the contract.
* Reclamation is ONE idle window that no caller can shorten. Two runs may share a
  key, and without an owner identity nothing can tell which run speaks for it.
* A rejoin and a sweep of the same key are serialized by a per-key lock, and the
  rejoin refreshes the tree's mtime before returning it; otherwise a rejoined tree
  idle for a week could be ``rmtree``-d by the next hourly sweep under a live
  consumer. The sweep takes that lock without waiting, so hygiene never blocks work.

The root is NOT reachable from a sandboxed process. ``sandbox`` masks it, and on
Linux the mask is a writable empty tmpfs bind, so ``allocate_work`` called inside
the sandbox succeeds but returns a directory whose bytes live in RAM and vanish with
the namespace. Only unsandboxed gateway code may allocate here. A sandboxed consumer
gets its one key directory from the code that spawns it, as ``extra_visible_dirs``
(as ``apps/backend.py`` does for Notes). No environment variable names this root,
because a sandboxed agent's namespace would make that path a lie.

Sweep hygiene follows :mod:`kiro_crew.agents_janitor`, with local helpers as in
:mod:`kiro_crew.mcp_gateway.backend_tmp`: ``os.lstat`` classification, symlinks
never followed, deletion only of direct children of the root, and per-entry
fail-open, because hygiene must never take the gateway down.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.loader import config_dir
from kiro_crew.owner_only_files import mkdirs_owner_only

logger = logging.getLogger(__name__)

#: Leaf of the data home holding every cross-process work directory. Three fences
#: name it, each pinned to this name by its own test: ``sandbox._CREW_HIDDEN_LEAVES``
#: masks it from subprocesses, ``sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES`` creates
#: it so that mask is not vacuous before the first allocation, and
#: ``security.paths._CREW_SECRET_LEAVES`` refuses it to the agent's file tools.
WORK_DIRNAME = "work"

#: Suffix of the per-entry lock file that serializes a rejoin against the sweep.
#: It sits BESIDE the entry so it survives the entry's ``rmtree``; the leading dot
#: cannot collide with a key, and the sweep skips it as a non-directory. It is
#: never reclaimed: deleting a lock another process holds is the race it prevents.
#: Because it outlives every entry it served, it is no evidence of allocation;
#: that lives inside the entry (:data:`_MARKER_NAME`).
_LOCK_SUFFIX = ".lock"

#: Allocation marker: an empty file :func:`allocate_work` writes INSIDE each entry
#: it creates, and :func:`sweep_work_root` requires before deleting. Inside, so
#: ``rmtree`` takes it with the tree and it cannot authorize deleting a directory
#: later restored at that name. It is never written onto a directory already at the
#: key (that is refused), so a directory this module did not create is never swept.
_MARKER_NAME = ".kirocrew-work"

#: Idle seconds before an entry is reclaimed (seven days, the escalation-hold
#: ceiling). Every write refreshes the tree's newest mtime, so a tree a job still
#: advances never reaches it. A consumer whose gap between runs exceeds this loses
#: its tree every cycle and needs another home: ``grace`` on the sweep is not a
#: per-entry escape, since the hourly maintenance sweep passes no argument.
IDLE_GRACE_SECONDS = 7 * 24 * 3600.0

#: Keys come from code, so this validates instead of rewriting. A leading
#: alphanumeric rules out ``.`` and ``..``, and no separator is admitted, so
#: ``work_root() / key`` cannot leave the root. The keyspace is flat and global:
#: compose a key as ``<caller>-<id>``, never a bare number, so an issue and a pull
#: request numbered alike do not share a directory.
_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class WorkRootBoundaryError(Exception):
    """A managed path cannot be used, and nothing was created or written through it.

    Raised when a managed component is a LINK (honouring it would write and later
    delete outside the root), or when the key holds a directory this module did not
    allocate (adopting it would let the sweep delete data nothing here created).
    """


def work_root() -> Path:
    """The managed root: ``<data home>/work``."""
    return config_dir() / WORK_DIRNAME


def _validated_key(key: str) -> str:
    """Return *key* unchanged, or raise :class:`ValueError`.

    Not a sanitizer: mapping rejected spellings onto accepted ones could put two
    callers' keys in one directory.
    """
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError("work-root key must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    return key


def _refuse_linked(path: Path, what: str) -> None:
    """Refuse *path* when it is a symlink or a Windows directory junction.

    Uses :func:`platform_compat.is_link_or_junction`, not ``os.path.islink``,
    which reports False for a junction on Windows, the platform without
    ``O_NOFOLLOW``.

    *what* is the path shape for the log, never the link target, which is the
    planter's input.
    """
    if not platform_compat.is_link_or_junction(path):
        return
    logger.warning("work-root: refusing to write through a linked %s", what)
    raise WorkRootBoundaryError(f"work root {what} is a link")


def _is_plain_dir(path: Path) -> bool:
    """A real directory: not a symlink, not a Windows junction, not a file, not absent.

    ``lstat`` reports a junction as a directory, so a name-surrogate reparse
    tag is checked too (:func:`platform_compat.lstat_is_name_surrogate`).
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not platform_compat.lstat_is_name_surrogate(info)


def _tree_newest_mtime(root: Path, fallback: float) -> float:
    """The newest mtime anywhere in *root*'s tree (lstat, symlinks never followed).

    A write through an open descriptor refreshes the file's mtime but not the top
    directory's, so tree-newest is the only faithful idle signal. An unreadable
    entry returns *fallback*, which reads as active and keeps the directory.

    Uncapped, like the scratch sweep: a cap that bailed to *fallback* would make
    every large abandoned tree look active forever. It runs hourly, off the loop.

    Windows junctions are not descended (a dangling one makes ``scandir`` raise,
    which would read the tree as active for good); their own mtime still counts.
    """
    newest = 0.0
    try:
        newest = os.lstat(root).st_mtime
        stack = [root]
        while stack:
            current = stack.pop()
            for entry in os.scandir(current):
                info = os.lstat(entry.path)
                if info.st_mtime > newest:
                    newest = info.st_mtime
                # lstat: symlinks never descend, nor do Windows junctions.
                if stat.S_ISDIR(info.st_mode) and not platform_compat.lstat_is_name_surrogate(info):
                    stack.append(Path(entry.path))
    except OSError:
        return fallback
    return newest


def _lock_path(root: Path, key: str) -> Path:
    """The lock file serializing *key*'s rejoin against the sweep."""
    return root / f".{key}{_LOCK_SUFFIX}"


def _marker_path(entry: Path) -> Path:
    """The allocation marker inside *entry*."""
    return entry / _MARKER_NAME


def _has_allocation_marker(entry: Path) -> bool:
    """Did :func:`allocate_work` create *entry*? Read with ``lstat``, never followed.

    A plain file only: a marker that is a LINK is not evidence, since the entry's
    writer chooses what it names.
    """
    try:
        info = os.lstat(_marker_path(entry))
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _write_allocation_marker(entry: Path) -> None:
    """Record that this module allocated *entry*, inside *entry* itself.

    ``O_NOFOLLOW`` where the platform has it, so a link planted at this name is
    refused rather than written through; ``O_CREAT`` without ``O_EXCL`` because a
    rejoin legitimately finds the marker its own earlier allocation left.
    """
    _refuse_linked(_marker_path(entry), f"allocation marker in {entry.name!r}")
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(_marker_path(entry), flags, 0o600)
    os.close(fd)


def _refresh(directory: Path) -> None:
    """Stamp *directory*'s own mtime to now, so a rejoined idle tree reads as in use.

    Without it the next sweep could delete a tree just handed to a caller.

    A failure is NOT swallowed: the caller learns the allocation failed instead of
    getting a path the next sweep deletes. A directory left behind already carries
    its marker, so the sweep can still reclaim it.
    """
    os.utime(directory)


def allocate_work(key: str) -> Path:
    """Create the work directory named by *key*, or REJOIN this module's own, and return it.

    Idempotent: a later process passing the same key gets the same directory. A
    rejoin requires this module's marker inside it; a stranger at the key is
    refused, not adopted.

    The rejoin runs under the key's lock and refreshes the tree before returning,
    so the sweep either keeps the refreshed tree or finishes first and the rejoin
    returns a fresh empty one. The lock helper's wait is bounded and fails closed.

    Raises :class:`ValueError` for a key this root cannot name, and
    :class:`WorkRootBoundaryError` when a managed component is a link or when the
    key is occupied by a directory this module did not allocate.
    """
    safe = _validated_key(key)
    root = work_root()
    # Checked before and after each mkdir: ``mkdir(..., exist_ok=True)`` succeeds
    # on a link to a directory, and a link can be planted between the checks.
    # Only managed components are judged; a data home reached through a link is
    # ordinary and must keep working.
    _refuse_linked(root, f"managed root {WORK_DIRNAME!r}")
    mkdirs_owner_only(root)
    _refuse_linked(root, f"managed root {WORK_DIRNAME!r}")
    path = root / safe
    lock_file = _lock_path(root, safe)
    _refuse_linked(lock_file, f"lock file for {safe!r}")
    with platform_compat.open_lock_file(lock_file) as fd:
        with platform_compat.file_lock(fd, exclusive=True):
            _refuse_linked(path, f"work dir {safe!r}")
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                # Only a tree this module allocated may be rejoined; marking a
                # stranger would let the sweep delete data nothing here created.
                # A genuine rejoin always finds its own marker, since only
                # ``rmtree`` removes it.
                _refuse_linked(path, f"work dir {safe!r}")
                if not _has_allocation_marker(path):
                    raise WorkRootBoundaryError(
                        f"work dir {safe!r} already exists without this module's "
                        "allocation marker, so it was not allocated here and is "
                        "not adopted"
                    )
            else:
                # If marking fails, an unmarked directory would wedge this key for
                # good, so remove it. ``os.rmdir`` removes only an empty directory,
                # and nothing has been handed out yet.
                try:
                    _write_allocation_marker(path)
                except BaseException:
                    try:
                        os.rmdir(path)
                    except OSError:
                        logger.warning("work-root: could not remove %r after a failed mark", safe)
                    raise
            _refresh(path)
    return path


def _reclaim_if_idle(child: Path, reference: float, grace: float) -> bool:
    """Judge *child* and remove it when idle past *grace*. Returns whether it went.

    Runs under the caller's per-entry lock, so no rejoin can land between the
    mtime read and the ``rmtree``.
    """
    try:
        idle = reference - _tree_newest_mtime(child, fallback=reference)
    except OSError:
        return False
    if idle < grace:
        return False
    if platform_compat.rmtree_force(child):
        return True
    # A partial rmtree may have removed the marker, and an unmarked entry is never
    # swept again: restore it so a later wake retries after another *grace*.
    logger.warning("work-root: could not fully remove %r; will retry", child.name)
    try:
        if os.path.lexists(child):
            _write_allocation_marker(child)
    except (OSError, WorkRootBoundaryError):
        logger.warning("work-root: could not restore the marker of %r", child.name)
    return False


def sweep_work_root(now: float | None = None, *, grace: float = IDLE_GRACE_SECONDS) -> int:
    """Periodic sweep: remove work directories idle past *grace*.

    No liveness signal, unlike the scratch sweep: outliving its creator is this
    root's contract. Idleness alone decides, with one window for every entry.

    *grace* applies to this whole sweep, not to one entry; the hourly maintenance
    sweep passes none. It serves targeted sweeps and tests.

    Each entry is judged under its own lock, taken WITHOUT waiting: a key a rejoin
    holds is in use and is judged again next wake, so hygiene never blocks work.

    Cases:

    * a linked managed root -- nothing is swept, since name-composed child paths
      stay inside the root only while the root is real;
    * a missing root -- nothing to do; an unlistable root -- reported, nothing swept;
    * a child that is not a plain directory (file, symlink, junction, lock file)
      -- skipped;
    * a child with no allocation marker, or whose marker is a LINK -- skipped, so an
      operator-placed or restored directory is never reclaimed;
    * a child whose lock file is a LINK -- skipped and reported;
    * a child whose lock a rejoin holds -- skipped this wake;
    * a child whose tree cannot be walked -- kept (reads as active);
    * a child idle less than *grace* -- kept; past *grace* -- removed;
    * a delete that fails -- reported and skipped, per entry.

    An entry held open without writes reads as idle and is removed past its window:
    the accepted cost of having no liveness gate.

    Must run off the event loop: the lock helper's poll-sleep would freeze every
    session on the loop thread.

    Returns the number of directories removed.
    """
    root = work_root()
    if platform_compat.is_link_or_junction(root):
        logger.warning("work-root: managed root %r is a link; skipping sweep", WORK_DIRNAME)
        return 0
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return 0
    except OSError:
        logger.debug("work-root: could not list %s; skipping sweep", root, exc_info=True)
        return 0
    reference = time.time() if now is None else now
    removed = 0
    for entry in entries:
        child = root / entry.name  # name-composed: cannot escape the root
        if not _is_plain_dir(child):
            continue  # symlinks, stray files and lock files are never swept
        lock_file = _lock_path(root, entry.name)
        if not _has_allocation_marker(child):
            # Not this module's to delete. Tested before the lock is opened,
            # because opening creates the lock file.
            continue
        if platform_compat.is_link_or_junction(lock_file):
            logger.warning("work-root: %r has a linked lock file; skipping it", entry.name)
            continue
        try:
            with platform_compat.open_lock_file(lock_file) as fd:
                with platform_compat.file_lock(fd, exclusive=True, wait=False):
                    if _reclaim_if_idle(child, reference, grace):
                        removed += 1
        except OSError:
            # ``BlockingIOError``: a rejoin holds this key. Any other error: the
            # lock could not be taken, so never delete unserialized.
            logger.debug("work-root: %r not reclaimable this wake", entry.name, exc_info=True)
            continue
    return removed
