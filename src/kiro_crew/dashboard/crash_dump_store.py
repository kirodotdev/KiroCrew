"""Crash-dump store — dedicated file routing for loop-stall watchdog dumps.

The existing loop_watchdog.py captures thread stacks on
event-loop wedge via faulthandler.  However, those dumps land in raw stderr
(interleaved with all other output in journal/terminal) and are effectively
undiscoverable.

This module provides:

1. A DEDICATED crash-dump file opened at gateway startup that faulthandler writes
   to directly (faulthandler needs a stable fd for process lifetime).
2. Rotation: keeps last N dumps, removes oldest on startup.
3. Newest-dump detection for doctor/startup surfacing.

Dump directory: ``<data home>/logs/crash-dumps/`` (data home = ``config_dir()``,
i.e. ``~/.kiro/crew`` or ``$KIROCREW_HOME``)
Filename pattern: ``loopstall-<ISO timestamp>.txt``

**fd lifetime guarantee (issue #1571):**

``faulthandler.dump_traceback_later`` captures a raw C file descriptor at arm
time and writes to it on its own C thread when the timer fires.  If the fd is
invalidated (closed, reassigned, or GC'd) between arm and fire, the dump writes
to nothing — or worse, to a recycled fd — and the crash file contains only the
header written at open time.

To prevent this, :func:`open_dump_file` obtains the fd via :func:`os.open`
(lowest-level, no Python buffering layer that could close/dup the fd behind our
back), wraps it in a *non-closing* Python file object for the header write, and
returns a :class:`DumpFile` that exposes ``.fileno()`` (what faulthandler needs)
while guaranteeing the underlying fd is never closed until the process exits.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_DEFAULT_MAX_DUMPS = 10
_DUMP_DIR_NAME = "crash-dumps"
DUMP_PREFIX = "loopstall-"
DUMP_SUFFIX = ".txt"

# Module-level reference to the open dump file — kept alive for process lifetime
# because faulthandler requires the fd to remain valid.
_active_dump_file: "DumpFile | None" = None


class DumpFile:
    """Thin wrapper around a raw OS file descriptor for faulthandler.

    faulthandler's C code calls ``fileno()`` on the file object we pass it and
    then uses that integer fd for all subsequent writes.  A regular Python
    ``open()`` returns a buffered text wrapper whose ``close()`` invalidates the
    fd — and the GC, a stray ``with`` block, or even internal ``io`` layer
    reshuffling can trigger that ``close()`` unexpectedly.

    This class:
    * Holds the fd obtained from :func:`os.open` directly.
    * Exposes ``fileno()`` so faulthandler can extract the fd.
    * Exposes ``write()`` and ``flush()`` so :func:`_default_dump` (which calls
      ``faulthandler.dump_traceback(file=...)`` ) and the header write work.
    * Never closes the fd (the OS reclaims it on process exit).
    """

    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self._path = path

    def fileno(self) -> int:
        return self._fd

    @property
    def path(self) -> Path:
        return self._path

    @property
    def closed(self) -> bool:
        """Return True only if the fd has been explicitly closed (never, in normal use)."""
        try:
            os.fstat(self._fd)
            return False
        except OSError:
            return True

    def write(self, data: str) -> int:
        """Write a string to the fd (UTF-8 encoded, unbuffered)."""
        encoded = data.encode("utf-8")
        return os.write(self._fd, encoded)

    def flush(self) -> None:
        """Flush the fd to disk (fsync is too aggressive; fdatasync where available)."""
        # os.write is unbuffered at the Python level; the kernel buffer is
        # flushed on its own schedule.  An explicit fsync here would hurt
        # latency on every beat() for no diagnostic gain — the dump content
        # that matters is written by faulthandler's C thread moments before
        # _exit(), and the kernel flushes dirty pages on exit.  No-op by design.
        pass

    def close(self) -> None:
        """Intentional no-op.  The fd lives until process exit.

        This exists so code that expects a file-like interface (e.g. a
        ``finally: f.close()`` in tests) does not raise AttributeError.
        The fd is *not* closed — faulthandler's C timer may fire at any moment.
        """
        pass

    if sys.platform == "win32":
        @property
        def name(self) -> str:
            """Provide the file path as ``name`` for diagnostics."""
            return str(self._path)
    else:
        @property
        def name(self) -> str:
            return str(self._path)


def get_dumps_dir() -> Path:
    """Resolve the crash-dumps directory under the data home's ``logs/``.

    ``config_dir()`` resolves to ``~/.kiro/crew`` (or ``$KIROCREW_HOME`` when
    set), so dumps land in ``<data home>/logs/crash-dumps/``.
    """
    d = config_dir() / "logs" / _DUMP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list_dumps(dumps_dir: Path | None = None) -> list[Path]:
    """Return existing dump files sorted oldest-first."""
    d = dumps_dir or get_dumps_dir()
    if not d.is_dir():
        return []
    dumps = sorted(
        (f for f in d.iterdir() if f.name.startswith(DUMP_PREFIX) and f.suffix == DUMP_SUFFIX),
        key=lambda p: p.stat().st_mtime,
    )
    return dumps


def rotate_dumps(max_dumps: int = _DEFAULT_MAX_DUMPS, dumps_dir: Path | None = None) -> int:
    """Remove oldest dumps if count exceeds max_dumps.  Returns number removed."""
    dumps = _list_dumps(dumps_dir)
    removed = 0
    # Keep max_dumps - 1 so there's room for the new one we're about to create
    while len(dumps) > max_dumps - 1:
        oldest = dumps.pop(0)
        try:
            oldest.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def open_dump_file(dumps_dir: Path | None = None) -> DumpFile:
    """Create and open a new dump file for this gateway session.

    The returned :class:`DumpFile` wraps a raw OS fd obtained via :func:`os.open`.
    That fd is never closed by Python — it lives until the process exits — so
    ``faulthandler.dump_traceback_later`` can capture it at arm time and rely on
    it remaining valid when the timer fires seconds (or minutes) later.

    Returns the :class:`DumpFile` (caller stores it to prevent GC of the wrapper,
    though the fd itself is OS-level and not GC'd).
    """
    global _active_dump_file  # noqa: PLW0603
    d = dumps_dir or get_dumps_dir()
    d.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"{DUMP_PREFIX}{ts}{DUMP_SUFFIX}"

    # Use os.open() for a raw fd that is never wrapped in a closable Python
    # buffered layer.  O_WRONLY|O_CREAT|O_TRUNC mirrors open("w") semantics.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if sys.platform == "win32":
        flags |= os.O_NOINHERIT
    else:
        flags |= os.O_CLOEXEC
    fd = os.open(str(path), flags, 0o644)

    f = DumpFile(fd, path)
    # Write a header so the file is identifiable even before a dump fires
    f.write(f"# KiroCrew loop-stall crash dump — opened {ts}\n")
    f.write(f"# PID: {os.getpid()}\n")
    f.write("# If thread stacks appear below, the event loop wedged and faulthandler fired.\n")
    f.write("\n")
    _active_dump_file = f
    return f


def newest_dump(dumps_dir: Path | None = None) -> Path | None:
    """Return the most recent dump file, or None if no dumps exist."""
    dumps = _list_dumps(dumps_dir)
    return dumps[-1] if dumps else None


def newest_dump_with_stacks(dumps_dir: Path | None = None) -> Path | None:
    """Return the newest dump that actually contains thread stacks (not just the header).

    A dump file that only has the 4-line header means the gateway exited cleanly
    without ever wedging.  We only surface dumps that have real content.
    """
    dumps = _list_dumps(dumps_dir)
    for path in reversed(dumps):
        try:
            # Header is 4 lines (3 comment lines + blank).  Real dump content
            # starts after that.
            content = path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            # If there are more than 4 lines, there's actual stack content
            if len(lines) > 4:
                return path
        except OSError:
            continue
    return None


def claim_dump_notification(dump_path: Path, dumps_dir: Path | None = None) -> bool:
    """Claim the right to notify about *dump_path*, once per dump.

    A dump stays on disk for up to a week and is re-detected on every gateway
    start, so notifying unconditionally would turn one stall into a week of
    identical alerts on every restart. The dump's own filename is the natural
    idempotency key. Returns True the first time it is claimed, False after.

    Best-effort: on any I/O failure it returns True (notify rather than go
    silent about a crash), since a duplicate alert is a much cheaper failure
    than a suppressed one.
    """
    try:
        marker = (dumps_dir or get_dumps_dir()) / ".notified"
        already = ""
        if marker.is_file():
            already = marker.read_text(encoding="utf-8", errors="replace").strip()
        if already == dump_path.name:
            return False
        marker.write_text(dump_path.name + "\n", encoding="utf-8")
        return True
    except OSError:
        logger.debug("crash-dump notification marker unavailable", exc_info=True)
        return True


def dump_age_seconds(dump_path: Path) -> float:
    """Return age of a dump file in seconds (never negative).

    ``st_mtime`` and ``time.time()`` are both derived from the wall clock, but a
    just-written file's mtime can round marginally AHEAD of an immediately
    following ``time.time()`` (sub-microsecond float jitter, or higher-resolution
    filesystem timestamps), yielding a tiny negative delta. An age is physically
    never negative, so clamp to 0.0 — otherwise callers comparing/formatting the
    age see a nonsensical negative right after a dump is created.
    """
    return max(0.0, time.time() - dump_path.stat().st_mtime)


def dump_first_stack_lines(dump_path: Path, max_lines: int = 5) -> list[str]:
    """Extract the first N lines of actual stack content from a dump file."""
    try:
        lines = dump_path.read_text(encoding="utf-8", errors="replace").splitlines()
        # Skip the 4-line header
        stack_lines = [ln for ln in lines[4:] if ln.strip()]
        return stack_lines[:max_lines]
    except OSError:
        return []


def dump_replay_lines(
    dump_path: Path, *, max_lines: int = 120, max_bytes: int = 8192
) -> tuple[list[str], bool]:
    """Read dump stack content for journal replay, respecting size caps.

    Returns (lines, truncated) — up to *max_lines* non-empty stack lines
    totalling at most *max_bytes* of text.  *truncated* is True when the
    dump exceeded either limit.
    """
    try:
        content = dump_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    all_lines = content.splitlines()
    # Skip the 4-line header
    stack_lines = [ln for ln in all_lines[4:] if ln.strip()]
    result: list[str] = []
    total = 0
    for ln in stack_lines:
        if len(result) >= max_lines or total + len(ln) > max_bytes:
            return result, True
        result.append(ln)
        total += len(ln)
    return result, False


def get_active_dump_file() -> DumpFile | None:
    """Return the currently active dump file (for passing to faulthandler)."""
    return _active_dump_file
