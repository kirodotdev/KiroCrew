"""Shared origin-validation helpers for CSRF and WebSocket checks.

Centralises dashboard URL parsing, bind-address resolution, origin-set
construction, and per-request origin validation so that ``server.py``
(CSRF middleware), ``ws.py`` (WebSocket handshake), and ``gateway.py``
(startup messages) all share a single source of truth.

The only user-facing config is ``dashboard.url`` — a single URL like
``http://my-host.example.com:8080``.  Everything else (port, bind
address, allowed origins) is derived from it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import parse_qs, quote, urlparse

from aiohttp import web

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 5476

_BIND_LOCAL = "127.0.0.1"
_BIND_ALL = "0.0.0.0"

# Loopback hostnames that all resolve to the same machine but are *distinct
# browser origins* (origin = scheme://host:port). The dashboard SPA stores user
# settings (theme, zoom, layout, ...) in per-origin localStorage, so a user who
# reaches the dashboard on more than one of these names gets a separate, empty
# settings bucket each time — settings appear to "reset". We canonicalize
# navigations among this set onto a single host (see should_canonicalize_host).
_CANONICALIZABLE_LOOPBACK_HOSTS = frozenset(
    {"127.0.0.1", "::1", "localhost", "kirocrew.localhost"}
)


# ---------------------------------------------------------------------------
# Hostname / IP helpers
# ---------------------------------------------------------------------------


def machine_hostname() -> str | None:
    """Return the machine hostname, or ``None`` on failure."""
    try:
        return socket.gethostname()
    except Exception:
        return None


def is_loopback(host: str) -> bool:
    """Return ``True`` if *host* is a loopback address (127.0.0.1, ::1, etc.)."""
    if host in ("localhost", "127.0.0.1", "::1", "kirocrew.localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


#: Headers that proxies/tunnels attach when forwarding a request. Browsers
#: never send these themselves, so their presence on a loopback-peer request
#: means the true client is somewhere else (tunnel, nginx, Caddy, ALB, …).
_PROXY_FORWARD_HEADERS = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Real-IP",
)


def is_direct_local_request(request: web.Request) -> bool:
    """Return ``True`` only for a request made directly from this machine.

    A loopback TCP peer alone is NOT sufficient: the gateway binds loopback
    and remote access is delivered via a same-host tunnel or reverse proxy,
    so a forwarded remote request also arrives from 127.0.0.1. Standard
    proxies (including the AEA tunnel path, nginx, Caddy, ALB) attach
    ``Forwarded``/``X-Forwarded-*``/``X-Real-IP`` headers; a browser talking
    to localhost directly never does. So: loopback peer AND no forwarding
    headers ⇒ direct local. Anything else is treated as remote (fail-closed
    for the secret-reveal and config-write gates).

    Known limits: an SSH port-forward (``ssh -L``), socat relay, or a proxy
    that strips all forwarding headers is indistinguishable from a local
    client at this layer. Establishing any of those requires SSH credentials
    or code execution on this host, which already grants direct read/write
    access to ``.env`` and config.json — so the gate is not the protection
    boundary against host-level actors and does not try to be. Its job is to
    stop a dashboard-token-only attacker arriving through the product's
    remote paths (tunnel, reverse proxy), which all attach forwarding
    headers. Hosted/multi-user deployments should force read-only via a
    server-side policy regardless of request origin.
    """
    if not is_loopback(request.remote or ""):
        return False
    return not any(h in request.headers for h in _PROXY_FORWARD_HEADERS)


def is_https_request(request: web.Request) -> bool:
    """Return ``True`` when the browser reached the dashboard over HTTPS.

    ``request.scheme`` alone is insufficient behind a TLS-terminating reverse
    proxy or tunnel: the proxy terminates HTTPS at its edge and forwards **plain
    HTTP to the loopback-bound gateway**, so ``request.scheme`` reads ``"http"``
    even though the browser's connection — and therefore the ``wss://`` dashboard
    WebSocket — is secure. When that happens the auth cookie is set WITHOUT
    ``Secure`` and modern mobile browsers withhold it from the ``wss://`` upgrade,
    so the live WebSocket 403s and the dashboard flaps online/offline.

    We therefore also honour ``X-Forwarded-Proto: https`` — but ONLY when the
    immediate peer is loopback. The gateway binds ``127.0.0.1``, so a loopback
    peer is the local tunnel/reverse-proxy; a remote attacker cannot reach the
    socket to forge the header. (Even if the header were spoofed over plain
    HTTP, the only effect is a ``Secure`` cookie the spoofer's own HTTP client
    then refuses to send back — a self-inflicted no-op, never a downgrade.)
    """
    if request.scheme == "https":
        return True
    xfp = request.headers.get("X-Forwarded-Proto", "")
    # X-Forwarded-Proto may be a comma-separated chain (proxy1, proxy2); the
    # left-most value is the original client-facing scheme.
    proto = xfp.split(",")[0].strip().lower()
    if proto == "https" and is_loopback(request.remote or ""):
        return True
    return False


# ---------------------------------------------------------------------------
# Dashboard URL parsing
# ---------------------------------------------------------------------------


def parse_dashboard_url(url: str) -> tuple[str, int]:
    """Parse ``dashboard.url`` into ``(hostname, port)``.

    Returns ``("", _DEFAULT_PORT)`` when *url* is empty.
    ``KIROCREW_PORT`` env var always overrides the port (dev mode).
    """
    if not url:
        host, port = "", _DEFAULT_PORT
    else:
        url = _ensure_scheme(url)
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or _DEFAULT_PORT
    env_port = os.environ.get("KIROCREW_PORT")
    if env_port:
        try:
            port = int(env_port)
        except ValueError:
            logger.warning(
                "KIROCREW_PORT=%r is not a valid integer; using port %d from config",
                env_port,
                port,
            )
    return host, port


def _ensure_scheme(url: str) -> str:
    """Prepend ``http://`` if *url* has no scheme."""
    return url if "://" in url else f"http://{url}"


