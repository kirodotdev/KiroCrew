"""Proxy routing across the Connections L1 smoke exchange."""

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
