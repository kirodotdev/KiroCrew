"""Explicit HTTP proxy selection without HTTP-client credential discovery."""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# The Slack Web API base the slack_sdk clients call; their proxy is picked for it.
SLACK_API_URL = "https://slack.com/api/"


def resolve_proxy(url: str) -> str | None:
    """Select HTTPS then HTTP proxy, respecting the destination host's bypass list.

    Read environment and OS settings on each call. Passing the result explicitly
    lets HTTP clients keep their environment-driven netrc credential lookup off.
    """
    proxies = urllib.request.getproxies()
    if not urllib.request.getproxies_environment():
        # From the OS settings, not the operator's environment. A Windows
        # `socks=` entry reaches here as socks4://, which the HTTP clients
        # cannot speak, so take the first usable one of https then http, and
        # connect direct when neither is. An environment value passes through
        # unchanged below and the client refuses it itself.
        candidates = [proxies.get("https"), proxies.get("http")]
        usable = [p for p in candidates if p and _scheme(p) in _USABLE_PROXY_SCHEMES]
        if not usable:
            unusable = next((p for p in candidates if p), None)
            if unusable is not None:
                _warn_unusable_scheme(_scheme(unusable))
            return None
        proxy: str | None = usable[0]
    else:
        proxy = proxies.get("https") or proxies.get("http")
    if not proxy:
        return None
    return None if is_bypassed(url) else proxy


def _scheme(proxy: str) -> str:
    return urllib.parse.urlsplit(proxy).scheme.lower()


# The proxy schemes aiohttp and slack_sdk can use. The stdlib already turns a
# Windows `http=` or `https=` entry into an http:// URL.
_USABLE_PROXY_SCHEMES = frozenset({"http", "https"})
_warned_schemes: set[str] = set()


def _warn_unusable_scheme(scheme: str) -> None:
    if scheme in _warned_schemes:
        return
    _warned_schemes.add(scheme)
    logger.warning(
        "Ignoring a %s:// system proxy: the HTTP clients cannot use it, so they connect direct",
        scheme or "schemeless",
    )


def is_bypassed(url: str) -> bool:
    """True when the bypass list names *url*'s host, bare or as ``host:port``.

    The port is the URL's own or its scheme's default. ``resolve_proxy`` and
    Slack Socket Mode use this rule, which reads the OS bypass list too. A
    caller on the event loop uses ``proxy_for_url``, which reads ``NO_PROXY``
    alone because the OS fallback can block on a DNS lookup.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return False
    default_port = 443 if parsed.scheme in ("https", "wss") else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        # An out-of-range port must not make the shared check raise; fall back
        # to the scheme default so the bare-host check still runs.
        port = default_port
    bypass = urllib.request.proxy_bypass
    return bool(bypass(host) or bypass(f"{host}:{port}"))


def proxy_for_url(proxy: str | None, url: str) -> str | None:
    """Return *proxy* for *url*, or ``None`` when ``NO_PROXY`` lists its host.

    Reads only the environment's bypass list, never OS proxy settings, so it is
    cheap enough to call per request on the event loop.
    """
    if not proxy:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        return proxy
    host = parsed.hostname or ""
    if not host:
        return proxy
    if port is None:
        port = 443 if parsed.scheme in ("https", "wss") else 80
    # ``host:port`` matches an entry naming the bare host or that exact port.
    if urllib.request.proxy_bypass_environment(f"{host}:{port}"):  # type: ignore[attr-defined]
        return None
    return proxy
