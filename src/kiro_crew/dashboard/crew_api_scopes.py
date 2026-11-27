"""Which Crew API route an app credential may call: one row per operation.

:data:`ROUTES` holds, for every operation in the Crew API spec, its API tier (Public
or Owner), the scopes of which any one is enough, and the entitlements it names. The
rows are the spec's own ``x-crew-tier``, ``x-crew-scope`` and ``x-crew-entitlement``
(``docs/request-for-change/assets/crew-api/crew-public.yaml`` and ``crew-owner.yaml``),
and ``test/test_crew_api_scopes.py`` holds the whole table equal to those two files.

:func:`refusal_for` is the one decision: it answers why an app credential may not call a
route, or ``None`` when the route's row admits it. It never admits by default. A
route with no row, an Owner route, a route that names an entitlement and the route
only an embed credential may call are each refused, whatever the scopes.

This is not :mod:`kiro_crew.dashboard.route_access`. That table labels every gateway
route with an action name and a *stability* tier, and no request reads it. This one
covers the Crew API alone, its ``api_tier`` says *who* may call a route, and token
auth reads it on each request that carries an app credential. The two share a key
shape, ``(METHOD, path pattern)``, and nothing else.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Final

from aiohttp import web

from kiro_crew.dashboard.crew_api_errors import contract_error, request_id_for

__all__ = [
    "ANY",
    "APP_REFUSED",
    "CREDENTIAL_KEY",
    "EMBED",
    "OWNER",
    "PUBLIC",
    "ROUTES",
    "Refusal",
    "RouteScope",
    "refusal_for",
    "require_scope",
    "route_key",
]

PUBLIC: Final = "public"
OWNER: Final = "owner"

#: ``x-crew-scope: any``: every verified credential may call the route.
ANY: Final = frozenset({"any"})
#: ``x-crew-scope: embed``: only an embed credential may call the route.
EMBED: Final = frozenset({"embed"})

#: Where token auth puts the verified app credential on the request.
CREDENTIAL_KEY: Final = "crew_api_credential"

APP_REFUSED = "an app credential may not call this endpoint"
_NO_CALLER = "this caller may not call this endpoint"


@dataclass(frozen=True)
class RouteScope:
    """One operation's API tier, scopes and entitlements, as the spec gives them."""

    api_tier: str
    scopes: frozenset[str]
    #: In the spec's order: the first is the one a refusal names.
    entitlements: tuple[str, ...] = ()


@dataclass(frozen=True)
class Refusal:
    """Why a caller may not call a route: the error code and its message.

    ``reason`` is the audit word. It differs from ``code`` only where two causes
    share one code. ``entitlement`` is set on ``entitlement_required`` alone.
    """

    code: str
    message: str
    reason: str
    entitlement: str = ""

    @property
    def audit(self) -> str:
        """The audit row's error: the code, with the reason where it says more."""
        return self.code if self.reason == self.code else f"{self.code}:{self.reason}"

    def response(self, request: web.Request, headers: dict[str, str] | None = None) -> web.Response:
        """The 403 in the contract's error body, naming the entitlement where one is missing."""
        extra = {"entitlement": self.entitlement} if self.entitlement else None
        return contract_error(403, self.code, self.message, request_id_for(request), headers, extra)


def _pub(*scopes: str, needs: tuple[str, ...] = ()) -> RouteScope:
    return RouteScope(PUBLIC, frozenset(scopes), needs)


def _own(scope: str) -> RouteScope:
    return RouteScope(OWNER, frozenset({scope}))


_PUBLIC_ANY: Final = RouteScope(PUBLIC, ANY)
_SCHEDULES: Final = _pub("schedules:manage", needs=("schedules.manage",))
_WORKFLOWS: Final = _pub("workflows:run", needs=("workflows.run",))

