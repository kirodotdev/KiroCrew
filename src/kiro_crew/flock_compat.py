"""Cross-platform ``flock`` shim so POSIX-only modules import on Windows.

The gateway (and everything that takes an advisory file lock — cron, gateway
singleton, session-pid, autonudge, aim_agents) runs only on macOS/Linux. But the
Windows ``kirocrew cloud`` thin-client runs ``python -m kiro_crew cloud launch``,
which imports the *whole* CLI — pulling those modules in at import time. A bare
module-level ``import fcntl`` (POSIX-only) would then crash the launcher on
Windows before it ever reaches the cloud handler.

This module provides the ``fcntl`` call surface those modules use — ``flock`` +
``LOCK_*`` (advisory file locks) and ``ioctl`` (PTY/terminal control):
- On POSIX: thin pass-throughs to the real :mod:`fcntl`.
- On Windows (no ``fcntl``): ``flock`` is a **no-op** and the constants are
  dummies; ``ioctl`` raises ``NotImplementedError`` (it's only used by the PTY
  terminal handler, which never runs on Windows). This is safe because the only
  processes that lock or drive a PTY are the gateway/cron/session machinery,
  which never run on Windows — the Windows path only provisions a *remote* Linux
  box and exits. It is NOT a general-purpose Windows implementation; it exists
  solely to keep the import graph loadable so the cloud client works.

Usage: replace ``import fcntl`` + ``fcntl.flock(...)`` / ``fcntl.ioctl(...)``
with ``from kiro_crew import flock_compat as fcntl`` (same call surface).
"""

from __future__ import annotations

from typing import Any

try:  # POSIX
    import fcntl as _fcntl

    LOCK_EX = _fcntl.LOCK_EX
    LOCK_SH = _fcntl.LOCK_SH
    LOCK_UN = _fcntl.LOCK_UN
    LOCK_NB = _fcntl.LOCK_NB

    def flock(fd: Any, operation: int) -> None:
        _fcntl.flock(fd, operation)

    def ioctl(fd: Any, request: int, arg: Any = 0, mutate_flag: bool = True) -> Any:
        return _fcntl.ioctl(fd, request, arg, mutate_flag)

    HAVE_FCNTL = True
except ImportError:  # Windows — no fcntl. Gateway/PTY code never runs here.
    LOCK_EX = 2
    LOCK_SH = 1
    LOCK_UN = 8
    LOCK_NB = 4

    def flock(fd: Any, operation: int) -> None:  # noqa: D401 - no-op on Windows
        return None

    def ioctl(fd: Any, request: int, arg: Any = 0, mutate_flag: bool = True) -> Any:
        # No PTY on the Windows cloud client; reaching here is a real bug, not an
        # import-time concern — surface it loudly rather than silently mis-behave.
        raise NotImplementedError("fcntl.ioctl is unavailable on Windows (POSIX-only)")

    HAVE_FCNTL = False
