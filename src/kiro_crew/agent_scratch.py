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

The WORK directory follows the session tree, not the process. The scratch
root is masked for every sandboxed process and each spawn is handed back only
its own directory as a private window -- so on their own, a dedicated subagent
process and a companion runtime could not read a brief the parent staged under
``$KIROCREW_SCRATCH``, and a runtime recycled for age or RSS would hand the
sessions it took over an empty directory mid-task. Both therefore receive the
tree's existing directory as a SECOND window (:func:`shared_scratch_window`)
and ``KIROCREW_SCRATCH`` names it (:func:`scratch_env`'s ``shared``), while
the temp triple and the kiro-cli log stay on the process's own directory.
Every process that mounts a tree it did not allocate ADDS itself to the
tree's owner marker (:func:`adopt_owner`) -- a child beside its parent, a
successor beside the predecessor it replaces -- and the sweep keeps a
directory while ANY named pgroup lives. A dead owner over a live user is how
the sweep comes to delete work in progress; naming every user is what rules
it out whichever process dies first (a parent that crashes under its
dedicated children, a successor that fails beside a draining predecessor).

kiro-cli's own log rides along. The CLI writes ``kiro-log/kiro-chat.log`` (plus
``mcp.log`` / ``lsp.log`` beside it) under ``$XDG_RUNTIME_DIR`` when that is
set, else ``$TMPDIR`` -- ONE file per machine, unlinked by whichever process
starts next once it passes 10 MiB. Every long-lived kiro-cli then keeps
writing into its own unlinked inode, which ``du`` cannot see and nothing
frees until the process exits; on Linux the runtime dir is a RAM-backed
tmpfs, so a host running many concurrent agents fills it and systemd stops
creating transient scopes. :func:`scratch_env` therefore pins
``KIRO_CHAT_LOG_FILE`` into this process's OWN scratch directory (the same
place the ``TMPDIR`` fallback already puts it on macOS), so the log is
per-process, on disk, and reclaimed with the directory. kiro-cli never
bounds a log while running, so :func:`cap_kiro_cli_logs` rotates any that
outgrows :data:`KIRO_CLI_LOG_CAP_BYTES` in place. The pin is set only where
that cap can run (:data:`_CAN_CAP_LOGS`): a pinned log nothing bounds is
worse than the CLI's own 10 MiB unlink, so Windows keeps kiro-cli's default
location until the cap runs there.

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
import threading
import time
from collections.abc import Iterable
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
OwnerOutcome = Literal["recorded", "unwritable", "refused", "stale", "garbled"]

#: A directory younger than this with no ``.owner`` yet is mid-spawn, not an
#: orphan: allocation happens before the child pid exists. Anything older
#: with no owner belongs to a spawn that never completed.
_UNOWNED_GRACE_SECONDS = 3600.0

_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: kiro-cli's log directory INSIDE a scratch dir, and the files it writes there.
#: The chat log MUST keep its default name: kiro-cli places ``mcp.log`` and
#: ``lsp.log`` beside the chat log only when it is named ``kiro-chat.log``.
KIRO_CLI_LOG_SUBDIR = "kiro-log"
KIRO_CLI_CHAT_LOG_NAME = "kiro-chat.log"
KIRO_CLI_LOG_NAMES = (KIRO_CLI_CHAT_LOG_NAME, "mcp.log", "lsp.log")
#: kiro-cli's own override for the chat log path.
KIRO_CHAT_LOG_FILE_ENV = "KIRO_CHAT_LOG_FILE"
#: A live kiro-cli log larger than this is rotated in place. At the CLI's
#: default level a process writes a few KiB a minute; ``KIRO_LOG_LEVEL=debug``
#: writes ~40 MiB a minute, and the cap exists for that setting.
KIRO_CLI_LOG_CAP_BYTES = 64 * 1024 * 1024
#: Newest bytes preserved in ``<name>.1`` when a log is rotated.
KIRO_CLI_LOG_KEEP_BYTES = 8 * 1024 * 1024
#: How often the gateway runs :func:`cap_kiro_cli_logs`. Bounds the overshoot
#: past the cap to ``interval x write rate``.
KIRO_CLI_LOG_CAP_INTERVAL_SECONDS = 300

