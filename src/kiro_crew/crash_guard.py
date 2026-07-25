"""Crash breadcrumb — ensures a dying gateway leaves a readable traceback.

Three layers of defense:

1. ``atexit`` handler — fires on any Python exit (but NOT on os._exit or SIGKILL).
   Writes to crash.log only when there is an active exception (sys.exc_info).
2. ``sys.excepthook`` — catches unhandled exceptions before Python prints to
   stderr and exits.  Appends to crash.log so the traceback survives even when
   stderr goes to /dev/null or a truncated pipe.
3. ``asyncio`` loop exception handler — catches "Task exception was never
   retrieved" and task-internal exceptions that asyncio would otherwise swallow
   at DEBUG level.  Writes to crash.log AND the normal logger at ERROR.

Call ``install(loop)`` once at gateway startup.  Safe to call multiple times
(idempotent via module-level flag).
"""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_INSTALLED = False
_CRASH_LOG: Path | None = None


def _crash_log_path() -> Path:
    """Resolve the crash log path — always under ``<config_dir>/logs/``."""
    from kiro_crew.config.paths import config_dir

    logs_dir = config_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "crash.log"


def _write_crash(header: str, exc_info: tuple | None = None) -> None:
    """Append a crash record to crash.log (best-effort, never raises)."""
    try:
        path = _CRASH_LOG or _crash_log_path()
        with open(path, "a") as f:
            f.write(f"\n{'=' * 72}\n")
            f.write(f"{header}\n")
            f.write(f"Time: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"PID:  {os.getpid()}\n")
            f.write(f"{'-' * 72}\n")
            if exc_info and exc_info[0] is not None:
                traceback.print_exception(*exc_info, file=f)
            else:
                f.write("(no exception info available)\n")
                # Dump current stack frames as fallback context
                f.write("\nThread stacks at exit:\n")
                faulthandler.dump_traceback(file=f)
            f.write(f"{'=' * 72}\n\n")
    except Exception:
        pass  # Never let crash logging itself crash the exit path


def _atexit_handler() -> None:
    """Fires on normal Python exit.  Writes breadcrumb only if dying with an exception."""
    exc_info = sys.exc_info()
    if exc_info[0] is not None:
        _write_crash("ATEXIT: process exiting with active exception", exc_info)


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    """Catches unhandled exceptions before Python's default stderr dump."""
    _write_crash(
        f"UNHANDLED EXCEPTION: {exc_type.__name__}: {exc_value}",
        (exc_type, exc_value, exc_tb),
    )
    # Still call the default hook so stderr gets the traceback too
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _asyncio_exception_handler(loop, context) -> None:  # noqa: no-blocking-call-on-event-loop
    """Catches exceptions swallowed by asyncio (task-never-retrieved, etc.).

    Writes to crash.log synchronously.  This is intentional: the handler fires
    during shutdown and fatal conditions where an executor Future would be
    silently dropped.  A single file-append is sub-millisecond on local disk
    and reliability of crash capture outweighs event-loop latency here.
    """
    exception = context.get("exception")
    message = context.get("message", "Unhandled asyncio exception")

    # Log to the normal logger at ERROR (default asyncio only logs at DEBUG)
    if exception:
        logger.error(
            "asyncio unhandled: %s — %s: %s",
            message,
            type(exception).__name__,
            exception,
            exc_info=exception,
        )
        header = f"ASYNCIO UNHANDLED: {message}"
        exc_tuple = (type(exception), exception, exception.__traceback__)
        _write_crash(header, exc_tuple)
    else:
        logger.error("asyncio unhandled: %s (no exception object)", message)
        _write_crash(f"ASYNCIO UNHANDLED (no exc): {message}")


def install(loop=None) -> None:
    """Install all crash-guard layers.  Idempotent.

    Args:
        loop: asyncio event loop.  If None, only atexit + excepthook are installed;
              the loop handler must be installed later via ``install_loop_handler(loop)``.
    """
    global _INSTALLED, _CRASH_LOG

    if _INSTALLED:
        return
    _INSTALLED = True

    _CRASH_LOG = _crash_log_path()

    # Layer 1: atexit
    atexit.register(_atexit_handler)

    # Layer 2: sys.excepthook
    sys.excepthook = _excepthook

    # Layer 3: faulthandler (C-level crashes → stderr → systemd journal)
    faulthandler.enable()

    # Layer 4: asyncio loop exception handler (if loop provided)
    if loop is not None:
        install_loop_handler(loop)

    logger.debug("crash_guard installed (crash_log=%s)", _CRASH_LOG)


def install_loop_handler(loop) -> None:
    """Install the asyncio exception handler on the given loop.

    Also pins ``_CRASH_LOG`` to the *current* config dir. asyncio reports
    unretrieved task exceptions at GC time — potentially long after the owning
    process/test has torn its environment down — so resolving the path lazily
    inside ``_write_crash`` can send a record to whichever ``KIROCREW_HOME`` is
    in effect at GC time rather than the one that was running. Pinning here
    keeps every record with the instance that produced it.
    """
    global _CRASH_LOG

    try:
        _CRASH_LOG = _crash_log_path()
    except Exception:  # pragma: no cover - path resolution is best-effort
        logger.debug("crash log path resolution failed", exc_info=True)
    loop.set_exception_handler(_asyncio_exception_handler)
