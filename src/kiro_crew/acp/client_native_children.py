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
child's text and tool calls; an adapter notice addressed to a child is its text, in the
notice's text form (``_child_text``). A child's
text never becomes the parent's text; a child's tool frames still continue to the
ordinary tool path after their activity prefix, which is how a kiro-cli child's tool
calls render too.

opencode sends no child frames: its ``task`` tool runs the subagent and reports it
only as the parent's own tool row (``_opencode_task_events``). That row still names
the task, the agent and the end, so it feeds the same roster, keyed by the row's
``toolCallId``, and the ``<task_result>`` the row completes with is the card's text.
The row itself still renders as the parent's tool call.

A child the adapter announced with ``capabilities.cancel`` can be stopped alone:
``cancel_native_child`` sends ``session/cancel`` naming it, the request a Stop on its
card makes (``ACP_BACKENDS_NATIVE_CHILD_CANCEL``). The permission and elicitation
requests each child asks this turn are remembered (``_note_native_child_request``), so
that Stop can settle the cards of the ones the stopped child leaves open
(``native_child_requests``).

The activity events leave here unredacted. ``client_turn._redact_native_child_events``
redacts them before the turn yields them, a child's text through that child's own
``StreamRedactor``: a chunk redacted alone would let a credential split across two
chunks through. The roster's display strings are redacted whole by the cards that
show them, so a bound applied here never cuts inside a token (``_label``).

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_native_child_by_tool_use``, ``_native_child_cancellable``,
``_native_child_overflow``, ``_native_child_rows``,
``_native_child_turn``, ``_prompt_or_tool_seen``.

Instance fields read: ``_native_child_requests``, ``_native_child_sids``, ``_process``,
``_session_id``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import transport_framing
from kiro_crew.acp._dispatch import parse_text_chunk
from kiro_crew.acp.harness_notices import parse_notice
from kiro_crew.acp.native_child_stamp import parent_tool_use
from kiro_crew.acp.session_handle_native_children import (
    NATIVE_CHILD_LABEL_CAP,
    NATIVE_CHILD_ROSTER_CAP,
    NATIVE_CHILDREN_UNCHARGED_KIND,
    _note_native_child,
    native_child_refusal,
)
from kiro_crew.acp.session_handle_native_children import (
    native_child_resume_refusal as _handle_resume_refusal,
)
from kiro_crew.acp.transport_framing import write_notification_best_effort
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_OPENCODE,
    ACP_CLIENT_CAPABILITIES,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    METHOD_CANCEL,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_NOTICE,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_HARNESS_NOTICES,
    ACP_BACKENDS_NATIVE_CHILD_CANCEL,
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
#: draft form and its other client-specific branches. ``session.notices`` is promised
#: because every reader of this client reads the ``notice`` the adapter then sends in
#: place of its blockquote text: ``acp.harness_notices`` the session's own, and
#: ``_child_text`` a child's (``ACP_BACKENDS_HARNESS_NOTICES``).
CLAUDE_CLIENT_CAPABILITIES: dict = {
    **ACP_CLIENT_CAPABILITIES,
    "subagents": {},
    "session": {"compaction": {}, "notices": {}},
}

#: ``clientCapabilities`` per ``AcpClient`` backend; a backend with no row declares
#: the shared :data:`ACP_CLIENT_CAPABILITIES` unchanged.
CLIENT_CAPABILITIES_BY_BACKEND: dict[str, dict] = {
    ACP_BACKEND_CLAUDE: CLAUDE_CLIENT_CAPABILITIES,
    ACP_BACKEND_CODEX: {**ACP_CLIENT_CAPABILITIES, "session": {"compaction": {}}},
}

#: The longest JSON-RPC id of a child's request kept for its Stop; a longer one is not
#: recorded (the request is still answered).
MAX_CHILD_REQUEST_ID_CHARS = 128

#: How a native card's id names the child it draws.
_NATIVE_CARD_PREFIX = "native:"

#: The ``idle`` stop reasons that mean the child finished its work.
_IDLE_FINISHED = frozenset({"end_turn", "max_tokens", "max_turn_requests", ""})

#: The run of non-space characters a cut string ends with.
_TRAILING_TOKEN = re.compile(r"\S+\Z")

