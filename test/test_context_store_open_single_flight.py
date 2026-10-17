"""``_build_store_vectors`` opens an uncached store once per burst, not once per caller.

A burst of first-touch requests on one uncached store -- ``member-memory.spec.ts``
seeds 65 facts five ``PUT /api/memory/semantic`` at a time -- reaches
``ContextBuilder.ensure_store`` while ``_vector_stores`` has no entry yet. Without
the single-flight every request launches its own ``_prepare`` worker: two
``KiroCrewConfig.load()`` each, an identity check with ``PRAGMA quick_check``, a
SQLite open plus migrations under the exclusive namespace lock, and then all but
the first discard their store at publication. A loop-stall dump of the E2E gateway
shows four such workers for one store at the instant the alarm fires.

These tests pin the single-flight: concurrent callers share one open, every caller
receives the one published instance, one caller's cancellation does not cancel the
open under the others while the last caller's does (so a cancelled open still never
publishes, the contract ``test_memv2_audit_a_prompt.py`` pins for one caller), a
refusal reaches every caller, the in-flight entry is released afterwards so the
next first touch opens again, a caller arriving in the loop step after the last
waiter cancelled starts a fresh open rather than joining the cancelled one, and a
release that lands while an open is in flight sends a later caller to a fresh open
instead of the one about to refuse.

The first-touch count needs no gate to be deterministic on the parent: a
``_prepare`` worker never consults the cache before it builds, only at publication,
so N callers always made N ``declared_store`` calls there. The tests that need an
open to be in flight gate ``declared_store``; every wait on that gate is bounded and
fails by name, and the gate is always released in a ``finally`` so a failed
assertion cannot leave a worker thread parked on it.
"""

from __future__ import annotations

import asyncio
import shutil
import threading

import pytest
from member_memory_helpers import forget_declared_stores, write_member_home

from kiro_crew import context, vector_memory
from kiro_crew.memory_stores import UnknownMemoryStore

STORE = "member-alice"
BURST = 5
#: Ceiling on every wait for a worker thread to reach or leave the gate. A healthy
#: run passes the gate in milliseconds; the ceiling only turns a hang into a named
#: failure instead of a pinned executor thread.
WAIT_SECS = 5.0


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    write_member_home(tmp_path, "alice")
    forget_declared_stores(monkeypatch)
    monkeypatch.setattr(context, "_vector_stores", {})
    monkeypatch.setattr(context, "_inflight_store_opens", {})
    db = tmp_path / "memory_stores" / STORE / "memory.db"
    seed = vector_memory.open_member_database(db, member_id="alice", store_id=STORE)
    seed.close()
    yield tmp_path
    for cached in context._vector_stores.values():
        cached.close()
    forget_declared_stores(monkeypatch)


@pytest.fixture
def opens(monkeypatch):
    """Count ``declared_store`` calls; optionally hold each one at a gate."""
    real = vector_memory.declared_store
    seen = {"calls": 0}
    started = threading.Event()
    gate = threading.Event()
    gate.set()

    def counting(*args, **kwargs):
        seen["calls"] += 1
        started.set()
        assert gate.wait(WAIT_SECS), "the test never released the declared_store gate"
        return real(*args, **kwargs)

    monkeypatch.setattr(vector_memory, "declared_store", counting)
    yield seen, started, gate
    # A failed assertion above the gate's own release must not leave a worker
    # thread parked on it past the test.
    gate.set()


async def _open_in_flight(started: threading.Event) -> None:
    """Wait, bounded, until a worker thread is parked at the gate."""
    assert await asyncio.to_thread(started.wait, WAIT_SECS), "no open reached declared_store"


@pytest.mark.asyncio
async def test_a_burst_of_first_touches_opens_the_store_once(home, opens):
    seen, _started, _gate = opens

    stores = await asyncio.gather(*(context._build_store_vectors(STORE) for _ in range(BURST)))

    assert seen["calls"] == 1, f"{seen['calls']} opens for {BURST} concurrent first touches"
    assert all(store is stores[0] for store in stores), "callers received different instances"
    assert stores[0] is not None
    assert context._vector_stores.get(STORE) is stores[0]
    assert STORE not in context._inflight_store_opens, "the in-flight entry outlived the open"


@pytest.mark.asyncio
async def test_a_cancelled_caller_does_not_cancel_the_open_under_the_others(home, opens):
    seen, started, gate = opens
    gate.clear()

    first = asyncio.ensure_future(context._build_store_vectors(STORE))
    second = asyncio.ensure_future(context._build_store_vectors(STORE))
    try:
        await _open_in_flight(started)
        first.cancel()
        # Let the cancellation reach the awaiter before the open is released.
        await asyncio.sleep(0)
    finally:
        gate.set()

    with pytest.raises(asyncio.CancelledError):
        await first
    store = await second
    assert store is not None
    assert seen["calls"] == 1
    assert context._vector_stores.get(STORE) is store
    assert STORE not in context._inflight_store_opens


