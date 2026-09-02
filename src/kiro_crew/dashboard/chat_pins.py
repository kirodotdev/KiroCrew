"""Chat message pin handlers — CRUD for per-session pinned messages."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_PREVIEW_INPUT_CHARS = 4096
_MAX_MESSAGE_TS_CHARS = 256
_MAX_MID_CHARS = 128
_MAX_PINS_PER_SLOT = 50
_VALID_ROLES = frozenset({"user", "assistant"})


def _authorize_app_slot(
    request: web.Request,
    state: DashboardState,
    slot_key: str,
    operation: str,
    *,
    missing_code: str = "slot_not_found",
) -> web.Response | None:
    """Deny app tokens access to unscoped or foreign chat slots.

    Dashboard callers have no app claim and retain owner-wide access. App
    callers receive an indistinguishable 404 so they cannot enumerate slots
    owned by the dashboard or another app (App Kit §5.2 / CWE-204).
    """
    request_app = request.get("app", "")
    if not request_app:
        return None
    slot = state.get_slot(slot_key)
    if slot is not None and slot._app == request_app:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="allowed",
            source="app_isolation",
            resources=f"slot={slot_key}",
        )
        return None
    reason = (
        "app cannot access unscoped slots"
        if slot is not None and not slot._app
        else "app does not own this slot"
    )
    sel().log_api_access(
        caller=request_app,
        operation=operation,
        outcome="denied",
        source="app_isolation",
        resources=f"slot={slot_key}",
        error=reason,
    )
    return web.json_response({"error": "not found", "code": missing_code}, status=404)


def _redacted_pin(pin: dict) -> dict:
    """Copy of ``pin`` with the free-text ``preview`` re-redacted at the output boundary.

    chat_pins.json read from disk may predate the current redactor patterns
    (or have been written by an older version), so the stored ``preview`` --
    the only field carrying user prose -- is re-run through the redactors on
    the way out. The remaining fields (``mid``, ``message_ts``, ``slot_key``)
    are structural identifiers and pass through unchanged.
    """
    return {
        **pin,
        "preview": redact_credentials(redact_exfiltration_urls(pin.get("preview", ""))[0])[0],
    }


async def api_chat_pins_list(request: web.Request) -> web.Response:
    """GET /api/chat/pins?slot=<slot_key> — list pinned messages."""
    state: DashboardState = request.app["state"]
    slot_key = request.query.get("slot", "")
    if not slot_key:
        return web.json_response(
            {"error": "slot query param required", "code": "missing_query_params"},
            status=400,
        )
    denied = _authorize_app_slot(request, state, slot_key, "chat.pins_list")
    if denied is not None:
        return denied
    request_app = request.get("app", "")
    pins = [p for p in state._chat_pins if p["slot_key"] == slot_key]
    # Record-level app ownership: app callers only see pins they created.
    # Legacy records (no origin_app) and dashboard-created records (origin_app="")
    # are visible ONLY to dashboard callers (request_app == "").
    if request_app:
        pins = [p for p in pins if p.get("origin_app", "") == request_app]
    else:
        # Dashboard callers see all pins in slots they own (full owner view).
        pass
    # Sort by pinned_at ascending
    pins.sort(key=lambda p: p.get("pinned_at", ""))
    # Re-redact previews at the output boundary (see _redacted_pin).
    pins = [_redacted_pin(p) for p in pins]
    return web.json_response({"pins": pins})


async def api_chat_pins_create(request: web.Request) -> web.Response:
    """POST /api/chat/pins — pin a chat message."""
    state: DashboardState = request.app["state"]
    body, body_error = await read_bounded_json(request)
    if body_error is not None:
        if body_error.status == 400 and body_error.text:
            try:
                error_body = json.loads(body_error.text)
            except ValueError:
                error_body = {}
            if error_body.get("code") == "body_not_object":
                return web.json_response(
                    {"error": "request body must be a JSON object", "code": "invalid_json"},
                    status=400,
                )
        return body_error
    assert body is not None

    def _str_field(key: str, default: str = "") -> str:
        value = body.get(key, default)
        return value.strip() if isinstance(value, str) else ""

    slot_key = _str_field("slot_key")
    message_ts = _str_field("message_ts")
    mid = _str_field("mid")
    if not slot_key or not mid:
        return web.json_response(
            {"error": "slot_key and mid are required", "code": "missing_required_fields"},
            status=400,
        )
    if len(mid) > _MAX_MID_CHARS:
        return web.json_response(
            {
                "error": f"mid exceeds {_MAX_MID_CHARS} characters",
                "code": "mid_too_large",
            },
            status=400,
        )
    if message_ts and len(message_ts) > _MAX_MESSAGE_TS_CHARS:
        return web.json_response(
            {
                "error": f"message_ts exceeds {_MAX_MESSAGE_TS_CHARS} characters",
                "code": "message_ts_too_large",
            },
            status=400,
        )
    denied = _authorize_app_slot(request, state, slot_key, "chat.pins_create")
    if denied is not None:
        return denied

    role = _str_field("role", "user") or "user"
    if role not in _VALID_ROLES:
        return web.json_response(
            {"error": "role must be user or assistant", "code": "invalid_role"},
            status=400,
        )
    preview_input = _str_field("preview")
    if len(preview_input) > _MAX_PREVIEW_INPUT_CHARS:
        return web.json_response(
            {
                "error": f"preview exceeds {_MAX_PREVIEW_INPUT_CHARS} characters",
                "code": "preview_too_large",
            },
            status=413,
        )
    # Redact credentials and exfiltration URLs BEFORE truncating so a secret
    # straddling the stored-preview boundary cannot survive as an unrecognized
    # fragment (same boundary rule as transcripts), then cap the preview.
    preview = redact_credentials(redact_exfiltration_urls(preview_input)[0])[0][:200]

    request_app = request.get("app", "")

    async with state._chat_pins_lock:
        # Re-verify slot ownership inside the lock for app-scoped callers.
        # Between the pre-lock authorization and acquiring the lock, the slot
        # may have been deleted or replaced by a different app — never persist
        # pin data into a slot that is no longer owned by this caller.
        denied_inner = _authorize_app_slot(request, state, slot_key, "chat.pins_create")
        if denied_inner is not None:
            return denied_inner

        # Idempotent: if already pinned by same caller for this (slot, mid),
        # return existing.  The uniqueness scope is (slot_key, mid, origin_app)
        # — a record created by a different origin_app is invisible to the
        # current caller and must NOT be returned (IDOR / metadata-leak, see
        # CWE-639).  When a foreign record exists for the same (slot, mid),
        # we proceed to create a new caller-owned record.
        existing = next(
            (
                p
                for p in state._chat_pins
                if p["slot_key"] == slot_key
                and p.get("mid") == mid
                and p.get("origin_app", "") == request_app
            ),
            None,
        )
        if existing:
            # Output-boundary rule applies here too: a pre-existing pin on
            # disk may hold an unredacted credential from an older version.
            return web.json_response(_redacted_pin(existing), status=200)

        # Scope the quota to the caller's own records so one origin_app filling
        # the slot to the cap cannot block a different origin_app from pinning
        # in the same slot -- matching the (slot_key, mid, origin_app) scope
        # used by the idempotency lookup above and api_chat_pins_list.
        slot_pin_count = sum(
            1
            for p in state._chat_pins
            if p["slot_key"] == slot_key and p.get("origin_app", "") == request_app
        )
        if slot_pin_count >= _MAX_PINS_PER_SLOT:
            return web.json_response(
                {
                    "error": f"slot already has {_MAX_PINS_PER_SLOT} pinned messages",
                    "code": "pin_limit_reached",
                },
                status=409,
            )

        pin = {
            "id": uuid.uuid4().hex[:12],
            "slot_key": slot_key,
            "mid": mid,
            "message_ts": message_ts,
            "role": role,
            "preview": preview,
            "pinned_at": datetime.now(timezone.utc).isoformat(),
            "origin_app": request_app,
        }
        state._chat_pins.append(pin)
        try:
            await asyncio.to_thread(state.save_chat_pins)
        except Exception:
            # Roll back in-memory append on persist failure
            state._chat_pins.pop()
            logger.warning("chat_pins: failed to persist pin", exc_info=True)
            return web.json_response(
                {"error": "failed to persist pin", "code": "persist_failed"}, status=500
            )
    # Broadcast after the lock is released: slot_key only, no pin content.
    # dashboard-user connections only (app tokens must not receive other slots'
    # pin signals — see CWE-269 and App Kit §5.2).
    state.broadcast_ws_owners("pins_changed", {"slot_key": slot_key})
    return web.json_response(pin, status=201)


async def api_chat_pins_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/pins/{id} — unpin by pin id."""
    state: DashboardState = request.app["state"]
    pin_id = request.match_info["id"]
    request_app = request.get("app", "")
    async with state._chat_pins_lock:
        idx = next((i for i, p in enumerate(state._chat_pins) if p["id"] == pin_id), None)
        if idx is None:
            return web.json_response({"error": "not found", "code": "pin_not_found"}, status=404)
        pin_record = state._chat_pins[idx]
        denied = _authorize_app_slot(
            request,
            state,
            pin_record["slot_key"],
            "chat.pins_delete",
            missing_code="pin_not_found",
        )
        if denied is not None:
            return denied
        # Record-level ownership: app callers can only delete pins they created.
        if request_app and pin_record.get("origin_app", "") != request_app:
            sel().log_api_access(
                caller=request_app,
                operation="chat.pins_delete",
                outcome="denied",
                source="pin_record_ownership",
                resources=f"pin={pin_id}",
                error="app does not own this pin record",
            )
            return web.json_response({"error": "not found", "code": "pin_not_found"}, status=404)
        removed = state._chat_pins.pop(idx)
        try:
            await asyncio.to_thread(state.save_chat_pins)
        except Exception:
            # Roll back: re-insert at original index
            state._chat_pins.insert(idx, removed)
            logger.warning("chat_pins: failed to persist unpin", exc_info=True)
            return web.json_response(
                {"error": "failed to persist pin", "code": "persist_failed"}, status=500
            )
    # Broadcast after the lock is released: slot_key only, no pin content.
    state.broadcast_ws_owners("pins_changed", {"slot_key": removed["slot_key"]})
    return web.json_response({"ok": True})


