"""Reading a credential file safely, for a :class:`BrowserCredentialProvider` that has one.

An edition's provider usually reads its session from a file a sign-in tool wrote
(a cookie jar, a token cache). The provider runs in the gateway, outside every
sandbox, so the file it opens is one the agent may have been able to WRITE even
where it cannot read: a planted symlink, a world-writable file someone else
replaced, or a multi-gigabyte file that stalls the gateway. These helpers refuse
all of those, the same way for every edition:

* :func:`read_source_file` opens the file without following a final symlink
  (``O_NOFOLLOW``), then checks the OPEN descriptor (``fstat``), so a swap between
  the check and the read cannot change what is read: a regular file, owned by the
  gateway's own uid, with no group or other permission bits, and at most
  *max_bytes* long. On Windows, where POSIX owner and mode bits do not exist, only
  the regular-file, not-a-link and size checks apply.
* :func:`source_fingerprint` is the cheap change detector the provider's
  ``fingerprint()`` can return: inode, size and modification time of the file
  (``lstat``, never its contents), or ``None`` when it is missing.

Every refusal raises :class:`CredentialSourceError` carrying a fixed ``code``.
The supervisor reports that code as the provider's status (a missing source is a
status, never a crash) and never the path or the contents.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from kiro_crew import platform_compat

#: Default ceiling for one credential file; a cookie jar or token cache is kilobytes.
DEFAULT_MAX_BYTES = 1024 * 1024

SOURCE_MISSING = "source_missing"
SOURCE_SYMLINK = "source_symlink"
SOURCE_NOT_REGULAR = "source_not_regular"
SOURCE_NOT_OWNED = "source_not_owned"
SOURCE_PERMISSIONS = "source_permissions"
SOURCE_TOO_LARGE = "source_too_large"
SOURCE_UNREADABLE = "source_unreadable"

CODES = frozenset(
    {
        SOURCE_MISSING,
        SOURCE_SYMLINK,
        SOURCE_NOT_REGULAR,
        SOURCE_NOT_OWNED,
        SOURCE_PERMISSIONS,
        SOURCE_TOO_LARGE,
        SOURCE_UNREADABLE,
    }
)


class CredentialSourceError(OSError):
    """A credential file that must not be read. ``code`` is one of :data:`CODES`.

    The message is the code alone: a path names the user's home, and the provider
    status it becomes is shown in Settings.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def read_source_file(path: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """The bytes of *path*, or :class:`CredentialSourceError` (see the module docstring)."""
    # O_NONBLOCK: a FIFO planted at the path would otherwise block the open until a
    # writer appears; the S_ISREG check after fstat then refuses it.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        if Path(path).is_symlink():
            raise CredentialSourceError(SOURCE_SYMLINK)
        fd = os.open(os.fspath(path), flags)
    except CredentialSourceError:
        raise
    except FileNotFoundError:
        raise CredentialSourceError(SOURCE_MISSING) from None
    except OSError as exc:
        # ELOOP is what O_NOFOLLOW answers for a final symlink (the is_symlink check
        # above is the only guard on Windows, which has no O_NOFOLLOW).
        code = SOURCE_SYMLINK if exc.errno == errno.ELOOP else SOURCE_UNREADABLE
        raise CredentialSourceError(code) from None
    try:
        info = os.fstat(fd)
        _check(info, max_bytes)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise CredentialSourceError(SOURCE_TOO_LARGE)
    return data


def _check(info: os.stat_result, max_bytes: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise CredentialSourceError(SOURCE_NOT_REGULAR)
    if info.st_size > max_bytes:
        raise CredentialSourceError(SOURCE_TOO_LARGE)
    if platform_compat.IS_WINDOWS:
        return
    if info.st_uid != platform_compat.effective_uid():
        raise CredentialSourceError(SOURCE_NOT_OWNED)
    if info.st_mode & 0o077:
        raise CredentialSourceError(SOURCE_PERMISSIONS)


def source_fingerprint(path: Path | str) -> str | None:
    """``"<inode>:<size>:<mtime_ns>"`` of *path* (not followed), or ``None`` if absent."""
    try:
        info = os.lstat(os.fspath(path))
    except OSError:
        return None
    return f"{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"


__all__ = [
    "CODES",
    "CredentialSourceError",
    "DEFAULT_MAX_BYTES",
    "read_source_file",
    "source_fingerprint",
]
