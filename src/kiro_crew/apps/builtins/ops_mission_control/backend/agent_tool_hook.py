"""The route-registry half of Ops Mission Control: the route its ``agentTools`` block names.

``app.json`` declares the ``ops_mission_control_api`` agent tool with the route
``agent-tools``. The gateway's ``POST /api/app-tools/call`` hands an in-gateway app's tool
call to the route registry (``/api/apps/<app>/<route>``), never to the gateway router,
so a route this built-in mounts on the router (``routes.register_routes``) is not reached
that way. ``backend.hooks.routes`` in ``app.json`` names :func:`register_agent_tool_routes`,
which registers that one route there.

The gateway loads this file under the app's own module namespace, so it holds nothing of
its own: the handler resolves ``routes`` from the package on every call, the module every
other route of the app, and every test patch, already uses.
"""

from __future__ import annotations

from typing import Any, cast

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import routes
from kiro_crew.apps.route_registry import AppRoute

#: The registry path the manifest's ``agentTools.route`` names, below the app's API base.
AGENT_TOOL_PATH = "/agent-tools"


async def _agent_tool(request: web.Request, _ctx: Any) -> web.Response:
    """The registry's ``(request, ctx)`` call of the gated ``routes._handle_agent_tool``.

    Every answer the route gives is a ``web.json_response``, the type the registry declares.
    """
    return cast(web.Response, await routes._require_enabled(routes._handle_agent_tool)(request))


def register_agent_tool_routes(_ctx: Any) -> list[AppRoute]:
    """``backend.hooks.routes``: the one route the registry serves for this app."""
    return [AppRoute("POST", AGENT_TOOL_PATH, _agent_tool)]
