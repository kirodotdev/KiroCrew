"""Agent Plugins REST API, read-only: the owner's list of installed plugins.

``GET /api/agent-plugins``  every installed plugin and Kiro Power, with its state,
the reason it is not usable, what it would contribute, and every reported issue;
and, when ``installed.json`` could not be read, why

This is the read-only half of the RFC's dashboard list. Nothing here writes a
file, starts a process or turns a plugin on, and no plugin reaches a session, so
every record says ``loaded: false``. The on/off switch is a separate, owner-only
write that this module does not offer (``docs/system-specs/modules/agent-plugins.md``
§6 says what is held and why).

Owner-only, by the rule ``aws_consent`` uses (:func:`is_owner_dashboard_request`):
the list names each stdio server's command and every problem found in a plugin's
files, which an app token or an allowed messaging user has no reason to read.
Every refusal is written to the security event log, as every other owner gate's is.

Discovery reads the disk, so it runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.agent_plugins.discovery import LEGACY_POWER_REASON, discover
from kiro_crew.agent_plugins.model import PluginRecord, RemoteServer, StdioServer
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_OPERATION = "agent_plugins.list"


def _sel():
    """Late-binding ``sel()``: the package import is the circular-import exception
    every sibling handler uses, and resolving per call lets tests monkeypatch it."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 -- circular import

    return _pkg.sel()


async def _audit_refusal(request: web.Request) -> None:
    """Record a refused read in the SEL; an audit failure never changes the 403.

    Off the event loop only when it could block, the gate ``decisions._audit``
    uses: once the boot-time warm succeeded ``sel()`` is an attribute read and the
    write an enqueue, but a failed warm makes the next call retry the log's
    initialisation on the caller's thread.
    """
    from kiro_crew.sel import sel_is_warm

    def _write() -> None:
        _sel().log_api_access(
            caller=str(request.get("user") or "anonymous"),
            operation=_OPERATION,
            outcome="denied",
            source="dashboard",
            resources="/api/agent-plugins",
            error=f"non-owner caller refused (app={request.get('app')!r})",
        )

    try:
        if sel_is_warm():
            _write()
        else:
            await asyncio.to_thread(_write)
    except Exception:
        logger.warning("SEL logging failed for %s", _OPERATION, exc_info=True)


def _server_payload(server: StdioServer | RemoteServer) -> dict[str, Any]:
    if isinstance(server, StdioServer):
        return {"name": server.name, "type": "stdio", "command": server.command}
    return {"name": server.name, "type": server.type, "url": server.url}


def plugin_payload(rec: PluginRecord) -> dict[str, Any]:
    """One record as the list shows it. ``loaded`` is false for every record.

    ``failed`` marks a ``reason`` or an issue ``message`` that reports a read that
    failed on this host rather than a rule the package breaks, so the dashboard
    can tell the two apart without reading the English.
    """
    manifest = rec.manifest
    return {
        "name": rec.name,
        "state": rec.state,
        "reason": rec.reason,
        "failed": rec.failed,
        "legacy_power": rec.state == "not-a-plugin" and rec.reason == LEGACY_POWER_REASON,
        "loaded": False,
        "version": manifest.version if manifest else None,
        "description": manifest.description if manifest else None,
        "servers": [_server_payload(s) for s in rec.servers],
        "skills": [{"name": s.name, "description": s.description} for s in rec.skills],
        "issues": [
            {"component": i.component, "entry": i.entry, "message": i.message, "failed": i.failed}
            for i in rec.issues
        ],
    }


async def api_agent_plugins_get(request: web.Request) -> web.Response:
    """GET /api/agent-plugins: every installed plugin, as discovery sees it."""
    if not is_owner_dashboard_request(request):
        logger.warning(
            "refused %s: the Agent Plugins list is a dashboard owner view (app=%s)",
            _OPERATION,
            request.get("app"),
        )
        await _audit_refusal(request)
        return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)
    found = await asyncio.to_thread(discover)
    return web.json_response(
        {"plugins": [plugin_payload(r) for r in found.plugins], "index_error": found.index_error}
    )