#: The cap needs ``openat``-style descriptor-relative opens and ``O_NOFOLLOW``
#: to act on a directory the SUBJECT process owns; without them it does nothing.
_CAN_CAP_LOGS = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "pread")
    and os.open in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)


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


class SharedScratchJoinError(ScratchBoundaryError):
    """A live process could not JOIN the owner marker of a tree it INHERITED.

    Raised by the spawners at their :func:`adopt_owner` site only -- never for
    the process's own directory -- so a caller that inherits on behalf of a
    slot (``session_background.get_bg_session``) can tell "the inherited tree
    is unjoinable" apart from "this process's own marker was tampered with":
    the first is grounds to abandon the inherit and spawn again on a fresh
    tree, the second says nothing about the inherited tree, which still holds
    the sessions' staged work and must be kept for the next attempt.
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


def _write_owner_marker(directory: Path, pids: "int | Iterable[int]") -> None:
    """Install *pids* as *directory*'s owner marker, never following a link.

    One pid per line. A single owner is the ordinary shape; a successor that
    adopts a tree APPENDS itself (:func:`adopt_owner`), so a marker can name
    the draining predecessor and the live successor at once and the sweep keeps
    the directory while ANY of them lives.

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
    if isinstance(pids, int):
        pids = (pids,)
    atomic_write(marker, "\n".join(str(pid) for pid in pids))


#: Largest owner marker this module will read. Pids are at most 11 bytes each
#: and a tree names its LIVE users only (:func:`adopt_owner` prunes the dead),
#: so a real marker is a few hundred bytes; anything past this is not a marker
#: this code wrote. The marker sits inside a directory the agent process owns
#: and writes, so an unbounded read here would hand that process the
#: gateway's memory (a marker replaced with a link to ``/dev/zero``, or simply
#: a huge regular file).
_OWNER_MARKER_MAX_BYTES = 4096

#: Largest pid a marker may name: the C ``int`` the kernel's pid_t is on every
#: supported platform. ``os.killpg`` raises ``OverflowError`` -- not
#: ``OSError`` -- past it, so a marker naming a bigger number would escape the
#: liveness probe's error handling instead of reading as dead.
_PID_MAX = 2**31 - 1


def _read_owner_pids(marker: Path) -> tuple[int, ...]:
    """The pids a marker names, one per line, read as a BOUNDED regular file.

    The marker lives in the agent's own directory, so it is read the way the
    log cap reads the agent's logs: lstat-refused when it is a link
    (:class:`ScratchBoundaryError`), opened ``O_NOFOLLOW`` where the platform
    has it so a link planted after the lstat is not followed either, opened
    ``O_NONBLOCK`` so a FIFO planted there cannot park the reader, checked
    with ``fstat`` to be a regular file no larger than
    :data:`_OWNER_MARKER_MAX_BYTES`, and read only that far. Anything else --
    absent (``OSError``), not regular, oversized, not integers, or an integer no
    process can have (``ValueError``: non-positive or past :data:`_PID_MAX`) --
    is not a marker this code wrote, and the callers treat it as absence of
    evidence, never as a pid.
    """
    _refuse_linked(marker, f"{OWNER_FILENAME} in {marker.parent.name!r}")
    # O_NONBLOCK: a FIFO planted at the marker's name passes the link check and
    # O_NOFOLLOW, and a blocking O_RDONLY open of a FIFO with no writer never
    # returns -- from the sweep or from a spawn's adopt, on an executor thread.
    # Non-blocking, the open returns at once and the fstat below rejects it as
    # not a regular file; for a regular file the flag changes nothing.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(marker, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("owner marker is not a regular file")
        if info.st_size > _OWNER_MARKER_MAX_BYTES:
            raise ValueError("owner marker is larger than any this module writes")
        # To EOF, not one read: a short read would hand back a PREFIX of the
        # marker, and a prefix that names only dead pids reads as a dead owner
        # over the live one the tail names -- the deletion this marker exists to
        # prevent. Bounded by the cap, so a file that grows under the read
        # cannot be read without end.
        chunks: list[bytes] = []
        remaining = _OWNER_MARKER_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > _OWNER_MARKER_MAX_BYTES:
        raise ValueError("owner marker is larger than any this module writes")
    pids = tuple(int(line) for line in data.decode("utf-8").split() if line)
    if not pids:
        raise ValueError("empty owner marker")
    if any(pid < 1 or pid > _PID_MAX for pid in pids):
        raise ValueError("owner marker names a pid no process can have")
    return pids


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


