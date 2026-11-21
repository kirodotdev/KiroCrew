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
    default_port = 443 if parsed.scheme in ("https", "wss") else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        # An out-of-range port must not make the shared check raise; fall back
        # to the scheme default so the bare-host check still runs.
        port = default_port
    # typeshed omits proxy_bypass_environment; the runtime has it.
    bypass = (
        urllib.request.proxy_bypass_environment  # type: ignore[attr-defined]
        if environment_only
        else urllib.request.proxy_bypass
    )
    return bool(bypass(host) or bypass(f"{host}:{port}"))
