"""Route registration for the Crew API Owner tier (``/v1/crew/owner/*``).

Each route wraps the handler of an existing ``/api/*`` route with
:func:`kiro_crew.dashboard.crew_api_owner.owner_route`. The ``/api/*`` twin stays
registered for the clients that still call it.

``test/fixtures/crew_api_owner_openapi.json`` describes every route registered
here, and ``test_crew_api_owner.py`` fails when a route and its description
disagree.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.crew_api_owner import owner_route


def register(app: web.Application) -> None:
    """Register the Crew API Owner routes on *app*."""
    # Literal paths: test_dashboard_route_table.py reads each slice's paths from source.
    # Twin of GET /api/members (routes/agents.py); the Bot prune gate holds both.
    app.router.add_get(
        "/v1/crew/owner/members", owner_route(handlers.api_members, "crew_api.members.list")
    )
