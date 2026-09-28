"""Descriptor-pinned directory traversal, and the platform capability it needs.

:func:`_add_pinned` walks a tree through held directory descriptors, so no path is
resolved twice. Its screened entry point, ``backup._add_tree``, stays in the facade
because it is a declared link-screen site. :func:`kind_unavailable_reason` turns the
capability into the one per-kind refusal every surface quotes.
"""

from __future__ import annotations

import logging
import os
import stat
import tarfile

from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import KIND_SESSIONS

logger = logging.getLogger(_FACADE_MODULE)


#: ``O_NOFOLLOW`` refuses to open a symlink at all, which is what makes the
#: descriptor-pinned add below race-free rather than merely check-then-open. It
#: does not exist on Windows, where the fallback is the ``S_ISREG`` fstat plus the
#: directory pruning: a swap is still caught the moment the descriptor is
#: inspected, it just cannot be refused at open time.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


#: ``O_NONBLOCK`` is what keeps the open itself from being a denial of service.
#: Opening a FIFO for reading BLOCKS until some writer appears, so a single named
#: pipe planted in an agent-writable session directory would hang the backup
#: thread forever -- the fstat that rejects it never gets to run. With this flag
#: the open returns immediately and ``S_ISREG`` does the rejecting. Regular files
#: ignore it, so nothing legitimate changes. Also absent on Windows, which has no
#: FIFOs to open.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


#: ``O_DIRECTORY`` makes "open this only if it is a directory" atomic with the
#: open, so a pinned descent cannot be tricked into opening a file (or, with
#: ``O_NOFOLLOW`` alongside it, a link) where a directory was expected. Absent on
#: Windows, which is one of the two reasons the fallback walk exists.
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


#: Depth ceiling for the pinned descent. One descriptor is held per level, so a
#: pathological tree could otherwise exhaust the process's fd budget. Session
#: trees are two or three deep; anything past this is not a session layout.
_MAX_TREE_DEPTH = 32


#: Whether this platform can do the pinned traversal at all. Both are needed:
#: ``dir_fd`` for ``os.open`` (the ``openat`` syscall) and an fd-accepting
#: ``os.scandir``. POSIX has both; Windows has neither.
_CAN_PIN_TRAVERSAL = (
    os.open in getattr(os, "supports_dir_fd", set())
    and os.scandir in getattr(os, "supports_fd", set())
    and _O_DIRECTORY != 0
)


#: Why the sessions backup refuses rather than degrading to a name-based walk.
#: Phrased for a human reading a failed run record, so it says what is missing and
#: that the refusal is the safe outcome rather than a bug to work around.
_NO_PINNING_REASON = (
    "sessions backup needs descriptor-pinned directory traversal (openat), which "
    "this platform does not provide. Walking these agent-writable directories by "
    "name would leave a window in which a directory swapped for a link could be "
    "archived and uploaded, so the backup is refused instead."
)


def _add_pinned(tar: tarfile.TarFile, dir_fd: int, arc_prefix: str, depth: int) -> int:
    """Archive one directory level, addressing every child RELATIVE to ``dir_fd``.

    This is what closes the ancestor-swap window that a path-based walk cannot.
    ``os.walk`` yields NAMES, and re-opening ``a/b/c.json`` re-resolves ``a`` and
    ``b`` from scratch: swapping either for a link between the check and the open
    redirects the read, and no amount of pre-checking the name helps because the
    check and the open are two separate resolutions of the same string.

    Here each level is held open as a descriptor and every child is opened with
    ``dir_fd=`` -- the kernel resolves the child against THAT descriptor, not
    against a path, so an ancestor renamed or relinked afterwards cannot change
    what is read. Combined with ``O_NOFOLLOW`` (the child itself may not be a
    link) and ``O_DIRECTORY`` (a directory child must really be a directory),
    the traversal never leaves the tree it was handed.
    """
    added = 0
    if depth > _MAX_TREE_DEPTH:
        logger.warning("aws-control backup: tree deeper than %s levels; pruned", _MAX_TREE_DEPTH)
        return added
    try:
        with os.scandir(dir_fd) as it:
            names = sorted(entry.name for entry in it)
    except OSError:
        return added
    for name in names:
        try:
            child = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=dir_fd)
        except OSError:
            # ELOOP (a link), ENOENT (gone mid-scan), EACCES, ENXIO (a FIFO with
            # no writer): not ours to archive, never a hard failure.
            continue
        try:
            st = os.fstat(child)
            if stat.S_ISDIR(st.st_mode):
                added += _add_pinned(tar, child, f"{arc_prefix}/{name}", depth + 1)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_nlink != 1:
                # A HARD link defeats every other defense here by construction:
                # it is a regular file (S_ISREG passes), it is not a symlink so
                # O_NOFOLLOW does not reject it, it carries no reparse point, and
                # it is opened relative to the pinned descriptor like any real
                # session file -- while pointing at another file's inode. So
                # `os.link("~/.aws/credentials", "<session dir>/notes.json")` in
                # an agent-writable directory would archive the credential bytes
                # and upload them. The link COUNT is what tells the two apart, and
                # it is read from the fstat of the descriptor being archived, so it
                # describes the inode actually about to be read. A genuine session
                # file has exactly one link; anything else is not ours to send.
                continue
            info = tarfile.TarInfo(name=f"{arc_prefix}/{name}")
            info.size = st.st_size
            info.mtime = int(st.st_mtime)
            info.mode = stat.S_IMODE(st.st_mode)
            info.type = tarfile.REGTYPE
            with os.fdopen(child, "rb", closefd=False) as fh:
                tar.addfile(info, fh)
            added += 1
        finally:
            os.close(child)
    return added


def kind_unavailable_reason(kind: str) -> str | None:
    """Why ``kind`` cannot run on THIS platform, or ``None`` when it can.

    The refusal itself is not new -- :func:`run_sessions_backup` has always
    raised on a platform without descriptor-pinned traversal, and that fail-close
    is correct and stays. What was missing is a way to ASK before starting: the
    kind was registered and offered identically everywhere, so on Windows the
    owner pressed a button and got a ``RuntimeError`` back as a failed run
    record. A capability question deserves an answer before the work, not an
    exception after it, so the same condition is readable up front here and the
    route layer turns it into a stated refusal.

    Returns the prose reason so every surface quotes ONE explanation. Callers
    must treat a non-``None`` result as "offer this as unavailable", not as an
    error to log.
    """
    if kind == KIND_SESSIONS and not _CAN_PIN_TRAVERSAL:
        return _NO_PINNING_REASON
    return None
