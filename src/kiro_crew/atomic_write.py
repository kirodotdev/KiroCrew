"""Atomic file write using unique temp filenames to avoid race conditions.

All atomic-write sites in KiroCrew should use this helper instead of
deterministic ``.tmp`` filenames, which cause ENOENT when concurrent
writers target the same file.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

from kiro_crew import platform_compat

_umask_lock = threading.Lock()
_default_mode: int | None = None


def _get_default_mode() -> int:
    """Return umask-based default file mode, cached after first call (thread-safe)."""
    global _default_mode
    if _default_mode is None:
        with _umask_lock:
            if _default_mode is None:
                u = os.umask(0)
                os.umask(u)
                _default_mode = 0o666 & ~u
    return _default_mode


def atomic_write(
    path: Path | str,
    content: str,
    *,
    fsync: bool = False,
    mode: int | None = None,
    newline: str | None = None,
) -> None:
    """Write *content* to *path* atomically via unique temp file + rename.

    Uses ``tempfile.mkstemp`` so concurrent writers never collide on the
    same temp filename.  On error the temp file is cleaned up.

    *mode* sets explicit permissions (e.g. ``0o600`` for secrets).
    ``None`` (default) applies umask-based permissions (matching ``open()``).

    *newline* is passed straight to ``open()``. The default (``None``) keeps
    the historical behavior: universal-newline translation, which rewrites
    ``\\n`` to ``\\r\\n`` on Windows. Pass ``""`` when the content must land on
    disk byte-for-byte — e.g. a document that is read back, edited and saved
    again, where translation on every save would accumulate carriage returns.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            fd = -1  # fdopen took ownership; prevent double-close
            # No-op on Windows (no POSIX permission bits / os.fchmod).
            platform_compat.fchmod_safe(
                f.fileno(), mode if mode is not None else _get_default_mode()
            )
            f.write(content)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
