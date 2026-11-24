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
    # connect reads the OS settings through _os_proxies when no proxy variable is set.
    import kiro_crew.http_proxy as http_proxy

    monkeypatch.setattr(http_proxy, "_os_proxies", lambda: dict(table))
    monkeypatch.setattr(http_proxy, "_os_bypass", lambda: None)
    return table


@pytest.fixture
def no_python_socks(monkeypatch):
    monkeypatch.setattr(smp, "_socks_supported", lambda: False)


@pytest.fixture
def env_sourced(proxies, monkeypatch):
    """Present the table as the operator's environment, not the OS settings.

    A SOCKS-only pick raises only for an environment proxy: an OS-sourced map
    goes through the Web API's filter and never raises.
    """
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: dict(proxies))
    return proxies


class TestSelectSocketModeProxy:
    def test_macos_socks_falls_back_to_https_proxy_without_python_socks(
        self, proxies, no_python_socks
    ):
        proxies.update(MACOS_ALL_THREE)
        assert select_socket_mode_proxy(WSS) == "http://127.0.0.1:7890"

    def test_socks_is_kept_when_python_socks_is_installed(self, env_sourced, monkeypatch):
        env_sourced.update(MACOS_ALL_THREE)
        monkeypatch.setattr(smp, "_socks_supported", lambda: True)
        assert select_socket_mode_proxy(WSS) == "socks5h://127.0.0.1:7890"

    def test_no_proxy_alone_uses_the_os_proxy_the_web_api_uses(
        self, proxies, monkeypatch, no_python_socks
    ):
        # The stdlib's getproxies() returns {"no": ...} here and skips the OS.
        import kiro_crew.http_proxy as http_proxy

        proxies["no"] = "example.test"
        monkeypatch.setattr(
            urllib.request, "getproxies_environment", lambda: {"no": "example.test"}
        )
        monkeypatch.setattr(http_proxy, "_os_proxies", lambda: dict(MACOS_ALL_THREE))
        monkeypatch.setattr(http_proxy, "_os_bypass", lambda: None)  # never the host's own list
        assert select_socket_mode_proxy(WSS) == "http://127.0.0.1:7890"
        assert http_proxy.resolve_proxy(http_proxy.SLACK_API_URL) == "http://127.0.0.1:7890"

    @pytest.mark.parametrize(
        ("source", "socks_map"),
        [
            ("env", {"socks": "http://127.0.0.1:7890"}),
            ("os-windows", {"socks": "socks://h:1080", "https": "socks4://h:1080"}),
            ("os-macos", {"socks": "http://h:1080"}),
        ],
    )
    def test_socks_only_raises_an_actionable_error(
        self, proxies, monkeypatch, no_python_socks, source, socks_map
    ):
        # A configured proxy no client can speak is an error, never a direct connection.
        proxies.update(socks_map)
        if source == "env":
            monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: dict(proxies))
        else:
            monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
        with pytest.raises(SocksProxyUnavailable) as caught:
            select_socket_mode_proxy(WSS)
        if source != "env":
            with pytest.raises(SocksProxyUnavailable):
                smp.select_socket_mode_proxy_from_os(WSS, {})
        reason = caught.value.status_reason
        assert "python-socks" in reason and "*.slack.com*" in reason
        assert len(reason) <= 120  # the settings badge's cap

    def test_env_socks_https_proxy_falls_back_to_http_proxy(self, proxies, no_python_socks):
        # The env-var form: HTTPS_PROXY=socks5h://..., HTTP_PROXY=http://...
        proxies.update({"https": "socks5h://127.0.0.1:1080", "http": "http://127.0.0.1:3128"})
        assert select_socket_mode_proxy(WSS) == "http://127.0.0.1:3128"

    def test_env_socks_https_proxy_alone_raises(self, env_sourced, no_python_socks):
        env_sourced["https"] = "socks5h://127.0.0.1:1080"
        with pytest.raises(SocksProxyUnavailable):
            select_socket_mode_proxy(WSS)

    def test_plain_https_proxy_is_used_as_is(self, proxies, no_python_socks):
        proxies["https"] = "http://proxy.example:3128"
        assert select_socket_mode_proxy(WSS) == "http://proxy.example:3128"

    def test_an_http_only_os_proxy_carries_the_websocket_and_the_web_api(
        self, proxies, monkeypatch, no_python_socks
    ):
        # websockets skips an http entry for wss://; resolve_proxy takes it.
        import kiro_crew.http_proxy as http_proxy

        monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
        proxies["http"] = "http://proxy:3128"
        monkeypatch.setattr(http_proxy, "_os_proxies", lambda: dict(proxies))
        monkeypatch.setattr(http_proxy, "_os_bypass", lambda: None)
        web_api = http_proxy.resolve_proxy(http_proxy.SLACK_API_URL)
        assert web_api == "http://proxy:3128"
        assert select_socket_mode_proxy(WSS) == web_api

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


