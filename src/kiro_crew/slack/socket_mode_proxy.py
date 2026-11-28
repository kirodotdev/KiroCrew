"""Socket Mode client that chooses its proxy explicitly.

``websockets`` 15, the floor ``setup.cfg`` sets because its ``connect`` is
the first to take ``proxy=``, detects a proxy on its own (``proxy=True``) through
``urllib.request.getproxies()``, and for a ``wss://`` URL it prefers a SOCKS
entry over the HTTPS one. On macOS ``getproxies()`` also reads the system proxy
settings, which local proxy tools such as Clash fill with HTTP, HTTPS *and*
SOCKS at once. Speaking SOCKS needs the optional ``python-socks`` package,
which Kiro Crew does not depend on, so the connect failed with ``ImportError``
even though the same proxy also answered HTTP CONNECT.

slack_sdk's websockets client calls ``websockets.connect`` without a ``proxy``
argument, so the only place to decide is its ``connect`` method, which
:class:`ProxySelectingSocketModeClient` overrides. The choice itself is
:func:`select_socket_mode_proxy`:

* the destination host is on the bypass list -> connect directly. The list is
  matched against the bare host as well as ``host:port``, because macOS
  matches its exception patterns against the whole string and ``*.slack.com``
  never matches ``wss-primary.slack.com:443``;
* proxies read from the OS settings (no proxy variable beyond ``NO_PROXY``):
  a usable HTTPS or HTTP entry is taken first, the one the Web API takes, so a
  Windows ``socks=`` entry the stdlib also writes into ``https`` as
  ``socks4://`` cannot shadow a real HTTP proxy. With only SOCKS configured,
  the SOCKS proxy is kept when ``python-socks`` imports, else
  :class:`SocksProxyUnavailable` is raised as below; only an empty map
  connects directly;
* otherwise the operator's environment applies with websockets' own precedence;
* a SOCKS pick is kept when ``python-socks`` is importable, else replaced by
  the HTTPS, then HTTP, proxy -- the same entries every other Kiro Crew HTTP
  client uses (``kiro_crew.http_proxy.resolve_proxy``);
* a SOCKS pick with no HTTP(S) proxy to fall back to raises
  :class:`SocksProxyUnavailable`, whose message tells the operator what to
  change. It never falls back to a direct connection: a configured proxy may
  be the only way out of the network, and silently routing around it would
  hide that.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import urllib.parse
import urllib.request
from typing import Any

import websockets
import websockets.uri
from slack_sdk.socket_mode.websockets import SocketModeClient
from websockets.uri import get_proxy as _ws_get_proxy

from kiro_crew import http_proxy as _http_proxy
from kiro_crew.http_proxy import (
    is_bypassed,
    os_bypassed,
    os_phase_needed,
    proxy_environment,
    system_proxies,
    usable_os_entry,
)

logger = logging.getLogger(__name__)


class SocksProxyUnavailable(RuntimeError):
    """A SOCKS proxy is configured for Slack, but nothing can speak SOCKS.

    ``status_reason`` is the message, which is short enough for the settings
    badge that shows why Slack is not connected.
    """

    @property
    def status_reason(self) -> str:
        return str(self)


def _socks_supported() -> bool:
    """Whether ``python-socks`` (what websockets uses for SOCKS) imports."""
    try:
        importlib.import_module("python_socks.async_.asyncio")
    except ImportError:
        return False
    return True


def _is_socks(proxy: str) -> bool:
    return urllib.parse.urlparse(proxy).scheme.lower().startswith("socks")


def _http_fallback(proxies: dict[str, str]) -> str | None:
    """The first non-SOCKS https then http entry, as ``resolve_proxy`` picks."""
    for scheme in ("https", "http"):
        candidate = proxies.get(scheme)
        if candidate and not _is_socks(candidate):
            return candidate
    return None


def select_socket_mode_proxy(uri: str) -> str | None:
    """Return the proxy URL to open ``uri`` through, or ``None`` for direct.

    Raises :class:`SocksProxyUnavailable` when the only proxy configured for
    ``uri`` is a SOCKS one and ``python-socks`` is not installed.
    """
    if is_bypassed(uri):
        return None
    proxies, from_os = system_proxies()
    if from_os:
        return _from_os_map(proxies)
    parsed = websockets.uri.parse_uri(uri)
    return _settle(_ws_get_proxy(parsed), proxies, parsed.secure)


def select_socket_mode_proxy_from_os(uri: str, env: dict[str, str]) -> str | None:
    """``select_socket_mode_proxy`` when *env* names no proxy beyond ``NO_PROXY``.

    Reads the OS settings and the snapshot, never ``os.environ``, so the
    connect path runs it in a worker thread while the loop may write it.
    """
    if os_bypassed(uri, env):
        return None
    return _from_os_map(_http_proxy._os_proxies())


def _from_os_map(proxies: dict[str, str]) -> str | None:
    # A configured proxy no client can speak is an actionable error, never a
    # silent direct connection: only an empty map goes direct.
    usable = usable_os_entry(proxies)
    if usable is not None:
        return usable
    socks = _os_socks_entry(proxies)
    if socks is None:
        return None
    if _socks_supported():
        return socks
    raise SocksProxyUnavailable(
        "SOCKS proxy set but python-socks is not installed: set an HTTPS proxy "
        "or bypass *.slack.com*"
    )


def _os_socks_entry(proxies: dict[str, str]) -> str | None:
    """The OS map's SOCKS proxy in a form websockets accepts, or ``None``.

    Windows writes ``socks=h:p`` as ``socks4://h:p`` under https/http and
    ``socks://h:p`` under socks; macOS writes ``http://h:p`` under socks.
    """
    for scheme in ("https", "http"):
        proxy = proxies.get(scheme)
        if proxy and _is_socks(proxy) and not proxy.startswith("socks://"):
            return proxy
    proxy = proxies.get("socks")
    if proxy:
        for prefix in ("http://", "socks://"):
            if proxy.startswith(prefix):
                return "socks5h://" + proxy[len(prefix) :]
        return proxy
    return None


def _settle(proxy: str | None, proxies: dict[str, str], secure: bool) -> str | None:
    if proxy is None and secure:
        # websockets never uses an http entry for wss://, but resolve_proxy
        # sends the Web API through it, so take it here too: one route for both.
        return _http_fallback(proxies)
    if proxy is None or not _is_socks(proxy) or _socks_supported():
        return proxy
    candidate = _http_fallback(proxies)
    if candidate is not None:
        logger.info(
            "Slack Socket Mode: a SOCKS proxy is configured but python-socks "
            "is not installed; connecting through the HTTP(S) proxy instead"
        )
        return candidate
    raise SocksProxyUnavailable(
        "SOCKS proxy set but python-socks is not installed: set an HTTPS proxy "
        "or bypass *.slack.com*"
    )


class ProxySelectingSocketModeClient(SocketModeClient):
    """slack_sdk's websockets Socket Mode client with an explicit proxy.

    ``connect`` mirrors the parent's body and differs in one argument: the
    ``proxy`` passed to ``websockets.connect``. The SDK's reconnect path calls
    ``connect`` as well, so every new session goes through the same choice.
    """

    async def connect(self) -> None:
        if self.wss_uri is None:
            self.wss_uri = await self.issue_new_wss_url()
        old_session = self.current_session
        connect_kwargs: dict[str, Any] = {"uri": self.wss_uri, "ping_interval": self.ping_interval}
        env = proxy_environment()
        if os_phase_needed(env):
            # Off the loop: the OS bypass matcher can block on a DNS lookup.
            # The environment was read above, on the loop that writes it.
            proxy = await asyncio.to_thread(select_socket_mode_proxy_from_os, self.wss_uri, env)
        else:
            proxy = select_socket_mode_proxy(self.wss_uri)
        connect_kwargs["proxy"] = proxy
        self.current_session = await websockets.connect(**connect_kwargs)
        session_id = await self.session_id()
        self.auto_reconnect_enabled = self.default_auto_reconnect_enabled
        self.logger.info(f"A new session ({session_id}) has been established")

        if self.current_session_monitor is not None:
            self.current_session_monitor.cancel()
        self.current_session_monitor = asyncio.ensure_future(self.monitor_current_session())

        if self.message_receiver is not None:
            self.message_receiver.cancel()
        self.message_receiver = asyncio.ensure_future(self.receive_messages())

        if old_session is not None:
            await old_session.close()
            self.logger.info(
                f"The old session ({self.build_session_id(old_session)}) has been abandoned"
            )
