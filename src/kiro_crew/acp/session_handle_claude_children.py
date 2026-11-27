"""Claude Code's Agent-tool children on a session of the shared runtime.

claude-agent-acp runs a subagent for the Agent (Task) tool inside the session's own
process and, because the host declares ``clientCapabilities.subagents``, announces it on
the parent session (``subagent_update``) and sends its work under the child's own
sessionId. On ``AcpClient`` ``client_native_children`` reads them; on the shared runtime
the runtime's reader routes the child's frames to the session that announced it
(``runtime_frame_accounting._note_announced_child``), and this module reads them on the
handle with the same functions and the same row shape:

* a ``subagent_update`` merges into the child's row and yields the roster
  (``client_native_children._note_subagent_update``); a child that ends has the text
  its redactor still holds put on its card first;
* a ``session_message`` on the child's session links the Agent call to the child and
  fills a missing task from the prompt;
* a child the roster listed in an earlier turn is listed again on its first frame of
  this turn, as a background child is on the client;
* an adapter notice addressed to the child is its card text;
* a tool call on the parent's session stamped with a known child's Agent call is that
  child's activity, then the parent's ordinary row, as on the client.

The child's other frames go on to the handle's own child arm
(``session_handle_updates._handle_update_frame``), which parses its tool calls into the
caches its permission requests are judged by and puts its text on its card through the
child's own redactor.

A child the adapter granted ``capabilities.cancel`` can be stopped alone
(:func:`cancel_native_child`, ``session/cancel`` naming it on the shared connection),
and the requests each child asks this turn are kept for that Stop
(:func:`native_child_requests`).

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_native_child_sids``, ``_native_child_turn``,
``_prompt_or_tool_seen``.

Instance fields read: ``_announced_subagent_roster``, ``_native_child_rows``, ``_runtime``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import client_native_children as children
from kiro_crew.acp._dispatch import redact_text
from kiro_crew.acp.types import (
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    METHOD_CANCEL,
    UPDATE_NOTICE,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    AcpEvent,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

logger = logging.getLogger("kiro_crew.acp.session_handle")


def _claude_children_declared(self: AcpSessionHandle) -> bool:
    """Whether this session reads claude's child form (the client's own rule)."""
    reader: Any = self
    return children._native_children_declared(reader)


def _native_child_roster(self: AcpSessionHandle) -> list[AcpEvent]:
    """The roster of this session's children, or none while it has none.

    This turn's claude children that have a task (the client's rule: a row with no
    task is held back until its ``session_message`` brings the prompt), then the
    children announced in codex-acp's form. ``raw_sdk_tasks.pending_events`` lists it
    ahead of a Workflow run's agents, so that roster names every child.
    """
    rows = [
        dict(self._native_child_rows[sid])
        for sid in self._native_child_turn
        if self._native_child_rows[sid]["initialQuery"]
    ]
    if rows:
        self._prompt_or_tool_seen = True
    rows += [dict(row) for row in self._announced_subagent_roster.values()]
    return [AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=rows)] if rows else []


def _native_child_of(self: AcpSessionHandle, session_id: object, update: dict[str, Any]) -> str:
    """The known claude child a frame belongs to, or ``""`` (the client's rule)."""
    reader: Any = self
    return children._native_child_of(reader, session_id, update)


def _reset_claude_child_turn(self: AcpSessionHandle) -> None:
    """Start a turn's claude roster (``client_native_children._reset_native_child_turn``)."""
    if self._claude_children_declared():
        children._start_native_child_turn(self)


def _claude_child_events(
    self: AcpSessionHandle, msg: JsonRpcMessage
) -> tuple[list[AcpEvent], bool] | None:
    """The claude-child reading of one ``session/update``, or None when it is no child's.

    Returns ``(events, consumed)``. ``consumed`` means the frame is not the parent's
    and nothing else reads it; otherwise *events* go before what the handle's own
    reading of the frame makes.
    """
    if not self._claude_children_declared():
        return None
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    reader: Any = self
    kind = update.get("sessionUpdate")
    if kind == children.UPDATE_SUBAGENT_UPDATE:
        child = update.get("sessionId")
        roster = children._note_subagent_update(reader, update)
        row = self._native_child_rows.get(child) if isinstance(child, str) else None
        if row is not None and row["status"].get("type") != "working":
            # The child ended: the text its redactor holds goes on its card first.
            roster = self._flush_announced_child_text(str(child)) + roster
        return roster, True
    frame_sid = params.get("sessionId")
    if isinstance(frame_sid, str) and frame_sid in self._native_child_rows:
        # A child that outlived an earlier turn: the turn start cleared its id from
        # this turn's set, but its row and route remain, so its first frame here
        # (a notice, say) is still the child's.
        self._native_child_sids.add(frame_sid)
    child_sid = self._native_child_of(frame_sid, update)
    if not child_sid:
        if kind == UPDATE_TOOL_CALL:
            # A child's call on the parent's session whose Agent-call link this
            # session has none of (refused at its cap): marked as a child's, so its
            # directive cannot act on the parent's chat, as on the per-chat client.
            stamped = children._parent_tool_use(update)
            if stamped:
                return children._untracked_child_call(str(stamped), update), False
        return None
    events: list[AcpEvent] = []
    if child_sid not in self._native_child_turn and child_sid in self._native_child_rows:
        # A background child started in an earlier turn: list it again here.
        self._native_child_turn[child_sid] = None
        events.extend(self._native_child_roster())
    if kind == children.UPDATE_SESSION_MESSAGE:
        return events + children._note_session_message(reader, child_sid, update), True
    if frame_sid != child_sid:
        # A child's frame on the parent's session, stamped with the child's Agent call.
        if kind == UPDATE_TOOL_CALL:
            tool_call_id = str(update.get("toolCallId") or "")
            if tool_call_id:
                self._prompt_or_tool_seen = True
                events.append(
                    AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=child_sid,
                        tool_call_id=tool_call_id,
                        title=redact_text(str(update.get("title") or "")),
                    )
                )
            return events, False
        if kind == UPDATE_TOOL_CALL_UPDATE:
            return events, False
        text = children._child_text(self.backend, kind, update)
        if text:
            self._prompt_or_tool_seen = True
            events.extend(self._child_text_activity(child_sid, text))
        return events, True
    if kind == UPDATE_NOTICE:
        text = children._child_text(self.backend, kind, update)
        if text:
            self._prompt_or_tool_seen = True
            events.extend(self._child_text_activity(child_sid, text))
        return events, True
    return events, False


