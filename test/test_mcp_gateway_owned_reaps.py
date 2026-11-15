"""Cancellation-safe backend reaps owned by the MCP gateway pool."""

from __future__ import annotations

import asyncio

import pytest
from test_mcp_gateway_spawn_gate import _within_ceiling
from test_mcp_policy_instance_binding import (  # noqa: F401 -- fixtures register by import
    MOVED_PORT,
    OWNER_PORT,
    _announce,
    _control_plane_key,
    _demo_resolver,
    _pooled,
    _sender,
    audit,
    owner,
    owner_port_state,
    spawner,
)

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import BackendPool

ENFORCE_LAUNCH_APPROVAL = True


def _hold_shutdown(
    backend: Backend,
) -> tuple[asyncio.Event, asyncio.Event, asyncio.Event]:
    """Hold one backend shutdown between observable start and finish events."""
    shutdown_started = asyncio.Event()
    finish_shutdown = asyncio.Event()
    shutdown_finished = asyncio.Event()

    async def slow_shutdown(*, timeout: float) -> None:
        assert timeout == 2.0
        shutdown_started.set()
        await finish_shutdown.wait()
        shutdown_finished.set()

    backend.shutdown.side_effect = slow_shutdown  # type: ignore[attr-defined]
    return shutdown_started, finish_shutdown, shutdown_finished


async def _assert_shutdown_all_joins(
    pool: BackendPool,
    finish_shutdown: asyncio.Event,
    shutdown_finished: asyncio.Event,
    reap_name: str,
) -> None:
    daemon_shutdown = asyncio.create_task(pool.shutdown_all())
    try:
        await asyncio.sleep(0)
        assert not daemon_shutdown.done(), "shutdown_all returned without joining the reap"
    finally:
        finish_shutdown.set()
        await _within_ceiling(daemon_shutdown, f"daemon shutdown joining the {reap_name} reap")
        await asyncio.sleep(0)

    assert shutdown_finished.is_set()
    assert not pool._shutdown_tasks


class TestOwnerPortOwnedReaps:
    @pytest.mark.asyncio
    async def test_daemon_shutdown_joins_a_discarded_private_backend_reap(
        self, owner, monkeypatch, audit, spawner  # noqa: F811
    ):
        """Daemon teardown keeps ownership after the acquire task is cancelled."""
        _sender(monkeypatch, owner)
        pool = BackendPool(4)
        gw._set_owner_port(OWNER_PORT)
        shutdown_started = asyncio.Event()
        finish_shutdown = asyncio.Event()
        shutdown_finished = asyncio.Event()

        async def slow_shutdown(*, timeout: float) -> None:
            assert timeout == 2.0
            shutdown_started.set()
            await finish_shutdown.wait()
            shutdown_finished.set()

        async def announce_during_spawn() -> None:
            stale, _stale_env = spawner.calls[0]
            stale.shutdown.side_effect = slow_shutdown
            assert (await _announce(MOVED_PORT, pool))["type"] == "owner-port-set"

        spawner.during_first = announce_during_spawn
        acquire = asyncio.create_task(
            gw._acquire_backend(
                pool,
                _control_plane_key(),
                _demo_resolver,
                exclusive_stub_uuid="stub-private",
            )
        )
        await _within_ceiling(shutdown_started.wait(), "the stale backend shutdown starting")
        acquire.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquire

        ((stale, stale_env),) = spawner.calls
        assert stale_env["KIROCREW_BOUND_PORT"] == str(OWNER_PORT)
        daemon_shutdown = asyncio.create_task(pool.shutdown_all())
        try:
            await asyncio.sleep(0)
            assert not daemon_shutdown.done(), "shutdown_all returned without joining the reap"
        finally:
            finish_shutdown.set()
            await _within_ceiling(shutdown_finished.wait(), "the stale backend shutdown finishing")
            await _within_ceiling(daemon_shutdown, "daemon shutdown joining the stale backend reap")

        stale.shutdown.assert_awaited_once()
        assert shutdown_finished.is_set()
        assert not pool._shutdown_tasks

    @pytest.mark.asyncio
    async def test_daemon_shutdown_joins_a_cancelled_drained_spawn_reap(self):
        """A cancellation cannot detach the backend that lost the add race."""
        pool = BackendPool(4)
        key = _control_plane_key()
        stale = _pooled(key, OWNER_PORT)
        shutdown_started = asyncio.Event()
        finish_shutdown = asyncio.Event()
        shutdown_finished = asyncio.Event()

        async def slow_shutdown(*, timeout: float) -> None:
            assert timeout == 2.0
            shutdown_started.set()
            try:
                await finish_shutdown.wait()
            finally:
                shutdown_finished.set()

        stale.shutdown.side_effect = slow_shutdown

        async def spawn() -> Backend:
            return stale

        acquire = asyncio.create_task(
            pool.get_or_create(key, spawn, reusable=lambda _backend: False)
        )
        await _within_ceiling(shutdown_started.wait(), "the discarded backend reap starting")
        acquire.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquire

        daemon_shutdown = asyncio.create_task(pool.shutdown_all())
        try:
            await asyncio.sleep(0)
            assert not daemon_shutdown.done(), "shutdown_all returned without joining the reap"
        finally:
            finish_shutdown.set()
            await _within_ceiling(shutdown_finished.wait(), "the discarded backend reap finishing")
            await _within_ceiling(
                daemon_shutdown, "daemon shutdown joining the discarded backend reap"
            )

        stale.shutdown.assert_awaited_once()
        assert shutdown_finished.is_set()
        assert not pool._shutdown_tasks


class TestPoolOwnedReaps:
    @pytest.mark.asyncio
    async def test_daemon_shutdown_joins_a_cancelled_draining_reap(self) -> None:
        pool = BackendPool(4)
        key = _control_plane_key()
        backend = _pooled(key, OWNER_PORT)
        await pool.add(key, backend)
        assert await pool.drain_matching_to_bluegreen(lambda candidate: candidate is backend) == 1
        shutdown_started, finish_shutdown, shutdown_finished = _hold_shutdown(backend)

        reap = asyncio.create_task(pool.reap_draining())
        await _within_ceiling(shutdown_started.wait(), "the draining backend reap starting")
        reap.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reap

        await _assert_shutdown_all_joins(
            pool, finish_shutdown, shutdown_finished, "draining backend"
        )
        backend.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_sweeper_cancelled_mid_reap_leaves_no_drained_backend_untracked(
        self,
    ) -> None:
        """The second backend left ``_draining`` too, so it needs a reap that shutdown joins."""
        pool = BackendPool(4)
        first = _pooled(_control_plane_key("kirocrew-core"), OWNER_PORT)
        second = _pooled(_control_plane_key("kirocrew-dashboard"), OWNER_PORT)
        for backend in (first, second):
            await pool.add(backend.pool_key, backend)
        assert await pool.drain_matching_to_bluegreen(lambda _candidate: True) == 2
        shutdown_started, finish_shutdown, shutdown_finished = _hold_shutdown(first)

        reap = asyncio.create_task(pool.reap_draining())
        await _within_ceiling(shutdown_started.wait(), "the first draining backend reap starting")
        reap.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reap

        assert pool.draining_count == 0
        await _assert_shutdown_all_joins(
            pool, finish_shutdown, shutdown_finished, "first draining backend"
        )
        first.shutdown.assert_awaited_once()
        second.shutdown.assert_awaited_once()
