"""Regression tests for the slowloris / CWE-400 read-timeout mitigation.

Covers the connection-level guard in ``kiro_crew.dashboard.slowloris`` and its
wiring into the dashboard / API server start paths:

- a connection that dribbles the request line/headers without ever completing
  them is force-closed after ``header_read_timeout`` (the slowloris vector);
- a well-formed request is served normally and its keep-alive connection is not
  spuriously reaped after the (one-shot, pre-handler) deadline elapses;
- ``build_hardened_runner`` actually wires the hardened server + timeouts;
- both ``start_dashboard`` and ``start_api_server`` use it.
"""

from __future__ import annotations

import asyncio
import errno
import socket

import aiohttp
import pytest
from aiohttp import web

from kiro_crew.dashboard import server as dashboard_server
from kiro_crew.dashboard.slowloris import (
    SlowlorisAppRunner,
    SlowlorisRequestHandler,
    SlowlorisServer,
    build_hardened_runner,
    reject_compressed_body_middleware,
)


async def _make_app() -> web.Application:
    app = web.Application()

    async def _ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", _ok)
    return app


@pytest.mark.asyncio
async def test_slow_header_read_is_force_closed() -> None:
    """A client that never finishes sending headers is dropped at the deadline."""
    runner = build_hardened_runner(
        await _make_app(), header_read_timeout=0.3, keepalive_timeout=75.0
    )
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    try:
        reader, writer = await asyncio.open_connection(host, port)
        # Partial request line + no terminating CRLFCRLF -> headers never complete.
        writer.write(b"GET / HTTP/1.1\r\n")
        await writer.drain()
        # The guard force-closes the connection; the peer read then sees EOF.
        data = await asyncio.wait_for(reader.read(), timeout=3.0)
        assert data == b""
        writer.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_complete_request_served_and_keepalive_not_reaped() -> None:
    """A well-formed request succeeds; the deadline never touches a live conn."""
    runner = build_hardened_runner(
        await _make_app(), header_read_timeout=0.3, keepalive_timeout=75.0
    )
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://{host}:{port}/") as resp:
                assert resp.status == 200
                assert await resp.text() == "ok"
            # Idle past the header deadline, then reuse the connection: the
            # one-shot pre-handler guard must not have closed it.
            await asyncio.sleep(0.6)
            async with session.get(f"http://{host}:{port}/") as resp2:
                assert resp2.status == 200
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_build_hardened_runner_wires_server_and_timeouts() -> None:
    """The factory installs the hardened server and forwards both timeouts."""
    runner = build_hardened_runner(
        await _make_app(), header_read_timeout=12.0, keepalive_timeout=34.0
    )
    assert isinstance(runner, SlowlorisAppRunner)
    # keepalive_timeout is forwarded to RequestHandler via the runner kwargs.
    assert runner._kwargs.get("keepalive_timeout") == 34.0
    await runner.setup()
    try:
        server = runner.server
        assert isinstance(server, SlowlorisServer)
        assert server._header_read_timeout == 12.0
        # The protocol factory yields our hardened handler with both knobs set.
        handler = server()
        assert isinstance(handler, SlowlorisRequestHandler)
        assert handler._srh_timeout == 12.0
        assert handler.keepalive_timeout == 34.0
    finally:
        await runner.cleanup()


def test_start_paths_use_hardened_runner() -> None:
    """Both server start paths import and use the hardened runner factory."""
    # Imported into the server module namespace -> both call sites resolve to it.
    assert dashboard_server.build_hardened_runner is build_hardened_runner
    import inspect

    src = inspect.getsource(dashboard_server)
    # Neither start path may fall back to a bare web.AppRunner(app).
    assert "web.AppRunner(app)" not in src
    # Both start paths call the hardened runner. Match the call prefix (not a
    # fixed closing paren) so passing extra kwargs — e.g.
    # max_field_size=_MAX_HEADER_FIELD_SIZE — still satisfies the invariant.
    assert src.count("build_hardened_runner(app") == 2


