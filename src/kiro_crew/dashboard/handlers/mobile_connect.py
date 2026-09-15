"""Phone-connection method listing — the CPP ``mobile_connect`` seam's consumer.

``GET /api/mobile-connect/methods`` answers ONE question for the dashboard:
which ways of handing a phone a live session exist on this deployment, under
the current governance ceiling. The rows come from
``current_context().mobile_connect`` (the personal-install Default is the
tailnet QR + one-time login link pair; an enterprise companion swaps the list)
and each id is filtered through the ``capabilities.mobile_connect`` scope.

The response is deliberately descriptor-only (``{id, kind}``): minting the
actual credential stays on each method's own endpoint (``/api/tailnet/mobile/qr``,
``/api/auth/mobile-link``), which re-run this same governance decision before
acting — a filtered list is presentation, never the control. An empty list
(edition returned none, policy denied all, or the seam read degraded) makes the
dashboard hide its "Connect your phone" entry rather than render dead buttons.

Auth floor matches ``/api/auth/mobile-link``'s read half: an authenticated,
non-app dashboard user. Restricted sessions may READ the list (the entry hides
nothing secret — kind names only); their mint attempts are refused by the mint
endpoints' own guards, which is where that refusal already lives.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.origin import check_origin

logger = logging.getLogger(__name__)

#: Governed scope for phone-connection methods (SCOPE_CATALOG row).
MOBILE_CONNECT_SCOPE = "capabilities.mobile_connect"

#: Surface key used for ALL mobile-connect governance evaluations. These
#: endpoints are dashboard-operator surfaces by construction (app tokens are
#: refused, auth floor is the dashboard user), and ``X-Session-Key`` is
#: CALLER-CONTROLLED — classifying by it would let a request carrying
#: ``slack:x`` dodge a profile bound to the ``dashboard`` surface. The request
#: header stays authoritative only for what it already guards elsewhere
#: (restricted-session checks); the governance surface is pinned here.
_DASHBOARD_SURFACE_KEY = "dashboard:ui"


def _governed_methods() -> list[dict[str, str]]:
    """Seam read + governance filter, shared by the endpoint and future callers.

    ``safe_context_call`` fallback is ``[]``: a degraded seam read HIDES the
    entry instead of guessing at methods whose mint endpoints would then 403.
    The capability check is fail-closed (a wrong-permit widens an auth
    surface); a per-id denial drops that row and keeps the rest.

    Governance classifies by the PINNED ``_DASHBOARD_SURFACE_KEY`` — never the
    caller-controlled ``X-Session-Key`` header (see the constant's rationale).
    Synchronous (profile resolution can touch the filesystem): call via
    ``asyncio.to_thread`` from a handler.
    """
    from kiro_crew.platform.context import current_context, safe_context_call
    from kiro_crew.platform.governance_profiles import vet_and_audit
    from kiro_crew.platform.interfaces import MobileConnectMethod

    methods: list[MobileConnectMethod] = safe_context_call(
        lambda: list(current_context().mobile_connect.connect_methods()),
        fallback_factory=list,
        log_message="mobile_connect.connect_methods degraded; hiding the connect entry",
    )
    if not methods:
        return []
    gate = vet_and_audit(
        MOBILE_CONNECT_SCOPE,
        "",
        session_key=_DASHBOARD_SURFACE_KEY,
        tool_name="mobile_connect_methods",
        log_warning=False,
        fail_closed=True,
    )
    if not getattr(gate, "permitted", False):
        return []
    out: list[dict[str, str]] = []
    for m in methods:
        mid = getattr(m, "id", "")
        kind = getattr(m, "kind", "")
        if not mid or not kind:
            continue  # malformed descriptor: drop, never shadow
        scoped = vet_and_audit(
            MOBILE_CONNECT_SCOPE,
            f"methods:{mid}",
            session_key=_DASHBOARD_SURFACE_KEY,
            tool_name="mobile_connect_methods",
            log_warning=False,
            fail_closed=True,
        )
        if getattr(scoped, "permitted", False):
            out.append({"id": mid, "kind": kind})
    return out


def _audit(user_id: str, outcome: str, detail: str = "") -> None:
    """Record a methods-listing refusal without making the read depend on SEL."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=user_id or "<unknown>",
            operation="mobile_connect_methods",
            outcome=outcome,
            source="mobile_connect",
            resources=detail,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("mobile_connect: SEL audit failed: %s", exc)


async def api_mobile_connect_methods(request: web.Request) -> web.Response:
    """GET /api/mobile-connect/methods → ``{"methods": [...]}`` (empty = hide the entry)."""
    if not check_origin(request, require=False):
        await asyncio.to_thread(_audit, "", "bad_origin", request.headers.get("Origin", ""))
        return web.json_response({"error": "bad origin", "code": "bad_origin"}, status=403)
    if not request.get("user"):
        await asyncio.to_thread(_audit, "", "unauthenticated")
        return web.json_response(
            {"error": "unauthenticated", "code": "unauthenticated"}, status=401
        )
    if request.get("app"):
        # App tokens act for an app, not the operator; connection methods are
        # an operator surface (mirrors /api/auth/mobile-link's refusal, audit
        # included — a rejected access belongs in the trail like a granted one).
        await asyncio.to_thread(_audit, str(request.get("user") or ""), "app_token_denied")
        return web.json_response(
            {"error": "app tokens cannot list connect methods", "code": "app_token_forbidden"},
            status=403,
        )
    methods = await asyncio.to_thread(_governed_methods)
    return web.json_response({"methods": methods})


def mint_denied_reason(method_id: str) -> str:
    """Deployment + governance re-check for a mint endpoint acting on *method_id*.

    Returns ``""`` when permitted, else a short reason. The listing above may
    have hidden the method already, but omission is presentation only — the
    endpoint that actually mints a credential must consult the same controls
    itself. TWO independent controls, both fail-closed:

    1. **The seam.** The method must exist in the active provider's
       ``connect_methods()``. An edition that swapped the provider to remove a
       method has disabled it — a direct POST to the built-in endpoint must not
       out-rank that. A degraded seam read denies (empty fallback), matching
       the listing's posture.
    2. **Governance.** Both halves (capability on/off and the ``methods``
       ruleset), mirroring the spawn capability's two-step. Classifies by the
       PINNED ``_DASHBOARD_SURFACE_KEY`` — never a caller-supplied key (see
       the constant's rationale).

    Synchronous: call via ``asyncio.to_thread`` from a handler.
    """
    from kiro_crew.platform.context import current_context, safe_context_call
    from kiro_crew.platform.governance_profiles import vet_and_audit

    offered: list[str] = safe_context_call(
        lambda: [getattr(m, "id", "") for m in current_context().mobile_connect.connect_methods()],
        fallback_factory=list,
        log_message="mobile_connect.connect_methods degraded; denying the mint",
    )
    if method_id not in offered:
        return f"method {method_id!r} is not offered by this deployment"

    gate = vet_and_audit(
        MOBILE_CONNECT_SCOPE,
        "",
        session_key=_DASHBOARD_SURFACE_KEY,
        tool_name="mobile_connect_mint",
        log_warning=False,
        fail_closed=True,
    )
    if not getattr(gate, "permitted", False):
        return getattr(gate, "reason", "") or "mobile connect disabled by policy"
    scoped = vet_and_audit(
        MOBILE_CONNECT_SCOPE,
        f"methods:{method_id}",
        session_key=_DASHBOARD_SURFACE_KEY,
        tool_name="mobile_connect_mint",
        log_warning=False,
        fail_closed=True,
    )
    if not getattr(scoped, "permitted", False):
        return getattr(scoped, "reason", "") or f"method {method_id!r} disabled by policy"
    return ""
