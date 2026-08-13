"""Fork session — copy messages into a new tab."""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_utils import (
    _sync_dashboard_slots,
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.state import DashboardState, request_slot_origin
from kiro_crew.history import carry_provenance
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_SLOTS_FOR_FORK = 500
_FORK_TITLE_MARKER = "↳ "

# Fork direction: "head" copies messages up to and including the fork point
# (the default); "tail" copies only the messages after it.
_FORK_DIRECTION_HEAD = "head"
_FORK_DIRECTION_TAIL = "tail"
_FORK_DIRECTIONS = (_FORK_DIRECTION_HEAD, _FORK_DIRECTION_TAIL)


async def api_chat_slot_fork(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/fork — fork session into a new tab.

    With ``direction="head"`` (default) copies messages up to and including
    ``at_message_index``. With ``direction="tail"`` copies
    only the messages after ``at_message_index``; the head is dropped.
    An optional ``prompt`` is returned so the frontend can send it.

    Body: ``{ at_message_index?: number, prompt?: string, mode?: string,
    direction?: "head"|"tail" }``
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    request_app = request.get("app", "")
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    # Rate/resource guard: reject if we're already at the cap. Counts slots still
    # under construction too (``live_slot_count``): the import path retracts a
    # slot from ``_slots`` while it is built, and those are allocated memory this
    # cap would otherwise ignore.
    if state.live_slot_count() >= _MAX_SLOTS_FOR_FORK:
        sel().log_api_access(
            caller=request_app or "dashboard", operation="chat.slot_fork",
            outcome="denied", source="rate_limit",
            resources=f"slot={name},slot_count={state.live_slot_count()}",
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
            return web.json_response({"error": "not found"}, status=404)
        if slot._app != request_app:
            sel().log_api_access(
                caller=request_app, operation="chat.slot_fork", outcome="denied",
                source="app_isolation", resources=f"slot={name}",
                error="app does not own this slot",
            )
            # Return 404 (not 403) so a slot owned by another app / an unscoped
            # slot is indistinguishable from a non-existent one — prevents an
            # app-scoped caller enumerating slots across the isolation boundary
            # (CWE-204). The true reason is recorded server-side via SEL above.
            return web.json_response({"error": "not found"}, status=404)

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
    if mode_override is not None and mode_override not in ("", "orchestrator", "crew"):
        return web.json_response({"error": "mode must be '', 'orchestrator' or 'crew'"}, status=400)
    direction = body.get("direction", _FORK_DIRECTION_HEAD)
    if direction not in _FORK_DIRECTIONS:
        return web.json_response(
            {"error": f"direction must be one of {list(_FORK_DIRECTIONS)}"}, status=400,
        )
    if direction == _FORK_DIRECTION_TAIL and not KiroCrewConfig.load().dashboard.tail_fork_enabled:
        # Server-side gate: tail-fork requested but disabled in config —
        # fall back to a normal head-fork rather than reject the request outright.
        # outcome="allowed" (not "denied"): the request still succeeds, just as a
        # head-fork instead of the requested tail-fork; "denied" would misleadingly
        # suggest the fork itself was rejected.
        sel().log_api_access(
            caller=request_app or "dashboard", operation="chat.slot_fork",
            outcome="allowed", source="dashboard",
            resources=f"slot={name},direction=tail",
            error="tail_fork_enabled is False; falling back to head-fork",
        )
        direction = _FORK_DIRECTION_HEAD
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
            all_messages = state.conversation_log.read_messages_chained(slot_history_key(slot))
        if all_messages and slot._dirty:
            new_msgs = slot.messages[slot._resumed_count:]
            if new_msgs:
                all_messages.extend(new_msgs)
        if slot._dirty:
            # Persist with best_effort=False so a lock timeout / I/O failure
            # PROPAGATES instead of being swallowed. The fork treats disk as the
            # source of truth (it re-reads the full history above) and clears
            # ``_dirty`` below — which also disables the periodic retry that
            # would otherwise re-flush the slot. Clearing ``_dirty`` after a
            # silently-dropped save would strand the unwritten source messages
            # and lose them permanently on the next gateway restart. Only mark
            # the slot clean once the durable write is CONFIRMED; on failure,
            # abort the fork (leaving ``_dirty`` set) rather than fork from a
            # partially-persisted source.
            try:
                await save_slot_off_loop(state, slot, best_effort=False)
            except Exception:
                logger.warning(
                    "chat_fork: durable save of source slot=%s failed; "
                    "aborting fork to avoid losing unwritten messages",
                    slot.key, exc_info=True,
                )
                return web.json_response(
                    {"error": "could not persist source session before fork; "
                              "please retry"},
                    status=503,
                )
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

    head_messages: list[dict] = []
    if direction == _FORK_DIRECTION_TAIL:
        if at_index is None:
            return web.json_response(
                {"error": "at_message_index is required for a tail fork"}, status=400,
            )
        head_messages = visible[: at_index + 1]
        visible = visible[at_index + 1:]
        if not visible:
            return web.json_response(
                {"error": "no messages after the fork point"}, status=400,
            )
    elif at_index is not None:
        visible = visible[: at_index + 1]

    new_slot = state.get_or_create_slot(
        name=None, agent=slot.agent, workspace=slot.workspace, model=slot.model,
        mode=mode_override if mode_override is not None else slot.mode,
        app=request_app,
        origin=request_slot_origin(request_app),
    )
    new_slot.forked_from = effective_session_key(slot)
    new_slot.reasoning_effort = slot.reasoning_effort
    # Inherit project folder so the fork appears next to its parent in the sidebar.
    new_slot.folder_id = slot.folder_id
    # Inherit tags (copied, so later edits to either slot's list stay independent).
    new_slot.tags = list(slot.tags)
    parent_title = slot.title if slot._titled else "Untitled"
    parent_title, _ = redact_exfiltration_urls(parent_title)
    parent_title, _ = redact_credentials(parent_title)
    # Strip a leading marker from the parent so it never compounds on a
    # fork-of-a-fork.
    parent_title = parent_title.removeprefix(_FORK_TITLE_MARKER)
    fork_word = "Tail of" if direction == _FORK_DIRECTION_TAIL else "Fork of"
    new_slot.title = f"{_FORK_TITLE_MARKER}{fork_word} {parent_title}"
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
            # A fork copies the parent's messages into a new session. Origin is
            # a property of the message, not of the file, so a copied inbound
            # channel turn keeps the origin it actually had.
            carry_provenance(new_slot.messages[-1], m)
        new_slot.drain()
        await save_slot_off_loop(state, new_slot)
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
            f"direction={direction},"
            f"head_count={len(head_messages)},"
            f"prompt_len={len(prompt)},mode={new_slot.mode}"
        ),
    )
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return web.json_response(
        {"ok": True, "key": new_slot.key, "title": new_slot.title,
         "messages": len(visible), "prompt": prompt,
         "folder_id": new_slot.folder_id or None,
         "direction": direction}
    )
