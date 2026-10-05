"""The dashboard's response header policy.

Cache control for hashed build assets, the Content-Security-Policy with its frame
ancestors, Permissions-Policy, the ``/vendor`` CORS and Private Network Access answers,
and the browser hardening headers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _BASE_CSP,
        _DEFAULT_PORT,
        _IMMUTABLE_CACHE_CONTROL,
        _IMMUTABLE_PATH_PREFIXES,
        _INSTANCES_FRAME_SRC_EXTRA,
        _LOOPBACK_FRAME_SRC,
        _NO_STORE_CACHE_CONTROL,
        _PERMISSIONS_POLICY,
        _PNA_REQUEST_HEADER,
        _PNA_RESPONSE_HEADER,
        _VENDOR_CORS_HEADER_VALUE,
        _VENDOR_PATH_PREFIX,
        _VENDOR_PREFLIGHT_MAX_AGE_SECS,
        _WORKER_ASSET_MARKER,
        _WORKER_CACHE_CONTROL,
        _cookie_port_from_host,
        frame_ancestors_value,
        token_embed_parent_port,
    )


async def _vendor_preflight_handler(request: web.Request) -> web.Response:
    """Answer the CORS / Private Network Access preflight for ``/vendor/*``.

    Forward-compat: current Chromium blocks the insecure-initiator load at
    the CORS layer WITHOUT sending a PNA preflight (verified empirically —
    the GET-with-Access-Control-Allow-Origin path of ``_apply_security_headers`` is
    the live fix).
    Chrome's PNA rollout answers a private-network subresource fetch with a
    preflight OPTIONS carrying ``Access-Control-Request-Private-Network:
    true``; ``add_static`` registers GET/HEAD only, so if/when that ships
    for this initiator class the preflight would 405 and the runtime load
    would fail closed again. The PNA grant header is echoed only when the
    request actually asks for it, per the PNA spec's request/response
    pairing.
    """
    headers = {
        "Access-Control-Allow-Origin": _VENDOR_CORS_HEADER_VALUE,
        "Access-Control-Allow-Methods": "GET, HEAD",
        "Access-Control-Max-Age": str(_VENDOR_PREFLIGHT_MAX_AGE_SECS),
    }
    if request.headers.get(_PNA_REQUEST_HEADER, "").lower() == "true":
        headers[_PNA_RESPONSE_HEADER] = "true"
    return web.Response(status=204, headers=headers)


def _asset_cache_control(path: str) -> str | None:
    """Cache-Control for a content-hashed ``/assets/`` path, or ``None``.

    Returns the year-long ``immutable`` policy for an ordinary hashed chunk
    (its URL is its version, so it is safe to cache forever), the short-lived
    ``_WORKER_CACHE_CONTROL`` for a worker script (whose CSP lives in its own
    cached header, so it must be re-fetched within a minute of a header-only
    build while staying cache-servable across a brief gateway-down window), or
    ``None`` for a path that is not under ``/assets/`` at all — the caller then
    applies the default no-store policy.
    """
    if not path.startswith(_IMMUTABLE_PATH_PREFIXES):
        return None
    if _WORKER_ASSET_MARKER in path.rsplit("/", 1)[-1].lower():
        return _WORKER_CACHE_CONTROL
    return _IMMUTABLE_CACHE_CONTROL


def _extra_frame_ancestors(
    request: "web.Request | None", app: "web.Application | None" = None
) -> list[str]:
    """Exact parent origins (beyond ``'self'``) permitted to frame this dashboard.

    Read from the ``embed_parent_port`` claim of the request's signed token: the
    multi-instance connect flow mints the remote token carrying the *parent*
    (embedding) dashboard's port — its ``KIROCREW_PORT`` — so the embedded remote
    authorizes exactly that loopback parent origin as a CSP frame-ancestor. The
    claim is carried through the link→session token exchange into the session
    cookie (see token_auth_middleware), which also stashes the validated port on
    the request BEFORE it revokes the link nonce. This reader prefers that stashed
    value, then the query token, then the ``mc_token_<port>`` cookie — so it works
    for the first ``?token=`` framed document (whose link nonce is revoked by the
    exchange) AND every subsequent cookie-authenticated framed load. The port is
    expanded to the loopback hosts (the desktop app may load on any of them).
    Exact origins only — **never a wildcard, never a hardcoded port** — and gated
    on a validly-signed token, so a random local page (which has no token) can
    never get its origin into ``frame-ancestors`` (clickjacking, CSE SEC-016).
    Empty (default ``'self'`` + ``X-Frame-Options`` posture) for any request
    without such a token. See docs/system-specs/modules/security.md.
    """
    if request is None:
        return []
    # Prefer the claim the auth middleware validated and stashed on the request:
    # it is set BEFORE the link→session exchange revokes the link nonce, so the
    # first ``?token=`` framed document (whose header the browser enforces) still
    # carries the parent origin. Fall back to the query token, then the
    # ``mc_token_<port>`` session cookie (steady-state cookie-authenticated
    # framed loads), mirroring token_auth_middleware's own extraction.
    port: int | None = None
    stashed = request.get("embed_parent_port")
    if isinstance(stashed, str) and stashed.isdigit():
        _p = int(stashed)
        if 1 <= _p <= 65535:
            port = _p
    if port is None:
        # Prefer the credential token_auth actually VALIDATED (it publishes it
        # as request["auth_token"]): its extraction can adopt the session cookie
        # over an invalid query token, so a fixed query-then-cookie re-derivation
        # could read an unverified value. Fall back to that order only when no
        # credential was published (e.g. a surface that never reached the
        # middleware's authenticated paths).
        published = request.get("auth_token", "")
        token = published if isinstance(published, str) else ""
        if not token:
            token = request.query.get("token") or ""
        if not token:
            port_fallback = app.get("port", _DEFAULT_PORT) if app is not None else _DEFAULT_PORT
            cookie_port = _cookie_port_from_host(request, port_fallback)
            token = request.cookies.get(f"mc_token_{cookie_port}", "")
        port = token_embed_parent_port(token)
    if port is None:
        return []
    # A CSP host-source admits only letters, digits and hyphens in the host, so a
    # bracketed IPv6 literal cannot be expressed: `http://[::1]:<port>` is refused by
    # the browser ("the directive 'frame-ancestors' does not support the source
    # expression") and dropped, so it never granted anything — it only logged a
    # warning on every framed response. There is no valid spelling to substitute,
    # so an IPv6-loopback parent cannot be authorized at all.
    return [f"http://{host}:{port}" for host in ("127.0.0.1", "localhost", "kirocrew.localhost")]


def _apply_security_headers(
    resp: web.StreamResponse,
    app: web.Application,
    path: str = "",
    request: "web.Request | None" = None,
) -> None:
    """Apply cache-control and security headers to a dashboard response.

    Sets four groups of headers (all via ``setdefault`` so handlers keep
    the ability to override):

    1. Cache-Control / Pragma / Expires — prevent Chrome from caching stale
       assets across upgrades. Content-hashed paths (``/assets/``) are the
       exception: their URL *is* the version, so they are served as
       ``immutable`` instead (see ``_IMMUTABLE_PATH_PREFIXES``).
    2. Content-Security-Policy — defense-in-depth against XSS. Primary XSS
       protection is rehypeSanitize (strips script/iframe/form/foreignObject
       at HAST level before rendering). CSP allows ``'unsafe-inline'``
       because widget iframes (blob: sandbox) inherit parent CSP per W3C
       spec — inline scripts in widgets need it. Widget isolation is
       enforced by ``sandbox="allow-scripts"`` (no parent DOM access) +
       widget-level CSP meta (connect-src 'none'). When the instances
       feature is enabled, ``frame-src`` is extended with a loopback
       wildcard so dynamically-connected tunnel ports can be framed.
    3. Permissions-Policy — required by Chrome 143+ to permit
       ``navigator.clipboard.writeText`` even on secure contexts. Without
       an explicit ``clipboard-write=(self)`` grant, the Copy-link button
       on published artifacts fails with a permissions-policy violation
       (crbug.com/414348233).
    """
    # Immutable only on success — during cold-start a request to /assets/*
    # may get 404 (static route not mounted) or 503 (SPA fallback answering).
    # Caching that error with max-age=31536000 would be a permanent black
    # screen, the same bug class sw.js fixes for the cache layer.
    # 206 (range) and 304 (conditional) are also valid static-handler
    # responses for hashed assets: a 304's headers merge into the stored
    # cache entry, so answering it with no-store would degrade the cached
    # immutable bundle.
    #
    # This check is NOT sufficient on its own for the static route: aiohttp's
    # ``FileResponse`` is built with status 200 and only stats the file inside
    # ``prepare()``, after the middleware chain has returned. A missing chunk
    # therefore passes through here as a 200 and becomes a 404 later, still
    # wearing the immutable header. ``_finalize_asset_cache_control`` (an
    # ``on_response_prepare`` handler, which runs once the status is final)
    # closes that hole; this early decision stays as the common path.
    status = getattr(resp, "status", None)
    asset_cc = _asset_cache_control(path) if status in (200, 206, 304) else None
    if asset_cc is not None:
        resp.headers.setdefault("Cache-Control", asset_cc)
    else:
        resp.headers.setdefault("Cache-Control", _NO_STORE_CACHE_CONTROL)
        resp.headers.setdefault("Pragma", "no-cache")
        resp.headers.setdefault("Expires", "0")

    state = app.get("state")
    instances_mgr = getattr(state, "instances_manager", None) if state else None
    # Loopback preview origins are always framable (Web Preview panel); the
    # *.localhost tunnel wildcard is added only when instances mode is active.
    frame_src_extra = _LOOPBACK_FRAME_SRC + (
        _INSTANCES_FRAME_SRC_EXTRA if instances_mgr is not None else ""
    )
    # frame-ancestors: ``'self'`` plus the EXACT parent origin carried in the
    # request token's embed_parent_port claim (see _extra_frame_ancestors) — never
    # a wildcard, never a hardcoded port. Lets the desktop app frame an embedded
    # instance dashboard across loopback ports, while any local page without a
    # validly-signed token stays blocked (clickjacking).
    extra_ancestors = _extra_frame_ancestors(request, app)
    # Same builder the sandboxed-document responses use. Hand-joining here instead
    # would leave the shell as the one ancestor source nothing validates, which is
    # exactly how an inexpressible entry (a bracketed IPv6 literal) reached a
    # header before and made engines drop the whole directive.
    frame_ancestors = frame_ancestors_value(extra_ancestors)
    resp.headers.setdefault(
        "Content-Security-Policy",
        _BASE_CSP.format(
            connect_src_extra=_LOOPBACK_FRAME_SRC,
            frame_src_extra=frame_src_extra,
            frame_ancestors=frame_ancestors,
        ),
    )
    resp.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
    # CORS approval for the vendored runtime files fetched by null-origin
    # sandboxed iframes; pairs with the /vendor OPTIONS preflight handler.
    # See _VENDOR_PATH_PREFIX for the full Private-Network-Access rationale.
    if path.startswith(_VENDOR_PATH_PREFIX):
        resp.headers.setdefault("Access-Control-Allow-Origin", _VENDOR_CORS_HEADER_VALUE)
    # Defense-in-depth browser headers (CWE-1021/693/200/319). All via setdefault
    # so a handler can override. The clickjacking control is CSP ``frame-ancestors``
    # above. X-Frame-Options is origin-exact (SAMEORIGIN) and cannot express the
    # allowlist, so we keep it as the legacy backstop ONLY in the default posture
    # (no extra ancestor trusted); when an operator has configured a cross-port
    # embed origin we omit it, otherwise SAMEORIGIN would contradict the CSP and
    # refuse the embed. Browsers honor frame-ancestors over X-Frame-Options when
    # both are present. nosniff blocks MIME-confusion; Referrer-Policy avoids
    # leaking the (token-bearing) dashboard URL cross-origin. HSTS is inert over
    # the default loopback HTTP bind but protects HTTPS tunnel/desktop access, so
    # it is set unconditionally (browsers ignore it on plain HTTP).
    if not extra_ancestors:
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


async def _finalize_asset_cache_control(request: web.Request, response: web.StreamResponse) -> None:
    """``on_response_prepare`` hook: never let an error under ``/assets/`` out
    with ``immutable``.

    ``_apply_security_headers`` runs in middleware, when a ``FileResponse``
    still reports status 200 — aiohttp defers the ``stat`` to ``prepare()``.
    A request for a chunk the running ``dist/`` does not have (mid-upgrade, or
    a stale bundle asking for a chunk the new build renamed) thus reached the
    wire as ``404`` + ``public, max-age=31536000, immutable``, and Chromium
    kept that 404 for a year under the request URL. Lucide icon chunks keep
    their content hash across releases, so one poisoned entry breaks the module
    graph of every later bundle that imports it: the entry ``<script
    type=module>`` fails silently and the page never boots — tunnel rebuilds and
    gateway restarts cannot fix it because the cache key is the local URL. This
    hook runs after the status is final and overwrites (not ``setdefault``) the
    header for exactly that case: a hashed-asset path whose final status is not
    one the cacheable policies admit. Covers both the ``immutable`` policy of an
    ordinary chunk and the short-lived ``_WORKER_CACHE_CONTROL`` of a worker —
    the worker policy is cacheable (``max-age`` plus an intermediary
    ``stale-if-error``), so leaving it on a 404 would let a browser cache the
    error and an intermediary serve the stale bytes of an orphaned worker.
    """
    if response.status in (200, 206, 304):
        return
    if not request.path.startswith(_IMMUTABLE_PATH_PREFIXES):
        return
    if response.headers.get("Cache-Control") not in (
        _IMMUTABLE_CACHE_CONTROL,
        _WORKER_CACHE_CONTROL,
    ):
        return
    response.headers["Cache-Control"] = _NO_STORE_CACHE_CONTROL
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def _install_asset_cache_control_finalizer(app: web.Application) -> None:
    """Register ``_finalize_asset_cache_control`` on ``app``. Idempotent."""
    if _finalize_asset_cache_control not in app.on_response_prepare:
        app.on_response_prepare.append(_finalize_asset_cache_control)
