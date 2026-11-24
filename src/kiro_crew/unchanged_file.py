"""Tell a writer that the file it is about to replace already holds the same bytes.

A temp-and-rename write puts a NEW inode under the name even when the content is
the same. The Linux sandbox pins some of those names for each sandboxed spawn (a
hook, a session) and refuses to start when the object under a pinned name changed
between the pass that saw it and the pin, so a rewrite that changes nothing still
fails every spawn that starts in that second. A writer that asks this first, and
writes only on ``False``, leaves the inode alone when nothing would change.

Stdlib only at import time, so near-leaf modules (``agent_state``) can import it;
the owner check reaches ``platform_compat`` lazily, at call time.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def already_holds(path: Path | str, payload: bytes, *, mode: int | None = None) -> bool:
    """Whether *path* is this user's regular, owner-only file holding *payload*.

    ``True`` only when skipping the write leaves the file exactly as the write
    would: one link, owned by this process's effective user, the permissions the
    writer sets (*mode* when given, else any mode with no group or other bits,
    which a writer that keeps the owner bits reproduces), and the same bytes.
    Anything else, a read error included, is ``False`` so the caller writes as it
    always did.

    Always ``False`` on Windows: there the writers also lock the file's ACL down,
    which a mode check cannot confirm, there is no uid to compare, and the pin
    this avoids is a Linux mount.
    """
    if os.name == "nt":
        return False
    try:
        info = os.lstat(path)
        if not _same_shape(info, len(payload), mode):
            return False
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return False
            if not _same_shape(opened, len(payload), mode):
                return False
            data = b""
            while len(data) <= len(payload):
                chunk = os.read(fd, len(payload) + 1 - len(data))
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)
    except OSError:
        return False
    return data == payload


def _same_shape(info: os.stat_result, size: int, mode: int | None) -> bool:
    """Regular, single-link, *size* bytes, ours, and the permissions the writer would set."""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
        return False
    # The write would leave a file this process owns, so another owner's is replaced.
    # No uid (Windows) is never a match.
    from kiro_crew.platform_compat import effective_uid  # noqa: PLC0415  near-leaf import

    uid = effective_uid()
    if uid is None or info.st_uid != uid:
        return False
    perms = stat.S_IMODE(info.st_mode)
    if mode is not None:
        return perms == mode
    return perms & 0o077 == 0