#: opencode's subagent tool, the title of its first ``tool_call`` frame
#: (``harness_tool_names.OPENCODE_TOOL_IDS_BY_KIRO_TOOL``; measured on 1.19.1).
_OPENCODE_TASK_TOOL = "task"
#: The child's answer inside the text a completed ``task`` row carries.
_OPENCODE_TASK_RESULT = re.compile(r"<task_result>(.*)</task_result>", re.DOTALL)


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
    open tool-call set's evictions are. An opencode session keeps its ``task`` rows'
    children without declaring anything (``_opencode_task_events``); any other session
    that declares nothing keeps no child state, so there is nothing to reset.
    """
    if not (self._native_children_declared() or self.backend == ACP_BACKEND_OPENCODE):
        return
    _start_native_child_turn(self)


def _start_native_child_turn(client: Any) -> None:
    """The turn-start reset of :func:`_reset_native_child_turn`, for a session that
    reads children; the shared runtime's session handle runs the same one."""
    client._native_child_turn.clear()
    # A request is read and answered within the turn that reads it.
    client._native_child_requests.clear()
    if client._native_child_overflow:
        logger.warning(
            "native child store made %d refusal(s) at its bound of %d last turn; "
            "those children got no card, their text was dropped and their tool calls "
            "rendered as ordinary rows",
            client._native_child_overflow,
            NATIVE_CHILD_ROSTER_CAP,
        )
        client._native_child_overflow = 0


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
    if "capabilities" in update:
        # The child's whole grant: a report that names none keeps the last one, and
        # ``null`` (a child the adapter has lost track of) withdraws it.
        granted = update["capabilities"]
        if isinstance(granted, dict) and isinstance(granted.get("cancel"), dict):
            self._native_child_cancellable.add(child_sid)
        else:
            self._native_child_cancellable.discard(child_sid)
    if update.get("title"):
        row["agentName"] = _label(update["title"])
    if update.get("description"):
        row["sessionName"] = row["initialQuery"] = _label(update["description"])
    status = _standard_state_status(update.get("state"))
    if status is not None:
        row["status"] = status
        if status["type"] != "working":
            _forget_native_child_requests(self._native_child_requests, child_sid)
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


_parent_tool_use = parent_tool_use


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
    if self.backend == ACP_BACKEND_OPENCODE:
        return _opencode_task_events(self, msg), False
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
    text = _child_text(self.backend, kind, update)
    if text:
        self._prompt_or_tool_seen = True
        events.append(AcpEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id=child_sid, text=text))
    # Anything else a child sends (its plan, its commands, its session info)
    # describes the child, never the parent.
    return events, True


def _child_text(backend: str, kind: object, update: dict[str, Any]) -> str:
    """The text a child's frame adds to its card, or ``""``.

    A child's reply text, and on a *backend* in ``ACP_BACKENDS_HARNESS_NOTICES`` an
    advisory the adapter addressed to the child (a background task the child started,
    stopped by the user: ``async-tasks.js`` routes it by the task's tool call) in the
    notice's text form (``harness_notices.transcript_block``), as a client without
    ``session.notices`` is sent it as the child's text. A card draws text, not notices.
    """
    if kind in (UPDATE_AGENT_MESSAGE_CHUNK, UPDATE_AGENT_THOUGHT_CHUNK):
        text, is_thinking = parse_text_chunk(update)
        return text if text and not is_thinking else ""
    if kind == UPDATE_NOTICE and backend in ACP_BACKENDS_HARNESS_NOTICES:
        notice = parse_notice(update)
        return notice.transcript if notice is not None else ""
    return ""


def _tool_content_text(content: object) -> str:
    """The first text of a tool row's ``content`` list (``{"type": "content", ...}``)."""
    if not isinstance(content, list):
        return ""
    for block in content:
        inner = block.get("content") if isinstance(block, dict) else None
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            return inner["text"]
    return ""


