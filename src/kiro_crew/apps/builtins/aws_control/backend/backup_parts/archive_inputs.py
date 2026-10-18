"""What both archive builders read from disk, each through a pinned descriptor.

:func:`_open_pinned_archive_fd` opens the staged archive a run fingerprints and uploads,
and proves it is a singly-named regular file of this process's own. :func:`_add_tree`
adds one session tree under its archive root, the screened entry to
``traversal._add_pinned``; the link-screen baseline declares it as a site of this module.
"""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path

from kiro_crew import platform_compat as _platform_compat
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.traversal import (
    _CAN_PIN_TRAVERSAL,
    _NO_PINNING_REASON,
    _O_DIRECTORY,
    _O_NOFOLLOW,
    _add_pinned,
)
from kiro_crew.platform_compat import is_link_or_junction


def _open_pinned_archive_fd(staging: Path, dir_fd: int, name: str) -> int:
    """Open an EXISTING ``name`` under *dir_fd* and prove it is a file of its own.

    Both backup kinds build their archive BY NAME in the pinned staging directory --
    ``snapshot_main`` / :func:`snapshot.prepare_redacted_copy` for the snapshot, the
    tar for the sessions archive -- so the earliest a run can take hold of the file is
    after it exists. The three checks (``O_NOFOLLOW`` + ``S_ISREG`` + ``st_nlink ==
    1`` + owner) are taken here so the fingerprints and the upload share one
    already-proven descriptor.

    *staging* is the directory the descriptor pins, consulted only where ``os.open``
    cannot take a ``dir_fd`` -- which on a real host means Windows. Both backup kinds
    reach this helper: the snapshot path pins the file ``snapshot_main`` wrote, and
    the sessions path pins the tar it just built in :func:`storage.pinned_staging`.
    On Windows (no ``dir_fd``) the ``staging``-relative arm opens the name with a
    reparse-point refusal and a deny-write share, so the file cannot be followed
    through a link and no other writer can open it while it is read.

    Raises ``OSError`` when the name is a symlink (``O_NOFOLLOW``) and
    ``ValueError`` when the descriptor is not a singly-named regular file owned by
    this process.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if _platform_compat.IS_POSIX:
        flags |= getattr(os, "O_NONBLOCK", 0)
    if os.open in os.supports_dir_fd:
        fd = os.open(name, flags, dir_fd=dir_fd)
    else:
        # Windows (and any platform with no ``dir_fd`` support). The payload was
        # created and closed in the pinned staging directory before this runs; the
        # reparse-point refusal + deny-write here opens that existing name without
        # following a link and without letting another writer in while it is read.
        # The pinned directory handle holds the path itself (a pinned directory can
        # be neither renamed nor deleted), so the name cannot be re-pointed between
        # the build and this open.
        fd = _platform_compat.open_file_no_reparse(
            os.path.join(str(staging), name), deny_write=True
        )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("the staged archive is not a regular file")
        if info.st_nlink != 1:
            raise ValueError("the staged archive has more than one name")
        if not _platform_compat.stat_owned_by_current_user(info):
            raise ValueError("the staged archive is owned by another user")
    except Exception:
        os.close(fd)
        raise
    return fd


def _add_tree(tar: tarfile.TarFile, root: Path, arc_prefix: str) -> int:
    """Add a directory tree to ``tar``, following no filesystem link.

    The session directories are agent-writable, so a link planted inside them
    must not become a read of whatever it points at, and an ancestor swapped
    mid-traversal must not redirect a read either.

    The descent is descriptor-pinned end to end (:func:`_add_pinned`): each level
    is a held descriptor, every child is opened relative to it, and the bytes are
    streamed from that same descriptor. No path is ever resolved twice, so there
    is no check-then-open window at any level.

    There is deliberately NO name-based fallback. A platform without ``openat``
    (``dir_fd``) and an fd-accepting ``os.scandir`` cannot make the check and the
    open one operation, so a name-based walk of these directories leaves a swap
    race open: a validated directory replaced by a junction to ``~/.aws`` between
    the check and the descent gets archived, and this archive is then uploaded
    unattended. Hardening narrows that window but nothing on such a platform
    closes it. Losing the backup there is a missing convenience; uploading
    credentials is not recoverable, so this refuses instead -- see
    :func:`run_sessions_backup`, which states the refusal before any work starts.

    Returns the number of files added.
    """
    if not _CAN_PIN_TRAVERSAL:
        # Defense in depth: run_sessions_backup refuses earlier and with a better
        # message. This is here so a future caller cannot reintroduce a
        # name-based walk of these directories by accident.
        raise RuntimeError(_NO_PINNING_REASON)
    if not root.is_dir() or is_link_or_junction(root):
        return 0
    try:
        root_fd = os.open(str(root), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError:
        return 0
    try:
        return _add_pinned(tar, root_fd, arc_prefix, depth=0)
    finally:
        os.close(root_fd)