# A Windows registry map with a `socks=` entry, as getproxies_registry returns it.
REGISTRY_SOCKS = {"socks": "socks://h:1080", "http": "socks4://h:1080", "https": "socks4://h:1080"}
SLACK_API = "https://slack.com/api/"


class TestOneRoutePerSource:
    """An OS map reaches both Slack legs through one filter; the environment is as written."""

    @pytest.fixture
    def os_map(self, monkeypatch):
        import kiro_crew.http_proxy as http_proxy

        table: dict[str, str] = {}
        monkeypatch.setattr(
            urllib.request, "getproxies_environment", lambda: {"no": "example.test"}
        )
        monkeypatch.setattr(http_proxy, "_os_proxies", lambda: dict(table))
        monkeypatch.setattr(http_proxy, "_os_bypass", lambda: None)
        monkeypatch.setattr(http_proxy, "_warned_schemes", set())
        return table

    def test_a_registry_socks_only_entry_raises_for_socket_mode_and_warns_for_the_web_api(
        self, os_map, caplog, no_python_socks
    ):
        import kiro_crew.http_proxy as http_proxy

        os_map.update(REGISTRY_SOCKS)
        with caplog.at_level("WARNING", logger=http_proxy.logger.name):
            with pytest.raises(SocksProxyUnavailable):
                select_socket_mode_proxy(WSS)
            with pytest.raises(SocksProxyUnavailable):
                smp.select_socket_mode_proxy_from_os(WSS, {"no": "example.test"})
            # The Web API cannot speak SOCKS: one warning and direct, as on main.
            assert http_proxy.resolve_proxy(SLACK_API) is None
        assert len([r for r in caplog.records if "system proxy" in r.getMessage()]) == 1

    @pytest.mark.parametrize(
        ("socks_map", "expected"),
        [
            (REGISTRY_SOCKS, "socks4://h:1080"),
            ({"socks": "http://h:1080"}, "socks5h://h:1080"),
            ({"socks": "socks://h:1080"}, "socks5h://h:1080"),
        ],
        ids=["windows", "macos", "windows-socks-key-only"],
    )
    def test_an_os_socks_only_entry_is_kept_with_python_socks(
        self, os_map, monkeypatch, socks_map, expected
    ):
        monkeypatch.setattr(smp, "_socks_supported", lambda: True)
        os_map.update(socks_map)
        assert select_socket_mode_proxy(WSS) == expected
        assert smp.select_socket_mode_proxy_from_os(WSS, {"no": "example.test"}) == expected

    def test_an_empty_os_map_connects_direct(self, os_map, no_python_socks):
        assert select_socket_mode_proxy(WSS) is None
        assert smp.select_socket_mode_proxy_from_os(WSS, {"no": "example.test"}) is None

    def test_an_http_entry_beside_a_registry_socks_entry_is_taken(self, os_map, no_python_socks):
        # http=...;socks=...: the stdlib's socks4:// rewrite must not shadow the HTTP entry.
        os_map.update(
            {"socks": "socks://h:1080", "https": "socks4://h:1080", "http": "http://p:3128"}
        )
        assert select_socket_mode_proxy(WSS) == "http://p:3128"
        assert smp.select_socket_mode_proxy_from_os(WSS, {"no": "example.test"}) == "http://p:3128"

    def test_a_usable_https_entry_beside_it_carries_both_legs(self, os_map, monkeypatch):
        import kiro_crew.http_proxy as http_proxy

        # Even with python-socks importable, the OS socks= entry is not used.
        monkeypatch.setattr(smp, "_socks_supported", lambda: True)

        os_map.update(REGISTRY_SOCKS, https="http://p:3128")
        assert select_socket_mode_proxy(WSS) == "http://p:3128"
        assert smp.select_socket_mode_proxy_from_os(WSS, {"no": "example.test"}) == "http://p:3128"
        assert http_proxy.resolve_proxy(SLACK_API) == "http://p:3128"

    def test_an_environment_socks_proxy_without_python_socks_still_raises(
        self, monkeypatch, no_python_socks
    ):
        monkeypatch.setattr(
            urllib.request, "getproxies_environment", lambda: {"https": "socks5://h:1080"}
        )
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "socks5://h:1080"})
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
        with pytest.raises(SocksProxyUnavailable):
            select_socket_mode_proxy(WSS)

    def test_an_environment_http_proxy_reaches_websockets_unchanged(self, monkeypatch):
        env = {"https": "http://envproxy:8080"}
        monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: dict(env))
        monkeypatch.setattr(urllib.request, "getproxies", lambda: dict(env))
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
        assert select_socket_mode_proxy(WSS) == "http://envproxy:8080"


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

    @pytest.mark.asyncio
    async def test_connect_runs_the_os_bypass_matcher_off_the_loop(
        self, proxies, no_python_socks, monkeypatch
    ):
        # NO_PROXY alone: the OS bypass list applies, and it can block on DNS.
        import threading

        import kiro_crew.http_proxy as http_proxy

        threads: list[int] = []

        def _matcher(host: str) -> bool:
            threads.append(threading.get_ident())
            return False

        monkeypatch.setattr(
            urllib.request, "getproxies_environment", lambda: {"no": "example.test"}
        )
        monkeypatch.setattr(http_proxy, "_os_proxies", lambda: dict(MACOS_ALL_THREE))
        monkeypatch.setattr(http_proxy, "_os_bypass", lambda: _matcher)
        monkeypatch.setattr(http_proxy, "_os_reader", lambda: dict)  # a macOS host
        session = MagicMock()
        session.close = AsyncMock()
        monkeypatch.setattr(websockets, "connect", AsyncMock(return_value=session))

        client = WSSocketModeClient(app_token="xapp-test")
        client.wss_uri = WSS
        monkeypatch.setattr(client, "monitor_current_session", AsyncMock())
        monkeypatch.setattr(client, "receive_messages", AsyncMock())
        try:
            await client.connect()
        finally:
            await client.close()

        assert threads and threading.get_ident() not in threads


