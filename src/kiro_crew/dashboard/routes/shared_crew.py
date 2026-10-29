"""Route registration for Shared Crew (``kiro_crew.shared_crew``).

Each Shared Crew lane keeps its handlers in a ``routes_*`` module beside its
behaviour. This slice registers those handlers after every existing slice, so no
Shared Crew path can shadow an existing route. The paths are written out here as
literals so the route-table pins can read them from source;
``test_dashboard_route_table.py`` checks they match each module's own
``register``.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.shared_crew import routes_admission, routes_capabilities, routes_collab


def register(app: web.Application) -> None:
    """Register the Shared Crew routes on *app*."""
    app.router.add_get("/api/shared-crew/capacity", routes_admission.api_capacity)
    app.router.add_get("/api/shared-crew/work", routes_admission.api_work)
    app.router.add_post("/api/shared-crew/work/{work_id}/cancel", routes_admission.api_work_cancel)
    app.router.add_get("/api/shared-crew/presence", routes_collab.api_shared_crew_presence)
    app.router.add_get("/api/shared-crew/decisions", routes_collab.api_shared_crew_decisions)
    app.router.add_get("/api/shared-crew/send-state", routes_collab.api_shared_crew_send_state)
    app.router.add_get("/api/shared-crew/typing-lease", routes_collab.api_shared_crew_typing_lease)
    app.router.add_get(
        "/api/shared-crew/capabilities", routes_capabilities.api_shared_crew_capabilities
    )