#: Serializes read-modify-write of a marker across the gateway's executor
#: threads: two children of one parent spawn concurrently and both adopt the
#: same tree, and an unlocked append would drop one of them from the marker.
_ADOPT_LOCK = threading.Lock()


def adopt_owner(path: Path, pid: int) -> OwnerOutcome:
    """ADD *pid* to *path*'s owner marker, keeping every LIVE pid already named.

    For every process that mounts a tree it did not allocate: a dedicated
    subagent process or companion runtime beside its parent, a successor
    runtime beside the predecessor it replaces. Replacing the marker
    (:func:`record_owner`) would be wrong in both directions -- a child or a
    successor that then fails leaves a dead pid over a tree its parent or
    predecessor still writes, and a marker that keeps naming only the
    allocator lets that process's exit read as dead-owner over live users.
    Naming EVERY user keeps the directory exactly as long as any of them runs
    (:func:`sweep_dead_scratch` keeps a dir while ANY recorded pgroup lives).

    Pids whose process group is already dead are dropped on the way, so a tree
    that chains through many recycles carries a bounded marker rather than
    every pid it ever had. The read-modify-write is serialized with
    :data:`_ADOPT_LOCK` -- concurrent adopters of one tree are ordinary
    (siblings of one fan-out), and an unlocked append would lose one.

    The marker is read through :func:`_read_owner_pids` -- lstat-refused when
    linked, opened without following, bounded -- because it sits in a
    directory the agent process writes. An ABSENT marker is left absent and
    reported ``"recorded"``: an unowned tree is never swept, so "kept while
    *pid* lives" already holds, and a marker naming only *pid* would make the
    allocator's tree reclaimable the hour after *pid* exits. A marker that is
    not one this module wrote (garbled, oversized, not a regular file) is
    ``"garbled"`` without a write: the sweep skips such a tree for good, so
    mounting it risks a leak and never a deletion, and the spawner proceeds
    with a warning -- a fatal answer here would let anything that can write
    the tree (every agent process it is mounted into) veto every later spawn
    on it. Only a planted LINK is ``"refused"``: that one steers a gateway
    write, and the spawn must not carry on.

    Other outcomes as for :func:`record_owner`, with one deliberate asymmetry on
    the failure side. ``"unwritable"`` still discards the marker first: a tree whose
    marker names only the predecessor would be swept the hour after that process
    exits while the successor is using it, and an UNOWNED tree is never swept --
    a leak a human can see beats a deletion nobody can undo. ``"stale"`` (the
    discard failed too) is then the don't-continue case the spawner reaps on,
    exactly as it does for its own directory.
    """
    marker = path / OWNER_FILENAME
    try:
        with _ADOPT_LOCK:
            try:
                existing = _read_owner_pids(marker)
            except FileNotFoundError:
                # UNOWNED: the sweep never touches it, so the guarantee this
                # adoption exists for -- kept while *pid* lives -- already
                # holds, and writing a marker naming only *pid* would make the
                # tree reclaimable the hour after *pid* exits while the
                # process that allocated it may still be using it.
                logger.debug("agent-scratch: %r is unowned; nothing to join", path.name)
                return "recorded"
            except ValueError:
                # Not a marker this module wrote (garbled, oversized, not a
                # regular file): nothing is written over it, and the tree is
                # one the sweep skips for good (it reads the same ValueError),
                # so the join is unnecessary for safety and the spawner may
                # proceed. Distinct from a planted link because a garbled
                # marker steers nothing -- it only leaks.
                logger.warning(
                    "agent-scratch: %s in %r is not a marker this module wrote; the tree "
                    "is left as-is and will not be swept",
                    OWNER_FILENAME,
                    path.name,
                )
                return "garbled"
            live = tuple(p for p in existing if p != pid and _pgroup_alive(p))
            _write_owner_marker(path, (*live, pid))
    except ScratchBoundaryError:
        return "refused"
    except OSError:
        logger.debug("agent-scratch: could not adopt owner for %r", path.name, exc_info=True)
        if not _discard_owner_marker(path):
            logger.warning(
                "agent-scratch: %r still names only its previous owner after a failed adoption",
                path.name,
            )
            return "stale"
        return "unwritable"
    return "recorded"


