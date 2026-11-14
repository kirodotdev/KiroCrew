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
    return None if is_bypassed(url) else proxy


def is_bypassed(url: str, *, environment_only: bool = False) -> bool:
    """True when the bypass list names *url*'s host, bare or as ``host:port``.

    The port is the URL's own or its scheme's default. ``resolve_proxy``, Slack
    Socket Mode and Telegram use this one rule. ``environment_only`` reads
    ``NO_PROXY`` alone: the OS fallback can resolve the host, which blocks, so
    a caller on the event loop whose proxy came from the environment uses it.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme in ("https", "wss") else 80)
    # typeshed omits proxy_bypass_environment; the runtime has it.
    bypass = (
        urllib.request.proxy_bypass_environment  # type: ignore[attr-defined]
        if environment_only
        else urllib.request.proxy_bypass
    )
    return bool(bypass(host) or bypass(f"{host}:{port}"))
