"""Tests for the Connections OAuth return-address relay."""

from __future__ import annotations

import asyncio
import json
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
