"""Shared bounding helper for append-only JSONL logs.

Several long-lived JSONL logs (the MCP stub's fallback audit log, the
subagents' slow-command log, per-member activity logs) grow one appended
record at a time from writers that must never stall an event loop. Each
needs the same bound: once the live file reaches its cap, rename it to a
single ``.1`` generation — O(1), no whole-file read — so total disk stays
at roughly twice the cap while one generation of history is kept.

This module owns only the rotation step. Each call site keeps its own
append, record shape, size cap, and error contract, because those differ
per log; what they share is exactly the rotate-by-rename.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew import platform_compat


def rotate_jsonl_at(path: Path, max_bytes: int) -> None:
    """Rotate ``path`` aside to ``<name>.1`` once it reaches ``max_bytes``.

    Call immediately before appending a record. Keeps ONE previous
    generation, replacing any older one, so total disk use stays bounded at
    about twice the cap. The live file can overshoot the cap by the few
    records written between a size check and the next rotation; callers
    accept that slack in exchange for never blocking.

    Rotation is guarded by a NON-BLOCKING try-lock on a sibling
    ``<name>.lock`` file so that two writers hitting the cap together
    cannot both rotate (the second would replace ``.1`` with the first's
    fresh live file, discarding a generation). A loser skips rotating — it
    never waits, so no caller can stall its event loop — and the next
    writer rotates. Every current caller is (or must be treated as) a
    multi-process writer, so the lock is unconditional; the cost to a
    single writer is one fd and one non-blocking syscall.

    Best-effort by contract: NEVER raises. Any failure — the lock file
    unopenable (fd exhaustion, read-only or ACL-restricted dir), a
    fresh-boot missing log, a Windows sharing violation rejecting the
    rename, an unusable path value — degrades to not rotating, so the
    caller's append still runs. Fd/disk exhaustion is a leading cause of
    the very incidents these logs diagnose, so a rotation failure must
    never cost the record; only a failure of the caller's own append may.
    """
    try:
        lock_fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            locked = platform_compat.try_acquire_lock(lock_fd, exclusive=True)
            try:
                if locked and path.stat().st_size >= max_bytes:
                    os.replace(path, path.with_name(path.name + ".1"))
            finally:
                if locked:
                    platform_compat.release_lock(lock_fd)
        finally:
            os.close(lock_fd)
    except (OSError, ValueError):
        pass
