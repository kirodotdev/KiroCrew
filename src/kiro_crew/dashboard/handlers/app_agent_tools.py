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

``POST /api/app-tools/prompted-call`` is the same decision for the tools a third-party
app declares (``app_agent_tools.third_party_tools``), posted by the ``kirocrew-apps``
server, whose calls are approved one by one. Each route resolves only its own set, so
``kirocrew-core`` cannot reach a third-party tool and ``kirocrew-apps`` cannot reach a
first-party one, whatever name either posts. It also refuses (403 ``cross_app``) a
session that one app owns calling another app's tool, on both dispatch paths.
The prompted route also refuses ``approval_grants_undetermined`` unless KAS keeps
its per-call approval or a spec-file-grant backend launched the calling session on a
Crew-minted copy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from aiohttp import web

from kiro_crew.agent_sdk.prompted_calls import PromptedCallLaunch
from kiro_crew.apps import app_agent_tools
from kiro_crew.apps.app_frame import process_backed
from kiro_crew.apps.manager import get_app_manifest, is_app_enabled
from kiro_crew.sel import sel
from kiro_crew.validation_parts.field_engine import ValidationError, validate_tool_args

logger = logging.getLogger(__name__)

#: The server that posts to each route, as the audit names the caller.
_CORE_CALLER = "kirocrew-core"
_PROMPTED_CALLER = "kirocrew-apps"

Resolved = tuple["app_agent_tools.DeclaredTool | None", bool, bool]


def _audit(caller: str, outcome: str, resource: str, error: str = "") -> None:
    try:
        sel().log_api_access(
            caller=caller,
            operation="app_agent_tool_call",
            outcome=outcome,
            source="app_routes",
            resources=resource,
            error=error,
        )
    except Exception:  # noqa: BLE001 - auditing never fails the request
        logger.debug("app agent tool audit failed", exc_info=True)


def _refuse(caller: str, status: int, code: str, error: str, resource: str) -> web.Response:
    _audit(caller, "denied", resource, error)
    return web.json_response({"code": code, "error": error}, status=status)


def _resolved(tool: app_agent_tools.DeclaredTool | None) -> Resolved:
    if tool is None:
        return None, False, False
    manifest = get_app_manifest(tool.app)
    return tool, is_app_enabled(tool.app), manifest is not None and process_backed(manifest)


def _resolve(name: str) -> Resolved:
    """``(tool, enabled, process_backed)`` of a first-party tool, read off the event loop."""
    return _resolved(app_agent_tools.declared_tool(name))


def _resolve_third_party(name: str) -> Resolved:
    """``(tool, enabled, process_backed)`` of a third-party tool, read off the event loop."""
    return _resolved(app_agent_tools.third_party_tool(name))


async def api_app_agent_tool_call(request: web.Request) -> web.StreamResponse:
    """POST /api/app-tools/call -- a first-party app's tool; see the module docstring."""
    # Looked up per call, never bound at import, so the module function stays the seam.
    return await _decide(request, _CORE_CALLER, lambda n: _resolve(n))


async def api_prompted_app_tool_call(request: web.Request) -> web.StreamResponse:
    """POST /api/app-tools/prompted-call -- a third-party app's tool; see the module docstring."""
    return await _decide(
        request, _PROMPTED_CALLER, lambda n: _resolve_third_party(n), own_app_only=True
    )


def _prompted_call_launch(request: web.Request) -> PromptedCallLaunch | None:
    """Return the authenticated calling session's recorded launch identity."""
    key = request.headers.get("X-Session-Key", "").strip()
    state = request.app.get("state")
    if not key or state is None:
        return None
    try:
        provider = state.sessions.get_provider(key)
        if provider is None:
            subagents = getattr(state, "subagents", None)
            if subagents is not None:
                provider = subagents.get_live_shared_provider(key)
        return provider.prompted_call_launch() if provider is not None else None
    except Exception:  # noqa: BLE001 -- an unknown launch must be refused below
        logger.warning(
            "prompted app call: calling session launch could not be resolved",
            exc_info=True,
        )
        return None


