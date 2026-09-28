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

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (
    KIND_SESSIONS,
    KIND_SNAPSHOT,
)

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

#: Why the snapshot backup is unavailable where its payload cannot be held from
#: creation. Quoted verbatim to the owner by :func:`kind_unavailable_reason`, so the
#: answer they get before pressing the button is the same one a failed run would give.
_NO_HELD_PAYLOAD_REASON = (
    "snapshot backups are unavailable on this platform: the snapshot payload is"
    " written by the snapshot builder before this app can hold it open, and"
    " without that hold another process running as the same user could replace"
    " the file between the build and the upload without being detected"
)

#: The mask is absent on a host that COULD otherwise run snapshots (Linux), so the
#: cause is the sandbox being off or a live agent unconfined -- a settings fix, not a
#: platform limit. Naming it separately stops a Linux operator being pointed at the
#: wrong fix ("unavailable on this platform") when re-enabling the sandbox or
#: restarting the unconfined agent is what restores the backup.
_NO_MASK_PAYLOAD_REASON = (
    "snapshot backups are paused: the staging directory is not masked by the"
    " sandbox, so another process running as the same user could replace the"
    " snapshot payload between the build and the upload without being detected."
    " This host could otherwise run snapshots -- re-enable the agent sandbox"
    " (agent.sandbox) or restart any agent currently running unconfined to restore"
    " them."
)

#: Why the sessions (archive) backup is unavailable where the upload body cannot be
#: held unrewritable from creation. The archive path builds its own body, but the
#: hold that makes those bytes safe -- a Linux memfd sealed F_SEAL_WRITE, which the
#: kernel refuses to let any writer change, through the descriptor or through a
#: /proc alias -- cannot be expressed on macOS, a BSD, Windows, or a Linux without
#: memfd_create/file seals, where a same-UID process could still rewrite the body.
#: Refused up front here, quoted verbatim, so the owner learns it before a run rather
#: than from a failed run record. Restoring those platforms with a producer-owned
#: sealed handle is tracked as a follow-up.
_NO_HOLDABLE_BODY_REASON = (
    "sessions backup is unavailable on this platform: the upload body cannot be"
    " held unrewritable from creation here. On a Linux host the body is an anonymous"
    " memfd sealed against every write -- through the descriptor and through any"
    " /proc alias -- for the whole transfer; macOS, the BSDs, Windows and a Linux"
    " without memfd/file seals have no equivalent, so a process running as the same"
    " user could replace the bytes between build and upload without being detected."
    " The backup is refused rather than upload bytes whose provenance cannot be"
    " established."
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

    Both kinds are answered here, and each for its own reason: the sessions kind
    needs descriptor-pinned traversal AND an upload body it can hold unrewritable
    from creation, and the snapshot kind needs to hold its payload from creation. A
    kind is unavailable when ITS OWN capability is missing, so one being refused here
    says nothing about the other.
    """
    if kind == KIND_SESSIONS and not _CAN_PIN_TRAVERSAL:
        return _NO_PINNING_REASON
    if kind == KIND_SESSIONS and not storage.can_hold_upload_body_from_creation():
        return _NO_HOLDABLE_BODY_REASON
    if kind == KIND_SNAPSHOT and not storage._staging_leaf_is_masked():
        # On Linux the mask being absent is a settings cause (sandbox off, or a live
        # agent unconfined), not a platform limit -- name it so the operator is told
        # the actual fix. Elsewhere the mask can never be present, so it is the
        # platform message.
        if storage.platform_compat.IS_LINUX:
            return _NO_MASK_PAYLOAD_REASON
        return _NO_HELD_PAYLOAD_REASON
    return None
