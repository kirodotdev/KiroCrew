"""Single-writer guard for a ``KIROCREW_HOME``.

Two ``kirocrew gateway`` processes bound to the same home each open the same
``sessions/*.jsonl`` as ``ConversationLog`` writers. The steady-save fast path
assumes a single writer per file, so the stale process's shutdown flush rolls
back newer on-disk content -- the dual-writer clobber that lost transcripts on
2026-06-23.

This module enforces the single-writer invariant at the source: the gateway
acquires an exclusive advisory ``flock`` on ``<home>/gateway.lock`` at startup
and holds it for the process lifetime. A second gateway on the same home is
refused with a message naming the incumbent pid.

``flock`` is the right primitive precisely because the kernel releases it when
the holding process dies (clean exit, crash, or ``kill -9``). A crashed gateway
therefore never wedges the home, and stale-lock reclaim is automatic -- there is
no fragile pid-liveness check. The pid written into the file is purely
informational, used only to name the holder in the refusal message.

Isolated homes (``--test-mode``/``--seed`` with a distinct ``KIROCREW_HOME``)
resolve to a different lock file and are unaffected.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

LOCK_FILENAME = "gateway.lock"


class GatewayLockError(RuntimeError):
    """Raised when another live gateway already owns this ``KIROCREW_HOME``."""

    def __init__(self, home: Path, holder_pid: int | None) -> None:
        self.home = home
        self.holder_pid = holder_pid
        if holder_pid is not None:
            detail = f"another gateway (pid {holder_pid}) already owns {home}"
        else:
            detail = f"another gateway already owns {home}"
        super().__init__(f"{detail}; stop it first or set KIROCREW_HOME to an isolated directory")


class GatewayLock:
    """Process-lifetime exclusive lock on a single ``KIROCREW_HOME``.

    Usable as a context manager or via explicit ``acquire()`` / ``release()``.
    The lock is advisory (``flock``) and scoped to the lock file's inode, so it
    works across bind mounts (e.g. a jailed gateway) but not across hosts/NFS --
    matching the single-host scope of ``KIROCREW_HOME``.
    """

    def __init__(self, home: Path) -> None:
        self._home = home
        self._path = home / LOCK_FILENAME
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> "GatewayLock":
        """Take the exclusive lock or raise ``GatewayLockError``.

        Fail-closed: any inability to take the lock refuses startup rather than
        proceeding as a second writer.
        """
        self._home.mkdir(parents=True, exist_ok=True)
        # O_RDWR | O_CREAT without truncation: a failed acquire must leave the
        # incumbent holder's pid intact so we can name it in the error.
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        # platform_compat.try_acquire_lock: fcntl.flock LOCK_EX|LOCK_NB on
        # POSIX; msvcrt.locking LK_NBLCK on Windows. Returns True iff acquired.
        # Both kernel primitives release automatically on process death, so a
        # crashed gateway never wedges the home — the automatic stale-lock
        # reclaim the docstring above depends on works uniformly on both
        # platforms.
        if not platform_compat.try_acquire_lock(fd, exclusive=True):
            # Held by a live process -- the kernel would have released a dead
            # holder's lock. Read its pid for the message, then refuse.
            holder = _read_pid(fd)
            os.close(fd)
            raise GatewayLockError(self._home, holder)

        # We hold the lock. Any prior holder is gone (its flock was auto-released
        # on death), so reclaim the file by stamping our pid over the stale one.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            # The lock itself is held (the invariant we care about); a failure to
            # record the pid only degrades the diagnostic message. Keep the lock.
            logger.warning("acquired gateway lock on %s but could not record pid", self._home)

        self._fd = fd
        logger.info("acquired gateway singleton lock on %s (pid %d)", self._home, os.getpid())
        return self

    def release(self) -> None:
        """Release the lock if held. Idempotent."""
        if self._fd is None:
            return
        platform_compat.release_lock(self._fd)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def __enter__(self) -> "GatewayLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _read_pid(fd: int) -> int | None:
    """Best-effort read of the holder pid recorded in the lock file."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode(errors="replace").strip()
    except OSError:
        return None
    try:
        return int(raw) if raw else None
    except ValueError:
        return None