def _note_native_child_request(self: AcpSessionHandle, msg: JsonRpcMessage) -> None:
    """Remember which child a permission or elicitation request is for, for its Stop
    (``client_native_children._note_native_child_request``). Never raises."""
    reader: Any = self
    children._note_native_child_request(reader, msg)


def native_child_requests(self: AcpSessionHandle, child_sid: str) -> tuple[str, ...]:
    """The ids of the permission and elicitation requests *child_sid* asked this turn."""
    reader: Any = self
    return children.native_child_requests(reader, child_sid)


async def cancel_native_child(self: AcpSessionHandle, child_sid: str) -> bool:
    """Ask the harness to stop the native child *child_sid*; whether it was asked.

    The client's conditions (``client_native_children.cancel_native_child``): a backend
    in ``ACP_BACKENDS_NATIVE_CHILD_CANCEL``, a child this session tracks whose last
    report granted ``capabilities.cancel`` and left it running, and never this
    session's own id, which would cancel the parent's turn. ``session/cancel`` naming
    the child goes out on the shared connection as a notification; the parent's turn
    and every other session on the process go on. A failed write answers False, so the
    caller closes the card itself.
    """
    reader: Any = self
    if not children.native_child_cancellable(reader, child_sid):
        return False
    try:
        await self._runtime.send_notification(METHOD_CANCEL, {"sessionId": child_sid})
    except Exception as exc:  # noqa: BLE001 - a Stop that cannot be sent closes the card
        logger.warning("cancelling native child %s failed: %s", child_sid, type(exc).__name__)
        return False
    logger.info("asked the harness to stop native child %s", child_sid)
    return True
