"""Shared origin-validation helpers for CSRF and WebSocket checks.

Centralises dashboard URL parsing, bind-address resolution, origin-set
construction, and per-request origin validation so that ``server.py``
(CSRF middleware), ``ws.py`` (WebSocket handshake), and ``gateway.py``
(startup messages) all share a single source of truth.

The only user-facing config is ``dashboard.url`` — a single URL like
``http://my-host.example.com:8080``.  Everything else (port, bind
address, allowed origins) is derived from it.

This module holds the checks that inspect a live ``web.Request``. The
request-independent URL / host / origin-set arithmetic lives in the stdlib-only
:mod:`kiro_crew.dashboard.urls` leaf and is re-exported below, so ``from
kiro_crew.dashboard.origin import ...`` keeps working for every existing caller
while CLI and MCP-stdio callers can import the leaf directly and skip the
``aiohttp`` stack.
"""

from __future__ import annotations

import logging

# ``socket`` is not used directly here anymore (the hostname helpers moved to
# ``urls``), but it is re-exported on purpose: callers and tests patch
# ``origin.socket.gethostname`` / ``.gethostbyname``, and because this is the one
# shared stdlib module object, patching it through this name still reaches the
# helpers in ``urls``.
import socket  # noqa: F401
from typing import TYPE_CHECKING
from urllib.parse import urlparse

# Re-exported for backwards compatibility — every name that used to be defined
# in this module is still importable from it.
from kiro_crew.dashboard.urls import (  # noqa: F401
    _BIND_ALL,
    _BIND_LOCAL,
    _CANONICALIZABLE_LOOPBACK_HOSTS,
    _DEFAULT_PORT,
    _ENV_BIND,
    _ensure_scheme,
    _host_without_port,
    bind_address_for,
    build_allowed_hosts,
    build_allowed_origins,
    build_dashboard_url,
    dashboard_origin,
    devspaces_proxy_url,
    format_dashboard_urls,
    is_local_only,
    is_loopback,
    machine_hostname,
    parse_dashboard_url,
    resolve_dashboard_host,
    should_canonicalize_host,
)

if TYPE_CHECKING:  # aiohttp is needed for annotations only
    from aiohttp import web

logger = logging.getLogger(__name__)

#: Liveness/readiness probe paths. Orchestrators (Kubernetes kubelet, Docker
#: HEALTHCHECK behind a remapped port, load balancers) address these by pod /
#: container IP, so their Host header can never appear in the dashboard's
#: host allowlist. The probe surface is exempted from the DNS-rebinding Host
#: barrier — safe because the handlers are token-free, secret-free, and
#: reveal build identity only when BOTH direct-local AND the Host allowlist
#: agree (see handlers.core._liveness_payload), so a rebound request gets
#: only the liveness bit an attacker can infer from the TCP connect anyway.
PROBE_PATHS = frozenset({"/api/health", "/api/live", "/api/ready"})


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


def is_proxied_request(request: web.Request) -> bool:
    """Return ``True`` when ``request.remote`` is a PROXY rather than the client.

    The question this answers is narrow and specific: *can anything keyed on
    ``request.remote`` identify one client?* It cannot when a proxy terminated
    the connection, because then every client behind that proxy presents the
    same address.

    The signal is the presence of any :data:`_PROXY_FORWARD_HEADERS` entry, and
    **peer locality is deliberately NOT part of the test**. Two proxy shapes
    exist and both collapse the address:

    * **same-host** — a tunnel or reverse proxy on this machine (cloudflared,
      ngrok, ``tailscale serve``, a local nginx) connecting from loopback; and
    * **upstream** — a proxy on *another* host (an nginx box, a container
      bridge, a LAN jump host) in front of a widened bind
      (``KIROCREW_BIND``), which presents a NON-loopback peer.

    An earlier version of this predicate required a loopback peer, on the
    premise that a non-loopback peer is the client itself. That premise is only
    true when no upstream proxy exists — with one, the non-loopback peer is the
    proxy and the address is just as shared. Scoping to loopback therefore
    reported the second shape as per-client, reproducing exactly the untrue
    claim this predicate was added to remove.

    Accepted trade-off, stated because it is a deliberate direction: a client
    that sends a forwarding header with no proxy in the path (a transparent
    forward proxy, a misconfigured client) is reported as proxied when its
    address is in fact its own. That is an OVER-warning. On a surface whose job
    is to say whether a security control is effective, over-warning is the safe
    error and under-warning is not.

    This is NOT the inverse of :func:`is_direct_local_request`, which also
    requires loopback because it answers a different question ("is this the
    local machine", for the secret-reveal and config-write gates).
    """
    return any(h in request.headers for h in _PROXY_FORWARD_HEADERS)


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
