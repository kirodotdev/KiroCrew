"""Tests for _start_site EADDRINUSE retry logic (gateway restart race).

Revision 2 adds:
- A real aiohttp integration test proving the retry actually re-binds and serves.
- A regression test proving that WITHOUT site.stop() the naive retry hits
  RuntimeError('Site ... is already registered').
"""

from __future__ import annotations

import asyncio
import errno
import socket
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web

from kiro_crew.dashboard.server import _start_site


def _no_reclaim() -> AsyncMock:
    """A reclaim stub that reports nothing reclaimable (the common no-op case).

    Injected into the mock-based tests so they never shell out to lsof/ps and
    the EADDRINUSE bookkeeping (warning-once, retry counts) is exercised in
    isolation from the reclaim probe.
    """
    return AsyncMock(return_value="no_holder")

# ---------------------------------------------------------------------------
# Helpers for mock-based tests
# ---------------------------------------------------------------------------


def _make_site() -> AsyncMock:
    """Return a mock TCPSite with async start()/stop() methods."""
    site = AsyncMock()
    site.start = AsyncMock()
    site.stop = AsyncMock()
    return site


def _eaddrinuse() -> OSError:
    exc = OSError(errno.EADDRINUSE, "Address already in use")
    exc.errno = errno.EADDRINUSE
    return exc


# ---------------------------------------------------------------------------
# Mock-based bookkeeping tests (kept from revision 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_site_succeeds_immediately() -> None:
    """Happy path: site.start() works on the first attempt."""
    site = _make_site()
    await _start_site(site, 7777, retries=5, delay=0.01)
    site.start.assert_awaited_once()
    site.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_site_retries_then_succeeds() -> None:
    """Port busy for 3 attempts, then succeeds on the 4th."""
    site = _make_site()
    site.start.side_effect = [
        _eaddrinuse(),
        _eaddrinuse(),
        _eaddrinuse(),
        None,  # succeeds on 4th attempt
    ]
    with patch("kiro_crew.dashboard.server.asyncio.sleep", new_callable=AsyncMock):
        await _start_site(site, 7777, retries=5, delay=0.01, reclaim=_no_reclaim())

    assert site.start.await_count == 4
    # site.stop() called once per failure to unregister
    assert site.stop.await_count == 3


