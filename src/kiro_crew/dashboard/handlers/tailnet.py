"""Tailnet dashboard-access status — one read-only endpoint for the status card.

Reports what the RUNNING SERVER TRUSTS, never a fresh probe. The distinction is
the whole design of this module, so it is stated once here and relied on below:
``tailnet.resolve_tailnet_host`` runs exactly once, during
``start_dashboard`` / ``start_api_server``, and its result is what actually went
into ``build_allowed_origins``. Re-probing the daemon at request time could report
a name the live origin set does **not** contain (the common case: tailscaled came
up after the gateway), and rendering that as "active" would be the
"checked-but-never-ran → looks fine" defect this repo already has a lesson about.
So the endpoint reads the startup value stashed on the app, and ``resolved_at``
makes its staleness legible.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.config import KiroCrewConfig

logger = logging.getLogger(__name__)


def _derive_state(*, pinned: bool, enabled: bool, host: str) -> str:
    """The card's single state value, derived HERE and nowhere else.

    One owner for the state machine: the frontend renders ``state`` and never
    re-derives it, so the two layers cannot disagree about what a host with
    ``enabled=true`` and an empty ``host`` means.

    Precedence, in order — a pin outranks everything because it is the one cause
    the operator cannot lift, and saying "off" to a pinned host would send them to
    a switch that is already in the position they want:

    1. ``pinned``  — an administrator's ceiling forbids the derivation.
    2. ``off``     — not pinned, and the operator has not enabled it.
    3. ``unresolved`` — enabled, but nothing was trusted at startup.
    4. ``active``  — enabled, and a name is in the live origin set.
    """
    if pinned:
        return "pinned"
    if not enabled:
        return "off"
    return "unresolved" if not host else "active"


async def api_tailnet_status(request: web.Request) -> web.Response:
    """GET /api/tailnet/status — tailnet access state for the dashboard card.

    ``enabled`` is the stored ``dashboard.tailscale.enabled`` as actually loaded
    (post-hydration, so a ``config.local.json`` overlay is reflected).
    ``governance_pinned`` is the POLICY-layer answer for
    ``capabilities.tailnet_origin``; surfacing it separately is the point, because
    the card must distinguish "off because the operator left the switch off"
    (flippable) from "off because an administrator pinned it", where the PATCH
    route itself returns 403 — the one case the user cannot lift.

    ``host`` / ``origin`` / ``resolved_at`` come from the STARTUP resolution
    stashed on the app (see the module docstring), not from a live daemon call.

    Read-only, and never 500s: an unreadable config is exactly when the operator
    wants this card. Failure degrades toward "off"/"unresolved" so the UI never
    claims an origin is trusted when we cannot prove it.
    """
    host = str(request.app.get("tailnet_host") or "")
    try:
        resolved_at = int(request.app.get("tailnet_resolved_at") or 0)
    except (TypeError, ValueError):
        resolved_at = 0

    try:
        # to_thread, not a bare load(): KiroCrewConfig.load() stats and reads
        # config.json (+ any config.local.json overlay), and this handler runs on
        # the aiohttp event loop — a synchronous read stalls every other request
        # behind it. Mirrors api_beacon_status.
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        enabled = bool(cfg.dashboard.tailscale.enabled)
    except Exception:
        logger.debug("tailnet status: config unreadable; reporting disabled", exc_info=True)
        enabled = False

    try:
        # No audit_tool: this is a pure READ the card refetches, and auditing an
        # inspection would append HMAC-chained SEL rows for a question rather than
        # a decision (see tailnet.is_governance_pinned_off).
        from kiro_crew.dashboard import tailnet

        pinned = await asyncio.to_thread(tailnet.is_governance_pinned_off)
    except Exception:  # pragma: no cover - the probe is itself guarded
        logger.debug("tailnet status: governance probe unavailable", exc_info=True)
        pinned = False

    return web.json_response(
        {
            "enabled": enabled,
            "governance_pinned": pinned,
            "host": host,
            "origin": f"https://{host}" if host else "",
            "resolved_at": resolved_at,
            "state": _derive_state(pinned=pinned, enabled=enabled, host=host),
        }
    )
