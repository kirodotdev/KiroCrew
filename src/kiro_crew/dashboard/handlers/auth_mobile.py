"""Authenticated mobile sign-in-link recovery endpoint.

The endpoint mints an ordinary one-time dashboard link for a separate mobile
browser. It is distinct from refresh-token rotation: the existing dashboard
session authorizes minting, while the recipient completes the normal link-to-
cookie exchange.

The minted credential must never exceed the caller's own. A dashboard session
can be deliberately bounded — a restricted (incognito/temporary/channel-guest)
slot, a boot-bound QR session that ends at gateway restart, or a ``no_refresh``
session whose short ``session_exp`` is the whole reason handing that device a
credential was acceptable. Without a guard, one POST from such a session would
mint a fresh 20-hour refresh-chained link and silently escape the ceiling the
operator set — the same laundering ``token_auth`` closes on the exchange path.
Enforced here in two halves: restricted sessions are refused outright, and the
caller's own token bounds (``boot``, ``no_refresh``, remaining ``session_exp``)
are carried into the minted link so the new session inherits — never exceeds —
them.

The sibling QR surface does NOT yet carry those bounds:
``tailnet_mobile._guard`` gates on app-token, owner and restricted-slot only,
so a ``no_refresh`` or short-``session_exp`` owner session still passes it and
mints a boot-bound, refresh-chained credential. Extending ``_caller_bounds`` to
that surface is tracked separately; do not read that guard as closing this
ceiling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.dashboard.token_auth import (
    LINK_WINDOW_SECS,
    MAX_SESSION_TTL_SECS,
    _b64url_decode,
    _cookie_port_from_host,
    generate_token,
)
from kiro_crew.dashboard.urls import build_dashboard_url, dashboard_origin
from kiro_crew.sel import sel as _sel_fn

logger = logging.getLogger(__name__)


def _audit(user_id: str, outcome: str, error: str = "") -> None:
    """Record mobile-link issuance without making authentication depend on SEL."""
    try:
        _sel_fn().log_api_access(
            caller=user_id or "<unknown>",
            operation="mobile_login_link",
            outcome=outcome,
            source="mobile_auth",
            resources=error,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("mobile_auth: SEL audit failed: %s", exc)


async def _audit_async(user_id: str, outcome: str, error: str = "") -> None:
    """Write the SEL record off-loop, including the first cold initialization.

    Same shape as the sibling ``tailnet_mobile._audit_async``, and for the same
    reason: the first audit after a restart pays SEL's synchronous filesystem
    initialization, which on the event loop stalls every other request the
    gateway is serving.
    """
    await asyncio.to_thread(_audit, user_id, outcome, error)


def _caller_bounds(request: web.Request) -> tuple[dict[str, str], int]:
    """Read the caller's own session bounds from the token that authenticated it.

    Returns ``(carried_claims, ttl_ceiling_seconds)``. ``ttl_ceiling`` is ``0``
    when the caller has no lifetime left to lend, which the handler refuses
    rather than minting against. Claims are carried, never re-derived: ``boot``
    copied verbatim (same rule as the link→session exchange in ``token_auth``),
    ``no_refresh`` copied so the recipient session never grows a refresh chain,
    and the remaining ``session_exp`` becomes the TTL ceiling so a short-lived
    caller cannot mint a longer-lived credential. Fail-closed on an unreadable
    payload: a caller whose bounds cannot be established gets a bounded
    (no-refresh, default-TTL-capped) link rather than an unbounded one.

    **Extraction order must mirror the middleware's, query param before cookie.**
    Only the credential the middleware actually validated has a verified
    signature; the other one was never checked. The middleware prefers
    ``?token=`` (``token_auth`` middleware and ``_extract_and_validate``, both
    ``request.query.get("token") or request.cookies.get(...)``), so reading the
    cookie first would let a request that authenticated with a bounded query
    token have its bounds read from an unverified, attacker-settable cookie —
    dropping ``no_refresh`` and raising the TTL ceiling to the full maximum,
    which is precisely the ceiling-escape this function exists to prevent.

    **A non-positive remaining lifetime is never rounded up.** Clamping it to a
    floor of one second would let a caller whose own session has just run out
    mint a link that outlives it, and the exchange the recipient performs starts
    a fresh window — so repeating the mint would walk the expiry forward
    indefinitely from a session that should already be dead. Report ``0`` and
    let the caller be refused.
    """
    port = request.app.get("port", 7777)
    cookie_name = f"mc_token_{_cookie_port_from_host(request, port)}"
    token = request.query.get("token", "") or request.cookies.get(cookie_name, "")
    carried: dict[str, str] = {}
    ttl_ceiling = MAX_SESSION_TTL_SECS
    if not token:
        # Authenticated without a readable token (unexpected on this surface):
        # fail closed by bounding the mint rather than trusting it.
        return {"no_refresh": "1"}, ttl_ceiling
    try:
        data = json.loads(_b64url_decode(token.split(".", 1)[0]))
        boot = str(data.get("boot", ""))
        if boot:
            carried["boot"] = boot
        if str(data.get("no_refresh", "")) == "1":
            carried["no_refresh"] = "1"
        session_exp = float(data.get("session_exp", 0.0))
        if session_exp:
            remaining = int(session_exp - time.time())
            if remaining <= 0:
                return carried, 0
            ttl_ceiling = min(ttl_ceiling, remaining)
    except Exception:
        return {"no_refresh": "1"}, ttl_ceiling
    return carried, ttl_ceiling


async def api_auth_mobile_link(request: web.Request) -> web.Response:
    """Mint a short-lived link to the configured external dashboard origin."""
    if not check_origin(request, require=False):
        await _audit_async("", "bad_origin", request.headers.get("Origin", ""))
        return web.json_response({"error": "bad_origin", "code": "bad_origin"}, status=403)

    user_id = request.get("user", "")
    if not user_id:
        await _audit_async("", "unauthenticated")
        return web.json_response(
            {"error": "unauthenticated", "code": "unauthenticated"}, status=401
        )
    if request.get("app", ""):
        await _audit_async(user_id, "app_token_denied")
        return web.json_response(
            {"error": "app_token_forbidden", "code": "app_token_forbidden"}, status=403
        )

    # A restricted (incognito/temporary/channel-guest) session must not trade
    # itself for a durable any-device credential — same predicate as the
    # sibling tailnet-mobile surface's guard.
    state = request.app.get("state")
    if state is not None and _is_restricted_session(state, request):
        await _audit_async(user_id, "restricted_session_denied")
        return web.json_response(
            {"error": "restricted_session", "code": "restricted_session"}, status=403
        )

    external_origin = dashboard_origin(request.app.get("dashboard_url", ""))
    if not external_origin:
        await _audit_async(user_id, "external_origin_unavailable")
        return web.json_response(
            {"error": "external_origin_unavailable", "code": "external_origin_unavailable"},
            status=409,
        )

    carried, ttl_ceiling = _caller_bounds(request)
    if ttl_ceiling <= 0:
        # The caller has no lifetime left to lend. Minting here would hand out a
        # credential that outlives the session authorizing it.
        await _audit_async(user_id, "caller_session_expired")
        return web.json_response(
            {"error": "caller_session_expired", "code": "caller_session_expired"}, status=403
        )

    token = generate_token(user_id, ttl_seconds=ttl_ceiling, extra=carried or None)
    await _audit_async(user_id, "issued")
    return web.json_response(
        {
            "url": build_dashboard_url(external_origin, token, local_only=False),
            "expires_in": LINK_WINDOW_SECS,
        },
        headers={"Cache-Control": "no-store"},
    )