def _opencode_task_events(client: AcpClient, msg: JsonRpcMessage) -> list[AcpEvent]:
    """Read an opencode ``task`` tool row as its subagent's card, in the claude shape.

    The row's ``tool_call`` (titled ``task``) opens the card, keyed by its
    ``toolCallId`` and admitted by the handle's ``_note_native_child``, so the same
    cap holds. Each frame of that row then sets the agent (``subagent_type``), the
    task (``description``, else ``prompt``) and the end: ``completed`` puts the
    ``<task_result>`` text on the card and closes it, ``failed`` closes it failed. The
    frames are never consumed: the row still renders as the parent's own call.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if not isinstance(update, dict):
        return []
    kind = update.get("sessionUpdate")
    call = update.get("toolCallId")
    if kind not in (UPDATE_TOOL_CALL, UPDATE_TOOL_CALL_UPDATE) or not isinstance(call, str):
        return []
    if kind == UPDATE_TOOL_CALL:
        if update.get("title") != _OPENCODE_TASK_TOOL or not _note_native_child(client, call):
            return []
        client._native_child_rows[call] = {
            "sessionId": call,
            "sessionName": "",
            "agentName": "",
            "initialQuery": "",
            "status": {"type": "working", "message": ""},
        }
    row = client._native_child_rows.get(call)
    if row is None:
        return []
    raw = update.get("rawInput")
    if isinstance(raw, dict):
        if raw.get("subagent_type"):
            row["agentName"] = _label(raw["subagent_type"])
        task = raw.get("description") or raw.get("prompt")
        if task:
            row["sessionName"] = row["initialQuery"] = _label(task)
    events: list[AcpEvent] = []
    status = update.get("status")
    if status == "completed":
        found = _OPENCODE_TASK_RESULT.search(_tool_content_text(update.get("content")))
        answer = found.group(1).strip() if found else ""
        # A row with no task never reaches the roster, so it has no card to draw on.
        if answer and row["initialQuery"]:
            events.append(AcpEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id=call, text=answer))
        row["status"] = {"type": "completed", "message": ""}
    elif status == "failed":
        row["status"] = {
            "type": "failed",
            "message": _label(_tool_content_text(update.get("content"))),
        }
    client._native_child_turn[call] = None
    return events + client._native_child_roster()


def native_child_cancellable(client: Any, child_sid: str) -> bool:
    """Whether a Stop may name *child_sid*: the gate :func:`cancel_native_child` asks.

    A backend in ``ACP_BACKENDS_NATIVE_CHILD_CANCEL``, a child this session tracks
    whose last report granted ``capabilities.cancel`` and left it running
    (``working``), and never this session's own id, which would cancel the parent's
    turn. The shared runtime's session handle asks the same gate.
    """
    row = client._native_child_rows.get(child_sid)
    return not (
        client.backend not in ACP_BACKENDS_NATIVE_CHILD_CANCEL
        or not child_sid
        or child_sid == client._session_id
        or child_sid not in client._native_child_cancellable
        or row is None
        or row["status"].get("type") != "working"
    )


async def cancel_native_child(self: AcpClient, child_sid: str) -> bool:
    """Ask the harness to stop the native child *child_sid*; whether it was asked.

    Sends ``session/cancel`` naming the child, on a backend in
    ``ACP_BACKENDS_NATIVE_CHILD_CANCEL``, for a child this session tracks whose last
    report granted ``capabilities.cancel`` and left it running (``working``: running
    or waiting on a request), and never for this session's own id, which would
    cancel the parent's turn. The turn's own cancel state is left alone, because
    the parent goes on: the child's end comes back as its ``subagent_update``
    (``idle`` with ``cancelled``), which closes its card. Written best effort, as the
    turn's own cancel is; a failed write answers False, so the caller closes the
    card itself.
    """
    if not native_child_cancellable(self, child_sid):
        return False
    process = self._process
    if process is None or process.stdin is None or process.returncode is not None:
        return False
    data = transport_framing.encode_frame(
        {"jsonrpc": "2.0", "method": METHOD_CANCEL, "params": {"sessionId": child_sid}}
    )
    try:
        outcome = await write_notification_best_effort(
            process.stdin,
            self._stdin_write_lock(),
            data,
            bound_secs=transport_framing._RESPONSE_WRITE_BOUND_SECS,
            on_written=self._record_sent,
        )
    except Exception as exc:  # noqa: BLE001 - a Stop that cannot be sent closes the card
        logger.warning("cancelling native child %s failed: %s", child_sid, type(exc).__name__)
        return False
    logger.info("asked the harness to stop native child %s (%s)", child_sid, outcome)
    return True


def _note_native_child_request(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Remember which tracked child the permission or elicitation request *msg* is for.

    A ``session/cancel`` naming a child makes Claude Code withdraw the requests the
    child has open, and the SDK then sends ``$/cancel_request`` for each. This client
    reads nothing while the turn waits on a person's answer, and skips that
    notification when it does read it, so the Stop that sent the cancel settles
    those cards itself (``native_child_requests``). Kept on a backend in
    ``ACP_BACKENDS_NATIVE_CHILD_CANCEL`` for the turn that reads the request, under
    ``NATIVE_CHILD_ROSTER_CAP`` with the oldest dropped first, and dropped when the
    child ends. The child is the one the frame's ``sessionId`` names, or the one the
    Agent-call stamp on its tool call names (``_native_child_of``). Never raises: it
    runs on the permission path, whose answer must not depend on it.
    """
    requests = getattr(self, "_native_child_requests", None)
    if requests is None or msg.id is None:
        return
    try:
        if self.backend not in ACP_BACKENDS_NATIVE_CHILD_CANCEL:
            return
        params = msg.params if isinstance(msg.params, dict) else {}
        tool_call = params.get("toolCall")
        child_sid = self._native_child_of(
            params.get("sessionId"), tool_call if isinstance(tool_call, dict) else {}
        )
        if not child_sid:
            return
        request_id = str(msg.id)
        if len(request_id) > MAX_CHILD_REQUEST_ID_CHARS:
            logger.warning(
                "a native child's request id past %d characters was not recorded",
                MAX_CHILD_REQUEST_ID_CHARS,
            )
            return
        if request_id not in requests and len(requests) >= NATIVE_CHILD_ROSTER_CAP:
            requests.pop(next(iter(requests)))
        requests[request_id] = child_sid
    except Exception:  # noqa: BLE001 - the request is answered whatever this records
        logger.debug("recording a native child's request failed", exc_info=True)


