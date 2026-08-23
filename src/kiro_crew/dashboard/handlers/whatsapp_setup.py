"""Dashboard endpoints for the WhatsApp channel setup flow."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig, update_config_locked
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
            {
                "error": "read-only from remote sessions (local machine only)",
                "code": "remote_read_only",
            },
            status=403,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "body_not_object"}, status=400
        )
    if "enabled" in body and not isinstance(body["enabled"], bool):
        return web.json_response(
            {"error": "enabled must be a boolean", "code": "enabled_not_bool"}, status=400
        )
    policies = ("self", "allowlist", "open", "disabled")
    if "dm_policy" in body and body["dm_policy"] not in policies:
        return web.json_response(
            {"error": "invalid dm_policy", "code": "invalid_dm_policy"}, status=400
        )
    if "allowed_wa_ids" in body and not isinstance(body["allowed_wa_ids"], list):
        return web.json_response(
            {"error": "allowed_wa_ids must be a list", "code": "allowed_wa_ids_not_list"},
            status=400,
        )
    if "groups" in body and not isinstance(body["groups"], list):
        return web.json_response(
            {"error": "groups must be a list", "code": "groups_not_list"}, status=400
        )
    session_folder = ""
    if "session_folder" in body:
        try:
            session_folder = clean_session_folder(body["session_folder"])
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc), "code": "invalid_session_folder"}, status=400
            )
    # `update_config_locked` rather than a hand-rolled read plus `atomic_write`:
    # it is documented as the required path for a new config.json mutation, and it
    # supplies the two things a bare write cannot. It holds an advisory lock on a
    # SIDECAR for the whole read-modify-write, so a concurrent settings writer
    # cannot land between this read and this write and lose one of the two edits;
    # and it preserves the file's mode, which matters because config.json can
    # carry other channels' inline credentials and a replacement written under the
    # process umask would publish them to every local user. The in-process lock
    # stays as well: it serializes this handler against the other dashboard
    # writers that still predate the locked primitive.
    stored: Dict[str, Any] = {}

    def _apply(data: Dict[str, Any]) -> Dict[str, Any]:
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
        # Read back inside the lock: the folder reconciliation below needs the
        # value that was actually STORED, not the one this request proposed.
        stored.update(wa)
        return data

    async with _get_config_lock():
        try:
            await asyncio.to_thread(lambda: update_config_locked(mutate=_apply))
        except Exception:
            # `on_corrupt="fail"` by default, so a corrupt file raises here rather
            # than being silently reset over the operator's other settings.
            logger.warning("whatsapp: config write failed", exc_info=True)
            return web.json_response(
                {"error": "config.json is corrupt", "code": "config_corrupt"}, status=500
            )
        _folder_name = stored_folder_name(stored.get("session_folder"))
        if _folder_name:
            _state = request.app.get("state")
            if _state is not None:
                await ensure_channel_folder(
                    _state,
                    "whatsapp",
                    _folder_name,
                    relabel="session_folder" in body,
                )
    return web.json_response({"ok": True, "restart_required": True})


async def whatsapp_qr_start(request: web.Request) -> web.Response:
    """POST /api/channels/whatsapp/qr/start (pairing runs on the live client)."""
    if not is_direct_local_request(request):
        return web.json_response({"error": "local machine only", "code": "local_only"}, status=403)
    client = _live_client(request)
    if client is None:
        return web.json_response(
            {
                "error": "channel not running (enable whatsapp and restart)",
                "code": "channel_not_running",
            },
            status=409,
        )
    return web.json_response({"ok": True, "state": client.state})


async def whatsapp_qr_status(request: web.Request) -> web.Response:
    """GET /api/channels/whatsapp/qr/status — current rotating QR as data URL."""
    client = _live_client(request)
    if client is None:
        return web.json_response({"state": "disabled", "qr_data_url": None, "detail": ""})
    qr_data_url = None
    codes = list(getattr(client, "latest_qr", []) or [])
    # The rotating code IS the pairing credential: whoever scans it links THEIR
    # phone as a device on the operator's WhatsApp account, with full read and
    # send access to every chat. So it is withheld from anything but a
    # direct-local request, exactly like the three mutating siblings.
    #
    # Only the code is withheld, not the whole response: `state` is what the
    # panel's read-only remote view polls to render its badge, and a 403 here
    # would leave a remote operator unable to see that the channel is connected.
    if client.state == "pairing" and codes and is_direct_local_request(request):
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
        return web.json_response({"error": "local machine only", "code": "local_only"}, status=403)
    client = _live_client(request)
    if client is None:
        return web.json_response(
            {"error": "channel not running", "code": "channel_not_running"}, status=409
        )
    try:
        await client.logout()
    except Exception:
        # The store is KEPT on a failed logout, and the call reports the failure.
        # Deleting it anyway is the worst of both outcomes: the device stays linked
        # on WhatsApp's side, still receiving and able to send, while the only
        # credential that could retry the logout or unlink it is gone. The operator
        # would then have to revoke it from their phone, having been told it
        # succeeded.
        logger.warning("whatsapp: logout failed; keeping the session", exc_info=True)
        return web.json_response(
            {
                "error": "could not unlink from WhatsApp; the device is still linked",
                "code": "logout_failed",
            },
            status=502,
        )

    def _remove_session_store() -> None:
        """Delete the store AND its sidecars, off the loop.

        The `-wal` and `-shm` companions are not incidental: SQLite keeps recently
        written pages in the write-ahead log, so removing only `session.db` can
        leave the newest session material on disk after an unlink that reported
        success. They are covered by the same sensitive-path protection for that
        reason, and they have to be covered by the same deletion.
        """
        base = Path(client.db_path)
        for target in (base, Path(f"{base}-wal"), Path(f"{base}-shm")):
            target.unlink(missing_ok=True)

    try:
        # Offloaded: the data home can be a network mount, and an unlink that
        # stalls there would stall the one gateway loop, and with it every session
        # and the liveness heartbeat.
        await asyncio.to_thread(_remove_session_store)
    except OSError:
        # The logout DID succeed, so the device is unlinked and the leftover file
        # is stale rather than live. Reported so the operator can remove it.
        logger.warning("whatsapp: session db delete failed", exc_info=True)
        return web.json_response(
            {
                "ok": True,
                "warning": "unlinked, but the local session file could not be removed",
                "code": "session_file_kept",
            }
        )
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