class TestSelectionBranches:
    """Branches of the selection helpers the end-to-end cases above do not reach."""

    def test_socks_support_is_false_when_python_socks_does_not_import(self, monkeypatch):
        def _missing(name):
            raise ImportError(name)

        monkeypatch.setattr(smp.importlib, "import_module", _missing)
        assert smp._socks_supported() is False

    def test_socks_support_is_true_when_python_socks_imports(self, monkeypatch):
        asked: list[str] = []
        monkeypatch.setattr(smp.importlib, "import_module", lambda name: asked.append(name))
        assert smp._socks_supported() is True
        assert asked == ["python_socks.async_.asyncio"]

    def test_http_fallback_takes_the_http_entry_when_https_is_socks(self):
        proxies = {"https": "socks5://h:1080", "http": "http://p:3128"}
        assert smp._http_fallback(proxies) == "http://p:3128"

    def test_http_fallback_is_none_with_only_socks_entries(self):
        assert smp._http_fallback({"https": "socks5://h:1080"}) is None

    def test_a_host_on_the_no_proxy_snapshot_connects_direct(self, monkeypatch):
        import kiro_crew.http_proxy as http_proxy

        monkeypatch.setattr(http_proxy, "_os_proxies", lambda: {"https": "http://p:3128"})
        monkeypatch.setattr(http_proxy, "_os_bypass", lambda: None)
        env = {"no": "wss-primary.slack.com"}
        assert smp.select_socket_mode_proxy_from_os(WSS, env) is None

    def test_a_host_the_os_bypass_list_names_connects_direct(self, monkeypatch):
        import kiro_crew.http_proxy as http_proxy

        monkeypatch.setattr(http_proxy, "_os_proxies", lambda: {"https": "http://p:3128"})
        monkeypatch.setattr(
            http_proxy, "_os_bypass", lambda: (lambda host: host == "wss-primary.slack.com")
        )
        assert smp.select_socket_mode_proxy_from_os(WSS, {"no": "example.test"}) is None
        assert smp.select_socket_mode_proxy_from_os(WSS, {}) is None

    def test_settle_sends_a_secure_uri_with_no_pick_through_the_http_entry(self):
        assert smp._settle(None, {"http": "http://p:3128"}, secure=True) == "http://p:3128"

    def test_settle_keeps_no_proxy_for_a_plain_uri(self):
        assert smp._settle(None, {"http": "http://p:3128"}, secure=False) is None

    def test_settle_replaces_socks_with_http_and_says_so(self, no_python_socks, caplog):
        proxies = {"https": "socks5://h:1080", "http": "http://p:3128"}
        with caplog.at_level("INFO", logger=smp.logger.name):
            assert smp._settle("socks5://h:1080", proxies, secure=True) == "http://p:3128"
        assert any("python-socks is not installed" in r.getMessage() for r in caplog.records)

    def test_settle_raises_when_socks_is_the_only_proxy(self, no_python_socks):
        with pytest.raises(SocksProxyUnavailable):
            smp._settle("socks5://h:1080", {"https": "socks5://h:1080"}, secure=True)


