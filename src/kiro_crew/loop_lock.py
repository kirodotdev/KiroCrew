"""Loop-bound asyncio lock for module-global declarations.

An ``asyncio.Lock`` binds to the event loop it is first used on; acquiring it
from a *different* loop raises ``RuntimeError`` on Python 3.10+. A module-global
``asyncio.Lock()`` therefore breaks whenever one process runs more than one
event loop over the module's lifetime — pytest-asyncio spinning a fresh loop
per test, or a gateway restart-in-process. This is the defect class behind
issue #4800 (and the flake trio it produced via a swallowed ``RuntimeError``:
#4177, #4789).

The repo grew ad-hoc versions of the fix shape (``_get_config_lock`` in
``dashboard/handlers/agents.py``, ``get_lock`` in ``messaging/auto_title.py``,
which every channel's auto-title now shares; ``__init__._LazyShutdownEvent``
and the semaphore in
``dashboard/handlers/link_meta.py`` are cousins for other primitives, left
as-is);  :class:`LoopBoundLock` is the shared chokepoint the module-global
locks now route through. Declare it at module level exactly like the lock it
replaces::

    _CACHE_LOCK = LoopBoundLock()

    async def handler() -> None:
        async with _CACHE_LOCK:
            ...

Construction is loop-free (safe at import time). Internally the instance keeps
one ``asyncio.Lock`` **per event loop**, in a ``WeakKeyDictionary`` keyed by
the loop, created lazily inside the first acquiring coroutine on that loop:

* ``acquire()``/``release()`` always pair against the *running* loop's lock,
  so a coroutine can never release a lock some other loop's coroutine holds —
  the failure mode a single mutable rebound pointer would have (release on
  loop A silently unlocking loop B's critical section).
* ``locked()`` reads the running loop's lock, matching what ``acquire()`` on
  that loop would contend with — the same object a bare lock's ``locked()``
  describes. Called outside any loop, it reports whether *any* loop's lock is
  held (the conservative answer for sync status probes).
* Weak keys mean a finished loop's lock dies with it: no strong reference
  retains a closed loop and its callback graph.

The guarantee is mutual exclusion *within* each event loop — the same contract
a module-global ``asyncio.Lock`` offered when it worked at all. Two loops
running concurrently (in different threads) each get their own lock and are
NOT mutually excluded; an ``asyncio`` primitive cannot await across loops, so
code needing cross-loop exclusion needs a different design (e.g. a single
owning loop, or a thread lock around non-awaiting sections).

``scripts/check_loop_bound_locks.py`` is the CI gate that keeps new bare
module-global ``asyncio.Lock()``/``Event()``/``Queue()`` declarations from
reintroducing the class.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref

logger = logging.getLogger(__name__)

__all__ = ["LoopBoundLock"]


class LoopBoundLock:
    """A drop-in module-global lock that lazily binds per running loop.

    Mirrors the ``asyncio.Lock`` surface the codebase actually uses on module
    globals: ``async with``, ``acquire()``, ``release()``, ``locked()``.
    """

    __slots__ = ("_locks", "_guard", "_warned")

    def __init__(self) -> None:
        # loop -> that loop's asyncio.Lock. Weak keys: a dead loop's entry
        # (and lock) is collectable. Guarded by a threading.Lock because two
        # loops live in two threads by definition, and WeakKeyDictionary
        # mutation is not atomic. Never awaited under the guard.
        self._locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._guard = threading.Lock()
        self._warned = False  # one-shot concurrent-loops warning latch

    def _sweep_closed(self) -> None:
        """Drop entries whose loop is closed. Caller holds ``self._guard``.

        Weak keys alone do NOT bound the table: a contended ``asyncio.Lock``
        strongly references its loop (waiter callbacks), so the VALUE keeps
        the weak KEY alive — one immortal entry per closed loop, growing
        without bound under pytest-asyncio's fresh-loop-per-test. Sweeping on
        insert bounds the table to the loops that are actually alive.
        """
        for loop in [lp for lp in self._locks if lp.is_closed()]:
            del self._locks[loop]

    def _bound(self) -> asyncio.Lock:
        """Return the running loop's lock, creating it on first use."""
        loop = asyncio.get_running_loop()
        with self._guard:
            lock = self._locks.get(loop)
            if lock is None:
                self._sweep_closed()
                if self._locks and not self._warned:
                    # Another LIVE loop already uses this lock: the two loops
                    # are NOT mutually excluded (per-loop contract). The bare
                    # lock this class replaced failed CLOSED here with a
                    # RuntimeError; keep that failure loud instead of letting
                    # it become a silent race. One-shot per instance.
                    self._warned = True
                    logger.warning(
                        "LoopBoundLock used from a second live event loop; "
                        "cross-loop mutual exclusion is NOT provided (see "
                        "kiro_crew.loop_lock docstring, #4800)",
                        stack_info=True,
                    )
                lock = self._locks[loop] = asyncio.Lock()
        return lock

    async def acquire(self) -> bool:
        return await self._bound().acquire()

    def release(self) -> None:
        loop = asyncio.get_running_loop()
        with self._guard:
            lock = self._locks.get(loop)
        if lock is None:
            raise RuntimeError("LoopBoundLock.release() called before any acquire on this loop")
        lock.release()

    def locked(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Outside any loop: a sync status probe. Report held if ANY
            # LIVE loop's lock is held — the conservative answer. Closed
            # loops are swept first: a lock a dead loop's holder never
            # released would otherwise read as held forever.
            with self._guard:
                self._sweep_closed()
                return any(lock.locked() for lock in self._locks.values())
        with self._guard:
            lock = self._locks.get(loop)
        return lock is not None and lock.locked()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        self.release()

    def __repr__(self) -> str:  # pragma: no cover — debug aid only
        with self._guard:
            n = len(self._locks)
            held = sum(1 for lock in self._locks.values() if lock.locked())
        return f"<LoopBoundLock loops={n} held={held}>"
