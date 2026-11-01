"""Large-transfer staging under the data home, off the system temp dir.

Snapshot build, snapshot restore and the dashboard state import each stage
multi-hundred-MB-to-GB trees before they produce their final artifact: the
snapshot build copies the whole data home in the clear, the restore extracts a
bundle, and the import EXTRACTS its uploaded archive into a working tree. Done
with a bare ``tempfile.TemporaryDirectory()`` -- no ``dir=`` -- they land in the
system temp dir, which under the shipped systemd unit is ``/tmp``. On a host
whose ``/tmp`` is a tmpfs (the default on Fedora, Arch, Amazon Linux 2023 and
Debian 13) those bytes are held in RAM until they are deleted, so a multi-GB
extraction, or a nightly backup of a large data home, can fill ``/tmp`` (50% of
RAM) or push the gateway's cgroup into reclaim; with no swap the pages cannot be
reclaimed, and a full ``/tmp`` hands every other process on the host ``ENOSPC``.

The import's UPLOAD spool (the 2 GiB ``.zip`` written before extraction) is NOT
staged here: ``dashboard.part_stream`` creates it on the serving event loop under a
cancellation-safety invariant shared by three upload handlers, and pointing that
on-loop create at a data home that may be NFS would risk freezing the loop on a
stalled metadata op. The spool stays in the system temp dir; this module stages the
large extraction/build work, not that spool.

:func:`staging_dir` returns ``<data home>/staging``, a sibling of the scratch
and work roots on the SAME real disk as the destination the staged bytes are
renamed toward. Staging there costs RAM only where the data home itself is a
tmpfs (which it is not under any shipped layout), and the final rename stays on
one filesystem, so it stays atomic. The callers keep using their own
``tempfile`` context managers and simply pass this directory as ``dir=``; the
context manager cleans the per-call subtree up on success AND on failure, so the
only residue is a tree orphaned by a hard crash (SIGKILL) between the create and
the context manager's cleanup.

That residue is what :func:`sweep_staging` reclaims. It is NOT redirecting the
gateway's ``tempfile`` base: ``sandbox_mount_sweep._mount_source_candidate_roots``
deliberately keys the namespace launcher's last-resort bind-source root on
``tempfile.gettempdir()`` and documents that the two must agree, so this module
touches only the three named staging sites and leaves ``tempfile.tempdir``
alone.

The root is owner-only (0700) and link-checked, the same tightening every
directory in the data home gets -- NOT a sandbox mask. Its sandbox disposition is
VISIBLE, matching the destinations these transfers produce: a snapshot's archive
lands in the fully-visible ``snapshots`` leaf (or a user-named path), and a
restore/import installs into the sandbox-visible data home. A staging copy is no
more exposed than the artifact it becomes -- a sibling agent could already read
the finished snapshot -- so staging adds no new exposure and gets no mask the
issue did not ask for (AGENTS.md). Because it is unmasked, an agent shelling out
to ``kirocrew snapshot``/``restore`` from inside the sandbox stages here on real
disk exactly like the unsandboxed gateway, with no tmpfs mask to fall back around.

Sweep hygiene follows the house rules of :mod:`kiro_crew.work_root` and
:mod:`kiro_crew.agents_janitor`: ``lstat`` classification, symlinks and
Windows junctions never followed, deletion only for direct children of the
managed root, per-entry fail-open so hygiene can never take the gateway down.
The root is PINNED by descriptor before it is listed (``pinned_directory``), so a
root swapped for a symlink cannot turn the LISTING onto another directory. The
removal of each idle subtree is :func:`pinned_fs.remove_tree_pinned` -- the one
shared pinned whole-tree delete every disposable-tree caller uses -- given a target
composed from ``config_dir().resolve()`` (the data home, which the agent cannot
swap) joined to the LITERAL ``staging/<name>``, NEVER ``Path.resolve()`` of the
agent-writable staging leaf. ``remove_tree_pinned`` pins the parent chain one
``openat`` per component with ``O_NOFOLLOW``, so a staging leaf (or the name)
swapped for a symlink to the data home is refused by that open rather than
followed, and the governance tree outside staging is never reached. The tree-mtime
idle probe (a transfer writing through an open descriptor never touches the top
directory's mtime, but every write refreshes the file's own) reuses the shared
:func:`work_root._tree_newest_mtime`, and is re-confirmed inside the pinned
``approve`` callback against the scanned descriptors before anything is removed.
Unlike ``work_root`` there is no caller-supplied key, no allocation marker and no
rejoin: every entry here is a per-call ``tempfile`` name owned by exactly one
in-flight transfer, so idleness past a short grace is the whole decision and an
entry a live transfer is still writing never reaches the window, because every
write refreshes the tree's newest mtime.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import time
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.config.loader import config_dir
from kiro_crew.platform_compat import PinnedDirectory, pinned_directory
from kiro_crew.work_root import _tree_newest_mtime

logger = logging.getLogger(__name__)

#: Leaf of the data home holding every large-transfer staging directory. A
#: sibling of the scratch and work roots, and -- like the ``snapshots`` leaf the
#: archive lands in -- an ordinary VISIBLE data-home directory, not a masked one.
STAGING_DIRNAME = "staging"

#: Idle seconds before a crash-orphaned staging entry is reclaimed. Six hours:
#: long enough that the slowest legitimate transfer (a multi-GB import extraction
#: on a loaded host) is never judged idle while it still runs -- every write
#: refreshes the tree's newest mtime, so an active transfer never approaches the
#: window -- and short enough that a tree orphaned by a hard crash does not sit in
#: the data home for days. There is no owner pid to probe (the creating process is
#: gone in the only case this reclaims), so idleness carries the whole decision,
#: the same shape ``work_root`` uses with a longer window for its cross-process
#: trees.
IDLE_GRACE_SECONDS = 6 * 3600.0


def staging_dir() -> Path:
    """The managed staging root: ``<data home>/staging``, owner-only.

    Created 0700 on first use and re-asserted owner-only on every call -- the same
    tightening every data-home directory gets, so a tree staged here is not
    readable by another local account between its create and its rename. This is
    NOT a sandbox mask: the leaf's disposition is VISIBLE, matching the
    destinations these transfers produce (the ``snapshots`` archive leaf and the
    data home a restore/import installs into), so an in-sandbox ``kirocrew
    snapshot``/``restore`` stages here on real disk exactly like the unsandboxed
    gateway. Link-checked the way ``storage_staging.staging_root`` and
    ``work_root`` are: a symlink or Windows junction planted at the leaf would put
    every staged file outside the data home, which no per-file check downstream can
    see, so it is refused rather than followed.

    One function so the callers share one root and one test can point it at a temp
    dir. The caller passes the returned path as ``dir=`` to its own ``tempfile``
    context manager; the per-call subtree's lifetime and cleanup stay the caller's.
    """
    base = config_dir()
    staging = base / STAGING_DIRNAME
    # Refuse a link at the leaf BEFORE and AFTER the mkdir: ``mkdir(exist_ok=True)``
    # happily accepts a pre-existing link to a directory, so resolving both sides
    # is what catches a leaf (or a component above it) swapped for a link that
    # would land staged bytes outside the data home. ``is_link_or_junction`` rather
    # than ``os.path.islink`` so a Windows junction is caught too.
    if platform_compat.is_link_or_junction(staging):
        raise ValueError("staging directory is not a real directory")
    # No parents=True: the leaf sits directly under the data home, which exists for
    # as long as the gateway (or the CLI's resolved home) does. A missing parent is
    # a real error, not something to paper over with a freshly minted tree.
    staging.mkdir(mode=0o700, exist_ok=True)
    if staging.resolve() != (base.resolve() / STAGING_DIRNAME):
        raise ValueError("staging directory resolves outside the data home")
    if not staging.is_dir():
        raise ValueError("staging directory is not a real directory")
    if platform_compat.IS_POSIX:
        platform_compat.chmod_safe(str(staging), 0o700)
    else:
        platform_compat.restrict_dir_to_owner(str(staging))
    return staging


def sweep_staging(now: float | None = None, *, grace: float = IDLE_GRACE_SECONDS) -> int:
    """Remove staging directories idle past *grace*; return how many went.

    Reclaims only crash orphans: a successful or failed transfer removes its own
    per-call ``TemporaryDirectory`` subtree itself, so an entry is left here only
    when the process was SIGKILLed between the create and that cleanup. There is no
    liveness signal (the creator is gone in exactly the case this reclaims) and no
    key, so idleness is the whole decision, judged the same for every entry.

    Each entry is read and removed fail-open and per-entry, so one unreadable or
    stuck entry never aborts the sweep or takes the gateway down. The cases:

    * a linked managed root -- nothing is swept: the root is PINNED before it is
      listed, so a link planted at it is refused by the open itself;
    * a missing root -- nothing to do;
    * an unlistable root -- reported, nothing swept;
    * a plain DIRECTORY (a snapshot build / restore extraction / import extraction
      ``TemporaryDirectory`` subtree) idle past *grace* -- removed; idle less -- kept;
      unwalkable -- kept (reads as active);
    * anything else (a plain file, a symlink, a Windows junction, a socket, a FIFO)
      -- skipped, never deleted. Every staging site creates a directory subtree, so a
      top-level non-directory is not a shape this leaf produces;
    * a delete that fails or is refused by the pinned walk -- reported and skipped.

    Runs off the event loop (the gateway hands it to a thread). *grace* scopes the
    whole sweep and the maintenance wake passes none, so it always judges by
    :data:`IDLE_GRACE_SECONDS`; the parameter serves a targeted caller and the
    tests.
    """
    root = config_dir() / STAGING_DIRNAME
    reference = time.time() if now is None else now
    # Pin the root by descriptor BEFORE listing it, instead of a path-based
    # ``is_link_or_junction`` check followed by ``os.scandir(root)`` /
    # ``os.unlink(root / name)``. The staging leaf is agent-writable (VISIBLE
    # disposition), so between a by-name check and a by-name delete an agent could
    # swap the root directory for a symlink to the data home; the path-based
    # ``os.unlink`` would then follow it and delete a governance file such as
    # ``security_policy.json`` outside the sandbox. ``pinned_directory`` opens the
    # root with ``O_DIRECTORY | O_NOFOLLOW`` (POSIX) or a no-``FILE_SHARE_DELETE``
    # handle (Windows): a symlink planted at the root is refused by the open, and
    # every enumeration and deletion below is relative to that descriptor, so the
    # root cannot be redirected out from under the sweep after it is pinned. This is
    # the same pinned-walk discipline ``storage_staging`` and ``session_transfer_staging`` use.
    try:
        root_pin = pinned_directory(root)
    except FileNotFoundError:
        return 0
    except OSError:
        # A link at the root (NotADirectoryError / ELOOP), or an unopenable root:
        # report and sweep nothing, exactly as the old link-and-unlistable branches
        # did, but now the refusal is the open itself rather than a racy precheck.
        logger.warning(
            "staging: managed root %r is not a pinnable directory; skipping sweep", STAGING_DIRNAME
        )
        return 0
    with root_pin:
        try:
            names = root_pin.names()
        except OSError:
            logger.debug("staging: could not list %s; skipping sweep", root, exc_info=True)
            return 0
        removed = 0
        for name in names:
            if root_pin.is_link(name):
                # A symlink or Windows junction top-level entry: never followed, never
                # deleted.
                continue
            if root_pin.is_dir(name):
                removed += _reclaim_dir_if_idle(root_pin, name, reference, grace)
            # A top-level non-directory is left untouched: every staging site
            # (snapshot build, restore extraction, import extraction) creates a
            # ``TemporaryDirectory`` SUBTREE, so a bare file here is not a shape this
            # leaf produces and the sweep does not claim to reclaim it.
    return removed


def _reclaim_dir_if_idle(
    root_pin: PinnedDirectory, name: str, reference: float, grace: float
) -> int:
    """Reclaim one staging subtree *name* idle past *grace*.

    On a platform that supports a pinned tree walk (POSIX), the removal is the shared
    :func:`pinned_fs.remove_tree_pinned` -- the one pinned whole-tree delete every
    disposable-tree caller uses. It pins the PARENT chain one ``openat`` per component
    with ``O_NOFOLLOW`` and opens the target through it, so a component swapped for a
    symlink after the path was formed is refused by the open rather than followed. The
    target is composed from ``config_dir().resolve()`` -- the data home, which the
    agent cannot swap -- joined to the LITERAL ``staging/<name>``, NOT
    ``(<root>).resolve() / name`` (that would pre-follow a staging leaf swapped for a
    symlink to the data home and hand the governance tree to the removal).

    Where the platform cannot pin a tree walk (Windows, no ``dir_fd`` ``openat``), the
    removal is a by-name :func:`platform_compat.rmtree_force` of the child. That is
    safe here because *root_pin* -- held by the caller for the whole sweep via
    :func:`platform_compat.pin_directory`, a handle opened WITHOUT
    ``FILE_SHARE_DELETE`` -- makes the staging root and every ancestor refuse a rename
    or delete while it lives, so the root path cannot be swapped out from under the
    by-name removal; the child itself was confirmed a real directory (not a junction)
    through that same pin before this is called.

    Idle is the tree's newest mtime (a transfer writing through an open descriptor
    never touches the top directory's mtime, but every write refreshes the file's own),
    read with the shared :func:`work_root._tree_newest_mtime`. On the pinned-walk path
    it is re-confirmed inside the ``approve`` callback against the scanned descriptors,
    so the removal commits only while the tree is still idle.
    """
    child = config_dir() / STAGING_DIRNAME / name
    try:
        idle = reference - _tree_newest_mtime(child, fallback=reference)
    except OSError:
        return 0
    if idle < grace:
        return 0

    if not pinned_fs.supports_pinned_tree_walk():
        # Windows: the caller's root pin blocks a root/ancestor swap, so a by-name
        # recursive remove of this already-confirmed child directory cannot escape the
        # pinned root. ``rmtree_force`` is the shared Windows-safe recursive delete.
        if platform_compat.rmtree_force(child):
            return 1
        logger.warning("staging: could not fully remove %r; will retry", name)
        return 0

    def _still_idle(root_fd: int, tree: object) -> str | None:
        # Re-judged against the pinned descriptor so the removal commits only while the
        # tree is still idle. Fail-safe: an unreadable entry reads as active and keeps
        # the tree. ``os.stat(dir_fd=..., follow_symlinks=False)`` throughout, so no
        # path is re-resolved and a swap cannot substitute an old tree for a live one.
        newest = 0.0
        stack = [(root_fd, False)]
        try:
            newest = os.fstat(root_fd).st_mtime
            while stack:
                fd, owned = stack.pop()
                try:
                    with os.scandir(fd) as entries:
                        listing = list(entries)
                    for entry in listing:
                        info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                        if info.st_mtime > newest:
                            newest = info.st_mtime
                        if stat.S_ISDIR(info.st_mode):
                            sub = os.open(
                                entry.name,
                                os.O_RDONLY
                                | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_NOFOLLOW", 0),
                                dir_fd=fd,
                            )
                            stack.append((sub, True))
                finally:
                    if owned:
                        os.close(fd)
        except OSError:
            return "staging child became unreadable"
        finally:
            # Close every descriptor this walk still OWNS and has not yet popped --
            # ``root_fd`` is the caller's and is excluded by its ``owned=False`` marker.
            # Without this, an ``os.stat`` / ``os.open`` raising mid-loop leaks the
            # child descriptors already queued in ``stack``; over enough races that
            # exhausts the gateway's descriptors (EMFILE).
            for fd, owned in stack:
                if owned:
                    with contextlib.suppress(OSError):
                        os.close(fd)
        return None if (reference - newest) >= grace else "staging child is active again"

    target = str(config_dir().resolve() / STAGING_DIRNAME / name)
    try:
        outcome = pinned_fs.remove_tree_pinned(target, what="staging child", approve=_still_idle)
    except pinned_fs.PinnedPathRefusal:
        # The swap the pinned parent walk exists to catch (a staging leaf, or the name,
        # turned into a link), or an unopenable target: keep the tree and retry.
        logger.warning("staging: refusing to remove %r; will retry", name)
        return 0
    if outcome.removed:
        return 1
    logger.warning("staging: could not fully remove %r (%s); will retry", name, outcome.reason)
    return 0
