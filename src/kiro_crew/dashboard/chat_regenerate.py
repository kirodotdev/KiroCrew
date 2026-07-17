"""Regenerate, variant switch, and edit-resend endpoints."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_VARIANTS = 20


async def api_chat_slot_regenerate(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/regenerate — regenerate the last assistant reply."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    async with slot._lock:
        if slot.running:
            return web.json_response({"error": "slot is running"}, status=409)

        msgs = slot.messages
        ai_idx = -1
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "assistant":
                ai_idx = i
                break
        if ai_idx < 0:
            return web.json_response({"error": "no assistant message to regenerate"}, status=400)
        u_idx = -1
        for i in range(ai_idx - 1, -1, -1):
            if msgs[i].get("role") == "user":
                u_idx = i
                break
        if u_idx < 0:
            return web.json_response({"error": "no preceding user message"}, status=400)

        user_msg = msgs[u_idx].get("content", "")
        if not user_msg:
            return web.json_response({"error": "empty user message"}, status=400)

        ai_msg = msgs[ai_idx]
        _rv = ai_msg.get("variants")
        variants: list[dict] = list(_rv) if isinstance(_rv, list) else []  # type: ignore[arg-type]
        current_entry = {"content": ai_msg.get("content", ""), "ts": ai_msg.get("ts", "")}
        if not any(v.get("content") == current_entry["content"] for v in variants):
            variants.append(current_entry)
        if len(variants) > _MAX_VARIANTS:
            variants = variants[-_MAX_VARIANTS:]

        del slot.messages[u_idx + 1:]
        slot._dirty = True
        slot._resumed_count = 0
        # Window was truncated → next save MUST be the archive-safe rewrite path.
        # If the inline save below fails, the flag keeps the flush loop on the
        # rewrite path so the dropped tail is still archived (#3).
        slot._pending_rewrite = True
        slot._pending_variants = variants

        try:
            msgs_snapshot = list(slot.messages)
            await asyncio.to_thread(_save_slot_to_history, state, slot, msgs_snapshot)
        except Exception:
            logger.warning("Regenerate: failed to rewrite session history", exc_info=True)

        sel().log_api_access(
            caller="dashboard",
            operation="chat.regenerate",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )

        hint = (
            "The user regenerated the previous response. Produce a fresh answer — "
            "vary phrasing, structure, or angle. Do not say you already answered or "
            "reference the prior reply."
        )
        task = asyncio.create_task(_run_chat(state, slot, user_msg, regenerate_hint=hint))
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _clear_pending_on_done(t: asyncio.Task) -> None:
            if slot._pending_variants:
                if not t.cancelled() and t.exception() is None:
                    logger.warning("Regenerate: pending variants not consumed by flush, discarding")
                slot._pending_variants = []

        task.add_done_callback(_clear_pending_on_done)
    state.push_slots_update()
    return web.json_response({"ok": True})


async def api_chat_slot_switch_variant(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/switch-variant — switch which regenerated variant is active."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON"}, status=400)
    try:
        idx = int(body.get("index"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid index"}, status=400)

    async with slot._lock:
        if slot.running:
            return web.json_response({"error": "slot is running"}, status=409)

        target = None
        for m in reversed(slot.messages):
            if m.get("role") == "assistant" and m.get("variants"):
                target = m
                break
        if target is None:
            return web.json_response({"error": "no variants"}, status=400)
        raw_target_variants = target.get("variants")
        variants: list[dict] = (
            list(raw_target_variants)  # type: ignore[arg-type]
            if isinstance(raw_target_variants, list)
            else []
        )
        if idx < 0 or idx >= len(variants):
            return web.json_response({"error": "index out of range"}, status=400)

        chosen = variants[idx]
        if not isinstance(chosen, dict):
            return web.json_response({"error": "corrupt variant entry"}, status=400)
        target_dict: dict = target
        target_dict["content"] = chosen.get("content", "")
        target_dict["ts"] = chosen.get("ts", target_dict.get("ts", ""))
        target_dict["variant_idx"] = idx
        slot._dirty = True
        slot._resumed_count = 0
        try:
            msgs_snapshot = list(slot.messages)
            await asyncio.to_thread(_save_slot_to_history, state, slot, msgs_snapshot)
        except Exception:
            logger.warning("switch-variant: failed to persist", exc_info=True)
        sel().log_api_access(
            caller="dashboard",
            operation="chat.switch_variant",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )
        _bc, _ = redact_exfiltration_urls(target_dict["content"])
        _bc, _ = redact_credentials(_bc)
        state.broadcast_ws(
            "chat_variant_switch",
            {"slot": slot.key, "index": idx, "content": _bc},
        )
        return web.json_response({"ok": True, "index": idx})


async def api_chat_slot_edit_resend(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/edit-resend — edit a user message and resend."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    index = body.get("index")
    ts = body.get("ts")
    content = (body.get("content") or "").strip()
    if not content:
        return web.json_response({"error": "content is required"}, status=400)

    async with slot._lock:
        if slot.running:
            return web.json_response({"error": "slot is running"}, status=409)

        msgs = slot.messages

        if ts:
            index = next((i for i, m in enumerate(msgs) if m.get("ts") == ts and m.get("role") == "user"), -1)
            if index < 0:
                return web.json_response({"error": "user message not found for ts"}, status=400)
        elif isinstance(index, int) and 0 <= index < len(msgs):
            if msgs[index].get("role") != "user":
                return web.json_response({"error": "index is not a user message"}, status=400)
        else:
            return web.json_response({"error": "index or ts required"}, status=400)

        del slot.messages[index:]
        slot._dirty = True
        slot._resumed_count = 0

        _bc, _ = redact_exfiltration_urls(content)
        _bc, _ = redact_credentials(_bc)
        slot.append("user", _bc, "msg msg-u")

        try:
            msgs_snapshot = list(slot.messages)
            await asyncio.to_thread(_save_slot_to_history, state, slot, msgs_snapshot)
        except Exception:
            logger.warning("edit-resend: failed to persist", exc_info=True)

        sel().log_api_access(
            caller="dashboard",
            operation="chat.edit_resend",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )

        task = asyncio.create_task(_run_chat(state, slot, _bc))
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error("edit-resend _run_chat failed for %s", slot.key, exc_info=t.exception())

        task.add_done_callback(_on_done)

    state.push_slots_update()
    return web.json_response({"ok": True})
