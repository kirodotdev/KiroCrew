"""Durable work root for state that must OUTLIVE the process that created it.

:mod:`kiro_crew.agent_scratch` hands every spawned agent process its own
``<data home>/scratch/<label>-<token8>`` and points ``TMPDIR`` and
``KIROCREW_SCRATCH`` at it. That directory's lifetime is bound to the process
that got it: the sweep reclaims it once the owner's process GROUP is dead and
the tree is idle. For per-process residue that is the right contract, and the
module states it as liveness-keyed, never age-keyed.

Some work has the opposite requirement. A scheduled job whose work directory
is created by one run and advanced by a later, DIFFERENT run -- a long-lived
clone that several runs take turns on -- needs a directory that survives the
creating process by construction. Per-process scratch reclaims such a
directory at exactly the wrong moment, so an author with nowhere else
sanctioned to go reaches for the system temp dir, which is the only location
that visibly outlives a process.

That fallback is the failure this module removes. Where the system temp dir is
a tmpfs, its pages are charged to the agent cgroup as ``shmem``; with no swap
they are unreclaimable, so the slice climbs to ``memory.high`` and stays
there, throttling every deadline inside it while ``oom_kill`` stays 0. Nothing
crashes and no symptom names the cause: an ACP handshake blows its budget,
zero-token script crons time out -- including whichever job would reclaim the
temp dir, so the one mechanism that relieves the pressure is starved by it --
and a throttled credential check surfaces as a false logged-out error.

Layout: ``<data home>/work/<key>/``, a sibling of the scratch root on the same
real disk, deliberately not tmpfs for the reason the scratch root states
verbatim: this residue can be large and must not occupy RAM.

* The *key* is caller-supplied and STABLE (an issue number, a pull-request
  number). Deterministic naming is the point, because a later run finds the
  directory again by computing the same key. Keys are therefore VALIDATED
  rather than sanitized (:data:`_KEY_RE`): rewriting a key would map two
  distinct ones onto a single directory and mix their work. A key that is
  UNIQUE per run -- a content fingerprint, a timestamp, a run id -- defeats the
  point twice: no later run recomputes it, so nothing is ever rejoined, and
  each one leaves behind a per-key lock file this module does not reclaim.
* There is no owner pid and no liveness gate. Outliving the creating process
  is the contract, so a liveness signal would reclaim precisely what has to
  survive.
* Reclamation is ONE idle window, and there is deliberately no way for a
  caller to shorten it. A window a caller can shorten needs an OWNER, because
  two runs legitimately share a key: whichever run shortens it is then speaking
  for the other, and wall-clock order cannot tell "the run holding this key
  finished" from "a run that lost the key finished", since the stale one's
  signal arrives later either way. Identity is the only thing that separates
  them, this root has none, and an idle window needs none -- so the window that
  cannot be shortened is the honest one. A caller-owned release lands with the
  first consumer that can prove ownership, not before.
* A rejoin and a sweep of the SAME key are serialized by a per-key lock, and the
  rejoin refreshes the tree before returning it. Without both, the sweep's only
  signal (tree-newest mtime) makes the cross-run case unsafe by construction: a
  weekly job rejoining a tree idle for a week would be handed a directory the
  next hourly wake is still entitled to ``rmtree``, and a consumer holding open
  descriptors would keep writing into unlinked inodes with nothing to notice.
  The sweep takes that lock WITHOUT waiting, so hygiene never blocks work.

The root is NOT reachable from a sandboxed process, and that is a HARD boundary
rather than a discouragement. ``sandbox`` masks it by name beside the scratch
root, and on Linux the mask is a writable empty tmpfs bind: an ``allocate_work``
call made from inside the sandbox therefore SUCCEEDS and hands back a directory
whose bytes vanish with the namespace -- the very shmem pathology described
above, reached through the module that exists to remove it. So the only caller
that may allocate here is unsandboxed gateway code, of which this repository's
sweep on the maintenance task is the first.

A sandboxed consumer is served the way ``apps/backend.py`` already serves the
Notes state files it owns: the code that SPAWNS it allocates host-side and passes
that one selected key directory back as ``extra_visible_dirs``, so the mask still
fences every other sandboxed process. No environment variable names this root --
a variable beside ``KIROCREW_SCRATCH`` would both collapse the distinction
between per-process residue and cross-process state that this module exists to
draw, and advertise to a sandboxed agent a path its own namespace makes a lie. An
agent that needs a durable directory gets one from the code that spawns it, never
from its own environment.

Sweep hygiene follows the house rules of :mod:`kiro_crew.agents_janitor`, and
the helpers below are local copies for the same reason
:mod:`kiro_crew.mcp_gateway.backend_tmp` keeps its own: ``os.lstat``
classification, symlinks never followed, deletion only for direct children of
the managed root, and per-entry fail-open, because hygiene must never take the
gateway down.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Leaf of the data home holding every cross-process work directory. Named here
#: and in THREE fences, each pinned to this name by its own test because each one
#: alone leaves a different path open: ``sandbox._CREW_HIDDEN_LEAVES`` masks it
#: from a spawned subprocess, ``sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES``
#: materialises it so that mask is not vacuous on a home where nothing has
#: allocated yet, and ``security.paths._CREW_SECRET_LEAVES`` refuses it to the
#: agent's own file tools. A rename here cannot quietly unfence the root.
WORK_DIRNAME = "work"

#: Suffix of the per-entry lock file that serializes a rejoin against the sweep,
#: kept BESIDE the entry rather than inside it so it survives the entry's own
#: ``rmtree`` and both sides keep agreeing on one file. The leading dot cannot
#: collide with a key (:data:`_KEY_RE` requires a leading alphanumeric), and the
#: file is a plain file, so the sweep skips it like any other non-directory. It
#: is never reclaimed: one empty file per distinct key is a bounded cost, and
#: deleting a lock file another process holds is the race this lock exists to
#: prevent.
#:
#: Locking is its ONLY duty. It deliberately carries no claim about whether this
#: module allocated the entry beside it: surviving the entry's ``rmtree`` is what
#: makes it a usable lock, and it is exactly what would make it a lying witness,
#: because the file outlives every directory it ever served. Allocation evidence
#: therefore lives INSIDE the entry (:data:`_MARKER_NAME`).
_LOCK_SUFFIX = ".lock"

#: Name of the allocation marker, a plain empty file written INSIDE each entry by
#: :func:`allocate_work` and required by :func:`sweep_work_root` before it deletes
#: anything. Inside, because the evidence has to share the DIRECTORY's lifetime:
#: ``rmtree`` takes the marker with the tree, so no marker can outlive the entry it
#: certified and authorize a delete at that name afterwards. A sidecar cannot have
#: that property -- it survives by design, so a key allocated and later reclaimed
#: would leave permanent standing evidence, and a directory an operator restored at
#: that name would be deleted on the strength of it.
#:
#: The leading dot keeps it out of the way of an entry's own contents, and the
#: marker is written ONLY into a directory :func:`allocate_work` itself created --
#: never onto one already sitting at the key, which is refused instead. Writing it
#: on adoption would be enough to break the guarantee on its own: the stranger
#: would then carry the exact evidence the sweep requires, and a directory an
#: operator placed here, or restored at a name an earlier sweep reclaimed, would be
#: deleted the first time any job allocated that key. So a directory this module
#: never created has no marker and is never reclaimed, however long it has been
#: idle.
_MARKER_NAME = ".kirocrew-work"

#: Idle seconds before an entry is reclaimed. Seven days matches the ceiling an
#: escalation hold already uses: a tree nothing has touched for a week is
#: abandoned, while one a job still advances never reaches the window, because
#: every write refreshes the tree's newest mtime. One window, because nothing
#: here can prove which run speaks for a shared key.
#:
#: A consumer whose own PERIOD exceeds this window loses its tree every cycle and
#: silently starts over, since nothing writes into the tree between its runs. There
#: is no per-entry escape from that, and ``grace`` below is NOT one: it applies to a
#: whole sweep, and the hourly maintenance wake that does the reclaiming in practice
#: passes no argument at all, so nothing another caller passes can lengthen one
#: entry's life. The obligation therefore sits with the consumer, and it is the
#: plain one: something has to touch the tree inside the window, so work whose gap
#: between runs is longer than this needs a home that is not this root. Recording a
#: per-entry retention in the marker would express it, at the price of turning a
#: presence check into a parsed file with a forgeable value, for a caller that does
#: not exist yet.
IDLE_GRACE_SECONDS = 7 * 24 * 3600.0

#: Keys come from code, not from user text, so this VALIDATES instead of
#: rewriting. A leading alphanumeric rules out ``.`` and ``..`` by
#: construction, and the class admits no separator on either platform, so
#: ``work_root() / key`` cannot leave the managed root.
#:
#: The keyspace is FLAT and global, and rejoining is the contract, so two callers
#: that compute the same key share one directory and advance each other's work.
#: A bare number is therefore never a key: an issue and a pull request numbered
#: alike would collide. Compose a key as ``<caller>-<id>`` -- the caller's own
#: name, then the identifier it is keyed on -- so the prefix is what keeps two
#: unrelated callers apart.
_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class WorkRootBoundaryError(Exception):
    """A managed path cannot be used, and nothing was created or written through it.

    Two cases reach this. A managed component is a LINK, so honouring it would
    write, and later delete, outside the root. Or the key is already occupied by a
    directory this module did not allocate, so adopting it would hand the sweep
    delete authority over data nothing here created.
    """


def work_root() -> Path:
    """The managed root: ``<data home>/work``."""
    return config_dir() / WORK_DIRNAME


def _validated_key(key: str) -> str:
    """Return *key* unchanged, or raise :class:`ValueError`.

    Deliberately not a sanitizer. A sanitizer maps every rejected spelling onto
    some accepted one, so two callers with different keys can land in one
    directory and advance each other's work; a caller that gets a refusal
    instead fixes its key.
    """
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError("work-root key must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    return key


def _refuse_linked(path: Path, what: str) -> None:
    """Refuse *path* when it is a symlink or a Windows directory junction.

    :func:`platform_compat.is_link_or_junction`, never ``os.path.islink``:
    ``islink`` reports False for a junction, so an ``islink``-only guard would
    leave the one platform without ``O_NOFOLLOW`` following exactly the link
    the other two refuse.

    *what* is the path SHAPE for the log, never the target: which file a
    planted link names is the planter's input, and nothing an operator acts on.
    """
    if not platform_compat.is_link_or_junction(path):
        return
    logger.warning("work-root: refusing to write through a linked %s", what)
    raise WorkRootBoundaryError(f"work root {what} is a link")


def _is_plain_dir(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def _tree_newest_mtime(root: Path, fallback: float) -> float:
    """The newest mtime anywhere in *root*'s tree (lstat, symlinks never followed).

    A process writing through an already-open descriptor never touches the top
    DIRECTORY's mtime, but every write refreshes the FILE's own mtime -- so
    tree-newest is the faithful idle signal on every platform, and the only one
    available here, since this root has no owner to probe. Fail-safe: an
    unreadable entry returns *fallback*, which reads as active and keeps the
    directory.

    Deliberately UNCAPPED, matching the scratch sweep: a cap that bails to
    *fallback* turns every tree larger than the cap into a permanently
    active-looking one, so a genuinely abandoned clone could never be
    reclaimed. The walk runs off the event loop on an hourly cadence.
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


