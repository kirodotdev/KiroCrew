"""Owner-only capability editor; all disk work runs outside the event loop."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from aiohttp import web

from kiro_crew.agent_capabilities import CapabilityError, CapabilityService
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_utils import drained_to_thread
from kiro_crew.dashboard.handlers._shared import (
    enumerate_skill_catalog,
    require_owner_dashboard_request,
    skill_uri_for_key,
)
from kiro_crew.dashboard.state import DashboardState

_SERVICE = web.AppKey("member_capability_service", CapabilityService)


def _catalog(project: str) -> dict[str, str]:
    # The catalog's project is the member's cwd, not an unrelated open chat.
    view = cast(
        DashboardState, SimpleNamespace(_slots={"member": SimpleNamespace(project=project)})
    )
    catalog = enumerate_skill_catalog(view, "member")
    return {key: uri for key in catalog if (uri := skill_uri_for_key(key, view, catalog))}


def _connections() -> dict[str, dict]:
    from kiro_crew.mcp_discovery import (
        _load_agent_config,
        _load_mcp_json_by_source,
        _scope_priority,
    )
    from kiro_crew.mcp_utils import kiro_oauth_wire_entry

    # Select one whole configured definition. Do not reconstruct a transport
    # from display metadata, which omits timeout and native OAuth options.
    sources = _load_mcp_json_by_source()
    result = dict(_load_agent_config().get("mcpServers", {}))
    for scope in _scope_priority(sources):
        for name, spec in sources.get(scope, {}).items():
            result.setdefault(name, spec)
    return {
        name: kiro_oauth_wire_entry(spec, store_entry=None, server=name)
        for name, spec in result.items()
        if isinstance(spec, dict)
    }


async def api_member_capabilities(request: web.Request) -> web.Response:
    operation = "member_capabilities." + request.method.lower()
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    service = request.app.get(_SERVICE)
    if service is None:
        service = CapabilityService(_catalog, _connections)
        request.app[_SERVICE] = service
    name = request.match_info["name"]
    try:
        if request.method == "GET":
            result = await drained_to_thread(service.get, name)
        else:
            try:
                body = await request.json()
            except ValueError:
                raise CapabilityError("invalid_json", 400) from None
            if request.method == "POST":
                result = await drained_to_thread(service.preview, name, body)
            else:
                from kiro_crew.dashboard.handlers.agents import _get_config_lock

                async with _get_config_lock():
                    result = await drained_to_thread(service.put, name, body)
                request.app["state"].push_refresh("agents")
                from kiro_crew.sel import sel

                sel().log_api_access(
                    caller="owner",
                    operation=operation,
                    outcome="success",
                    source="dashboard",
                    resources="member_capabilities",
                )
        # A preview is proposed state, not a runtime adoption receipt. Disk
        # validation failures must also survive a still-live older provider.
        manager = getattr(request.app.get("state"), "sessions", None)
        if (
            request.method != "POST"
            and manager is not None
            and result["mode"] == "inherited"
            and result["runtime"]["status"] != "failed"
        ):
            result["runtime"] = manager.capability_runtime_view(
                name, result["runtime"]["saved_revision"]
            )
        return web.json_response(result)
    except CapabilityError as exc:
        return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)
    except (OSError, ValueError):
        # Source/sidecar/parser errors may quote secret-bearing bytes or paths.
        return web.json_response(
            {"error": "capabilities_unavailable", "code": "capabilities_unavailable"}, status=503
        )


async def inherited_template_action(
    request: web.Request, member: str, action: str, publish_name: str = ""
) -> web.Response | None:
    """Adapt owner-authorized legacy buttons without discarding inheritance intent."""
    from kiro_crew import agent_state
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    target = request.match_info["name"]
    try:
        inherited = await drained_to_thread(agent_state.get_capabilities, target)
        publication = (
            await drained_to_thread(agent_state.get_publish_info, publish_name)
            if action == "publish"
            else None
        )
        if inherited is None and publication is None:
            return None
        service = request.app.get(_SERVICE)
        if service is None:
            service = CapabilityService(_catalog, _connections)
            request.app[_SERVICE] = service
        async with _get_config_lock():
            if action == "reset":
                await drained_to_thread(service.reset, member, target)
                prepared = await drained_to_thread(KiroCrewConfig.load)
                result = {"ok": True, "template": prepared.agents[member].kiro_agent}
            else:
                result = await drained_to_thread(service.publish, member, target, publish_name)
        request.app["state"].push_refresh("agents")
        return web.json_response(result)
    except CapabilityError as exc:
        return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)
    except (OSError, ValueError):
        return web.json_response(
            {"error": "capabilities_unavailable", "code": "capabilities_unavailable"}, status=503
        )
