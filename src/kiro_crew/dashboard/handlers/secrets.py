"""Secrets vault management API handlers for the dashboard."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.config.loader import config_dir
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(__name__)


async def api_secrets_list(request: web.Request) -> web.Response:
    """GET /api/secrets — list stored secret names (values are never exposed)."""
    vault = SecretVault(config_dir())
    # `list_names` is synchronous: it reads the whole store off disk and parses
    # the JSON. Calling it directly would block the gateway's event loop for the
    # duration of that read, stalling every other request. `SecretVault.set` and
    # `.delete` already offload internally via `asyncio.to_thread`; this is the
    # one read path that does not, so it is wrapped here.
    names = await asyncio.to_thread(vault.list_names)
    return web.json_response({"names": sorted(names)})


async def api_secrets_set(request: web.Request) -> web.Response:
    """POST /api/secrets — store or update a secret.

    Body: {"name": "...", "value": "..."}
    """
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "Invalid JSON body", "code": "invalid_json"}, status=400)

    # A JSON body can legally be an array, string or number, and `name`/`value`
    # can legally be any JSON type. Calling `.strip()` on a non-string raised
    # AttributeError and surfaced as an HTTP 500 on well-formed-but-wrong input,
    # so both the container and the field types are checked before use.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "Request body must be a JSON object", "code": "invalid_body"},
            status=400,
        )

    name = body.get("name")
    value = body.get("value")

    if not isinstance(name, str):
        return web.json_response(
            {"error": "Secret name must be a string", "code": "invalid_name_type"},
            status=400,
        )
    if not isinstance(value, str):
        return web.json_response(
            {"error": "Secret value must be a string", "code": "invalid_value_type"},
            status=400,
        )

    name = name.strip()

    if not name:
        return web.json_response(
            {"error": "Secret name is required", "code": "missing_name"}, status=400
        )
    if not value:
        return web.json_response(
            {"error": "Secret value is required", "code": "missing_value"}, status=400
        )

    vault = SecretVault(config_dir())
    await vault.set(name, value)
    logger.info("Vault entry '%s' stored via dashboard", name)
    return web.json_response({"ok": True, "name": name})


async def api_secrets_delete(request: web.Request) -> web.Response:
    """DELETE /api/secrets/{name} — delete a secret."""
    name = request.match_info["name"]
    if not name:
        return web.json_response(
            {"error": "Secret name is required", "code": "missing_name"}, status=400
        )

    vault = SecretVault(config_dir())
    await vault.delete(name)
    logger.info("Vault entry '%s' deleted via dashboard", name)
    return web.json_response({"ok": True, "name": name})


def setup_secrets_routes(app: web.Application) -> None:
    """Register /api/secrets routes on the dashboard app."""
    app.router.add_get("/api/secrets", api_secrets_list)
    app.router.add_post("/api/secrets", api_secrets_set)
    app.router.add_delete("/api/secrets/{name}", api_secrets_delete)
