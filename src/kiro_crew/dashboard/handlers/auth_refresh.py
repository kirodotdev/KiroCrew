"""Auth refresh endpoints: ``GET /api/auth/me`` and ``POST /api/auth/refresh``.

Spec: ``docs/system-specs/features/dashboard-token-auth.md``.

Both endpoints handle their own auth (the standard ``token_auth_middleware``
exempts ``/api/auth/refresh`` so an expired access cookie does not block
the refresh path). ``/api/auth/me`` requires a valid access cookie and is
gated by the standard middleware.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import threading
import time
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.origin import check_origin, is_https_request
from kiro_crew.dashboard.refresh_tokens import (
    MAX_REFRESH_TTL_SECS,
    REFRESH_COOKIE_PATH,
    _get_state,
    cookie_jar_needs_pruning,
    foreign_port_cookies,
    generate_refresh_token,
    refresh_cookie_name,
    refresh_token_boot,
    refresh_token_peer_key,
    refresh_token_requires_peer,
    validate_refresh_token,
)
from kiro_crew.dashboard.tailnet import (
    TailnetTrust,
    login_allowed,
    peer_pin_key,
    peer_pin_key_for_claim,
    resolve_forwarded_peer,
)
from kiro_crew.dashboard.token_auth import (
    MAX_SESSION_TTL_SECS,
    _cookie_port_from_host,
    bind_token_ip,
    bind_token_peer,
    extract_numeric_claim,
    generate_token,
    revoke_access_cookie,
)
from kiro_crew.sel import sel as _sel_fn

logger = logging.getLogger(__name__)


# --- Rate limiter ------------------------------------------------------------

# Per-source-IP token bucket on POST /api/auth/refresh. Defense-in-depth
# against an attacker holding one valid refresh cookie pumping rotations
# to balloon the on-disk state file. 60 calls/min is generous for a real
# user (1 every 5s during a flaky network) but caps an automated pump.
_REFRESH_RATE_WINDOW_SECS = 60.0
_REFRESH_RATE_MAX_CALLS = 60

# Bounding for the in-memory per-IP bucket map. Without eviction the
# ``_refresh_rate_buckets`` dict grows without bound: every distinct source
# IP that ever hits /api/auth/refresh leaves a permanent entry (an empty
# deque once its timestamps age out), so a wide spread of client IPs (or a
# spoofed-XFF pump) slowly leaks memory. We sweep stale/empty buckets at most
# once per window; that reclaim path is the sole way live buckets leave the
# map. A hard cap (``_REFRESH_RATE_MAX_BUCKETS``) then fails CLOSED: once the
# map is full, previously-unseen source IPs are rate-limited outright rather
# than admitted by evicting an existing bucket. We deliberately do NOT evict a
# live bucket to make room — evicting the "least-recently-active" victim was
# abusable (see ``_rate_limited``): a saturated attacker never appends a
# timestamp on denied calls, so their bucket freezes at exhaustion time and
# becomes the eviction target under an XFF/botnet pump, letting them drop their
# own exhausted bucket and re-mint a fresh full allowance. Fail-closed removes
# that reset path entirely. To avoid a sustained-flood / trusted-XFF
# availability gap (a pinned-at-capacity map denying legitimate new IPs while
# full of reclaimable stale buckets), the sweep is invoked UNCONDITIONALLY
# when an insertion is refused at the cap — reclaiming dead space exactly when
# it matters without dropping any still-in-window bucket.
_REFRESH_RATE_SWEEP_INTERVAL_SECS = 60.0
_REFRESH_RATE_MAX_BUCKETS = 4096

_refresh_rate_lock = threading.Lock()
_refresh_rate_buckets: dict[str, collections.deque[float]] = {}
# Monotonic-ish wall-clock timestamp of the last full sweep (guarded by
# _refresh_rate_lock). Starts at -inf so the first call always sweeps.
_refresh_rate_last_sweep: float = float("-inf")


def _sweep_rate_buckets(now: float, *, force: bool = False) -> None:
    """Evict stale/empty per-IP buckets. MUST hold ``_refresh_rate_lock``.

    Runs at most once per ``_REFRESH_RATE_SWEEP_INTERVAL_SECS`` unless
    ``force`` is set. Drops any bucket whose timestamps have all aged past the
    window (leaving it empty). This stale/empty reclaim is the ONLY way a live
    bucket leaves the map — we never evict a bucket that still holds in-window
    timestamps. Combined with the fail-closed hard cap in ``_rate_limited``
    (unseen IPs are rejected once the map is full, not admitted by eviction),
    the map is strictly bounded by ``_REFRESH_RATE_MAX_BUCKETS`` and no client
    can force its own bucket to be dropped early.

    ``force=True`` bypasses the interval throttle. It is used when an insertion
    is refused at ``_REFRESH_RATE_MAX_BUCKETS`` so reclaimable stale space is
    freed exactly when it matters (avoiding a sustained-flood / trusted-XFF
    availability gap where legitimate new IPs are denied for up to a window
    while the map is actually full of dead buckets). Because a forced sweep
    still only drops buckets whose timestamps have ALL aged out, it does NOT
    reopen the eviction-reset path: a saturated attacker's still-in-window
    bucket is never reclaimed, so flooding the map to capacity cannot drop the
    attacker's own exhausted bucket.
    """
    global _refresh_rate_last_sweep
    if not force and now - _refresh_rate_last_sweep < _REFRESH_RATE_SWEEP_INTERVAL_SECS:
        return
    _refresh_rate_last_sweep = now
    cutoff = now - _REFRESH_RATE_WINDOW_SECS
    stale: list[str] = []
    for ip, bucket in _refresh_rate_buckets.items():
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            stale.append(ip)
    for ip in stale:
        _refresh_rate_buckets.pop(ip, None)


def _rate_limited(client_ip: str, now: float | None = None) -> bool:
    """Return True if *client_ip* is over the per-minute refresh cap.

    Fails CLOSED when client_ip is empty: bucket unknown-IP requests
    under a single sentinel key so they share a rate limit. Never
    silently bypass the cap — defense-in-depth per the
    security-controls rule (deny-by-default).
    """
    if not client_ip:
        # True fail-closed: an empty client_ip means we cannot
        # rate-limit this request at all. Deny rather than bucket
        # under a shared sentinel that would still allow 60/min
        # through. aiohttp `request.remote` is only empty on
        # malformed/severed connections, so this denies basically no
        # legit traffic while removing the hidden allow-path.
        return True
    if now is None:
        now = time.time()
    cutoff = now - _REFRESH_RATE_WINDOW_SECS
    with _refresh_rate_lock:
        # Periodic sweep of stale/empty buckets so the map can't grow
        # without bound across many distinct source IPs over time.
        _sweep_rate_buckets(now)
        bucket = _refresh_rate_buckets.get(client_ip)
        if bucket is None:
            # New source IP. Enforce the hard cap by failing CLOSED: once the
            # map is full, reject previously-unseen IPs rather than evicting a
            # live bucket to admit them. Eviction-to-admit was abusable — a
            # saturated attacker's bucket freezes at exhaustion (denied calls
            # append no timestamp), so under an XFF/botnet pump it became the
            # "least-recently-active" eviction victim, letting the attacker
            # drop their own exhausted bucket and re-create a fresh full
            # allowance. Rejecting unseen IPs at the cap removes that reset
            # path; stale buckets are reclaimed only by the periodic sweep.
            if len(_refresh_rate_buckets) >= _REFRESH_RATE_MAX_BUCKETS:
                # Map is at capacity. Before failing closed, reclaim stale
                # space UNCONDITIONALLY (bypass the interval throttle — the
                # lock is already held). Without this, the sweep is throttled
                # to once per window even at capacity, so under a sustained
                # flood (or trusted-XFF pump / organic IP churn) that keeps the
                # map pinned at the cap, a legitimate previously-unseen IP is
                # denied /api/auth/refresh for up to a window even when most
                # buckets are stale and reclaimable — surfacing as unexplained
                # forced logouts. Forcing the sweep here frees dead buckets
                # exactly when it matters. It does NOT reopen the
                # eviction-reset path: the sweep only drops buckets whose
                # timestamps have ALL aged out, so a saturated attacker's
                # still-in-window bucket is never reclaimed (they cannot flood
                # the map to drop their own exhausted bucket).
                _sweep_rate_buckets(now, force=True)
                if len(_refresh_rate_buckets) >= _REFRESH_RATE_MAX_BUCKETS:
                    return True
            bucket = _refresh_rate_buckets[client_ip] = collections.deque()
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= _REFRESH_RATE_MAX_CALLS:
            return True
        bucket.append(now)
        return False


# --- Helpers -----------------------------------------------------------------


def _client_ip(request: web.Request) -> str:
    """Best-effort client IP extraction.

    Uses ``request.remote`` (aiohttp's normalised peer) which already
    consults ``X-Forwarded-For`` when the app is configured to trust it
    and falls back to the transport peer otherwise. This matches the
    pattern used elsewhere in ``token_auth.py`` (search "request.remote")
    and means tunneled / reverse-proxied deployments report the
    upstream client IP rather than ``127.0.0.1`` for everyone — the
    grace-window IP lock would be a no-op otherwise.
    """
    return request.remote or ""


def _set_access_cookie(
    resp: web.Response,
    request: web.Request,
    token: str,
    session_exp: float,
) -> None:
    """Set the per-port access cookie. Mirrors the middleware's logic.

    No-op when the session is already expired (max_age <= 0) — we never
    want to silently extend a stale session by falling back to the full
    TTL via the ``or`` operator (which fires because 0 is falsy).
    """
    port = request.app.get("port", 7777)
    cookie_name = f"mc_token_{_cookie_port_from_host(request, port)}"
    remaining = int(session_exp - time.time())
    if remaining <= 0:
        # Already-expired session: don't set a cookie. Caller treats
        # this as "session is over" — the existing 403 / re-mint flow
        # surfaces normally.
        return
    max_age = min(remaining, MAX_SESSION_TTL_SECS)
    resp.set_cookie(
        cookie_name,
        token,
        httponly=True,
        samesite="Lax",
        # Secure flag set when the browser reached us over HTTPS — including
        # via a TLS-terminating tunnel/proxy that forwards plain HTTP to the
        # loopback gateway (X-Forwarded-Proto). Without this the wss://
        # dashboard WebSocket is denied the cookie and flaps online/offline.
        # Localhost plain HTTP still omits it so the browser will send it back.
        secure=is_https_request(request),
        path="/",
        max_age=max_age,
    )


def _set_refresh_cookie(
    resp: web.Response,
    request: web.Request,
    token: str,
    session_exp: float,
) -> None:
    """Set the per-port refresh cookie, path-restricted to /api/auth/.

    Path scope covers BOTH /api/auth/refresh and /api/auth/logout so
    the cookie is sent to the logout endpoint (otherwise logout cannot
    revoke the chain and silently no-ops).

    No-op when the session is already expired — see _set_access_cookie
    for the rationale (avoid silent TTL extension via ``or`` fallback).
    """
    port = request.app.get("port", 7777)
    cookie_name = refresh_cookie_name(_cookie_port_from_host(request, port))
    remaining = int(session_exp - time.time())
    if remaining <= 0:
        return
    max_age = min(remaining, MAX_REFRESH_TTL_SECS)
    resp.set_cookie(
        cookie_name,
        token,
        httponly=True,
        samesite="Lax",
        # See _set_access_cookie for the conditional-Secure rationale.
        secure=is_https_request(request),
        path=REFRESH_COOKIE_PATH,
        max_age=max_age,
    )


def _clear_refresh_cookie(
    resp: web.Response,
    request: web.Request,
) -> None:
    port = request.app.get("port", 7777)
    cookie_name = refresh_cookie_name(_cookie_port_from_host(request, port))
    resp.set_cookie(cookie_name, "", max_age=0, path=REFRESH_COOKIE_PATH)


def _expire_foreign_port_cookies(
    resp: web.Response,
    request: web.Request,
) -> None:
    """Expire per-port auth cookies left behind by gateways on OTHER ports.

    Keeps the shared 127.0.0.1 cookie jar from growing past aiohttp's header
    limit (see ``refresh_tokens.foreign_port_cookies``). Runs on the refresh
    happy path, which — unlike the initial page mint — sees BOTH cookie types
    because ``/api/auth/refresh`` matches the access (``/``) and refresh
    (``/api/auth``) cookie paths, so one refresh cycle trims the whole jar.

    No-op unless the jar is actually approaching the limit
    (``cookie_jar_needs_pruning``) so co-existing live gateways in one browser
    keep their sessions until accumulation genuinely threatens overflow.
    """
    if not cookie_jar_needs_pruning(request.cookies):
        return
    port = request.app.get("port", 7777)
    current = _cookie_port_from_host(request, port)
    for name, path in foreign_port_cookies(request.cookies, current):
        resp.set_cookie(name, "", max_age=0, path=path)


def _audit(
    user_id: str,
    operation: str,
    outcome: str,
    error: str = "",
) -> None:
    """SEL audit log for refresh-related events."""
    try:
        _sel_fn().log_api_access(
            caller=user_id or "<unknown>",
            operation=operation,
            outcome=outcome,
            source="refresh_tokens",
            resources=error,
        )
    except Exception as exc:  # pragma: no cover
        # SEL must never block auth flows, but log the failure so it's observable.
        logger.debug("refresh_tokens: SEL audit failed: %s", exc)


# --- Public handlers ---------------------------------------------------------


async def api_auth_me(request: web.Request) -> web.Response:
    """Return the authenticated user's identity and cookie expiry timestamps.

    The frontend scheduler reads ``session_exp`` to schedule the next
    refresh. ``refresh_exp`` is included so the UI can warn the user of
    pending re-auth (e.g., 'session expires in 2 days').
    """
    user_id = request.get("user", "")
    if not user_id:
        return web.json_response({"error": "unauthenticated"}, status=401)

    # Read the credential the MIDDLEWARE VALIDATED, published as
    # ``request["auth_token"]`` — the same contract ``_caller_bounds`` and the
    # frame-ancestors reader follow. Re-extracting the cookie here was only ever
    # correct because extraction order was guaranteed to match the middleware's;
    # it does not hold when a valid ``?token=`` won (pre-existing) and, now that
    # an invalid query token falls back to the cookie, the order is not fixed at
    # all. Reading the wrong credential mis-reports ``session_exp``, which is
    # what drives the frontend's proactive-refresh scheduler.
    #
    # The re-extraction stays as a FALLBACK rather than being deleted. On the
    # first request of a link exchange the published credential is the link
    # token, whose nonce this very request added to the cookie denylist, so the
    # numeric read yields nothing — exactly the case where the old path already
    # reported 0.0. Falling back keeps this strictly better than before instead
    # of trading one blind spot for another.
    port = request.app.get("port", 7777)
    cookie_name = f"mc_token_{_cookie_port_from_host(request, port)}"
    published = request.get("auth_token", "")
    access_token = published if isinstance(published, str) and published else ""
    # session_exp is a FLOAT claim; the string-only extract_claims_from_token
    # silently drops it (always yielding 0.0 here), which disabled the
    # frontend's proactive-refresh scheduler. Use the numeric extractor.
    session_exp = extract_numeric_claim(access_token, "session_exp") or 0.0
    if not session_exp:
        session_exp = (
            extract_numeric_claim(request.cookies.get(cookie_name, ""), "session_exp") or 0.0
        )

    # Refresh-cookie expiry: best effort — if a refresh cookie is present,
    # we report its session_exp. The cookie is path-scoped to /api/auth/
    # so it IS sent here (api_auth_me lives at /api/auth/me); when absent
    # we report 0 which is safe.
    refresh_exp = 0.0
    refresh_cookie = request.cookies.get(
        refresh_cookie_name(_cookie_port_from_host(request, port)), ""
    )
    if refresh_cookie:
        valid, _uid, _reason, _cid, _jti, exp = validate_refresh_token(refresh_cookie)
        if valid:
            refresh_exp = exp

    return web.json_response(
        {
            "user_id": user_id,
            "session_exp": session_exp,
            "refresh_exp": refresh_exp,
        }
    )


async def api_auth_refresh(request: web.Request) -> web.Response:
    """Validate the refresh cookie, mint a fresh access+refresh pair, return them.

    Implements the rotation-on-use semantics: each call consumes the
    presented refresh ``jti`` and issues a NEW one with the same ``chain_id``.
    Reuse outside the multi-tab grace window auto-revokes the chain.
    """
    # Defense-in-depth CSRF check. SameSite=Lax already blocks the
    # canonical cross-site POST attack, but a malicious origin loaded
    # in a subframe or via XHR with credentials would still send the
    # cookie. Reject if Origin is set and not in the dashboard
    # allow-list. Loopback origins are trusted (see check_origin).
    if not check_origin(request, require=False):
        _audit("", "refresh_token_use", "bad_origin", request.headers.get("Origin", ""))
        return web.json_response({"error": "bad_origin"}, status=403)

    client_ip = _client_ip(request)
    now = time.time()

    # Per-IP rate limit. Defense-in-depth against an attacker pumping
    # rotations with a stolen refresh cookie.
    if _rate_limited(client_ip, now=now):
        _audit("", "refresh_token_use", "rate_limited", client_ip)
        return web.json_response(
            {"error": "rate_limited"},
            status=429,
            headers={"Retry-After": str(int(_REFRESH_RATE_WINDOW_SECS))},
        )

    port = request.app.get("port", 7777)
    cookie_name = refresh_cookie_name(_cookie_port_from_host(request, port))
    refresh_token = request.cookies.get(cookie_name, "")

    if not refresh_token:
        _audit("", "refresh_token_use", "no_cookie")
        return web.json_response({"error": "no_refresh_cookie"}, status=401)

    valid, user_id, reason, chain_id, jti, _exp = validate_refresh_token(refresh_token)
    if not valid:
        if reason == "chain revoked":
            _audit(user_id, "refresh_token_use", "chain_revoked", reason)
            resp = web.json_response({"error": "refresh_chain_revoked"}, status=401)
            _clear_refresh_cookie(resp, request)
            return resp
        _audit(user_id, "refresh_token_use", "invalid", reason)
        return web.json_response({"error": "invalid_refresh"}, status=401)

    state = _get_state()

    # An identity-bound chain (the persistent QR session shape) may only be USED
    # while a daemon-verified tailnet peer can be established. Placed here, ahead
    # of BOTH the reuse branch and the mint, because every path below this line
    # hands the caller a live credential: the grace-replay branch re-serves the
    # cached pair and re-sets both cookies without minting anything, so a check
    # sited at the mint leaves a 60-second window in which a replayed token is
    # honoured with no identity check at all. That window was the review finding.
    #
    # Ordering it BEFORE reuse detection is deliberate, not incidental. Reuse
    # detection revokes the chain, so letting it run first would let any
    # unverified caller destroy a legitimate 30-day session by replaying one
    # consumed token — a sign-out handed to anyone who can reach the port. An
    # unverified caller gets 401 either way, so refusing first loses no theft
    # signal: a thief who CAN verify still trips reuse detection normally.
    #
    # A refusal does NOT revoke. Identity resolution fails transiently (daemon
    # restart, a stripped header), and burning a 30-day credential over a blip
    # would turn a recoverable hiccup into a re-scan. The session simply cannot
    # be used until identity is established again, which is the honest reading of
    # "bounded by identity" — and it is the only bound available here, since
    # behind `tailscale serve` every request arrives from 127.0.0.1, so an
    # address pin would read as a pin while excluding nobody.
    carried_require_peer = refresh_token_requires_peer(refresh_token)
    carried_peer_key = refresh_token_peer_key(refresh_token) if carried_require_peer else ""
    verified_peer_key = (
        await _verified_peer_key(request, carried_peer_key) if carried_require_peer else ""
    )
    if carried_require_peer and (
        not verified_peer_key or not carried_peer_key or verified_peer_key != carried_peer_key
    ):
        if not verified_peer_key:
            outcome = "peer_unverified"
            code = "peer_identity_unverified"
            message = (
                "This device could not be verified on the tailnet, so the session "
                "cannot be renewed. Reconnect to the tailnet and try again."
            )
        elif not carried_peer_key:
            outcome = "peer_binding_missing"
            code = "peer_binding_missing"
            message = (
                "This session predates restart-safe device binding and cannot be "
                "renewed. Show a new phone-access QR code and scan it once."
            )
        else:
            outcome = "peer_mismatch"
            code = "peer_identity_mismatch"
            message = "This session belongs to a different tailnet device and cannot be renewed."
        _audit(user_id, "refresh_token_use", outcome, chain_id)
        # Prose in ``error``, machine id in ``code``, per the error-code contract:
        # the dashboard renders ``error`` verbatim into a localized UI, so an
        # un-coded identifier would be untranslatable by construction.
        return web.json_response(
            {
                "error": message,
                "code": code,
            },
            status=401,
        )

    # Reuse detection: if this jti is already consumed AND it is NOT
    # within the multi-tab grace window, treat as theft and revoke.
    if state.is_consumed(jti):
        cached = state.grace_replacement(chain_id, jti, client_ip, now=now)
        if cached:
            # Multi-tab race: re-serve the same replacement pair.
            try:
                payload = json.loads(cached)
            except json.JSONDecodeError:
                payload = None
            if payload:
                # SECURITY: filter out the "_"-prefixed private keys
                # (raw tokens) before JSON serialization. Tokens belong
                # ONLY in Set-Cookie headers, never in the response body
                # where JS or a network observer can read them.
                public = {k: v for k, v in payload.items() if not k.startswith("_")}
                resp = web.json_response(public)
                _set_access_cookie(resp, request, payload["_access_token"], payload["session_exp"])
                _set_refresh_cookie(
                    resp, request, payload["_refresh_token"], payload["refresh_exp"]
                )
                _expire_foreign_port_cookies(resp, request)
                _audit(user_id, "refresh_token_use", "grace_replay")
                return resp
        # Genuine reuse → revoke the chain.
        # We use the now+MAX_REFRESH_TTL_SECS as the eviction floor so the
        # revocation outlives any token in this chain.
        # Wrap in to_thread: revoke_chain does sync file I/O (mode=0o600
        # atomic-rename writes to refresh_chains.json) — must not block
        # the event loop.
        await asyncio.to_thread(state.revoke_chain, chain_id, now + MAX_REFRESH_TTL_SECS)
        _audit(user_id, "refresh_token_use", "reuse_detected", chain_id)
        resp = web.json_response({"error": "refresh_chain_revoked"}, status=401)
        _clear_refresh_cookie(resp, request)
        return resp

    # Happy path: mint a fresh access + refresh pair.
    # register_nonce=False: this is a cookie-only session token (validated on the
    # cookie path via use_session_exp, never the one-time link path), so it must
    # NOT be added to the bounded 50-slot nonce set — otherwise each refresh
    # churns/evicts pending one-time link nonces (e.g. Slack challenge links).
    # Mirrors the middleware's own link->session exchange in token_auth.py.
    # Boot binding is CARRIED rather than read from current_boot_id() here.
    #
    # Today the two are equivalent, and the reason is worth stating rather than
    # leaving as luck: validate_refresh_token above has ALREADY rejected a token
    # whose boot does not match this process, so anything reaching this line is
    # either unbound or bound to the current boot. Re-deriving would produce the
    # same value.
    #
    # Carrying is still what belongs here. It keeps this function's output a
    # function of its INPUT, so the rotated pair says what the presented
    # credential said instead of what the process happens to be — and that
    # property does not depend on a check in another module having run first. If
    # a future change ever admits an unvalidated or partially-validated token to
    # this path, carrying degrades to "unbound", while re-deriving would silently
    # mint a live binding for it.
    carried_boot = refresh_token_boot(refresh_token)
    _carried_claims: dict[str, str] = {}
    if carried_boot:
        _carried_claims["boot"] = carried_boot
    if carried_require_peer:
        # Carried onto BOTH halves of the rotated pair. Dropping it on either one
        # would make the FIRST rotation silently downgrade an identity-bound
        # session to an ordinary one — the same shape of defect as the review
        # finding this check answers, one rotation later.
        _carried_claims["require_peer"] = "1"
    new_access_token = generate_token(
        user_id,
        ttl_seconds=MAX_SESSION_TTL_SECS,
        register_nonce=False,
        peer_key=carried_peer_key,
        extra=_carried_claims or None,
    )
    new_session_exp = now + MAX_SESSION_TTL_SECS
    new_refresh_token, _new_chain, _new_jti, new_refresh_exp = generate_refresh_token(
        user_id,
        chain_id=chain_id,
        boot=carried_boot,
        require_peer=carried_require_peer,
        peer_key=carried_peer_key,
    )

    public_payload = {
        "refreshed_at": now,
        "session_exp": new_session_exp,
        "refresh_exp": new_refresh_exp,
    }
    # Stash the actual tokens alongside the public payload so the multi-tab
    # grace window can re-serve the same response without minting again.
    # Typed as ``dict[str, Any]`` because we deliberately mix float and str
    # values; the "_"-prefixed keys are filtered out before JSON
    # serialization in the grace-replay path (see line ~280).
    grace_payload: dict[str, Any] = dict(public_payload)
    grace_payload["_access_token"] = new_access_token
    grace_payload["_refresh_token"] = new_refresh_token

    # Wrap mark_consumed in to_thread: it does sync file I/O
    # (atomic-rename write to ~/.kiro/crew/refresh_chains.json) — must
    # not block the event loop.
    await asyncio.to_thread(
        state.mark_consumed,
        jti,
        chain_id=chain_id,
        exp=now + MAX_REFRESH_TTL_SECS,
        ip=client_ip,
        replacement=json.dumps(grace_payload, separators=(",", ":")),
    )

    if carried_require_peer:
        # The signed claim, not a second whois result, is authoritative. The
        # request already proved it matches ``verified_peer_key`` above; mirror
        # that exact key into the hot in-memory map for posture/reporting.
        bind_token_peer(new_access_token, carried_peer_key, new_session_exp)
    else:
        await _rebind_rotated_token_to_peer(
            request, new_access_token, new_session_exp, boot_bound=bool(carried_boot)
        )

    resp = web.json_response(public_payload)
    _set_access_cookie(resp, request, new_access_token, new_session_exp)
    _set_refresh_cookie(resp, request, new_refresh_token, new_refresh_exp)
    _expire_foreign_port_cookies(resp, request)
    _audit(user_id, "refresh_token_use", "ok")
    return resp


async def _verified_peer_key(request: web.Request, claimed_peer_key: str = "") -> str:
    """Return the allowed daemon-verified peer key for this request.

    Reads the same ``tailnet_trust`` gate and the same resolver that
    :func:`_rebind_rotated_token_to_peer` uses, so "can this be pinned" and "may
    this rotate" cannot answer differently — a rotation admitted here that the
    rebind then could not pin is precisely the gap this pair closes.
    """
    trust = request.app.get("tailnet_trust")
    if not (isinstance(trust, TailnetTrust) and trust.enforces_identity):
        return ""
    try:
        peer = await resolve_forwarded_peer(request, trust)
        if peer is None or not login_allowed(peer.login, trust.allowed_logins):
            return ""
        if claimed_peer_key:
            return peer_pin_key_for_claim(peer, claimed_peer_key)
        return peer_pin_key(peer, trust.pin_scope)
    except Exception:  # noqa: BLE001 - an auth decision must not 500 on the probe
        logger.debug("refresh: tailnet peer resolution failed", exc_info=True)
        return ""


async def _rebind_rotated_token_to_peer(
    request: web.Request, access_token: str, session_exp: float, *, boot_bound: bool = False
) -> None:
    """Carry the peer pin across an access-token rotation (RFC §3).

    This endpoint is bypassed by the auth middleware, so without this the
    replacement access token would be UNBOUND — one rotation would launder a
    node-scoped identity pin into an any-peer token. When the refresh request
    itself resolves a verified peer, the fresh token is pinned to that peer's
    key; when no peer resolves (non-tailnet setups or daemon down) the
    token stays unbound, which is byte-for-byte the pre-identity behaviour.
    The middleware's early allowlist deny already covers this route (it runs
    before the bypass list), so a verified-but-unallowlisted peer never
    reaches this mint in the first place.

    ``boot_bound`` additionally preserves the ADDRESS pin when tailnet identity
    trust is off. That case previously could not arise for the session type that
    needs it most: a phone-access QR session was minted ``no_refresh``, so it
    never rotated and the ``ip:`` pin the middleware set at the exchange held for
    its whole life. Letting such a session rotate without this would drop the pin
    on the first rotation, so a stolen rotated cookie would authenticate from any
    reachable peer — a real regression, not a theoretical one.

    Scoped to boot-bound sessions on purpose. Pinning EVERY rotation would change
    roaming behaviour for ordinary browser sessions, which today survive an
    address change precisely because their rotated token is unbound; that is a
    separate decision and does not belong in a change about phone sessions.
    """
    trust = request.app.get("tailnet_trust")
    if isinstance(trust, TailnetTrust) and trust.enforces_identity:
        peer = await resolve_forwarded_peer(request, trust)
        if peer is not None:
            bind_token_peer(access_token, peer_pin_key(peer, trust.pin_scope), session_exp)
            return
    if boot_bound:
        # Same key shape the middleware uses for the default address pin, so the
        # rotated token is checked exactly as the one it replaces was.
        client_ip = request.remote or "unknown"
        bind_token_ip(access_token, client_ip, session_exp)


async def api_auth_logout(request: web.Request) -> web.Response:
    """Revoke the caller's session (access + refresh) and clear both cookies.

    Two independent revocations happen here:

    1. Refresh chain: ``revoke_chain`` on the chain_id in the refresh cookie,
       so a stolen refresh cookie cannot be replayed (up to 30 days otherwise).
    2. Access cookie: ``revoke_access_cookie`` adds the access cookie's nonce
       to a persisted server-side denylist (CWE-613). Without this the access
       cookie — a self-contained signed token — stays a valid bearer credential
       for the rest of its ~20h TTL even after the Set-Cookie max_age=0 below,
       because clearing the browser copy does nothing to a saved/stolen copy.
       This revokes ONLY the caller's session, unlike the global
       ``revoke_all_sessions`` (kirocrew logout) which kills every session.

    The endpoint bypasses the standard ``token_auth_middleware`` (see
    ``token_auth.py``) so it is callable even when the access cookie has
    just expired — same design as ``/api/auth/refresh``.
    """
    # CSRF defense — same pattern as /api/auth/refresh.
    if not check_origin(request, require=False):
        _audit("", "refresh_token_logout", "bad_origin", request.headers.get("Origin", ""))
        return web.json_response({"error": "bad_origin"}, status=403)

    port = request.app.get("port", 7777)
    cookie_port = _cookie_port_from_host(request, port)
    refresh_cookie = request.cookies.get(refresh_cookie_name(cookie_port), "")

    user_id = ""
    chain_id = ""
    if refresh_cookie:
        valid, user_id, _reason, chain_id, _jti, _exp = validate_refresh_token(refresh_cookie)
        if valid and chain_id:
            # Revoke the chain so the cookie cannot be replayed even if
            # the browser ignores the Set-Cookie below (extension tampering,
            # network truncation, attacker holds a copy already).
            # `revoke_chain` does sync file I/O — wrap in to_thread so it
            # doesn't block the event loop.
            await asyncio.to_thread(
                _get_state().revoke_chain,
                chain_id,
                time.time() + MAX_REFRESH_TTL_SECS,
            )
            _audit(user_id, "refresh_token_logout", "ok", chain_id)
        else:
            _audit(user_id or "", "refresh_token_logout", "invalid_refresh")
    else:
        _audit("", "refresh_token_logout", "no_cookie")

    # Per-session access-cookie revocation (CWE-613). The access cookie is a
    # self-contained signed token, so the Set-Cookie max_age=0 below only
    # clears the BROWSER's copy — a saved/stolen copy stays valid for the rest
    # of its ~20h TTL. Add this specific cookie's nonce to the persisted
    # server-side denylist so validate_token rejects it on the very next
    # request, terminating only this session (not all of them). Wrapped in
    # to_thread because revoke writes the denylist file (atomic-rename, 0600).
    access_cookie = request.cookies.get(f"mc_token_{cookie_port}", "")
    if access_cookie:
        revoked = await asyncio.to_thread(revoke_access_cookie, access_cookie)
        _audit(user_id or "", "access_cookie_revoked", "ok" if revoked else "noop")

    # Always clear both cookies on the response, even when no refresh
    # cookie was present — the caller's intent is "log me out", and
    # leaving a stale access cookie behind would defeat that intent.
    resp = web.json_response({"logged_out": True})
    _clear_refresh_cookie(resp, request)
    # Mirror the access cookie name + path used in token_auth's middleware
    # so this clear actually overrides the existing cookie.
    resp.set_cookie(
        f"mc_token_{cookie_port}",
        "",
        max_age=0,
        path="/",
    )
    return resp