def _lock_path(root: Path, key: str) -> Path:
    """The lock file serializing *key*'s rejoin against the sweep."""
    return root / f".{key}{_LOCK_SUFFIX}"


def _marker_path(entry: Path) -> Path:
    """The allocation marker inside *entry*."""
    return entry / _MARKER_NAME


def _has_allocation_marker(entry: Path) -> bool:
    """Did :func:`allocate_work` create *entry*? Read with ``lstat``, never followed.

    A plain file only. A marker that is a LINK is not evidence: the entry's own
    writer chooses what it names, so honouring it would let a planted link
    authorize the delete of a directory this module never allocated -- the same
    reason every other component here is judged with ``lstat``.
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
    """Stamp *directory*'s own mtime to now, so an idle tree reads as in use.

    This is what makes a REJOIN mean "in use" to :func:`sweep_work_root`, whose
    only signal is the tree's newest mtime. Without it, rejoining a tree nothing
    has written to for a week hands the caller a directory the very next sweep is
    still entitled to delete: ``mkdir(exist_ok=True)`` does not touch an existing
    directory's mtime.

    A failure here is NOT swallowed, and that is the whole contract: a stamp that
    silently did not happen leaves the tree in exactly the state having no stamp
    at all leaves it, so a caller would be handed a rejoined tree the next sweep
    deletes under it. Letting the ``OSError`` out means the caller learns its
    allocation did not succeed instead of receiving a path that looks live and is
    not; the empty directory left behind already carries its allocation marker, so
    the sweep can still reclaim it, which is the safe direction to fail in.
    """
    os.utime(directory)


def allocate_work(key: str) -> Path:
    """Create the work directory named by *key*, or REJOIN this module's own, and return it.

    Idempotent by design: a later, different process passing the same key gets
    the same directory, which is the whole reason this root exists. A rejoin
    requires this module's own allocation marker inside the directory, so a
    stranger already sitting at the key is REFUSED rather than adopted: marking
    one would hand the sweep delete authority over data nothing here created.

    The whole rejoin runs under this key's own lock, and refreshes the tree
    before returning it, so a directory handed to a caller is never one the sweep
    is still entitled to delete: a sweep either sees the refreshed mtime and
    keeps the tree, or finishes first and the rejoin then hands back a fresh
    empty directory rather than a tree deleted under a live consumer. Waiting is
    bounded by the lock helper, which fails CLOSED -- a rejoin that cannot
    serialize raises instead of racing.

    Raises :class:`ValueError` for a key this root cannot name, and
    :class:`WorkRootBoundaryError` when a managed component is a link or when the
    key is occupied by a directory this module did not allocate.
    """
    safe = _validated_key(key)
    root = work_root()
    # Twice around each mkdir, which is both the step a pre-planted link
    # subverts and the moment one could win the race with the first check:
    # ``mkdir(..., exist_ok=True)`` SUCCEEDS on a link to a directory, so
    # without the second check every write here -- and the sweep's ``rmtree``
    # -- lands under whatever that link names. Only the managed components are
    # judged, never the data home above them: reaching a home directory
    # through a link is ordinary, and refusing it would break every caller on
    # such a host rather than protect it.
    _refuse_linked(root, f"managed root {WORK_DIRNAME!r}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
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
                # Something is ALREADY at this key, and only a tree THIS module
                # allocated may be rejoined. Marking a stranger would hand the
                # sweep irreversible delete authority over data nothing here
                # created -- the very thing the marker exists to withhold -- and
                # it would break the guarantee this module states twice over, for
                # an operator-placed directory and for one restored at a name an
                # earlier sweep reclaimed. A genuine rejoin is unaffected: it
                # always finds the marker its own earlier allocation left inside
                # the tree, because only ``rmtree`` takes that marker away.
                _refuse_linked(path, f"work dir {safe!r}")
                if not _has_allocation_marker(path):
                    raise WorkRootBoundaryError(
                        f"work dir {safe!r} already exists without this module's "
                        "allocation marker, so it was not allocated here and is "
                        "not adopted"
                    )
            else:
                # Created here, so the marker names a directory this module made.
                # A failure between the two would leave an unmarked directory that
                # every later call must refuse, wedging a deterministic key for
                # good, so the directory is removed and the caller retries from a
                # clean name instead. ``os.rmdir`` cannot take data with it: it
                # removes an EMPTY directory only, and nothing has been handed out
                # yet for anything to have been written into.
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

    Every read here happens under the caller's per-entry lock, which is the point:
    the mtime that decides deletion is read after any rejoin has either completed
    or been shut out, so the decision cannot be made stale by one arriving between
    the read and the ``rmtree``.
    """
    try:
        idle = reference - _tree_newest_mtime(child, fallback=reference)
    except OSError:
        return False
    if idle < grace:
        return False
    try:
        shutil.rmtree(child)
    except OSError:
        logger.debug("work-root: could not remove %s", child, exc_info=True)
        return False
    return True


