"""``POST /api/apps/{name}/local-copy``: make a marketplace app the owner's own.

The owner-only, explicit way to change a marketplace app, which otherwise
refuses a folder update (``app_update_market_source``). Body, all optional:

* ``confirmReplace``: ``true`` to turn the app itself into a local app under
  the same name.

See :mod:`kiro_crew.apps.manager_local_copy`. The app must be disabled first.
A ``source`` folder is refused here: once the app is local, the ordinary
``POST /api/apps/{name}/update`` applies one.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from kiro_crew.apps.manager_install import app_lifecycle_lock
from kiro_crew.apps.manager_local_copy import make_local_copy
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel


async def handle_local_copy_app(request: web.Request) -> web.Response:
    """Make a local copy of marketplace app ``{name}`` (owner only)."""
    from kiro_crew.dashboard.handlers._shared import (
        body_not_object_error,
        require_owner_dashboard_request,
    )

    denied = await require_owner_dashboard_request(request, "app_local_copy")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    if "source" in body:
        return web.json_response(
            {
                "error": (
                    "make the local copy first, then apply a folder with "
                    f"POST /api/apps/{name}/update"
                ),
                "code": "local_copy_takes_no_source",
            },
            status=400,
        )
    confirm = body.get("confirmReplace", False)
    if not isinstance(confirm, bool):
        return web.json_response(
            {
                "error": "confirmReplace must be a boolean",
                "code": "invalid_local_copy_request",
            },
            status=400,
        )
    async with app_lifecycle_lock(name):
        result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            lambda: make_local_copy(name, confirm_replace=confirm),
        )
    sel().log_api_access(
        caller="dashboard",
        operation="app_local_copy",
        outcome="success" if result.ok else "failed",
        resources=name,
        error="" if result.ok else (result.error or ""),
    )
    if result.ok:
        return web.json_response(result.to_dict())
    return web.json_response(
        {
            "ok": False,
            "name": name,
            "error": result.error or "",
            "code": result.error_code or "app_local_copy_failed",
        },
        status=400,
    )
