"""Dashboard URL / host / origin-set helpers — stdlib-only leaf module.

Split out of :mod:`kiro_crew.dashboard.origin` so callers that only need URL
and host arithmetic can import them WITHOUT pulling ``aiohttp``. The helpers
here take plain strings and ints; everything that inspects a live
``web.Request`` (CSRF, Host barrier, direct-local and HTTPS detection) stays in
``origin``, which re-exports every name below so existing import paths keep
working.

Why it matters: ``kirocrew`` CLI invocations and every MCP stdio subprocess
import ``parse_dashboard_url`` on the way to resolving the dashboard port.
Reaching it through ``origin`` cost ~605ms and 1124 modules — the ``aiohttp``
stack plus (before ``dashboard/__init__`` went lazy) the entire handler tree —
for two lines of URL parsing. Keep this module stdlib-only.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import parse_qs, quote, urlparse

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
        # urlparse raises ValueError on a malformed IPv6 literal
        # ("http://[::1"), and .port raises ValueError on a non-integer port
        # ("http://host:notaport"). dashboard.url is a user-editable config
        # field parsed unguarded during gateway startup (SlackGateway.
        # _init_dashboard, cli_server, cli_doctor, mcp_core), so one typo aborted
        # the whole boot. Degrade to defaults instead — mirrors dashboard_origin().
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            port = parsed.port or _DEFAULT_PORT
        except ValueError:
            logger.warning("Ignoring malformed dashboard_url: %s", url)
            host, port = "", _DEFAULT_PORT
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


#: Explicit bind-address override for container/orchestrated deployments.
#: See :func:`bind_address_for`.
_ENV_BIND = "KIROCREW_BIND"


def bind_address_for(local_only: bool) -> str:
    """Return the TCP bind address string for aiohttp.

    ``KIROCREW_BIND`` (env) overrides the bind ADDRESS only — nothing else.
    It exists for containers: inside Docker, published ports (``-p``) map to
    the container's bridge interface, so a loopback-bound gateway is
    unreachable from the host even though the operator explicitly asked for
    the port to be exposed. The official image sets ``KIROCREW_BIND=0.0.0.0``;
    binding all interfaces *inside the container's network namespace* exposes
    nothing on the host beyond what ``-p`` explicitly publishes.

    Deliberately narrow:

    * ``local_only`` semantics are UNCHANGED — dashboard URLs, the CSRF
      origin allowlist, host canonicalization, and the strict internal-path
      rules keep their loopback behavior (correct for the common
      ``-p 5476:5476`` + ``http://localhost:5476`` mapping). Only the TCP
      bind widens.
    * Safe by construction: ``token_auth_middleware`` is mounted
      unconditionally in BOTH server paths (``start_dashboard`` and
      ``start_api_server``, which both resolve their bind through this
      function), so a wider bind never exposes an unauthenticated surface
      beyond the three ``PROBE_PATHS`` liveness probes (token-exempt by
      design, secret-free payloads). CSRF ``check_origin`` and the
      DNS-rebinding ``check_host`` barrier apply to every request except
      those same probe paths (orchestrators address pods by IP — see
      ``PROBE_PATHS``), and the ``is_direct_local_request`` gates (secret
      reveal, channel config writes) treat bridge peers as remote —
      fail-closed.
    * The value must parse as an IP address (``0.0.0.0``, ``::``, or a
      specific interface address). Anything else is ignored with a warning
      and the normal local-only resolution applies — a typo can only ever
      *narrow* exposure back to loopback, never widen it.
    """
    override = os.environ.get(_ENV_BIND, "").strip()
    if override:
        try:
            ipaddress.ip_address(override)
        except ValueError:
            logger.warning(
                "Ignoring %s=%r: not a valid IP address; binding loopback",
                _ENV_BIND,
                override,
            )
        else:
            logger.info(
                "%s=%s: binding this address instead of loopback. Every client "
                "must still present a valid dashboard token (token auth is "
                "always mounted); config writes remain restricted to direct "
                "local sessions.",
                _ENV_BIND,
                override,
            )
            return override
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
        lines.append("👻 Run 24/7:  see docs/guides/remote-and-mobile.md for systemd service setup")

    return lines


# ---------------------------------------------------------------------------
# Allowed-origin set
# ---------------------------------------------------------------------------


def build_allowed_origins(
    port: int,
    local_only: bool,
    configured_host: str = "",
    dashboard_url: str = "",
    tailnet_host: str = "",
) -> set[str]:
    """Compute the set of allowed origins for the dashboard.

    When *dashboard_url* is provided, its origin (scheme + host + port)
    is added as-is so that reverse-proxy setups (e.g. Caddy with TLS on
    a custom domain) pass the CSRF check without code changes.

    *tailnet_host* is this machine's MagicDNS name when tailnet access is
    enabled, and adds ``https://<name>`` — no port, because ``tailscale serve``
    fronts the dashboard on 443. Kept as a **parameter rather than a lookup** so
    this function stays pure and testable: the caller owns the daemon call and
    the validation (``dashboard/tailnet.py``), and passes "" when Tailscale is
    disabled, absent, or produced nothing trustworthy. Nothing here re-validates
    it, so nothing may pass an unvalidated value in.
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
    # Tailnet origin (§4). Already validated by dashboard/tailnet.py — see the
    # docstring: this function must not be the place that decides whether a
    # subprocess-derived hostname is trustworthy.
    if tailnet_host:
        origins.add(f"https://{tailnet_host}")
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
    # send Origin ``http://localhost:8777``). Only the bound port and these
    # opted-in ports are accepted, so a malicious local web page on an
    # arbitrary port cannot pass the CSRF origin check.
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
