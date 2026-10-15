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
    if proxy and urllib.request.proxy_bypass(urllib.parse.urlparse(url).hostname or ""):
        return None
    return proxy
