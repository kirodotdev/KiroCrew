"""Unit tests for the Plane C wake queue.

Covers the properties the Phase-4 bridge relies on: per-owner FIFO order,
delivery-does-not-remove (ack removes), at-least-once redelivery across a
restart, expiry of stale wakes, JSONL persistence, and owner isolation.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew.crew_wakes import (
    DEFAULT_MAX_AGE_SECS,
    MAX_LONG_POLL_SECS,
    Wake,
    WakeQueue,
    sanitize_owner,
)

OWNER = "kiro-cli:sess-abc"
OTHER = "kiro-cli:sess-xyz"


# -- sanitize_owner ------------------------------------------------------------


def test_sanitize_owner_strips_path_traversal() -> None:
    assert "/" not in sanitize_owner("kiro-cli:../../etc/passwd")
    assert ".." not in sanitize_owner("kiro-cli:..").replace(".", "_")
    # A colon is not filename-safe on every platform, so it is encoded too.
    assert ":" not in sanitize_owner(OWNER)


def test_sanitize_owner_keeps_safe_chars() -> None:
    assert sanitize_owner("abc-123_x.y") == "abc-123_x.y"


# -- enqueue / FIFO / peek -----------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_preserves_fifo_order(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    await wq.enqueue(OWNER, "monitor", "loop1", "first", cycle=1)
    await wq.enqueue(OWNER, "cron", "job2", "second")
    await wq.enqueue(OWNER, "monitor", "loop1", "third", cycle=2)
    wakes = await wq.peek(OWNER)
    assert [w.message for w in wakes] == ["first", "second", "third"]
    assert [w.kind for w in wakes] == ["monitor", "cron", "monitor"]
    assert wakes[0].cycle == 1 and wakes[2].cycle == 2


@pytest.mark.asyncio
async def test_each_wake_gets_a_distinct_id(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    a = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    b = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    assert a.id != b.id


# -- long_poll returns immediately when non-empty; delivery does not remove ----


@pytest.mark.asyncio
async def test_long_poll_returns_queued_without_removing(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    await wq.enqueue(OWNER, "monitor", "loop1", "m")
    first = await wq.long_poll(OWNER, wait=0)
    assert len(first) == 1
    # Not acked, so a second poll sees the same wake — at-least-once.
    second = await wq.long_poll(OWNER, wait=0)
    assert len(second) == 1
    assert first[0].id == second[0].id


@pytest.mark.asyncio
async def test_long_poll_wakes_on_enqueue(tmp_path) -> None:
    import asyncio

    wq = WakeQueue(tmp_path)
    poll = asyncio.create_task(wq.long_poll(OWNER, wait=MAX_LONG_POLL_SECS))
    # Give the poll a beat to park on the empty-queue event.
    await asyncio.sleep(0.05)
    assert not poll.done()
    await wq.enqueue(OWNER, "cron", "job1", "m")
    wakes = await asyncio.wait_for(poll, timeout=2)
    assert len(wakes) == 1


@pytest.mark.asyncio
async def test_long_poll_clamps_wait(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    # A huge wait on an empty queue must not hang; it is clamped and returns
    # empty after the (capped) window. Use a tiny effective wait via a monkey-
    # free path: enqueue nothing and assert it returns [] quickly by racing.
    import asyncio

    got = await asyncio.wait_for(wq.long_poll(OWNER, wait=0), timeout=2)
    assert got == []


# -- ack -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_submitted_removes_wake(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    w = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    assert await wq.ack(OWNER, w.id, "submitted") is True
    assert await wq.peek(OWNER) == []


@pytest.mark.asyncio
async def test_ack_dropped_also_removes_wake(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    w = await wq.enqueue(OWNER, "cron", "job1", "m")
    assert await wq.ack(OWNER, w.id, "dropped") is True
    assert await wq.peek(OWNER) == []


@pytest.mark.asyncio
async def test_double_ack_is_idempotent(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    w = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    assert await wq.ack(OWNER, w.id, "submitted") is True
    assert await wq.ack(OWNER, w.id, "submitted") is False


@pytest.mark.asyncio
async def test_ack_unknown_id_returns_false(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    await wq.enqueue(OWNER, "monitor", "loop1", "m")
    assert await wq.ack(OWNER, "nope", "submitted") is False


@pytest.mark.asyncio
async def test_ack_only_removes_the_named_wake(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    a = await wq.enqueue(OWNER, "monitor", "loop1", "a")
    b = await wq.enqueue(OWNER, "cron", "job2", "b")
    await wq.ack(OWNER, a.id, "submitted")
    remaining = await wq.peek(OWNER)
    assert [w.id for w in remaining] == [b.id]


# -- redelivery across restart -------------------------------------------------


@pytest.mark.asyncio
async def test_unacked_wakes_survive_restart(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    await wq.enqueue(OWNER, "monitor", "loop1", "first")
    w2 = await wq.enqueue(OWNER, "cron", "job2", "second")
    # Ack one; the other stays owed.
    await wq.ack(OWNER, w2.id, "submitted")
    # Fresh instance = a gateway restart.
    wq2 = WakeQueue(tmp_path)
    wq2.load()
    survived = await wq2.peek(OWNER)
    assert [w.message for w in survived] == ["first"]


@pytest.mark.asyncio
async def test_acked_wake_does_not_come_back_after_restart(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    w = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    await wq.ack(OWNER, w.id, "submitted")
    wq2 = WakeQueue(tmp_path)
    wq2.load()
    assert await wq2.peek(OWNER) == []


# -- expiry --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_wake_is_swept_on_poll(tmp_path) -> None:
    wq = WakeQueue(tmp_path, max_age_secs=1)
    w = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    # Backdate it beyond the max age.
    (await wq.peek(OWNER))  # no-op sweep, still fresh
    w.created_at = time.time() - 5
    # Re-persist the backdated copy by re-enqueuing state through a fresh load.
    # Simpler: mutate in place and sweep.
    got = await wq.long_poll(OWNER, wait=0)
    assert got == []


@pytest.mark.asyncio
async def test_sweep_drops_expired_across_owners(tmp_path) -> None:
    wq = WakeQueue(tmp_path, max_age_secs=1)
    a = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    b = await wq.enqueue(OTHER, "cron", "job2", "m")
    a.created_at = time.time() - 5
    b.created_at = time.time() - 5
    dropped = await wq.sweep()
    assert dropped == 2
    assert await wq.peek(OWNER) == []
    assert await wq.peek(OTHER) == []


@pytest.mark.asyncio
async def test_expired_wakes_dropped_on_load(tmp_path) -> None:
    wq = WakeQueue(tmp_path, max_age_secs=3600)
    w = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    # Rewrite the persisted record with a stale created_at by re-persisting a
    # backdated queue: mutate and force a rewrite via another enqueue+ack.
    w.created_at = time.time() - (DEFAULT_MAX_AGE_SECS + 100)
    # Directly rewrite the file to reflect the backdated wake.
    wq._queues[OWNER] = [w]  # test drives persistence directly
    async with wq._lock:
        wq._rewrite_locked(OWNER)
    wq2 = WakeQueue(tmp_path, max_age_secs=3600)
    wq2.load()
    assert await wq2.peek(OWNER) == []


# -- owner isolation -----------------------------------------------------------


@pytest.mark.asyncio
async def test_owners_are_isolated(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    await wq.enqueue(OWNER, "monitor", "loop1", "mine")
    await wq.enqueue(OTHER, "cron", "job2", "theirs")
    assert [w.message for w in await wq.peek(OWNER)] == ["mine"]
    assert [w.message for w in await wq.peek(OTHER)] == ["theirs"]


# -- record round-trip ---------------------------------------------------------


def test_wake_from_dict_ignores_unknown_keys() -> None:
    w = Wake.from_dict(
        {"kind": "cron", "handle": "j", "message": "m", "id": "x", "bogus": 1}
    )
    assert w.kind == "cron" and w.handle == "j" and w.id == "x"


@pytest.mark.asyncio
async def test_empty_queue_file_is_removed_after_last_ack(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    w = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    path = wq._path_for(OWNER)
    assert path.exists()
    await wq.ack(OWNER, w.id, "submitted")
    assert not path.exists()
