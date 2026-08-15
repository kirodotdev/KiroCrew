"""Tests for the Connections OAuth return-address relay."""

from __future__ import annotations

import asyncio
import json
import struct
from socket import SO_LINGER, SOL_SOCKET
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import connections


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:43123/?code=x",
        "http://10.0.0.5:43123/?code=x",
        "http://127.0.0.1:80/?code=x",
        "http://127.0.0.1/?code=x",
        "http://127.0.0.1:43123/",
        "http://user@127.0.0.1:43123/?code=x",
        "http://127.0.0.1:43123/?code=x&unexpected=value",
        "http://127.0.0.1:43123/?code=x#fragment",
        "http://127.0.0.1:43123/?code=x%0d%0aHost:evil",
    ],
)
def test_return_address_validation_rejects_non_loopback_or_incomplete_urls(value):
    assert connections._validated_loopback_return_address(value) is None


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_return_address_validation_accepts_runtime_callback_shape(host):
    value = f"http://{host}:43123/callback?code=one-time&state=opaque"
    callback = connections._validated_loopback_return_address(value)
    assert callback is not None
    assert callback.port == 43123
    assert callback.request_target == "/callback?code=one-time&state=opaque"


@pytest.mark.asyncio
async def test_relay_delivers_to_loopback_without_following_redirects(monkeypatch):
    received: list[dict[str, str]] = []

    async def callback(request: web.Request) -> web.Response:
        received.append(dict(request.query))
        return web.Response(status=200)

    callback_app = web.Application()
    callback_app.router.add_get("/callback", callback)
    callback_server = TestServer(callback_app, host="127.0.0.1")
    await callback_server.start_server()

    relay_app = web.Application()
    relay_app.router.add_post("/api/mcp/oauth/relay", connections.api_mcp_oauth_relay)
    relay_client = TestClient(TestServer(relay_app))
    await relay_client.start_server()
    audit = MagicMock()
    monkeypatch.setattr(connections, "sel", lambda: audit)

    try:
        return_address = str(callback_server.make_url("/callback?code=one-time&state=opaque"))
        response = await relay_client.post(
            "/api/mcp/oauth/relay",
            json={"server": "notion", "redirect_url": return_address},
        )
        assert response.status == 200
        assert await response.json() == {"ok": True}
        assert received == [{"code": "one-time", "state": "opaque"}]
        audit.log_api_access.assert_called_once_with(
            caller="dashboard",
            operation="mcp_oauth_callback_relay",
            outcome="completed",
            resources="notion",
        )
    finally:
        await relay_client.close()
        await callback_server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], "not-an-object"])
