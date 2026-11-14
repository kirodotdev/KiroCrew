"""Dev Fleet health routes and the reads behind them.

``/health`` is the gateway's 15 s liveness poll and answers from the backend alone.
``/api/health`` serves the restart handshake's start identity through a shared reader:
one read in flight, a finished read reused for ``_GATEWAY_START_ID_TTL``, a read that
raises answered as ``null``. ``_gateway_service_active`` shares one probe between
concurrent cache misses. The restart path keeps reading the identity uncached.

Every service read is a stub at the backend seam: no ``systemctl``, no sleeps. Slow
reads wait on an ``asyncio.Event`` the test sets after every caller has registered,
and the TTL moves on a ``ManualClock`` installed on ``live``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.apps.builtins.dev_fleet import http_api, live, runtime, server
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until, until_parked

#: Bound on every await the test itself unblocks, so a missed release fails by name.
_BOUND = 10.0
_CALLERS = 8


class _GatedBackend:
    """A service backend whose reads wait until the test sets ``release``."""

    def __init__(self, stamp: str = "stamp-1") -> None:
        self.stamp = stamp
        self.start_id_reads = 0
        self.active_probes = 0
        self.restarts = 0
        self.release = asyncio.Event()

    async def start_id(self) -> str:
        self.start_id_reads += 1
        await self.release.wait()
        return self.stamp

    async def active(self) -> bool:
        self.active_probes += 1
        await self.release.wait()
        return True

    async def restart_detached(self) -> tuple[bool, str]:
        self.restarts += 1
        return True, ""


class _Callers:
    """Counts callers that have entered the reader under test.

    The reader joins or starts the shared read before its first suspension, so
    ``entered == n`` seen from the test means all ``n`` callers have registered.
    """

    def __init__(self) -> None:
        self.entered = 0

    async def health(self) -> tuple[int, dict]:
        self.entered += 1
        resp = await http_api.api_health(make_mocked_request("GET", "/api/health"))
        return resp.status, json.loads(resp.text)

    async def service_active(self) -> bool:
        self.entered += 1
        return await live._gateway_service_active()


async def _registered(callers: _Callers, count: int) -> None:
    await async_wait_until(
        lambda: callers.entered == count, describe=lambda: f"{callers.entered} entered"
    )


@pytest.mark.asyncio
async def test_liveness_route_answers_ok_without_any_service_read(monkeypatch):
    run_cmd = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)
    start_id = AsyncMock(return_value="stamp-1")
    monkeypatch.setattr(live, "_gateway_start_id", start_id)

    async with TestClient(TestServer(server.create_app())) as client:
        run_cmd.reset_mock()  # app startup's own discovery is not under test
        for _ in range(25):
            resp = await client.get("/health")
            assert resp.status == 200
            assert await resp.json() == {"status": "ok"}

    run_cmd.assert_not_awaited()
    start_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_api_health_requests_share_one_identity_read(monkeypatch):
    backend = _GatedBackend("stamp-1")
    monkeypatch.setattr(live, "_gateway_backend", lambda: backend)
    callers = _Callers()

    tasks = [asyncio.ensure_future(callers.health()) for _ in range(_CALLERS)]
    await _registered(callers, _CALLERS)
    await until_parked(backend.release)
    assert backend.start_id_reads == 1

    backend.release.set()
    answers = await asyncio.wait_for(asyncio.gather(*tasks), _BOUND)

    assert answers == [(200, {"status": "ok", "start_id": "stamp-1"})] * _CALLERS
    assert backend.start_id_reads == 1


@pytest.mark.asyncio
async def test_a_finished_read_is_reused_for_the_ttl_then_read_again(monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch, live)
    start_id = AsyncMock(side_effect=["stamp-1", "stamp-2"])
    monkeypatch.setattr(live, "_gateway_start_id", start_id)
    callers = _Callers()

    assert await callers.health() == (200, {"status": "ok", "start_id": "stamp-1"})
    clock.advance(live._GATEWAY_START_ID_TTL / 2)
    assert await callers.health() == (200, {"status": "ok", "start_id": "stamp-1"})
    assert start_id.await_count == 1

    clock.advance(live._GATEWAY_START_ID_TTL / 2)
    assert await callers.health() == (200, {"status": "ok", "start_id": "stamp-2"})
    assert start_id.await_count == 2


@pytest.mark.asyncio
async def test_a_read_that_raises_answers_null_and_is_reused_for_the_ttl(monkeypatch, caplog):
    ManualClock().install(monkeypatch, live)
    start_id = AsyncMock(side_effect=RuntimeError("systemctl show timed out"))
    monkeypatch.setattr(live, "_gateway_start_id", start_id)
    callers = _Callers()

    with caplog.at_level(logging.DEBUG, logger=runtime.logger.name):
        assert await callers.health() == (200, {"status": "ok", "start_id": None})
    assert "start identity read failed" in caplog.text
    assert await callers.health() == (200, {"status": "ok", "start_id": None})
    assert start_id.await_count == 1


@pytest.mark.asyncio
async def test_a_cancelled_request_does_not_cancel_the_shared_read(monkeypatch):
    backend = _GatedBackend("stamp-1")
    monkeypatch.setattr(live, "_gateway_backend", lambda: backend)
    callers = _Callers()

    dropped = asyncio.ensure_future(callers.health())
    kept = asyncio.ensure_future(callers.health())
    await _registered(callers, 2)
    await until_parked(backend.release)
    dropped.cancel()
    await asyncio.wait([dropped], timeout=_BOUND)
    assert dropped.cancelled()

    backend.release.set()
    assert await asyncio.wait_for(kept, _BOUND) == (200, {"status": "ok", "start_id": "stamp-1"})
    assert backend.start_id_reads == 1


@pytest.mark.asyncio
async def test_concurrent_service_active_misses_share_one_probe(monkeypatch):
    monkeypatch.setattr(live, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    backend = _GatedBackend()
    monkeypatch.setattr(live, "_gateway_backend", lambda: backend)
    callers = _Callers()

    tasks = [asyncio.ensure_future(callers.service_active()) for _ in range(_CALLERS)]
    await _registered(callers, _CALLERS)
    await until_parked(backend.release)
    assert backend.active_probes == 1

    backend.release.set()
    assert await asyncio.wait_for(asyncio.gather(*tasks), _BOUND) == [True] * _CALLERS
    assert backend.active_probes == 1
    assert live._GATEWAY_SERVICE_ACTIVE is True


@pytest.mark.asyncio
async def test_restart_reads_a_fresh_identity_while_the_health_cache_holds_one(monkeypatch):
    ManualClock().install(monkeypatch, live)
    monkeypatch.setattr(live, "_MAKE_LIVE_COMMITTED", False)
    backend = _GatedBackend("before-restart")
    backend.release.set()
    monkeypatch.setattr(live, "_gateway_backend", lambda: backend)

    assert await live._cached_gateway_start_id() == "before-restart"
    backend.stamp = "fresh"
    out = await live._restart_gateway()

    assert out == {"ok": True, "start_id": "fresh"}
    assert backend.restarts == 1
    assert backend.start_id_reads == 2
    # The health cache still holds its value: the restart did not read through it.
    assert await live._cached_gateway_start_id() == "before-restart"


class _ForeignLoopTask:
    """An unfinished read that belongs to another event loop (a closed one)."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.close()

    def done(self) -> bool:
        return False

    def get_loop(self) -> asyncio.AbstractEventLoop:
        return self._loop


@pytest.mark.asyncio
async def test_a_read_left_in_flight_on_another_loop_is_not_joined(monkeypatch):
    monkeypatch.setattr(live, "_GATEWAY_START_ID_INFLIGHT", _ForeignLoopTask())
    monkeypatch.setattr(live, "_GATEWAY_SERVICE_INFLIGHT", _ForeignLoopTask())
    monkeypatch.setattr(live, "_GATEWAY_SERVICE_ACTIVE", None)
    backend = _GatedBackend("stamp-1")
    backend.release.set()
    monkeypatch.setattr(live, "_gateway_backend", lambda: backend)

    assert await asyncio.wait_for(live._cached_gateway_start_id(), _BOUND) == "stamp-1"
    assert await asyncio.wait_for(live._gateway_service_active(), _BOUND) is True
    assert backend.start_id_reads == 1
    assert backend.active_probes == 1
