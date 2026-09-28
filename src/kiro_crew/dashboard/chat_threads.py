"""Reply threads on crewmate chat messages: HTTP handlers and the crewmate's turn.

A thread is the set of replies attached to ONE message of a crewmate's chat
(a member-mode slot), addressed by that message's durable ``meta.mid``.
Replies live in a sidecar beside the slot's transcript, owned by
``ConversationLog.read_threads`` / ``append_thread_reply`` (which write under
the transcript's own lock), and never enter ``slot.messages``: the main chat
shows only what the crewmate says to the user, and a thread is where one of
those messages gets discussed.

Posting a reply runs the crewmate's turn in an isolated ``thread:<slot>:<mid>``
session with the parent message, the main-chat context around it and the
thread so far as its envelope; the crewmate's answer lands in the same thread.
The turn runs under the side chat's tool posture -- read-only on the kiro
harness, no tools elsewhere -- because the thread panel has no approval card
to fall back to. Actions still go through the main chat.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew.acp_backends import ACP_BACKENDS_SIDE_READONLY
from kiro_crew.agent_discovery import warm_project_agent_names
from kiro_crew.agent_sdk.host_auth import signed_out_message
from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.side_readonly_spec import ReadOnlySpecError, publish_readonly_spec
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.dashboard.ws import broadcast_thread_reply
from kiro_crew.history import (
    THREAD_MID_RE,
    THREAD_REPLY_ID_RE,
    THREADS_MAX_REPLIES_PER_SIDECAR,
    THREADS_MAX_REPLIES_PER_THREAD,
    ConversationLog,
    HistoryLockTimeout,
    ThreadStoreUnreadable,
)
from kiro_crew.hooks import HookManager
from kiro_crew.llm_helpers import (
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    stream_and_collect,
)
from kiro_crew.members import DM_SLOT_MODE
from kiro_crew.security import StreamRedactor, redact
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_REPLY_BYTES = 32_768
#: The crewmate's stored reply is clipped here. A model can produce far more
#: than a thread bubble can show, and the panel reads the whole sidecar; the
#: streamed frames still carried the full text while it was being written.
_MAX_STORED_REPLY_CHARS = 64_000
_CLIPPED_MARKER = "\n\n[reply clipped]"
#: Replies one thread holds; the store answers 409 past it rather than growing
#: a sidecar the panel reads whole. The reader (``history.read_threads``) cuts
#: any sidecar to these same bounds, so they are spelled once, there.
_MAX_REPLIES_PER_THREAD = THREADS_MAX_REPLIES_PER_THREAD
#: Replies one chat's sidecar holds across every thread -- the whole-file bound.
#: At the 64k clip above that is a few hundred MB at most, and in practice a
#: chat with thousands of thread replies is one nobody is reading in a panel.
_MAX_REPLIES_PER_SIDECAR = THREADS_MAX_REPLIES_PER_SIDECAR
#: Main-chat messages before the parent that travel into the thread envelope.
_CONTEXT_BEFORE_PARENT = 6
_MAX_CONTEXT_LINE_CHARS = 1_500
_MAX_THREAD_LINE_CHARS = 4_000
#: Replies of the thread so far that travel into the envelope: the newest ones,
#: the rest named by count. Every reply cold-starts a session and re-sends the
#: envelope, so a long thread must degrade to elided context, not to a prompt
#: that grows with every exchange.
_THREAD_TAIL_REPLIES = 40

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

THREAD_INSTRUCTIONS = (
    "You are replying in a thread that hangs off ONE message of your chat with "
    "the user. The thread is where that message gets discussed; your reply lands "
    "in the thread, not in the main chat, and the main chat will not see it. "
    "Answer what the thread asks, about the message it was opened on; use the "
    "surrounding chat only as background. Keep replies short and self-contained, "
    "the way a reply in a chat thread reads."
)

THREAD_BOUNDARY_PROMPT = (
    "This thread is read-only: lookups work here, but changes don't. Reading "
    "files, searching, fetching pages and read-only shell commands run without "
    "asking, so use them when a reply needs them. Writing or editing files, "
    "shell commands that modify anything, and MCP tools are refused here, even "
    "when the user asks for them. Never claim that a tool is unconfigured or "
    "suggest enabling it. If the user wants a change made, tell them to ask in "
    "the main chat."
)

THREAD_BOUNDARY_PROMPT_NO_TOOLS = (
    "This thread is context-only: tools are unavailable here, even when the "
    "user asks for them, so reply from the chat and your own knowledge. Never "
    "claim that a tool is unconfigured or suggest enabling it. If tool-backed "
    "work is needed, tell the user to ask in the main chat."
)

#: The visible fallback when the model produced no text -- the same boundary the
#: prompt states, in the user's vocabulary.
_NO_TEXT_FALLBACK_TOOLS = (
    "This thread is read-only: lookups work here, but changes don't. "
    "Ask in the main chat to take action."
)
_NO_TEXT_FALLBACK_NO_TOOLS = (
    "This thread can't use tools on this agent backend. Ask in the main chat to take action."
)
#: One sentence per failure. A retry line only where a retry can succeed.
_FAILED_FALLBACK = "The reply didn't go through. Try again."
_UNREADABLE_FALLBACK = "This chat's threads can't be read right now, so the reply was not kept."
_NO_SPEC_FALLBACK = "This crewmate can't reply in threads right now. Ask in the main chat."
_CREWMATE_GONE_FALLBACK = (
    "This crewmate is no longer available, so the reply was not answered. "
    "Restore the crewmate or choose another one."
)
_FULL_FALLBACK = "This thread is full. Ask in the main chat instead."
_SIDECAR_FULL_FALLBACK = (
    "This chat has no room for more thread replies. Start a new chat to keep discussing."
)
_GONE_FALLBACK = "This chat is gone, so the reply was not kept."
_PARENT_GONE_FALLBACK = "That message is no longer in this chat, so the reply was not kept."
_UNANSWERED_FALLBACK = "This reply was stored but not answered: Kiro Crew stopped. Send it again."


def _awaits_answer(replies: list[dict[str, Any]], index: int) -> bool:
    """Whether the user reply at *index* is still owed the crewmate's answer.

    The last row is; so is one followed only by the "stored but not answered" row a
    stopped turn leaves behind -- that row TELLS the user to send again, and the
    re-send carries the same id, so it has to count as the ask it invites rather
    than as an answered duplicate.
    """
    tail = replies[index + 1 :]
    if not tail:
        return True
    return (
        len(tail) == 1
        and tail[0].get("role") == ROLE_ASSISTANT
        and tail[0].get("content") == _UNANSWERED_FALLBACK
    )


_INTERRUPTED_MARKER = "[reply interrupted: Kiro Crew stopped before it finished]"
_REPLACED_FALLBACK = "This chat was replaced while the reply was being written, so it was not kept."

_PARENT_HEADER = "[The message this thread is on]"
_CONTEXT_HEADER = "[Chat just before that message -- background only]"
_THREAD_HEADER = "[Thread so far]"
_BLOCK_END = "[End]"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unavailable() -> web.Response:
    """503 for every case where the thread store cannot be read or written:
    no conversation log, a damaged sidecar, a lock timeout. One plain sentence;
    the reason is in the log line the caller wrote."""
    return web.json_response(
        {"error": "Threads are unavailable right now.", "code": "threads_unavailable"},
        status=503,
    )


def thread_session_key(slot_key: str, mid: str) -> str:
    """The isolated ACP session one thread runs in. ``sel._infer_source`` and
    ``session._STATELESS_PREFIXES`` both know the ``thread:`` prefix."""
    return f"thread:{slot_key}:{mid}"


def _new_reply(role: str, content: str, reply_id: str | None = None) -> dict[str, Any]:
    return {"id": reply_id or uuid.uuid4().hex, "role": role, "content": content, "ts": _now_iso()}


#: Threads with a crewmate turn in flight (``slot.key + ":" + mid``). Reserved
#: BEFORE the reply handler's first await once the check passed -- the loop is
#: single-threaded, so check-and-reserve with no await between them is atomic --
#: and released by the turn's ``finally`` or by the handler's own failure arms.
_in_flight: set[str] = set()


def _redacted_reply(reply: dict[str, Any]) -> dict[str, Any]:
    """The reply-schema fields of *reply* -- and only those -- with the prose
    re-redacted at the output boundary, as ``chat_pins._redacted_pin`` does: a
    sidecar written under an older redactor is re-run through the current one
    on the way out, and a field the store did not write never leaves."""
    return {
        "id": str(reply.get("id", "")),
        "role": str(reply.get("role", "")),
        "content": redact(str(reply.get("content", ""))),
        "ts": str(reply.get("ts", "")),
    }


def summarize(threads: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Per-parent footer data: reply count, last reply time, who took part.

    ``participants`` lists roles in order of first appearance, so the footer's
    faces read left to right as the thread did.
    """
    out: dict[str, dict[str, Any]] = {}
    for mid, replies in threads.items():
        if not replies:
            continue
        participants: list[str] = []
        for r in replies:
            role = r.get("role")
            if isinstance(role, str) and role not in participants:
                participants.append(role)
        out[mid] = {
            "count": len(replies),
            "last_reply_ts": str(replies[-1].get("ts", "")),
            "participants": participants,
        }
    return out


