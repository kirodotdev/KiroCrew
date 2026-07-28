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

import aiohttp
import pytest
from aiohttp import web

from kiro_crew.dashboard import server as dashboard_server
from kiro_crew.dashboard.slowloris import (
    SlowlorisAppRunner,
    SlowlorisRequestHandler,
    SlowlorisServer,
    build_hardened_runner,
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