async def api_chat_pins_delete_by_query(request: web.Request) -> web.Response:
    """DELETE /api/chat/pins?slot=<slot_key>&mid=<mid> — unpin by slot + mid.

    Falls back to message_ts matching for legacy compatibility.
    """
    state: DashboardState = request.app["state"]
    slot_key = request.query.get("slot", "")
    mid = request.query.get("mid", "")
    message_ts = request.query.get("message_ts", "")
    if not slot_key or (not mid and not message_ts):
        return web.json_response(
            {
                "error": "slot and mid (or message_ts) query params required",
                "code": "missing_query_params",
            },
            status=400,
        )
    denied = _authorize_app_slot(request, state, slot_key, "chat.pins_delete")
    if denied is not None:
        return denied
    request_app = request.get("app", "")
    async with state._chat_pins_lock:
        # Prefer mid-based lookup; fall back to message_ts for legacy pins
        if mid:
            idx = next(
                (
                    i
                    for i, p in enumerate(state._chat_pins)
                    if p["slot_key"] == slot_key and p.get("mid") == mid
                ),
                None,
            )
        else:
            idx = next(
                (
                    i
                    for i, p in enumerate(state._chat_pins)
                    if p["slot_key"] == slot_key and p.get("message_ts") == message_ts
                ),
                None,
            )
        if idx is None:
            return web.json_response({"error": "not found", "code": "pin_not_found"}, status=404)
        pin_record = state._chat_pins[idx]
        # Record-level ownership: app callers can only delete pins they created.
        if request_app and pin_record.get("origin_app", "") != request_app:
            sel().log_api_access(
                caller=request_app,
                operation="chat.pins_delete",
                outcome="denied",
                source="pin_record_ownership",
                resources=f"slot={slot_key},mid={mid or message_ts}",
                error="app does not own this pin record",
            )
            return web.json_response({"error": "not found", "code": "pin_not_found"}, status=404)
        removed = state._chat_pins.pop(idx)
        try:
            await asyncio.to_thread(state.save_chat_pins)
        except Exception:
            state._chat_pins.insert(idx, removed)
            logger.warning("chat_pins: failed to persist unpin", exc_info=True)
            return web.json_response(
                {"error": "failed to persist pin", "code": "persist_failed"}, status=500
            )
    # Broadcast after the lock is released: slot_key only, no pin content.
    state.broadcast_ws_owners("pins_changed", {"slot_key": slot_key})
    return web.json_response({"ok": True})
