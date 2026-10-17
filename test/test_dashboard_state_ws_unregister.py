"""``api_ws`` unregisters the socket it registered, however the handler ends.

This file pins that a connect frame ended by a ``BaseException`` still reaches the
handler's unregistering ``finally``, and that twenty clients which reset the TCP
connection or close cleanly while the connect frames are still being sent leave
both WebSocket registries empty. It runs a real aiohttp test server over the
``echo_state`` fixture and resets raw TCP connections, which no sibling needs.
The other DashboardState WebSocket tests live in ``test_dashboard_state_ws.py``
and ``test_dashboard_state_ws_status_refresh.py``, and the autouse event-loop
fixture in ``dashboard_state_ws_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from unittest.mock import MagicMock

import pytest
from dashboard_state_ws_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    sync_event_loop,
)
from test_chat_send_echo_scope import echo_state  # noqa: F401  (fixture, used by name)

from kiro_crew.testing.wait import async_wait_until, await_until


class TestWebSocketUnregisteredOnEveryExit:
    """``api_ws`` unregisters the socket it registered, however the handler ends.

    The connect-time frames are sent after the socket is registered and under an
    ``except Exception`` that lets the handler carry on. A ``BaseException`` there --
    the ``CancelledError`` a server with handler cancellation delivers when the
    client goes away, or a ``GeneratorExit`` -- must still end in the ``finally``
    that unregisters; a socket that skips it stays in ``_ws_clients`` and
    ``_owner_ws_clients`` for the life of the gateway.
    """

    @pytest.mark.parametrize("ending", [asyncio.CancelledError, GeneratorExit])
    @pytest.mark.asyncio
    async def test_a_connect_frame_ended_by_a_base_exception_unregisters(
        self, monkeypatch, ending
    ) -> None:
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.return_value = []
        state._yolo = False
        state._folders = []
        state.folders_generation.return_value = 0

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OWNER", "app": "", "is_dashboard_user": True})
                self.app = {"state": state}

        class FakeWebSocket(dict):
            closed = False

            async def prepare(self, request) -> None:
                return None

            async def send_str(self, payload: str) -> None:
                raise ending()

            async def send_json(self, payload: dict) -> None:
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", MagicMock())
        monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())

        with contextlib.suppress(asyncio.CancelledError, GeneratorExit):
            await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))

        state.register_ws.assert_called_once()
        state.unregister_ws.assert_called_once_with(fake_ws)

    @pytest.mark.parametrize("disconnect", ["abort", "clean_close"])
    @pytest.mark.asyncio
    async def test_twenty_disconnects_during_the_connect_frames_leave_no_socket_registered(
        self, echo_state, monkeypatch, disconnect: str  # noqa: F811
    ) -> None:
        """Twenty clients each go away while the handler is still sending its connect
        frames: an abort (a closed tab, a dropped network) or, as the control, a clean
        close. aiohttp's test server cancels a handler whose client is gone, so an
        abort lands in the connect block as a ``CancelledError``. Every socket must
        leave both registries."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        from kiro_crew.dashboard.ws import api_ws

        state = echo_state
        connect_frames_held = asyncio.Event()

        async def _held_members_baseline(ws) -> None:
            # The connect block's second frame waits, so each disconnect lands
            # inside the connect block rather than in the main receive loop.
            await await_until(connect_frames_held)

        monkeypatch.setattr(state, "send_members_subscribed", _held_members_baseline)
        if disconnect == "clean_close":
            # The control closes from the main receive loop, so nothing is held.
            connect_frames_held.set()
        app = _make_app(state)
        app["allowed_origins"] = set()
        app.router.add_get("/api/ws", api_ws)
        try:
            async with TestClient(TestServer(app)) as client:
                host, port = client.server.host, client.server.port
                origin = f"http://{host}:{port}"
                app["allowed_origins"].add(origin)
                for _ in range(20):
                    if disconnect == "abort":
                        await _abort_after_the_connect_snapshot(host, port, origin)
                    else:
                        socket_ = await client.ws_connect("/api/ws", headers={"Origin": origin})
                        frame = await await_until(asyncio.ensure_future(socket_.receive_json()))
                        assert frame["type"] == "slots"
                        await socket_.close()
                await async_wait_until(
                    lambda: not state._ws_clients and not state._owner_ws_clients
                )
                left = (len(state._ws_clients), len(state._owner_ws_clients))
        finally:
            connect_frames_held.set()
        assert left == (0, 0), f"sockets still registered after 20 {disconnect}s: {left}"


async def _abort_after_the_connect_snapshot(host: str, port: int, origin: str) -> None:
    """Upgrade, read the connect snapshot, then reset the TCP connection: no close frame."""
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        (
            f"GET /api/ws HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    received = b""
    while b"slots" not in received:
        chunk = await await_until(asyncio.ensure_future(reader.read(4096)))
        assert chunk, "the server closed the connection before its connect snapshot"
        received += chunk
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()
