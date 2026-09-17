"""``slot_switch_session_lock`` across event loops.

The helper caches one ``asyncio.Lock`` per session key in a process-wide
``WeakValueDictionary``. A lock binds to the loop it is first contended on,
so a cached lock that outlives its loop (a reference kept past a per-test
loop) must not be handed to a caller on another loop: awaiting it there
raises ``RuntimeError: ... is bound to a different event loop``.
"""

from __future__ import annotations

import asyncio

from kiro_crew.llm_helpers import _slot_switch_session_locks, slot_switch_session_lock

_KEY = "dash:test-cross-loop"


def _contend(lock: asyncio.Lock) -> "asyncio.Future[None]":
    """Queue a second waiter on ``lock`` so the mixin records the running
    loop; the caller must already hold the lock."""
    return asyncio.ensure_future(lock.acquire())


async def _release_waiter(waiter: "asyncio.Future[None]") -> None:
    await asyncio.sleep(0)
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass


def _bind_on_fresh_loop(key: str) -> asyncio.Lock:
    """Return a lock for ``key`` that has been contended, and so bound, on a
    loop that is closed by the time this returns."""

    async def _run() -> asyncio.Lock:
        lock = slot_switch_session_lock(key)
        async with lock:
            await _release_waiter(_contend(lock))
        return lock

    return asyncio.run(_run())


def test_lock_bound_on_a_closed_loop_is_replaced_for_the_next_loop() -> None:
    _slot_switch_session_locks.pop(_KEY, None)
    stale = _bind_on_fresh_loop(_KEY)
    assert getattr(stale, "_loop", None) is not None
    assert _slot_switch_session_locks.get(_KEY) is stale

    async def _use() -> asyncio.Lock:
        lock = slot_switch_session_lock(_KEY)
        async with lock:
            # Contention is what trips the loop check: on a lock bound to the
            # closed loop this raises ``RuntimeError``.
            await _release_waiter(_contend(lock))
        return lock

    fresh = asyncio.run(_use())
    assert fresh is not stale
    # The stale lock is still alive (this test holds it) yet the cache now
    # serves the replacement.
    assert _slot_switch_session_locks.get(_KEY) is fresh


def test_lock_is_reused_within_one_loop() -> None:
    _slot_switch_session_locks.pop(_KEY, None)

    async def _twice() -> tuple[asyncio.Lock, asyncio.Lock]:
        first = slot_switch_session_lock(_KEY)
        async with first:
            await _release_waiter(_contend(first))
        second = slot_switch_session_lock(_KEY)
        return first, second

    first, second = asyncio.run(_twice())
    assert first is second


def test_unbound_lock_is_reused_across_loops() -> None:
    _slot_switch_session_locks.pop(_KEY, None)
    unbound = slot_switch_session_lock(_KEY)
    assert getattr(unbound, "_loop", None) is None

    async def _use() -> asyncio.Lock:
        return slot_switch_session_lock(_KEY)

    assert asyncio.run(_use()) is unbound
