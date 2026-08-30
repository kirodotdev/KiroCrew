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

The bound-reading half lives in ``_shared._caller_bounds`` because the sibling
tailnet QR mint (``tailnet_mobile``) enforces the same invariant on its own
mint; one shared reader keeps the two surfaces from drifting apart.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _caller_bounds, _is_restricted_session
from kiro_crew.dashboard.handlers.mobile_connect import mint_denied_reason
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.dashboard.token_auth import (
    LINK_WINDOW_SECS,
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

    # Governance chokepoint: minting a mobile sign-in link is the "login_link"
    # method of the capabilities.mobile_connect scope. The methods listing may
    # already hide this method, but omission is presentation only — the mint
    # itself re-runs the decision (fail-closed inside mint_denied_reason).
    denied = await asyncio.to_thread(mint_denied_reason, "login_link")
    if denied:
        await _audit_async(user_id, "governance_denied", denied)
        return web.json_response({"error": denied, "code": "governance_denied"}, status=403)

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

    caller_peer_key = carried.pop("peer_key", "")
    token = generate_token(
        user_id,
        ttl_seconds=ttl_ceiling,
        peer_key=caller_peer_key,
        extra=carried or None,
    )
    await _audit_async(user_id, "issued")
    return web.json_response(
        {
            "url": build_dashboard_url(external_origin, token, local_only=False),
            # The live click window: generate_token clamps the link-click
            # ``exp`` to the session TTL, so a caller lending less than the
            # nominal window mints a link that dies with its own remaining
            # lifetime — report that, not the constant, or the UI countdown
            # overstates how long the link actually works.
            "expires_in": min(LINK_WINDOW_SECS, ttl_ceiling),
        },
        headers={"Cache-Control": "no-store"},
    )
