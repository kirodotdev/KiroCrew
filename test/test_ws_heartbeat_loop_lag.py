"""The dashboard socket keeps a peer whose PONG was queued behind a loop stall.

aiohttp arms its PONG timeout on the loop's clock. When the loop stalls past that
deadline with the peer's PONG already in the kernel, the resumed loop runs the
expired timer before the queued reset, and the stock socket is closed 1006 although
the peer answered at once. ``ws_heartbeat.LagTolerantWebSocketResponse`` starts the
window again in that case, and still drops a peer that sends nothing.

The client here is a raw TCP socket, so the test decides exactly when the PONG
reaches the kernel; the stall is the event loop held by a busy wait on its own
clock, which is what a blocking call on the loop does.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew.dashboard import ws_heartbeat
from kiro_crew.dashboard.ws_heartbeat import LagTolerantWebSocketResponse
from kiro_crew.testing.wait import async_wait_until, await_until

#: A short heartbeat, so the PONG window is a fifth of a second.
_HEARTBEAT_S = 0.4
#: The server's PING: FIN + opcode 0x9, no payload (server frames are unmasked).
_PING = b"\x89\x00"
#: A client PONG: FIN + opcode 0xA, masked, no payload, an all-zero mask key.
_PONG = b"\x8a\x80\x00\x00\x00\x00"
_HANDSHAKE = (
    "GET /ws HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
    "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    "Sec-WebSocket-Version: 13\r\n\r\n"
)


@pytest.fixture
def short_tolerance(_floor_monkeypatch) -> float:
    """A tolerance well inside the short window, so the stall stays short."""
    _floor_monkeypatch.setattr(
        ws_heartbeat, "PONG_LATE_TOLERANCE_S", 0.05  # flake-ok: the stall outlasts it by design
    )
    return 0.05


async def _read_until(reader: asyncio.StreamReader, buffer: bytes, marker: bytes) -> bytes:
    while marker not in buffer:
        chunk = await await_until(asyncio.ensure_future(reader.read(4096)))
        assert chunk, f"the server closed the connection before sending {marker!r}"
        buffer += chunk
    return buffer


async def _socket_waiting_on_a_pong(server_sockets: list[web.WebSocketResponse], server):
    """Open a raw client and read up to the server's first PING."""
    reader, writer = await asyncio.open_connection(server.host, server.port)
    writer.write(_HANDSHAKE.format(host=server.host, port=server.port).encode())
    await writer.drain()
    received = await _read_until(reader, b"", b"\r\n\r\n")
    head, _, rest = received.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 101"), head
    await _read_until(reader, rest, _PING)
    assert len(server_sockets) == 1
    return server_sockets[0], writer


def _stall_loop_past_the_pong_deadline(ws: web.WebSocketResponse, tolerance: float) -> None:
    """Hold the event loop until the PONG deadline is over *tolerance* behind it."""
    loop = asyncio.get_running_loop()
    deadline = ws._pong_response_cb
    assert deadline is not None, "no PONG timeout is armed"
    end = deadline.when() + tolerance * 2
    while loop.time() < end:
        pass


async def _outcome_after_the_stall(ws: web.WebSocketResponse, heartbeat_when: float) -> str:
    """``kept`` once the queued PONG reset the heartbeat, ``closed`` once the socket
    closed; read together, so a later unanswered PING cannot change the answer."""
    return await async_wait_until(
        lambda: "closed" if ws.closed else ("kept" if ws._heartbeat_when > heartbeat_when else "")
    )


async def _served(handler_cls: type[web.WebSocketResponse]):
    sockets: list[web.WebSocketResponse] = []

    async def _serve(request: web.Request) -> web.WebSocketResponse:
        ws = handler_cls(heartbeat=_HEARTBEAT_S)
        await ws.prepare(request)
        sockets.append(ws)
        async for _msg in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/ws", _serve)
    server = TestServer(app)
    await server.start_server()
    return server, sockets


class TestPongQueuedBehindAStall:
    @pytest.mark.asyncio
    async def test_a_pong_sent_in_time_keeps_the_socket_through_the_stall(
        self, short_tolerance
    ) -> None:
        server, sockets = await _served(LagTolerantWebSocketResponse)
        try:
            ws, writer = await _socket_waiting_on_a_pong(sockets, server)
            heartbeat_when = ws._heartbeat_when
            # The peer answers at once; the loop stalls before the server reads it.
            writer.write(_PONG)
            _stall_loop_past_the_pong_deadline(ws, short_tolerance)

            assert await _outcome_after_the_stall(ws, heartbeat_when) == "kept"
            writer.close()
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_the_stock_socket_drops_that_peer(self, short_tolerance) -> None:
        """The aiohttp behaviour the subclass exists for, so an aiohttp that fixes
        it upstream shows up here."""
        server, sockets = await _served(web.WebSocketResponse)
        try:
            ws, writer = await _socket_waiting_on_a_pong(sockets, server)
            heartbeat_when = ws._heartbeat_when
            writer.write(_PONG)
            _stall_loop_past_the_pong_deadline(ws, short_tolerance)

            assert await _outcome_after_the_stall(ws, heartbeat_when) == "closed"
            assert ws.close_code == 1006
            writer.close()
        finally:
            await server.close()


class TestSilentPeer:
    @pytest.mark.asyncio
    async def test_a_peer_that_never_answers_is_still_dropped(self, short_tolerance) -> None:
        server, sockets = await _served(LagTolerantWebSocketResponse)
        try:
            ws, writer = await _socket_waiting_on_a_pong(sockets, server)

            await async_wait_until(lambda: ws.closed)

            assert ws.close_code == 1006
            writer.close()
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_a_peer_silent_through_a_stall_is_dropped_one_window_later(
        self, short_tolerance
    ) -> None:
        server, sockets = await _served(LagTolerantWebSocketResponse)
        try:
            ws, writer = await _socket_waiting_on_a_pong(sockets, server)
            _stall_loop_past_the_pong_deadline(ws, short_tolerance)

            await async_wait_until(lambda: ws.closed)

            assert ws.close_code == 1006
            writer.close()
        finally:
            await server.close()
