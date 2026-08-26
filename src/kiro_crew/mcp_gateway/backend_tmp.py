"""Per-process temp-dir containment for gateway-spawned MCP servers.

Third-party MCP servers write telemetry, caches, and scratch files into
whatever temp directory their process sees. Spawned with an inherited
default, that is the shared system temp dir -- which nothing ever cleans, so
their output accumulates for as long as the host lives (issue #5064). Setting
``TMPDIR``/``TMP``/``TEMP`` at the spawn chokepoint contains every
well-behaved server without touching any server's code. A server that
hardcodes ``/tmp`` ignores the variables and keeps today's behavior: this is
containment, not a sandbox guarantee.

Layout: ``<data home>/run/mcp-tmp/<digest12>-<token8>/`` -- one directory per
spawned PROCESS, not per PoolKey. Two live processes can share a PoolKey (a
connection-private backend spawned beside the shared one), so a sweep keyed
on the digest alone would delete the temp dir out from under the survivor.
The digest prefix keeps the directory attributable to its server config; the
fresh token makes the shutdown sweep single-owner by construction.

PoolKey invariant (same reasoning as the ``KIROCREW_SPAWNED_ENV`` marker in
:func:`kiro_crew.mcp_gateway.backend.spawn_backend`): the injected value is
derived from the key's own digest plus a token generated AFTER pool-identity
resolution, and is never folded into the PoolKey hash -- so it can neither
split nor collapse pooled-backend identity.

Sweep hygiene follows the house rules established by
:mod:`kiro_crew.agents_janitor`: ``os.lstat`` snapshots, symlinks are never
followed (a symlink where a directory is expected is skipped entirely),
deletion happens only for direct children of the managed root, and every
failure is tolerated per entry (fail-open -- hygiene must never take the
daemon down).
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import stat
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Subdirectory of ``<data home>/run`` that holds every per-process temp dir.
_SUBDIR = "mcp-tmp"


def backend_tmp_root() -> Path:
    """The managed root: ``<data home>/run/mcp-tmp``."""
    return config_dir() / "run" / _SUBDIR


def allocate_backend_tmp(pool_digest: str) -> Path:
    """Create and return a fresh private temp dir for ONE backend process.

    ``0o700`` like the rest of ``<data home>/run``. A PROVISIONAL owner (the
    spawning process) is recorded inside the same allocation step: deletion is
    permitted only for owned-and-dead directories, so a dir that cannot get an
    owner must not exist at all -- if the owner write fails (ENOSPC, inode
    exhaustion), the dir is removed and the failure propagates, degrading the
    spawn to inherited temp instead of leaving an unreclaimable orphan.
    :func:`record_owner` later replaces the provisional pid with the child's.
    """
    root = backend_tmp_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # ``mode=0o700`` is a POSIX-bits no-op on Windows: under a permissive
    # custom data home the dir would inherit a readable DACL, exposing any
    # token-bearing temp file a backend writes. The shim applies an
    # owner-only DACL with (OI)(CI) inheritance there (0o700 on POSIX), so
    # children created inside are covered as well.
    platform_compat.restrict_dir_to_owner(root)
    name = f"{pool_digest[:12]}-{secrets.token_hex(4)}"
    path = root / name
    path.mkdir(mode=0o700)
    platform_compat.restrict_dir_to_owner(path)
    try:
        (path / OWNER_FILENAME).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def allocate_probe_tmp() -> Path:
    """Create and return a fresh private temp dir for ONE probe spawn.

    Same allocation contract as :func:`allocate_backend_tmp` (provisional
    owner written atomically, raise-and-remove on failure). The probe cleans
    its own dir in its ``finally`` -- unlike a backend, a probe knows exactly
    when its lifecycle ends -- and a cleanup that never ran (crash) leaves an
    owner-dead dir the daemon sweep reclaims once idle.
    """
    return allocate_backend_tmp("probe")


def tmp_env(path: Path) -> dict[str, str]:
    """The ``TMPDIR``/``TMP``/``TEMP`` triple pointing a child at *path*.

    All three deliberately: ``tempfile`` honors ``TMPDIR`` on POSIX but
    ``TMP``/``TEMP`` on Windows, and shell ``mktemp`` reads ``TMPDIR`` --
    setting the triple covers every well-behaved consumer on both platforms.
    """
    value = str(path)
    return {"TMPDIR": value, "TMP": value, "TEMP": value}


def _is_plain_dir(path: Path) -> bool:
    """A real directory -- not a symlink, not a file, not absent.

    ``os.lstat``, never ``stat``: a symlink planted where a directory is
    expected must classify as a symlink so the sweep skips it instead of
    following it out of the managed root.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


#: Owner-pid marker written right after the backend process spawns.
OWNER_FILENAME = ".owner"

#: A token dir younger than this with no ``.owner`` yet is mid-spawn, not an
#: orphan.
_UNOWNED_GRACE_SECONDS = 3600.0