def dashboard_origin(url: str) -> str:
    """Return the browser-facing origin for *url*, or ``""`` if invalid.

    Reuses the same scheme-defaulting logic as :func:`parse_dashboard_url`
    so that bare hostnames (``myhost:8080``) are normalised to ``http://``.
    Default ports (80 for http, 443 for https) are stripped to match
    browser ``Origin`` header behaviour.
    """
    if not url:
        return ""
    url = _ensure_scheme(url)
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        logger.warning("Ignoring malformed dashboard_url: %s", url)
        return ""
    if not host:
        return ""
    if scheme not in ("http", "https"):
        logger.warning("Ignoring non-HTTP dashboard_url scheme: %s", scheme)
        return ""
    # urlparse strips [] from IPv6 — re-wrap to match browser Origin header
    if ":" in host:
        host = f"[{host}]"
    default_port = {"http": 80, "https": 443}.get(scheme)
    if port == default_port:
        port = None
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


# ---------------------------------------------------------------------------
# Remote-proxy detection (OSS: no managed proxy)
# ---------------------------------------------------------------------------


def devspaces_proxy_url(port: int) -> str | None:
    """Return the managed-proxy base URL, or ``None`` (always ``None`` in OSS).

    Symbol preserved for callers (``is_local_only``, ``format_dashboard_urls``,
    ``build_allowed_origins``); there is no managed reverse-proxy in the public
    build, so this always returns ``None``.  Users behind their own proxy add
    its origin via ``dashboard.url`` or ``KIROCREW_CORS_ORIGINS``.
    """
    return None


# ---------------------------------------------------------------------------
# Local-only mode resolution
# ---------------------------------------------------------------------------


def is_local_only(dashboard_host: str, slack_connected: bool) -> bool:
    """Determine whether the dashboard should bind to loopback only.

    Always returns ``True`` (bind ``127.0.0.1``) in the public build.
    To expose the dashboard beyond loopback, run your own reverse proxy
    (e.g. Caddy/nginx with TLS) and add its origin via ``dashboard.url``
    or ``KIROCREW_CORS_ORIGINS``.
    """
    # A managed proxy could require a 0.0.0.0 binding; there is none in OSS
    # (devspaces_proxy_url always returns None), so we always bind loopback.
    # Safety: slack_connected=True guarantees start_dashboard() mounts
    # token_auth_middleware before any non-loopback binding is used.
    if devspaces_proxy_url(0) is not None and slack_connected:
        logger.info("Managed proxy detected: binding 0.0.0.0 (token auth via Slack)")
        return False
    return True


def bind_address_for(local_only: bool) -> str:
    """Return the TCP bind address string for aiohttp."""
    return _BIND_LOCAL if local_only else _BIND_ALL


