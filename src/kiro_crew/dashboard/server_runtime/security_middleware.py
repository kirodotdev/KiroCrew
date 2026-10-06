"""The refusing layers both gateway chains are built from.

The deny-audit boundary, the ``Host`` and CSRF barriers with the ``_audit_denied``
record they share, the audited actor label, the edition-contributed mixed internal
paths, and the loopback-host canonical redirect.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _CSRF_SAFE_METHODS,
        _MIXED_INTERNAL_API_PATHS,
        _PRE_AUDIT_DENY_STATUSES,
        _STRICT_INTERNAL_API_PATHS,
        _VIA_PROXY_SUFFIX,
        AUDIT_CLAIMED_KEY,
        PROBE_PATHS,
        check_host,
        check_origin,
        current_context,
        internal_path_matches,
        is_csrf_exempt,
        is_proxied_request,
        logger,
        mark_audit_claimed,
        safe_context_call,
        sel,
        sel_is_warm,
        should_canonicalize_host,
    )


def audit_actor(request: web.Request, caller: str) -> str:
    """The identity to file this request's audit record under.

    ``caller`` is the label the middleware was built with (``dashboard_user``
    for the full dashboard, ``mcp_tool`` for the headless API server), or an
    identity a deny site already derived from the request.

    A FORWARDED request is filed under a DIFFERENT name. The gateway binds
    loopback, so remote access arrives through a same-host forwarder (a tunnel,
    a sidecar, a reverse proxy) which presents the credential it was given: with
    the owner's cookie that is indistinguishable from the owner sitting at the
    machine, and every such request was recorded as plain ``dashboard_user``.
    That is the one fact an operator reading the log afterwards most needs and
    could not get -- whether an action was taken by the person or arrived over a
    forwarding path on their behalf.

    The signal is :func:`origin.is_proxied_request`: any ``Forwarded`` /
    ``X-Forwarded-*`` / ``X-Real-IP`` header. It is the predicate the rest of
    the gateway bootstrap already trusts for "``request.remote`` is not the client", and
    it over-warns rather than under-warns (a client that sends a forwarding
    header with no proxy in the path is reported as forwarded). For an audit
    label, over-warning is the safe direction: it never files a forwarded action
    as the person's own.

    Its known limit is the same one :func:`origin.is_direct_local_request`
    documents: a forwarder that strips every forwarding header is invisible
    here. This makes the ordinary product paths distinguishable, which is what
    the log could not do at all before; it is not a boundary against a forwarder
    that is deliberately hiding.

    An APP-token request is filed under the app's name, read from the ``app``
    claim ``token_auth_middleware`` publishes, rather than under ``caller``:
    otherwise every app call reads as the person's own action. That is the name
    every app-isolation row and the deny-audit boundary already record. An
    internal-secret request whose app claim was DERIVED from the calling session
    (a managed tool call made by an app's agent) keeps its transport in the
    label, ``<caller>:<app>``, so it is never filed as the app's own client. A
    request that carries no claim (the claim is ``""`` for the dashboard user,
    absent before token auth runs) keeps ``caller``.
    """
    request_app = request.get("app", "")
    actor = caller
    if isinstance(request_app, str) and request_app:
        actor = f"{caller}:{request_app}" if request.get("internal_auth") is True else request_app
    return f"{actor}{_VIA_PROXY_SUFFIX}" if is_proxied_request(request) else actor


async def _audit_denied(caller: str, request: web.Request, error: str) -> None:
    """Record a middleware refusal in the SEL, best-effort.

    Shared by every middleware that denies BEFORE ``sel_audit_middleware`` runs
    (that one is registered inner to them, so a bare raise produces a 403 that
    appears nowhere in the audit log). One helper rather than per-site calls
    because the property below is easy to omit at a new deny site and
    invisible when omitted:

    * BEST-EFFORT — a trust root too short to sign the chain makes construction
      raise, and an unguarded write would turn the refusal into a 500: losing
      the denial in order to report it.

    No thread hop on the healthy path: the SEL singleton is warmed at startup
    (:func:`kiro_crew.sel.warm_sel_singleton`, awaited by both start paths
    before the middleware chain is built), so ``log_api_access`` here only
    enqueues to the writer thread (after its one-time start on first
    ``log()``). The warm is best-effort, though: when it FAILS, the
    next ``sel()`` retries ``_init_locked`` -- trust-dir creation, key load,
    a tail read of the log -- on the calling thread, and this helper runs on
    the event loop for every denied request. So the hop is kept for exactly
    that case, gated on :func:`kiro_crew.sel.sel_is_warm`: two attribute
    reads on the healthy path, a worker thread on the degraded one, never
    blocking file I/O on the loop. The ``except`` stays because construction
    can raise on either path.

    Calling this CLAIMS the request (:func:`origin.mark_audit_claimed`) so the
    deny-audit boundary outer to every barrier does not record the same refusal
    a second time. The claim is set unconditionally, before the write: a write
    that failed here fails identically in the boundary, so retrying in the
    boundary buys nothing.

    ``caller`` goes through :func:`audit_actor`, so a refusal that arrived
    through a forwarder is filed under ``<caller>_via_proxy``. Applied here, in
    the one helper every barrier's deny path already calls, rather than at each
    of the three call sites -- a new barrier gets it by using the helper.
    """
    mark_audit_claimed(request)
    actor = audit_actor(request, caller)

    def _write() -> None:
        sel().log_api_access(
            caller=actor,
            operation=f"{request.method} {request.path}",
            outcome="denied",
            resources=request.path,
            error=error,
        )

    try:
        if sel_is_warm():
            _write()
        else:
            await asyncio.to_thread(_write)
    except Exception:
        logger.warning("Failed to log a middleware denial to SEL", exc_info=True)


def _make_deny_audit_middleware(caller: str) -> Callable:
    """Build the audit boundary for refusals raised BEFORE the audit middleware.

    SHARED by BOTH entrypoints (``start_dashboard`` and the ``--slack-only``
    ``start_api_server``) so the two chains can never drift — same rationale as
    :func:`_make_host_validation_middleware`.

    ``sel_audit_middleware`` is registered INNER to the Host, CSRF and token
    barriers, so a refusal one of them raises produces a 403 that the audit
    middleware never observes. The three known sites each call
    :func:`_audit_denied` themselves and a source-string test pins that they keep
    doing so — but a pin only catches what someone remembers to run, and the
    omission is invisible in production: the refusal simply appears nowhere in
    the audit log. That is the deny-or-audit violation the pin exists to paper
    over.

    Registered OUTER to every barrier, this middleware makes the guarantee
    positional. It catches the refusal on its way out and records it unless some
    inner layer already claimed the request, so a future deny site that forgets
    everything is still audited; forgetting now costs the record's reason
    DETAIL, not the record. The per-site calls become enrichment rather than the
    guarantee.

    Its scope is deliberately narrow, so the audit surface is unchanged and no
    refusal is recorded twice:

    * Only a RAISED ``web.HTTPException`` whose status is in
      :data:`_PRE_AUDIT_DENY_STATUSES`. Everything else propagates untouched.
    * Only an UNCLAIMED request (:data:`origin.AUDIT_CLAIMED_KEY`). A layer claims
      when it has written the specific record itself: the two barriers through
      :func:`_audit_denied`, ``sel_audit_middleware`` for the requests it
      actually logs (so its ``outcome="error"`` entry for a handler's 403 is not
      doubled), and the two WebSocket origin refusals that log their own denial.
      All four go through :func:`origin.mark_audit_claimed`. Not claiming is the
      safe direction: the refusal is then recorded here under a generic reason.
      The one refusal that reaches this middleware unclaimed today is
      ``ws.py``'s cross-origin WebSocket 403, which was audited nowhere before.
    * Returned responses are NOT inspected. ``token_auth_middleware`` returns
      its 401/403 rather than raising and audits each with a specific reason
      code, so its records stay single.

    Best-effort and off the loop come from :func:`_audit_denied`; the refusal is
    re-raised unchanged either way, so an audit failure can never convert a 403
    into a 500.

    ``caller`` is only the FALLBACK label. A refusal raised inner to
    ``token_auth_middleware`` carries an authenticated identity on the request by
    the time it reaches here, and recording the static label instead would file an
    app's or a user's refusal under ``dashboard_user`` — the attribution problem
    ``handlers.terminal``'s own deny site avoids by reading
    ``request["user"]``. Note ``request["app"]`` is ``""`` for the dashboard user
    and that emptiness is POSITIVE proof of them (see ``token_auth``), so an empty
    app falls through to the user rather than to the label.
    """

    @web.middleware  # type: ignore[misc]
    async def deny_audit_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        try:
            return await handler(request)  # type: ignore[operator]
        except web.HTTPException as exc:
            if exc.status in _PRE_AUDIT_DENY_STATUSES and not request.get(AUDIT_CLAIMED_KEY):
                # Status and reason only — never the exception body. The record
                # already carries method, path and caller; what a claimed record
                # adds is the deny site's own explanation, which by definition
                # is missing here.
                # ``audit_actor`` adds the app claim itself, so pass the
                # transport label only and the app is named once.
                await _audit_denied(
                    request.get("user") or caller,
                    request,
                    f"refused with {exc.status} {exc.reason} before the audit middleware",
                )
            raise

    return deny_audit_middleware


def _make_host_validation_middleware(caller: str) -> Callable:
    """Build the DNS-rebinding ``Host``-header barrier middleware.

    SHARED by BOTH entrypoints (``start_dashboard`` and the ``--slack-only``
    ``start_api_server``) so the two chains can never drift — same rationale
    as ``server._STRICT_INTERNAL_API_PATHS``. In particular this is the SINGLE
    exemption point for ``origin.PROBE_PATHS``: a change to the exemption is
    necessarily a change in both servers, where test_api_health.py pins it
    through a real middleware chain (disallowed-Host probe allowed,
    disallowed-Host non-probe denied).

    Rejects any request whose ``Host`` header does not name a host we serve.
    Runs on EVERY method (GET data-exfil is the rebinding payload) and
    independently of the CSRF Origin check and loopback trust — a rebound
    request is loopback at the socket but forges ``Host``. See
    ``origin.check_host`` for the missing-Host and empty-allowlist
    deny-by-default carve-outs.

    Probe exemption: orchestrator health probes (kubelet, Docker HEALTHCHECK,
    LBs) address the gateway by container/pod IP, which by construction is
    never in the host allowlist. The probe handlers are token-free/secret-free
    and additionally gate their identity fields on ``check_host``, so
    exempting them leaks nothing a rebound page could not already infer from
    a bare TCP connect (see ``origin.PROBE_PATHS``). This is a permanent,
    deliberate carve-out in a security control: treat ANY addition to
    ``PROBE_PATHS`` as a security review.

    ``caller`` labels the SEL audit line (``dashboard_user`` for the full
    dashboard, ``mcp_tool`` for the headless API server).
    """

    @web.middleware  # type: ignore[misc]
    async def host_validation_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if request.path not in PROBE_PATHS and not check_host(request):
            # SEL audit (security-relevant permission decision): make
            # DNS-rebinding attempts visible in the audit log, mirroring the
            # API-access audit.
            await _audit_denied(
                caller,
                request,
                f"host header not allowed: {request.headers.get('Host', '')[:100]}",
            )
            raise web.HTTPForbidden(
                text="Host header not allowed.",
                content_type="text/plain",
            )
        return await handler(request)  # type: ignore[operator]

    return host_validation_middleware


def _make_csrf_middleware(caller: str) -> Callable:
    """Build the cross-site CSRF barrier middleware.

    SHARED by BOTH entrypoints (``start_dashboard`` and the ``--slack-only``
    ``start_api_server``) so the two chains can never drift — same rationale as
    :func:`_make_host_validation_middleware`. In particular this is the SINGLE
    read point for ``token_auth.CSRF_EXEMPT_EXACT_METHODS``, so an exemption can
    never be granted on one server and withheld on the other.

    Blocks state-mutating requests that a cross-origin page issued. Loopback
    local processes (mcp-core, cron, doctor) send no Origin header and are
    trusted by ``check_origin``; a browser always sends Origin, so a cross-site
    page is rejected here even before token auth runs.

    Webhook exemption: a self-authenticating external webhook is a
    server-to-server caller that sends neither ``Origin`` nor ``Referer``, which
    ``check_origin`` can only accept from a loopback peer — so without the
    exemption the route is unreachable in the topology that exposes the gateway
    directly, with no configuration that fixes it. Those handlers ignore cookies
    and authenticate a bearer credential a browser cannot forge, which is the
    entire threat CSRF addresses; ``token_auth.CSRF_EXEMPT_EXACT_METHODS`` holds
    the full decision, and any addition to it is a security review. The exempted
    request is still audited — ``sel_audit_middleware`` logs every mutating
    ``/api/`` call in both chains — so the carve-out writes no SEL event of its
    own, matching ``PROBE_PATHS`` on the Host barrier.

    ``caller`` labels the SEL audit line (``dashboard_user`` for the full
    dashboard, ``mcp_tool`` for the headless API server).
    """

    @web.middleware  # type: ignore[misc]
    async def csrf_middleware(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        guarded = request.method not in _CSRF_SAFE_METHODS and not is_csrf_exempt(
            request.path, request.method
        )
        if guarded and not check_origin(request, require=True, fallback_header="Referer"):
            await _audit_denied(
                caller,
                request,
                "CSRF check failed: origin not allowed: "
                f"{request.headers.get('Origin', '')[:100]}",
            )
            raise web.HTTPForbidden(
                text="CSRF check failed: request origin not allowed.",
                content_type="text/plain",
            )
        return await handler(request)  # type: ignore[operator]

    return csrf_middleware


def _would_soften_a_strict_path(candidate: str) -> bool:
    """Whether admitting *candidate* to the mixed set reclassifies a strict route.

    BOTH directions, because `internal_path_matches` is prefix-based and the
    request is what gets matched, not the entry:

    * candidate is a strict entry, or a CHILD of one — the obvious case.
    * candidate is an ANCESTOR of a strict entry — the case a one-directional
      check misses. Contributing ``/api/browser`` against the strict
      ``/api/browser/command`` admits every route beneath it, so a request for
      the strict path matches BOTH sets, and token_auth's off-loopback arm tests
      ``_matches_mixed`` first (``elif _matches_internal: if _matches_mixed:``) —
      the strict hard-deny is replaced by cookie acceptance.

    The docstring's "never an app root, enumerate" is guidance; this is the
    enforcement, so the ancestor direction is not left to the contributor.
    """
    if internal_path_matches(candidate, _STRICT_INTERNAL_API_PATHS):
        return True
    return any(internal_path_matches(strict, {candidate}) for strict in _STRICT_INTERNAL_API_PATHS)


def _mixed_internal_api_paths() -> frozenset[str]:
    """``_MIXED_INTERNAL_API_PATHS`` plus the edition's contributed paths.

    Both middleware construction sites build their mixed set through here — the
    dashboard chain and the headless ``--slack-only`` one — so the two can never
    disagree about which routes an internal loopback caller may reach. Drift
    there is an auth bug, not a cosmetic one.

    WHY A SEAM AT ALL. An edition mounts its routes through
    ``DashboardContributor.contribute_routes``, so the core cannot name those
    paths in a module-level frozenset. Without the contribution, an edition's own
    MCP tool authenticating with the loopback ``X-Internal-Secret`` handshake is
    not recognized as internal: token_auth ignores the secret, falls through to
    cookie auth, and the tool answers ``Token required`` on every call.

    TWO LIMITS THE CORE ENFORCES rather than trusting the contributor:

    * a contributed path matching a CORE STRICT entry is DROPPED. Strict and mixed
      differ off-loopback — strict hard-denies, mixed accepts a validated
      cookie — so admitting one would soften a route the core deliberately keeps
      loopback-only. The overlap is checked in BOTH directions (see
      :func:`_would_soften_a_strict_path`): a contributed ANCESTOR of a strict
      entry reclassifies it just as a child does. Dropping is audited, because a
      silently-ignored contribution and an honoured one look identical from the
      edition's side.
    * the result is a UNION, so a contribution can never remove a core entry. A
      contributor returning an unrelated or empty set is harmless by construction,
      which is why the read below can fail closed to "no contribution".

    Fail-closed through ``safe_context_call``, the idiom this repo centralizes for
    exactly this seam: a ``PlatformCompositionError`` is RE-RAISED, because a host
    that could not compose its companion must abort rather than fall back to
    open-source defaults, while any other contributor failure degrades to no
    contribution. A contributor that raises, hands back a generator that raises
    part-way through iteration, returns a non-iterable, or yields non-string
    entries therefore contributes nothing rather than widening the admitted set on
    a value the core could not check — and none of those can abort the gateway
    bind, which is what a raise escaping middleware construction would do.

    BOTH outcomes are recorded, because each is invisible to a different party: a
    dropped contribution is invisible to the EDITION, and an honoured one is
    invisible to the OPERATOR. So the admitted set is logged and SEL-audited at
    composition time alongside the drop audit — without it SEL cannot tell a
    deployment whose auth surface an edition widened from a stock one. A public
    build contributes nothing and stays silent.
    """

    def _read() -> set[str]:
        # LOOKUP separated from INVOCATION on purpose. Guarding the call itself
        # against AttributeError would also swallow one raised INSIDE an
        # implemented contributor, so a genuinely broken edition would take the
        # silent "predates the seam" path and contribute nothing with no warning —
        # indistinguishable from an honoured empty contribution, which is the
        # confusion the audit below exists to remove. A MISSING method is the happy
        # path (returns nothing, silently); a BROKEN one raises and is reported.
        reader = getattr(current_context().dashboard, "mixed_internal_api_paths", None)
        if reader is None:
            return set()
        # Materialized INSIDE the thunk. A contributor may hand back a generator,
        # and one that raises part-way through iteration is a contributor failure
        # like any other — but the comprehension is where it surfaces, so leaving
        # it outside would let it escape middleware construction and stop the
        # gateway binding at all. A non-iterable raises TypeError here and lands on
        # the same degrade path.
        return {p for p in reader() if isinstance(p, str) and p.startswith("/")}

    def _degraded() -> set[str]:
        # Invoked only on the degrade path and INSIDE the except block, so
        # ``exc_info`` still carries the live exception. WARNING rather than the
        # helper's debug line because a broken contributor is a fault an operator
        # has to see: the edition's tool will answer Token required with nothing
        # else naming the cause.
        logger.warning(
            "dashboard contributor mixed_internal_api_paths failed; "
            "contributing no internal paths",
            exc_info=True,
        )
        return set()

    # safe_context_call, not a hand-written try/except: it is the CPP fail-closed
    # idiom this repo centralizes, and the reason is exactly the divergence a copy
    # invites — a bare ``except Exception`` swallows PlatformCompositionError, and a
    # non-standalone host that could not compose its companion MUST abort rather
    # than silently fall back to open-source defaults. Degrading THAT to the core
    # set would answer a mis-composed edition with a quietly narrower auth surface.
    entries = safe_context_call(_read, fallback_factory=_degraded, log_message=None)

    softening = {p for p in entries if _would_soften_a_strict_path(p)}
    if softening:
        # Loud, and dropped rather than honoured: the edition asked for a route
        # the core keeps loopback-only to be reachable off-loopback with a cookie.
        logger.error(
            "dashboard contributor tried to soften strict internal paths to mixed; " "dropping %s",
            sorted(softening),
        )
        try:
            sel().log_api_access(
                caller="dashboard_contributor",
                operation="mixed_internal_api_paths",
                outcome="denied",
                source="dashboard",
                resources=",".join(sorted(softening)),
                error="would soften a core strict path",
            )
        except Exception:  # pragma: no cover - audit must not change the outcome
            logger.debug("SEL audit for dropped internal paths failed", exc_info=True)
        entries -= softening

    if entries:
        # The symmetric half of the drop audit, and the reason both exist: a
        # dropped contribution is invisible to the EDITION, and an honoured one is
        # invisible to the OPERATOR. Without this, SEL cannot distinguish a
        # deployment whose auth surface an edition widened from a stock one, which
        # is exactly the composed surface SEL exists to make visible.
        #
        # Only when something was actually admitted: a public build contributes an
        # empty set, so staying silent there keeps every stock gateway start free
        # of a line that says nothing.
        logger.info(
            "dashboard contributor admitted %d internal-reachable path(s): %s",
            len(entries),
            sorted(entries),
        )
        try:
            sel().log_api_access(
                caller="dashboard_contributor",
                operation="mixed_internal_api_paths",
                outcome="allowed",
                source="dashboard",
                resources=",".join(sorted(entries)),
            )
        except Exception:  # pragma: no cover - audit must not change the outcome
            logger.debug("SEL audit for admitted internal paths failed", exc_info=True)

    return _MIXED_INTERNAL_API_PATHS | frozenset(entries)


def build_host_canonical_redirect(
    canonical_host: str, holds_every_family: Callable[[], bool] | None = None
) -> Any:
    """Build the loopback-host-canonicalization middleware.

    Converges non-canonical loopback aliases (127.0.0.1 / ::1 / localhost) onto
    *canonical_host* with a 302 so the SPA's per-origin localStorage settings
    are not split across hostnames. Only top-level document GET/HEAD navigations
    are redirected (see :func:`should_canonicalize_host`); APIs, WebSockets, and
    sub-resource fetches are untouched. Pass ``canonical_host=""`` (e.g. when not
    local_only) to make the middleware a no-op so reverse-proxy / remote-host
    deployments are never redirected.

    *holds_every_family* answers, at REDIRECT time, whether this gateway holds
    every loopback family the destination name can resolve to. It gates the same
    rule the local-token mint follows, for the same reason: an ambiguous name
    names a SET of listeners, and a 302 onto it is a credential send, because the
    browser follows it carrying the host-only ``Lax`` session cookie on exactly
    the top-level navigation this middleware converts. ``?token=`` in the query
    is refused separately and is not the only credential in play.

    A destination this gateway does not fully hold therefore gets NO redirect: the
    document stays on the literal it dialled, which this gateway does hold. The
    cost is the split-localStorage annoyance the redirect exists to avoid, paid
    only while a family is uncovered -- and the most common reason it is uncovered
    is that another process holds that family's port, which is the party the
    redirect would hand the cookie to.

    Omitting the callable keeps the redirect ungated, for callers whose
    *canonical_host* is not an ambiguous name (a literal names one listener) and
    for unit tests of the gating rules themselves.

    Extracted to a module-level factory (rather than an inline closure) so the
    runtime behavior — the 302, port+path+``?token=`` preservation, and the
    gating — is unit-testable.
    """

    @web.middleware  # type: ignore[misc]
    async def host_canonical_redirect(
        request: web.Request,
        handler: object,
    ) -> web.StreamResponse:
        if canonical_host and should_canonicalize_host(
            request.host,
            canonical_host,
            method=request.method,
            sec_fetch_dest=request.headers.get("Sec-Fetch-Dest"),
            carries_credential=bool(request.query.get("token")),
        ):
            if holds_every_family is not None and not holds_every_family():
                logger.warning(
                    "not canonicalizing %s onto %s: this gateway does not hold every "
                    "loopback family that name resolves to, so the redirect would carry "
                    "the session cookie to whoever holds the rest",
                    request.host,
                    canonical_host,
                )
                return await handler(request)  # type: ignore[operator]
            # Preserve port + path + query -- only host changes. A navigation
            # carrying ?token= never reaches here, so that query cannot be moved
            # to a host other than the one it was addressed to.
            raise web.HTTPFound(location=str(request.url.with_host(canonical_host)))
        return await handler(request)  # type: ignore[operator]

    return host_canonical_redirect
