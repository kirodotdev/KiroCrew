"""Route registration for the Crew API Owner tier (``/v1/crew/owner/*``).

Each route is served through :func:`kiro_crew.dashboard.crew_api_owner.owner_route`.
The Bot roster wraps the handler of an existing ``/api/*`` route, whose twin stays
registered for the clients that still call it. The grant-request routes have no twin:
they are the owner's side of local authorization (``crew_api_grant_requests``).

``test/fixtures/crew_api_owner_openapi.json`` describes every route registered
here, and ``test_crew_api_owner.py`` fails when a route and its description
disagree.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import crew_api_grant_requests, handlers
from kiro_crew.dashboard.crew_api_owner import owner_route


def register(app: web.Application) -> None:
    """Register the Crew API Owner routes on *app*."""
    # Literal paths: test_dashboard_route_table.py reads each slice's paths from source.
    # Twin of GET /api/members (routes/agents.py); the Bot prune gate holds both.
    app.router.add_get(
        "/v1/crew/owner/bots", owner_route(handlers.api_members, "crew_api.bots.list")
    )
    app.router.add_get(
        "/v1/crew/owner/grant-requests",
        owner_route(crew_api_grant_requests.list_grant_requests, "crew_api.grant_requests.list"),
    )
    app.router.add_post(
        "/v1/crew/owner/grant-requests/{request_id}/decision",
        owner_route(crew_api_grant_requests.decide_grant_request, "crew_api.grant_requests.decide"),
    )