# ---------------------------------------------------------------------------
# Dashboard host / URL helpers
# ---------------------------------------------------------------------------


def resolve_dashboard_host(local_only: bool, configured_host: str = "") -> str:
    """Return the hostname users should use to reach the dashboard."""
    if configured_host:
        return configured_host
    if local_only:
        # Canonical loopback host is plain ``localhost``. It resolves in every
        # browser and through SSH port-forwards, so a dev who tunnels into a
        # Linux devdesk and opens the dashboard in Safari can always reach it.
        # ``kirocrew.localhost`` (and other ``*.localhost`` names) are NOT
        # resolved by Safari / the macOS system resolver — RFC 6761's loopback
        # rule is a SHOULD that only Chrome/Firefox and the Linux stub resolver
        # honor — so canonicalizing onto ``kirocrew.localhost`` would 302 those
        # users to a host their browser can't resolve. ``*.localhost`` stays
        # trusted in build_allowed_origins() for the multi-instance embedded-pane
        # iframes; it is just not the single host the emitters + 302 converge on.
        return "localhost"
    return machine_hostname() or "localhost"


def _host_without_port(host_header: str) -> str:
    """Return the host from a ``Host`` header value, IPv6-bracket aware.

    Mirrors the rest of this file's IPv6 handling (``is_loopback``,
    ``dashboard_origin``, ``build_allowed_origins`` all treat ``::1`` as
    first-class). A naive ``split(":")[0]`` would yield ``"["`` for an
    ``[::1]:7777`` Host and silently defeat the loopback match.

        ``[::1]:7777`` -> ``::1``   ``localhost:7777`` -> ``localhost``
        ``[::1]``      -> ``::1``   ``127.0.0.1``      -> ``127.0.0.1``
    """
    h = (host_header or "").strip()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end != -1 else h[1:]
    return h.split(":")[0]


def should_canonicalize_host(
    request_host: str,
    canonical_host: str,
    *,
    method: str,
    sec_fetch_dest: str | None,
) -> bool:
    """Return True if a request should be 302-redirected to *canonical_host*.

    Converges loopback aliases (127.0.0.1 / localhost / kirocrew.localhost) onto
    a single origin so the SPA's per-origin localStorage settings are not split
    across hostnames (see _CANONICALIZABLE_LOOPBACK_HOSTS). Conservative on every
    axis so it can never touch a request that isn't a same-machine top-level
    page navigation:

    * only GET/HEAD (never a mutating request),
    * only true top-level document navigations (``Sec-Fetch-Dest: document`` —
      absent/empty/websocket/XHR are left alone, so APIs and WebSockets and
      sub-resource fetches are never redirected),
    * only when both the request host and the canonical host are in the
      canonicalizable loopback set (real hostnames / reverse-proxy vhosts are
      never redirected),
    * only when they actually differ.

    The caller passes the port-stripped comparison via *request_host*'s host part;
    this helper strips any ``:port`` itself for safety.
    """
    if method not in ("GET", "HEAD"):
        return False
    if sec_fetch_dest != "document":
        return False
    host = _host_without_port(request_host or "")
    if host not in _CANONICALIZABLE_LOOPBACK_HOSTS:
        return False
    if canonical_host not in _CANONICALIZABLE_LOOPBACK_HOSTS:
        return False
    return host != canonical_host


def build_dashboard_url(base_url: str, token: str = "", *, local_only: bool = True) -> str:
    """Build the authenticated dashboard URL."""
    if local_only is not True and not token:
        raise ValueError("token is required when dashboard is not local-only")
    return f"{base_url}?token={quote(token, safe='')}" if token else base_url


def format_dashboard_urls(
    authed_url: str,
    *,
    port: int,
    local_only: bool = True,
    has_custom_host: bool = False,
) -> list[str]:
    """Return startup log lines describing how to reach the dashboard."""
    parsed_query = urlparse(authed_url).query
    _qs = f"?{parsed_query}" if parsed_query else ""
    if local_only is not True and "token" not in parse_qs(parsed_query):
        raise ValueError("token is required when dashboard is not local-only")
    _is_remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))

    if _is_remote:
        mh = machine_hostname() or "localhost"
        lines: list[str] = [
            f"👻 Dashboard: ssh -NL {port}:localhost:{port} {mh}",
            f"             then open http://localhost:{port}{_qs}",
        ]
    else:
        lines = ["👻 Dashboard:", f"   {authed_url}"]

    if local_only and not has_custom_host and not _is_remote:
        mh_local = machine_hostname()
        if mh_local and mh_local != "localhost":
            try:
                ip = socket.gethostbyname(mh_local)
                if ip and ip != "127.0.0.1":
                    lines.append(f"👻 Remote:    ssh -NL {port}:localhost:{port} {mh_local}")
            except Exception:
                pass

    proxy = devspaces_proxy_url(port)
    if proxy and not local_only:
        lines.append(f"👻 Proxy:     {proxy}{_qs}")

    if _is_remote:
        lines.append("👻 Run 24/7:  see docs/REMOTE_DESKTOP_SETUP.md for systemd service setup")

    return lines


