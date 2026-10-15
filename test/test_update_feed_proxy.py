"""Update-feed proxy selection without outbound network access."""

import urllib.request
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

# Capture the transport before the suite's network floor replaces this seam.
from kiro_crew.dashboard.handlers.updates import _fetch_feed_bytes

_FEED_URL = "https://updates.example/feed/stable/latest-cli.json"
_HTTPS_PROXY = "http://https-proxy.example:3128"
_HTTP_PROXY = "http://http-proxy.example:3128"


@pytest.fixture
def feed_transport(monkeypatch):
    response = MagicMock(spec=aiohttp.ClientResponse)
    response.status = 200

    async def chunks(_size):
        yield b'{"version":'
        yield b'"0.10.0"}'

    response.content = MagicMock()
    response.content.iter_chunked.side_effect = chunks
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)
    sessions = []
    session_class = aiohttp.ClientSession

    def make_session(*args, **kwargs):
        session = session_class(*args, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", make_session)
    return request, sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proxies", "expected"),
    [
        ({"https": _HTTPS_PROXY, "http": _HTTP_PROXY}, _HTTPS_PROXY),
        ({"https": _HTTPS_PROXY}, _HTTPS_PROXY),
        ({"http": _HTTP_PROXY}, _HTTP_PROXY),
        ({}, None),
    ],
    ids=["https-first", "https-only", "http-fallback", "direct"],
)
async def test_feed_proxy_selection(monkeypatch, feed_transport, proxies, expected):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
    bypass = MagicMock(return_value=False)
    monkeypatch.setattr(urllib.request, "proxy_bypass", bypass)
    request, sessions = feed_transport

    assert await _fetch_feed_bytes(_FEED_URL) == (200, b'{"version":"0.10.0"}')

    request.assert_awaited_once()
    assert request.await_args.args == ("GET", _FEED_URL)
    assert request.await_args.kwargs.get("proxy") == expected
    assert len(sessions) == 1
    assert sessions[0].trust_env is False
    assert sessions[0].closed
    if expected:
        bypass.assert_called_once_with("updates.example")
    else:
        bypass.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("no_proxy", "expected"),
    [
        ("updates.example", None),
        (".example", None),
        ("*", None),
        ("other.example", _HTTPS_PROXY),
    ],
    ids=["exact-host", "domain", "wildcard", "other-host"],
)
async def test_feed_no_proxy(monkeypatch, feed_transport, no_proxy, expected):
    for name in ("http_proxy", "https_proxy", "no_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", _HTTPS_PROXY)
    monkeypatch.setenv("NO_PROXY", no_proxy)
    # Use the environment readers on every OS, independent of system settings.
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(urllib.request, "proxy_bypass", urllib.request.proxy_bypass_environment)
    request, sessions = feed_transport

    assert await _fetch_feed_bytes(_FEED_URL) == (200, b'{"version":"0.10.0"}')

    request.assert_awaited_once()
    assert request.await_args.args == ("GET", _FEED_URL)
    assert request.await_args.kwargs.get("proxy") == expected
    assert sessions[0].trust_env is False