def _forget_native_child_requests(requests: dict[str, str], child_sid: str) -> None:
    """Drop from *requests* the ones recorded for *child_sid*, a child that has ended."""
    for request_id in [rid for rid, sid in requests.items() if sid == child_sid]:
        del requests[request_id]


def native_child_requests(self: AcpClient, child_sid: str) -> tuple[str, ...]:
    """The ids of the permission and elicitation requests *child_sid* asked this turn.

    Strings, as the dashboard keys a request's card. Some may be answered already:
    the caller settles only the cards still waiting.
    """
    return tuple(rid for rid, sid in self._native_child_requests.items() if sid == child_sid)


def native_child_resume_refusal(self: AcpClient, conversation_id: str) -> str | None:
    """The typed ``native_child_not_resumable`` reason when *conversation_id* is one
    of this session's Claude children, else None.

    The same refusal an ``AcpSessionHandle`` gives a kiro-cli or KAS child, read
    from the same child-id set: the continuation and ``POST /api/spawn`` paths ask
    the provider's client, which on the claude backend is this one. An agent of one of
    the session's Claude Code Workflow runs is such a child too, named by its card id
    (``<task_id>:<agent>``, ``native:<task_id>:<agent>`` on the card), running or ended
    (``raw_sdk_tasks.workflow_card_ids``).
    """
    # Imported here rather than at the top: that module's cards import ``_label`` from this one.
    from kiro_crew.acp.raw_sdk_tasks import workflow_card_ids

    if conversation_id.removeprefix(_NATIVE_CARD_PREFIX) in workflow_card_ids(self):
        return native_child_refusal(conversation_id, self._session_id)
    return _handle_resume_refusal(self, conversation_id)


def report_native_children(self: AcpClient, budget: Any) -> int:
    """Report this turn's Claude children to *budget* as uncharged residency,
    returning the count reported.

    The count an ``AcpSessionHandle`` reports, read from this client's turn: the
    children seen this turn plus the ones the cap refused, and the agents of the
    session's Claude Code Workflow runs that still run (``raw_sdk_tasks.live_card_ids``).
    The child-id set is not the count, because its ids outlive the turn and a finished
    child stays in it. The children run inside the parent's adapter process, which is
    already charged, so this reports and never charges. Session health asks the slot's
    client, which on the claude backend is this one.
    """
    # Imported here rather than at the top, as in ``native_child_resume_refusal``.
    from kiro_crew.acp.raw_sdk_tasks import live_card_ids

    count = len(self._native_child_turn) + self._native_child_overflow + len(live_card_ids(self))
    report = getattr(budget, "report_uncharged", None)
    if callable(report):
        report(NATIVE_CHILDREN_UNCHARGED_KIND, count, label=self._session_id)
    return count