# ---------------------------------------------------------------------------
# Allowed-origin set
# ---------------------------------------------------------------------------


def build_allowed_origins(
    port: int, local_only: bool, configured_host: str = "", dashboard_url: str = ""
) -> set[str]:
    """Compute the set of allowed origins for the dashboard.

    When *dashboard_url* is provided, its origin (scheme + host + port)
    is added as-is so that reverse-proxy setups (e.g. Caddy with TLS on
    a custom domain) pass the CSRF check without code changes.
    """
    origins: set[str] = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://kirocrew.localhost:{port}",
    }
    if os.environ.get("KIROCREW_HOME"):
        origins.add("http://localhost:3000")
        origins.add("http://127.0.0.1:3000")
    if configured_host:
        origins.add(f"http://{configured_host}:{port}")
    if dashboard_url:
        origin = dashboard_origin(dashboard_url)
        if origin:
            origins.add(origin)
    if not local_only:
        mh = machine_hostname()
        if mh:
            origins.add(f"http://{mh}:{port}")
    # Managed-proxy origin (None in OSS; see devspaces_proxy_url)
    proxy = devspaces_proxy_url(port)
    if proxy:
        origins.add(proxy)
    # Manual CORS override for future environments
    for _co in os.environ.get("KIROCREW_CORS_ORIGINS", "").split(","):
        if _co.strip():
            origins.add(_co.strip())
    # Extra loopback ports the operator explicitly trusts — typically the
    # local end of an SSH tunnel (``-L 8777:localhost:7777`` makes the browser
    # send Origin ``http://localhost:8777``). This replaces the previous
    # blanket "trust any loopback port" behaviour (CSE SEC-016): only the bound
    # port and these opted-in ports are accepted, so a malicious local web page
    # on an arbitrary port can no longer pass the CSRF origin check.
    for _p in os.environ.get("KIROCREW_ALLOWED_LOOPBACK_PORTS", "").split(","):
        _p = _p.strip()
        if _p.isdigit():
            origins.add(f"http://127.0.0.1:{_p}")
            origins.add(f"http://localhost:{_p}")
            origins.add(f"http://[::1]:{_p}")
            origins.add(f"http://kirocrew.localhost:{_p}")
    return origins


def build_allowed_hosts(allowed_origins: set[str]) -> set[str]:
    """Derive the ``Host``-header allowlist from the CSRF origin allowlist.

    DNS-rebinding defense-in-depth: the ``Host`` header must name a
    host we actually serve. We reuse the SAME source of truth as the CSRF Origin
    check (``allowed_origins``) so the two layers can never drift — every origin
    the dashboard trusts contributes its hostname. The comparison is
    port-independent (hostname only), so an SSH-tunnel local port such as
    ``localhost:8777`` still matches ``localhost``. The canonical loopback names
    are always included as a floor so local tooling / doctor / mcp-core are never
    rejected.
    """
    hosts: set[str] = {"localhost", "127.0.0.1", "::1", "kirocrew.localhost"}
    for origin in allowed_origins:
        try:
            parsed_host = urlparse(origin).hostname
        except ValueError:
            parsed_host = None
        if parsed_host:
            hosts.add(parsed_host.lower())
    return hosts


# ---------------------------------------------------------------------------
# Per-request origin check
# ---------------------------------------------------------------------------


