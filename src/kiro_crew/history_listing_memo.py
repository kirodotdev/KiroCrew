"""A short-lived memo of whole-directory session listings.

A full listing stats every transcript. On a store of thousands of files the
sidebar, palette, folder and restore readers asking at once kept a worker thread
holding the GIL long enough to starve the event loop. The memo is shared by every
``ConversationLog`` over one directory and keyed on the directory's
``st_mtime_ns`` plus the process-wide write epoch. Every write through a log in
this process moves the epoch, so it is seen at once. Another process's write is
bounded by :data:`TTL_SECS`: an append moves nothing in the key, and a create
moves the directory mtime only when it lands on a later clock tick than the
listing.
"""

from __future__ import annotations

import threading
import time

TTL_SECS = 2.0

_memo: dict[str, tuple[tuple[int, int], float, list[dict]]] = {}
_lock = threading.Lock()


def lookup(directory: str, key: tuple[int, int]) -> list[dict] | None:
    """Fresh copies of a memoized listing for *key*, or ``None``."""
    with _lock:
        hit = _memo.get(directory)
    if hit is None or hit[0] != key or time.monotonic() - hit[1] >= TTL_SECS:
        return None
    # Callers decorate the rows they get, so each gets its own.
    return [dict(row) for row in hit[2]]


def clock() -> float:
    """The memo's clock; a listing reads it before its scan to stamp ``built_at``."""
    return time.monotonic()


def store(directory: str, key: tuple[int, int], built_at: float, rows: list[dict]) -> None:
    """Record a complete listing that began at *built_at* (``time.monotonic``)."""
    with _lock:
        _memo[directory] = (key, built_at, [dict(row) for row in rows])
