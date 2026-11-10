"""``POST /api/app-tools/call`` -- hand one app agent tool call to the app that declares it.

A first-party app may declare MCP tools in its manifest (``agentTools``) and serve them
from its own process (``apps/app_agent_tools.py`` says which apps are read). The ``kirocrew-core`` MCP server lists those tools and posts each
call here; this handler is where the call is decided:

1. MACHINE-only. The route is in ``_STRICT_INTERNAL_API_PATHS`` and the handler
   re-asserts ``request["internal_auth"] is True``, because a ``local_only=False``
   deployment reclassifies strict paths as mixed.
2. The app is the one whose manifest declares ``tool``, never a name in the request, so
   a call can reach only the declaring app, and only through its declared route.
3. The arguments must already be exactly what the declared schema's validation returns
   (the MCP server ran the same check), so an argument the declaration does not allow
   never reaches the app, and the bytes forwarded are the bytes checked.
4. The app must be enabled.
5. A tool declared ``strictSession`` must arrive with an ``X-Session-Key``: the MCP
   server sends the strictly resolved key for one and posts nothing without it, so a
   call without the header did not come through that gate.

Then the call goes where the app's code runs, through the dispatcher that already
serves that app, so no new trust path exists: the app's own gateway route when it
registered one at the declared route through the route registry
(``/api/apps/<app>/<route>``), else its backend process through the signed reverse
proxy (``/apps/<app>/api/<route>``, principal ``agent-tool``). An app that has a
backend process AND gateway routes thereby serves a tool that needs the gateway's host
namespaces (the systemd user bus, a mint a sandboxed process is refused) from the
gateway, as the core tool it replaces did. Both
forward the body this request already holds (the proxy is handed the bytes read here); the app's answer is relayed unchanged, and
the MCP server redacts and caps it.

Body: ``{"tool": str, "arguments": object}``. Refusals: 403 ``loopback_only``,
``app_not_enabled`` and ``session_required``, 404 ``unknown_tool``, 400 ``invalid_json``
and ``invalid_arguments``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps import app_agent_tools
from kiro_crew.apps.app_frame import process_backed
from kiro_crew.apps.manager import get_app_manifest, is_app_enabled
from kiro_crew.sel import sel
from kiro_crew.validation_parts.field_engine import ValidationError, validate_tool_args

logger = logging.getLogger(__name__)


def _audit(outcome: str, resource: str, error: str = "") -> None:
    try:
        sel().log_api_access(
            caller="kirocrew-core",
            operation="app_agent_tool_call",
            outcome=outcome,
            source="app_routes",
            resources=resource,
            error=error,
        )
    except Exception:  # noqa: BLE001 - auditing never fails the request
        logger.debug("app agent tool audit failed", exc_info=True)


def _refuse(status: int, code: str, error: str, resource: str) -> web.Response:
    _audit("denied", resource, error)
    return web.json_response({"code": code, "error": error}, status=status)


def _resolve(name: str) -> tuple[app_agent_tools.DeclaredTool | None, bool, bool]:
    """``(tool, enabled, process_backed)``, read off the event loop."""
    tool = app_agent_tools.declared_tool(name)
    if tool is None:
        return None, False, False
    manifest = get_app_manifest(tool.app)
    return tool, is_app_enabled(tool.app), manifest is not None and process_backed(manifest)


async def api_app_agent_tool_call(request: web.Request) -> web.StreamResponse:
    """POST /api/app-tools/call -- see the module docstring."""
    if request.get("internal_auth") is not True:
        return _refuse(403, "loopback_only", "internal-secret authentication required", "")
    try:
        body: Any = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or set(body) != {"tool", "arguments"}:
        return _refuse(400, "invalid_json", "body must be {tool, arguments}", "")
    name, args = body["tool"], body["arguments"]
    if not isinstance(name, str) or not isinstance(args, dict):
        return _refuse(400, "invalid_json", "tool must be a string, arguments an object", "")
    tool, enabled, backed = await asyncio.to_thread(_resolve, name)
    if tool is None:
        return _refuse(404, "unknown_tool", f"no installed app declares {name!r}", name)
    resource = f"{tool.app}:{name}"
    try:
        cleaned = validate_tool_args(
            json.loads(json.dumps(args)), app_agent_tools.build_tool_schema(tool)
        )
    except ValidationError as exc:
        return _refuse(400, "invalid_arguments", f"{exc.field}: {exc.message}", resource)
    if cleaned != args:
        return _refuse(400, "invalid_arguments", "arguments are not in validated form", resource)
    if not enabled:
        return _refuse(403, "app_not_enabled", f"app {tool.app!r} is not enabled", resource)
    if tool.decl.strict_session and not request.headers.get("X-Session-Key", "").strip():
        return _refuse(
            403, "session_required", f"{name!r} needs the calling session's key", resource
        )
    _audit("granted", resource)
    from kiro_crew.apps.hooks_integration import get_route_registry

    registry = get_route_registry()
    in_gateway = registry is not None and registry.serves(tool.app, "POST", tool.route)
    if backed and not in_gateway:
        from kiro_crew.apps import routes as app_routes

        request.match_info["name"] = tool.app
        request.match_info["path"] = tool.route
        # The body was read above, so the proxy is handed the same bytes: it would
        # otherwise see a payload at EOF and forward none.
        return await app_routes.handle_app_api_proxy(request, read_body=await request.read())
    if registry is None:
        return _refuse(503, "app_routes_unavailable", "app routes are not served here", resource)
    # The registry's own dispatch, never the router: only a route the app registered
    # can answer, never a core route under /api/apps/<app>/.
    request.match_info["app_name"] = tool.app
    request.match_info["path"] = tool.route
    return await registry.dispatch(request)
