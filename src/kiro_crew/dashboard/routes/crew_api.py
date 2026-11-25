"""Route registration for the Crew API (``/v1/``).

Each route but ``/v1/crew/grant`` is served through
:func:`kiro_crew.dashboard.crew_api_owner.owner_route`.
The Bot roster wraps the handler of an existing ``/api/*`` route, whose twin stays
registered for the clients that still call it. The grant-request and grant routes have
no twin: they are the owner's side of local authorization (``crew_api_grant_requests``). Nor
have the approval routes: they name a tool approval by an ``apr_`` id the dashboard's
own routes do not have (``crew_api_approvals``).

``test/fixtures/crew_api_owner_openapi.json`` describes every Owner route registered
here, and ``test_crew_api_owner.py`` fails when a route and its description
disagree.

Public tier: ``/v1/models``, ``/v1/responses``, ``/v1/responses/{response_id}``,
``/v1/responses/{response_id}/input_items``, ``/v1/crew/capabilities`` and
``/v1/crew/info``, each served by its own handler behind the same ``owner_route``
gate. ``/v1/models`` also serves a verified app
credential with an empty list until a grant carries a Bot range or a route lets
an app run an agent. The owner gets the full catalog; the three ``/v1/responses``
routes serve the owner alone. ``/v1/crew/capabilities`` and ``/v1/crew/info`` serve
the machine owner and a verified app credential; the app's ``agents`` come from its
grant alone.
``/v1/crew/grant`` is not behind ``owner_route``: it serves an app credential its own
grant and answers a first-party session 404 (``crew_api_grant_self``).
``/v1/crew/bots`` serves a verified app credential the Bots of its grant and the
machine owner every Bot (``crew_api_bots``).
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import crew_api_approvals, crew_api_grant_requests, handlers
from kiro_crew.dashboard.crew_api_bots import api_bots
from kiro_crew.dashboard.crew_api_grant_self import api_grant_self
from kiro_crew.dashboard.crew_api_instance import api_capabilities, api_info
from kiro_crew.dashboard.crew_api_models import api_models
from kiro_crew.dashboard.crew_api_owner import owner_route
from kiro_crew.dashboard.crew_api_response_reads import (
    api_response_input_items,
    api_response_read,
)
from kiro_crew.dashboard.crew_api_response_records import STORE_KEY, ResponseStore
from kiro_crew.dashboard.crew_api_responses import api_responses


def register(app: web.Application) -> None:
    """Register the Crew API routes on *app*."""
    # Literal paths: test_dashboard_route_table.py reads each slice's paths from source.
    # Twin of GET /api/members (routes/agents.py); the Bot prune gate holds both.
    app.router.add_get(
        "/v1/crew/owner/bots", owner_route(handlers.api_members, "crew_api.bots.list")
    )
    app.router.add_get(
        "/v1/crew/owner/grant-requests",
        owner_route(crew_api_grant_requests.list_grant_requests, "crew_api.grant_requests.list"),
    )
    app.router.add_get(
        "/v1/crew/owner/grant-requests/{request_id}",
        owner_route(crew_api_grant_requests.get_grant_request, "crew_api.grant_requests.get"),
    )
    app.router.add_get(
        "/v1/crew/owner/grants",
        owner_route(crew_api_grant_requests.list_grants, "crew_api.grants.list"),
    )
    app.router.add_delete(
        "/v1/crew/owner/grants/{grant_id}",
        owner_route(crew_api_grant_requests.revoke_grant, "crew_api.grants.revoke"),
    )
    app.router.add_post(
        "/v1/crew/owner/grant-requests/{request_id}/decision",
        owner_route(crew_api_grant_requests.decide_grant_request, "crew_api.grant_requests.decide"),
    )
    app.router.add_get(
        "/v1/crew/owner/approvals",
        owner_route(crew_api_approvals.list_approvals, "crew_api.approvals.list"),
    )
    app.router.add_get(
        "/v1/crew/owner/approvals/{approval_id}",
        owner_route(crew_api_approvals.read_approval, "crew_api.approvals.read"),
    )
    app.router.add_post(
        "/v1/crew/owner/approvals/{approval_id}/decision",
        owner_route(crew_api_approvals.decide_approval, "crew_api.approvals.decide"),
    )
    app.router.add_get("/v1/models", api_models)
    app.router.add_post("/v1/responses", api_responses)
    # One store for the three response routes: the records and the running turns.
    app[STORE_KEY] = ResponseStore()
    app.router.add_get("/v1/responses/{response_id}", api_response_read)
    app.router.add_get("/v1/responses/{response_id}/input_items", api_response_input_items)
    app.router.add_get("/v1/crew/capabilities", api_capabilities)
    app.router.add_get("/v1/crew/info", api_info)
    app.router.add_get("/v1/crew/grant", api_grant_self)
    app.router.add_get("/v1/crew/bots", api_bots)