class TestConnectBranches:
    """``connect``: the URL issue, the environment path, and replacing a live session."""

    @pytest.mark.asyncio
    async def test_an_environment_proxy_is_picked_on_the_loop_and_old_tasks_replaced(
        self, monkeypatch
    ):
        env = {"https": "http://envproxy:8080"}
        monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: dict(env))
        monkeypatch.setattr(urllib.request, "getproxies", lambda: dict(env))
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
        thread_calls: list[object] = []

        async def _no_thread(fn, *args, **kwargs):
            thread_calls.append(fn)
            return fn(*args, **kwargs)

        monkeypatch.setattr(smp.asyncio, "to_thread", _no_thread)
        session = MagicMock()
        session.close = AsyncMock()
        old_session = MagicMock()
        old_session.close = AsyncMock()
        fake_connect = AsyncMock(return_value=session)
        monkeypatch.setattr(websockets, "connect", fake_connect)

        client = WSSocketModeClient(app_token="xapp-test")
        client.wss_uri = None
        monkeypatch.setattr(client, "issue_new_wss_url", AsyncMock(return_value=WSS))
        monkeypatch.setattr(client, "monitor_current_session", AsyncMock())
        monkeypatch.setattr(client, "receive_messages", AsyncMock())
        old_monitor = MagicMock()
        old_receiver = MagicMock()
        client.current_session = old_session
        client.current_session_monitor = old_monitor
        client.message_receiver = old_receiver
        try:
            await client.connect()
        finally:
            await client.close()

        assert client.wss_uri == WSS
        assert fake_connect.await_args.kwargs["proxy"] == "http://envproxy:8080"
        assert thread_calls == []
        old_monitor.cancel.assert_called_once()
        old_receiver.cancel.assert_called_once()
        old_session.close.assert_awaited_once()