@pytest.mark.asyncio
async def test_start_site_exits_after_exhausting_retries() -> None:
    """All retries exhausted → SystemExit(1)."""
    site = _make_site()
    site.start.side_effect = _eaddrinuse()

    with patch("kiro_crew.dashboard.server.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(SystemExit) as exc_info:
            await _start_site(site, 7777, retries=3, delay=0.01, reclaim=_no_reclaim())

    assert exc_info.value.code == 1
    assert site.start.await_count == 3
    # stop called for each failed attempt
    assert site.stop.await_count == 3


@pytest.mark.asyncio
async def test_start_site_reraises_non_eaddrinuse() -> None:
    """Non-EADDRINUSE OSError is re-raised immediately, no retry."""
    site = _make_site()
    other_err = OSError(errno.EACCES, "Permission denied")
    other_err.errno = errno.EACCES
    site.start.side_effect = other_err

    with pytest.raises(OSError) as exc_info:
        await _start_site(site, 7777, retries=5, delay=0.01)

    assert exc_info.value.errno == errno.EACCES
    site.start.assert_awaited_once()
    site.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_site_logs_warning_once() -> None:
    """A single WARNING is logged on the first EADDRINUSE, not on every retry."""
    site = _make_site()
    site.start.side_effect = [_eaddrinuse(), _eaddrinuse(), None]

    with (
        patch("kiro_crew.dashboard.server.asyncio.sleep", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.server.logger") as mock_logger,
    ):
        await _start_site(site, 7777, retries=5, delay=0.5, reclaim=_no_reclaim())

    # warning called exactly once (on first failure)
    assert mock_logger.warning.call_count == 1
    call_args = mock_logger.warning.call_args[0]
    assert "7777" in str(call_args)
    assert "waiting" in call_args[0].lower()


@pytest.mark.asyncio
async def test_start_site_reclaims_stale_holder_then_binds() -> None:
    """First EADDRINUSE triggers a reclaim; the freed port binds on retry."""
    site = _make_site()
    site.start.side_effect = [_eaddrinuse(), None]  # busy once, then free
    reclaim = AsyncMock(return_value="reclaimed")

    with (
        patch("kiro_crew.dashboard.server.asyncio.sleep", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.server.logger") as mock_logger,
    ):
        await _start_site(site, 7777, retries=5, delay=0.01, reclaim=reclaim)

    reclaim.assert_awaited_once_with(7777)
    assert site.start.await_count == 2
    # The "Reclaimed … rebinding" message is logged, not the "waiting" one.
    warnings = " ".join(str(c) for c in mock_logger.warning.call_args_list)
    assert "Reclaimed" in warnings
    assert "waiting" not in warnings.lower()


@pytest.mark.asyncio
async def test_start_site_reclaim_probe_error_falls_back_to_wait() -> None:
    """A raising reclaim probe never blocks startup — we fall back to wait/retry."""
    site = _make_site()
    site.start.side_effect = [_eaddrinuse(), None]
    reclaim = AsyncMock(side_effect=RuntimeError("lsof blew up"))

    with (
        patch("kiro_crew.dashboard.server.asyncio.sleep", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.server.logger") as mock_logger,
    ):
        await _start_site(site, 7777, retries=5, delay=0.01, reclaim=reclaim)

    reclaim.assert_awaited_once_with(7777)
    assert site.start.await_count == 2
    # Falls back to the original "waiting …" warning.
    warnings = " ".join(str(c) for c in mock_logger.warning.call_args_list)
    assert "waiting" in warnings.lower()


@pytest.mark.asyncio
async def test_start_site_healthy_peer_skips_waiting_message() -> None:
    """When the holder is a live peer, we don't emit the misleading wait message."""
    site = _make_site()
    site.start.side_effect = _eaddrinuse()  # never frees
    reclaim = AsyncMock(return_value="healthy_peer")

    with (
        patch("kiro_crew.dashboard.server.asyncio.sleep", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.server.logger") as mock_logger,
    ):
        with pytest.raises(SystemExit):
            await _start_site(site, 7777, retries=3, delay=0.01, reclaim=reclaim)

    warnings = " ".join(str(c) for c in mock_logger.warning.call_args_list)
    assert "waiting" not in warnings.lower()


# ---------------------------------------------------------------------------
# REAL aiohttp integration test (revision 2) — no mocks, real bind
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Discover an ephemeral port that is currently free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_start_site_real_bind_retry() -> None:
    """Integration: occupy a port, prove _start_site retries and serves HTTP 200.

    This is the test that would have caught revision 1's bug — it uses real
    aiohttp objects and actually binds to the network.
    """
    port = _find_free_port()

    # Occupy the port with a plain TCP socket.
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    occupier.bind(("127.0.0.1", port))
    occupier.listen(1)

    # Build a real aiohttp app + runner + site.
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)

    loop = asyncio.get_running_loop()
    # Release the occupier after 0.4s so the retry can bind.
    loop.call_later(0.4, occupier.close)

    try:
        await _start_site(site, port, retries=40, delay=0.1, reclaim=_no_reclaim())

        # Verify the server actually serves traffic.
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                body = await resp.text()
                assert body == "ok"

        # No leaked site registrations.
        assert len(runner.sites) == 1
    finally:
        await runner.cleanup()
        # Ensure occupier is closed even if test fails early.
        try:
            occupier.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Regression test: prove naive retry WITHOUT stop() raises RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_naive_retry_without_stop_raises_runtime_error() -> None:
    """Proves the exact bug: calling site.start() twice without stop() raises
    RuntimeError('Site ... is already registered') when the port is occupied.

    This locks in the failure mode that revision 1 missed.
    """
    port = _find_free_port()

    # Occupy the port.
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    occupier.bind(("127.0.0.1", port))
    occupier.listen(1)

    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)

    try:
        # First start() should fail with EADDRINUSE.
        with pytest.raises(OSError) as first_exc:
            await site.start()
        assert first_exc.value.errno == errno.EADDRINUSE

        # Naive retry WITHOUT stop() → RuntimeError about re-registration.
        with pytest.raises(RuntimeError, match="already registered"):
            await site.start()
    finally:
        await runner.cleanup()
        try:
            occupier.close()
        except OSError:
            pass
