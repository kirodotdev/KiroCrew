"""A store write never commits after its caller's budget has run out.

``ResourceSubscriptionStore`` writes wait for a file lock that another process,
such as a stopped ``kirocrew resource-subs list``, may hold. Pinned here: a write
given a deadline gives up waiting there, a lock granted only after it is let go
unused, and a removal whose budget ran out while it waited is still on disk once
the lock comes free, so the caller's refusal is the truth.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from test_mcp_resource_subscription_tools import SERVER, SESSION, URI, _record

from kiro_crew.config.integration_sections import McpResourceSubscriptionsConfig
from kiro_crew.mcp_gateway import resource_subscriptions as rs
from kiro_crew.mcp_gateway.resource_subscription_store import (
    ResourceSubscriptionStore,
    StoreDeadlinePassed,
)
from kiro_crew.mcp_gateway.resource_subscriptions import ResourceSubscriptionService

#: The longest the competing holder waits for the test to let go of the lock. It
#: only bounds a write that ignores its deadline; reaching it fails the test.
WATCHDOG_SECS = 30.0


@pytest.fixture
def store(tmp_path: Path) -> ResourceSubscriptionStore:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(SESSION, (URI,), ("rsub_deadline00000",)))
    return store


@contextmanager
def _lock_held_elsewhere(store: ResourceSubscriptionStore) -> Iterator[None]:
    """Another holder of the store lock until the block ends.

    A write that ignores its deadline blocks the block; the holder then lets go
    after :data:`WATCHDOG_SECS` so the test fails rather than hanging.
    """
    taken = threading.Event()
    release = threading.Event()
    released_by_test = threading.Event()

    def hold() -> None:
        with store._locked():
            taken.set()
            if release.wait(WATCHDOG_SECS):
                released_by_test.set()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert taken.wait(5), "the competing holder never took the lock"
    try:
        yield
    finally:
        release.set()
        holder.join(WATCHDOG_SECS + 5)
    assert (
        released_by_test.is_set()
    ), "the lock came free on the watchdog: a write inside the block ignored its deadline"


def test_a_write_past_its_deadline_is_refused_and_never_lands(
    store: ResourceSubscriptionStore,
) -> None:
    with _lock_held_elsewhere(store):
        with pytest.raises(OSError):
            # The lock is held through the whole call, so this deadline always passes.
            store.remove(SESSION, SERVER, deadline=time.monotonic() + 0.2)  # flake-ok: lock is held
    assert [r.session_key for r in store.load()] == [SESSION]


def test_a_lock_granted_at_the_deadline_is_let_go_unused(
    store: ResourceSubscriptionStore,
) -> None:
    with pytest.raises(StoreDeadlinePassed):
        store.remove(SESSION, SERVER, deadline=time.monotonic() - 1)  # flake-ok: already passed
    assert [r.session_key for r in store.load()] == [SESSION]


@pytest.mark.asyncio
async def test_a_removal_out_of_budget_while_the_store_is_locked_stays_undone(
    store: ResourceSubscriptionStore,
) -> None:
    switched_off = McpResourceSubscriptionsConfig(enabled=False)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: switched_off,
        unsubscribe_budget_secs=0.5,  # flake-ok: the store lock is held past this budget
    )
    await service.start()
    try:
        with _lock_held_elsewhere(store):
            assert await service.unsubscribe(SESSION, SERVER, URI) is False
        assert [r.session_key for r in store.load()] == [SESSION]
        assert [r.session_key for r in service.records(SESSION)] == [SESSION]
    finally:
        await service.close()


class _HolderHolding:
    """A started holder carrying *uris*, as far as the subscribe cleanup reads it."""

    def __init__(self, uris: list[str]) -> None:
        self.uris = list(uris)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _started_service(store: ResourceSubscriptionStore) -> ResourceSubscriptionService:
    switched_off = McpResourceSubscriptionsConfig(enabled=False)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=rs.RecordingSink(),
        settings=lambda: switched_off,
        subscribe_commit_reserve_secs=0.1,
    )
    await service.start()
    return service


@pytest.mark.asyncio
async def test_the_earlier_removal_a_subscribe_finishes_first_gives_up_at_the_budget(
    store: ResourceSubscriptionStore,
) -> None:
    service = await _started_service(store)
    try:
        with _lock_held_elsewhere(store):
            with pytest.raises(rs.SubscribeRefused, match="could not be removed"):
                await service._settle_unretired_committed(SESSION, rs._Budget(0.5))
        assert [r.session_key for r in store.load()] == [SESSION]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_a_refused_subscribe_cleanup_gives_up_at_the_budget(
    store: ResourceSubscriptionStore,
) -> None:
    service = await _started_service(store)
    key = (SESSION, SERVER)
    installed = service._records[key]
    holder = _HolderHolding([URI, "crux://reviews/CR-carried"])
    service._holders[key] = holder  # type: ignore[assignment]
    try:
        with _lock_held_elsewhere(store):
            await service._settle_started_holder_committed(
                key, holder, installed, None, rs._Budget(0.5)  # type: ignore[arg-type]
            )
        (stored,) = store.load()
        assert stored.uris == [URI]
        assert holder.closed, "a cleanup that could not record the holder keeps it running"
    finally:
        service._holders.pop(key, None)
        await service.close()
