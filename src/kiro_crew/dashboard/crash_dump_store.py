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
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_DEFAULT_MAX_DUMPS = 10
_DUMP_DIR_NAME = "crash-dumps"
DUMP_PREFIX = "loopstall-"
DUMP_SUFFIX = ".txt"

# Module-level reference to the open dump file — kept alive for process lifetime
# because faulthandler requires the fd to remain valid.
_active_dump_file: IO[str] | None = None


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


def open_dump_file(dumps_dir: Path | None = None) -> IO[str]:
    """Create and open a new dump file for this gateway session.

    The returned file object MUST be kept alive for the entire process lifetime
    because faulthandler.dump_traceback_later holds a reference to the fd.

    Returns the open file object (caller stores it to prevent GC).
    """
    global _active_dump_file  # noqa: PLW0603
    d = dumps_dir or get_dumps_dir()
    d.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"{DUMP_PREFIX}{ts}{DUMP_SUFFIX}"
    f = open(path, "w", encoding="utf-8")  # noqa: SIM115 — intentionally kept open for process lifetime
    # Write a header so the file is identifiable even before a dump fires
    f.write(f"# KiroCrew loop-stall crash dump — opened {ts}\n")
    f.write(f"# PID: {os.getpid()}\n")
    f.write("# If thread stacks appear below, the event loop wedged and faulthandler fired.\n")
    f.write("\n")
    f.flush()
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


def get_active_dump_file() -> IO[str] | None:
    """Return the currently active dump file (for passing to faulthandler)."""
    return _active_dump_file
