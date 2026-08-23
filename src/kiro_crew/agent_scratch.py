"""Per-process scratch containment for spawned agent (kiro-cli) processes.

Agent sessions have no designated scratch location, so their working residue
-- repository clones, pytest basetemps, probe scripts, screenshots -- lands
in the shared system temp dir, where nothing ties it to the session that made
it and the OS tmp reaper deletes by AGE, killing long-lived in-flight work
while leaving everything younger to accumulate (issue #5063).

Each spawned agent process gets ``<data home>/scratch/<label>-<token8>/`` and
the ``TMPDIR``/``TMP``/``TEMP`` triple plus ``KIROCREW_SCRATCH`` pointing at
it, so ``tempfile`` users, pytest basetemps, shell ``mktemp``, and
prompt-guided work products all land somewhere OWNED. On real disk (the data
home), deliberately not tmpfs: agent residue can be large and must not
occupy RAM.

Reclamation is keyed on PROCESS liveness, never on file age:

* Allocation writes a PROVISIONAL owner pid atomically with the directory
  (and fails the allocation otherwise); the spawner replaces it with the
  child's pid right after spawn. There is deliberately NO per-teardown
  sweep -- agent processes die many ways (clean shutdown, kill escalation,
  crash, gateway restart with survivors), and the positive liveness signal
  below covers every death path by construction.
* The gateway sweeps hourly (first pass an hour after start -- never on the
  boot path). A directory is removed only when its recorded owner's process
  GROUP is dead AND the whole tree has been idle past the grace window; a
  directory with a live owner is never touched (agent processes can outlive
  a gateway restart), an ownerless directory is never deleted, and a garbled
  owner file is left for a human.

Sweep hygiene follows the house rules of :mod:`kiro_crew.agents_janitor`:
``os.lstat`` classification, symlinks never followed, deletion only for
direct children of the managed root, per-entry fail-open.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import stat
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_SUBDIR = "scratch"

#: Owner-pid marker written by the spawner right after the child starts.
OWNER_FILENAME = ".owner"

#: A directory younger than this with no ``.owner`` yet is mid-spawn, not an
#: orphan: allocation happens before the child pid exists. Anything older
#: with no owner belongs to a spawn that never completed.
_UNOWNED_GRACE_SECONDS = 3600.0

_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def scratch_root() -> Path:
    """The managed root: ``<data home>/scratch``."""
    return config_dir() / _SUBDIR


def allocate_scratch(label: str) -> Path:
    """Create and return a fresh scratch dir for ONE agent process.

    *label* is a human attribution hint (a session key or ``runtime``); it is
    sanitized to a filename-safe token and truncated -- the random suffix is
    what makes the directory unique and its sweep single-owner.

    A PROVISIONAL owner (the spawning process) is recorded inside the same
    allocation step: deletion is permitted only for owned-and-dead-and-idle
    directories, so a dir that cannot get an owner must not exist at all --
    if the owner write fails (ENOSPC, inode exhaustion), the dir is removed
    and the failure propagates, degrading the spawn to inherited temp.
    :func:`record_owner` later replaces the provisional pid with the child's.
    """
    root = scratch_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe = _LABEL_SAFE.sub("-", label)[:40].strip("-") or "agent"
    path = root / f"{safe}-{secrets.token_hex(4)}"
    path.mkdir(mode=0o700)
    try:
        (path / OWNER_FILENAME).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def record_owner(path: Path, pid: int) -> None:
    """Record the spawned child's pid so the boot sweep can check liveness."""
    try:
        (path / OWNER_FILENAME).write_text(str(pid), encoding="utf-8")
    except OSError:
        # Fail-open: an unowned dir is reclaimed by the grace-window rule.
        logger.debug("agent-scratch: could not record owner for %r", path.name, exc_info=True)


def scratch_env(path: Path) -> dict[str, str]:
    """Env exports pointing a child's temp AND scratch at *path*.

    ``TMPDIR``/``TMP``/``TEMP`` cover ``tempfile`` and shell ``mktemp`` on
    both platforms; ``KIROCREW_SCRATCH`` is the prompt-visible name for
    deliberate work products (clones, logs, screenshots).
    """
    value = str(path)
    return {"TMPDIR": value, "TMP": value, "TEMP": value, "KIROCREW_SCRATCH": value}