async def test_relay_rejects_valid_non_object_json(body):
    relay_app = web.Application()
    relay_app.router.add_post("/api/mcp/oauth/relay", connections.api_mcp_oauth_relay)
    relay_client = TestClient(TestServer(relay_app))
    await relay_client.start_server()
    try:
        response = await relay_client.post(
            "/api/mcp/oauth/relay",
            data=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 400
        assert await response.json() == {
            "error": "request body must be an object",
            "code": "invalid_request_body",
        }
    finally:
        await relay_client.close()


@pytest.mark.asyncio
async def test_relay_rejects_unknown_provider_before_network(monkeypatch):
    relay_app = web.Application()
    relay_app.router.add_post("/api/mcp/oauth/relay", connections.api_mcp_oauth_relay)
    relay_client = TestClient(TestServer(relay_app))
    await relay_client.start_server()
    audit = MagicMock()
    monkeypatch.setattr(connections, "sel", lambda: audit)

    try:
        response = await relay_client.post(
            "/api/mcp/oauth/relay",
            json={"server": "unknown", "redirect_url": "http://127.0.0.1:43123/?code=x"},
        )
        assert response.status == 400
        assert (await response.json())["code"] == "invalid_server"
        audit.log_api_access.assert_not_called()
    finally:
        await relay_client.close()


@pytest.mark.asyncio
async def test_relay_rejects_non_loopback_before_network(monkeypatch):
    relay_app = web.Application()
    relay_app.router.add_post("/api/mcp/oauth/relay", connections.api_mcp_oauth_relay)
    relay_client = TestClient(TestServer(relay_app))
    await relay_client.start_server()
    audit = MagicMock()
    monkeypatch.setattr(connections, "sel", lambda: audit)

    try:
        response = await relay_client.post(
            "/api/mcp/oauth/relay",
            json={"server": "notion", "redirect_url": "http://10.0.0.5:43123/?code=x"},
        )
        assert response.status == 400
        assert (await response.json())["code"] == "invalid_loopback_return_address"
        audit.log_api_access.assert_not_called()
    finally:
        await relay_client.close()


@pytest.mark.asyncio
async def test_relay_sends_bracketed_ipv6_host_header(monkeypatch):
    captured: dict[str, bytes] = {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        captured["request"] = await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    try:
        callback_server = await asyncio.start_server(handle, "::1", 0)
    except OSError:
        pytest.skip("IPv6 loopback unavailable in this environment")
    port = callback_server.sockets[0].getsockname()[1]

    relay_app = web.Application()
    relay_app.router.add_post("/api/mcp/oauth/relay", connections.api_mcp_oauth_relay)
    relay_client = TestClient(TestServer(relay_app))
    await relay_client.start_server()
    audit = MagicMock()
    monkeypatch.setattr(connections, "sel", lambda: audit)

    try:
        response = await relay_client.post(
            "/api/mcp/oauth/relay",
            json={"server": "notion", "redirect_url": f"http://[::1]:{port}/?code=one-time"},
        )
        assert response.status == 200
        assert await response.json() == {"ok": True}
        # RFC 7230 §5.4: IPv6 literals in Host MUST be bracketed.
        assert f"Host: [::1]:{port}".encode("ascii") in captured["request"]
    finally:
        await relay_client.close()
        callback_server.close()
        await callback_server.wait_closed()


async def _post_relay(port: int) -> tuple[int, dict]:
    """Drive the relay endpoint against a loopback port and return (status, body)."""
    relay_app = web.Application()
    relay_app.router.add_post("/api/mcp/oauth/relay", connections.api_mcp_oauth_relay)
    relay_client = TestClient(TestServer(relay_app))
    await relay_client.start_server()
    try:
        response = await relay_client.post(
            "/api/mcp/oauth/relay",
            json={"server": "notion", "redirect_url": f"http://127.0.0.1:{port}/?code=one-time"},
        )
        return response.status, await response.json()
    finally:
        await relay_client.close()


@pytest.mark.asyncio
async def test_a_port_nothing_is_bound_to_is_reported_as_an_expired_approval(monkeypatch):
    """A refused dial means the code is unredeemable, not that the paste was wrong.

    The loopback listener and the PKCE verifier are created by the process that
    minted the authorize URL and die with it, so a port the kernel reports as
    unbound proves the approval is spent. Answering with the delivery-failure
    message sends the user back to re-paste an address that cannot ever work.
    """
    listener = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = int(listener.sockets[0].getsockname()[1])
    listener.close()
    await listener.wait_closed()
    audit = MagicMock()
    monkeypatch.setattr(connections, "sel", lambda: audit)

    status, body = await _post_relay(port)

    assert status == 409
    assert body["code"] == "approval_superseded"
    audit.log_api_access.assert_called_once_with(
        caller="dashboard",
        operation="mcp_oauth_callback_relay",
        outcome="denied",
        resources="notion",
    )


@pytest.mark.asyncio
async def test_a_listener_that_accepts_but_never_answers_stays_a_delivery_failure(monkeypatch):
    """Accepting the connection proves the listener is there, so it is not expired.

    Only a refused dial may reach the expired-approval verdict. A slow or wedged
    listener that already accepted still holds the verifier, so calling its
    approval spent would discard a redeemable one.
    """
    accepted: list[asyncio.StreamWriter] = []

    async def accept_and_stall(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(writer)
        await asyncio.Event().wait()

    listener = await asyncio.start_server(accept_and_stall, "127.0.0.1", 0)
    port = int(listener.sockets[0].getsockname()[1])
    audit = MagicMock()
    monkeypatch.setattr(connections, "sel", lambda: audit)
    # The read bound is what this exercises; shorten it so the test is not paced
    # by the production 5s deadline.
    real_wait_for = asyncio.wait_for

    async def quick_wait_for(awaitable, timeout):  # type: ignore[no-untyped-def]
        return await real_wait_for(awaitable, 0.25 if timeout == 5 else timeout)

    monkeypatch.setattr(connections.asyncio, "wait_for", quick_wait_for)

    try:
        status, body = await _post_relay(port)
    finally:
        for writer in accepted:
            writer.close()
        listener.close()
        await listener.wait_closed()

    assert accepted, "the listener never accepted, so this is not the case under test"
    assert status == 502
    assert body["code"] == "oauth_callback_unreachable"
    assert audit.log_api_access.call_args.kwargs["outcome"] == "failed"


@pytest.mark.asyncio
async def test_a_listener_that_resets_mid_exchange_stays_a_delivery_failure(monkeypatch):
    """A reset after accept is a transport failure against a listener that exists."""

    async def accept_and_reset(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        socket = writer.get_extra_info("socket")
        if socket is not None:
            socket.setsockopt(SOL_SOCKET, SO_LINGER, struct.pack("ii", 1, 0))
        writer.close()

    listener = await asyncio.start_server(accept_and_reset, "127.0.0.1", 0)
    port = int(listener.sockets[0].getsockname()[1])
    audit = MagicMock()
    monkeypatch.setattr(connections, "sel", lambda: audit)

    try:
        status, body = await _post_relay(port)
    finally:
        listener.close()
        await listener.wait_closed()

    assert status == 502
    assert body["code"] == "oauth_callback_unreachable"
    assert audit.log_api_access.call_args.kwargs["outcome"] == "failed"


@pytest.mark.asyncio
async def test_a_non_http_responder_stays_a_delivery_failure(monkeypatch):
    """Something answered, just not in HTTP — a live port, so not an expired approval."""

    async def garble(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"NOT-HTTP hello\r\n")
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(garble, "127.0.0.1", 0)
    port = int(listener.sockets[0].getsockname()[1])
    audit = MagicMock()
    monkeypatch.setattr(connections, "sel", lambda: audit)

    try:
        status, body = await _post_relay(port)
    finally:
        listener.close()
        await listener.wait_closed()

    assert status == 502
    assert body["code"] == "oauth_callback_unreachable"
    assert audit.log_api_access.call_args.kwargs["outcome"] == "failed"


@pytest.mark.asyncio
async def test_a_live_listener_that_rejects_the_code_stays_a_delivery_failure(monkeypatch):
    """A 4xx from the listener proves it is alive, so it keeps the 502 verdict."""

    async def callback(_request: web.Request) -> web.Response:
        return web.Response(status=400)

    callback_app = web.Application()
    callback_app.router.add_get("/", callback)
    callback_server = TestServer(callback_app, host="127.0.0.1")
    await callback_server.start_server()
    audit = MagicMock()
    monkeypatch.setattr(connections, "sel", lambda: audit)

    try:
        status, body = await _post_relay(int(callback_server.port or 0))
    finally:
        await callback_server.close()

    assert status == 502
    assert body["code"] == "oauth_callback_rejected"
    assert audit.log_api_access.call_args.kwargs["outcome"] == "failed"
