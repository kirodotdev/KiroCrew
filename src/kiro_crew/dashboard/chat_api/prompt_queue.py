"""The waiting-prompt routes: cancel, edit and reorder a queued message, and take
back a steer the running turn has not read yet."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        DashboardState,
        _edit_queued_by_id,
        _remove_queued_by_id,
        attachment_meta,
        deny_app_slot_access,
        queue_entry_is_user_origin,
        queued_text_for_display,
        read_bounded_json,
        sel,
        slot_not_found,
    )


async def api_chat_slot_queue_cancel(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot}/queue/{queue_id} — cancel a queued message.

    Removes the message from the backend queue and broadcasts a
    ``queue_cancel`` WebSocket event so the frontend can move the
    text back to the input box.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    queue_id = request.match_info["queue_id"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_cancel")
    if denied is not None:
        return denied
    # Read the entry's origin before removing it: a cancel puts the text back in
    # the composer, so a redacted copy of the user's own words would replace the
    # link they typed with a placeholder.
    _entry = next((i for i in slot._queue if i["id"] == queue_id), None)
    from kiro_crew.dashboard import responsive_chat

    if _entry is not None and responsive_chat.row_already_written(slot, _entry):
        # Its user row is already in the transcript: removing only the queue
        # entry would leave the message there and hand the text back to the
        # composer, where sending it again writes a second copy.
        return web.json_response(
            {
                "error": "this message is already in the conversation and can no longer be cancelled",
                "code": "queue_item_already_sent",
            },
            status=409,
        )
    _user_origin = queue_entry_is_user_origin(_entry)
    content = slot.queue_remove_by_id(queue_id)
    if content is None:
        return web.json_response({"error": "queue item not found"}, status=404)
    _remove_queued_by_id(slot.messages, queue_id)
    slot.invalidate_source_links()
    # A cross-session delivery has a sender holding a "queued, will run later"
    # receipt, and the retracted card below is on this session, which the sender
    # does not read. Its own transcript is told the message will never run.
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import (
        SEND_DISCARD_QUEUE_CANCEL,
        notify_send_origin_discarded,
    )

    notify_send_origin_discarded(
        state,
        entry_meta=_entry.get("meta") if _entry is not None else None,
        target_slot=slot,
        text=content,
        cause=SEND_DISCARD_QUEUE_CANCEL,
    )
    _redacted = queued_text_for_display(content, user_origin=_user_origin)
    state.broadcast_ws("queue_cancel", {"slot": name, "queue_id": queue_id, "content": _redacted})
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_cancel",
        tool_kind="permission",
        outcome="allowed",
        metadata={"queue_id": queue_id, "slot": name},
    )
    return web.json_response({"ok": True, "content": _redacted})


async def api_chat_slot_queue_edit(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/queue/{queue_id} — edit a queued message.

    Accepts ``{"content": "new text"}`` and replaces the content of the
    matching queue item in place (order preserved).  Broadcasts a
    ``queue_edit`` WebSocket event so all connected clients update in sync.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    queue_id = request.match_info["queue_id"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_edit")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return web.json_response({"error": "content must be a non-empty string"}, status=400)
    queued = next((i for i in slot._queue if i.get("id") == queue_id), None)
    from kiro_crew.dashboard import responsive_chat

    if queued is not None and responsive_chat.row_already_written(slot, queued):
        return web.json_response(
            {
                "error": "this message is already in the conversation and can no longer be edited",
                "code": "queue_item_already_sent",
            },
            status=409,
        )
    if not slot.queue_edit_by_id(
        queue_id,
        content,
        directive_user_origin=not bool(request.get("app", "")),
    ):
        return web.json_response({"error": "queue item not found"}, status=404)
    # The stored text is what the edit normalized to (attachment markers are
    # renumbered when the edit dropped one), so the row and the broadcast echo
    # the ENTRY, not the request body.
    entry = next((i for i in slot._queue if i["id"] == queue_id), None)
    stored = entry.get("content") if entry is not None else None
    if isinstance(stored, str):
        content = stored
    _edit_queued_by_id(slot.messages, queue_id, content)
    slot.invalidate_source_links()
    _redacted = queued_text_for_display(content, user_origin=queue_entry_is_user_origin(entry))
    frame: dict[str, Any] = {"slot": name, "queue_id": queue_id, "content": _redacted}
    # The edit prunes and renumbers the entry's attachment lists alongside the
    # text (`prune_attachment_meta`), so the frame carries the lists the
    # renumbered markers now index -- the client replaces the row's lists from
    # it. Same `meta` shape and redaction as `queue_entry_view`, read straight
    # off the entry so the content is not redacted a second time. Absent when
    # the entry has none left (or never had any): the client reads absence on
    # THIS frame as "no lists", so a row whose markers the edit all removed
    # drops its stale lists too.
    _edit_attachments = attachment_meta(entry.get("meta")) if entry is not None else {}
    if _edit_attachments:
        frame["meta"] = _edit_attachments
    state.broadcast_ws("queue_edit", frame)
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_edit",
        tool_kind="permission",
        outcome="allowed",
        metadata={"queue_id": queue_id, "slot": name},
    )
    return web.json_response({"ok": True, "content": _redacted})


async def api_chat_slot_queue_reorder(request: web.Request) -> web.Response:
    """PUT /api/chat/slots/{slot}/queue/order — reorder queued messages.

    Accepts ``{"order": ["qid1", "qid2", ...]}`` and rearranges the slot's
    ``_queue`` to match the given id sequence.  Broadcasts a ``queue_reorder``
    WebSocket event so all connected clients update in sync.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_reorder")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        return web.json_response({"error": "order must be a list of queue id strings"}, status=400)
    # Build lookup of current queue items by id
    by_id = {item["id"]: item for item in slot._queue}
    # Validate all ids exist
    missing = [qid for qid in order if qid not in by_id]
    if missing:
        return web.json_response({"error": f"unknown queue ids: {missing}"}, status=400)
    # Reorder: place requested ids first in given order, then any remaining
    reordered = [by_id[qid] for qid in order if qid in by_id]
    remaining = [item for item in slot._queue if item["id"] not in set(order)]
    slot._queue[:] = reordered + remaining
    # Reorder the queued messages in the messages list to match
    queued_msgs = [m for m in slot.messages if m.get("role") == "queued"]
    other_msgs = [m for m in slot.messages if m.get("role") != "queued"]
    queued_by_id: dict[str | None, dict] = {}
    for m in queued_msgs:
        try:
            cls = json.loads(m.get("cls", "{}"))
            queued_by_id[cls.get("queue_id")] = m
        except (json.JSONDecodeError, TypeError):
            pass
    reordered_msgs = [queued_by_id[qid] for qid in order if qid in queued_by_id]
    remaining_msgs = [m for m in queued_msgs if m not in reordered_msgs]
    slot.messages[:] = other_msgs + reordered_msgs + remaining_msgs
    slot.invalidate_source_links()
    state.broadcast_ws(
        "queue_reorder", {"slot": name, "order": [item["id"] for item in slot._queue]}
    )
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_reorder",
        tool_kind="permission",
        outcome="allowed",
        metadata={"slot": name, "order_len": len(order)},
    )
    return web.json_response({"ok": True})


