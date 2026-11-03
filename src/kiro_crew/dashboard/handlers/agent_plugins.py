"""Agent Plugins install REST API, for the dashboard owner only.

``POST /api/agent-plugins/install``    install from ``{"path": ...}`` or ``{"url": ...}``
``POST /api/agent-plugins/uninstall``  remove ``{"name": ...}``

Installing writes into the powers home Kiro IDE and kiro-cli share, so it is an
owner action like the AWS consent surface: an app token or an allow-listed
messaging user is refused, and there is no MCP tool or CLI verb for it. A
plugin installed here lands off. The work touches the filesystem and may run
``git clone``, so it runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from kiro_crew.agent_plugins import install as install_mod
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_INVALID_JSON = "invalid_json"
_CODE_BAD_REQUEST = "agent_plugin_bad_request"

#: Refusal code -> HTTP status; any other refusal is a 400.
_STATUS_FOR = {
    install_mod.CODE_ALREADY_INSTALLED: 409,
    install_mod.CODE_NOT_INSTALLED: 404,
    install_mod.CODE_INDEX_UNREADABLE: 409,
    install_mod.CODE_CLONE_FAILED: 502,
}


async def _body(request: web.Request, operation: str) -> dict[str, Any] | web.Response:
    if not is_owner_dashboard_request(request):
        logger.warning(
            "refused %s: installing plugins is a dashboard owner action (app=%s)",
            operation,
            request.get("app"),
        )
        return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": _CODE_INVALID_JSON}, status=400
        )
    return body


def _refusal(exc: install_mod.InstallError) -> web.Response:
    return web.json_response(
        {"error": str(exc), "code": exc.code}, status=_STATUS_FOR.get(exc.code, 400)
    )


def _result_payload(result: install_mod.InstallResult) -> dict[str, Any]:
    record = result.record
    conversion = None
    if result.conversion is not None:
        conversion = {
            "sourceManifest": result.conversion.source_manifest,
            "notConverted": list(result.conversion.not_converted),
            "skippedServers": dict(result.conversion.skipped_servers),
            "notes": list(result.conversion.notes),
        }
    return {
        "name": result.name,
        "state": record.state,
        "replaced": result.replaced,
        "conversion": conversion,
        "notes": list(result.notes),
        "skills": [s.name for s in record.skills],
        "servers": [s.name for s in record.servers],
        "issues": [asdict(i) for i in record.issues],
    }


async def api_agent_plugins_install(request: web.Request) -> web.Response:
    """POST /api/agent-plugins/install — copy or clone a plugin into the powers home.

    Body: ``{"path": "<folder>"}`` or ``{"url": "https://...", "ref"?: ..., "subdir"?: ...}``,
    plus an optional ``"replace": true``.
    """
    body = await _body(request, "agent_plugins.install")
    if isinstance(body, web.Response):
        return body
    path, url = body.get("path"), body.get("url")
    replace = body.get("replace", False)
    if not isinstance(replace, bool) or (path is None) == (url is None):
        return web.json_response(
            {"error": "give exactly one of path or url", "code": _CODE_BAD_REQUEST}, status=400
        )
    try:
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                return web.json_response(
                    {"error": "path must be a folder path", "code": _CODE_BAD_REQUEST}, status=400
                )
            result = await asyncio.to_thread(
                install_mod.install_from_folder, path.strip(), replace=replace
            )
        else:
            if not isinstance(url, str):
                return web.json_response(
                    {"error": "url must be a string", "code": _CODE_BAD_REQUEST}, status=400
                )
            result = await asyncio.to_thread(
                install_mod.install_from_git,
                url,
                ref=body.get("ref"),
                subdir=body.get("subdir"),
                replace=replace,
            )
    except install_mod.InstallError as exc:
        return _refusal(exc)
    return web.json_response({"ok": True, "plugin": _result_payload(result)})


async def api_agent_plugins_uninstall(request: web.Request) -> web.Response:
    """POST /api/agent-plugins/uninstall — remove an installed plugin, keeping its data."""
    body = await _body(request, "agent_plugins.uninstall")
    if isinstance(body, web.Response):
        return body
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return web.json_response(
            {"error": "name is required", "code": _CODE_BAD_REQUEST}, status=400
        )
    try:
        await asyncio.to_thread(install_mod.uninstall, name)
    except install_mod.InstallError as exc:
        return _refusal(exc)
    return web.json_response({"ok": True, "name": name})
