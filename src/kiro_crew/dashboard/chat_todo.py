"""A person ticking a row of the agent's checklist pill.

The pill above the composer mirrors the list the agent keeps with kiro-cli's
``todo_list`` tool. Until now the tool was the only writer: the pill could not
be edited from the dashboard, and when the native conversation restarted
(agent switch, failed ``session/load``, poisoned-conversation discard,
``/clear``) the agent's own list came back empty while the pill kept the old
one -- so neither the agent nor the person could tick the remaining rows.

This route lets the person flip one row. It writes the slot's copy only; the
agent learns of it the way it learns of the whole list after a restart, through
``Slot.todo_recovery_prompt`` on the next fresh native session.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.chat_folders import member_slot_write_refused
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.remote_relay import remote_bound_refusal
from kiro_crew.dashboard.state import DashboardState, _fold_line_breaks
from kiro_crew.dashboard.token_auth import (
    effective_request_app,
    refuse_unattributable_caller,
)
from kiro_crew.sel import sel

_OPERATION = "chat.slot_todo"


async def api_chat_slot_todo(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/todo -- tick or untick one checklist row.

    Body: ``{"id": "<task id>", "text": "<the row's text as shown>", "completed":
    true|false}``. ``text`` is what binds the click to the task the person saw:
    ids are positional, so a mismatch means the list changed under the click. Answers the slot's
    refreshed ``todo`` payload (the same shape the ``slots`` snapshot carries)
    and broadcasts the same ``todo_update`` delta the agent's own tool result
    does, so every open tab repaints the pill.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    if (refusal := refuse_unattributable_caller(state, request, _OPERATION)) is not None:
        return refusal
    if (refusal := member_slot_write_refused(state, request, slot, _OPERATION)) is not None:
        return refusal
    # A person's click on a pill. An app agent's session has no pill a person
    # can click, and the agent's own writer is its todo_list tool, so an app
    # caller is refused with the indistinguishable 404 the other slot writes use.
    request_app = effective_request_app(state, request)
    if request_app:
        sel().log_api_access(
            caller=request_app,
            operation=_OPERATION,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="checklist rows are ticked by the person, not by an app",
        )
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    # A remote-bound session relays the peer's checklist; the local slot holds
    # no list to write. The pill draws such rows read-only; this is the server
    # side of the same rule. AFTER the app fence, so an app caller cannot tell a
    # bound slot (409) from any other slot it does not own (404).
    if (refusal := remote_bound_refusal(slot)) is not None:
        return refusal
    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    assert body is not None
    task_id = body.get("id")
    completed = body.get("completed")
    expected_text = body.get("text")
    if (
        not isinstance(task_id, (str, int))
        or not isinstance(completed, bool)
        or not isinstance(expected_text, str)
    ):
        return web.json_response(
            {
                "error": "body must carry a task id, its text and a boolean completed",
                "code": "todo_bad_body",
            },
            status=400,
        )
    # The body read above is the one suspension point between the lookup and
    # the write. A slot popped and recreated under the same name inside it
    # would take a stale write under the replacement's key; refuse instead.
    if state._slots.get(name) is not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    payload = slot.todo_payload()
    if payload is None:
        return web.json_response(
            {"error": "this session has no checklist", "code": "todo_absent"}, status=404
        )
    stored_text = slot.todo_task_text(str(task_id))
    if stored_text is None:
        return web.json_response(
            {"error": "no such task", "code": "todo_task_not_found"}, status=404
        )
    # Task ids are positional and the agent can replace the whole list between
    # the person's click and this write. The click carries the row's text; a
    # mismatch means the id now names a different task, so the write is refused
    # rather than ticking (and telling the agent not to redo) the wrong one.
    #
    # CLICK IDENTITY IS RAW, not canonicalized. The pin/override matching in
    # state.py canonicalizes (folds line breaks AND neutralizes markers) so a
    # row the AGENT rebuilds from the recovery block still matches. That is the
    # wrong equality here: the neutralizers are lossy, so two DIFFERENT task
    # texts can collapse to one canonical string (a real "[Task checklist]" line
    # and a marker-bearing line both become "[marker-removed]"). If the agent
    # replaces the clicked row with such a text under the same id, the canonical
    # forms would match and a click meant for the original would toggle the
    # replacement. The person clicked the text they SAW, so identity compares
    # the raw text they submitted against the raw stored text (folded only, so a
    # multi-line row's display still matches its one-line wire form).
    if _fold_line_breaks(stored_text) != _fold_line_breaks(expected_text):
        return web.json_response(
            {
                "error": "the checklist changed under this click; the row is a different task now",
                "code": "todo_task_stale",
                "todo": payload,
            },
            status=409,
        )
    changed = slot.set_todo_task_completed(str(task_id), completed)
    if changed:
        state.broadcast_ws("todo_update", {"slot": slot.key, "todo": slot.todo_payload()})
    # Audited whether or not the flag moved: an idempotent re-submit (two tabs
    # ticking the same row) is still an accepted write against the slot.
    sel().log_api_access(
        caller="dashboard",
        operation=_OPERATION,
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key} task={task_id} completed={completed} changed={changed}",
    )
    return web.json_response({"ok": True, "todo": slot.todo_payload()})
