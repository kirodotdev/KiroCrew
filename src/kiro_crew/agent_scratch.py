"""Per-process scratch containment for spawned agent (kiro-cli) processes.

Agent sessions have no designated scratch location, so their working residue
-- repository clones, pytest basetemps, probe scripts, screenshots -- lands
in the shared system temp dir, where nothing ties it to the session that made
it and the OS tmp reaper deletes by AGE, killing long-lived in-flight work
while leaving everything younger to accumulate.

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

The WRITE side is bound by the same rule, and has to be: a spawned agent OWNS
its scratch directory -- it is that process's own ``TMPDIR`` (:func:`scratch_env`)
-- while the gateway that records the owner marker inside it runs UNSANDBOXED.
So the managed root and the marker are both link-checked before anything is
created or written through them, and the marker is installed by rename instead
of by truncating a name the child can point somewhere else.
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
from typing import Literal

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_SUBDIR = "scratch"

#: Owner-pid marker written by the spawner right after the child starts.
OWNER_FILENAME = ".owner"

#: What :func:`record_owner` did. A spawner has to tell a write that could not
#: HAPPEN from one that was REFUSED: the first is hygiene lost (a full disk) and
#: the agent still runs, the second is a live child steering an unsandboxed
#: gateway write and the spawn must not continue. ``"stale"`` is the third
#: don't-continue case and is nobody's fault: the write failed AND the marker it
#: left behind could not be cleared, so the directory still names the gateway.
OwnerOutcome = Literal["recorded", "unwritable", "refused", "stale"]

#: A directory younger than this with no ``.owner`` yet is mid-spawn, not an
#: orphan: allocation happens before the child pid exists. Anything older
#: with no owner belongs to a spawn that never completed.
_UNOWNED_GRACE_SECONDS = 3600.0

_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ScratchBoundaryError(Exception):
    """A scratch path is a link, so writing through it would leave the boundary.

    Deliberately NOT an ``OSError``. Every write site here carries an ``except
    OSError`` branch meaning "the write did not happen, and the grace-window
    rule covers an unowned dir", and a planted link is not a failed write, so
    it must not read as one.

    The same event has two right answers, which is why the two raise sites are
    handled differently:

    * from :func:`allocate_scratch` no child exists yet, and scratch is hygiene
      rather than a spawn prerequisite -- the spawners catch this beside
      ``OSError`` and spawn with inherited temp.
    * from :func:`record_owner` a child is already LIVE and is aiming an
      unsandboxed gateway write at a path of its own choosing -- the spawners'
      live-process guard reaps it, which stops the attempt instead of declining
      one write of it.
    """


def scratch_root() -> Path:
    """The managed root: ``<data home>/scratch``."""
    return config_dir() / _SUBDIR


def _refuse_linked(path: Path, what: str) -> None:
    """Refuse *path* when it is a symlink or a Windows directory junction.

    :func:`platform_compat.is_link_or_junction`, never ``os.path.islink``:
    ``islink`` reports False for a junction, so an ``islink``-only guard would
    leave the one platform without ``O_NOFOLLOW`` following exactly the link
    the other two refuse.

    *what* is the path SHAPE for the log -- a managed-root or marker name plus
    the allocation's own ``<label>-<token8>`` basename. Never the target: which
    file a planted link names is the attacker's input, and nothing an operator
    can act on.
    """
    if not platform_compat.is_link_or_junction(path):
        return
    logger.warning("agent-scratch: refusing to write through a linked %s", what)
    raise ScratchBoundaryError(f"agent scratch {what} is a link")


def _write_owner_marker(directory: Path, pid: int) -> None:
    """Install *pid* as *directory*'s owner marker, never following a link.

    Two independent guards, because neither does the other's job:

    * an ``lstat`` REFUSAL on the directory and on the marker, so a planted
      link is a reported boundary event the caller can act on;
    * an ATOMIC INSTALL -- :func:`kiro_crew.atomic_write.atomic_write` stages
      the bytes in an ``O_EXCL`` temp beside the marker and moves it on with
      ``os.replace``, which does not follow the final component on any
      platform. A link planted in the window after the lstat therefore
      replaces the LINK, and its target keeps its contents; the previous
      ``Path.write_text`` opened ``O_WRONLY|O_CREAT|O_TRUNC`` by name and
      truncated that target instead.

    The directory must also already BE a directory, for the reason given at
    that check: the atomic install would otherwise create one.

    What is left is an ANCESTOR swapped between the lstat and the staging open.
    Closing that needs descriptor-relative traversal, which Windows does not
    expose -- ``atomic_write._refuse_linked_parent`` documents the same
    residual window for secret writes -- so an lstat refusal plus a
    non-following install is the portable ceiling here, not a chosen floor.
    """
    _refuse_linked(directory, f"scratch dir {directory.name!r}")
    if not _is_plain_dir(directory):
        # Absent, or replaced by a file. NOT an attack signal -- the allocation
        # can simply be gone -- but it still must not reach the write:
        # ``atomic_write`` does ``mkdir(parents=True, exist_ok=True)`` on the
        # parent, which ``Path.write_text`` never did, so an absent directory
        # would be built back. That resurrects a dir nothing allocated, and if
        # the managed root had been swapped for a link since the allocation
        # checked it, builds that tree under the link's target. An ``OSError``
        # lands in the fail-open branch, where a write that could not happen
        # belongs.
        raise NotADirectoryError(f"agent scratch dir is not a directory: {directory.name}")
    marker = directory / OWNER_FILENAME
    _refuse_linked(marker, f"{OWNER_FILENAME} in {directory.name!r}")
    atomic_write(marker, str(pid))


def _discard_owner_marker(directory: Path) -> bool:
    """Remove *directory*'s owner marker after a FAILED update; did it go?

    ``atomic_write`` stages into a temp and moves it on, so a write that never
    completed leaves the PREVIOUS marker byte-for-byte intact. On the update
    path those previous bytes are the provisional pid of the SPAWNING gateway,
    and a stale owner is worse than none: the gateway exits, its pgroup goes
    dead, and the next boot sweep reads dead-owner-plus-idle on a directory
    whose real owner -- the child, spawned ``start_new_session=True`` -- is
    still alive holding it. An UNOWNED dir is never swept, so removing the
    marker is what keeps that child's temp dir.

    ``os.unlink`` removes a link itself and never follows one, but the marker
    is still lstat-refused first, matching :func:`_refuse_linked`'s rule that
    nothing here acts on a path the child has turned into a link.

    False means the stale marker SURVIVED -- a Windows file lock on the marker
    is the reachable case, since deleting a file another process holds open is
    refused there. The caller must not treat that as hygiene lost: the
    directory still names a pid that dies with the gateway, which is the whole
    condition this discard exists to prevent.
    """
    marker = directory / OWNER_FILENAME
    if platform_compat.is_link_or_junction(marker):
        logger.warning(
            "agent-scratch: refusing to discard a linked %s in %r",
            OWNER_FILENAME,
            directory.name,
        )
        return False
    try:
        os.unlink(marker)
    except FileNotFoundError:
        return True  # never installed, or already gone
    except OSError:
        logger.debug(
            "agent-scratch: could not discard owner marker for %r", directory.name, exc_info=True
        )
        return False
    return True


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

    Raises :class:`ScratchBoundaryError` when the managed root is a link.
    """
    root = scratch_root()
    # Twice around the mkdir, which is both the step a pre-planted link
    # subverts and the moment one could win the race with the first check:
    # ``mkdir(parents=True, exist_ok=True)`` SUCCEEDS on a link to a directory,
    # so without this every allocation -- and the sweep's ``shutil.rmtree``
    # target -- lands under whatever that link names. Only the ``scratch``
    # component is judged, never the data home above it: reaching a home
    # directory through a link is ordinary, and refusing it would break every
    # spawn on such a host rather than protect it.
    _refuse_linked(root, f"managed root {_SUBDIR!r}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _refuse_linked(root, f"managed root {_SUBDIR!r}")
    safe = _LABEL_SAFE.sub("-", label)[:40].strip("-") or "agent"
    path = root / f"{safe}-{secrets.token_hex(4)}"
    # No ``exist_ok``: ``mkdir`` never follows the final component, so this
    # already fails on anything sitting at the name, a link included.
    path.mkdir(mode=0o700)
    try:
        _write_owner_marker(path, os.getpid())
    except (OSError, ScratchBoundaryError):
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def record_owner(path: Path, pid: int) -> OwnerOutcome:
    """Record the spawned child's pid so the boot sweep can check liveness.

    Reports which of four things happened, because a spawner must act on two
    of them and must not act on the other two:

    * ``"recorded"`` -- the marker now names *pid*.
    * ``"unwritable"`` -- an ``OSError``, and still fail-open: scratch is
      hygiene, an unowned dir is covered by the grace-window rule, and failing
      a spawn because the disk is full would stop the agent for a reason that
      is not a security one. The stale marker is DISCARDED first, so the
      directory really is unowned rather than still labelled with the
      provisional gateway pid (see :func:`_discard_owner_marker`).
    * ``"stale"`` -- the write failed AND the discard could not clear what it
      left, so the directory still names the gateway. The spawners fail the
      spawn on this exactly as they do on a refusal, for a different reason:
      not an attack, but a marker that will read as a dead owner over a live
      child, which is how the sweep comes to delete work in progress. Reaping
      the child is recoverable; deleting its temp dir later is not.
    * ``"refused"`` -- the directory or the marker is a LINK. The child is live
      by now and owns this directory, so a link there is it steering this
      unsandboxed gateway write. Nothing is written, and the spawners fail the
      spawn on it, reaping the child rather than leaving it running with one
      declined write behind it.

    A refusal is REPORTED rather than raised for two reasons. It keeps this
    callable from a caller that cannot absorb an exception without leaking the
    process it is recording, and it keeps the two "did not write" answers
    distinguishable: an exception for one beside a silent return for the other
    collapses them at every call site that already catches ``OSError``.
    """
    try:
        _write_owner_marker(path, pid)
    except ScratchBoundaryError:
        return "refused"  # already logged at warning by _refuse_linked
    except OSError:
        # Fail-open: the sweep never deletes an UNOWNED dir, so a failed write
        # leaves one that is kept, not reclaimed -- but only once the
        # provisional marker is gone, because a failed atomic write leaves the
        # gateway's pid in place and a STALE owner reads as dead to the sweep
        # while the child it names is still running.
        logger.debug("agent-scratch: could not record owner for %r", path.name, exc_info=True)
        if not _discard_owner_marker(path):
            logger.warning(
                "agent-scratch: %r still names this gateway after a failed owner update",
                path.name,
            )
            return "stale"
        return "unwritable"
    return "recorded"


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


def _tree_newest_mtime(root: Path, fallback: float) -> float:
    """The newest mtime anywhere in *root*'s tree (lstat, symlinks never followed).

    A live process writing through an already-open file descriptor never
    touches the top DIRECTORY's mtime, but every write refreshes the FILE's
    own mtime -- so tree-newest is the faithful idle signal on every
    platform, and the only one available on Windows (no process groups).
    Fail-safe: an unreadable entry returns *fallback* (reads as active, the
    sweep keeps the dir).

    Deliberately UNCAPPED: an entry cap that bails to *fallback* turns every
    tree larger than the cap into a permanently active-looking one, so a
    dead owner that wrote enough entries could never be reclaimed and
    repeated runs would exhaust storage. The walk runs off the event loop
    (``asyncio.to_thread``) on an hourly cadence, and its size is bounded
    by what one runtime wrote into its OWN scratch dir, so a full metadata
    walk is the right trade (see ``mcp_gateway.backend_tmp``).
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
    if platform_compat.is_link_or_junction(root):
        # The per-entry lstat below classifies the CHILDREN, and
        # "name-composed: cannot escape the root" holds only while the root is
        # real -- a link here aims every rmtree at another tree. Fail-open like
        # the rest of the sweep: report it and sweep nothing.
        logger.warning("agent-scratch: managed root %r is a link; skipping sweep", _SUBDIR)
        return 0
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
        marker = child / OWNER_FILENAME
        if platform_compat.is_link_or_junction(marker):
            # A LINK where the marker belongs, read by the one loop here that
            # DELETES. ``read_text`` follows it, so an owner planted by the very
            # process being judged would name whatever pid those bytes hold --
            # pick a dead one and the sweep destroys a live agent's own tree.
            # Joins the unowned-or-garbled rule below: never delete on evidence
            # the subject controls. The dir is then unreclaimable while the link
            # stands, which is the safe direction to fail.
            logger.warning(
                "agent-scratch: %r has a linked %s; not judging its owner",
                entry.name,
                OWNER_FILENAME,
            )
            continue
        try:
            pid = int(marker.read_text(encoding="utf-8").strip())
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