def check_origin(
    request: web.Request,
    *,
    require: bool = True,
    fallback_header: str | None = None,
) -> bool:
    """Validate the request origin against ``app["allowed_origins"]``.

    Loopback requests (127.0.0.1, ::1) without an Origin header are
    always trusted — local processes like mcp-core and doctor don't
    send Origin headers but are not cross-origin attacks.  A browser
    on the same machine would always send an Origin header.
    """
    allowed: set[str] = request.app["allowed_origins"]
    origin = request.headers.get("Origin") or ""
    if not origin and fallback_header:
        origin = request.headers.get(fallback_header, "")
    if not origin:
        # No Origin header: trust loopback (local processes), reject others
        if is_loopback(request.remote or ""):
            return True
        return not require
    origin_base = "/".join(origin.split("/")[:3]) if "://" in origin else ""
    if origin_base in allowed:
        return True
    # Same-origin loopback fallback: allow a loopback Origin when it
    # matches the request's own Host, i.e. a genuine same-origin request. This
    # is what the multi-instance embedded iframe produces — it is served at
    # ``<host>:<tunnelPort>`` and opens its WebSocket to that very same
    # ``location.host``, so Origin == Host. It does NOT reopen CSE SEC-016: a
    # malicious local page on an arbitrary port sends its own Origin (e.g.
    # ``localhost:9999``) while the Host is the gateway's port, so they differ
    # and this branch rejects it. Browsers forbid scripts from forging either
    # the Origin or Host header, so the equality is a sound CSRF boundary.
    if origin_base:
        parsed = urlparse(origin_base)
        if is_loopback(parsed.hostname or ""):
            req_host = request.headers.get("Host", "")
            origin_hostport = origin_base.split("://", 1)[-1]
            if req_host and origin_hostport == req_host:
                return True
    # NOTE (CSE SEC-016): we deliberately do NOT blanket-trust every loopback
    # origin regardless of port. A browser always sends an Origin header, so a
    # malicious local web page on an arbitrary port would otherwise pass this
    # CSRF check (cookies are auto-attached). SSH-tunnel users whose browser
    # sends a different local port must opt that port in via
    # ``KIROCREW_ALLOWED_LOOPBACK_PORTS`` (folded into the allowed set above).
    return False


def check_host(request: web.Request) -> bool:
    """Validate the request ``Host`` header against the dashboard's host allowlist.

    Independent DNS-rebinding barrier. A rebound request arrives on
    the loopback socket (the victim's browser resolved the attacker domain to
    127.0.0.1) but carries the attacker's domain in ``Host``. So, unlike
    :func:`check_origin`, this:

    * runs for EVERY method — GET-based data exfiltration is the rebinding
      payload, and CSRF only guards mutating methods; and
    * does NOT trust a loopback ``request.remote`` — the whole point of DNS
      rebinding is that the connection *is* loopback while the ``Host`` is forged.

    A missing/empty ``Host`` is allowed ONLY from a loopback ``request.remote``:
    HTTP/1.1 browsers ALWAYS send ``Host``, so a browser-driven rebinding attack
    always presents a non-empty forged host (rejected below) and never reaches
    this branch; only non-browser local IPC clients (mcp-core, doctor) omit
    ``Host``, and those are always loopback. Rather than blanket-allowing a
    headerless request, we positively confirm the loopback origin — a headerless
    request from a non-loopback remote is denied (deny-by-default). This mirrors
    :func:`check_origin`, which likewise trusts only loopback when the Origin
    header is absent.

    A missing/empty ``allowed_origins`` is treated as a DENIAL (deny-by-default),
    not fail-open: ``build_allowed_origins`` always returns at least the loopback
    origins, so at runtime the set is never empty — a falsy value therefore means
    misconfiguration or a bug/race, and silently bypassing the whole Host check in
    that case would be a fail-open authorization hole.
    """
    allowed_origins: set[str] | None = request.app.get("allowed_origins")
    if not allowed_origins:
        # Deny-by-default: never skip the Host check because the allowlist is
        # missing/None (unset key, race) or empty. The allowlist is populated at
        # startup before this middleware runs, so this only fires on a bug.
        return False
    raw_host = request.headers.get("Host", "")
    if not raw_host:
        # No Host header: positively confirm a loopback origin (local IPC) rather
        # than blanket-allowing. A headerless request from a non-loopback remote
        # is denied. Browser-driven rebinding always sends a forged non-empty Host
        # and is handled below, so this carve-out cannot weaken the barrier.
        return is_loopback(request.remote or "")
    # Derive the host allowlist live from allowed_origins (the SAME set the
    # tunnel adds to / discards from at connect / disconnect) so the Host check
    # and the CSRF Origin check can never drift out of sync.
    allowed = build_allowed_hosts(allowed_origins)
    host = _host_without_port(raw_host).lower()
    return host in allowed
