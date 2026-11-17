"""``acp.session_provider_shared.shutdown_shared``: a KAS chat leaving a shared process.

Chat sharing is KAS-only, so these drive the real shutdown on a KAS runtime: the
eviction keeps the stored record, and every failure along the way (the cancel,
the eviction, the release, the kill) still returns the lease and never strands
the process. The doubles come from ``test_chat_runtime_sharing``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from test_chat_runtime_sharing import KillableRuntime, a_key, shared_provider, spawner

import kiro_crew.runtime_ownership as ro
from kiro_crew.acp_backends import ACP_BACKEND_KAS


class KasRuntime(KillableRuntime):
    """A KAS runtime that records which session it kept the record of."""

    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self.acp_backend = ACP_BACKEND_KAS
        self.kept: list[str] = []

    @contextlib.contextmanager
    def keep_record_on_teardown(self, session_id):
        self.kept.append(session_id)
        yield


@pytest.fixture
def table():
    ro._reset_for_tests()
    yield ro.RUNTIME_OWNERSHIP
    ro._reset_for_tests()


def _two_on(table, rt):
    one = asyncio.run(table.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
    two = asyncio.run(table.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))
    return one, two


def test_a_kas_chat_leaving_evicts_inside_the_record_keeping_scope(table):
    rt = KasRuntime(pid=9700)
    one, _two = _two_on(table, rt)
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", one.lease)
    handle.session_id = "sess-1"
    hooks = []
    handle._cancel_hook_tasks = lambda: hooks.append(1)
    asyncio.run(provider.shutdown())
    assert rt.kept == ["sess-1"] and handle.destroyed == 1
    assert hooks == [1]
    assert rt.kills == [] and table.leases_on_runtime(rt) == 1


def test_a_failed_cancel_still_evicts_and_releases(table):
    rt = KasRuntime(pid=9701)
    one, _two = _two_on(table, rt)
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", one.lease)
    handle.session_id = "sess-1"
    handle.is_turn_active = True

    async def boom():
        raise RuntimeError("cancel failed")

    handle.cancel = boom
    asyncio.run(provider.shutdown())
    assert handle.destroyed == 1 and table.leases_on_runtime(rt) == 1


def test_a_failed_eviction_still_returns_the_lease(table):
    rt = KasRuntime(pid=9702)
    one, _two = _two_on(table, rt)
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", one.lease)
    handle.session_id = "sess-1"

    async def boom():
        raise RuntimeError("session/close failed")

    handle.destroy = boom
    asyncio.run(provider.shutdown())
    assert provider._runtime_lease is None and table.leases_on_runtime(rt) == 1


def test_a_cancelled_eviction_returns_the_lease_then_reraises(table):
    rt = KasRuntime(pid=9703)
    (only,) = [asyncio.run(table.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))]
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", only.lease)
    handle.session_id = "sess-1"

    async def cancelled():
        raise asyncio.CancelledError()

    handle.destroy = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.shutdown())
    assert table.leases_on_runtime(rt) == 0
    assert rt.kills == ["provider shutdown (last shared session)"]


def test_no_lease_means_no_release_and_no_kill(table):
    rt = KasRuntime(pid=9704)
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", None)
    handle.session_id = "sess-1"
    asyncio.run(provider.shutdown())
    assert handle.destroyed == 1 and rt.kills == []


def test_a_failed_kill_of_the_last_holder_is_survived(table):
    rt = KasRuntime(pid=9706)
    (only,) = [asyncio.run(table.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))]
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", only.lease)
    handle.session_id = "sess-1"

    async def boom(**_kw):
        raise RuntimeError("kill failed")

    rt.kill = boom
    asyncio.run(provider.shutdown())
    assert provider._runtime_lease is None and table.leases_on_runtime(rt) == 0
