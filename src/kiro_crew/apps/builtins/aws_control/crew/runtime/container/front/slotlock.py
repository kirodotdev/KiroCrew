"""Per-conversation serialization.

The backend returns 409 for a second concurrent turn on one slot id. That is a
correct backend behaviour, not something the caller should ever see: a customer
who sends two messages quickly to the same conversation must have the second one
queued, not rejected. So the front process serializes turns *per slot id*.

A global lock would be wrong in the other direction: turns for DIFFERENT
conversations must run concurrently, or one slow crew blocks every caller.

The lock for a slot must be held for the whole turn, including the entire SSE
stream, because the slot stays busy on the backend until the stream ends. The
async context manager returned here is therefore entered inside the streaming
generator, not merely around the request handler.

Slots with no id (the backend mints one per request, so there is no shared
conversation to contend on) are not serialized at all.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class SlotSerializer:
    """One asyncio lock per live slot id, created on demand and reaped when idle.

    Locks are reference-counted so the map does not grow without bound over the
    life of the process: the entry for a slot is removed once no turn holds or
    waits on it.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, int] = {}
        self._guard = asyncio.Lock()

    @asynccontextmanager
    async def for_slot(self, slot_id: str):
        # No id means no shared conversation: run concurrently, take no lock.
        if not slot_id:
            yield
            return

        async with self._guard:
            lock = self._locks.get(slot_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[slot_id] = lock
                self._waiters[slot_id] = 0
            self._waiters[slot_id] += 1

        try:
            async with lock:
                yield
        finally:
            async with self._guard:
                self._waiters[slot_id] -= 1
                if self._waiters[slot_id] <= 0:
                    self._locks.pop(slot_id, None)
                    self._waiters.pop(slot_id, None)

    def _live_slots(self) -> int:
        """Number of slots currently tracked. For tests and diagnostics only."""
        return len(self._locks)