class _RacedClosedSocket:
    """Socket stub that fails setsockopt like a raced-closed fd.

    Mirrors the macOS failure where the peer closes between ``accept()`` and
    ``connection_made``: every ``setsockopt`` raises ``EINVAL``. aiohttp's
    ``tcp_nodelay`` (base-protocol path) suppresses its own OSError, but
    ``tcp_keepalive`` (request-handler path) does not — that unguarded call is
    the one this regression pins.
    """

    family = socket.AF_INET

    def setsockopt(self, level: int, optname: int, value: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")


class _RacedClosedTransport(asyncio.Transport):
    """Transport whose extra-info socket raises on setsockopt; records close."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "socket":
            return _RacedClosedSocket()
        return default

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


@pytest.mark.asyncio
async def test_connection_made_oserror_is_contained() -> None:
    """A socket that raced closed before setup is dropped without an escape.

    aiohttp's base ``connection_made`` runs ``tcp_keepalive`` unguarded; on a
    raced-closed socket the ``setsockopt`` OSError would otherwise escape the
    transport callback as an unhandled asyncio exception dump. The guard must
    swallow it, close the dead transport, and leave the deadline unarmed.
    """
    runner = build_hardened_runner(
        await _make_app(), header_read_timeout=30.0, keepalive_timeout=75.0
    )
    await runner.setup()
    try:
        server = runner.server
        assert isinstance(server, SlowlorisServer)
        handler = server()
        assert isinstance(handler, SlowlorisRequestHandler)
        transport = _RacedClosedTransport()

        # Must not raise despite the base call failing on setsockopt.
        handler.connection_made(transport)

        # The connection is treated as dead: transport closed, deadline never
        # armed, and no request-serving task was started for it.
        assert transport.closed
        assert handler._srh_handle is None
        assert handler._task_handler is None
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_connection_made_non_oserror_still_escapes() -> None:
    """The guard is scoped to OSError; other exceptions must propagate."""
    runner = build_hardened_runner(
        await _make_app(), header_read_timeout=30.0, keepalive_timeout=75.0
    )
    await runner.setup()
    try:
        server = runner.server
        assert isinstance(server, SlowlorisServer)
        handler = server()

        class _Boom(asyncio.Transport):
            def get_extra_info(self, name: str, default: object = None) -> object:
                if name == "socket":
                    raise RuntimeError("not an OSError")
                return default

        with pytest.raises(RuntimeError):
            handler.connection_made(_Boom())
    finally:
        await runner.cleanup()


# ── Request-body decompression hardening (event-loop wedge, CWE-400) ──────────
#
# aiohttp's parser inflates a Content-Encoding request body synchronously on
# the event loop (DeflateBuffer.feed_data -> decompress_sync); one
# compression-bomb request can stall the loop past the loop-stall watchdog and
# kill the gateway. The hardened runner therefore runs with
# auto_decompress=False, and the paired middleware answers such requests with
# an explicit 415.


def _gzip_body() -> tuple[bytes, int]:
    """A small gzip payload plus its decompressed length (they must differ)."""
    import gzip

    raw = b"x" * 4096
    compressed = gzip.compress(raw)
    assert len(compressed) != len(raw)
    return compressed, len(raw)


@pytest.mark.asyncio
async def test_hardened_runner_disables_auto_decompress() -> None:
    """The runner forces auto_decompress off and the handler protocol factory
    receives it — including through the TypeError failsafe."""
    runner = build_hardened_runner(await _make_app())
    assert runner._kwargs.get("auto_decompress") is False
    # The failsafe kwarg strip must keep the security knob rather than
    # silently re-enabling decompression.
    await runner.setup()
    try:
        server = runner.server
        assert isinstance(server, SlowlorisServer)
        kept = {k for k in server._kwargs if k in ("debug", "access_log_class", "auto_decompress")}
        assert "auto_decompress" in kept
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_compressed_body_is_not_inflated_server_side() -> None:
    """With the hardened runner, a gzip body reaches the handler as raw bytes.

    This is the structural proof that no DeflateBuffer ran on the loop: were
    auto_decompress still on, the handler would see the decompressed length.
    """
    compressed, raw_len = _gzip_body()
    app = web.Application()
    seen: dict[str, int] = {}

    async def _echo_len(request: web.Request) -> web.Response:
        seen["len"] = len(await request.read())
        return web.Response(text="ok")

    app.router.add_post("/", _echo_len)
    runner = build_hardened_runner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://{host}:{port}/",
                data=compressed,
                headers={"Content-Encoding": "gzip"},
                # The client must not renegotiate the body we hand it.
                compress=False,
            ) as resp:
                assert resp.status == 200
        assert seen["len"] == len(compressed)
        assert seen["len"] != raw_len
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_middleware_rejects_compressed_body_with_415(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Content-Encoding body gets an explicit 415 before any handler runs."""
    compressed, _ = _gzip_body()
    app = web.Application(middlewares=[reject_compressed_body_middleware])
    handled: list[str] = []

    async def _handler(_request: web.Request) -> web.Response:
        handled.append("ran")
        return web.Response(text="ok")

    app.router.add_post("/", _handler)
    runner = build_hardened_runner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://{host}:{port}/",
                data=compressed,
                headers={"Content-Encoding": "gzip"},
                compress=False,
            ) as resp:
                assert resp.status == 415
                body = await resp.json()
                assert body["error"] == "unsupported_content_encoding"
            assert handled == []
            # The rejection identifies the sender (peer/method/path/encoding)
            # so an unexpected legitimate client is diagnosable from logs.
            reject_logs = [
                r for r in caplog.records if "rejected compressed request body" in r.getMessage()
            ]
            assert len(reject_logs) == 1
            message = reject_logs[0].getMessage()
            assert "peer=127.0.0.1" in message
            assert "method=POST" in message
            assert "path=/" in message
            assert "content_encoding=gzip" in message
            # Plain requests pass through untouched.
            async with session.post(f"http://{host}:{port}/", data=b"plain") as resp2:
                assert resp2.status == 200
            assert handled == ["ran"]
            # identity is a no-op encoding and must not be rejected.
            async with session.post(
                f"http://{host}:{port}/",
                data=b"plain",
                headers={"Content-Encoding": "identity"},
            ) as resp3:
                assert resp3.status == 200
            # A stray header on a body-less request is harmless: nothing to
            # decompress, so it passes.
            async with session.get(
                f"http://{host}:{port}/x",
                headers={"Content-Encoding": "gzip"},
            ) as resp4:
                assert resp4.status == 404
    finally:
        await runner.cleanup()


def test_start_paths_use_compressed_body_middleware() -> None:
    """Both server start paths wire the 415 middleware into their app."""
    assert dashboard_server.reject_compressed_body_middleware is reject_compressed_body_middleware
    import inspect

    src = inspect.getsource(dashboard_server)
    # Three occurrences: the module import plus one per middleware list
    # (dashboard + headless API server).
    assert src.count("reject_compressed_body_middleware,") == 3
