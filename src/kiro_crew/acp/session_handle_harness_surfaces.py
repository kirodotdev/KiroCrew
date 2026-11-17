"""The claude-agent-acp surfaces a session of the shared runtime shows.

``AcpClient`` reads them for a chat on its own process (``client_turn``,
``client_tool_events``, ``client_usage``); a chat on the shared runtime reads its
frames here, one session at a time, through the same readers:

* an adapter ``notice`` (``acp.harness_notices.parse_notice``) is the reply text of
  its advisory, with the notice attached for its row, in place of the frame;
* a mode change (``harness_notices.mode_notice_event``) and the checklist a ``plan``
  carries (``acp.plan_checklist.todo_event``) are events before the frame's own;
* a ``read`` tool call names the argument the row previews (``acp.read_path_arg``);
* a native compaction's summary (``acp.compaction_summary``): its streamed chunks are
  kept and are no frame of the turn, and the ``completed`` card's title carries it;
* a claude Agent-tool child's frame (``session_handle_claude_children``), read before
  any of these.

Each reader checks its own ``ACP_BACKENDS_*`` set and that the frame names this
session, so a frame of another session, or of a backend outside the sets, passes
through unchanged (harness-parity H13).

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none (the readers it calls write the handle's own fields).

Instance fields read: none (the helper that reads this session's own frames is
handed the handle).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.acp import compaction_summary, harness_notices, plan_checklist
from kiro_crew.acp.compaction_summary import UPDATE_COMPACTION
from kiro_crew.acp.read_path_arg import read_path_arg
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    UPDATE_NOTICE,
    AcpEvent,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle


def _handle_update(self: AcpSessionHandle, msg: JsonRpcMessage) -> list[AcpEvent]:
    """A ``session/update`` notification's events, with this session's harness surfaces.

    A claude child's frame is read first (``session_handle_claude_children``): one it
    consumes makes only its card's events, and any other goes on with what that
    reading put ahead of it.
    """
    child = self._claude_child_events(msg)
    if child is None:
        return _own_update(self, msg)
    events, consumed = child
    return events if consumed else [*events, *_own_update(self, msg)]


def _own_update(handle: AcpSessionHandle, msg: JsonRpcMessage) -> list[AcpEvent]:
    """What the frame parser makes of *msg*, with this session's own surfaces."""
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    frame_sid = str(params.get("sessionId") or "")
    if not isinstance(update, dict) or (frame_sid and frame_sid != handle._session_id):
        return handle._handle_update_frame(msg)
    # The readers take the per-chat client; they read the fields this handle names
    # alike (``_session_id``, ``backend``, ``_compaction_summaries``, the mode fields).
    reader: Any = handle
    kind = update.get("sessionUpdate")
    if kind == UPDATE_NOTICE:
        notice = harness_notices.adapter_notice(reader, msg)
        if notice is not None:
            return [AcpEvent(kind=EVENT_TEXT_CHUNK, text=notice.transcript, harness_notice=notice)]
    if compaction_summary.note_chunk(reader, msg):
        return []
    before: list[AcpEvent] = []
    todo = plan_checklist.todo_event(reader, msg)
    if todo is not None:
        before.append(todo)
    mode = harness_notices.mode_notice_event(reader, msg)
    if mode is not None:
        before.append(mode)
    events = handle._handle_update_frame(msg)
    read_arg = read_path_arg(handle.backend, update.get("kind"))
    if read_arg:
        for event in events:
            if event.kind in (EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE):
                event.read_path_arg = read_arg
    if kind == UPDATE_COMPACTION:
        _fold_compaction_summary(reader, msg, events)
    return [*before, *events]


def _fold_compaction_summary(handle: Any, msg: JsonRpcMessage, events: list[AcpEvent]) -> None:
    """Title a native compaction's ``completed`` card with its summary, as the client does.

    A compaction that ends any other way (failed, cancelled, a repeated terminal the
    lifecycle drops) has nothing to show, so what it streamed is forgotten.
    """
    statuses = [event for event in events if event.kind == EVENT_COMPACTION_STATUS]
    completed = [event for event in statuses if event.text == "completed"]
    if completed:
        for event in completed:
            if not event.title:
                event.title = compaction_summary.completed_summary(handle, msg)
    elif not any(event.text == "started" for event in statuses):
        compaction_summary.forget(handle, msg)