#: Serializes the sweep's final look-and-delete against a spawn's
#: validate-and-touch (:func:`shared_scratch_window`), so neither can interleave
#: the other's two steps: the sweep never removes a tree a spawn has just marked
#: active, and a spawn never marks a tree the sweep is already removing. Held
#: only across lstat/utime and the rmtree; never across marker I/O.
_SWEEP_LOCK = threading.Lock()


def shared_scratch_window(path: Path | None) -> Path | None:
    """The scratch dir another process of the same session tree may be handed, or None.

    A path this returns is marked ACTIVE for the sweep's grace window (its
    directory mtime is refreshed under :data:`_SWEEP_LOCK`): the spawner mounts
    it next and its new user cannot be in the owner marker before the process
    exists, while the allocator may already be dead and the tree idle -- the
    crash-recovery case -- and an hourly sweep landing between the mount and
    the adoption would otherwise delete it. The sweep's own rule (a tree with
    a fresh mtime is in use, whoever owns it) then holds the tree for the hour
    a spawn needs seconds of; nothing has to be released, and a second heir
    validating the same tree simply refreshes it again.

    Returns *path* when it is a plain directory directly under the managed
    root; None (and a debug line) for anything else -- absent, a link, a file,
    or a path outside the root -- so the spawner falls back to the child's own
    directory alone. Fail-open in the same sense as the rest of this module:
    losing the shared window costs the child visibility, not the spawn.
    """
    if path is None:
        return None
    root = scratch_root()
    if path.parent != root:
        logger.debug("agent-scratch: shared window %r is not under the managed root", path.name)
        return None
    # The lstat and the touch are one step under the sweep lock, so the sweep
    # cannot remove the tree between "it exists" and "it is active".
    with _SWEEP_LOCK:
        if platform_compat.is_link_or_junction(path) or not _is_plain_dir(path):
            logger.debug("agent-scratch: shared window %r is gone or not a plain dir", path.name)
            return None
        try:
            if os.utime in os.supports_follow_symlinks:
                os.utime(path, None, follow_symlinks=False)
            else:
                os.utime(path, None)  # lstat above already refused a link
        except OSError:
            # No refresh means no hold. That only matters for a tree the sweep
            # could take: a readable marker naming only dead pids. A tree with a
            # live owner is never swept whatever its mtime, and an unowned or
            # garbled marker is never swept either, so those are handed out as
            # before. The dead-owner tree is exactly what the hold protects --
            # without it the sweep may remove the tree under the mount, so the
            # window is refused and the spawn falls back to its own directory:
            # visibility lost, not files.
            if _sweep_could_reclaim(path):
                logger.warning(
                    "agent-scratch: could not refresh shared window %r and its owners are "
                    "dead; not mounting a tree the sweep may reclaim",
                    path.name,
                    exc_info=True,
                )
                return None
            logger.debug(
                "agent-scratch: could not touch shared window %r", path.name, exc_info=True
            )
    return path


