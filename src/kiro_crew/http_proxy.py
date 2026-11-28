"""Explicit HTTP proxy selection without HTTP-client credential discovery."""

from __future__ import annotations

import urllib.parse
import urllib.request


def resolve_proxy(url: str) -> str | None:
    """Select HTTPS then HTTP proxy, respecting the destination host's bypass list.

    Read environment and OS settings on each call. Passing the result explicitly
    lets HTTP clients keep their environment-driven netrc credential lookup off.
    """
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http")
    if not proxy:
        return None
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # A bypass entry may name the bare host or ``host:port``, as Socket Mode checks.
    if urllib.request.proxy_bypass(host) or urllib.request.proxy_bypass(f"{host}:{port}"):
        return None
    return proxy


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
