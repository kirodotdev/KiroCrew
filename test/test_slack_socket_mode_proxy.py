"""Slack Socket Mode picks its proxy explicitly (``slack/socket_mode_proxy.py``).

websockets 15 prefers a SOCKS proxy for ``wss://`` and needs ``python-socks``
to speak it. On macOS the system proxy settings usually carry HTTP, HTTPS and
SOCKS together, so without an explicit choice the Socket Mode connect failed
with ``ImportError`` although the HTTPS proxy would have worked.

``urllib.request.getproxies`` / ``proxy_bypass`` and the python-socks probe are
replaced at the module seams, so no test depends on the host's proxy settings,
its environment, or whether python-socks happens to be installed.
"""

from __future__ import annotations

import urllib.request
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets

import kiro_crew.slack.socket_mode_proxy as smp
from kiro_crew.slack.events import WSSocketModeClient
from kiro_crew.slack.socket_mode_proxy import SocksProxyUnavailable, select_socket_mode_proxy

WSS = "wss://wss-primary.slack.com/link/?ticket=t&app_id=a"

# What macOS reports with SOCKS, HTTP and HTTPS enabled in System Settings.
MACOS_ALL_THREE = {
    "http": "http://127.0.0.1:7890",
    "https": "http://127.0.0.1:7890",
    "socks": "http://127.0.0.1:7890",
}


@pytest.fixture
def proxies(monkeypatch):
    """Install a getproxies() result and an empty bypass list; return the dict."""
    table: dict[str, str] = {}
    monkeypatch.setattr(urllib.request, "getproxies", lambda: dict(table))
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    return table


@pytest.fixture
def no_python_socks(monkeypatch):
    monkeypatch.setattr(smp, "_socks_supported", lambda: False)


class TestSelectSocketModeProxy:
    def test_macos_socks_falls_back_to_https_proxy_without_python_socks(
        self, proxies, no_python_socks
    ):
        proxies.update(MACOS_ALL_THREE)
        assert select_socket_mode_proxy(WSS) == "http://127.0.0.1:7890"

    def test_socks_is_kept_when_python_socks_is_installed(self, proxies, monkeypatch):
        proxies.update(MACOS_ALL_THREE)
        monkeypatch.setattr(smp, "_socks_supported", lambda: True)
        assert select_socket_mode_proxy(WSS) == "socks5h://127.0.0.1:7890"

    def test_socks_only_raises_an_actionable_error(self, proxies, no_python_socks):
        proxies["socks"] = "http://127.0.0.1:7890"
        with pytest.raises(SocksProxyUnavailable) as caught:
            select_socket_mode_proxy(WSS)
        reason = caught.value.status_reason
        assert "python-socks" in reason and "*.slack.com*" in reason
        assert len(reason) <= 120  # the settings badge's cap

    def test_env_socks_https_proxy_falls_back_to_http_proxy(self, proxies, no_python_socks):
        # The env-var form: HTTPS_PROXY=socks5h://..., HTTP_PROXY=http://...
        proxies.update({"https": "socks5h://127.0.0.1:1080", "http": "http://127.0.0.1:3128"})
        assert select_socket_mode_proxy(WSS) == "http://127.0.0.1:3128"

    def test_env_socks_https_proxy_alone_raises(self, proxies, no_python_socks):
        proxies["https"] = "socks5h://127.0.0.1:1080"
        with pytest.raises(SocksProxyUnavailable):
            select_socket_mode_proxy(WSS)

    def test_plain_https_proxy_is_used_as_is(self, proxies, no_python_socks):
        proxies["https"] = "http://proxy.example:3128"
        assert select_socket_mode_proxy(WSS) == "http://proxy.example:3128"

    def test_no_proxy_connects_directly(self, proxies, no_python_socks):
        assert select_socket_mode_proxy(WSS) is None

    def test_host_only_bypass_pattern_connects_directly(
        self, proxies, no_python_socks, monkeypatch
    ):
        # macOS matches exception patterns against the whole string, so
        # ``*.slack.com`` matches the bare host but not ``host:443``.
        proxies.update(MACOS_ALL_THREE)
        monkeypatch.setattr(
            urllib.request, "proxy_bypass", lambda host: host == "wss-primary.slack.com"
        )
        assert select_socket_mode_proxy(WSS) is None


class TestConnectPassesTheChosenProxy:
    """The gateway's Socket Mode client (``events.WSSocketModeClient``)."""

    @pytest.mark.asyncio
    async def test_macos_socks_settings_connect_through_https_proxy(
        self, proxies, no_python_socks, monkeypatch
    ):
        proxies.update(MACOS_ALL_THREE)
        session = MagicMock()
        session.close = AsyncMock()
        fake_connect = AsyncMock(return_value=session)
        monkeypatch.setattr(websockets, "connect", fake_connect)

        client = WSSocketModeClient(app_token="xapp-test")
        client.wss_uri = WSS
        monkeypatch.setattr(client, "monitor_current_session", AsyncMock())
        monkeypatch.setattr(client, "receive_messages", AsyncMock())
        try:
            await client.connect()
        finally:
            await client.close()

        fake_connect.assert_awaited_once()
        kwargs = fake_connect.await_args.kwargs
        assert kwargs["uri"] == WSS
        assert kwargs["proxy"] == "http://127.0.0.1:7890"
        assert client.current_session is session
