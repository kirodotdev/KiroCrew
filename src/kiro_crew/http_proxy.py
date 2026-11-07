"""Explicit HTTP proxy selection without HTTP-client credential discovery."""

from __future__ import annotations

import urllib.parse
import urllib.request


def resolve_proxy(url: str) -> str | None:
    """Select HTTPS then HTTP proxy, respecting the destination host's bypass list.

    Read environment and OS settings on each call. Passing the result explicitly
    lets HTTP clients keep their environment-driven netrc credential lookup off.
    A ``NO_PROXY`` entry may be port-qualified (``host:443``), so the effective
    port -- the URL's own, or the scheme's default -- is checked alongside the
    bare host.
    """
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http")
    if not proxy:
        return None
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if parsed.scheme in ("https", "wss") else 80
    if urllib.request.proxy_bypass(host) or urllib.request.proxy_bypass(f"{host}:{port}"):
        return None
    return proxy
