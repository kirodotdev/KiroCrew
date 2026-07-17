"""Auto-nudge HTTP API — list / start / stop / update loops for chat slots."""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.autonudge import get_instance as _autonudge_get
from kiro_crew.config.loader import workspace_dir_for
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def resolve_stop_sentinel(slot_key: str, workspace: str = "default") -> str:
    """Compute the per-slot sentinel path."""
    ws_dir = workspace_dir_for(workspace)
    safe_key = slot_key.replace("/", "_").replace(":", "_")
    return str(ws_dir / f".stop-{safe_key}")


def render_nudge_message(message: str, stop_sentinel_path: str | None) -> str:
    """Replace {{STOP_FILE}} template with the resolved sentinel path."""
    return message.replace("{{STOP_FILE}}", stop_sentinel_path or "")


def _serialize(loop: Any) -> dict:
    return asdict(loop)


async def api_autonudge_list(request: web.Request) -> web.Response:
    """GET /api/autonudge — list all active loops."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "loops": []})
    return web.json_response({"enabled": True, "loops": [_serialize(lp) for lp in svc.list_all()]})


async def api_autonudge_get(request: web.Request) -> web.Response:
    """GET /api/autonudge/{slot_key} — loop bound to this slot (or null)."""
    svc = _autonudge_get()
    slot_key = request.match_info["slot_key"]
    if svc is None:
        return web.json_response({"enabled": False, "loop": None})
    loop = svc.get_by_slot(slot_key)
    return web.json_response({"enabled": True, "loop": _serialize(loop) if loop else None})


async def api_autonudge_start(request: web.Request) -> web.Response:
    """POST /api/autonudge — start or replace a loop on a slot.

    Body: { slot_key, message, idle_secs?, max_cycles?, stop_sentinel_path? }
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {"error": "auto-nudge disabled (KIROCREW_AUTONUDGE not set)"}, status=503
        )
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    slot_key = (body.get("slot_key") or "").strip()
    message = (body.get("message") or "").strip()
    if not slot_key or not message:
        return web.json_response({"error": "slot_key and message required"}, status=400)
    if slot_key not in state._slots:
        return web.json_response({"error": f"unknown slot {slot_key}"}, status=404)
    if len(message) > 8000:
        return web.json_response({"error": "message too long (max 8000 chars)"}, status=400)
    stop_sentinel_path = (body.get("stop_sentinel_path") or "").strip()
    if stop_sentinel_path and is_sensitive_path(stop_sentinel_path):
        return web.json_response(
            {"error": "stop_sentinel_path points to a sensitive location"}, status=400
        )
    # Auto-default: per-slot sentinel so multiple loops don't clash
    if not stop_sentinel_path:
        slot = state._slots.get(slot_key)
        if slot:
            stop_sentinel_path = resolve_stop_sentinel(slot_key, getattr(slot, "workspace", "default"))
            Path(stop_sentinel_path).unlink(missing_ok=True)
    loop = await svc.add(
        slot_key=slot_key,
        message=message,
        idle_secs=int(body.get("idle_secs", 60)),
        max_cycles=int(body.get("max_cycles", 0)),
        stop_sentinel_path=stop_sentinel_path,
    )
    sel().log_tool_invocation(
        session_key=slot_key,
        source="dashboard",
        tool_name="autonudge_start",
        outcome="success",
        metadata={
            "loop_id": loop.id,
            "idle_secs": loop.idle_secs,
            "max_cycles": loop.max_cycles,
            "caller": request.remote or "",
        },
    )
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_update(request: web.Request) -> web.Response:
    """PATCH /api/autonudge/{loop_id} — update message / idle_secs / active."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"error": "auto-nudge disabled"}, status=503)
    loop_id = request.match_info["loop_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if "message" in body and len(body["message"]) > 8000:
        return web.json_response({"error": "message too long"}, status=400)
    loop = await svc.update(
        loop_id,
        message=body.get("message"),
        idle_secs=body.get("idle_secs"),
        max_cycles=body.get("max_cycles"),
        active=body.get("active"),
    )
    if loop is None:
        return web.json_response({"error": "loop not found"}, status=404)
    sel().log_tool_invocation(
        session_key=loop.slot_key,
        source="dashboard",
        tool_name="autonudge_update",
        outcome="success",
        metadata={
            "loop_id": loop_id,
            "fields": [k for k in ("message", "idle_secs", "max_cycles", "active") if k in body],
            "caller": request.remote or "",
        },
    )
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_delete(request: web.Request) -> web.Response:
    """DELETE /api/autonudge/{loop_id} — stop and remove a loop."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"error": "auto-nudge disabled"}, status=503)
    loop_id = request.match_info["loop_id"]
    # Capture slot_key for audit before removal (loop is gone after remove()).
    existing = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    await svc.remove(loop_id)
    sel().log_tool_invocation(
        session_key=existing.slot_key if existing else "",
        source="dashboard",
        tool_name="autonudge_delete",
        outcome="success" if existing else "noop",
        metadata={"loop_id": loop_id, "caller": request.remote or ""},
    )
    return web.json_response({"ok": True})