def _sweep_could_reclaim(path: Path) -> bool:
    """Would :func:`sweep_dead_scratch` judge *path* reclaimable on its owner alone?

    True only for a readable marker naming no live process group. Absent,
    linked, garbled or oversized markers are the cases the sweep skips for good
    (never delete on evidence the subject controls), so they read as False.
    """
    try:
        pids = _read_owner_pids(path / OWNER_FILENAME)
    except (OSError, ValueError, ScratchBoundaryError):
        return False
    return not any(_pgroup_alive(pid) for pid in pids)


def scratch_env(path: Path, *, shared: Path | None = None) -> dict[str, str]:
    """Env exports pointing a child's temp, scratch AND kiro-cli log at *path*.

    ``TMPDIR``/``TMP``/``TEMP`` cover ``tempfile`` and shell ``mktemp`` on
    both platforms; ``KIROCREW_SCRATCH`` is the prompt-visible name for
    deliberate work products (clones, logs, screenshots).
    ``KIRO_CHAT_LOG_FILE`` pins kiro-cli's log to ``<path>/kiro-log/kiro-chat.log``
    so it is per-process (see the module docstring), but only where
    :func:`cap_kiro_cli_logs` can bound it (:data:`_CAN_CAP_LOGS`); elsewhere
    (Windows) the key is omitted and kiro-cli keeps its default location. Where
    set, it overrides any inherited value, because an inherited value names a
    path SHARED with other processes, which is the failure this exists to prevent.

    *shared* is the session tree's work directory when this process is not
    the tree's first (see :func:`shared_scratch_window`): ``KIROCREW_SCRATCH``
    then names THAT directory, so a subagent and its parent -- or a recycled
    runtime's successor and the sessions it took over -- read and write one
    place under one name. The temp triple and the log stay on *path*: temp
    files are per-process by construction, and two live processes appending
    to one kiro-cli log is exactly the sharing the pin exists to prevent.
    """
    value = str(path)
    env = {
        "TMPDIR": value,
        "TMP": value,
        "TEMP": value,
        "KIROCREW_SCRATCH": str(shared) if shared is not None else value,
    }
    if _CAN_CAP_LOGS:
        env[KIRO_CHAT_LOG_FILE_ENV] = str(path / KIRO_CLI_LOG_SUBDIR / KIRO_CLI_CHAT_LOG_NAME)
    return env


def cap_kiro_cli_logs(
    *,
    cap_bytes: int = KIRO_CLI_LOG_CAP_BYTES,
    keep_bytes: int = KIRO_CLI_LOG_KEEP_BYTES,
) -> int:
    """Periodic: rotate in place every kiro-cli log under scratch past *cap_bytes*.

    kiro-cli holds each log open in append mode for the life of the process
    and checks size only at startup, so an outside bound has to act on the
    OPEN file: the newest *keep_bytes* are copied to ``<name>.1`` and the log
    is truncated to zero. ``O_APPEND`` makes the writer's next record land at
    the new end, so no hole is created. Records written between the tail copy
    and the truncate are lost; this is a diagnostic log, and losing a few
    lines beats losing the disk.

    Every scratch directory is OWNED by a sandboxed agent process while this
    runs unsandboxed, so nothing here trusts a name the agent controls: the
    ``kiro-log`` directory is opened ``O_NOFOLLOW`` and every member is opened
    relative to that descriptor, also ``O_NOFOLLOW``; a member is acted on only
    if the fstat of the descriptor actually opened shows a regular file with a
    single link owned by this uid (a planted hard link would otherwise truncate
    the file it points at); the rotated copy is created ``O_EXCL`` after
    unlinking the old name, never written through an existing entry. Per-entry
    fail-open. Platforms without descriptor-relative opens do nothing.

    Returns the number of logs rotated.
    """
    if not _CAN_CAP_LOGS:
        return 0
    root = scratch_root()
    if platform_compat.is_link_or_junction(root):
        logger.warning("agent-scratch: managed root %r is a link; skipping log cap", _SUBDIR)
        return 0
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return 0
    except OSError:
        logger.debug("agent-scratch: could not list %s; skipping log cap", root, exc_info=True)
        return 0
    rotated = 0
    for entry in entries:
        child = root / entry.name  # name-composed: cannot escape the root
        if not _is_plain_dir(child):
            continue
        rotated += _cap_log_dir(child / KIRO_CLI_LOG_SUBDIR, cap_bytes, keep_bytes)
    if rotated:
        hint = ""
        if os.environ.get("KIRO_LOG_LEVEL", "").lower() in ("debug", "trace"):
            hint = "; KIRO_LOG_LEVEL=%s in the gateway environment is the likely cause" % (
                os.environ["KIRO_LOG_LEVEL"],
            )
        logger.warning(
            "agent-scratch: rotated %d kiro-cli log(s) larger than %d MiB%s",
            rotated,
            cap_bytes // (1024 * 1024),
            hint,
        )
    return rotated


