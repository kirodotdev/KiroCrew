"""Per-principal storage accounting and session-creation rate limiting.

Storage. The data home sits on a filesystem without project quotas on the
measured profile, so there is no kernel disk quota to lean on. Instead the
admission gate charges each principal for the bytes their accepted work adds
(request payloads here; uploads and logs through :meth:`StorageAccount.charge`
from their own handlers) in the durable ``principal_storage`` ledger in
``tasks.db``, and refuses new work with ``storage_limit`` once a principal is
over ``principal_storage_mb``. It is application accounting: it bounds growth
caused through Crew entry points, and it is reported as ``app_accounting``,
never as a quota.

Session creation. :class:`SessionCreateRate` is a per-principal sliding
window, so many tabs of one person share one allowance and cheap empty
sessions cannot be minted without bound.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from kiro_crew.taskq.store import TaskStore


class StorageAccount:
    """The durable per-principal byte ledger. Coroutines run on the store's writer thread."""

    def __init__(self, tasks: TaskStore, *, clock: Callable[[], float] = time.time) -> None:
        self._tasks = tasks
        self._clock = clock

    async def charge(self, budget_key: str, nbytes: int) -> int:
        """Add *nbytes* (negative to credit) to *budget_key*; returns the new total."""
        total: int = await self._tasks.run(
            self._tasks.storage_charge, budget_key, int(nbytes), at=self._clock()
        )
        return total

    async def used(self, budget_key: str) -> int:
        value: int = await self._tasks.run(self._tasks.storage_bytes, budget_key)
        return value

    async def would_exceed(self, budget_key: str, nbytes: int, limit: int) -> bool:
        return (await self.used(budget_key)) + max(0, int(nbytes)) > limit


class SessionCreateRate:
    """At most ``per_minute`` session creations per principal in any 60 s window."""

    WINDOW_SECONDS = 60.0

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = max(1, int(per_minute))
        self._clock = clock
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}

    def set_rate(self, per_minute: int) -> None:
        with self._lock:
            self._per_minute = max(1, int(per_minute))

    def allow(self, budget_key: str) -> tuple[bool, int]:
        """Record one creation if allowed. Returns ``(allowed, retry_after_seconds)``."""
        now = self._clock()
        with self._lock:
            events = self._events.setdefault(budget_key, deque())
            while events and now - events[0] >= self.WINDOW_SECONDS:
                events.popleft()
            if len(events) >= self._per_minute:
                retry = int(self.WINDOW_SECONDS - (now - events[0])) + 1
                return False, max(1, retry)
            events.append(now)
            # Bound memory: an idle principal's empty window is dropped.
            for key in [k for k, v in self._events.items() if not v and k != budget_key]:
                self._events.pop(key, None)
            return True, 0


__all__ = ["SessionCreateRate", "StorageAccount"]
