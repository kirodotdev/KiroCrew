"""Codex's 5-hour and weekly usage windows, read from the rollout logs Codex writes itself.

On every turn Codex CLI appends a ``token_count`` event whose ``rate_limits`` block holds the
two windows to the session's rollout log, ``CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``.
codex-acp's ``usage_update`` frames carry only ``used``/``size``, so these logs are the one
local source. ``CODEX_HOME`` is the gateway's own. Only files named ``rollout-*.jsonl`` under
``sessions/`` are opened and a link at that name is refused, so the adapter's own ``auth.json``
is not read.
"""

from __future__ import annotations

import json
import math
import os
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat

#: Rollout logs looked at, most recently written first; the newest one holding a block wins.
_MAX_ROLLOUTS = 5
#: Bytes read from the end of each log. A turn ends with its ``token_count`` line, so the
#: newest block sits in the tail and a long session costs no more than a short one.
_TAIL_BYTES = 1 << 20
_RATE_LIMITS_KEY = b'"rate_limits"'
#: The newest reading found and the ``sessions`` directory it came from. It answers when a
#: disposable session has deleted the only log that held a block.
_last_reading: tuple[Path, dict[str, Any]] | None = None


def _sessions_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def _newest_rollouts(sessions: Path) -> list[tuple[float, Path]]:
    """Up to ``_MAX_ROLLOUTS`` ``(mtime, path)`` pairs, most recently written first.

    Every log is considered, because a resumed session keeps appending to the log in the day
    directory it started in. A linked directory, a Windows junction included, is not entered,
    so a link loop cannot multiply the walk.
    """
    found: list[tuple[float, Path]] = []
    for root, dirs, files in os.walk(sessions):
        dirs[:] = [d for d in dirs if not platform_compat.is_link_or_junction(Path(root, d))]
        for name in files:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                path = Path(root, name)
                try:
                    found.append((path.lstat().st_mtime, path))
                except OSError:
                    continue
    found.sort(key=lambda item: item[0], reverse=True)
    return found[:_MAX_ROLLOUTS]


def _tail_lines(path: Path) -> list[bytes]:
    """The whole lines in the last ``_TAIL_BYTES`` of *path*, newest first.

    Opened non-blocking and checked to be a regular file, so a FIFO planted at a log's name
    is skipped instead of waiting for a writer.
    """
    fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return []
        start = max(0, info.st_size - _TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        lines = os.read(fd, _TAIL_BYTES).splitlines()
    finally:
        os.close(fd)
    # A tail that starts mid-file starts mid-line.
    return (lines[1:] if start else lines)[::-1]


def _number(value: object) -> int | float | None:
    """*value* when it is a finite JSON number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _window(raw: object, now: float) -> dict[str, Any] | None:
    """One normalised window; ``stale`` when its reset time has already passed."""
    if not isinstance(raw, dict):
        return None
    used = _number(raw.get("used_percent"))
    if used is None:
        return None
    resets_at = _number(raw.get("resets_at"))
    return {
        "used_percent": used,
        "resets_at": resets_at,
        "window_minutes": _number(raw.get("window_minutes")),
        "stale": resets_at is not None and resets_at <= now,
    }


def _reading(line: bytes, now: float) -> dict[str, Any] | None:
    """The windows one rollout line carries, or ``None`` when it carries none."""
    if _RATE_LIMITS_KEY not in line:
        return None
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return None
    payload = record.get("payload") if isinstance(record, dict) else None
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    five_hour = _window(limits.get("primary"), now)
    weekly = _window(limits.get("secondary"), now)
    if five_hour is None and weekly is None:
        return None
    captured_at = record.get("timestamp")
    return {
        "five_hour": five_hour,
        "weekly": weekly,
        "captured_at": captured_at if isinstance(captured_at, str) else None,
    }


def _restale(reading: dict[str, Any], now: float) -> dict[str, Any]:
    """A copy of a kept *reading* whose ``stale`` flags are judged at *now*."""
    copy = dict(reading)
    for key in ("five_hour", "weekly"):
        window = reading.get(key)
        if isinstance(window, dict):
            resets_at = window.get("resets_at")
            copy[key] = {**window, "stale": resets_at is not None and resets_at <= now}
    return copy


def _captured_ts(reading: dict[str, Any]) -> float | None:
    """The epoch seconds of *reading*'s ``captured_at``, or ``None`` when it has none."""
    captured = reading.get("captured_at")
    if not isinstance(captured, str):
        return None
    try:
        return datetime.fromisoformat(captured.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _newer(kept: dict[str, Any], found: dict[str, Any]) -> bool:
    """Whether the *kept* reading was recorded after the *found* one.

    The record timestamps decide when both have one, because a log's modification time
    also moves for lines that carry no block. Otherwise the log modification times decide.
    """
    kept_ts, found_ts = _captured_ts(kept), _captured_ts(found)
    if kept_ts is not None and found_ts is not None:
        return kept_ts > found_ts
    return bool(kept.get("source_mtime", 0) > found.get("source_mtime", 0))


def read_codex_rate_limits() -> dict[str, Any] | None:
    """The newest windows Codex recorded locally, or ``None`` when there is no reading.

    ``five_hour`` is the block's ``primary`` window and ``weekly`` its ``secondary``; either
    is ``None`` when the block lacks it. ``source_mtime`` is the log's modification time.
    A disposable Codex session deletes its rollout at teardown, so the last reading found for
    the same store is held in memory only. It is returned when none of the newest logs holds
    a block that was recorded after it.
    Blocking file I/O: call it off the event loop.
    """
    global _last_reading
    now = time.time()
    sessions = _sessions_dir()
    kept = _last_reading[1] if _last_reading is not None and _last_reading[0] == sessions else None
    for mtime, path in _newest_rollouts(sessions):
        try:
            lines = _tail_lines(path)
        except OSError:
            continue
        for line in lines:
            reading = _reading(line, now)
            if reading is not None:
                reading["source_mtime"] = mtime
                if kept is not None and _newer(kept, reading):
                    # This log's newest block is older than the kept reading; an older
                    # log can still hold a newer block.
                    break
                _last_reading = (sessions, dict(reading))
                return reading
    if kept is not None:
        return _restale(kept, now)
    return None