async def api_chat_slot_steer_retract(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/steers/{mid}/retract — take back a pending steer.

    Body ``{"action": "unsend" | "edit" | "queue"}``. Answers 202 once the take-back
    is armed. Whether the turn read the steer first is decided afterwards and lands
    on the steer's own row (``steer_retract``), which is what the dashboard reads.
    409 when the steer is not pending, another steer or take-back is in
    flight, or the harness cannot drop a steer. Refused to an app: a take-back
    is offered only on the session's own human's steer.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    mid = request.match_info["mid"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_steer_retract")
    if denied is not None:
        return denied
    request_app = request.get("app", "")
    if request_app:
        reason = "only the person who sent a steer can take it back"
        sel().log_api_access(
            caller=request_app,
            operation="slot_steer_retract",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error=reason,
        )
        return web.json_response({"error": reason, "code": "steer_retract_forbidden"}, status=403)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    from kiro_crew.dashboard import steer_retract

    action = body.get("action")
    if not isinstance(action, str) or action not in steer_retract.RETRACT_ACTIONS:
        return web.json_response(
            {
                "error": "action must be one of: edit, queue, unsend",
                "code": "steer_retract_bad_action",
            },
            status=400,
        )
    outcome = await steer_retract.begin(state, slot, mid, action)
    refusal = steer_retract.REFUSALS.get(outcome)
    if refusal is not None:
        return web.json_response({"error": refusal[0], "code": refusal[1]}, status=409)
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="steer_retract",
        tool_kind="permission",
        outcome="allowed",
        metadata={"slot": name, "action": action},
    )
    return web.json_response({"ok": True, "pending": True}, status=202)
