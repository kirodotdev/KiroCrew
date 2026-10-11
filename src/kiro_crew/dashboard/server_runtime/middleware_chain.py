"""The ordered middleware chains both gateway entrypoints install.

Each entrypoint builds its refusing barriers from the shared factories in
``security_middleware`` and hands them to its installer here, which holds the chain's
order, the layers only that chain uses and the SEL request audit. The tailnet trust and
the loopback-host canonicalization the chains are built with live here too.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _STRICT_INTERNAL_API_PATHS,
        AMBIGUOUS_LOOPBACK_HOSTS,
        DashboardState,
        KiroCrewConfig,
        _apply_security_headers,
        _holds_every_loopback_family,
        _is_spa_shell_request,
        _mixed_internal_api_paths,
        agent_route_arm,
        audit_actor,
        build_allowed_origins,
        build_host_canonical_redirect,
        degraded_config_files,
        handlers,
        logger,
        make_route_latency_middleware,
        mark_audit_claimed,
        reject_compressed_body_middleware,
        resolve_dashboard_host,
        sel,
        slot_ownership_middleware,
        tailnet,
        tailnet_effective_allowed_logins,
        tailnet_identity_unknown,
        token_auth_middleware,
    )


def _tailnet_origin_enabled() -> bool:
    """Read the live recovery opt-in; callers offload this blocking config read."""

    return bool(KiroCrewConfig.load().dashboard.tailscale.enabled)


async def _resolve_tailnet_trust(_cfg: KiroCrewConfig) -> tailnet.TailnetTrust:
    """The identity-trust value both entrypoints build token auth with.

    Identity trust (RFC §2–§3.1): validated at config load, governance ceiling
    applied inside the shared helper — ONE code path for both startup surfaces,
    so they cannot drift.
    """
    _ts_cfg = _cfg.dashboard.tailscale
    return await tailnet.governed_tailnet_trust(
        _ts_cfg.trust_identity,
        tailnet_effective_allowed_logins(_cfg.degraded_sections, _ts_cfg.allowed_logins),
        _ts_cfg.pin_scope,
        bind_refresh_chains=_ts_cfg.bind_refresh_chains,
        # An unreadable tailnet policy resolves allowed_logins to [] and so
        # trust_identity to False, which is "no login restriction". The values
        # alone cannot tell that from "never configured"; degraded_sections can.
        identity_unknown=tailnet_identity_unknown(_cfg.degraded_sections),
        unreadable_files=tuple(degraded_config_files(_cfg.degraded_sections)),
    )


def _dashboard_canonical_redirect(local_only: bool, state: DashboardState) -> Any:
    """The dashboard's loopback-host canonicalization middleware.

    Host canonicalization: converge loopback aliases (127.0.0.1 / localhost /
    kirocrew.localhost) onto a single origin so the SPA's per-origin
    localStorage (theme, zoom, layout, notifications, ...) is never split
    across hostnames. localStorage keys on scheme://host:port, so reaching the
    dashboard on "localhost" one time and "kirocrew.localhost" the next (e.g.
    `kirocrew token` printing localhost while the gateway
    auto-opens kirocrew.localhost) lands the browser in a different, empty
    bucket and all settings appear reset. The canonical host is resolved once
    at startup (it is stable for the gateway's lifetime). Only top-level
    document GET/HEAD navigations on a non-canonical loopback alias are
    redirected (see should_canonicalize_host); APIs, WebSockets, and
    sub-resource fetches are untouched — once the document settles on the
    canonical host every later request is already canonical. Disabled unless
    local_only, so reverse-proxy / remote-host deployments are never affected.

    Gated on holding every family the canonical name resolves to, resolved at
    redirect time rather than here: the canonical host is fixed before either
    socket is bound, so a degraded second bind -- or a listener that dies
    later -- must be able to withdraw the redirect, not just the sidecar.
    """
    _canonical_host = resolve_dashboard_host(local_only) if local_only else ""
    return build_host_canonical_redirect(
        _canonical_host,
        holds_every_family=(
            (lambda: _holds_every_loopback_family(state))
            if _canonical_host in AMBIGUOUS_LOOPBACK_HOSTS
            else None
        ),
    )


def _install_dashboard_middlewares(
    app: web.Application,
    *,
    deny_audit_middleware: Callable,
    host_canonical_redirect: Any,
    host_validation_middleware: Callable,
    csrf_middleware: Callable,
    internal_secret: str,
    port: int,
    local_only: bool,
    tailnet_trust: tailnet.TailnetTrust,
    tailnet_host: str,
    configured_host: str,
    dashboard_url: str,
) -> None:
    """Install the dashboard's middleware chain on *app*, outermost first.

    The order is a security contract: route latency outermost, then the deny-audit
    boundary outer to every barrier that can refuse, the host canonicalization,
    the ``Host`` barrier, the header policy, CSRF, token auth, the SEL request
    audit and the per-slot ownership checkpoint, with the SPA fallback innermost.
    ``_register_workflow_lifecycle`` and ``_register_crewmate_prune_gate`` append
    their gates after it. The barriers are built by the entrypoint from the shared
    factories; the layers only this chain uses are built here.
    """

    # No-cache: prevents Chrome from caching stale assets
    @web.middleware  # type: ignore[misc]
    async def no_cache_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        resp = await handler(request)  # type: ignore[operator]
        if hasattr(resp, "headers"):
            _apply_security_headers(resp, request.app, request.path, request)
        return resp  # type: ignore[return-value]

    # SPA fallback: serve index.html for client-side React Router paths.
    # Uses the same _is_spa_shell_request predicate as the auth middleware so
    # the two layers never drift. Bare /apps/{name} paths (no sub-path) are
    # treated as SPA navigations and served index.html — this fixes browser
    # refresh on e.g. /apps/code-review-sage which has no server-side route.
    @web.middleware  # type: ignore[misc]
    async def spa_fallback(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        try:
            return await handler(request)  # type: ignore[operator]
        except web.HTTPNotFound:
            if _is_spa_shell_request(request):
                return await handlers.index(request)
            raise

    # SEL: log mutating API operations
    _sel_log_methods = {"POST", "PUT", "DELETE", "PATCH"}

    @web.middleware  # type: ignore[misc]
    async def sel_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method in _sel_log_methods and request.path.startswith("/api/"):
            # Claim only what this middleware actually records. Its except arm
            # logs a refusal raised below this point, so the boundary must not
            # add a second entry for it — but a request OUTSIDE this branch is
            # logged nowhere here, and claiming it would hand the boundary a
            # promise no one keeps (a cross-origin WebSocket GET refused in its
            # handler would be silently unaudited).
            mark_audit_claimed(request)
            from kiro_crew.sel import sel

            # Every mutating /api/ call was filed under the flat
            # ``dashboard_user``, so an action a forwarder relayed on the
            # owner's behalf read exactly like the owner performing it.
            actor = audit_actor(request, "dashboard_user")
            try:
                resp = await handler(request)  # type: ignore[operator]
                sel().log_api_access(
                    caller=actor,
                    operation=f"{request.method} {request.path}",
                    outcome="ok" if resp.status < 400 else "error",
                    resources=request.path,
                )
                return resp  # type: ignore[return-value]
            except Exception as exc:
                sel().log_api_access(
                    caller=actor,
                    operation=f"{request.method} {request.path}",
                    outcome="error",
                    resources=request.path,
                    error=str(exc)[:200],
                )
                raise
        return await handler(request)  # type: ignore[operator]

    # Explicit middleware ordering — self-documenting and immune to future insertions
    app.middlewares[:] = [
        # Outermost: privacy-safe per-route latency. Times the FULL
        # in-gateway handling (all middleware + handler). Labels are limited to
        # method / bounded route_template / status_class — never a real path,
        # query, id, or body — so it cannot leak content or explode cardinality.
        make_route_latency_middleware(),
        # Outer to every barrier that can refuse, so a pre-audit 403 is recorded
        # by POSITION rather than by each deny site remembering to. Inner to the
        # latency middleware only, which keeps that one's "times the FULL
        # in-gateway handling" contract intact.
        deny_audit_middleware,
        host_canonical_redirect,
        host_validation_middleware,
        # Compressed request bodies are refused outright (415): the hardened
        # runner runs with auto_decompress=False (see dashboard.slowloris), so
        # they could never be served — this gives senders the honest error
        # before any handler reads raw compressed bytes.
        reject_compressed_body_middleware,
        no_cache_middleware,
        csrf_middleware,
        token_auth_middleware(
            internal_paths=_STRICT_INTERNAL_API_PATHS,
            mixed_internal_paths=_mixed_internal_api_paths(),
            internal_secret=internal_secret,
            port=port,
            local_only=local_only,
            spa_shell_handler=handlers.index,
            tailnet_trust=tailnet_trust,
            agent_route_arm=agent_route_arm,
        ),
        sel_audit_middleware,
        # Inner to token auth (it reads the ``app`` claim) and to the audit
        # record: every /api/chat/slots/{slot}/* route takes one app-ownership
        # decision here before its handler runs (dashboard/slot_ownership.py).
        slot_ownership_middleware,
        spa_fallback,
    ]

    # Verify security invariant: if dashboard_url expands the CSRF origin
    # set for a remote URL, token auth middleware MUST be active.
    if dashboard_url:
        _has_token_auth = any(getattr(mw, "_is_token_auth", False) for mw in app.middlewares)
        if _has_token_auth:
            app["allowed_origins"] = build_allowed_origins(
                port, local_only, configured_host, dashboard_url, tailnet_host=tailnet_host
            )
            logger.info(
                "dashboard_url=%s: added to CSRF allowed origins (token auth verified)",
                dashboard_url,
            )
        else:
            logger.error(
                "dashboard_url=%s requires token auth — refusing to start without it. "
                "Enable Slack or remove dashboard.url from config.",
                dashboard_url,
            )
            raise RuntimeError("dashboard_url requires token auth middleware")


def _install_api_middlewares(
    app: web.Application,
    *,
    deny_audit_middleware: Callable,
    host_validation_middleware: Callable,
    csrf_middleware: Callable,
    internal_secret: str,
    port: int,
    local_only: bool,
    tailnet_trust: tailnet.TailnetTrust,
) -> None:
    """Install the headless (``--slack-only``) API server's middleware chain on *app*.

    At parity with :func:`_install_dashboard_middlewares` over the MCP route surface
    both servers mount: route latency outermost, then the deny-audit boundary, the
    ``Host`` barrier, CSRF, token auth with no SPA shell, the SEL request audit and
    the per-slot ownership checkpoint. ``_register_workflow_lifecycle`` appends its
    gate after it.
    """
    # SEL audit middleware — log mutating MCP tool calls
    _sel_methods = {"GET", "POST", "PUT", "PATCH", "DELETE"}

    @web.middleware  # type: ignore[misc]
    async def sel_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.method in _sel_methods and request.path.startswith("/api/"):
            # Claim only what this middleware records — same contract as the
            # dashboard's (see origin.AUDIT_CLAIMED_KEY): its except arm owns a
            # refusal raised below this point, and a request it does not log is
            # left unclaimed so the boundary can record one.
            mark_audit_claimed(request)
            # ``sel`` resolves through the server module's globals; no in-function
            # import needed (the host and CSRF barriers read it the same way).
            # Same forwarder distinction as the dashboard chain's: this server is
            # reached the same way, so its records must be readable the same way.
            actor = audit_actor(request, "mcp_tool")
            try:
                resp = await handler(request)  # type: ignore[operator]
                sel().log_api_access(
                    caller=actor,
                    operation=f"{request.method} {request.path}",
                    outcome="ok" if resp.status < 400 else "error",
                    resources=request.path,
                )
                return resp  # type: ignore[return-value]
            except Exception as exc:
                sel().log_api_access(
                    caller=actor,
                    operation=f"{request.method} {request.path}",
                    outcome="error",
                    resources=request.path,
                    error=str(exc)[:200],
                )
                raise
        return await handler(request)  # type: ignore[operator]

    # Explicit ordering mirrors start_dashboard: latency → deny-audit → host →
    # csrf → token → audit.
    app.middlewares[:] = [
        # Outermost: privacy-safe, bounded-cardinality per-route latency (rec #1).
        # The MCP routes are registered AFTER this assignment, so the middleware
        # captures its route-template set LAZILY on the first request (by which
        # point every route is registered) — see make_route_latency_middleware.
        make_route_latency_middleware(),
        # Outer to every barrier that can refuse: a pre-audit 403 is recorded by
        # POSITION here, not by each deny site remembering to.
        deny_audit_middleware,
        host_validation_middleware,
        # 415 for compressed request bodies — same rationale as start_dashboard
        # (the hardened runner never decompresses; see dashboard.slowloris).
        reject_compressed_body_middleware,
        csrf_middleware,
        token_auth_middleware(
            internal_paths=_STRICT_INTERNAL_API_PATHS,
            mixed_internal_paths=_mixed_internal_api_paths(),
            internal_secret=internal_secret,
            port=port,
            local_only=local_only,
            # No SPA shell in headless mode: a no-token request must be denied
            # outright, never served an HTML shell (there is no UI to boot).
            spa_shell_handler=None,
            tailnet_trust=tailnet_trust,
            agent_route_arm=agent_route_arm,
        ),
        sel_audit_middleware,
        # Same per-slot app-ownership checkpoint as the dashboard chain, so a
        # per-slot route registered on this server is decided the same way.
        slot_ownership_middleware,
    ]
