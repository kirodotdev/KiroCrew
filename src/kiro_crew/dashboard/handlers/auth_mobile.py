"""Authenticated mobile sign-in-link recovery endpoint.

The endpoint mints an ordinary one-time dashboard link for a separate mobile
browser. It is distinct from refresh-token rotation: the existing dashboard
session authorizes minting, while the recipient completes the normal link-to-
cookie exchange.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.dashboard.origin import check_origin
from kiro_crew.dashboard.token_auth import (
    LINK_WINDOW_SECS,
    MAX_SESSION_TTL_SECS,
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


async def api_auth_mobile_link(request: web.Request) -> web.Response:
    """Mint a short-lived link to the configured external dashboard origin."""
    if not check_origin(request, require=False):
        _audit("", "bad_origin", request.headers.get("Origin", ""))
        return web.json_response({"error": "bad_origin", "code": "bad_origin"}, status=403)

    user_id = request.get("user", "")
    if not user_id:
        return web.json_response(
            {"error": "unauthenticated", "code": "unauthenticated"}, status=401
        )
    if request.get("app", ""):
        _audit(user_id, "app_token_denied")
        return web.json_response(
            {"error": "app_token_forbidden", "code": "app_token_forbidden"}, status=403
        )

    external_origin = dashboard_origin(request.app.get("dashboard_url", ""))
    if not external_origin:
        _audit(user_id, "external_origin_unavailable")
        return web.json_response(
            {"error": "external_origin_unavailable", "code": "external_origin_unavailable"},
            status=409,
        )

    token = generate_token(user_id, ttl_seconds=MAX_SESSION_TTL_SECS)
    _audit(user_id, "issued")
    return web.json_response(
        {
            "url": build_dashboard_url(external_origin, token, local_only=False),
            "expires_in": LINK_WINDOW_SECS,
        },
        headers={"Cache-Control": "no-store"},
    )
