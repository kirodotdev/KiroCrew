"""``POST|DELETE /api/connection-proxy``: the Crew-owned Connection proxy route.

A strict internal path (``_STRICT_INTERNAL_API_PATHS``) bound through
``mcp_routes._deferred``, so the gateway boot path never imports the proxy. The
handler re-asserts internal authority and the Unix-socket transport itself; see
:mod:`kiro_crew.connections.crew_oauth_proxy`.
"""

from __future__ import annotations

from aiohttp import web


async def api_connection_proxy(request: web.Request) -> web.StreamResponse:
    from kiro_crew.connections.crew_oauth_proxy import handle

    return await handle(request)
