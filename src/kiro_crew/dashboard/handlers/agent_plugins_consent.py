"""Agent Plugins REST API: the owner's list and on/off switch.

``GET  /api/agent-plugins``  every installed plugin with its state, servers, skills, issues
``POST /api/agent-plugins``  ``{"name": ..., "enabled": bool}`` turns one plugin on or off

This handler is the only writer of ``agent-plugins.json``
(:mod:`kiro_crew.agent_plugins.consent`), and being the only writer is what keeps
"the agent cannot turn a plugin on" true: the record is sealed read-only in the
sandbox and fenced from the agent's file tools, and there is no MCP tool and no
CLI verb that writes it. Both routes refuse anyone but the dashboard owner, by
the rule ``aws_consent`` uses (:func:`is_owner_dashboard_request`): an app token
or an allowed messaging user is not the owner.

Consent is to a plugin by name: an update to an enabled plugin keeps it on. A
plugin that is not valid cannot be turned on. A change reaches sessions at their
next spec rebuild.

Discovery reads the disk, so it runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.agent_plugins import consent, discover_plugins
from kiro_crew.agent_plugins.model import PluginRecord, RemoteServer, StdioServer
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_INVALID_JSON = "invalid_json"
_CODE_INVALID_REQUEST = "agent_plugin_invalid_request"
_CODE_NOT_FOUND = "agent_plugin_not_found"
_CODE_NOT_ENABLEABLE = "agent_plugin_not_enableable"


def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    if is_owner_dashboard_request(request):
        return None
    logger.warning(
        "refused %s: turning Agent Plugins on or off is a dashboard owner action (app=%s)",
        operation,
        request.get("app"),
    )
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


def _server_payload(server: StdioServer | RemoteServer) -> dict[str, Any]:
    if isinstance(server, StdioServer):
        return {"name": server.name, "type": "stdio", "command": server.command}
    return {"name": server.name, "type": server.type, "url": server.url}


def _plugin_payload(rec: PluginRecord) -> dict[str, Any]:
    manifest = rec.manifest
    return {
        "name": rec.name,
        "state": rec.state,
        "reason": rec.reason,
        "version": manifest.version if manifest else None,
        "description": manifest.description if manifest else None,
        "servers": [_server_payload(s) for s in rec.servers],
        "skills": [{"name": s.name, "description": s.description} for s in rec.skills],
        "issues": [
            {"component": i.component, "entry": i.entry, "message": i.message} for i in rec.issues
        ],
    }


async def api_agent_plugins_get(request: web.Request) -> web.Response:
    """GET /api/agent-plugins: every installed plugin, as discovery sees it."""
    denied = _deny_non_owner(request, "agent_plugins.list")
    if denied:
        return denied
    records = await asyncio.to_thread(discover_plugins)
    return web.json_response({"plugins": [_plugin_payload(r) for r in records]})


def _audit(name: str, outcome: str) -> None:
    """Record the owner's switch in the Security Event Log. Never raises."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="operator",
            operation=f"agent_plugins.{outcome}",
            outcome=outcome,
            source="agent-plugins",
            resources=name,
        )
    except Exception:  # noqa: BLE001 — the audit must not undo the owner's choice
        logger.debug("could not write the Agent Plugins audit event", exc_info=True)


def _switch(name: str, on: bool) -> tuple[int, dict[str, Any]]:
    """Apply the owner's choice for *name*. Runs off the event loop."""
    rec = next((r for r in discover_plugins() if r.name == name), None)
    if not on:
        consent.set_enabled(name, False)
        _audit(name, "disabled")
        state = None if rec is None else ("off" if rec.state == "on" else rec.state)
        return 200, {"ok": True, "name": name, "state": state}
    if rec is None:
        return 404, {"error": "no installed plugin has that name", "code": _CODE_NOT_FOUND}
    if rec.state not in ("off", "on"):
        return 409, {
            "error": f"the plugin is {rec.state} and cannot be turned on",
            "code": _CODE_NOT_ENABLEABLE,
        }
    consent.set_enabled(name, True)
    _audit(name, "enabled")
    return 200, {"ok": True, "name": name, "state": "on"}


async def api_agent_plugins_post(request: web.Request) -> web.Response:
    """POST /api/agent-plugins: turn one plugin on or off."""
    denied = _deny_non_owner(request, "agent_plugins.switch")
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — any unparseable body is the same refusal
        return web.json_response(
            {"error": "the body is not valid JSON", "code": _CODE_INVALID_JSON}, status=400
        )
    name = body.get("name") if isinstance(body, dict) else None
    on = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name or not isinstance(on, bool):
        return web.json_response(
            {
                "error": "send a plugin name and enabled true or false",
                "code": _CODE_INVALID_REQUEST,
            },
            status=400,
        )
    status, payload = await asyncio.to_thread(_switch, name, on)
    return web.json_response(payload, status=status)
