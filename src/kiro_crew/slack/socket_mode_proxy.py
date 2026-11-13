"""Socket Mode client that chooses its proxy explicitly.

``websockets`` 15 detects a proxy on its own (``proxy=True``) through
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
* otherwise websockets' own precedence picks the proxy;
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

logger = logging.getLogger(__name__)

# websockets 15 is the first release that detects a proxy, and the first whose
# ``connect`` accepts ``proxy=``. Older releases always connect directly.
_ws_get_proxy = getattr(websockets.uri, "get_proxy", None)


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


def _bypassed(uri: str) -> bool:
    parsed = urllib.parse.urlparse(uri)
    host = parsed.hostname or ""
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    return bool(urllib.request.proxy_bypass(host) or urllib.request.proxy_bypass(f"{host}:{port}"))


def select_socket_mode_proxy(uri: str) -> str | None:
    """Return the proxy URL to open ``uri`` through, or ``None`` for direct.

    Raises :class:`SocksProxyUnavailable` when the only proxy configured for
    ``uri`` is a SOCKS one and ``python-socks`` is not installed.
    """
    if _ws_get_proxy is None or _bypassed(uri):
        return None
    proxy: str | None = _ws_get_proxy(websockets.uri.parse_uri(uri))
    if proxy is None or not _is_socks(proxy) or _socks_supported():
        return proxy
    proxies = urllib.request.getproxies()
    for scheme in ("https", "http"):
        candidate = proxies.get(scheme)
        if candidate and not _is_socks(candidate):
            logger.info(
                "Slack Socket Mode: a SOCKS proxy is configured but python-socks "
                "is not installed; connecting through the %s proxy instead",
                scheme.upper(),
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
        if _ws_get_proxy is not None:
            connect_kwargs["proxy"] = select_socket_mode_proxy(self.wss_uri)
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
