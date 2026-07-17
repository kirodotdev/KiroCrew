"""Fork session — copy messages into a new tab."""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import _history_key_for, _sync_dashboard_slots
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_SLOTS_FOR_FORK = 500
_FORK_TITLE_MARKER = "↳ "


async def api_chat_slot_fork(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/fork — fork session into a new tab.

    Creates a new slot with messages copied from the source up to
    ``at_message_index`` (inclusive, into the visible user/assistant list).
    An optional ``prompt`` is returned so the frontend can send it.

    Body: ``{ at_message_index?: number, prompt?: string }``
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    request_app = request.get("app", "")
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    # Rate/resource guard: reject if we're already at the cap.
    if len(state._slots) >= _MAX_SLOTS_FOR_FORK:
        sel().log_api_access(
            caller=request_app or "dashboard", operation="chat.slot_fork",
            outcome="denied", source="rate_limit",
            resources=f"slot={name},slot_count={len(state._slots)}",
            error="slot cap reached",
        )
        return web.json_response(
            {"error": f"slot cap reached ({_MAX_SLOTS_FOR_FORK})"}, status=429,
        )

    # App ownership check (App Kit §5.2)
    if request_app:
        if not slot._app:
            sel().log_api_access(
                caller=request_app, operation="chat.slot_fork", outcome="denied",
                source="app_isolation", resources=f"slot={name}",
                error="app cannot fork unscoped slots",
            )
            return web.json_response({"error": "app cannot fork unscoped slots"}, status=403)
        if slot._app != request_app:
            sel().log_api_access(
                caller=request_app, operation="chat.slot_fork", outcome="denied",
                source="app_isolation", resources=f"slot={name}",
                error="app does not own this slot",
            )
            return web.json_response({"error": "app does not own this slot"}, status=403)

    if slot.memory_mode != "persistent":
        sel().log_api_access(
            caller=request_app or "dashboard", operation="chat.slot_fork",
            outcome="denied", source="dashboard",
            resources=f"slot={name},memory_mode={slot.memory_mode}",
            error="non-persistent slot",
        )
        return web.json_response({"error": "cannot fork a non-persistent session"}, status=400)
    if request.body_exists:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)
    else:
        body = {}
    at_index = body.get("at_message_index")
    prompt = body.get("prompt")
    mode_override = body.get("mode")
    if mode_override is not None and mode_override not in ("", "orchestrator"):
        return web.json_response({"error": "mode must be '' or 'orchestrator'"}, status=400)
    if prompt is not None and not isinstance(prompt, str):
        return web.json_response({"error": "prompt must be a string"}, status=400)
    prompt = (prompt or "").strip()
    if len(prompt) > 32_768:
        return web.json_response(
            {"error": "prompt too long (max 32768 chars)"}, status=400,
        )

    # Read disk FIRST (full history). Use chained read so the index space
    # matches what the frontend renders against — slot detail (chat_handlers)
    # also uses read_messages_chained, and visibleIndexMap is built off that.
    # Without this, indices past the current session-file boundary error out
    # with `out of range` even though the user clicked a visible message.
    async with slot._fork_lock:
        all_messages: list[dict] = []
        if state.conversation_log:
            all_messages = state.conversation_log.read_messages_chained(_history_key_for(slot.key))
        if all_messages and slot._dirty:
            new_msgs = slot.messages[slot._resumed_count:]
            if new_msgs:
                all_messages.extend(new_msgs)
        if slot._dirty:
            _save_slot_to_history(state, slot)
            slot._resumed_count = len(slot.messages)
            slot._dirty = False
        if not all_messages:
            all_messages = list(slot.messages)
    visible = [m for m in all_messages if m.get("role") in ("user", "assistant")]
    if not visible:
        return web.json_response({"error": "no messages to fork"}, status=400)
    if at_index is not None:
        if isinstance(at_index, bool) or not isinstance(at_index, int) or at_index < 0:
            return web.json_response(
                {"error": "at_message_index must be a non-negative integer"},
                status=400,
            )
        if at_index >= len(visible):
            return web.json_response(
                {"error": f"at_message_index {at_index} out of range (have {len(visible)} visible messages)"},
                status=400,
            )
        visible = visible[: at_index + 1]

    new_slot = state.get_or_create_slot(
        name=None, agent=slot.agent, workspace=slot.workspace, model=slot.model,
        mode=mode_override if mode_override is not None else slot.mode,
        app=request_app,
    )
    new_slot.forked_from = _history_key_for(slot.key)
    new_slot.reasoning_effort = slot.reasoning_effort
    # Inherit project folder so the fork appears next to its parent in the sidebar.
    new_slot.folder_id = slot.folder_id
    parent_title = slot.title if slot._titled else "Untitled"
    parent_title, _ = redact_exfiltration_urls(parent_title)
    parent_title, _ = redact_credentials(parent_title)
    # Strip a leading marker from the parent so it never compounds on a
    # fork-of-a-fork.
    parent_title = parent_title.removeprefix(_FORK_TITLE_MARKER)
    new_slot.title = f"{_FORK_TITLE_MARKER}Fork of {parent_title}"
    new_slot._titled = True

    try:
        for m in visible:
            role = m.get("role", "assistant")
            content = m.get("content", "")
            if role != "user":
                content, _ = redact_exfiltration_urls(content)
                content, _ = redact_credentials(content)
            cls = "msg msg-u" if role == "user" else "msg msg-a"
            new_slot.append(role, content, cls, ts=m.get("ts", ""), meta=m.get("meta"), broadcast=False)
        new_slot.drain()
        _save_slot_to_history(state, new_slot)
        new_slot._resumed_count = len(new_slot.messages)
    except Exception:
        state._slots.pop(new_slot.key, None)
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation="chat.slot_fork",
            outcome="error",
            source="dashboard",
            resources=f"from={slot.key},to={new_slot.key}",
            error="fork finalisation failed",
        )
        raise
    sel().log_api_access(
        caller=request_app or "dashboard",
        operation="chat.slot_fork",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"from={slot.key},to={new_slot.key},messages={len(visible)},"
            f"at_index={at_index if at_index is not None else 'last'},"
            f"prompt_len={len(prompt)},mode={new_slot.mode}"
        ),
    )
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return web.json_response(
        {"ok": True, "key": new_slot.key, "title": new_slot.title,
         "messages": len(visible), "prompt": prompt,
         "folder_id": new_slot.folder_id or None}
    )
