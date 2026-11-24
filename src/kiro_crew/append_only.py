"""Best-effort append-only FS attribute (the ``chattr +a`` analogue) for the
gateway-owned security logs.

Setting the append-only flag -- ``FS_APPEND_FL`` on Linux, ``SF_APPEND`` on
macOS -- lets ``O_APPEND`` writes keep working while truncate, in-place
overwrite, rename and unlink are refused by the kernel for any process without
``CAP_LINUX_IMMUTABLE`` (on Linux) / root (on macOS). That is exactly the fence
the security-event log (``security_events.jsonl``) and the shell audit log
(``audit.log``) need: an in-sandbox MCP server or the bundled audit hook must
still be able to APPEND a record, but the sandboxed agent's own shell -- which
shares the namespace with those appenders -- must not be able to rewrite or
erase the audit trail.

The flag is set by the GATEWAY, which owns the data home. Setting or clearing it
is itself a privileged operation (``CAP_LINUX_IMMUTABLE`` on Linux, which in
practice means root), so on an unprivileged gateway, a filesystem that does not
support the attribute (tmpfs, many network filesystems, non-ext/xfs/btrfs), or a
non-POSIX platform, these helpers DEGRADE GRACEFULLY: they log a warning at most
once per path and return ``False`` without raising. The log keeps working, just
without the kernel guard. The durable fix for that case is to route the
in-sandbox appends through the gateway so the file is never opened inside the
sandbox at all (see ``AGENTS.md``); this attribute is defense-in-depth under
that.

Linux rotation note: a file with ``FS_APPEND_FL`` set cannot be renamed, so the
gateway's rename-based rotation must :func:`clear_append_only` the live file
before the rename (it runs unsandboxed, so it has the capability when the set
did). The next append re-creates the live file and re-applies the flag.
"""

from __future__ import annotations

import logging
import os
import struct
from pathlib import Path

from kiro_crew.platform_compat import IS_LINUX, IS_MACOS

logger = logging.getLogger(__name__)

# <uapi/linux/fs.h>: the append-only inode flag read/written by the
# FS_IOC_GETFLAGS / FS_IOC_SETFLAGS ioctls, whose payload is a C ``long``.
_FS_APPEND_FL = 0x00000020

# Build the ioctl request numbers for THIS platform's ``long`` width rather than
# hard-coding the 64-bit values, so a 32-bit host gets the right size field (and
# anything unexpected simply fails the ioctl and degrades). Mirrors the kernel's
# _IOR('f', 1, long) / _IOW('f', 2, long).
_IOC_WRITE = 1
_IOC_READ = 2
_LONG_SIZE = struct.calcsize("l")


def _ioc(direction: int, type_char: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(type_char) << 8) | nr


_FS_IOC_GETFLAGS = _ioc(_IOC_READ, "f", 1, _LONG_SIZE)
_FS_IOC_SETFLAGS = _ioc(_IOC_WRITE, "f", 2, _LONG_SIZE)

# One warning per (path, action) so a cleanup loop or a per-append re-assert does
# not repeat the same line hundreds of times when the flag cannot be set.
_warned: set[str] = set()


def _warn_once(path: Path, action: str, exc: Exception) -> None:
    key = f"{action}:{path}"
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(
        "SEL append-only: could not %s the kernel append-only flag on %s "
        "(%s); the log keeps working without it. On Linux this needs "
        "CAP_LINUX_IMMUTABLE and a supporting filesystem; this is logged once "
        "per path.",
        action,
        path,
        type(exc).__name__,
    )


def _linux_set_flag(path: Path, *, enable: bool) -> None:
    import fcntl

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        buf = bytearray(_LONG_SIZE)
        fcntl.ioctl(fd, _FS_IOC_GETFLAGS, buf)
        (flags,) = struct.unpack("l", buf)
        new_flags = flags | _FS_APPEND_FL if enable else flags & ~_FS_APPEND_FL
        if new_flags == flags:
            return
        fcntl.ioctl(fd, _FS_IOC_SETFLAGS, struct.pack("l", new_flags))
    finally:
        os.close(fd)


def _macos_set_flag(path: Path, *, enable: bool) -> None:
    import stat

    sf_append = getattr(stat, "SF_APPEND", 0x00040000)
    st = os.stat(path)
    current = getattr(st, "st_flags", 0)
    new_flags = current | sf_append if enable else current & ~sf_append
    if new_flags == current:
        return
    os.chflags(path, new_flags)  # type: ignore[attr-defined]


def set_append_only(path: str | os.PathLike[str], *, warn: bool = True) -> bool:
    """Set the append-only flag on *path*. Best effort; never raises.

    Returns ``True`` when the flag is set (or was already set), ``False`` when it
    could not be applied (unprivileged, unsupported filesystem, or a non-POSIX
    platform). A failure logs a one-time warning unless *warn* is ``False`` --
    which callers on a hot, best-effort path (the shell-audit-log sweep, where an
    unprivileged gateway is the ordinary case) pass to stay quiet.
    """
    p = Path(path)
    try:
        if IS_LINUX:
            _linux_set_flag(p, enable=True)
            return True
        if IS_MACOS:
            _macos_set_flag(p, enable=True)
            return True
        return False
    except OSError as exc:
        if warn:
            _warn_once(p, "set", exc)
        return False


def clear_append_only(path: str | os.PathLike[str], *, warn: bool = True) -> bool:
    """Clear the append-only flag on *path* so the gateway can rename/rotate it.

    Best effort; never raises. Returns ``True`` when the flag is cleared (or was
    not set / the platform has no such flag, so a rename is already allowed),
    ``False`` only when the clear was attempted and refused (logged once unless
    *warn* is ``False``).
    """
    p = Path(path)
    try:
        if IS_LINUX:
            _linux_set_flag(p, enable=False)
            return True
        if IS_MACOS:
            _macos_set_flag(p, enable=False)
            return True
        return True
    except OSError as exc:
        if warn:
            _warn_once(p, "clear", exc)
        return False