def _cap_log_dir(log_dir: Path, cap_bytes: int, keep_bytes: int) -> int:
    """Rotate the oversized members of one ``kiro-log`` directory; see the caller."""
    try:
        dir_fd = os.open(log_dir, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError:
        # Absent (the CLI has not logged yet), or a link (ELOOP) -- never followed.
        return 0
    try:
        return sum(
            1
            for name in KIRO_CLI_LOG_NAMES
            if _rotate_in_place(dir_fd, name, cap_bytes, keep_bytes)
        )
    finally:
        os.close(dir_fd)


def _rotate_in_place(dir_fd: int, name: str, cap_bytes: int, keep_bytes: int) -> bool:
    """Tail-copy then truncate ``name`` (relative to *dir_fd*) when it exceeds the cap."""
    try:
        fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return False  # absent, a link, or unopenable: nothing to bound
    try:
        info = os.fstat(fd)
        # Judge the descriptor we hold, not the name: a regular file, with the
        # one link kiro-cli gave it, owned by us. Anything else was planted.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            return False
        if info.st_size <= cap_bytes:
            return False
        keep = min(keep_bytes, info.st_size)
        try:
            tail = os.pread(fd, keep, info.st_size - keep)
            _write_rotated(dir_fd, name + ".1", tail)
        except OSError:
            # The copy is a courtesy; the truncate is the bound. A full disk is
            # exactly when the copy fails and the truncate matters most.
            logger.debug("agent-scratch: could not keep a tail of %s", name, exc_info=True)
        os.ftruncate(fd, 0)
        return True
    except OSError:
        logger.debug("agent-scratch: could not rotate %s", name, exc_info=True)
        return False
    finally:
        os.close(fd)


def _write_rotated(dir_fd: int, name: str, data: bytes) -> None:
    """Replace ``name`` under *dir_fd* with *data*, never writing through an existing entry."""
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    # O_EXCL after the unlink: an entry re-planted in between (a link, a hard
    # link) makes this fail with EEXIST instead of being written through.
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


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

    * A dir ANY of whose recorded owner pids is alive is never touched -- agent
      processes can outlive a gateway restart, so a fresh gateway must not
      clear wholesale, and a tree a successor adopted names its draining
      predecessor beside it (:func:`adopt_owner`).
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
            pids = _read_owner_pids(marker)
        except (OSError, ValueError, ScratchBoundaryError):
            # Unowned, garbled, oversized or linked: never delete on absence
            # of evidence.
            continue
        if any(_pgroup_alive(pid) for pid in pids):
            # A tree adopted by a successor names its predecessor too; either
            # one alive is a live user.
            continue
        with _SWEEP_LOCK:
            # Final look under the lock a spawn's validate-and-touch also
            # takes: a spawn that marked this tree active since the idle
            # reading above wins, and the tree is left for the next sweep.
            try:
                if reference - os.lstat(child).st_mtime < _UNOWNED_GRACE_SECONDS:
                    continue
            except OSError:
                continue
            shutil.rmtree(child, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("agent-scratch: sweep removed %d dead idle scratch dir(s)", removed)
    return removed
