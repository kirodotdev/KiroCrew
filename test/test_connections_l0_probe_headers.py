"""Tests for the Connections L0 probe's HTTP request handling.

Kept out of ``test_connections_l0_probe.py`` because that file forbids building a
real HTTP client. This one builds the real client against a loopback server, so
the header limit under test is aiohttp's own parser, not a fake. Still offline.
"""

import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_connections_l0_probe import (
    AUTHORIZATION_URL,
    RESOURCE_URL,
    FakeResponse,
    FakeSession,
    provider,
    routes,
)
from yarl import URL

from kiro_crew.connections import l0_probe
from kiro_crew.testing.wait import default_timeout

# Mixpanel's issuer sends a ~12 KB Content-Security-Policy; aiohttp's default
# header line limit is 8190 bytes.
LARGE_HEADER_VALUE = "default-src 'self' " + "https://a.example.com " * 540


async def _metadata(_request: web.Request) -> web.Response:
    return web.json_response(
        {"issuer": "https://auth.example.com"},
        headers={"Content-Security-Policy": LARGE_HEADER_VALUE},
    )


@pytest.mark.asyncio
async def test_a_large_response_header_does_not_hide_the_metadata_document():
    assert 8190 < len(LARGE_HEADER_VALUE) < l0_probe._MAX_HEADER_FIELD_BYTES
    app = web.Application()
    app.router.add_get("/.well-known/oauth-authorization-server", _metadata)
    async with TestServer(app, host="127.0.0.1") as server:
        url = str(server.make_url("/.well-known/oauth-authorization-server"))
        async with l0_probe.client_session() as session:
            document = await l0_probe._get_json(session, url, 10.0)
    assert document == {"issuer": "https://auth.example.com"}


@pytest.mark.asyncio
async def test_metadata_requests_apply_proxy_and_bypass_per_destination(monkeypatch):
    proxy = "http://proxy.example.com:3128"
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("no_proxy", "auth.example.com")
    session = FakeSession(routes())
    requests = []
    get = session.get

    def record_proxy(url, **kwargs):
        requests.append((url, kwargs.get("proxy")))
        return get(url, **kwargs)

    monkeypatch.setattr(session, "get", record_proxy)

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=default_timeout())

    assert result["ok"]
    assert requests == [(RESOURCE_URL, URL(proxy)), (AUTHORIZATION_URL, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proxy",
    [
        "http://test-user:test-password@proxy.example.com:bad",
        "test-user:test-password@proxy.example.com:3128",
    ],
)
async def test_invalid_proxy_url_keeps_credentials_out_of_the_report(monkeypatch, proxy):
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("no_proxy", "auth.example.com")

    async with l0_probe.client_session() as session:
        result = await l0_probe.probe_provider(
            session, provider(), timeout_seconds=default_timeout()
        )

    assert not result["ok"]
    assert result["errors"]
    assert all("invalid metadata proxy URL" in error for error in result["errors"])
    assert all("test-password" not in error for error in result["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [RESOURCE_URL, AUTHORIZATION_URL])
async def test_proxy_failure_keeps_credentials_out_of_the_report(monkeypatch, destination):
    monkeypatch.setenv("https_proxy", "http://probe-user:probe-password@proxy.example.com:3128")
    monkeypatch.setenv("no_proxy", "")
    session = FakeSession(routes())
    get = session.get

    def fail_proxied_request(url, **kwargs):
        if url == destination:
            raise aiohttp.ClientConnectionError(
                "http://probe-user:probe-password@proxy.example.com/?token=probe-token "
                "Proxy-Authorization: Basic probe-auth"
            )
        return get(url, **kwargs)

    monkeypatch.setattr(session, "get", fail_proxied_request)
    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=default_timeout())

    assert not result["ok"]
    report = json.dumps(result)
    for secret in ("probe-user", "probe-password", "probe-token", "probe-auth"):
        assert secret not in report
    assert any("metadata request through proxy failed" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_proxy_preserves_safe_response_diagnostic(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.example.com:3128")
    monkeypatch.setenv("no_proxy", "")
    session = FakeSession(routes())
    session.routes[RESOURCE_URL] = FakeResponse(
        200, headers={"Content-Encoding": "gzip; token=probe-token"}
    )

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=default_timeout())

    assert not result["ok"]
    assert any("unsupported Content-Encoding" in error for error in result["errors"])
    assert "probe-token" not in json.dumps(result)
