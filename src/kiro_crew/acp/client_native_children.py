"""Claude Code's native subagents on an ``AcpClient`` session (harness-parity row H16).

claude-agent-acp starts a subagent for the Agent (Task) tool inside the parent's own
process. Once the client declares ``clientCapabilities.subagents``, the adapter
announces each child on the parent session (``subagent_update``: the child's
sessionId, a title, a description and its state), sends the delegated prompt on the
child session (``session_message``, whose ``messageId`` is the Agent call's
``toolCallId``), and re-addresses the child's own frames to the child's sessionId.
Without the declaration the child's frames arrive on the parent session, where they
render as the parent's own rows.

This module turns those frames into the events the native Activity cards already
read: an ``EVENT_SUBAGENT_LIST`` roster of this turn's children that have a task, in
the kiro-cli shape, on every lifecycle change, and ``EVENT_SUBAGENT_ACTIVITY`` for a
child's text and tool calls. A child's
text never becomes the parent's text; a child's tool frames still continue to the
ordinary tool path after their activity prefix, which is how a kiro-cli child's tool
calls render too.

The activity events leave here unredacted. ``client_turn._redact_native_child_events``
redacts them before the turn yields them, a child's text through that child's own
``StreamRedactor``: a chunk redacted alone would let a credential split across two
chunks through. The roster's display strings are redacted whole by the cards that
show them, so a bound applied here never cuts inside a token (``_label``).

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_native_child_by_tool_use``, ``_native_child_overflow``,
``_native_child_rows``, ``_native_child_turn``, ``_prompt_or_tool_seen``.

Instance fields read: ``_native_child_sids``, ``_session_id``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch import parse_text_chunk
from kiro_crew.acp.session_handle_native_children import (
    NATIVE_CHILD_LABEL_CAP,
    NATIVE_CHILD_ROSTER_CAP,
    _note_native_child,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_CLIENT_CAPABILITIES,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger, as for every ``AcpClient`` owner.
logger = logging.getLogger("kiro_crew.acp.client")


#: The ACP subagents form claude-agent-acp sends a client that declares
#: ``subagents`` (measured on 0.94.0): the child's lifecycle, on the session that
#: started it.
UPDATE_SUBAGENT_UPDATE = "subagent_update"
#: The prompt a child was handed, on the child's own session. Its ``messageId`` is
#: the parent's Agent ``toolCallId`` for the first delegation.
UPDATE_SESSION_MESSAGE = "session_message"

#: What the Claude backend declares. ``subagents`` is promised only because this
#: module handles every frame it turns on: the two lifecycle updates above and the
#: child-addressed frames the adapter re-routes once it is declared. JetBrains AIR's
#: own ``_meta`` opt-in is never sent: it switches the adapter into that client's
#: draft form and its other client-specific branches.
CLAUDE_CLIENT_CAPABILITIES: dict = {**ACP_CLIENT_CAPABILITIES, "subagents": {}}

#: ``clientCapabilities`` per ``AcpClient`` backend; a backend with no row declares
#: the shared :data:`ACP_CLIENT_CAPABILITIES` unchanged.
CLIENT_CAPABILITIES_BY_BACKEND: dict[str, dict] = {
    ACP_BACKEND_CLAUDE: CLAUDE_CLIENT_CAPABILITIES,
}

#: The ``idle`` stop reasons that mean the child finished its work.
_IDLE_FINISHED = frozenset({"end_turn", "max_tokens", "max_turn_requests", ""})

#: The run of non-space characters a cut string ends with.
_TRAILING_TOKEN = re.compile(r"\S+\Z")


def client_capabilities_for(backend: str) -> dict:
    """The ``clientCapabilities`` an ``AcpClient`` on *backend* declares."""
    return CLIENT_CAPABILITIES_BY_BACKEND.get(backend, ACP_CLIENT_CAPABILITIES)


def _label(value: object) -> str:
    """One backend-authored display string, bounded for storage.

    The cards redact the whole string they receive, so the cut must not leave part
    of a credential: a URL cut before its ``@`` does not read as one and would
    reach the card unredacted. A token the bound splits is dropped whole.
    """
    text = str(value or "")
    if len(text) <= NATIVE_CHILD_LABEL_CAP:
        return text
    cut = text[:NATIVE_CHILD_LABEL_CAP]
    if text[NATIVE_CHILD_LABEL_CAP].isspace():
        return cut
    return _TRAILING_TOKEN.sub("", cut)


def _standard_state_status(state: object) -> dict[str, str] | None:
    """A ``subagent_update`` state as a roster ``status``, or None to keep the last one.

    A frame without a ``state`` object only changes the title or the description.
    ``requires_action`` keeps the card running with no message: the card's tool line
    shows a message until a later non-empty one replaces it, and nothing replaces it
    when the child resumes, so it would go on saying the child waits on the user.
    """
    if not isinstance(state, dict):
        return None
    name = str(state.get("state") or "")
    if name == "idle":
        reason = str(state.get("stopReason") or "")
        if reason in _IDLE_FINISHED:
            return {"type": "completed", "message": ""}
        if reason == "cancelled":
            return {"type": "failed", "message": "cancelled"}
        error = state.get("error")
        detail = error.get("message") if isinstance(error, dict) else ""
        return {"type": "failed", "message": _label(detail or reason)}
    return {"type": "working", "message": ""}


def _first_text(content: object) -> str:
    """The first text block of a ``session_message`` content list."""
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return block["text"]
    return ""


def _native_children_declared(self: AcpClient) -> bool:
    """Whether this session declared ``subagents``, so the adapter sends the child form."""
    return "subagents" in client_capabilities_for(self.backend)


def _reset_native_child_turn(self: AcpClient) -> None:
    """Start a turn's roster: a card is listed once its child is seen this turn.

    The ids, rows and Agent-call links outlive the turn, because a background child
    keeps running after the turn that started it and its frames are read by a later
    one. What the bounds refused last turn is said once here and then cleared, as the
    open tool-call set's evictions are. A session that declares nothing keeps no child
    state, so there is nothing to reset.
    """
    if not self._native_children_declared():
        return
    self._native_child_turn.clear()
    if self._native_child_overflow:
        logger.warning(
            "native child store made %d refusal(s) at its bound of %d last turn; "
            "those children got no card, their text was dropped and their tool calls "
            "rendered as ordinary rows",
            self._native_child_overflow,
            NATIVE_CHILD_ROSTER_CAP,
        )
        self._native_child_overflow = 0


def _native_child_roster(self: AcpClient) -> list[AcpEvent]:
    """The roster of this turn's children that have a task.

    The cards skip a child whose first report has no task, for good, so a row
    without one is held back until its ``session_message`` supplies the prompt. It is
    never listed before then, so its absence closes no card.
    """
    rows = [
        self._native_child_rows[sid]
        for sid in self._native_child_turn
        if self._native_child_rows[sid]["initialQuery"]
    ]
    if not rows:
        return []
    self._prompt_or_tool_seen = True
    return [AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=[dict(row) for row in rows])]


def _note_subagent_update(self: AcpClient, update: dict[str, Any]) -> list[AcpEvent]:
    """Merge one ``subagent_update`` into its row and return the roster.

    The child id is admitted by the handle's own ``_note_native_child``, so the
    client and the handle share one cap and one answer for a child past it: the id is
    counted in ``_native_child_overflow`` (said at the next turn's start) and gets no
    row, its text and side frames are dropped, and its tool frames render as the
    parent's ordinary rows, each call still marked as a child's
    (``_untracked_child_call``).
    """
    child_sid = update.get("sessionId")
    if not isinstance(child_sid, str) or not _note_native_child(self, child_sid):
        return []
    row = self._native_child_rows.setdefault(
        child_sid,
        {
            "sessionId": child_sid,
            "sessionName": "",
            "agentName": "",
            "initialQuery": "",
            "status": {"type": "working", "message": ""},
        },
    )
    if update.get("title"):
        row["agentName"] = _label(update["title"])
    if update.get("description"):
        row["sessionName"] = row["initialQuery"] = _label(update["description"])
    status = _standard_state_status(update.get("state"))
    if status is not None:
        row["status"] = status
    self._native_child_turn[child_sid] = None
    return self._native_child_roster()


def _note_session_message(
    self: AcpClient, child_sid: str, update: dict[str, Any]
) -> list[AcpEvent]:
    """Link a delegation to its child and fill a missing task from its prompt."""
    row = self._native_child_rows.get(child_sid)
    if row is None:
        return []
    message_id = update.get("messageId")
    if isinstance(message_id, str) and 0 < len(message_id) <= MAX_ACP_SESSION_ID_LEN:
        if message_id in self._native_child_by_tool_use or (
            len(self._native_child_by_tool_use) < NATIVE_CHILD_ROSTER_CAP
        ):
            self._native_child_by_tool_use[message_id] = child_sid
        else:
            # Past the cap a link is refused and counted, like a child id.
            self._native_child_overflow += 1
    if row["initialQuery"]:
        return []
    prompt = _label(_first_text(update.get("content")))
    if not prompt:
        return []
    row["sessionName"] = row["initialQuery"] = prompt
    return self._native_child_roster()


def _parent_tool_use(update: dict[str, Any]) -> str:
    """The Agent call a frame is stamped with, or ``""`` for an unstamped frame.

    The adapter stamps ``parentToolUseId`` as a string only on a subagent's frames;
    the parent's own frames carry none, or carry it as null.
    """
    meta = update.get("_meta")
    claude_meta = meta.get("claudeCode") if isinstance(meta, dict) else None
    value = claude_meta.get("parentToolUseId") if isinstance(claude_meta, dict) else None
    return value if isinstance(value, str) else ""


def _native_child_of(self: AcpClient, session_id: object, update: dict[str, Any]) -> str:
    """The known child a frame belongs to, or ``""`` for the parent's own frame.

    A child's frames carry its sessionId. One that names the parent's session can
    still be a child's when it is stamped with the Agent call it serves: the adapter
    keeps a child's tool call on the session where a permission request first made it.
    """
    if isinstance(session_id, str) and session_id in self._native_child_sids:
        return session_id
    parent_tool_use = _parent_tool_use(update)
    if parent_tool_use:
        return self._native_child_by_tool_use.get(parent_tool_use, "")
    return ""


def _untracked_child_call(child_key: str, update: dict[str, Any]) -> list[AcpEvent]:
    """Mark a tool call of a child this session does not track as a child's call.

    A child past the cap, or one whose Agent-call link was refused, gets no card, but
    its tool calls still arrive here. The consumers refuse a session directive only
    from a call an activity event names, so without this event the call would read as
    the parent's own, and its directive would act on the parent's chat. The event
    carries no title and nothing is stored, so the bounds still hold. *child_key* is
    the child's sessionId, or the Agent call it serves when the frame names the
    parent's session.
    """
    tool_call_id = str(update.get("toolCallId") or "")
    if not child_key or not tool_call_id:
        return []
    return [
        AcpEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id=child_key, tool_call_id=tool_call_id)
    ]


def _native_child_events(self: AcpClient, msg: JsonRpcMessage) -> tuple[list[AcpEvent], bool]:
    """The native-child reading of one ``session/update``.

    Returns ``(events, consumed)``. ``consumed`` means the frame is not the parent's:
    every reader skips its ordinary handling. A child's tool frame is not consumed,
    so after its activity prefix it reaches the tool path like any other call.
    """
    if not self._native_children_declared():
        return [], False
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if not isinstance(update, dict):
        return [], False
    kind = update.get("sessionUpdate")
    if kind == UPDATE_SUBAGENT_UPDATE:
        return self._note_subagent_update(update), True
    session_id = params.get("sessionId")
    child_sid = self._native_child_of(session_id, update)
    if not child_sid:
        # Once ``subagents`` is declared the adapter names another session only for
        # a child, so an id this session does not track is a child past the cap. It
        # gets no card. Its tool frames keep the ordinary path, as before the
        # declaration: the provenance caches its permission requests are judged by
        # fill from them. A tool call is still marked as a child's, and so is one on
        # the parent's session stamped with an Agent call this session has no link
        # for. Its text and side frames are dropped, never the parent's.
        foreign = (
            isinstance(session_id, str)
            and bool(session_id)
            and bool(self._session_id)
            and session_id != self._session_id
        )
        if kind == UPDATE_TOOL_CALL:
            child_key = session_id if foreign else _parent_tool_use(update)
            return _untracked_child_call(str(child_key), update), False
        return [], foreign and kind != UPDATE_TOOL_CALL_UPDATE
    events: list[AcpEvent] = []
    if child_sid not in self._native_child_turn and child_sid in self._native_child_rows:
        # A background child started in an earlier turn: list it again here.
        self._native_child_turn[child_sid] = None
        events.extend(self._native_child_roster())
    if kind == UPDATE_SESSION_MESSAGE:
        return events + self._note_session_message(child_sid, update), True
    if kind == UPDATE_TOOL_CALL:
        tool_call_id = str(update.get("toolCallId") or "")
        if tool_call_id:
            self._prompt_or_tool_seen = True
            events.append(
                AcpEvent(
                    kind=EVENT_SUBAGENT_ACTIVITY,
                    sub_session_id=child_sid,
                    tool_call_id=tool_call_id,
                    title=str(update.get("title") or ""),
                )
            )
        return events, False
    if kind == UPDATE_TOOL_CALL_UPDATE:
        return events, False
    if kind in (UPDATE_AGENT_MESSAGE_CHUNK, UPDATE_AGENT_THOUGHT_CHUNK):
        text, is_thinking = parse_text_chunk(update)
        if text and not is_thinking:
            self._prompt_or_tool_seen = True
            events.append(
                AcpEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id=child_sid, text=text)
            )
    # Anything else a child sends (its plan, its commands, its session info)
    # describes the child, never the parent.
    return events, True