# Keyed by (METHOD, path pattern), the pattern being the route's canonical path.
ROUTES: Final[Mapping[tuple[str, str], RouteScope]] = {
    ("POST", "/v1/responses"): _pub("responses:write"),
    ("GET", "/v1/responses/{response_id}"): _pub("conversations:read", "responses:write"),
    ("POST", "/v1/responses/{response_id}/cancel"): _pub("responses:write"),
    ("GET", "/v1/responses/{response_id}/input_items"): _pub("conversations:read"),
    ("POST", "/v1/files"): _pub("responses:write"),
    ("DELETE", "/v1/files/{file_id}"): _pub("responses:write"),
    ("POST", "/v1/conversations"): _pub("responses:write"),
    ("GET", "/v1/conversations"): _pub("conversations:read"),
    ("GET", "/v1/conversations/{conversation_id}"): _pub("conversations:read"),
    ("POST", "/v1/conversations/{conversation_id}"): _pub("responses:write"),
    ("DELETE", "/v1/conversations/{conversation_id}"): _pub("responses:write"),
    ("GET", "/v1/conversations/{conversation_id}/items"): _pub("conversations:read"),
    ("GET", "/v1/models"): _PUBLIC_ANY,
    ("GET", "/v1/crew/capabilities"): _PUBLIC_ANY,
    ("GET", "/v1/crew/info"): _PUBLIC_ANY,
    ("GET", "/v1/crew/grant"): _PUBLIC_ANY,
    ("GET", "/v1/crew/events"): _pub("conversations:read", "instance:read", "bots:wake"),
    ("GET", "/v1/crew/approvals"): _pub("conversations:read"),
    ("GET", "/v1/crew/approvals/{approval_id}"): _pub("conversations:read"),
    ("POST", "/v1/crew/approvals/{approval_id}/decision"): RouteScope(PUBLIC, EMBED),
    ("GET", "/v1/crew/bots"): _pub("bots:chat", "bots:read"),
    ("POST", "/v1/crew/bots"): _pub("bots:create", needs=("bots.create",)),
    ("GET", "/v1/crew/bots/{bot_id}/goals"): _pub("bots:read"),
    ("GET", "/v1/crew/bots/{bot_id}/reports"): _pub("bots:read"),
    ("POST", "/v1/crew/bots/{bot_id}/wake"): _pub("bots:wake"),
    ("GET", "/v1/crew/artifacts"): _pub("artifacts:read"),
    ("POST", "/v1/crew/artifacts"): _pub("artifacts:write"),
    ("GET", "/v1/crew/artifacts/{artifact_id}"): _pub("artifacts:read"),
    ("PATCH", "/v1/crew/artifacts/{artifact_id}"): _pub("artifacts:write"),
    ("DELETE", "/v1/crew/artifacts/{artifact_id}"): _pub("artifacts:write"),
    ("GET", "/v1/crew/artifacts/{artifact_id}/versions"): _pub("artifacts:read"),
    ("GET", "/v1/crew/artifacts/{artifact_id}/comments"): _pub("artifacts:read"),
    ("POST", "/v1/crew/artifacts/{artifact_id}/comments"): _pub("artifacts:write"),
    ("POST", "/v1/crew/notifications"): _pub("notifications:send", needs=("notifications.send",)),
    ("GET", "/v1/crew/schedules"): _SCHEDULES,
    ("POST", "/v1/crew/schedules"): _SCHEDULES,
    ("PATCH", "/v1/crew/schedules/{schedule_id}"): _SCHEDULES,
    ("DELETE", "/v1/crew/schedules/{schedule_id}"): _SCHEDULES,
    ("POST", "/v1/crew/schedules/{schedule_id}/run"): _SCHEDULES,
    ("GET", "/v1/crew/workflows/runs"): _WORKFLOWS,
    ("POST", "/v1/crew/workflows/runs"): _WORKFLOWS,
    ("GET", "/v1/crew/workflows/runs/{run_id}"): _WORKFLOWS,
    ("POST", "/v1/crew/workflows/runs/{run_id}/cancel"): _WORKFLOWS,
    ("POST", "/v1/crew/memory/recall"): _pub("memory:read", needs=("memory.read", "memory.global")),
    ("POST", "/v1/crew/knowledge/search"): _pub("knowledge:read", needs=("knowledge.read",)),
    ("GET", "/v1/crew/webhooks"): _pub("webhooks:manage"),
    ("POST", "/v1/crew/webhooks"): _pub("webhooks:manage"),
    ("DELETE", "/v1/crew/webhooks/{webhook_id}"): _pub("webhooks:manage"),
    ("GET", "/v1/crew/owner/bots"): _own("bots:manage"),
    ("GET", "/v1/crew/owner/grants"): _own("grants:manage"),
    ("PATCH", "/v1/crew/owner/grants/{grant_id}"): _own("grants:manage"),
    ("DELETE", "/v1/crew/owner/grants/{grant_id}"): _own("grants:manage"),
    ("POST", "/v1/crew/owner/grants/{grant_id}/resume"): _own("grants:manage"),
    ("GET", "/v1/crew/owner/grants/{grant_id}/decisions"): _own("grants:manage"),
    ("GET", "/v1/crew/owner/grant-requests"): _own("grants:manage"),
    ("GET", "/v1/crew/owner/grant-requests/{request_id}"): _own("grants:manage"),
    ("POST", "/v1/crew/owner/grant-requests/{request_id}/decision"): _own("grants:manage"),
    ("GET", "/v1/crew/owner/approvals"): _own("approvals:answer"),
    ("GET", "/v1/crew/owner/approvals/{approval_id}"): _own("approvals:answer"),
    ("POST", "/v1/crew/owner/approvals/{approval_id}/decision"): _own("approvals:answer"),
    ("GET", "/v1/crew/owner/connectors"): _own("grants:manage"),
    ("GET", "/v1/crew/owner/connectors/{client_id}"): _own("grants:manage"),
    ("GET", "/v1/crew/owner/bundles"): _own("grants:manage"),
    ("GET", "/v1/crew/owner/bundles/{bundle_id}"): _own("grants:manage"),
    ("PATCH", "/v1/crew/owner/bundles/{bundle_id}"): _own("grants:manage"),
    ("DELETE", "/v1/crew/owner/bundles/{bundle_id}"): _own("grants:manage"),
}