# ── Parent lookup ─────────────────────────────────────────────────────────────


def _row_mid(row: dict[str, Any]) -> str:
    meta = row.get("meta")
    mid = meta.get("mid") if isinstance(meta, dict) else None
    return mid if isinstance(mid, str) else ""


def _visible(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("role") in (ROLE_USER, ROLE_ASSISTANT)]


async def _transcript(state: DashboardState, slot: _ChatSlot) -> list[dict[str, Any]]:
    """The slot's user/assistant rows, by the rule ``api_chat_slot_detail`` reads
    by: the frozen disk prefix plus the memory window, after the same
    reconciliation the detail and resume handlers run, so a parent that exists
    only on disk (a foreign append, a persistence race) is found rather than
    answered 404. Imported lazily: ``chat_handlers`` is the module every chat
    surface hangs off and imports its helpers back at call time for the same
    reason."""
    from kiro_crew.dashboard.chat_handlers import _reconcile_slot_window

    log = state.conversation_log
    if log is None:
        return _visible(list(slot.messages))
    await _reconcile_slot_window(state, slot)
    older = slot._disk_older_count
    if older <= 0:
        return _visible(list(slot.messages))
    key = slot_history_key(slot)
    try:
        disk = await asyncio.to_thread(log.read_messages_chained, key)
    except Exception:
        logger.warning("read_messages_chained failed for %s", key, exc_info=True)
        disk = []
    return _visible((disk[:older] if disk else []) + list(slot.messages))


