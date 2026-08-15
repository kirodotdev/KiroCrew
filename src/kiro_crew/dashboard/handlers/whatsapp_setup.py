"""Dashboard endpoints for the WhatsApp channel setup flow."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from aiohttp import web

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.dashboard.channel_folders import (
    clean_session_folder,
    ensure_channel_folder,
    stored_folder_name,
)
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.dashboard.handlers.messaging import is_direct_local_request

logger = logging.getLogger(__name__)


def _live_client(request: web.Request) -> Any:
    """The running WhatsAppClient, or None (channel disabled/not started)."""
    state = request.app.get("state")
    transports = getattr(state, "channel_transports", {}) or {}
    transport = transports.get("whatsapp")
    return getattr(transport, "client", None)


async def whatsapp_config_get(request: web.Request) -> web.Response:
    """GET /api/whatsapp/config: status + policy (the channel has no secrets)."""
    wa = (await asyncio.to_thread(KiroCrewConfig.load)).whatsapp
    state = request.app.get("state")
    client = _live_client(request)
    return web.json_response(
        {
            "configured": bool(wa.enabled and getattr(state, "whatsapp_connected", False)),
            "connected": bool(getattr(state, "whatsapp_connected", False)),
            "connect_error": str(getattr(state, "whatsapp_connect_error", ""))[:120],
            "state": str(getattr(client, "state", "unpaired")),
            "read_only": not is_direct_local_request(request),
            "enabled": bool(wa.enabled),
            "dm_policy": wa.dm_policy,
            "allowed_wa_ids": [str(u) for u in wa.allowed_wa_ids],
            "groups": list(wa.groups),
            "session_folder": wa.session_folder,
        }
    )


async def whatsapp_config_save(request: web.Request) -> web.Response:
    """PUT /api/whatsapp/config: persist policy fields (config-lock serialized)."""
    if not is_direct_local_request(request):
        return web.json_response(
            {"error": "read-only from remote sessions (local machine only)"}, status=403
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    if "enabled" in body and not isinstance(body["enabled"], bool):
        return web.json_response({"error": "enabled must be a boolean"}, status=400)
    policies = ("self", "allowlist", "open", "disabled")
    if "dm_policy" in body and body["dm_policy"] not in policies:
        return web.json_response({"error": "invalid dm_policy"}, status=400)
    if "allowed_wa_ids" in body and not isinstance(body["allowed_wa_ids"], list):
        return web.json_response({"error": "allowed_wa_ids must be a list"}, status=400)
    if "groups" in body and not isinstance(body["groups"], list):
        return web.json_response({"error": "groups must be a list"}, status=400)
    session_folder = ""
    if "session_folder" in body:
        try:
            session_folder = clean_session_folder(body["session_folder"])
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "code": "invalid_session_folder"}, status=400
            )
    async with _get_config_lock():
        cp = config_path()

        def _read_config() -> Dict[str, Any]:
            return json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}

        try:
            data: Dict[str, Any] = await asyncio.to_thread(_read_config)
        except Exception:
            return web.json_response({"error": "config.json is corrupt"}, status=500)
        if not isinstance(data, dict):
            return web.json_response({"error": "config.json is corrupt"}, status=500)
        if not isinstance(data.get("whatsapp"), dict):
            data["whatsapp"] = {}
        wa = data["whatsapp"]
        if "enabled" in body:
            wa["enabled"] = bool(body["enabled"])
        if "dm_policy" in body:
            wa["dm_policy"] = str(body["dm_policy"])
        if "allowed_wa_ids" in body:
            wa["allowed_wa_ids"] = [
                str(u).strip() for u in body["allowed_wa_ids"] if str(u).strip()
            ]
        if "groups" in body:
            wa["groups"] = [g for g in body["groups"] if isinstance(g, dict)]
        if "session_folder" in body:
            wa["session_folder"] = session_folder
        serialized = json.dumps(data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(atomic_write, cp, serialized)
        _folder_name = stored_folder_name(wa.get("session_folder"))
        if _folder_name:
            _state = request.app.get("state")
            if _state is not None:
                await ensure_channel_folder(
                    _state, "whatsapp", _folder_name,
                    relabel="session_folder" in body,
                )
    return web.json_response({"ok": True, "restart_required": True})


async def whatsapp_qr_start(request: web.Request) -> web.Response:
    """POST /api/channels/whatsapp/qr/start (pairing runs on the live client)."""
    if not is_direct_local_request(request):
        return web.json_response({"error": "local machine only"}, status=403)
    client = _live_client(request)
    if client is None:
        return web.json_response(
            {"error": "channel not running (enable whatsapp and restart)"}, status=409
        )
    return web.json_response({"ok": True, "state": client.state})


async def whatsapp_qr_status(request: web.Request) -> web.Response:
    """GET /api/channels/whatsapp/qr/status — current rotating QR as data URL."""
    client = _live_client(request)
    if client is None:
        return web.json_response({"state": "disabled", "qr_data_url": None, "detail": ""})
    qr_data_url = None
    codes = list(getattr(client, "latest_qr", []) or [])
    if client.state == "pairing" and codes:
        qr_data_url = await asyncio.to_thread(_render_qr, codes, client.latest_qr_at)
    return web.json_response(
        {
            "state": client.state,
            "qr_data_url": qr_data_url,
            "detail": str(getattr(client, "state_detail", ""))[:200],
        }
    )


def _render_qr(codes: list, emitted_at: float) -> "str | None":
    """PNG data URL for the currently-valid rotating code (~20s each).
    segno ships with the whatsapp extra (a neonize dependency); this path is
    only reachable while the channel runs, so the import resolves."""
    import time

    import segno

    idx = 0
    if emitted_at:
        idx = min(int((time.monotonic() - emitted_at) // 20), len(codes) - 1)
    try:
        return segno.make(codes[idx]).png_data_uri(scale=6)
    except Exception:
        logger.warning("whatsapp: QR render failed", exc_info=True)
        return None


async def whatsapp_unlink(request: web.Request) -> web.Response:
    """POST /api/channels/whatsapp/unlink: logout, then delete the session DB."""
    if not is_direct_local_request(request):
        return web.json_response({"error": "local machine only"}, status=403)
    client = _live_client(request)
    if client is None:
        return web.json_response({"error": "channel not running"}, status=409)
    try:
        await client.logout()
    except Exception:
        logger.warning("whatsapp: logout failed; deleting session anyway", exc_info=True)
    try:
        import pathlib

        pathlib.Path(client.db_path).unlink(missing_ok=True)
    except OSError:
        logger.warning("whatsapp: session db delete failed", exc_info=True)
    return web.json_response({"ok": True})


async def whatsapp_groups_get(request: web.Request) -> web.Response:
    """GET /api/whatsapp/groups: joined groups for the Settings picker."""
    client = _live_client(request)
    if client is None or not client.is_connected:
        return web.json_response({"groups": []})
    return web.json_response({"groups": await client.list_groups()})


def setup_whatsapp_routes(app: web.Application) -> None:
    """Register the WhatsApp setup routes (mirrors setup_weixin_routes)."""
    app.router.add_get("/api/whatsapp/config", whatsapp_config_get)
    app.router.add_put("/api/whatsapp/config", whatsapp_config_save)
    app.router.add_get("/api/whatsapp/groups", whatsapp_groups_get)
    app.router.add_post("/api/channels/whatsapp/qr/start", whatsapp_qr_start)
    app.router.add_get("/api/channels/whatsapp/qr/status", whatsapp_qr_status)
    app.router.add_post("/api/channels/whatsapp/unlink", whatsapp_unlink)
