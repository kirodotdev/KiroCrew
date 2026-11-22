"""Proxy routing across the Connections L1 smoke exchange."""

import json
import threading

import aiohttp
import pytest
from test_connections_l1_smoke import (
    FULL_EXCHANGE,
    FakeSession,
    happy_path,
    provider,
    run_one,
    server,
)
from yarl import URL

from kiro_crew.connections import l1_smoke


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "bypass", "use_proxy"),
    [
        ("https://mcp.example.com/mcp", "unrelated.example.com", True),
        ("https://mcp.example.com/mcp", "mcp.example.com", False),
        ("https://localhost/mcp", "unrelated.example.com", False),
        ("https://127.0.0.1/mcp", "unrelated.example.com", False),
        ("https://[::1]/mcp", "unrelated.example.com", False),
    ],
)
async def test_entire_smoke_exchange_uses_the_endpoint_proxy(monkeypatch, url, bypass, use_proxy):
    proxy = "http://proxy.example.com:3128"
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("no_proxy", bypass)
    monkeypatch.setattr("kiro_crew.connections.l1_smoke.grant_present", lambda *a, **kw: True)
    session = FakeSession(happy_path())
    item = provider()
    item["mcp_url"] = url
    srv = server()
    srv.url = url

    result = await run_one(session, item=item, srv=srv)

    assert result["verdict"] == "PASS"
    assert session.methods == FULL_EXCHANGE
    expected = URL(proxy) if use_proxy else None
    assert [kwargs.get("proxy") for _, kwargs in session.calls] == [expected] * 3


@pytest.mark.asyncio
async def test_system_proxy_lookup_runs_off_the_smoke_event_loop(monkeypatch):
    event_loop_thread = threading.get_ident()
    lookup_threads = []
    proxy = "http://proxy.example.com:3128"

    def resolve(_url):
        lookup_threads.append(threading.get_ident())
        return proxy

    monkeypatch.setattr(l1_smoke, "resolve_proxy", resolve)
    monkeypatch.setattr(l1_smoke, "grant_present", lambda *a, **kw: True)
    session = FakeSession(happy_path())

    result = await run_one(session)

    assert result["verdict"] == "PASS"
    assert session.methods == FULL_EXCHANGE
    assert len(lookup_threads) == len(FULL_EXCHANGE)
    assert all(thread_id != event_loop_thread for thread_id in lookup_threads)
    assert all(kwargs["proxy"] == URL(proxy) for _, kwargs in session.calls)


@pytest.mark.asyncio
async def test_proxy_transport_error_keeps_authorization_out_of_the_smoke_report(monkeypatch):
    monkeypatch.setattr(l1_smoke, "resolve_proxy", lambda _url: "http://proxy.example.com:3128")
    monkeypatch.setattr(l1_smoke, "grant_present", lambda *a, **kw: True)
    session = FakeSession(happy_path())

    def fail_request(_url, **_kwargs):
        raise aiohttp.ClientConnectionError("Proxy-Authorization: Basic probe-auth")

    monkeypatch.setattr(session, "post", fail_request)
    result = await run_one(session)

    assert result["verdict"] == "FAIL"
    assert "probe-auth" not in json.dumps(result)
    assert result["errors"] == ["MCP request through proxy failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proxy",
    [
        "http://test-user:test-password@proxy.example.com:bad",
        "test-user:test-password@proxy.example.com:3128",
    ],
)
async def test_invalid_proxy_url_keeps_credentials_out_of_the_smoke_report(monkeypatch, proxy):
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("no_proxy", "unrelated.example.com")
    monkeypatch.setattr("kiro_crew.connections.l1_smoke.grant_present", lambda *a, **kw: True)
    session = FakeSession(happy_path())

    result = await run_one(session)

    assert result["verdict"] == "FAIL"
    assert result["errors"] == ["invalid MCP smoke proxy URL"]
    assert not session.calls
