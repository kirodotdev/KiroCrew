"""Owner-only capability editor; all disk work runs outside the event loop."""

from __future__ import annotations

import logging
from collections.abc import Collection
from types import SimpleNamespace
from typing import cast

from aiohttp import web

from kiro_crew.agent_capabilities import CapabilityError, CapabilityService
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.agent_name_segment import agent_route_name, invalid_agent_name_response
from kiro_crew.dashboard.chat_utils import drained_to_thread
from kiro_crew.dashboard.handlers._bounded_read import TIMED_OUT, bounded_read
from kiro_crew.dashboard.handlers._shared import (
    enumerate_skill_catalog,
    require_owner_dashboard_request,
    skill_uri_for_key,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.mcp_gateway.secret_uri import routed_for_secret_reference, secret_reference_keys

logger = logging.getLogger(__name__)

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


def _routes(name: str, entry: object, routed: Collection[str]) -> bool:
    """Whether the overlay rewriter wraps this spec server for *routed* (its own rule)."""
    env = entry.get("env") if isinstance(entry, dict) else None
    return name in routed or bool(
        secret_reference_keys(env) and routed_for_secret_reference(name, routed)
    )


def _put_routed(service: CapabilityService, name: str, body: object) -> tuple[dict, dict]:
    """Save, and name the gateway-routed servers it gave a Bot's own file.

    Routed: a ``set`` of an ``mcpServers`` entry the current
    ``mcp_gateway.stub_servers`` routes (``_routes``), on a Bot that owns its
    file. Only such a save changes what the Bot's overlay must carry. Answers
    each routed id with its saved entry. Blocking.
    """
    from kiro_crew.mate_capabilities import _load_spec

    result = service.put(name, body)
    if result.get("mode") != "mate" or not isinstance(body, dict):
        return result, {}
    ids: set[str] = {
        op["id"]
        for op in body.get("operations") or []
        if isinstance(op, dict)
        and isinstance(op.get("id"), str)
        and op.get("section") == "mcpServers"
        and op.get("action") == "set"
    }
    if not ids:
        return result, {}
    try:
        stubs = frozenset(KiroCrewConfig.load().mcp_gateway.stub_servers)
        servers = _load_spec(name)[1].get("mcpServers") or {}
    except Exception:  # noqa: BLE001 - the save is done; it only skips the refresh
        logger.warning("Could not read the routing after a Bot save", exc_info=True)
        return result, {}
    return result, {i: servers.get(i) for i in ids if _routes(i, servers.get(i), stubs)}


async def _put_and_refresh_overlays(service: CapabilityService, name: str, body: object) -> dict:
    """Save under the overlay refresh, so the Bot's worker gets its new routed stub.

    The write runs as the refresh's ``register`` step, under the broker's
    refresh lock: a write landing between the refresh's probe and its publish
    would publish a stub for a target the daemon does not hold. With no broker
    (or a refresh that failed before registering) the save runs here instead.
    A routed server the refresh did not publish as a stub (it asked for a
    restart, raised, found no broker, or the running broker does not serve that
    server yet) is surfaced as ``gateway_restart_required`` for the change card.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock
    from kiro_crew.mcp_gateway.overlay_refresh import refresh_after_agent_change

    box: list = []

    async def _register() -> bool:
        try:
            async with _get_config_lock():
                box.append(await drained_to_thread(_put_routed, service, name, body))
        except Exception as exc:  # noqa: BLE001 - re-raised below, after the refresh
            # A refused save wrote nothing, so there is nothing to rewrite.
            box.append(exc)
            return False
        except BaseException as exc:
            box.append(exc)
            raise
        return bool(box[0][1])

    refreshed = await refresh_after_agent_change(_register)
    if not box:  # no broker serves, or the refresh failed before registering
        async with _get_config_lock():
            box.append(await drained_to_thread(_put_routed, service, name, body))
    if isinstance(box[0], BaseException):
        raise box[0]
    result, routed = box[0]
    applied = isinstance(refreshed, dict) and refreshed.get("applied") is True
    served = refreshed.get("serving_stub_servers", ()) if applied else ()
    if any(not _routes(i, entry, served) for i, entry in routed.items()):
        result["gateway_restart_required"] = True
    return result


async def api_member_capabilities(request: web.Request) -> web.Response:
    operation = "member_capabilities." + request.method.lower()
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    service = request.app.get(_SERVICE)
    if service is None:
        service = CapabilityService(_catalog, _connections)
        request.app[_SERVICE] = service
    name = agent_route_name(request)
    if name is None:
        return invalid_agent_name_response()
    try:
        if request.method == "GET":
            # Not ``drained_to_thread``: a GET mutates nothing, so a read still
            # blocked at the deadline is left to its thread instead of holding
            # the request open until the mount answers.
            result = await bounded_read(service.get, name, what="Capabilities")
        else:
            try:
                body = await request.json()
            except ValueError:
                raise CapabilityError("invalid_json", 400) from None
            if request.method == "POST":
                result = await drained_to_thread(service.preview, name, body)
            else:
                result = await _put_and_refresh_overlays(service, name, body)
                request.app["state"].push_refresh("agents")
                if result.get("mode") == "mate":
                    # A crewmate that owns its file: its running workers mount
                    # what it approves, so they are reloaded onto the new file
                    # (``crewmate_worker_reload``; a busy one at its turn end).
                    from kiro_crew.dashboard.crewmate_worker_reload import (
                        schedule_reload_workers,
                    )

                    schedule_reload_workers(request.app["state"], name)
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
        if exc.file:
            # A basename only, never a directory: the member's own agent file,
            # or the project file that shadows it. The pane points at the file
            # a refusal is about. Literal bodies keep `code` visible
            # to the error-code contract scan.
            return web.json_response(
                {"error": exc.code, "code": exc.code, "file": exc.file},
                status=exc.status,
            )
        if exc.ref:
            # The owner's own draft reference, never file bytes: the pane names
            # the entry instead of re-deriving the shape rule client-side.
            return web.json_response(
                {"error": exc.code, "code": exc.code, "ref": exc.ref}, status=exc.status
            )
        return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)
    except TimeoutError:
        # Before OSError, which TimeoutError is: the pane says why for this one.
        return web.json_response(
            {
                "error": "capabilities_unavailable",
                "code": "capabilities_unavailable",
                "reason": TIMED_OUT,
            },
            status=503,
        )
    except (OSError, ValueError) as exc:
        # Source/sidecar/parser errors may quote secret-bearing bytes or paths,
        # so the log names the error's type only.
        logger.warning("Capabilities %s for Bot %r failed: %s", operation, name, type(exc).__name__)
        return web.json_response(
            {"error": "capabilities_unavailable", "code": "capabilities_unavailable"}, status=503
        )


async def api_member_capabilities_detach(request: web.Request) -> web.Response:
    """Stop following the Parent; the member keeps its saved values as its own."""
    operation = "member_capabilities.detach"
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    service = request.app.get(_SERVICE)
    if service is None:
        service = CapabilityService(_catalog, _connections)
        request.app[_SERVICE] = service
    name = agent_route_name(request)
    if name is None:
        return invalid_agent_name_response()
    try:
        try:
            body = await request.json()
        except ValueError:
            raise CapabilityError("invalid_json", 400) from None
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        async with _get_config_lock():
            result = await drained_to_thread(service.detach, name, body)
        request.app["state"].push_refresh("agents")
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="owner",
            operation=operation,
            outcome="success",
            source="dashboard",
            resources="member_capabilities",
        )
        return web.json_response(result)
    except CapabilityError as exc:
        return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)
    except (OSError, ValueError):
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