def _find_parent(
    rows: list[dict[str, Any]], mid: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The parent row for *mid* and the rows just before it, or ``(None, [])``."""
    for i, row in enumerate(rows):
        if _row_mid(row) == mid:
            start = max(0, i - _CONTEXT_BEFORE_PARENT)
            return row, rows[start:i]
    return None, []


def _parent_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "mid": _row_mid(row),
        "role": row.get("role", ""),
        "content": redact(str(row.get("content", "") or "")),
        "ts": row.get("ts", ""),
    }


# ── Envelope ──────────────────────────────────────────────────────────────────


def _line(role: str, text: str, cap: int) -> str:
    label = "User" if role == ROLE_USER else "You"
    text = text or ""
    if role != ROLE_USER:
        # The envelope leaves the dashboard's own storage (kiro-cli persists it),
        # so the crewmate's earlier words are scrubbed on the way out. Scrub the
        # WHOLE text before cutting it: a credential straddling the cap would
        # otherwise be cut to a fragment the redactor cannot recognise and
        # leave as plain text.
        text = redact(text)
    return f"{label}: {text[:cap]}"


def build_thread_message(
    parent: dict[str, Any],
    context_before: list[dict[str, Any]],
    replies: list[dict[str, Any]],
    text: str,
    *,
    tools_available: bool,
) -> str:
    """The whole envelope, every time: a thread turn always cold-starts its
    session, so nothing carries over between replies."""
    text = text.strip()
    parts: list[str] = [THREAD_INSTRUCTIONS]
    if context_before:
        lines = [
            _line(str(r.get("role", "")), str(r.get("content", "") or ""), _MAX_CONTEXT_LINE_CHARS)
            for r in context_before
        ]
        parts.append(f"{_CONTEXT_HEADER}\n" + "\n".join(lines) + f"\n{_BLOCK_END}")
    parts.append(
        f"{_PARENT_HEADER}\n"
        + _line(
            str(parent.get("role", "")),
            str(parent.get("content", "") or ""),
            _MAX_THREAD_LINE_CHARS,
        )
        + f"\n{_BLOCK_END}"
    )
    # The reply being answered is the last one in the store; the block holds
    # the ones before it.
    prior = replies[:-1] if replies and replies[-1].get("role") == ROLE_USER else replies
    if prior:
        elided = max(0, len(prior) - _THREAD_TAIL_REPLIES)
        lines = [
            _line(str(r.get("role", "")), str(r.get("content", "") or ""), _MAX_THREAD_LINE_CHARS)
            for r in prior[elided:]
        ]
        if elided:
            lines.insert(0, f"[{elided} earlier {'reply' if elided == 1 else 'replies'} not shown]")
        parts.append(f"{_THREAD_HEADER}\n" + "\n".join(lines) + f"\n{_BLOCK_END}")
    parts.append(THREAD_BOUNDARY_PROMPT if tools_available else THREAD_BOUNDARY_PROMPT_NO_TOOLS)
    parts.append(f"User: {text}")
    return "\n\n".join(parts)


# ── Handlers ──────────────────────────────────────────────────────────────────


def _deny_foreign_app(request: web.Request, slot: _ChatSlot, operation: str) -> web.Response | None:
    """App tokens see only their own slots (App Kit §5.2); a crewmate chat is
    never app-owned, so an app caller gets the anti-enumeration 404."""
    request_app = request.get("app", "")
    if not request_app:
        return None
    if slot._app and request_app == slot._app:
        return None
    try:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="app does not own this slot",
        )
    except Exception:  # noqa: BLE001 -- the refusal must reach the caller regardless
        logger.debug("SEL audit unavailable for thread refusal", exc_info=True)
    return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)


async def threads_enabled() -> bool:
    """Whether ``dashboard.crewmate_threads`` is on, read live.

    The watcher's snapshot when it has one (a plain attribute read), else a disk
    load off the loop. The flag is off by default; nothing about a thread is
    served, stored or sent while it is off.
    """
    from kiro_crew.config import live

    cfg = live.snapshot()
    if cfg is None:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    return bool(cfg.dashboard.crewmate_threads)


def _feature_off() -> web.Response:
    # The same shape the app-isolation refusal answers with, so a caller cannot
    # tell "threads are off" from "no such slot": the feature's presence is not
    # enumerable.
    return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)


def _resolve_slot(
    request: web.Request, state: DashboardState, slot_key: str, operation: str
) -> tuple[_ChatSlot | None, web.Response | None]:
    """The member-mode slot a thread request names, or the response refusing it."""
    if not slot_key:
        return None, web.json_response(
            {"error": "slot query param required", "code": "missing_query_params"}, status=400
        )
    slot = state.get_slot(slot_key)
    if slot is None:
        return None, web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    denied = _deny_foreign_app(request, slot, operation)
    if denied is not None:
        return None, denied
    if slot.mode != DM_SLOT_MODE:
        return None, web.json_response(
            {
                "error": "Threads live on a crewmate's chat.",
                "code": "not_crewmate_chat",
            },
            status=409,
        )
    return slot, None


def _valid_mid(mid: str) -> bool:
    """A minted row id (``mint_row_mid``) and nothing else -- the one shape a
    thread key may have, at the route and in the store alike."""
    return bool(THREAD_MID_RE.match(mid or ""))


async def _read_threads(
    state: DashboardState, slot: _ChatSlot
) -> tuple[dict[str, list[dict[str, Any]]] | None, web.Response | None]:
    """The slot's thread map, or the 503 that stands in for it.

    No conversation log, or a sidecar whose bytes are not a thread map, both
    answer the same plain sentence: the panel cannot show replies it cannot
    read, and a damaged sidecar is never overwritten to make the error go away.
    """
    log = state.conversation_log
    if log is None:
        return None, _unavailable()
    try:
        return await asyncio.to_thread(log.read_threads, slot_history_key(slot)), None
    except ThreadStoreUnreadable:
        logger.warning("thread sidecar unreadable for slot=%s", slot.key, exc_info=True)
        return None, _unavailable()


async def api_chat_threads_summary(request: web.Request) -> web.Response:
    """GET /api/chat/threads?slot=<key> -- reply counts per parent message.

    A separate read, not folded into the slot's message list: the transcript
    read path stays unchanged, and the footer data is small enough to fetch
    beside it.
    """
    if not await threads_enabled():
        return _feature_off()
    state: DashboardState = request.app["state"]
    slot, refused = _resolve_slot(
        request, state, request.query.get("slot", ""), "chat.threads_summary"
    )
    if refused is not None:
        return refused
    assert slot is not None
    threads, refused = await _read_threads(state, slot)
    if refused is not None:
        return refused
    assert threads is not None
    return web.json_response({"threads": summarize(threads)})


async def api_chat_thread_detail(request: web.Request) -> web.Response:
    """GET /api/chat/threads/{mid}?slot=<key> -- one thread: parent + replies."""
    if not await threads_enabled():
        return _feature_off()
    state: DashboardState = request.app["state"]
    mid = request.match_info["mid"]
    if not _valid_mid(mid):
        return web.json_response({"error": "invalid mid", "code": "invalid_mid"}, status=400)
    slot, refused = _resolve_slot(
        request, state, request.query.get("slot", ""), "chat.thread_detail"
    )
    if refused is not None:
        return refused
    assert slot is not None
    rows = await _transcript(state, slot)
    parent, _ = _find_parent(rows, mid)
    if parent is None:
        return web.json_response(
            {"error": "That message is no longer in this chat.", "code": "parent_not_found"},
            status=404,
        )
    threads, refused = await _read_threads(state, slot)
    if refused is not None:
        return refused
    assert threads is not None
    return web.json_response(
        {
            "parent": _parent_payload(parent),
            "replies": [_redacted_reply(r) for r in threads.get(mid, [])],
            "in_flight": f"{slot.key}:{mid}" in _in_flight,
        }
    )


async def api_chat_thread_reply(request: web.Request) -> web.Response:
    """POST /api/chat/threads/{mid}/reply -- ``{slot_key, text, reply_id?}``.

    Stores the user's reply, answers 202 with it, and runs the crewmate's turn
    in the background; the crewmate's reply arrives over ``chat.thread_reply``.
    ``reply_id`` (32 hex, minted by the panel per send) makes the send
    idempotent: a client that never saw the 202 -- the response lost on the
    wire -- re-sends the same id and gets the stored reply back, never a second
    row and a second turn.
    """
    if not await threads_enabled():
        return _feature_off()
    state: DashboardState = request.app["state"]
    mid = request.match_info["mid"]
    if not _valid_mid(mid):
        return web.json_response({"error": "invalid mid", "code": "invalid_mid"}, status=400)
    # The shared 64 KB ceiling: a reply body is one short text field plus a slot
    # key, and the text itself is capped at 32 KiB below.
    body, body_error = await read_bounded_json(request)
    if body_error is not None:
        return body_error
    assert body is not None
    slot_key = body.get("slot_key")
    text = body.get("text")
    if not isinstance(slot_key, str) or not isinstance(text, str):
        return web.json_response(
            {"error": "slot_key and text are required", "code": "missing_required_fields"},
            status=400,
        )
    reply_id = body.get("reply_id")
    if reply_id is not None and not (
        isinstance(reply_id, str) and THREAD_REPLY_ID_RE.match(reply_id)
    ):
        return web.json_response(
            {"error": "invalid reply_id", "code": "invalid_reply_id"}, status=400
        )
    text = text.strip()
    if not text:
        return web.json_response(
            {"error": "Write a reply first.", "code": "empty_reply"}, status=400
        )
    try:
        text_bytes = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        # JSON admits a lone surrogate ("\ud800") as a Python str that no
        # encoding can carry: a validation failure, not a crash.
        return web.json_response(
            {"error": "That reply has characters that can't be sent.", "code": "invalid_text"},
            status=400,
        )
    if text_bytes > _MAX_REPLY_BYTES:
        return web.json_response(
            {
                "error": "That reply is too long. Replies are capped at 32 KB.",
                "code": "reply_too_long",
            },
            status=413,
        )
    slot, refused = _resolve_slot(request, state, slot_key, "chat.thread_reply")
    if refused is not None:
        return refused
    assert slot is not None
    log = state.conversation_log
    if log is None:
        return _unavailable()
    history_key = slot_history_key(slot)
    # The transcript this reply is admitted against. Captured BEFORE the parent
    # lookup, so it is never younger than the rows the parent was found in: a
    # member chat deleted and recreated under the same key after this line is
    # told apart at the store write (identity mismatch, or -- for a transcript
    # whose metadata predates the field -- the parent's mid not on disk), and
    # one replaced before it is simply the chat the parent belongs to. Handed
    # to both appends so the crewmate's reply is held to the same transcript.
    identity = await asyncio.to_thread(log.thread_transcript_identity, history_key)
    rows = await _transcript(state, slot)
    parent, context_before = _find_parent(rows, mid)
    if parent is None:
        return web.json_response(
            {"error": "That message is no longer in this chat.", "code": "parent_not_found"},
            status=404,
        )
    flight_key = f"{slot.key}:{mid}"
    if reply_id is not None:
        # A re-send of a reply the store already holds (the 202 was lost).
        # Answered, or its turn still running: hand the stored row back and do
        # nothing else. Stored as the LAST row with no turn active -- the turn
        # failed, or a restart took it before an answer landed -- the re-send is
        # the user asking again: run the turn for the stored reply, without
        # storing or broadcasting it a second time.
        try:
            stored_replies = (await asyncio.to_thread(log.read_threads, history_key)).get(mid, [])
        except ThreadStoreUnreadable:
            return _unavailable()
        for index, existing in enumerate(stored_replies):
            if existing.get("id") != reply_id:
                continue
            unanswered = _awaits_answer(stored_replies, index)
            if unanswered and flight_key not in _in_flight:
                _in_flight.add(flight_key)
                run_id = uuid.uuid4().hex
                task = asyncio.create_task(
                    _run_thread_turn(
                        state,
                        slot,
                        mid,
                        run_id,
                        str(existing.get("content", "")),
                        parent,
                        context_before,
                        flight_key,
                        identity,
                    )
                )
                state._background_tasks.add(task)
                task.add_done_callback(state._background_tasks.discard)
                return web.json_response(
                    {"reply": _redacted_reply(existing), "run_id": run_id, "duplicate": True},
                    status=202,
                )
            return web.json_response(
                {"reply": _redacted_reply(existing), "run_id": "", "duplicate": True},
                status=202,
            )
    if flight_key in _in_flight:
        return web.json_response(
            {
                "error": f"{slot.agent or 'Your crewmate'} is still replying. Wait for that reply.",
                "code": "thread_turn_in_flight",
            },
            status=409,
        )
    # Reserve NOW, with no await between the check above and this line: the
    # store write below suspends, and two replies racing through it (a
    # double-click) would otherwise both pass the check and run two turns on
    # one thread. The reservation is released by the one ``finally`` below on
    # every path that does not hand it to the turn -- a refusal, and an error
    # the store write raises that nothing here names (an ``OSError`` from the
    # sidecar write) -- so no failure can leave the thread refusing replies
    # until restart.
    _in_flight.add(flight_key)
    launched = False
    try:
        reply = _new_reply(ROLE_USER, text, reply_id)
        try:
            # One seat below each cap: a user reply is admitted only while the
            # crewmate's answer to it still fits, so the answer the turn then
            # writes (under the full caps) is never the reply that finds the
            # thread full.
            outcome = await _append_user_reply(log, history_key, mid, reply, identity)
        except ThreadStoreUnreadable:
            logger.warning("thread sidecar unreadable for slot=%s", slot.key, exc_info=True)
            return _unavailable()
        except HistoryLockTimeout:
            logger.warning("thread store lock timeout for slot=%s", slot.key)
            return _unavailable()
        except OSError:
            # The sidecar could not be written (disk full, a permission
            # change): the store is unavailable, the same answer an unreadable
            # sidecar gets, not an unhandled 500.
            logger.warning("thread sidecar write failed for slot=%s", slot.key, exc_info=True)
            return _unavailable()
        if outcome == "duplicate":
            # Raced with the same re-send: the store has it once; hand that back.
            stored_replies = (await asyncio.to_thread(log.read_threads, history_key)).get(mid, [])
            existing = next((r for r in stored_replies if r.get("id") == reply["id"]), reply)
            return web.json_response(
                {"reply": _redacted_reply(existing), "run_id": "", "duplicate": True},
                status=202,
            )
        if outcome == "full":
            return web.json_response(
                {"error": _FULL_FALLBACK, "code": "thread_full"},
                status=409,
            )
        if outcome == "sidecar_full":
            return web.json_response(
                {"error": _SIDECAR_FULL_FALLBACK, "code": "threads_full"},
                status=409,
            )
        if outcome == "replaced":
            return web.json_response(
                {
                    "error": "This chat was replaced. Open it again to reply.",
                    "code": "transcript_replaced",
                },
                status=409,
            )
        if outcome != "ok":
            # No transcript on disk yet (a chat younger than its first flush),
            # a parent row the slot has not flushed yet ("unflushed"), or a
            # transcript deleted under us: nothing durable to attach a thread to.
            return web.json_response(
                {
                    "error": "This chat isn't ready for threads yet. Try again in a moment.",
                    "code": "transcript_missing",
                },
                status=409,
            )
        run_id = uuid.uuid4().hex
        broadcast_thread_reply(
            state,
            slot_key=slot.key,
            mid=mid,
            run_id=run_id,
            role=ROLE_USER,
            content=text,
            reply=reply,
        )
        task = asyncio.create_task(
            _run_thread_turn(
                state, slot, mid, run_id, text, parent, context_before, flight_key, identity
            )
        )
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
        launched = True
        return web.json_response({"reply": reply, "run_id": run_id}, status=202)
    finally:
        if not launched:
            _in_flight.discard(flight_key)


async def _append_user_reply(
    log: ConversationLog, history_key: str, mid: str, reply: dict[str, Any], identity: str | None
) -> str:
    """Store the user's reply off the loop, cancellation-safe.

    The worker thread keeps running when the awaiting handler is cancelled (a
    gateway shutdown mid-request), so a plain ``to_thread`` could commit the
    reply AFTER the handler has given up on it: stored, never answered, and the
    reservation the ``finally`` releases would never launch a turn for it. So
    the append is shielded and, on cancellation, drained: if it committed, a
    terminal assistant row saying the reply was not answered is written beside
    it (under the full caps, the seat the user reply left), so the thread the
    user reopens says what happened instead of showing a question with no
    answer. The cancellation is then re-raised.
    """
    pending = asyncio.ensure_future(
        asyncio.to_thread(
            log.append_thread_reply,
            history_key,
            mid,
            reply,
            max_replies=_MAX_REPLIES_PER_THREAD - 1,
            max_total=_MAX_REPLIES_PER_SIDECAR - 1,
            expected_created_at=identity,
        )
    )
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        try:
            outcome = await pending
        except Exception:
            raise asyncio.CancelledError() from None
        if outcome == "ok":
            try:
                await asyncio.to_thread(
                    log.append_thread_reply,
                    history_key,
                    mid,
                    _new_reply(ROLE_ASSISTANT, _UNANSWERED_FALLBACK),
                    max_replies=_MAX_REPLIES_PER_THREAD,
                    max_total=_MAX_REPLIES_PER_SIDECAR,
                    expected_created_at=identity,
                )
            except Exception:
                logger.warning(
                    "thread reply committed during cancellation; failure row not written "
                    "for mid=%s",
                    mid,
                    exc_info=True,
                )
        raise


async def _persist_interrupted(
    state: DashboardState,
    slot: _ChatSlot,
    mid: str,
    history_key: str,
    chunks: list[str],
    identity: str | None,
) -> None:
    """Best-effort store of an interrupted turn's answer (see the caller)."""
    log = state.conversation_log
    if log is None:
        return
    partial = redact("".join(chunks)).strip()
    if partial:
        text = f"{partial[:_MAX_STORED_REPLY_CHARS]}\n\n{_INTERRUPTED_MARKER}"
    else:
        text = _UNANSWERED_FALLBACK
    try:
        await asyncio.to_thread(
            log.append_thread_reply,
            history_key,
            mid,
            _new_reply(ROLE_ASSISTANT, text),
            max_replies=_MAX_REPLIES_PER_THREAD,
            max_total=_MAX_REPLIES_PER_SIDECAR,
            expected_created_at=identity,
        )
    except Exception:
        logger.warning(
            "Thread turn cancelled; interrupted answer not stored for slot=%s mid=%s",
            slot.key,
            mid,
            exc_info=True,
        )


# ── The crewmate's turn ───────────────────────────────────────────────────────


def _is_auth_required(exc: BaseException) -> bool:
    """Whether *exc* is the harness's signed-out error.

    Matched by class name: the ACP exception type lives behind the agent-SDK
    import boundary, which application code may not cross, and the SDK exports
    no auth-required type of its own yet. ``stream_and_collect`` raises the
    harness's own error, so the name is the one stable handle.
    """
    return type(exc).__name__ == "AcpAuthRequired"


async def _run_thread_turn(
    state: DashboardState,
    slot: _ChatSlot,
    mid: str,
    run_id: str,
    text: str,
    parent: dict[str, Any],
    context_before: list[dict[str, Any]],
    flight_key: str,
    identity: str | None,
) -> None:
    """Background task: one crewmate reply into the thread on *mid*.

    The shape is the side turn's (``handlers/side._run_side_turn``) without its
    steer/queue ledger: resolve the slot's agent, derive the read-only spec on
    the kiro harness, run the envelope in the thread's own session, store and
    broadcast the answer. Every failure arm broadcasts a plain-language final
    frame so the panel never waits on a reply that will not come, and a reply
    the store refused is published as the failure it is, never as a reply.
    """
    session_key = thread_session_key(slot.key, mid)
    history_key = slot_history_key(slot)
    chunks: list[str] = []
    # Set only when a cancellation lands on the final store write that committed.
    answer_committed = False
    # Rolling-buffer redactor for the live stream, as the side chat's: per-frame
    # redaction alone misses a secret split across chunk boundaries.
    wsred = StreamRedactor()

    def _on_chunk(text: str) -> None:
        chunks.append(text)
        safe = wsred.feed(text)
        if safe:
            broadcast_thread_reply(
                state, slot_key=slot.key, mid=mid, run_id=run_id, role=ROLE_ASSISTANT, content=safe
            )

    def _final(
        content: str, *, is_error: bool = False, reply: dict[str, Any] | None = None
    ) -> None:
        broadcast_thread_reply(
            state,
            slot_key=slot.key,
            mid=mid,
            run_id=run_id,
            role=ROLE_ASSISTANT,
            content=content,
            is_error=is_error,
            final=True,
            ts=time.time(),
            reply=reply,
        )

    acquired_key = ""
    backend: str | None = None
    try:
        log = state.conversation_log
        if log is None:
            _final(_FAILED_FALLBACK, is_error=True)
            return
        project: str | None = slot.project or None
        slot_agent: str | None = slot.agent or None
        kiro_agent: str | None = None
        hooks: HookManager | None = (
            state.context_builder.hooks if state.context_builder is not None else None
        )
        try:
            # The config load reads files: off the loop. The resolver stays
            # inline on purpose, as the main chat's and the side chat's do --
            # offloading it would swallow a StopIteration into a Future and
            # hang the await; it reads only the warmed in-memory snapshot.
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            backend = cfg.agent.acp_backend
            await warm_project_agent_names(
                project or "", operation="thread_reply", source="dashboard"
            )
            # No memory-file validation: that path opens the member's SQLite
            # database synchronously, and this resolver runs inline on the loop
            # (see the note above), so a locked database would stall every
            # gateway task for the busy timeout. The main chat's resolvers pass
            # the same flag; only the kiro agent name is needed here.
            bindings = resolve_agent_bindings(cfg, slot_agent, project, validate_memory_files=False)
            if slot_agent and not slot._app and not bindings.requested_resolved:
                # The crewmate this slot belongs to does not resolve (deleted
                # or renamed while its chat stayed open). The resolver falls
                # back to the default agent; answering with it here would store
                # a stranger's words under the crewmate's name, with no marker.
                # The main chat refuses this state; so does the thread.
                logger.warning(
                    "Thread turn: crewmate %r no longer resolves for slot=%s; not replying",
                    slot_agent,
                    slot.key,
                )
                _final(_CREWMATE_GONE_FALLBACK, is_error=True)
                return
            kiro_agent = bindings.kiro_agent
        except Exception:
            logger.warning(
                "Thread turn: failed to resolve agent bindings for slot=%s; using raw slot.agent",
                slot.key,
                exc_info=True,
            )
        tools_available = backend is not None and backend in ACP_BACKENDS_SIDE_READONLY
        if tools_available:
            base_agent = kiro_agent or slot_agent or "kirocrew"
            published = await asyncio.to_thread(publish_readonly_spec, base_agent, project)
            agent: str | None = published.name
            approval_policy = ToolApprovalPolicy.READ_ONLY
        else:
            agent = kiro_agent or slot_agent
            approval_policy = ToolApprovalPolicy.REJECT_ALL

        # A thread turn never reuses a session: the one it acquires is destroyed
        # in the finally below, so every reply cold-starts under the agent, the
        # project and the derived spec resolved THIS turn and receives the whole
        # envelope. A retained session would stay bound to the agent and cwd of
        # the turn that created it after the slot's project or agent changed.
        provider, _is_new, _resumed = await state.sessions.get_or_create(
            session_key, agent=agent, cwd=project
        )
        acquired_key = session_key
        threads = await asyncio.to_thread(log.read_threads, history_key)
        message = build_thread_message(
            parent,
            context_before,
            threads.get(mid, []),
            text,
            tools_available=tools_available,
        )
        try:
            response_text = redact(
                await stream_and_collect(
                    provider,
                    message,
                    approval_policy=approval_policy,
                    hooks=hooks,
                    session_key=session_key,
                    agent=slot_agent or "kirocrew",
                    app=slot._app or "",
                    on_chunk=_on_chunk,
                )
            )
        except PromptBusyExhaustedError:
            logger.warning("Thread turn aborted (prompt busy): slot=%s mid=%s", slot.key, mid)
            _final(_FAILED_FALLBACK, is_error=True)
            return
        if not chunks:
            response_text = (
                _NO_TEXT_FALLBACK_TOOLS if tools_available else _NO_TEXT_FALLBACK_NO_TOOLS
            )
        if len(response_text) > _MAX_STORED_REPLY_CHARS:
            response_text = response_text[:_MAX_STORED_REPLY_CHARS] + _CLIPPED_MARKER
        stored = _new_reply(ROLE_ASSISTANT, response_text)
        # The store write runs on a worker thread that a cancellation does not
        # stop: shielded and drained, so a cancel that lands while the full
        # answer is committing is known as such and the interruption row is
        # not written beside an answer the thread already holds.
        pending = asyncio.ensure_future(
            asyncio.to_thread(
                log.append_thread_reply,
                history_key,
                mid,
                stored,
                max_replies=_MAX_REPLIES_PER_THREAD,
                max_total=_MAX_REPLIES_PER_SIDECAR,
                expected_created_at=identity,
            )
        )
        try:
            outcome = await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                answer_committed = (await pending) == "ok"
            except Exception:
                answer_committed = False
            raise
        if outcome == "ok":
            _final(response_text, reply=stored)
        elif outcome == "full":
            _final(_FULL_FALLBACK, is_error=True)
        elif outcome == "sidecar_full":
            _final(_SIDECAR_FULL_FALLBACK, is_error=True)
        elif outcome == "replaced":
            _final(_REPLACED_FALLBACK, is_error=True)
        elif outcome == "unflushed":
            # The parent row left the transcript while the model wrote (a
            # rewind past it): nothing durable to hang the answer off.
            _final(_PARENT_GONE_FALLBACK, is_error=True)
        else:
            _final(_GONE_FALLBACK, is_error=True)
    except asyncio.CancelledError:
        # A gateway restart cancels this task mid-stream. The panel's live row
        # is dropped on reconnect, so whatever streamed would be lost with it:
        # persist the redacted partial answer under an interruption marker (or
        # the "not answered" row when nothing streamed) so the reopened thread
        # says what happened, then let the cancellation through. Not when the
        # cancel landed on the final write and the whole answer committed: the
        # thread already holds it, and a row saying it was interrupted would lie.
        if not answer_committed:
            await _persist_interrupted(state, slot, mid, history_key, chunks, identity)
        raise
    except ThreadStoreUnreadable:
        logger.warning(
            "Thread turn: sidecar unreadable for slot=%s mid=%s", slot.key, mid, exc_info=True
        )
        _final(_UNREADABLE_FALLBACK, is_error=True)
    except HistoryLockTimeout:
        logger.warning("Thread turn: store lock timeout for slot=%s mid=%s", slot.key, mid)
        _final(_FAILED_FALLBACK, is_error=True)
    except ReadOnlySpecError as exc:
        logger.warning(
            "Thread turn refused: read-only agent spec unavailable (%s) for slot=%s mid=%s: %s",
            exc.code,
            slot.key,
            mid,
            exc.detail,
        )
        # A permission refusal, audited like the app-isolation one: the turn
        # was denied the read-only posture it must run under. The audit is
        # best-effort here: an audit subsystem that failed at boot must not
        # also cost the panel its terminal frame.
        try:
            sel().log_api_access(
                caller=slot._app or "dashboard",
                operation="thread_reply",
                outcome="denied",
                source="read_only_spec",
                resources=f"slot={slot.key}",
                error=f"{exc.code}: {exc.detail}",
            )
        except Exception:
            logger.warning(
                "Thread turn: read-only-spec refusal not audited for slot=%s",
                slot.key,
                exc_info=True,
            )
        _final(_NO_SPEC_FALLBACK, is_error=True)
    except Exception as exc:
        if _is_auth_required(exc):
            # A signed-out harness is actionable: say what to do, and latch the
            # readiness service signed-out as the main chat does.
            logger.warning("Thread turn auth required: slot=%s mid=%s", slot.key, mid)
            from kiro_crew.dashboard.chat_runner import _mark_kiro_signed_out

            _mark_kiro_signed_out(state)
            _final(signed_out_message(backend or ""), is_error=True)
        else:
            logger.exception("Thread turn failed: slot=%s mid=%s run_id=%s", slot.key, mid, run_id)
            _final(_FAILED_FALLBACK, is_error=True)
    finally:
        _in_flight.discard(flight_key)
        if acquired_key:
            try:
                state.sessions.release(acquired_key)
                await state.sessions.destroy(acquired_key)
            except Exception:
                logger.debug("Failed to end thread session %s", acquired_key, exc_info=True)