def record_owner(path: Path, pid: int) -> None:
    """Record the spawned backend's pid so the boot sweep can check liveness."""
    try:
        (path / OWNER_FILENAME).write_text(str(pid), encoding="utf-8")
    except OSError:
        # Fail-open: an unowned dir falls under the grace-window rule instead.
        logger.debug("backend-tmp: could not record owner for %r", path.name, exc_info=True)


def _pgroup_alive(pid: int) -> bool:
    """Whether any member of *pid*'s PROCESS GROUP is still alive.

    The recorded owner is the launcher pid, and ``spawn_backend`` uses
    ``start_new_session=True`` -- so the launcher's pid IS the group id and
    ordinary descendants keep it even after the launcher exits. Probing the
    GROUP (:func:`platform_compat.pgroup_exists`) is therefore the
    tree-faithful liveness signal: a live child holding an open file
    descriptor produces no directory-mtime evidence at all, while the group
    probe still sees it. A descendant that setsid()s OUT of the group evades
    this probe -- and equally evades ``kill_process_tree`` -- so it is owned
    by the spawn-marker orphan reaper, not by this sweep; the sweep's
    liveness boundary deliberately matches the kill path's tree boundary.
    """
    if pid <= 0:
        return False
    return platform_compat.pgroup_exists(pid)


def sweep_backend_tmp(path: Path) -> None:
    """Remove ONE backend's temp dir after its process is gone (fail-open).

    Containment guard: only a direct child of the managed root is ever
    removed -- a path from anywhere else (bug, stale attribute) is refused
    and logged rather than deleted.
    """
    if path.parent != backend_tmp_root():
        logger.warning(
            "backend-tmp: refusing to sweep a path outside the managed root: %r",
            path.name,
        )
        return
    if not _is_plain_dir(path):
        # Already gone, or replaced by something that is not a plain
        # directory (e.g. a planted symlink) -- never followed, never removed.
        return
    shutil.rmtree(path, ignore_errors=True)


def _tree_newest_mtime(root: Path, fallback: float) -> float:
    """The newest mtime anywhere in *root*'s tree (lstat, symlinks never followed).

    A live process writing through an already-open file descriptor never
    touches the top DIRECTORY's mtime, but every write refreshes the FILE's
    own mtime -- so tree-newest is the faithful idle signal on every
    platform, and the only one available on Windows (no process groups).
    Fail-safe: an unreadable entry returns *fallback* (reads as active, the
    sweep keeps the dir).

    Deliberately UNCAPPED: an entry cap that bails to *fallback* turns every
    tree larger than the cap into a permanently active-looking one, so a dead
    backend that wrote enough entries could never be reclaimed and repeated
    runs would exhaust storage -- the exact defect this module exists to fix.
    The walk runs off the event loop (``asyncio.to_thread`` / daemon thread)
    on an hourly cadence, and its size is bounded by what one backend wrote
    into its OWN temp dir, so a full metadata walk is the right trade.
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
                if stat.S_ISDIR(info.st_mode):  # lstat: symlinks never descend
                    stack.append(Path(entry.path))
    except OSError:
        return fallback
    return newest


def sweep_all_backend_tmp() -> int:
    """Sweep (boot + periodic): remove dirs whose owner is DEAD and whose
    content is IDLE.

    Deletion needs BOTH signals, because each alone is an unfaithful proxy
    for "no process is using this":

    * The recorded owner is the LAUNCHER pid, and a wrapper launcher (npx ->
      node) can exit while its server child lives on and keeps writing temp
      files -- ``tempfile`` creates entries directly under ``$TMPDIR``, so a
      live user keeps the dir's mtime fresh. Dead owner + fresh mtime reads
      as "still in use": kept.
    * A dir with NO owner record is NEVER deleted here. Allocation writes a
      provisional owner atomically-with-creation and fails the allocation
      otherwise, so an ownerless dir indicates a state this code did not
      produce -- deleting on absence-of-evidence is how live data gets lost.
    * A garbled owner file is left for a human.

    Probe dirs are allocated with the same contract and normally removed by
    the probe's own ``finally``; one whose cleanup never ran is reclaimed by
    the generic owner-dead + idle path above.

    Returns the number of directories removed.
    """
    root = backend_tmp_root()
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return 0
    except OSError:
        logger.debug("backend-tmp: could not list %s; skipping sweep", root, exc_info=True)
        return 0
    reference = time.time()
    removed = 0
    for entry in entries:
        # Name-composed child path: nothing here can traverse outside root.
        child = root / entry.name
        if not _is_plain_dir(child):
            # Symlinks and stray files are never swept.
            continue
        try:
            idle = reference - _tree_newest_mtime(child, fallback=reference)
        except OSError:
            continue
        if idle < _UNOWNED_GRACE_SECONDS:
            continue  # recently active anywhere in the tree, whoever owns it
        owner_file = child / OWNER_FILENAME
        try:
            pid = int(owner_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            # Unowned or garbled: never delete on absence of evidence.
            continue
        if _pgroup_alive(pid):
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("backend-tmp: sweep removed %d dead idle dir(s)", removed)
    return removed
