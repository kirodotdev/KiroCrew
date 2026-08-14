"""Tests for connection-private (non-poolable) backend acquisition.

A stub is emitted for every server; ``poolable`` decides only whether the
backend behind it may be shared. These tests pin the four properties that make
the private path safe, each of which a shared-bucket implementation breaks:

* two connections under an IDENTICAL PoolKey never resolve onto one backend
* their storage digests differ, so an MCP Apps callback cannot cross sessions
* they sit outside the ``max_backends`` budget and cannot starve a pooled acquire
* release hands the backend back for shutdown, and is a no-op for a pooled stub
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey

pytestmark = pytest.mark.xdist_group("mcp_gateway")


def _make_pool_key(server: str = "srv", agent: str = "agent") -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name=agent,
        command_args_hash="abc123",
        effective_env_hash="def456",
        work_dir="/tmp/test",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="ghi789",
        approval_mode="reads",
        trust_all_tools=False,
        user_identity="testuser",
        config_snapshot_hash="jkl012",
    )


def _make_mock_backend(pool_key: PoolKey, *, pid: int = 1) -> Backend:
    proc = MagicMock()
    proc.returncode = None
    proc.pid = pid
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    stdin = MagicMock()
    stdin.close = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    return Backend(
        pool_key=pool_key,
        process=proc,
        stdin=stdin,
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )


def _spawner(backend: Backend):
    async def _spawn() -> Backend:
        return backend
    return _spawn


@pytest.mark.asyncio
async def test_identical_keys_do_not_share_a_private_backend() -> None:
    """The property PoolKey cannot express, so the acquisition path must.

    Both connections compute the SAME digest — that is why routing them through
    the shared bucket would silently collapse them onto one process.
    """
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    first, second = _make_mock_backend(key, pid=1), _make_mock_backend(key, pid=2)

    got_first = await pool.acquire_exclusive(key, "stub-a", _spawner(first))
    got_second = await pool.acquire_exclusive(key, "stub-b", _spawner(second))

    assert got_first is first
    assert got_second is second
    assert key.stable_hash() == key.stable_hash()  # same key, by construction
    # Neither is reachable through the shared index, so a later pooled acquire
    # for the same key cannot land on one of them.
    assert await pool.get(key) is None
    assert len(pool) == 0


@pytest.mark.asyncio
async def test_private_backends_get_distinct_storage_digests() -> None:
    """The MCP Apps callback guarantee.

    ``get_by_digest`` is the ONLY way an app callback resolves a backend. If two
    private backends for the same server shared a digest, a callback issued by
    one session could execute against another session's process.
    """
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    first, second = _make_mock_backend(key, pid=1), _make_mock_backend(key, pid=2)

    await pool.acquire_exclusive(key, "stub-a", _spawner(first))
    await pool.acquire_exclusive(key, "stub-b", _spawner(second))

    assert first.storage_digest != second.storage_digest
    assert await pool.get_by_digest(first.storage_digest) is first
    assert await pool.get_by_digest(second.storage_digest) is second
    # The bare PoolKey digest addresses neither: it is not a private backend's
    # identity, so it must not resolve to an arbitrary one of them.
    assert await pool.get_by_digest(key.stable_hash()) is None


@pytest.mark.asyncio
async def test_private_backends_are_outside_the_capacity_budget() -> None:
    """A private backend sits at refcount 1 for life, so it can never be an
    eviction victim. Counting it against ``max_backends`` would let private
    connections accumulate as unevictable occupants until a POOLED acquire found
    nothing to evict and was rejected — connections that opted out of sharing
    denying service to one that opted in.
    """
    pool = BackendPool(max_backends=1)
    key_a, key_b = _make_pool_key(server="a"), _make_pool_key(server="b")

    for i in range(5):
        await pool.acquire_exclusive(
            key_a, f"stub-{i}", _spawner(_make_mock_backend(key_a, pid=i))
        )

    assert pool.stats()["exclusive"] == 5
    assert len(pool) == 0  # none of them consumed the budget

    # The single budgeted slot is still free for a pooled acquire.
    shared = _make_mock_backend(key_b, pid=99)
    assert await pool.get_or_create(key_b, _spawner(shared)) is shared
    assert len(pool) == 1


@pytest.mark.asyncio
async def test_release_returns_the_backend_and_is_a_noop_for_pooled() -> None:
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key)
    await pool.acquire_exclusive(key, "stub-a", _spawner(backend))

    assert await pool.release_exclusive("stub-a") is backend
    assert pool.stats()["exclusive"] == 0
    # Idempotent, and safe on a stub that never had a private backend — the
    # disconnect path calls this unconditionally.
    assert await pool.release_exclusive("stub-a") is None
    assert await pool.release_exclusive("never-existed") is None


@pytest.mark.asyncio
async def test_shutdown_all_reaps_private_backends() -> None:
    """Daemon teardown races a stub disconnect. Without collecting these, a
    private backend outlives the gateway holding its pipes open.
    """
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key)
    backend.shutdown = AsyncMock()  # type: ignore[method-assign]
    await pool.acquire_exclusive(key, "stub-a", _spawner(backend))

    await pool.shutdown_all(timeout=0.1)

    backend.shutdown.assert_awaited()
    assert pool.stats()["exclusive"] == 0


@pytest.mark.asyncio
async def test_double_register_on_one_connection_reaps_the_loser() -> None:
    """``stub_uuid`` is a fresh uuid4 per connection, so a collision means the
    same connection registered twice (the respawn path). Overwriting the entry
    would orphan a live subprocess.
    """
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    first, second = _make_mock_backend(key, pid=1), _make_mock_backend(key, pid=2)
    first.shutdown = AsyncMock()  # type: ignore[method-assign]

    await pool.acquire_exclusive(key, "stub-a", _spawner(first))
    await pool.acquire_exclusive(key, "stub-a", _spawner(second))

    first.shutdown.assert_awaited()
    assert pool.stats()["exclusive"] == 1
    assert await pool.get_by_digest(second.storage_digest) is second


@pytest.mark.asyncio
async def test_pooled_backend_digest_is_the_plain_poolkey_hash() -> None:
    """The shared path is unchanged: reuse depends on two connections computing
    one digest, so a pooled backend must NOT carry a per-connection suffix.
    """
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key)

    await pool.get_or_create(key, _spawner(backend))

    assert backend.exclusive_token == ""
    assert backend.storage_digest == key.stable_hash()
    assert await pool.get_by_digest(key.stable_hash()) is backend


@pytest.mark.asyncio
async def test_cancel_between_spawn_and_registration_reaps_the_child() -> None:
    """The window where the child is reachable from nothing.

    Registration needs ``_lock``, and acquiring it yields — so a cancel landing
    between ``spawn()`` returning and the entry appearing leaves a live process
    that neither the connection teardown (which looks it up by stub_uuid) nor
    ``shutdown_all`` (which walks the maps) can find.
    """
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key)
    backend.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Hold the pool lock so the acquire parks exactly inside the window.
    await pool._lock.acquire()
    task = asyncio.create_task(
        pool.acquire_exclusive(key, "stub-a", _spawner(backend))
    )
    # Let it run up to the lock wait, then cancel it there.
    for _ in range(20):
        await asyncio.sleep(0)
        if backend.shutdown.await_count or task.done() or pool._lock.locked():
            break
    task.cancel()
    pool._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The reap is scheduled as a task so a cancelled caller cannot skip it.
    for _ in range(50):
        await asyncio.sleep(0)
        if backend.shutdown.await_count:
            break
    backend.shutdown.assert_awaited()
    assert pool.stats()["exclusive"] == 0


@pytest.mark.asyncio
async def test_private_acquire_takes_no_reservation_to_release() -> None:
    """The reservation refcount is per DIGEST, and ``poolable`` is deliberately
    not a PoolKey dimension — so a pooled and a private connection can share one
    digest (reachable when the allowlist changes under a daemon that outlives the
    gateway: the old overlay's stub still registers poolable, the new one does
    not).

    A private acquire must therefore not reserve, or the pooled connection's
    protection would be inflated; and the connection handler must not release on
    its behalf, or the pooled backend becomes evictable before its stub attaches.
    This pins the pool half: private acquisition leaves the digest's reservation
    exactly as it found it.
    """
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    digest = key.stable_hash()

    # A pooled connection is mid-handout: reserved, not yet attached.
    pool.reserve(key)
    assert pool._reserved_digests.get(digest) == 1

    await pool.acquire_exclusive(
        key, "private-stub", _spawner(_make_mock_backend(key, pid=7))
    )

    # Untouched — the private acquire neither added nor consumed a reservation.
    assert pool._reserved_digests.get(digest) == 1


@pytest.mark.asyncio
async def test_private_backends_appear_in_lifecycle_enumerators() -> None:
    """Out of the reuse index and the capacity budget, but NOT out of the process
    lifecycle.

    ``all_backends()`` feeds the shutdown drain predicate and the abort handler;
    ``live_backend_pids()`` feeds the pidfile that stops a SIGKILLed gatewayd
    orphaning children. A private backend missing from either gets its pending
    reply discarded, its in-flight calls left uncancellable, or its process
    leaked.
    """
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    private = _make_mock_backend(key, pid=4242)
    await pool.acquire_exclusive(key, "stub-a", _spawner(private))

    assert private in pool.all_backends()
    assert 4242 in pool.live_backend_pids()


@pytest.mark.asyncio
async def test_concurrent_private_acquires_do_not_interleave_registrations() -> None:
    """Distinct connections arriving on the same tick each keep their own entry."""
    pool = BackendPool(max_backends=2)
    key = _make_pool_key()
    backends = [_make_mock_backend(key, pid=i) for i in range(8)]

    await asyncio.gather(*[
        pool.acquire_exclusive(key, f"stub-{i}", _spawner(b))
        for i, b in enumerate(backends)
    ])

    assert pool.stats()["exclusive"] == 8
    assert len({b.storage_digest for b in backends}) == 8


# --- connection-handler half of the reservation contract ---------------------
#
# The pool test above proves ``acquire_exclusive`` takes no reservation. This
# half proves the handler does not RELEASE one on a private connection's behalf,
# which is where the unbalanced decrement actually lived.


def _register_frame(*, poolable: bool) -> dict[str, Any]:
    return {
        "type": "register",
        "stub_uuid": "ex-stub-0001",
        "server_name": "echo-mcp",
        "agent_name": "ex-agent",
        "command_args_hash": "0" * 64,
        "effective_env_hash": "1" * 64,
        "work_dir": "/tmp",
        "binary_version": "deadbeef",
        "os_uid": 1000,
        "sandbox_mode": "standard",
        "autoapprove_set_hash": "2" * 64,
        "approval_mode": "interactive",
        "trust_all_tools": False,
        "poolable": poolable,
        "user_identity": "ex",
        "channel_id": "C_EX",
        "config_snapshot_hash": "3" * 64,
        "session_key": "dashboard:chat-EX-1",
        "session_type": "dashboard",
        "principal_id": "ex",
    }


_TOOL_CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}


class _FrameReader:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._q = [(json.dumps(f) + "\n").encode() for f in frames]

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        if not self._q:
            raise asyncio.IncompleteReadError(b"", None)
        return self._q.pop(0)


class _NullWriter:
    def write(self, _b: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def get_extra_info(self, _name: str, default: Any = None) -> Any:
        return default


class _HandlerBackend:
    supports_caller_identity = True
    quarantined = False
    exclusive_token = ""

    def __init__(self) -> None:
        self._pending_requests: dict = {}

    async def attach_stub(self, _uuid: str) -> "asyncio.Queue[bytes]":
        return asyncio.Queue()

    async def detach_stub(self, _uuid: str) -> int:
        return 0

    async def cancel_in_flight_for_stub(self, _stub_uuid: str) -> list[str]:
        return []

    async def recycle_if_idle(self) -> bool:
        return False

    async def forward_from_stub(self, _uuid: str, _msg: dict, caller: Any = None) -> None:
        pass


class _RecordingPool:
    """Records whether the handler released a hand-out reservation."""

    def __init__(self) -> None:
        self.unreserved: list[Any] = []

    def unreserve(self, key: Any) -> None:
        self.unreserved.append(key)

    async def release_exclusive(self, _stub_uuid: str) -> None:
        return None


async def _drive_handler(monkeypatch: pytest.MonkeyPatch, *, poolable: bool) -> _RecordingPool:
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway import socketsec

    monkeypatch.setattr(socketsec, "PEER_IDENTITY_SUPPORTED", True)
    monkeypatch.setattr(
        socketsec, "check_peer_is_self", lambda _w: socketsec.PeerCredResult.MATCH
    )
    monkeypatch.setattr(socketsec, "socket_owner_only", lambda _path: True)

    class _NullSEL:
        def log_api_access(self, **kwargs: Any) -> None:
            pass

    async def _fake_acquire(_pool: Any, _key: Any, _resolver: Any, **_kw: Any):
        return _HandlerBackend(), True

    async def _fake_drain(_inbox: Any, _writer: Any, _stub_uuid: str = "") -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(gw, "SecurityEventLog", _NullSEL)
    monkeypatch.setattr(gw, "_acquire_backend", _fake_acquire)
    monkeypatch.setattr(gw, "_drain_inbox_to_stub", _fake_drain)

    pool = _RecordingPool()
    await asyncio.wait_for(
        gw._handle_connection(
            _FrameReader([_register_frame(poolable=poolable), _TOOL_CALL]),
            _NullWriter(),
            pool=pool,
            resolver=object(),
            socket_path=Path("/tmp/ex.sock"),
            hot_keys=None,
        ),
        timeout=5.0,
    )
    return pool


@pytest.mark.asyncio
async def test_handler_does_not_release_a_reservation_for_a_private_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decrement that would drop a pooled connection's eviction protection.

    A private connection reserved nothing, so releasing is not a harmless no-op:
    ``unreserve`` decrements the shared per-digest refcount, and a pooled
    connection with an identical PoolKey would lose its protection before its
    stub attaches.
    """
    pool = await _drive_handler(monkeypatch, poolable=False)
    assert pool.unreserved == []


@pytest.mark.asyncio
async def test_handler_still_releases_for_a_pooled_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not leak the other way: a pooled connection DID reserve, so
    failing to release would pin its backend against idle and LRU eviction for
    the life of the process."""
    pool = await _drive_handler(monkeypatch, poolable=True)
    assert len(pool.unreserved) == 1