@pytest.mark.asyncio
async def test_cancelling_every_caller_cancels_the_open_and_publishes_nothing(home, opens):
    seen, started, gate = opens
    gate.clear()

    callers = [asyncio.ensure_future(context._build_store_vectors(STORE)) for _ in range(2)]
    try:
        await _open_in_flight(started)
        shared = context._inflight_store_opens[STORE].task
        for caller in callers:
            caller.cancel()
        for caller in callers:
            with pytest.raises(asyncio.CancelledError):
                await caller
        assert shared.cancelled() or shared.cancelling(), "the open outlived its last caller"
    finally:
        gate.set()
    with pytest.raises(asyncio.CancelledError):
        await shared

    assert seen["calls"] == 1
    assert STORE not in context._vector_stores, "a cancelled open published its store"
    assert STORE not in context._inflight_store_opens


@pytest.mark.asyncio
async def test_a_caller_arriving_as_the_last_waiter_cancels_starts_a_fresh_open(home, opens):
    """The cancelled task finishes only on a later loop step; a caller landing in
    between must not join it and inherit a CancelledError nobody sent it."""
    seen, started, gate = opens
    gate.clear()

    first = asyncio.ensure_future(context._build_store_vectors(STORE))
    try:
        await _open_in_flight(started)
        stale_task = context._inflight_store_opens[STORE].task
        first.cancel()
        # One step: the awaiter's cancel handler runs; the task has not finished.
        await asyncio.sleep(0)
        assert not stale_task.done()
        assert context._inflight_store_opens.get(STORE) is None or (
            context._inflight_store_opens[STORE].task is not stale_task
        ), "the cancelled open is still joinable"
        second = asyncio.ensure_future(context._build_store_vectors(STORE))
        await asyncio.sleep(0)
        assert context._inflight_store_opens[STORE].task is not stale_task
    finally:
        gate.set()

    with pytest.raises(asyncio.CancelledError):
        await first
    store = await second
    assert store is not None
    assert seen["calls"] == 2
    assert context._vector_stores.get(STORE) is store
    assert STORE not in context._inflight_store_opens


@pytest.mark.asyncio
async def test_a_release_during_an_open_sends_a_later_caller_to_a_fresh_open(home, opens):
    """``release_cached_memory_store`` bumps the cache generation, so the open in
    flight refuses at publication. A caller arriving after the bump must not join
    that open; it starts its own under the new generation and gets a store."""
    seen, started, gate = opens
    gate.clear()

    stale = asyncio.ensure_future(context._build_store_vectors(STORE))
    try:
        await _open_in_flight(started)
        stale_task = context._inflight_store_opens[STORE].task
        context.release_cached_memory_store(STORE)
        fresh = asyncio.ensure_future(context._build_store_vectors(STORE))
        await asyncio.sleep(0)
        assert (
            context._inflight_store_opens[STORE].task is not stale_task
        ), "the later caller joined the open that is about to refuse"
    finally:
        gate.set()

    with pytest.raises(UnknownMemoryStore, match="changed during preparation"):
        await stale
    store = await fresh
    assert store is not None
    assert seen["calls"] == 2
    assert context._vector_stores.get(STORE) is store
    assert STORE not in context._inflight_store_opens


@pytest.mark.asyncio
async def test_a_refusal_reaches_every_caller_and_releases_the_entry(home, opens):
    seen, _started, _gate = opens
    shutil.rmtree(home / "memory_stores" / STORE)

    results = await asyncio.gather(
        *(context._build_store_vectors(STORE) for _ in range(BURST)), return_exceptions=True
    )

    assert all(isinstance(result, UnknownMemoryStore) for result in results), results
    assert seen["calls"] == 0, "admission runs before the open, so nothing was opened"
    assert STORE not in context._vector_stores
    assert STORE not in context._inflight_store_opens


@pytest.mark.asyncio
async def test_the_next_first_touch_after_a_release_opens_again(home, opens):
    seen, _started, _gate = opens

    first = await context._build_store_vectors(STORE)
    assert first is not None and seen["calls"] == 1
    # A release drops the cached handle; the following first touch must open anew
    # rather than hand out the closed instance or a stale in-flight task.
    context.release_cached_memory_store(STORE)
    assert STORE not in context._vector_stores

    again = await asyncio.gather(*(context._build_store_vectors(STORE) for _ in range(BURST)))

    assert seen["calls"] == 2
    assert all(store is again[0] for store in again)
    assert again[0] is not first
    assert context._vector_stores.get(STORE) is again[0]
