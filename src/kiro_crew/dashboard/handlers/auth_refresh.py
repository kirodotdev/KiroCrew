"""Auth refresh endpoints: ``GET /api/auth/me`` and ``POST /api/auth/refresh``.

Spec: ``docs/token-refresh/REQUIREMENTS.md``.

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
    generate_refresh_token,
    refresh_cookie_name,
    validate_refresh_token,
)
from kiro_crew.dashboard.token_auth import (
    MAX_SESSION_TTL_SECS,
    _cookie_port_from_host,
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

_refresh_rate_lock = threading.Lock()
_refresh_rate_buckets: dict[str, collections.deque[float]] = {}


def _rate_limited(client_ip: str, now: float | None = None) -> bool:
    """Return True if *client_ip* is over the per-minute refresh cap.

    Fails CLOSED when client_ip is empty: bucket unknown-IP requests
    under a single sentinel key so they share a rate limit. Never
    silently bypass the cap — defense-in-depth per AutoSDE
    security-controls (deny-by-default).
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
        bucket = _refresh_rate_buckets.setdefault(client_ip, collections.deque())
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
    TTL via the ``or`` operator (which fires because 0 is falsy). Per
    AutoSDE finding f-... on rev3.
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
        # Per AutoSDE finding (CR-281631553 post 41).
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

    # The middleware has already validated the access cookie. We can read
    # session_exp from the cookie payload directly.
    port = request.app.get("port", 7777)
    cookie_name = f"mc_token_{_cookie_port_from_host(request, port)}"
    access_token = request.cookies.get(cookie_name, "")
    # session_exp is a FLOAT claim; the string-only extract_claims_from_token
    # silently drops it (always yielding 0.0 here), which disabled the
    # frontend's proactive-refresh scheduler. Use the numeric extractor.
    session_exp = extract_numeric_claim(access_token, "session_exp") or 0.0

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
            resp = web.json_response(
                {"error": "refresh_chain_revoked"}, status=401
            )
            _clear_refresh_cookie(resp, request)
            return resp
        _audit(user_id, "refresh_token_use", "invalid", reason)
        return web.json_response(
            {"error": "invalid_refresh"}, status=401
        )

    state = _get_state()

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
                _set_access_cookie(
                    resp, request, payload["_access_token"], payload["session_exp"]
                )
                _set_refresh_cookie(
                    resp, request, payload["_refresh_token"], payload["refresh_exp"]
                )
                _audit(user_id, "refresh_token_use", "grace_replay")
                return resp
        # Genuine reuse → revoke the chain.
        # We use the now+MAX_REFRESH_TTL_SECS as the eviction floor so the
        # revocation outlives any token in this chain.
        # Wrap in to_thread: revoke_chain does sync file I/O (mode=0o600
        # atomic-rename writes to refresh_chains.json) — must not block
        # the event loop. Per kirocrew-cr-reviewer rule 4d5.
        await asyncio.to_thread(
            state.revoke_chain, chain_id, now + MAX_REFRESH_TTL_SECS
        )
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
    new_access_token = generate_token(
        user_id, ttl_seconds=MAX_SESSION_TTL_SECS, register_nonce=False
    )
    new_session_exp = now + MAX_SESSION_TTL_SECS
    new_refresh_token, _new_chain, _new_jti, new_refresh_exp = generate_refresh_token(
        user_id, chain_id=chain_id
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
    # (atomic-rename write to ~/.kirocrew/refresh_chains.json) — must
    # not block the event loop. Per kirocrew-cr-reviewer rule 4d5.
    await asyncio.to_thread(
        state.mark_consumed,
        jti,
        chain_id=chain_id,
        exp=now + MAX_REFRESH_TTL_SECS,
        ip=client_ip,
        replacement=json.dumps(grace_payload, separators=(",", ":")),
    )

    resp = web.json_response(public_payload)
    _set_access_cookie(resp, request, new_access_token, new_session_exp)
    _set_refresh_cookie(resp, request, new_refresh_token, new_refresh_exp)
    _audit(user_id, "refresh_token_use", "ok")
    return resp


# Note: the initial-mint refresh-cookie attach helper used to live here as
# `attach_refresh_cookie_to_response`. It was inlined into
# token_auth.py's middleware so token_auth.py no longer needs an in-method
# import of this module — top-level imports only, no circular dependency.


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
        valid, user_id, _reason, chain_id, _jti, _exp = validate_refresh_token(
            refresh_cookie
        )
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
