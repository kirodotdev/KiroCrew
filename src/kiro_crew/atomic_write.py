"""Atomic file write using unique temp filenames to avoid race conditions.

All atomic-write sites in KiroCrew should use this helper instead of
deterministic ``.tmp`` filenames, which cause ENOENT when concurrent
writers target the same file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

_umask_lock = threading.Lock()
_default_mode: int | None = None

# Bounded retry budget for the Windows rename window. ``os.replace`` on Windows
# raises ``PermissionError`` when ANY other handle is open on either path, and a
# just-created temp file is exactly what a Search-indexer or AV scanner reaches
# for, so the rename can fail with WinError 5 / 32 / 33 while nothing is wrong.
# POSIX imposes no such restriction, so the retry is Windows-only: a
# PermissionError there is a genuine permission fault and must surface at once
# rather than after a second of sleeping.
#
# Shape mirrors the create-race retry in ``dashboard/token_secret.py``. A
# scanner hold is short but not instantaneous, so this trades ~0.45s of
# worst-case added latency on a doomed write against surviving the common
# transient. The numbers are a heuristic, not a measured hold-time distribution.
_REPLACE_MAX_ATTEMPTS = 10
_REPLACE_BACKOFF_SECONDS = 0.05


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


def _on_event_loop() -> bool:
    """Whether this thread is currently running an asyncio event loop.

    Mirrors the probe guarding ``CronService``'s store lock: a worker started by
    ``asyncio.to_thread`` or ``run_in_executor`` has no running loop of its own,
    so a caller that offloads its write keeps the retry while the loop thread
    itself never sleeps.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def replace_with_retry(src: Path | str, dst: Path | str) -> None:
    """``os.replace(src, dst)``, retrying the Windows sharing-violation window.

    Atomic replacement is the last step of every tmp-file-plus-rename writer. On
    Windows that rename fails with ``PermissionError`` if any other handle is
    open on either path. An indexer or an AV scanner touching the freshly
    written temp file is enough, so a correct atomic write can still lose its
    payload for reasons unrelated to the caller. Retry a bounded number of times
    so the transient resolves instead of propagating.

    On POSIX this is a plain ``os.replace``: the OS permits replacing an open
    file, so a ``PermissionError`` means the caller genuinely cannot write there
    and is re-raised immediately rather than slept over.

    The retry sleeps, so it is gated on there being no running event loop in
    this thread. A caller reached from the gateway loop gets the plain
    ``os.replace`` semantics it had before this retry existed: the
    ``PermissionError`` propagates on the first attempt rather than pausing the
    single loop for the whole budget (``no-blocking-call-on-event-loop``). This
    is a property of this function, so it holds no matter how many sync helpers
    sit between a coroutine and this call. Callers wanting the retry on a
    loop-driven path offload the write (``asyncio.to_thread`` /
    ``run_in_executor``), as ``AutoNudgeService`` already does; the worker has no
    loop of its own, so the retry applies there.

    The final attempt sits OUTSIDE the retry loop on purpose. With it inside,
    a budget of 0 would skip the body entirely and return having renamed
    nothing, which every caller reads as success: a silently lost write. Out
    here, any budget of 1 or less simply degrades to a plain ``os.replace``.
    """
    for attempt in range(_REPLACE_MAX_ATTEMPTS - 1):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            if not platform_compat.IS_WINDOWS:
                raise
            if _on_event_loop():
                logger.debug(
                    "atomic rename contended at %s on the event loop; "
                    "re-raising instead of sleeping (offload the write to retry)",
                    dst,
                )
                raise
            logger.debug(
                "atomic rename contended at %s; retrying (attempt %d/%d)",
                dst,
                attempt + 1,
                _REPLACE_MAX_ATTEMPTS,
            )
            time.sleep(_REPLACE_BACKOFF_SECONDS)
    os.replace(str(src), str(dst))


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

    *newline* is passed straight to ``open()``. The default (``None``) applies
    universal-newline translation, which rewrites ``\\n`` to ``\\r\\n`` on
    Windows. Pass ``""`` when the content must land on disk byte-for-byte —
    e.g. a document that is read back, edited and saved again, where
    translation on every save would accumulate carriage returns.
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
        replace_with_retry(tmp, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