def _is_plain_dir(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def _pgroup_alive(pid: int) -> bool:
    """Whether any member of *pid*'s PROCESS GROUP is still alive.

    The recorded owner is the launcher pid, and both spawn chokepoints use
    ``start_new_session=True`` -- so the launcher's pid IS the group id and
    ordinary descendants keep it even after the launcher exits. Probing the
    GROUP (:func:`platform_compat.pgroup_exists`) is therefore the
    tree-faithful liveness signal: a live child holding an open file
    descriptor produces no directory-mtime evidence at all, while the group
    probe still sees it. A descendant that setsid()s OUT of the group evades
    this probe -- and equally evades ``kill_process_tree`` -- so it is owned
    by the escaped-children reaper, not by this sweep; the sweep's liveness
    boundary deliberately matches the kill path's tree boundary.
    """
    if pid <= 0:
        return False
    return platform_compat.pgroup_exists(pid)


#: Entry cap for the tree-idle walk: a tree too big to scan cheaply reads as
#: ACTIVE (kept) -- the sweep must stay cheap and err toward keeping.
_TREE_IDLE_SCAN_CAP = 10_000


def _tree_newest_mtime(root: Path, fallback: float) -> float:
    """The newest mtime anywhere in *root*'s tree (lstat, symlinks never followed).

    A live process writing through an already-open file descriptor never
    touches the top DIRECTORY's mtime, but every write refreshes the FILE's
    own mtime -- so tree-newest is the faithful idle signal on every
    platform, and the only one available on Windows (no process groups).
    Fail-safe: an unreadable entry or a tree past the scan cap returns
    *fallback* (reads as active, the sweep keeps the dir).
    """
    newest = 0.0
    seen = 0
    try:
        newest = os.lstat(root).st_mtime
        stack = [root]
        while stack:
            current = stack.pop()
            for entry in os.scandir(current):
                seen += 1
                if seen > _TREE_IDLE_SCAN_CAP:
                    return fallback
                info = os.lstat(entry.path)
                if info.st_mtime > newest:
                    newest = info.st_mtime
                if stat.S_ISDIR(info.st_mode):  # lstat: symlinks never descend
                    stack.append(Path(entry.path))
    except OSError:
        return fallback
    return newest


def sweep_dead_scratch(now: float | None = None) -> int:
    """Periodic sweep: remove directories whose owner is DEAD and content IDLE.

    Deletion needs BOTH signals, because each alone is an unfaithful proxy
    for "no process is using this":

    * A dir whose recorded owner pid is alive is never touched -- agent
      processes can outlive a gateway restart, so a fresh gateway must not
      clear wholesale.
    * A dead owner with a FRESH mtime reads as still-in-use and is kept:
      the recorded owner is the launcher pid, and descendants can outlive
      it while still writing (``tempfile`` creates entries directly under
      ``$TMPDIR``, keeping the dir mtime fresh).
    * A dir with NO owner record is NEVER deleted. Allocation writes a
      provisional owner atomically-with-creation and fails otherwise, so an
      ownerless dir indicates a state this code did not produce -- deleting
      on absence of evidence is how live work gets lost.
    * A garbled owner file is left for a human.

    Returns the number of directories removed.
    """
    root = scratch_root()
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return 0
    except OSError:
        logger.debug("agent-scratch: could not list %s; skipping sweep", root, exc_info=True)
        return 0
    reference = time.time() if now is None else now
    removed = 0
    for entry in entries:
        child = root / entry.name  # name-composed: cannot escape the root
        if not _is_plain_dir(child):
            continue  # symlinks and stray files are never swept
        try:
            idle = reference - _tree_newest_mtime(child, fallback=reference)
        except OSError:
            continue
        if idle < _UNOWNED_GRACE_SECONDS:
            continue  # recently active anywhere in the tree, whoever owns it
        try:
            pid = int((child / OWNER_FILENAME).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            # Unowned or garbled: never delete on absence of evidence.
            continue
        if _pgroup_alive(pid):
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("agent-scratch: sweep removed %d dead idle scratch dir(s)", removed)
    return removed