def route_key(request: web.Request) -> tuple[str, str]:
    """The :data:`ROUTES` key of the route *request* matched.

    The pattern is ``""`` when the path matched no route, which has no row.
    """
    resource = getattr(getattr(request.match_info, "route", None), "resource", None)
    pattern = getattr(resource, "canonical", "")
    return request.method, pattern if isinstance(pattern, str) else ""


def refusal_for(
    key: tuple[str, str], held: Collection[str], owner_only_today: Collection[str]
) -> Refusal | None:
    """Why an app credential holding the scopes *held* may not call route *key*.

    ``None`` when the route's row admits it. The order is the contract's: the tier,
    then the entitlement, then the scope. *owner_only_today* names the Public
    patterns that serve the owner alone for now. It is read before the table, so
    such a route is refused by tier even where its row would admit the caller.
    """
    if key[1] in owner_only_today:
        return Refusal("owner_only", APP_REFUSED, "owner_only")
    row = ROUTES.get(key)
    if row is None:
        return Refusal("owner_only", APP_REFUSED, "no-row")
    if row.api_tier != PUBLIC:
        return Refusal("owner_only", APP_REFUSED, "owner_only")
    if row.entitlements:
        # shortcut: no app credential carries an entitlement yet, so a route that
        # names one is refused outright. Replace this branch when entitlement
        # checks exist.
        missing = row.entitlements[0]
        return Refusal("entitlement_required", f"needs {missing}", "entitlement_required", missing)
    if row.scopes == EMBED:
        return Refusal(
            "approval_requires_first_party",
            "an app credential may not answer an approval",
            "approval_requires_first_party",
        )
    if row.scopes == ANY or not row.scopes.isdisjoint(held):
        return None
    wanted = " or ".join(sorted(row.scopes))
    return Refusal("insufficient_scope", f"needs {wanted}", "insufficient_scope")


def require_scope(
    request: web.Request, key: tuple[str, str], owner_only_today: Collection[str]
) -> web.Response | None:
    """The 403 for a handler whose caller may not call route *key*, else ``None``.

    A handler that serves apps calls this first, with its own route's key, so a
    route registered under the wrong row is refused rather than served. It decides
    from what token auth positively set on the request, never from what is absent:

    - a verified app credential (:data:`CREDENTIAL_KEY`) is judged by the table;
    - a first-party session, ``request["is_dashboard_user"] is True`` with
      ``request["app"] == ""``, passes: the route's own gate judges the owner or a teammate;
    - any other request is refused, one that carries neither marker included.

    *owner_only_today* is :func:`refusal_for`'s, and has no default: a caller that left it
    out would admit an app to a route that serves the owner alone for now.
    """
    credential = request.get(CREDENTIAL_KEY)
    if credential is not None:
        refusal = refusal_for(key, credential.scopes, owner_only_today)
    elif request.get("is_dashboard_user") is True and request.get("app") == "":
        refusal = None
    else:
        refusal = Refusal("owner_only", _NO_CALLER, "unknown-caller")
    return None if refusal is None else refusal.response(request)
