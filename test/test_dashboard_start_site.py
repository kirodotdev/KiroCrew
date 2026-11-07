"""Tests for _start_site EADDRINUSE retry logic (gateway restart race).

Revision 2 adds:
- A real aiohttp integration test proving the retry actually re-binds and serves.
- A regression test proving that WITHOUT site.stop() the naive retry hits
  RuntimeError('Site ... is already registered').
"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
from unittest.mock import AsyncMock, MagicMock, patch

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


# Helpers for mock-based tests


def _make_site() -> AsyncMock:
    """Return a mock TCPSite with async start()/stop() methods."""
    site = AsyncMock()
    site.start = AsyncMock()
    site.stop = AsyncMock()
    site._server = MagicMock()
    return site


def _eaddrinuse() -> OSError:
    exc = OSError(errno.EADDRINUSE, "Address already in use")
    exc.errno = errno.EADDRINUSE
    return exc


class _PortRecordingSite(web.TCPSite):
    """A real ``TCPSite`` that keeps the kernel-chosen port once it binds.

    The port is read at bind time, so a later listener release cannot hide it.
    """

    bound_port = 0

    async def start(self) -> None:
        await super().start()
        server = self._server
        assert server is not None
        self.bound_port = server.sockets[0].getsockname()[1]


# Mock-based bookkeeping tests (kept from revision 1)


@pytest.mark.asyncio
async def test_start_site_succeeds_immediately() -> None:
    """Happy path: a free port binds on the first attempt and serves."""
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    # Port 0 lets the kernel pick the port at bind time, so no other process
    # can take it between choosing and binding.
    site = _PortRecordingSite(runner, "127.0.0.1", 0)
    reclaim = AsyncMock()
    try:
        await _start_site(site, 0, retries=5, delay=0.01, reclaim=reclaim)
        port = site.bound_port
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert (resp.status, await resp.text()) == (200, "ok")
        assert len(runner.sites) == 1
    finally:
        await runner.cleanup()
    reclaim.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_site_retries_then_succeeds(caplog) -> None:
    """Port busy for 3 attempts, then succeeds on the 4th."""
    site = _make_site()
    site.start.side_effect = [
        _eaddrinuse(),
        _eaddrinuse(),
        _eaddrinuse(),
        None,  # succeeds on 4th attempt
    ]
    server_log = "kiro_crew.dashboard.server"
    with (
        patch("kiro_crew.dashboard.server.asyncio.sleep", new_callable=AsyncMock),
        caplog.at_level(logging.INFO, logger=server_log),
    ):
        await _start_site(site, 7777, retries=5, delay=0.01, reclaim=_no_reclaim())

    # One waiting notice for the whole busy spell, and no give-up error.
    assert [r.getMessage() for r in caplog.records if r.name == server_log] == [
        "Port 7777 in use — waiting up to 0s for the previous gateway to release it…"
    ]
    assert site.start.await_count == 4
    # The partially-started site is released listener-only once per failure:
    # its LISTEN socket is closed and it is unregistered, without TCPSite.stop()
    # firing the application's on_shutdown signals.
    assert site._server.close.call_count == 3
    site.stop.assert_not_awaited()


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
    # released once per failed attempt, listener-only
    assert site._server.close.call_count == 3
    site.stop.assert_not_awaited()


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


_SERVER_LOG = "kiro_crew.dashboard.server"


@pytest.mark.parametrize(
    ("outcome", "says", "suggests_stop"),
    [
        # Another program holds the port: never send the user to stop a gateway
        # that does not exist.
        ("foreign_holder", "held by another program, not a Kiro Crew gateway", False),
        ("healthy_peer", "another Kiro Crew gateway holds it", True),
        ("booting_peer", "another Kiro Crew gateway holds it", True),
        ("reclaim_failed", "another Kiro Crew gateway holds it", True),
        # lsof missing (stock containers): the holder is unknown, so both remedies.
        ("unavailable", "held by another program or another Kiro Crew gateway", True),
    ],
)
@pytest.mark.asyncio
async def test_start_site_give_up_message_follows_the_holder(
    caplog: pytest.LogCaptureFixture, outcome: str, says: str, suggests_stop: bool
) -> None:
    """The final error names what holds the port and always offers --port."""
    site = _make_site()
    site.start.side_effect = _eaddrinuse()
    reclaim = AsyncMock(return_value=outcome)

    with (
        caplog.at_level(logging.INFO, logger=_SERVER_LOG),
        pytest.raises(SystemExit),
    ):
        await _start_site(site, 5476, retries=2, delay=0, reclaim=reclaim)

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert says in errors[0]
    assert "kirocrew gateway --port 5477" in errors[0]
    assert "KIROCREW_PORT=5477" in errors[0]
    assert "systemctl" not in errors[0]
    assert ("kirocrew stop" in errors[0]) is suggests_stop
    if outcome == "unavailable":
        waits = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert waits == [
            "Port 5476 in use by an unidentified process — waiting up to 0s in case it is released…"
        ]


@pytest.mark.asyncio
async def test_reserve_port_give_up_message_names_a_foreign_holder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reservation path gives the same holder-aware message as _start_site."""
    from kiro_crew.dashboard.server import _reserve_dashboard_port

    with (
        patch("kiro_crew.dashboard.server._bind_once", side_effect=_eaddrinuse()),
        caplog.at_level(logging.INFO, logger=_SERVER_LOG),
        pytest.raises(SystemExit),
    ):
        await _reserve_dashboard_port(
            "127.0.0.1",
            65535,
            retries=2,
            delay=0,
            reclaim=AsyncMock(return_value="foreign_holder"),
        )

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "held by another program, not a Kiro Crew gateway" in errors[0]
    # The top port has no +1, so the suggestion steps down instead.
    assert "kirocrew gateway --port 65534" in errors[0]
    assert "kirocrew stop" not in errors[0]


