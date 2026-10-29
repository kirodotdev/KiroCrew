"""Dashboard token authentication.

HMAC-SHA256 token generation, validation, IP binding, consumption
tracking, and aiohttp middleware for Slack-gated dashboard access.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import json
import logging
import re
import sys as _sys
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.dashboard import tailnet_owner
from kiro_crew.dashboard.kiro_relay.admission import admit_relay_caller
from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller
from kiro_crew.dashboard.origin import (
    is_https_request,
    is_loopback,
    is_proxied_request,
    mark_audit_claimed,
)
from kiro_crew.dashboard.refresh_tokens import (
    MAX_REFRESH_TTL_SECS,
    REFRESH_COOKIE_PATH,
    bind_chain_peer,
    cookie_jar_needs_pruning,
    foreign_port_cookies,
    generate_refresh_token,
    refresh_cookie_name,
)

# Canonical revocation-generation definitions live in revocation_gen so the
# refresh-token module can consult the counter without recreating the
# token_auth <-> refresh_tokens import cycle (same pattern as token_secret
# below). Re-exported here for backwards compatibility. Both validators read
# the LIVE value via current_revocation_gen() — never an import-time copy —
# so a bump is visible to every subsequent validation in-process.
from kiro_crew.dashboard.revocation_gen import (  # noqa: F401  # re-exports
    _REVOCATION_FILE,
    _load_revocation_gen,
    bump_revocation_gen,
    current_revocation_gen,
    current_revocation_gen_or_none,
)
from kiro_crew.dashboard.tailnet import (
    REFUSE_NOT_ALLOWED,
    ForwardedPeer,
    TailnetTrust,
    forwarded_peer_refusal,
    is_forwarded_tailnet_request,
    login_allowed,
    peer_pin_key,
    peer_pin_key_for_claim,
    resolve_forwarded_peer,
)
from kiro_crew.dashboard.tailnet_direct import DIRECT_PEER_REQUEST_KEY, DirectGate, GateRef
from kiro_crew.dashboard.tailnet_direct import refusal_response as tailnet_direct_refusal
from kiro_crew.dashboard.tailnet_direct import run_with_reverify
from kiro_crew.dashboard.token_auth_app_scope import _enforce_app_scope, _prime_app_api_allowlist
from kiro_crew.dashboard.token_auth_callers import (
    _derive_internal_caller_app,
    _internal_caller_record_missing,
    _unix_request_socket,
    _verify_unix_peer,
)
from kiro_crew.dashboard.token_auth_paths import (
    _APPS_UI_BYPASS_RE,
    _BYPASS_EXACT,
    _BYPASS_EXACT_METHODS,
    _BYPASS_PREFIXES,
    _credential_mismatch_detail,
    _deny,
    _is_spa_shell_request,
    _log_auth,
)
from kiro_crew.dashboard.token_auth_tokens import (
    MAX_SESSION_TTL_SECS,
    _b64url_decode,
    _cookie_port_from_host,
    _ct_eq,
    _get_revoked_store,
    _state,
    bind_token_peer,
    check_token_peer,
    claims_an_app_unverified,
    generate_token,
    required_peer_key_unverified,
    requires_verified_peer_unverified,
    validate_token_with_app,
)

# Canonical HMAC-secret definitions live in token_secret to break the import
# cycle between token_auth and refresh_tokens. Re-exported here for backwards
# compatibility — callers elsewhere import these names from token_auth. The
# fork keeps the LAZY _get_secret() (NOT an eager module-level _SECRET =
# _load_or_create_secret()) so that merely importing this module never writes
# token_signing.key into $KIROCREW_HOME (the CLI imports token_auth for every
# kirocrew subcommand; an import-time write would break gateway --seed and
# pollute the home for read-only commands).
from kiro_crew.dashboard.token_secret import (  # noqa: F401  # re-exports
    _SECRET_KEY_FILE,
    _get_secret,
    _load_or_create_secret,
)
from kiro_crew.sel import sel as _sel_fn
from kiro_crew.shared_crew.identity import publish_actor


def internal_path_matches(path: str, entries: Iterable[str]) -> bool:
    """Return whether *path* is an exact or child path of an internal route."""
    return any(path == entry or path.startswith(entry + "/") for entry in entries)


logger = logging.getLogger("kiro_crew.dashboard.token_auth")


def token_auth_middleware(
    *,
    internal_paths: frozenset[str] = frozenset(),
    mixed_internal_paths: frozenset[str] = frozenset(),
    internal_secret: str = "",
    port: int = 5476,
    local_only: bool = True,
    spa_shell_handler: Callable[..., Any] | None = None,
    tailnet_trust: TailnetTrust | None = None,
    tailnet_direct: DirectGate | GateRef | None = None,
) -> Callable[..., Any]:
    """Factory returning aiohttp middleware for token-based dashboard auth.

    ALL requests require a valid token — loopback is no longer exempt
    because local port forwarders (socat, ssh -R, custom scripts) make
    remote traffic appear as 127.0.0.1, bypassing auth entirely.

    *internal_paths* are exact paths that internal processes (mcp-core,
    doctor) call — these require loopback AND a matching
    ``X-Internal-Secret`` header (read from ``~/.kiro/crew/.local_secret``).
    Non-loopback access to these paths is always denied.

    *mixed_internal_paths* are paths called by BOTH internal processes
    (loopback + secret) AND the browser (cookie auth).  On non-loopback
    they perform explicit cookie validation (deny-by-default) instead
    of hard-denying, so DCV/SSH-forwarded browsers polling these routes
    (e.g. ``/api/spawn`` every 5s) don't trigger false session-expired
    banners.  Use this for any internal-path that the browser polls.

    *tailnet_trust* is the operator's identity-trust opt-in (RFC §2–§3.1,
    validated at config load). When set and enabled, a request arriving from
    the local ``tailscale serve`` proxy is attributed to the daemon-verified
    tailnet peer: the session pin binds to a ``ts:``-prefixed peer key instead of
    the proxy's loopback address, the allowlist is enforced, and audit records
    name the login. When ``None`` (the default, and every failure mode of the
    resolution) behaviour is byte-for-byte the existing token+IP path, except
    for sessions carrying ``require_peer=1``: those explicitly opt out of the
    fallback and fail closed until the daemon verifies an allowed peer.

    *tailnet_direct* is the gate of the direct tailnet listener
    (:mod:`kiro_crew.dashboard.tailnet_direct`). It decides only requests whose
    local socket address is one of this machine's tailnet addresses: those are
    authenticated by the daemon's ``whois`` of the TCP peer and the login
    allowlist, with no dashboard token. Every other request, loopback and
    ``tailscale serve`` alike, runs the token flow unchanged. A
    :class:`~kiro_crew.dashboard.tailnet_direct.GateRef` is read per request,
    so the gateway can fill it in after the bind.
    """

    # NOTE: the signing-secret and revoked-nonce singletons are NOT warmed
    # here anymore. This factory is invoked from `start_dashboard()` /
    # `start_api_server()`, which are `async def` and therefore run ON the
    # event loop — a synchronous warm-up here (blocking key-file read plus, on
    # Windows, an owner-only DACL on first create) would block the loop during
    # startup (no-blocking-call-on-event-loop). The async startup paths instead
    # `await warm_auth_singletons()` (which offloads to a worker thread) BEFORE
    # constructing this middleware chain, so the first auth op still hits the
    # already-built singletons without any blocking I/O landing on the loop.

    def _extract_and_validate_token(
        request: web.Request, _port: int
    ) -> tuple[bool, str, str, str, str, bool]:
        """Extract token from query param or cookie and validate it.

        Returns ``(valid, user_id, reason, app_name, token, from_query)``. Used by
        internal-path browser/app auth (no secret header). The main auth flow
        has its own extraction with peer-binding and from_cookie tracking that
        this helper intentionally does not replicate — but its callers still
        run the peer-pin check on the returned token, so an internal path never
        accepts a session pin the main flow would refuse.

        ``from_query`` names the credential the verdict belongs to: true for the
        ``?token=`` query credential, false for the session cookie, including a
        cookie adopted after a failed query token. It is the main flow's
        ``not from_cookie``, so a caller can publish the same
        ``auth_from_query_token`` mark from the credential actually validated.
        """
        cookie_name = f"mc_token_{_cookie_port_from_host(request, _port)}"
        query_token = request.query.get("token") or ""
        cookie_token = request.cookies.get(cookie_name, "")
        if not query_token and not cookie_token:
            return False, "", "no token", "", "", False
        if query_token:
            valid, uid, reason, app = validate_token_with_app(query_token, use_session_exp=True)
            # A stale ``?token=`` must not veto a still-valid session cookie
            # (same rule as the main flow): fall through to the cookie only
            # when the query token failed AND a cookie exists; otherwise the
            # query token's verdict (and its failure reason) stands.
            #
            # An APP token is excluded from the fallback. An app's UI is served
            # from this same origin, so the browser attaches the dashboard
            # user's cookie alongside the app's own ``?token=``; adopting that
            # cookie when the app token has expired would replace an app-scoped
            # identity with the user's own and skip the ``_enforce_app_scope``
            # gate below. An expired app token must be refused as an expired
            # app token, so the caller re-exchanges its app secret.
            if valid or not cookie_token or claims_an_app_unverified(query_token):
                return valid, uid, reason, app, query_token, True
        valid, uid, reason, app = validate_token_with_app(cookie_token, use_session_exp=True)
        return valid, uid, reason, app, cookie_token, False

    def _current_direct_gate() -> DirectGate | None:
        if isinstance(tailnet_direct, GateRef):
            return tailnet_direct.gate
        return tailnet_direct

    async def _direct_tailnet_gate(
        request: web.Request, handler: object, gate: DirectGate
    ) -> tuple[web.StreamResponse | None, object]:
        """Decide a request on the direct tailnet listener before any token is read.

        Returns ``(response, handler)``. A response is final: a refusal, or the
        handler's answer for a token-free request the daemon admitted. ``None``
        hands the request to the token flow, which then also enforces its own
        credential: that is only an APP token, so an app keeps its scope. A
        human token or cookie on this listener is ignored, never required.
        """
        path = request.path
        is_ws = (request.headers.get("Upgrade") or "").lower() == "websocket"
        direct_peer, refusal = await gate.verify(request, fresh=is_ws)
        if refusal or direct_peer is None:
            refusal = refusal or "whois_failed"
            mark_audit_claimed(request)
            _sel_fn().log_api_access(
                caller=direct_peer.login if direct_peer is not None else (request.remote or ""),
                forwarded=is_proxied_request(request),
                operation="tailnet_direct_auth",
                outcome="denied",
                source="token_auth",
                resources=path,
                error=refusal,
            )
            return tailnet_direct_refusal(request, refusal), handler
        request[DIRECT_PEER_REQUEST_KEY] = direct_peer
        # Every response is re-verified while it runs, not only a WebSocket: an
        # event stream lives as long, and a removed person must lose it too.

        async def _reverified(req: web.Request) -> Any:
            return await run_with_reverify(req, gate, direct_peer, handler)  # type: ignore[arg-type]

        wrapped: object = _reverified
        # Machine-to-machine paths stay loopback-only, and the internal secret
        # is never accepted off loopback.
        strict = internal_path_matches(path, internal_paths) and not internal_path_matches(
            path, mixed_internal_paths
        )
        if strict or "X-Internal-Secret" in request.headers:
            mark_audit_claimed(request)
            _log_auth(request, direct_peer.login, "denied", "tailnet direct: internal path")
            return _deny(request, "Forbidden"), wrapped
        query_token = request.query.get("token") or ""
        if query_token and claims_an_app_unverified(query_token):
            return None, wrapped
        # The owner's own Tailscale user gets the owner subject (tailnet_owner).
        subject, unconfirmed = tailnet_owner.direct_subject(request, gate, direct_peer)
        if unconfirmed is not None:
            mark_audit_claimed(request)
            _log_auth(request, direct_peer.login, "denied", "tailnet direct: confirm required")
            return unconfirmed, wrapped
        request["user"] = subject
        request["app"] = ""
        request["auth_token"] = ""
        request["auth_tailnet_direct"] = True
        request["is_dashboard_user"] = True
        request["auth_from_query_token"] = False
        _log_auth(request, direct_peer.login, "granted", "tailnet direct whois")
        await publish_actor(request, trust=tailnet_trust, serve_peer=None)
        return await wrapped(request), wrapped  # type: ignore[operator]

    @web.middleware
    async def middleware(request: web.Request, handler: object) -> web.StreamResponse:
        gate = _current_direct_gate()
        if gate is not None and gate.owns(request):
            response, handler = await _direct_tailnet_gate(request, handler, gate)
            if response is not None:
                return response
        return await _token_flow(request, handler)

    async def _token_flow(request: web.Request, handler: object) -> web.StreamResponse:
        path = request.path

        # A relay-delivered request (``dashboard/kiro_relay``): the relay is its
        # authentication, so the person it named is admitted (or refused) here,
        # before any branch that weighs a Crew credential, a tailnet peer or a
        # loopback address. No relayed request continues past this point.
        _relay_caller = published_relay_caller(request)
        if _relay_caller is not None:
            _relay_refusal = admit_relay_caller(request, _relay_caller)
            if _relay_refusal is not None:
                return _relay_refusal
            return await handler(request)  # type: ignore[operator,no-any-return]

        # Forwarded-peer resolution (RFC §2), once per request — the WebSocket
        # path therefore resolves once at upgrade, never per frame. ``None`` on
        # every unresolvable or failure case, in which case everything below is
        # byte-for-byte the existing token+IP behaviour.
        #
        # Gated on the request PRESENTING A CREDENTIAL (query token, an access
        # or refresh cookie), not on where in the middleware it would be
        # consumed: a credential-less request (static assets, probes) can never
        # bind or satisfy a pin, so resolving identity for it would only hand
        # an unauthenticated local caller a header-driven daemon spawn. The
        # gate is credential-PRESENCE rather than validity so the allowlist
        # deny below still covers the /api/auth/refresh bypass.
        peer: ForwardedPeer | None = None
        if (
            tailnet_trust is not None
            and tailnet_trust.enforces_identity
            and (
                bool(request.query.get("token"))
                or any(c.startswith(("mc_token_", "mc_refresh_")) for c in request.cookies)
            )
        ):
            peer = await resolve_forwarded_peer(request, tailnet_trust)
            if peer is not None and not login_allowed(peer.login, tailnet_trust.allowed_logins):
                # The allowlist is mandatory (RFC §3): a daemon-verified login
                # that the operator did not allowlist is denied outright — a
                # positive identity outside the allowlist must never fall
                # through to a path it could satisfy with a leaked token.
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=peer.login,
                    forwarded=is_proxied_request(request),
                    operation="tailnet_peer_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error="login not in allowed_logins",
                )
                _log_auth(request, peer.login, "denied", "tailnet login not allowed")
                return _deny(request, "tailnet login not allowed")
            if (
                peer is None
                and tailnet_trust.identity_unknown
                and is_forwarded_tailnet_request(request, tailnet_trust)
            ):
                # Config load could not read the operator's allowlist, and this
                # forwarded tailnet peer could not be attributed either (daemon
                # down, timeout, or a header that disagreed). Falling through
                # here would admit it on the token alone — which is exactly the
                # widening this gate exists to stop, so the "unknown policy"
                # deny would announce itself and then not happen.
                #
                # Distinct from ``forwarded_peer_refusal`` below, which still
                # admits a whois-named allowlisted login as Unknown: here the
                # allowlist itself is missing, so no login can be checked.
                #
                # ``is_forwarded_tailnet_request`` is what keeps this from being
                # a lockout: a local request resolves to no peer for an entirely
                # different reason and is not touched, so the operator can still
                # reach the dashboard to repair config.json.
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=request.remote or "",
                    forwarded=is_proxied_request(request),
                    operation="tailnet_peer_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error="tailnet allowlist unreadable and peer unresolved",
                )
                _log_auth(request, "", "denied", "tailnet identity policy unreadable")
                return _deny(request, "tailnet login not allowed")
            refusal = await forwarded_peer_refusal(request, tailnet_trust, peer)
            if refusal is not None:
                # The allowlist fails closed: a forwarded tailnet request that
                # names no allowed login is refused, never admitted unnamed.
                _sel_fn().log_api_access(
                    caller=request.remote or "",
                    forwarded=True,
                    operation="tailnet_peer_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error=refusal,
                )
                _log_auth(request, "", "denied", f"tailnet identity unverified: {refusal}")
                if refusal == REFUSE_NOT_ALLOWED:
                    return _deny(request, "tailnet login not allowed")
                return _deny(request, "tailnet identity unverified")
        # Audit attribution (RFC §3): when a peer resolved, the trail names a
        # person; otherwise it stays the immediate peer address as today.
        _caller = peer.login if peer is not None else (request.remote or "")

        def _audit_uid(user_id: str) -> str:
            """The identity ``_log_auth`` should record as the caller.

            The daemon-verified login when a peer resolved (the RFC's audit
            requirement — the trail names a person, not the proxy or a bare
            token subject); the token identity otherwise.
            """
            return peer.login if peer is not None else user_id

        def _peer_key_for_request() -> str:
            """The pin key this request must satisfy (RFC §3)."""
            if peer is not None and tailnet_trust is not None:
                return peer_pin_key(peer, tailnet_trust.pin_scope)
            return f"ip:{request.remote or 'unknown'}"

        def _check_pin(
            token: str, *, allow_unbound_require_peer_link: bool = False
        ) -> tuple[bool, str]:
            """Peer-pin check with an honest failure reason.

            When the stored pin is a tailnet identity but NO peer resolved on
            this request, the mismatch is reported as the identity being
            UNVERIFIED rather than a device mismatch: this request could not
            establish who is behind the proxy (daemon blip, header stripped,
            or a genuinely different client), and "device identity mismatch"
            would send the user chasing a re-enrolled device that never
            changed. Fail-closed either way — an identity-pinned session is
            never satisfiable by an unverified proxied request.
            """
            requires_peer = requires_verified_peer_unverified(token)
            if peer is None and requires_peer:
                # Restart-persistent phone cookies intentionally outlive the
                # in-memory pin map.  Do not let an empty map plus a transient
                # whois failure downgrade such a cookie (or its original link)
                # to the proxy's shared ip:127.0.0.1 identity.
                return False, "tailnet identity unverified"
            request_peer_key = _peer_key_for_request()
            if requires_peer:
                signed_peer_key = required_peer_key_unverified(token)
                if signed_peer_key and peer is not None:
                    # Existing credentials keep the scope they were issued
                    # with even if the operator later changes the default.
                    request_peer_key = peer_pin_key_for_claim(peer, signed_peer_key)
                if signed_peer_key and signed_peer_key != request_peer_key:
                    mismatch = (
                        "device identity mismatch"
                        if signed_peer_key.startswith("ts:node:")
                        else "peer identity mismatch"
                    )
                    return False, mismatch
                if not signed_peer_key and not allow_unbound_require_peer_link:
                    # A claimless one-time QR link is allowed to acquire its
                    # first binding during exchange.  A surviving COOKIE is
                    # different: after restart an empty map cannot say which
                    # allowed node originally owned it, so legacy claimless
                    # sessions fail closed and must scan one new QR.  A hot
                    # in-memory binding is not accepted as a substitute: doing
                    # so would let that legacy cookie mint a claimless child.
                    return False, "tailnet session device binding missing"
            ok, mismatch = check_token_peer(token, request_peer_key)
            if not ok and peer is None and mismatch != "IP mismatch":
                mismatch = "tailnet identity unverified"
            if ok and peer is not None and not _state.has_binding(token):
                # For restart-persistent sessions the signed peer claim above
                # has already proved this is the ORIGINAL device. Ordinary
                # sessions retain the historical first-verified-peer behaviour.
                # Scoped to resolved peers: re-pinning ip: keys would change
                # app-token and multi-hop semantics that predate identity pins.
                repin_key = request_peer_key
                bind_token_peer(token, repin_key)
                _sel_fn().log_api_access(
                    caller=peer.login,
                    forwarded=is_proxied_request(request),
                    operation="tailnet_peer_bind",
                    outcome="granted",
                    source="token_auth",
                    resources=f"{repin_key} (first-use re-pin)",
                )
            return ok, mismatch

        # Internal API paths: loopback + secret grants immediate access.
        # If the secret is missing (browser request), fall through to
        # normal cookie auth so dashboard pages can call these routes.
        _matches_strict = internal_path_matches(path, internal_paths)
        _matches_mixed = internal_path_matches(path, mixed_internal_paths)
        # local_only=False: treat ALL internal paths as mixed (backward compat
        # with mainline's local_only semantics — user opted into remote access)
        if not local_only and _matches_strict and not _matches_mixed:
            _matches_mixed = True
            _matches_strict = False
        _matches_internal = _matches_strict or _matches_mixed
        # A request on the dashboard's unix socket is same-machine by
        # construction (the socket lives in the 0700 data home), so it
        # qualifies as "local" for the internal branch even though it has no
        # loopback peer IP (request.remote is empty for AF_UNIX transports).
        _unix_sock = _unix_request_socket(request) if _matches_internal else None
        if _matches_internal and (_unix_sock is not None or is_loopback(request.remote or "")):
            # Kernel-attested peer verification (AF_UNIX only): deny a caller
            # whose /proc ancestry resolves to a DIFFERENT session than the
            # one its X-Session-Key header declares. Runs before either auth
            # flavor grants — a mismatched peer is denied no matter what
            # credentials it carries. TCP loopback is untouched (no peer
            # credentials to check).
            if _unix_sock is not None:
                _peer_deny = await _verify_unix_peer(request, _unix_sock, path)
                if _peer_deny is not None:
                    return _peer_deny
            _has_secret_header = "X-Internal-Secret" in request.headers
            if _has_secret_header:
                _provided_secret = request.headers["X-Internal-Secret"]
                # Secret header present — validate it strictly
                if not internal_secret:
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=_caller,
                        forwarded=is_proxied_request(request),
                        operation="internal_auth",
                        outcome="denied",
                        source="token_auth",
                        resources=path,
                        error="no internal secret configured",
                    )
                    _log_auth(request, "internal", "denied", "no internal secret configured")
                    return _deny(request, "Forbidden")
                if _ct_eq(internal_secret, _provided_secret):
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=_caller,
                        forwarded=is_proxied_request(request),
                        operation="internal_auth",
                        outcome="granted",
                        source="token_auth",
                        resources=path,
                    )
                    _log_auth(request, "internal", "granted", "")
                    # Mark the grant so handlers can distinguish "the internal
                    # loopback caller (kiro-cli / MCP) authenticated" from "no
                    # auth ran at all".
                    request["internal_auth"] = True
                    if path == "/api/chat" or path.startswith("/api/chat/"):
                        from kiro_crew.dashboard.handlers._shared import private_chat_route_refusal

                        memory_refusal = await private_chat_route_refusal(request)
                        if memory_refusal is not None:
                            return memory_refusal
                    # Derive the app identity ONCE, here, so every ownership
                    # check downstream sees it. The secret proves
                    # the call came from inside, not who made it, so identity
                    # comes from the authenticated calling session.
                    #
                    # Publishing is deliberately NARROWING-ONLY: the claim is
                    # set only when an app is positively resolved. When the
                    # caller is the person, ``request["app"]`` is left ABSENT
                    # rather than set to ``""`` — several sites read a PRESENT
                    # empty claim as positive proof of the dashboard user
                    # (``"app" not in request or request["app"] != ""`` in
                    # handlers/source_providers, and handlers/kiro_prerequisite),
                    # so writing ``""`` here would turn their refusal of this
                    # transport into an admission. Absence keeps those exactly
                    # as they were; presence makes the app-ownership guards bite.
                    _derived_app = _derive_internal_caller_app(request)
                    if _derived_app:
                        request["app"] = _derived_app
                        # POSITIVE non-dashboard-user signal for the WS scope
                        # gate, which must never infer trust from a falsy app
                        # claim (CWE-269).
                        request["is_dashboard_user"] = False
                    elif _internal_caller_record_missing(request):
                        # A delegated caller whose OWN record is gone. Absence of
                        # an app claim is only trustworthy for a caller that
                        # never had a record to carry one; a cron whose job was
                        # deleted mid-run, or a subagent evicted from the
                        # registry, would otherwise keep the app reach the
                        # deleted record was the only proof of. Refused rather
                        # than admitted as the person -- the narrow, positively
                        # confirmed case, not every unplaceable caller (a Slack
                        # thread has no record to be missing).
                        _sel = _sel_fn()
                        _sel.log_api_access(
                            caller=_caller,
                            forwarded=is_proxied_request(request),
                            operation="internal_auth",
                            outcome="denied",
                            source="token_auth",
                            resources=path,
                            error="delegated caller's own record is gone",
                        )
                        _log_auth(request, "internal", "denied", "delegated record missing")
                        return _deny(request, "Forbidden", "caller_record_missing")
                    await publish_actor(
                        request, trust=tailnet_trust, serve_peer=None, internal=True
                    )
                    return await handler(request)  # type: ignore[operator]
                # Wrong secret → deny (don't fall through). An empty provided
                # value means the gateway received no credential from this
                # caller. Masking that withholds it and listener desync are
                # both possible; the client-facing decoder states that without
                # promising one repair. It is not the instance mix-up a
                # held-but-stale credential would be.
                _detail = f"wrong secret ({_credential_mismatch_detail(internal_secret, _provided_secret)})"
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=_caller,
                    forwarded=is_proxied_request(request),
                    operation="internal_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error=_detail,
                )
                _log_auth(request, "internal", "denied", _detail)
                if not _provided_secret:
                    return _deny(request, "Forbidden", "internal_secret_missing")
                return _deny(request, "Forbidden", "internal_auth_mismatch")
            # No secret header (browser request) → verify cookie/query-param auth
            # inline to satisfy deny-by-default: positively confirm auth
            # at the decision point rather than deferring to downstream.
            # NOTE: uses _extract_and_validate_token helper (defined above)
            # for cookie/query-param validation.
            _valid, _uid, _reason, _app, _tok, _from_query = _extract_and_validate_token(
                request, port
            )
            if not _valid:
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=_caller,
                    forwarded=is_proxied_request(request),
                    operation="internal_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error=f"cookie auth failed: {_reason}",
                )
                _log_auth(request, "internal", "denied", f"cookie auth failed: {_reason}")
                return _deny(request, "Forbidden")
            # Session-pin enforcement (RFC §3). Internal paths validate the
            # cookie, and skipping the pin here would let a peer-pinned session
            # be replayed against /api/chat, /api/spawn and friends from a
            # client the pin excludes.
            _pin_ok, _pin_mismatch = _check_pin(_tok)
            if not _pin_ok:
                _log_auth(request, _audit_uid(_uid), "denied", _pin_mismatch)
                return _deny(request, _pin_mismatch)
            # Expose identity so downstream handlers (and app-scope) see it.
            request["user"] = _uid
            request["app"] = _app
            # The credential actually validated (see the main path's note).
            request["auth_token"] = _tok
            # POSITIVE dashboard-user signal for the WS scope gate: the WS
            # layer must never infer trust from a falsy app claim (CWE-269).
            request["is_dashboard_user"] = not _app
            # WHICH credential authenticated, as the main path publishes it: the
            # helper reports the one it validated, so a stale ``?token=`` beside
            # a live cookie still reads as the cookie.
            request["auth_from_query_token"] = _from_query
            # App tokens are confined to their declared scope even on internal
            # paths (e.g. /api/chat, /api/spawn are mixed_internal) — otherwise
            # an app token would reach them on loopback with NO app identity set
            # and be treated as the dashboard user (privilege escalation).
            await _prime_app_api_allowlist(_app)
            _scope_deny = _enforce_app_scope(request, _app, path)
            if _scope_deny is not None:
                return _scope_deny
            # A dashboard-user cookie poll on a mixed-internal path is the same
            # request the ``_log_auth`` row below already audits (operation
            # ``dashboard.token_auth``), so emitting an ``internal_auth`` row
            # here too double-logs one request — the signal the high-frequency
            # ``/api/workflows/runs`` poll dominates. The ``_log_auth`` row
            # carries the daemon-verified caller (``_audit_uid`` — the peer
            # login when a peer resolved, else the token subject), so the single
            # surviving row names the identity the suppressed row would have.
            # An app token is a distinct security decision (its scoped reach onto
            # a mixed route), so it keeps the per-layer ``source=token_auth`` row.
            if _app:
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=_caller,
                    forwarded=is_proxied_request(request),
                    operation="internal_auth",
                    outcome="granted",
                    source="token_auth",
                    resources=path,
                    error="cookie auth (no secret header)",
                )
            _log_auth(request, _audit_uid(_uid), "granted", f"cookie auth for {_uid}")
            await publish_actor(request, trust=tailnet_trust, serve_peer=peer)
            return await handler(request)  # type: ignore[operator]
        elif _matches_internal:
            if _matches_mixed:
                # Mixed paths on non-loopback (DCV/SSH-forwarded browsers):
                # explicit cookie validation, mirroring the loopback
                # no-secret-header branch above.  Deny-by-default —
                # positively confirm auth at this decision point rather
                # than relying on downstream fall-through.
                # If X-Internal-Secret header is present, validate it first
                # (defense-in-depth: wrong secret = deny, even with valid cookie)
                if "X-Internal-Secret" in request.headers:
                    if not internal_secret or not _ct_eq(
                        internal_secret, request.headers["X-Internal-Secret"]
                    ):
                        # Same fingerprint detail and code as the loopback arm
                        # above. This arm was left on the bare string, so a
                        # denial here still could not say which side was wrong --
                        # in particular an ABSENT credential (a caller that could
                        # read no credential file at all) read identically to a
                        # caller holding the wrong one, which is the exact
                        # confusion the fingerprint exists to remove.
                        _detail = (
                            "wrong secret (non-loopback mixed, "
                            f"{_credential_mismatch_detail(internal_secret, request.headers['X-Internal-Secret'])})"
                        )
                        _sel = _sel_fn()
                        _sel.log_api_access(
                            caller=_caller,
                            forwarded=is_proxied_request(request),
                            operation="internal_auth",
                            outcome="denied",
                            source="token_auth",
                            resources=path,
                            error=_detail,
                        )
                        _log_auth(request, "internal", "denied", _detail)
                        if not request.headers["X-Internal-Secret"]:
                            return _deny(request, "Forbidden", "internal_secret_missing")
                        return _deny(request, "Forbidden", "internal_auth_mismatch")
                _valid, _uid, _reason, _app, _tok, _from_query = _extract_and_validate_token(
                    request, port
                )
                if not _valid:
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=_caller,
                        forwarded=is_proxied_request(request),
                        operation="internal_auth",
                        outcome="denied",
                        source="token_auth",
                        resources=path,
                        error=f"mixed non-loopback cookie auth failed: {_reason}",
                    )
                    _log_auth(
                        request,
                        "internal",
                        "denied",
                        f"mixed non-loopback cookie auth failed: {_reason}",
                    )
                    return _deny(request, "Forbidden")
                # Session-pin enforcement (RFC §3) — same rationale as the
                # loopback branch above.
                _pin_ok, _pin_mismatch = _check_pin(_tok)
                if not _pin_ok:
                    _log_auth(request, _audit_uid(_uid), "denied", _pin_mismatch)
                    return _deny(request, _pin_mismatch)
                # Expose identity + confine app tokens to their declared scope
                # (same rationale as the loopback branch above).
                request["user"] = _uid
                request["app"] = _app
                # The credential actually validated (see the main path's note).
                request["auth_token"] = _tok
                # POSITIVE dashboard-user signal for the WS scope gate (see
                # the loopback branch above).
                request["is_dashboard_user"] = not _app
                # WHICH credential authenticated (see the loopback branch's note).
                request["auth_from_query_token"] = _from_query
                await _prime_app_api_allowlist(_app)
                _scope_deny = _enforce_app_scope(request, _app, path)
                if _scope_deny is not None:
                    return _scope_deny
                # Same dedup as the loopback mixed branch above: the dashboard
                # user's grant is already audited by ``_log_auth``, whose row
                # carries the daemon-verified caller (``_audit_uid``); only an
                # app token's scoped grant keeps the ``source=token_auth`` row.
                if _app:
                    _sel = _sel_fn()
                    _sel.log_api_access(
                        caller=_caller,
                        forwarded=is_proxied_request(request),
                        operation="internal_auth",
                        outcome="granted",
                        source="token_auth",
                        resources=path,
                        error="mixed non-loopback cookie auth",
                    )
                _log_auth(
                    request,
                    _audit_uid(_uid),
                    "granted",
                    f"mixed non-loopback cookie auth for {_uid}",
                )
                await publish_actor(request, trust=tailnet_trust, serve_peer=peer)
                return await handler(request)  # type: ignore[operator]
            else:
                # INVARIANT: non-loopback access to strict internal paths is
                # ALWAYS denied.  Do NOT remove this branch — without it,
                # non-loopback requests would silently fall through to
                # normal cookie auth, defeating the machine-to-machine
                # isolation that the internal-secret design provides.
                _sel = _sel_fn()
                _sel.log_api_access(
                    caller=_caller,
                    forwarded=is_proxied_request(request),
                    operation="internal_auth",
                    outcome="denied",
                    source="token_auth",
                    resources=path,
                    error="non-loopback source",
                )
                _log_auth(request, "internal", "denied", "non-loopback source")
                return _deny(request, "Forbidden")

        # Bypass static assets
        if any(path.startswith(p) for p in _BYPASS_PREFIXES):
            return await handler(request)  # type: ignore[operator]
        if path in _BYPASS_EXACT:
            return await handler(request)  # type: ignore[operator]
        # Method-scoped exact bypasses. A non-listed method on the same path
        # falls through to the ordinary token gate rather than bypassing it.
        _bypass_methods = _BYPASS_EXACT_METHODS.get(path)
        if _bypass_methods is not None and request.method in _bypass_methods:
            return await handler(request)  # type: ignore[operator]
        # Icon files: anchored regex with bounded digit count to prevent
        # ReDoS and ensure only legitimate PWA icon paths bypass auth.
        if re.fullmatch(r"/icon-\d{1,4}\.png", path):
            return await handler(request)  # type: ignore[operator]

        # Installed-app UI bundles: anchored to /apps/{name}/ui/* only.
        # Does NOT match the reverse-proxy path /apps/{name}/api/*.
        # Restricted to safe methods (GET/HEAD) — static file serving only.
        # If a write-capable handler is ever registered under /apps/{name}/ui/,
        # it stays auth-protected because the bypass never fires for it.
        if _APPS_UI_BYPASS_RE.match(path) and request.method in ("GET", "HEAD"):
            return await handler(request)  # type: ignore[operator]

        # Bypass app token exchange (App Kit §5.1) — app authenticates
        # via X-App-Secret header, not a token cookie.
        if re.match(r"^/api/apps/[a-z0-9][a-z0-9_-]*/token$", path) and request.method == "POST":
            return await handler(request)  # type: ignore[operator]

        # Bypass /api/auth/refresh — the handler authenticates via the
        # refresh cookie (path-restricted to this endpoint), not the access
        # cookie. Adding here lets refresh succeed even when the access
        # cookie has just expired (the whole point of the refresh flow).
        # GET is also allowed for /api/auth/me which is gated by normal auth
        # below — only POST /api/auth/refresh bypasses.
        if path == "/api/auth/refresh" and request.method == "POST":
            return await handler(request)  # type: ignore[operator]

        # Bypass /api/auth/logout — same rationale as /api/auth/refresh:
        # the handler authenticates via the refresh cookie and must work
        # even if the access cookie has just expired (so a user can still
        # tear down their refresh chain on the way out).
        if path == "/api/auth/logout" and request.method == "POST":
            return await handler(request)  # type: ignore[operator]

        # Extract token from query param or cookie
        cookie_name = f"mc_token_{_cookie_port_from_host(request, port)}"
        token = request.query.get("token") or ""
        from_cookie = False
        if not token:
            token = request.cookies.get(cookie_name, "")
            from_cookie = bool(token)

        if not token:
            # Cold-start (no token — e.g. the access cookie expired over a
            # weekend). Serve the shell directly (not the matched handler) so
            # the app boots and self-recovers via the refresh cookie. Default-
            # deny: fires only when a shell handler is wired and the path is a
            # non-data GET/HEAD nav; no cookie minted.
            if spa_shell_handler is not None and _is_spa_shell_request(request):
                _log_auth(request, "", "shell_unauth", "SPA shell served (no token, cold-start)")
                return await spa_shell_handler(request)  # type: ignore[operator]
            _log_auth(request, "", "denied", "Token required")
            return _deny(request, "Token required")

        valid, user_id, reason, app_name = validate_token_with_app(
            token, use_session_exp=from_cookie
        )
        if not valid and not from_cookie:
            # A stale ``?token=`` must not veto a still-valid session cookie.
            # Re-opening a bookmarked / previously-scanned link replays the
            # long-expired link token in the URL; the browser ALSO sends the
            # session cookie that link was exchanged for, and that cookie is
            # the current credential. Treat the invalid query token as absent
            # and validate the cookie instead — this grants nothing a
            # cookie-only request would not already get (the cookie runs the
            # full validation + the peer-pin check below), it only stops a
            # dead historical token from being a one-vote veto. When the
            # cookie is missing or also invalid, the original query-token
            # failure stands so the denial reason names the credential the
            # caller actually presented.
            #
            # An APP token never takes this path. An installed app's UI is
            # served from this same origin, so the browser attaches the
            # dashboard user's session cookie alongside the app's own
            # ``?token=``. Falling back there would swap the app's scoped
            # identity for the user's own — ``app_name`` would come back empty,
            # ``_enforce_app_scope`` below would become a no-op, and an app
            # whose token merely EXPIRED would silently gain the user's full
            # API reach. An expired app token must be refused as such, so the
            # app re-exchanges its secret at ``/api/apps/<name>/token``. The
            # claim is read unverified, which is sound because it can only make
            # this decision stricter (see ``claims_an_app_unverified``).
            _cookie_token = request.cookies.get(cookie_name, "")
            if _cookie_token and not claims_an_app_unverified(token):
                _c_valid, _c_uid, _c_reason, _c_app = validate_token_with_app(
                    _cookie_token, use_session_exp=True
                )
                if _c_valid:
                    token = _cookie_token
                    from_cookie = True
                    valid, user_id, reason, app_name = (
                        _c_valid,
                        _c_uid,
                        _c_reason,
                        _c_app,
                    )
        if not valid:
            # Cold-start variant: an expired/forged token is present (cookie
            # survived but its token lapsed). Same rationale — serve the shell
            # so the SPA can boot and silently refresh.
            if spa_shell_handler is not None and _is_spa_shell_request(request):
                # Distinct outcome (NOT "ok"): keep forged-token navigations
                # detectable by SEL anomaly detection while still serving the
                # secret-free shell.
                _log_auth(
                    request,
                    "",
                    "shell_unauth_invalid_token",
                    f"SPA shell served (invalid token: {reason})",
                )
                return await spa_shell_handler(request)  # type: ignore[operator]
            _log_auth(request, "", "denied", reason)
            return _deny(request, reason)

        # The session pin key (RFC §3): the daemon-verified peer identity when
        # one resolved, else the immediate address — byte-for-byte today's pin.
        peer_key = _peer_key_for_request()

        _pin_ok, _pin_mismatch = _check_pin(token, allow_unbound_require_peer_link=not from_cookie)
        if not _pin_ok:
            _log_auth(request, _audit_uid(user_id), "denied", _pin_mismatch)
            return _deny(request, _pin_mismatch)

        # Extract session_exp for cookie and IP binding on first query-param use
        session_exp = 0.0
        session_token = token
        if not from_cookie:
            _link_nonce = ""
            _embed_parent_port = ""
            _no_refresh = False
            _session_peer_key = ""
            try:
                payload_bytes = _b64url_decode(token.split(".")[0])
                data = json.loads(payload_bytes)
                session_exp = float(data.get("session_exp", 0.0))
                _link_nonce = str(data.get("nonce", ""))
                # Carry the multi-instance frame-ancestors claim (the embedding
                # parent dashboard's port) THROUGH the exchange. The framed
                # document is authenticated by the session cookie minted below,
                # so without this the cookie would drop the claim and the CSP
                # reader (server._extra_frame_ancestors) would fall back to bare
                # ``'self'`` — the blank embedded-pane bug.
                _epp = data.get("embed_parent_port")
                if isinstance(_epp, str) and _epp:
                    _embed_parent_port = _epp
                # A link minted for a device this dashboard does not control (the
                # tailnet phone-access QR) says so with this claim, and the
                # promise it encodes is an EXPIRY: that session ends when its
                # short session_exp does. Honoured by NOT issuing a refresh
                # cookie below, rather than by a check in the refresh handler —
                # a credential that was never minted cannot be replayed, whereas
                # a guarded one relies on every future refresh entry point
                # remembering the guard.
                _no_refresh = str(data.get("no_refresh", "")) == "1"
            except Exception:
                session_exp = 0.0
            # Expose the frame-ancestors parent-port claim to the response-header
            # layer NOW, BEFORE the link nonce is revoked below. The FIRST framed
            # instance document is loaded via ``?token=`` and the browser enforces
            # that response's ``frame-ancestors``; re-validating the (about-to-be-
            # revoked) link token in server._extra_frame_ancestors would return
            # None and fall back to bare ``'self'`` (blank pane). Signature is
            # already verified above; this only carries the loopback parent port.
            if _embed_parent_port:
                request["embed_parent_port"] = _embed_parent_port
            # Token→session exchange (CWE-613 / secure token handling): NEVER
            # reuse the one-time URL/link token string as the long-lived session
            # cookie. The link token is exposed in URLs, Slack messages, terminal
            # history, browser history and access/proxy logs. Minting a SEPARATE
            # session token here (fresh nonce, same identity + remaining
            # lifetime) means an observer of any of those channels obtains only
            # the 5-minute link — not the 20-hour session credential. The link
            # token stops being a bearer credential the moment its short ``exp``
            # window closes; the cookie is an unrelated string. Per-session
            # revocation still works because the minted token carries its own
            # nonce (see RevokedNonceStore / api_auth_logout).
            _remaining = int(session_exp - time.time()) if session_exp else MAX_SESSION_TTL_SECS
            if _remaining > 0:
                # Claims that must SURVIVE the exchange are copied explicitly.
                # The session token is a fresh mint, so anything not named here
                # is dropped — and silently dropping ``boot`` would be the worst
                # possible failure: the link would be boot-bound, the cookie it
                # became would not, and the phone session would quietly outlive
                # the restart it was supposed to end at. The claim is carried,
                # never re-derived from current_boot_id(), so a link minted by a
                # PREVIOUS process cannot launder itself into a live session —
                # validate_token has already rejected it by this point, and
                # re-deriving would hide that.
                _carried: dict[str, str] = {}
                if _embed_parent_port:
                    _carried["embed_parent_port"] = _embed_parent_port
                _token_boot = str(data.get("boot", ""))
                if _token_boot:
                    _carried["boot"] = _token_boot
                # Carried for the same reason ``boot`` is: the session token is a
                # fresh mint, so a claim not named here is silently dropped — and
                # dropping this one would turn an identity-bound persistent
                # session into an ordinary rotating one, which is exactly the
                # binding it was minted to keep.
                _token_require_peer = "1" if str(data.get("require_peer", "")) == "1" else ""
                if _token_require_peer:
                    _carried["require_peer"] = _token_require_peer
                    # A delegated link may already be device-bound; an initial
                    # QR link deliberately is not.  Preserve an existing signed
                    # key, otherwise enroll the verified peer redeeming it.
                    _session_peer_key = required_peer_key_unverified(token) or peer_key
                # ``no_refresh`` gets the same treatment as ``boot`` and for the
                # same reason: the claim must survive the exchange or a DOWNSTREAM
                # consumer that reads the session cookie to learn the caller's
                # bounds (the mobile-link mint) sees an unbounded session and
                # re-mints an unbounded credential — the exact laundering the
                # claim exists to prevent. Validation never acts on it on the
                # cookie path, so carrying it is inert for auth itself; the
                # refresh chain is still suppressed below by never being minted.
                if _no_refresh:
                    _carried["no_refresh"] = "1"
                session_token = generate_token(
                    user_id,
                    ttl_seconds=_remaining,
                    app=app_name,
                    register_nonce=False,
                    peer_key=_session_peer_key,
                    extra=_carried or None,
                )
            # Kill the link token AS A COOKIE. Exchange alone is not enough: the
            # link token still carries the 20h ``session_exp``, so a captured
            # copy (from a log/Slack/history) could otherwise be presented
            # directly as ``mc_token_<port>`` and validate on the cookie path for
            # the full session. Adding its nonce to the persisted denylist makes
            # validate_token(use_session_exp=True) reject it. Crucially the
            # query-param LINK path (use_session_exp=False) does NOT consult the
            # denylist, so legitimate re-navigation of the same link URL — remote
            # instance iframes re-deriving /?token=, self-nudge polling — keeps
            # working within the 5-minute window (it just re-exchanges for a
            # fresh session cookie each time). Guarded by is_revoked so repeated
            # exchanges of the same link don't re-write the denylist file.
            if _link_nonce and session_exp and not _get_revoked_store().is_revoked(_link_nonce):
                # revoke() does synchronous file I/O (mkdir/write/chmod/replace);
                # offload so it never blocks the event loop. is_revoked above is
                # an in-memory check and is cheap enough to run inline.
                await asyncio.to_thread(_get_revoked_store().revoke, _link_nonce, session_exp)
            # Bind the SESSION token (what becomes the cookie) to the peer key,
            # not the consumed URL token. ``proxied`` is recorded so Security
            # Posture can tell the user whether that pin is per-client or shared
            # with everyone behind a same-host tunnel — it does not affect the
            # binding itself. A session pinned to a daemon-verified tailnet peer
            # is per-client even though the request is proxied, so the flag is
            # only set when NO peer resolved.
            bound_peer_key = _session_peer_key or peer_key
            bind_token_peer(
                session_token,
                bound_peer_key,
                session_exp,
                proxied=is_proxied_request(request) and peer is None,
            )
            if peer is not None:
                # The one permission decision that changes state: this session
                # is now pinned to a verified identity and the audit trail
                # re-attributes to a login. One SEL row per session, at bind —
                # not per request, which would only add volume.
                _sel_fn().log_api_access(
                    caller=peer.login,
                    forwarded=is_proxied_request(request),
                    operation="tailnet_peer_bind",
                    outcome="granted",
                    source="token_auth",
                    resources=bound_peer_key,
                )

            # Token-consumption anchor seam (Default: no-op, OSS-identical). A
            # Slack challenge-redirect link, once opened on a verified device,
            # consumes its token here — the edition opens the bounded per-(user,
            # channel) auth window that lets follow-up Slack traffic flow inline.
            # channel/thread_ts ride the token's signed ``extra`` payload (``data``);
            # absent for non-challenge tokens, so the window is only opened for a
            # real challenge exchange. Fail-safe: ``safe_context_call`` swallows an
            # observer error (fallback=None) so it never blocks token consumption /
            # login, while still re-raising ``PlatformCompositionError`` — the boot
            # invariant that a mis-composed edition MUST abort rather than silently
            # degrade. Do NOT wrap this in a bare ``except Exception``: that is
            # exactly the swallow ``safe_context_call`` centralizes to prevent.
            _chan = str(data.get("channel", "")) if isinstance(data, dict) else ""
            _thread = (data.get("thread_ts") if isinstance(data, dict) else None) or None
            if _chan:
                from kiro_crew.platform import current_context, safe_context_call

                safe_context_call(
                    lambda: current_context().dashboard.on_token_consumed(
                        user_id, _chan, session_exp, _thread
                    ),
                    fallback=None,
                    log_message="dashboard.on_token_consumed observer failed",
                )

        # Expose authenticated identity to handlers (deny-by-default)
        request["user"] = user_id
        request["app"] = app_name
        # The credential this request was ACTUALLY authenticated with. Handlers
        # that read signed claims out of the caller's own token (the mobile-link
        # mint's bounds, the frame-ancestors parent port) must read THIS, not
        # re-extract with their own query-then-cookie order: only the credential
        # validated here has a verified signature, and since the cookie fallback
        # above can adopt the cookie over an invalid query token, re-extraction
        # is no longer guaranteed to pick the same one. Reading the other
        # credential would let a bounded caller have its bounds read from an
        # unverified, attacker-settable value.
        # On a query-link exchange the session token carries the newly
        # established signed peer binding. Publish THAT bounded credential to
        # handlers that may mint a child link during this same request; exposing
        # the claimless enrollment link would let the child silently drop the
        # just-established device scope.
        request["auth_token"] = session_token
        # POSITIVE dashboard-user signal for the WS scope gate (see above).
        request["is_dashboard_user"] = not app_name
        # WHICH credential authenticated: the ``?token=`` the caller presented,
        # or the session cookie the fallback above adopted after that query
        # token proved invalid. One bit, derived from the same ``from_cookie``
        # the cookie-set branch below already keys on -- a fresh cookie is set
        # only on a query-token exchange, so this is that decision named.
        #
        # A status code cannot carry it. ``/api/auth/me`` is not owner-gated, so
        # a session that is authenticated but owner-denied answers 200 there on
        # its cookie alone; a caller reading only the status would take that for
        # "the token I sent was accepted". ``api_auth_me`` returns this so the
        # in-banner re-auth exchange can tell the two apart, which it cannot do
        # from Set-Cookie: that header is unreadable from a browser.
        request["auth_from_query_token"] = not from_cookie

        # App-token least-privilege gate (CWE-269): an app token is confined to
        # its own namespace + its manifest ``permissions.api`` allowlist. This
        # is the primary enforcement point for the normal cookie/query-param
        # flow (e.g. /api/sessions, /api/config/*, the /apps/<other>/api proxy).
        await _prime_app_api_allowlist(app_name)
        _scope_deny = _enforce_app_scope(request, app_name, path)
        if _scope_deny is not None:
            return _scope_deny

        # Attribution only, after every decision above: publishing never denies.
        await publish_actor(request, trust=tailnet_trust, serve_peer=peer)
        # Proceed to handler
        resp = await handler(request)  # type: ignore[operator]

        # Set cookie after handler (needs response object)
        if not from_cookie:
            cookie_max_age = MAX_SESSION_TTL_SECS
            if session_exp:
                remaining = int(session_exp - time.time())
                if 0 < remaining <= MAX_SESSION_TTL_SECS:
                    cookie_max_age = remaining
            resp.set_cookie(
                cookie_name,
                session_token,
                httponly=True,
                samesite="Lax",
                # Secure only when over HTTPS (direct or via a
                # TLS-terminating tunnel/proxy — see is_https_request).
                # Localhost plain HTTP must not set it or the browser
                # refuses to send it back.
                secure=is_https_request(request),
                path="/",
                max_age=cookie_max_age,
            )
            # Clean up legacy cookie from pre-port-specific era
            resp.set_cookie("mc_token", "", max_age=0, path="/")

            # Trim other-port auth cookies from the shared 127.0.0.1 jar so it
            # can't grow past aiohttp's header limit (see
            # refresh_tokens.foreign_port_cookies). Gated on jar size so live
            # co-existing gateways keep their sessions until accumulation
            # genuinely threatens overflow. This page request only carries
            # other-port ACCESS cookies (path "/"); other-port refresh cookies
            # (path "/api/auth") are trimmed on the next refresh call.
            if cookie_jar_needs_pruning(request.cookies):
                for _stale_name, _stale_path in foreign_port_cookies(
                    request.cookies, _cookie_port_from_host(request, port)
                ):
                    resp.set_cookie(_stale_name, "", max_age=0, path=_stale_path)

            # Initial mint via token URL: also attach a refresh cookie so
            # the user does not have to re-mint via URL every ~20h. Inlined
            # here (rather than calling handlers.auth_refresh) to keep the
            # import top-level and the cycle direction one-way:
            # token_auth → refresh_tokens, never the reverse.
            #
            # SKIPPED ENTIRELY for a no-refresh session. Such a link was minted
            # for a device this dashboard does not control (the tailnet
            # phone-access QR), and its short ``session_exp`` is the whole reason
            # handing that device a credential is acceptable. A refresh chain
            # would defeat it: ``api_auth_refresh`` re-mints at
            # MAX_SESSION_TTL_SECS without carrying the original ceiling forward,
            # so one rotation silently promotes a ~1h session to the 20h cap.
            # Enforced by never minting the chain rather than by a check inside
            # the refresh handler — a credential that does not exist cannot be
            # replayed, while a guarded one relies on every future refresh entry
            # point remembering the guard.
            if _no_refresh:
                # Not minting one is only HALF the guarantee. The browser may
                # ALREADY hold a refresh cookie for this port from an earlier
                # full session (a prior `?token=` link, a Slack `!dashboard`
                # link), and `api_auth_refresh` authenticates on the refresh
                # cookie ALONE — it never checks that the access token beside it
                # belongs to the same session. Left in place, that residual
                # credential rotates into a 20-hour session and the QR's short
                # window is bypassed by a path that never touched this branch.
                #
                # Every way a refresh credential can reach this session, and
                # where each is closed:
                #   1. minted here during the exchange   -> not minted (below)
                #   2. already in the browser            -> expired here
                #   3. rotated by api_auth_refresh       -> unreachable once 1+2
                #      are closed, since rotation needs an existing chain
                #
                # Only THIS port's cookie is cleared. A `mc_refresh_<other>`
                # belongs to a different gateway instance, cannot refresh this
                # session, and is not ours to revoke.
                resp.set_cookie(
                    refresh_cookie_name(_cookie_port_from_host(request, port)),
                    "",
                    max_age=0,
                    path=REFRESH_COOKIE_PATH,
                )
            else:
                try:
                    # The refresh chain inherits the boot binding. Without this
                    # the chain is the escape hatch: the access cookie would die
                    # at restart while a 30-day refresh credential beside it
                    # re-minted a fresh session on the next visit, and "ends at
                    # restart" would be false by one rotation.
                    #
                    # Peer binding for the CHAIN. The QR "persistent" session
                    # shape carries its own ``require_peer`` claim on the link;
                    # an ordinary Phase-3 session does not, so without this its
                    # chain would be minted UNBOUND even though its access token
                    # is pinned to a verified peer. That asymmetry is a
                    # laundering path: a refresh cookie stolen from allowed node
                    # A, replayed from allowed node B, rotated cleanly and handed
                    # back an access token pinned to B. Binding here closes it at the mint, so
                    # the chain says who owns it from its first byte rather than
                    # relying on the rotation handler to infer it.
                    #
                    # Gated on a RESOLVED peer, not on ``peer_key``: that helper
                    # answers ``ip:<addr>`` when no peer resolved, and binding a
                    # chain to the tunnel's shared loopback address would read as
                    # a pin while excluding nobody. ``bind_refresh_chains`` is the
                    # operator's documented opt-out for cross-device roaming at
                    # node scope.
                    _refresh_require_peer = str(data.get("require_peer", "")) == "1"
                    _refresh_peer_key = _session_peer_key
                    if (
                        not _refresh_require_peer
                        and peer is not None
                        and tailnet_trust is not None
                        and tailnet_trust.bind_refresh_chains
                        and peer_key.startswith("ts:")
                    ):
                        _refresh_require_peer = True
                        _refresh_peer_key = peer_key
                    refresh_token, chain_id, _jti, refresh_exp = generate_refresh_token(
                        user_id,
                        boot=str(data.get("boot", "")),
                        require_peer=_refresh_require_peer,
                        peer_key=_refresh_peer_key,
                    )
                    refresh_remaining = int(refresh_exp - time.time())
                    if refresh_remaining > 0:
                        if _refresh_peer_key:
                            # Server-side twin of the signed claim above. The
                            # claim is authoritative and cannot be forged, but it
                            # only binds chains whose mint path remembered to set
                            # it — and this issue exists because one did and the
                            # others did not. A record the presented token cannot
                            # influence makes the next forgetful mint path fail
                            # closed instead of silently unbound. Offloaded
                            # because it writes refresh_chains.json.
                            await asyncio.to_thread(
                                bind_chain_peer, chain_id, _refresh_peer_key, refresh_exp
                            )
                        resp.set_cookie(
                            refresh_cookie_name(_cookie_port_from_host(request, port)),
                            refresh_token,
                            httponly=True,
                            samesite="Lax",
                            secure=is_https_request(request),
                            path=REFRESH_COOKIE_PATH,
                            max_age=min(refresh_remaining, MAX_REFRESH_TTL_SECS),
                        )
                        # Audit the initial-mint event so forensics can trace any
                        # subsequent chain revocation back to the user it was
                        # issued to.
                        try:
                            _sel_fn().log_api_access(
                                caller=user_id,
                                forwarded=is_proxied_request(request),
                                operation="refresh_token_initial_mint",
                                outcome="ok",
                                source="refresh_tokens",
                                resources=chain_id,
                            )
                        except Exception as exc:  # pragma: no cover
                            # SEL must never block auth flows, but log the failure
                            # so it's observable.
                            logger.debug("token_auth: SEL audit failed: %s", exc)
                except Exception as _refresh_err:
                    # Refresh cookie is best-effort. If something goes wrong
                    # here, the access cookie still works as before — the
                    # user just won't get the refresh upgrade until next mint.
                    logger.warning(
                        "token_auth: failed to attach refresh cookie (%s); "
                        "access cookie still set, user can re-mint as before",
                        _refresh_err,
                    )

        _log_auth(request, _audit_uid(user_id), "ok", "")
        return resp  # type: ignore[return-value]

    middleware._is_token_auth = True  # type: ignore[attr-defined]  # sentinel for server.py security gate
    return middleware


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.token_auth_app_scope import (  # noqa: F401
        _APP_PERMS_PRIME_MARGIN,
        _APP_PERMS_TTL,
        _APP_TOKEN_IMPLICIT_ALLOW,
        RESERVED_APP_PATH_SEGMENTS,
        _api_pattern_matches,
        _app_api_allowlist,
        _app_owns_path,
        _app_perms_cache,
        _app_perms_lock,
        app_token_path_allowed,
    )
    from kiro_crew.dashboard.token_auth_callers import (  # noqa: F401
        KNOWN_INTERNAL_CALLERS,
        MEMBER_CHAT_PRINCIPAL_KEY,
        _bound_session_keys_on_chain,
        _cron_job_exists,
        _cron_job_owner,
        _key_segment,
        _live_session_keys_on_chain,
        _slot_by_linked_key,
        _subagent_caller_record,
        _subagent_owner,
        _subagent_record_exists,
        app_owns_transcript,
        caller_names_a_missing_slot,
        caller_record_is_missing,
        derive_caller_app,
        effective_request_app,
        folder_principal,
        refuse_unattributable_caller,
        request_origin,
    )
    from kiro_crew.dashboard.token_auth_paths import (  # noqa: F401
        _403_HTML,
        _APP_WINDOW_EXCLUDED_PATHS,
        _APPS_SPA_EXCLUDED_RE,
        _SELF_AUTH_WEBHOOK_METHODS,
        AGENT_HOOK_PATH,
        CSRF_EXEMPT_EXACT_METHODS,
        SPA_FALLBACK_EXCLUDED_PREFIXES,
        TEAMS_WEBHOOK_PATH,
        UPDATE_REVALIDATE_PATH,
        _credential_fingerprint,
        is_csrf_exempt,
        register_app_window_paths,
    )
    from kiro_crew.dashboard.token_auth_tokens import (  # noqa: F401
        _REVOKED_NONCES_FILE,
        LINK_WINDOW_SECS,
        MAX_CONCURRENT_NONCES,
        RevokedNonceStore,
        TokenStateManager,
        _b64url_encode,
        _evict_expired,
        _revoked_store_lock,
        _revoked_store_singleton,
        _sign,
        _token_pin_key,
        bind_token_ip,
        check_token_ip,
        extract_claims_from_token,
        extract_numeric_claim,
        extract_prompt_from_token,
        generate_app_secret,
        is_consumed,
        mark_consumed,
        parse_duration,
        proxied_pin_observed,
        revoke_access_cookie,
        revoke_all_sessions,
        token_embed_parent_port,
        try_consume,
        validate_app_secret,
        validate_token,
        warm_auth_singletons,
        write_app_secret,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import (  # noqa: F401
        platform_compat,
    )
    from kiro_crew.dashboard.boot_id import (  # noqa: F401
        current_boot_id,
    )
    from kiro_crew.dashboard.origin import (  # noqa: F401
        request_is_unix_socket,
    )
    from kiro_crew.executors import (  # noqa: F401
        subprocess_executor,
    )
    from kiro_crew.mcp_gateway.socketsec import (  # noqa: F401
        PeerCredResult,
        check_peer_is_self,
        get_peer_pid,
    )
    from kiro_crew.messaging.link import (  # noqa: F401
        is_channel_session_key,
    )
    from kiro_crew.peer_resolve import (  # noqa: F401
        resolve_peer_tenancy,
    )
    from kiro_crew.runtime_ownership import (  # noqa: F401
        session_keys_bound_to_pid,
    )
    from kiro_crew.session_token_sig import (  # noqa: F401
        verify_session_token,
    )

# This module is the facade of dashboard token authentication: every name is reached through
# it as ``kiro_crew.dashboard.token_auth.<name>``. A read is served by the owner that binds
# the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. The
# middleware above stays here. New work goes to the owner of its responsibility:
#
#   ``token_auth_paths`` -- requests that skip token auth, and the refusal the rest receive
#   ``token_auth_tokens`` -- minting, validating, pinning and revoking session tokens
#   ``token_auth_app_scope`` -- what an app token may reach
#   ``token_auth_callers`` -- Unix-socket peer verification and request attribution
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.token_auth_paths",
    "kiro_crew.dashboard.token_auth_tokens",
    "kiro_crew.dashboard.token_auth_app_scope",
    "kiro_crew.dashboard.token_auth_callers",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