def sweep_work_root(now: float | None = None, *, grace: float = IDLE_GRACE_SECONDS) -> int:
    """Periodic sweep: remove work directories idle past *grace*.

    There is no liveness signal here, unlike the scratch sweep: outliving its
    creator is this root's contract, so a dead-owner test would reclaim the
    entries the root exists to keep. Idleness therefore carries the whole
    decision, and it is the same window for every entry -- nothing here can
    prove which of two runs sharing a key speaks for it.

    *grace* scopes THIS sweep, every entry it walks, and it is not a way to give
    one entry a longer life: the hourly maintenance wake that reclaims in practice
    passes no argument, so it always judges by :data:`IDLE_GRACE_SECONDS`. The
    parameter serves a caller running its own targeted sweep, and the tests.

    Each entry is judged and removed under that entry's own lock, taken WITHOUT
    waiting: a key a rejoin holds right now is in use, so it is skipped and the
    next wake judges it again. Not waiting is deliberate -- hygiene must never be
    the thing that blocks work, and an entry deferred an hour costs nothing.

    Every case, and the verdict each gets:

    * a linked managed root -- nothing is swept at all, because the
      name-composed child paths below stay inside the root only while the root
      itself is real;
    * a missing root -- nothing to do;
    * an unlistable root -- reported, and nothing is swept;
    * a child that is not a plain directory (a stray file, a symlink, a
      junction, another key's lock file) -- skipped, never deleted;
    * a child with NO allocation marker inside it -- skipped, because nothing
      here allocated the directory currently at that name: the marker is written
      only by ``allocate_work``, inside the entry, so it dies with the entry. A
      directory an operator placed at this leaf themselves is therefore never
      reclaimed, however long it has been idle, and neither is one restored at a
      name this sweep reclaimed earlier;
    * a child whose marker is a LINK -- not evidence, so skipped, since the
      entry's own writer chooses what such a link names;
    * a child whose lock file is a LINK -- skipped and reported, since locking
      through it would serialize on a file the entry's writer chose;
    * a child whose lock a rejoin holds -- skipped for this wake;
    * a child whose tree cannot be walked -- kept, because an unreadable tree
      reads as active;
    * a child idle less than *grace* -- kept;
    * a child idle past *grace* -- removed;
    * a delete that fails -- reported and skipped, per entry.

    An entry a process holds open without writing reads as idle, and past its
    window it is removed. That is the accepted cost of having no liveness gate:
    a job that still needs its directory refreshes the tree by working in it,
    and one nothing has written to for a week is abandoned.

    Runs off the event loop (the gateway hands it to a thread), which the lock
    helper requires: a poll-sleep on the loop thread would freeze every session.

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
            # No allocation evidence INSIDE this entry, so it is not this module's
            # to delete. The marker shares the directory's lifetime: ``rmtree``
            # takes it along, so a key this sweep reclaimed leaves none behind and
            # a directory restored at that same name is skipped for good rather
            # than deleted on the strength of the previous incarnation's evidence.
            # The sidecar lock cannot serve here for exactly that reason -- it
            # survives every entry it ever served, so its presence says nothing
            # about the directory currently at this name. Tested BEFORE the lock is
            # opened, because opening CREATES the lock file, and an entry this
            # module will never sweep should not accumulate one.
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
            # ``BlockingIOError`` is the ordinary case: a rejoin holds this key,
            # so the entry is in use. Any other error means the lock itself could
            # not be taken, and an unserialized delete is exactly what this lock
            # exists to prevent, so the entry is left for the next wake.
            logger.debug("work-root: %r not reclaimable this wake", entry.name, exc_info=True)
            continue
    return removed