# REAL aiohttp integration test (revision 2) — no mocks, real bind


def _occupy_free_port() -> tuple[socket.socket, int]:
    """Bind a listening socket to a kernel-chosen port and return it with the port.

    Binding port 0 directly leaves no gap in which another process could take
    the port before the test holds it.
    """
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    return occupier, occupier.getsockname()[1]


@pytest.mark.asyncio
async def test_start_site_real_bind_retry() -> None:
    """Integration: occupy a port, prove _start_site retries and serves HTTP 200.

    This is the test that would have caught revision 1's bug — it uses real
    aiohttp objects and actually binds to the network.
    """
    occupier, port = _occupy_free_port()

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


# Regression test: prove naive retry WITHOUT stop() raises RuntimeError


@pytest.mark.asyncio
async def test_boot_retry_releases_the_listener_without_shutting_the_app_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EADDRINUSE retry frees the port listener-only, never via ``TCPSite.stop``.

    On aiohttp 3.9/3.10 ``stop()`` also runs ``on_shutdown`` signals, which would tear
    down an app that has not started serving.
    """
    occupier, port = _occupy_free_port()

    fired: list[str] = []

    async def _on_shutdown(_app: web.Application) -> None:
        fired.append("app")

    async def _forbidden_stop(self: web.TCPSite) -> None:
        raise AssertionError("the boot retry must not call TCPSite.stop")

    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    app.on_shutdown.append(_on_shutdown)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)

    loop = asyncio.get_running_loop()
    loop.call_later(0.4, occupier.close)

    try:
        with monkeypatch.context() as mp:
            mp.setattr(web.TCPSite, "stop", _forbidden_stop, raising=False)
            await _start_site(site, port, retries=40, delay=0.1, reclaim=_no_reclaim())
            assert len(runner.sites) == 1
            assert fired == []
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/") as resp:
                    assert resp.status == 200
    finally:
        # Undone before cleanup: runner.cleanup() legitimately stops the sites.
        await runner.cleanup()
        try:
            occupier.close()
        except OSError:
            pass


@pytest.mark.asyncio
async def test_naive_retry_without_stop_raises_runtime_error() -> None:
    """Proves the exact bug: calling site.start() twice without stop() raises
    RuntimeError('Site ... is already registered') when the port is occupied.

    This locks in the failure mode that revision 1 missed.
    """
    occupier, port = _occupy_free_port()

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