def _prompted_call_grant_refusal(request: web.Request, resource: str) -> web.Response | None:
    """Refuse dispatch unless KAS or a Crew-copied spec-file harness keeps per-call approval."""
    launch = _prompted_call_launch(request)
    if launch is None:
        return _refuse(
            _PROMPTED_CALLER,
            403,
            "approval_grants_undetermined",
            "the calling session's launch grants could not be verified",
            resource,
        )
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKENDS_SPEC_FILE_GRANTS

    if launch.backend == ACP_BACKEND_KAS:
        return None
    if launch.backend not in ACP_BACKENDS_SPEC_FILE_GRANTS:
        return _refuse(
            _PROMPTED_CALLER,
            403,
            "approval_grants_undetermined",
            f"the {launch.backend} harness can pre-approve this server from its own settings, "
            "so the per-call approval cannot be verified",
            resource,
        )
    if launch.launched_on_crew_copy:
        return None
    return _refuse(
        _PROMPTED_CALLER,
        403,
        "approval_grants_undetermined",
        "kiro-cli did not launch this session on a Kiro Crew copy of its agent spec; "
        "agent.native_skill_projection may be off, or the skill view could not be "
        "prepared when the session started",
        resource,
    )


async def _decide(
    request: web.Request,
    caller: str,
    resolve: Callable[[str], Resolved],
    *,
    own_app_only: bool = False,
) -> web.StreamResponse:
    if request.get("internal_auth") is not True:
        return _refuse(caller, 403, "loopback_only", "internal-secret authentication required", "")
    try:
        body: Any = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or set(body) != {"tool", "arguments"}:
        return _refuse(caller, 400, "invalid_json", "body must be {tool, arguments}", "")
    name, args = body["tool"], body["arguments"]
    if not isinstance(name, str) or not isinstance(args, dict):
        return _refuse(
            caller, 400, "invalid_json", "tool must be a string, arguments an object", ""
        )
    tool, enabled, backed = await asyncio.to_thread(resolve, name)
    if tool is None:
        return _refuse(caller, 404, "unknown_tool", f"no installed app declares {name!r}", name)
    resource = f"{tool.app}:{name}"
    try:
        cleaned = validate_tool_args(
            json.loads(json.dumps(args)), app_agent_tools.build_tool_schema(tool)
        )
    except ValidationError as exc:
        return _refuse(caller, 400, "invalid_arguments", f"{exc.field}: {exc.message}", resource)
    if cleaned != args:
        return _refuse(
            caller, 400, "invalid_arguments", "arguments are not in validated form", resource
        )
    if not enabled:
        return _refuse(caller, 403, "app_not_enabled", f"app {tool.app!r} is not enabled", resource)
    calling_app = request.get("app") or ""
    if own_app_only and calling_app and calling_app != tool.app:
        # token_auth names the app that owns the calling session. The signed proxy
        # refuses a cross-app call on its own, but the route registry does not, so
        # the refusal is made here for both.
        return _refuse(
            caller,
            403,
            "cross_app",
            f"a session app {calling_app!r} owns may not call app {tool.app!r}",
            resource,
        )
    if caller == _PROMPTED_CALLER:
        grant_refusal = _prompted_call_grant_refusal(request, resource)
        if grant_refusal is not None:
            return grant_refusal
    if tool.decl.strict_session and not request.headers.get("X-Session-Key", "").strip():
        return _refuse(
            caller, 403, "session_required", f"{name!r} needs the calling session's key", resource
        )
    _audit(caller, "granted", resource)
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
        return _refuse(
            caller, 503, "app_routes_unavailable", "app routes are not served here", resource
        )
    # The registry's own dispatch, never the router: only a route the app registered
    # can answer, never a core route under /api/apps/<app>/.
    request.match_info["app_name"] = tool.app
    request.match_info["path"] = tool.route
    return await registry.dispatch(request)
