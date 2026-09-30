"""Threads: an ordinary chat session anchored to one message of another conversation.

A thread is not a container. It is a RELATION -- ``(surface, conversation,
message_id)`` -- between one message and one ordinary session that hangs off it.
The session is minted through the same core ``session_create`` uses, so it has
everything a chat surface has by construction: tools, approval cards, a model, a
steer channel, a queue, memory, a transcript and a place in the sidebar.

Two halves live here:

* the **anchor index** (``ConversationLog.read_thread_anchors`` and friends), a
  sidecar beside the parent's transcript recording which slot each message's
  thread lives in, so a conversation can list its threads in one read; and
* :func:`open_thread`, the one coroutine both openers call -- the user clicking a
  message (``POST /api/chat/threads/{mid}/open``) and an agent opening a thread on
  the message it is answering (MCP ``thread_open``).

Opening never touches the running turn. It holds no semaphore, does not gate on
``has_active_turn()``, and is not a mid-turn message primitive: it mints a
SIBLING session and takes a non-consuming SNAPSHOT of the parent's partial text
(:func:`in_flight_snapshot`). A streaming assistant row has no ``mid`` yet -- ids
are minted post-turn -- so a thread opened on one anchors to the user message
that started that turn, which is where the work was asked for anyway, and the
partial reply travels into the seed as quoted context.

Version 1's threads (replies in the sidecar, answered by a cold read-only
``thread:<slot>:<mid>`` session) are read in place and never rewritten. They
render as a read-only fold; nothing migrates them, because replaying them as a
session's transcript would assert a history that session never had.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard import thread_projection
from kiro_crew.dashboard.chat_utils import _collapse_wire_rows, slot_history_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.dashboard.ws import broadcast_thread_anchor
from kiro_crew.history import (
    THREAD_ANCHOR_TITLE_MAX_CHARS,
    THREAD_MID_RE,
    THREADS_MAX_ANCHORS_PER_SIDECAR,
    HistoryLockTimeout,
    ThreadStoreUnreadable,
)
from kiro_crew.messaging.link import SURFACE_DASHBOARD, ThreadAnchor
from kiro_crew.security import redact
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

#: The close card's role. Display-only on purpose: an ``assistant`` row would sit
#: in :data:`kiro_crew.context.RECALL_ROLES`, and replay keeps role and content
#: alone -- so the card's text would reach the parent's next cold turn as the
#: crewmate's own earlier words. A role outside that set is dropped from replay
#: whole, which is the accurate account: nothing was said.
ROLE_THREAD_CLOSED = "thread_closed"

#: Rows that make a retracted thread session somebody's conversation rather than
#: an empty shell, so the retraction archives it instead of deleting it. A chunk
#: counts: a turn is already answering, and its text is the person's answer even
#: before the row persists. See ``_retract``.
_RETRACT_KEEPING_ROLES = frozenset({ROLE_USER, ROLE_ASSISTANT, "chunk", "streaming"})

#: How much of the parent's in-flight reply travels into the seed. A snapshot is
#: context, not a transcript: enough for the thread to know what it is discussing,
#: bounded so a long stream cannot make the seed the biggest message in the
#: session. The parent keeps the whole text -- this cuts only the quote.
_MAX_SNAPSHOT_CHARS = 4_000
#: Summary-card prose for a closed thread.
#:
#: Prose and not a bracketed marker. ``ThreadClosedCard`` renders this row from its
#: ``meta.thread_summary`` and never shows the text, but the sidebar's session-card
#: preview has no renderer and shows the raw content -- so a ``[Thread closed]``
#: here surfaced as a leaked token in the one place nothing was styling it. Worded
#: exactly as the card's own heading (``pages.chat.thread.card_ended``) so the two
#: places a reader meets this state cannot drift into different words for it.
_CLOSE_CARD_HEADER = "Thread ended."

#: Parent rows one ``thread_context_read`` may return. The answer lands in a
#: model's context, so an uncapped read of a long parent would be exactly the
#: verbatim history the projection design exists to avoid -- a caller that wants
#: more pages for it.
_CONTEXT_READ_MAX_ROWS = 40
#: Total characters one context read may return. The row cap bounds how many rows
#: come back and this bounds how big they may be together, so a span of long pastes
#: costs the caller a page rather than its whole context window.
_CONTEXT_READ_MAX_CHARS = 24_000


#: The shapes ``opened_by`` may take, mirroring the store's own rule so an opener
#: is normalized before the write rather than refused by it.
_OPENED_BY_RE = re.compile(r"^(?:user|agent:[A-Za-z0-9_:.\-]{1,200})$")


# ── The anchor ────────────────────────────────────────────────────────────────


#: The dashboard's name for the neutral anchor. ONE type across every surface --
#: it lives in ``messaging/link.py`` beside ``ChannelLink``, which is what it is
#: plus a message id -- so a Slack anchor and a dashboard anchor are the same
#: object with a different ``surface``, and neither can drift into its own shape.
#: The dashboard's extra rule, that its own ``mid`` is a minted row id, is checked
#: by :func:`_valid_mid` at the route and again in :func:`open_thread`; the type
#: itself holds every surface's ids to a bounded opaque shape and no more, because
#: a type that pattern-matched one surface's spelling would refuse the others.
Anchor = ThreadAnchor


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_mid(mid: str) -> bool:
    """A minted row id (``mint_row_mid``) and nothing else -- the one shape an
    anchor's message id may have, at the route and in the store alike."""
    return bool(THREAD_MID_RE.match(mid or ""))


def _unavailable() -> web.Response:
    """503 for every case where the anchor index cannot be read or written: no
    conversation log, a damaged sidecar, a lock timeout. One plain sentence; the
    reason is in the log line the caller wrote."""
    return web.json_response(
        {"error": "Threads are unavailable right now.", "code": "threads_unavailable"},
        status=503,
    )


# ── The in-flight snapshot ────────────────────────────────────────────────────


def in_flight_snapshot(slot: _ChatSlot) -> str:
    """The parent's still-streaming assistant text, copied and never consumed.

    The dashboard does not run turns through ``TurnDriver``, so there is no
    driver-side accumulator to ask. The text lives on the SLOT: ``chat_runner``
    appends one ``role="chunk"`` row per delta to ``slot.messages`` (and one more
    for the redactor's withheld tail at each segment flush), broadcasting the same
    bytes as a ``chat_chunk`` frame. The turn's own ``assistant_text`` local is
    unreachable from here and is reset at every tool boundary, so the chunk rows
    are both the readable copy and the complete one for the segment in flight.

    Three properties make this safe to call on a live turn:

    * the list reference is COPIED first, and ``purge_chunks`` REBINDS
      ``slot.messages`` rather than mutating it, so a segment finalizing under
      this read cannot empty the copy;
    * ``_collapse_wire_rows`` never mutates its input dicts (they are shared with
      the window the event loop appends to) -- it returns a fresh merged row; and
    * nothing here touches ``slot._pending``, the queue a live SSE or
      OpenAI-compat reader owns. ``release_pending_chunks`` and ``purge_chunks``
      are the consuming reads and are deliberately not called.

    Returns ``""`` when no text is streaming, which is the ordinary case for a
    thread opened on a finished message.
    """
    rows = list(slot.messages)
    text = "".join(
        str(row.get("content", "") or "")
        for row in _collapse_wire_rows(rows)
        if row.get("role") == "chunk"
    )
    return redact(text)


# ── Summaries (the footer's data, recast over anchors) ─────────────────────────


def summarize(
    anchors: dict[str, dict[str, Any]],
    legacy: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-message footer data for one conversation.

    Two kinds of row fold into one map, because the footer renders one badge per
    message and does not care which era the thread came from:

    * an ANCHOR gives ``{kind: "session", thread_slot, title, opened_by,
      opened_at, closed_at, summary_mid}``;
    * a LEGACY v1 thread gives ``{kind: "legacy", count, last_reply_ts,
      participants}`` -- the shape version 1's footer already read, so
      ``ThreadFooter`` keeps working while worker (b) is in flight.

    An anchor wins when a message has both: the live thread is the one to open,
    and the legacy replies are still reachable through the fold.
    """
    out: dict[str, dict[str, Any]] = {}
    for mid, replies in (legacy or {}).items():
        if not replies:
            continue
        participants: list[str] = []
        for reply in replies:
            role = reply.get("role")
            if isinstance(role, str) and role not in participants:
                participants.append(role)
        out[mid] = {
            "kind": "legacy",
            "count": len(replies),
            "last_reply_ts": str(replies[-1].get("ts", "")),
            "participants": participants,
        }
    for mid, anchor in anchors.items():
        out[mid] = {
            "kind": "session",
            "thread_slot": str(anchor.get("thread_slot", "")),
            # The one free-text field an anchor row holds, redacted at the output
            # boundary as the reply content was: a title written under an older
            # redactor is re-run through the current one on the way out.
            "title": redact(str(anchor.get("title", ""))),
            "opened_by": str(anchor.get("opened_by", "")),
            "opened_at": str(anchor.get("opened_at", "")),
            "closed_at": anchor.get("closed_at"),
            "summary_mid": anchor.get("summary_mid"),
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


def _find_parent(rows: list[dict[str, Any]], mid: str) -> dict[str, Any] | None:
    """The row carrying *mid*, or ``None``."""
    for row in rows:
        if _row_mid(row) == mid:
            return row
    return None


def _parent_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "mid": _row_mid(row),
        "role": row.get("role", ""),
        "content": redact(str(row.get("content", "") or "")),
        "ts": row.get("ts", ""),
    }


def turn_anchor_mid(rows: list[dict[str, Any]]) -> str:
    """The mid of the user message that started the LAST turn in *rows*.

    What a thread opened on a streaming reply anchors to. A streaming assistant
    row has no ``mid`` -- ids are minted when the row persists, post-turn -- so
    there is nothing to hang an anchor off; the message that ASKED for the work
    is persisted, carries a mid, and is where the paradigm puts the thread anyway
    ("the thread hangs under the sentence that started it").

    Read backwards for the newest user row with a mid, so a turn that already
    emitted finished segments still anchors to its own prompt rather than to one
    of its own replies. ``""`` when the conversation has no such row.
    """
    for row in reversed(rows):
        if row.get("role") != ROLE_USER:
            continue
        mid = _row_mid(row)
        if mid:
            return mid
    return ""


# ── open_thread ───────────────────────────────────────────────────────────────


class ThreadOpenError(Exception):
    """A refusal :func:`open_thread` could not carry out.

    ``code`` is the machine-readable reason both entry points publish; ``status``
    the HTTP one. Carries ``thread_slot`` when the refusal is ``already_open``, so
    the caller can point at the thread that already exists instead of making the
    user hunt for it.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 409,
        thread_slot: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.thread_slot = thread_slot


#: What the store's admission outcomes mean to a caller. Spelled here rather than
#: at each call site so the HTTP route and the MCP tool refuse identically.
_ADMISSION_REFUSALS: dict[str, tuple[str, str, int]] = {
    "missing": (
        "This chat isn't ready for threads yet. Try again in a moment.",
        "transcript_missing",
        409,
    ),
    "unflushed": (
        "This chat isn't ready for threads yet. Try again in a moment.",
        "transcript_missing",
        409,
    ),
    "replaced": (
        "This chat was replaced. Open it again to start a thread.",
        "transcript_replaced",
        409,
    ),
    "full": (
        "This chat has no room for more threads. Start a new chat to keep discussing.",
        "threads_full",
        409,
    ),
    "sidecar_full": (
        "This chat has no room for more threads. Start a new chat to keep discussing.",
        "threads_full",
        409,
    ),
}


def _refuse_admission(outcome: str, *, thread_slot: str = "") -> ThreadOpenError:
    if outcome == "duplicate":
        return ThreadOpenError(
            "This message already has a thread.",
            code="already_open",
            status=409,
            thread_slot=thread_slot,
        )
    message, code, status = _ADMISSION_REFUSALS.get(
        outcome, ("The thread could not be opened. Try again.", "thread_open_failed", 409)
    )
    return ThreadOpenError(message, code=code, status=status)


async def open_thread(
    state: DashboardState,
    anchor: Anchor,
    *,
    title: str,
    agent: str | None = None,
    opened_by: str,
    note: str = "",
    in_flight: bool = False,
) -> dict[str, Any]:
    """Mint an ordinary session anchored to *anchor*, and deliver *note* if given.

    Nothing is injected at open. A thread learns what it hangs off at its FIRST
    TURN, as a summary its own projector builds from the parent as it stands then
    (:mod:`~kiro_crew.dashboard.thread_projection`) -- so a thread opened today and
    written to next week discusses next week's parent, and a thread beside a busy
    parent is not carrying a quote of one old message forever.

    *note* is whatever the opener actually SAID, and it is delivered as the
    thread's own first user message. That is what gives it the parent chat's queue
    receipt: it travels the ordinary send path, so the thread's composer shows
    exactly what any chat shows. A bare click delivers nothing and leaves the
    thread waiting for its reader, as the drawer's empty hint says it will.

    *in_flight* says the caller resolved this anchor from a streaming turn rather
    than from a row the person pointed at, and only the caller can know it: by
    the time the anchor arrives here it is an ordinary ``mid`` either way. It
    reaches the ``thread/opened`` entry and nothing else -- the anchor index
    projects to a fixed field set (:func:`~kiro_crew.history._thread_anchor_row`)
    and has no room for it, so a reader asking which anchors were resolved has
    the crew log to ask.

    The one implementation both openers call. In order:

    1. resolve the anchored conversation and confirm the anchor is admissible
       (read-only probe, so a refusal costs no session);
    2. mint the thread's slot through ``session_control.create_session`` -- the
       same core ``session_create`` uses. NOT ``session_fork``: fork copies the
       whole parent transcript and refuses an ``agent`` override, while a thread
       wants a curated seed and an optional agent;
    3. record the anchor in the parent's anchor index, which is the ONE record of
       the relation on this surface, and what lets the parent list its threads in
       one read. A late refusal here retracts the session;
    4. emit ``thread/opened`` and announce the anchor to the dashboard; then
    5. deliver the opener's note, when there is one.

    Returns ``{"thread_slot", "anchor", "title", "seeded"}``, where ``seeded``
    says whether a note was delivered.
    Raises :class:`ThreadOpenError` for every refusal. Never touches the parent's
    running turn: no semaphore, no ``has_active_turn()`` gate, no queue entry.
    """
    if anchor.surface != SURFACE_DASHBOARD:
        raise ThreadOpenError(
            f"threads on {anchor.surface!r} are not served here",
            code="surface_unsupported",
            status=400,
        )
    if not _valid_mid(anchor.mid):
        raise ThreadOpenError("invalid mid", code="invalid_mid", status=400)
    log = state.conversation_log
    if log is None:
        raise ThreadOpenError(
            "Threads are unavailable right now.", code="threads_unavailable", status=503
        )
    parent_slot = state.get_slot(anchor.conversation)
    if parent_slot is None:
        raise ThreadOpenError("not found", code="slot_not_found", status=404)
    parent_key = slot_history_key(parent_slot)

    # Captured BEFORE the parent lookup, so it is never younger than the rows the
    # parent was found in: a chat deleted and recreated under the same key after
    # this line is told apart at the store write, and one replaced before it is
    # simply the chat the parent belongs to.
    identity = await asyncio.to_thread(log.thread_transcript_identity, parent_key)
    rows = await _transcript(state, parent_slot)
    parent = _find_parent(rows, anchor.mid)
    if parent is None:
        raise ThreadOpenError(
            "That message is no longer in this chat.", code="parent_not_found", status=404
        )
    try:
        probe = await asyncio.to_thread(
            log.thread_anchor_admissible, parent_key, anchor.mid, expected_created_at=identity
        )
        existing = await asyncio.to_thread(log.read_thread_anchors, parent_key)
    except ThreadStoreUnreadable:
        logger.warning("thread sidecar unreadable for slot=%s", parent_slot.key, exc_info=True)
        raise ThreadOpenError(
            "Threads are unavailable right now.", code="threads_unavailable", status=503
        ) from None
    except HistoryLockTimeout:
        logger.warning("thread store lock timeout for slot=%s", parent_slot.key)
        raise ThreadOpenError(
            "Threads are unavailable right now.", code="threads_unavailable", status=503
        ) from None
    if probe != "ok":
        raise _refuse_admission(
            probe, thread_slot=str((existing.get(anchor.mid) or {}).get("thread_slot", ""))
        )
    # `existing` is read ONCE, here, and is the pre-mint picture. Minting is a round
    # trip, so it must not be the source of the slot named in a LATE refusal; see
    # `_winning_thread_slot`.

    # Mint through the shared core. The CALLER is the anchored conversation
    # itself, which is what makes the child inherit the right things without a
    # second policy: the parent's workspace (the memory boundary), its trust
    # posture, its project, and a ``session/opened`` entry whose ``parent`` link
    # already records the thread's lineage -- so ``thread/opened`` adds the anchor
    # and nothing else.
    from kiro_crew.dashboard import session_control as sc

    clean_title = redact(title.strip())[:THREAD_ANCHOR_TITLE_MAX_CHARS]
    try:
        created = await sc.create_session(
            state,
            caller_session_key=parent_key,
            title=clean_title,
            agent=(agent or "").strip(),
            folder_id=parent_slot.folder_id or "",
        )
    except sc.SessionControlError as exc:
        # Surfaced verbatim: the create core's refusals are already
        # caller-readable, and translating them here would hide which one fired.
        raise ThreadOpenError(
            str(exc),
            code=getattr(exc, "code", "thread_open_failed"),
            status=getattr(exc, "status", 409),
        ) from None
    thread_slot_key = str(created.get("target", ""))
    thread_slot = state.get_slot(thread_slot_key)
    if thread_slot is None:
        raise ThreadOpenError(
            "The thread could not be opened. Try again.", code="thread_open_failed", status=409
        )
    thread_key = slot_history_key(thread_slot)

    async def _retract(reason: str) -> None:
        """Close a minted session whose anchor could not be recorded.

        An anchorless thread is worse than no thread: it is a session in the
        sidebar that the conversation it belongs to cannot find, seeded with a
        quote of a message nothing links it to.
        """
        from kiro_crew.dashboard.chat_handlers import close_slot

        try:
            await close_slot(state, thread_slot, thread_slot_key)
        except Exception:
            logger.warning(
                "thread open: anchor write failed (%s) and the minted slot %s "
                "could not be closed",
                reason,
                thread_slot_key,
                exc_info=True,
            )
            return
        # Closing a slot ARCHIVES its conversation, which is right for a chat
        # somebody used and wrong for an EMPTY one: a session that never became a
        # thread and that nothing points at is the orphan the retraction exists to
        # avoid -- findable in history, belonging to no conversation. So an empty
        # one is removed outright.
        #
        # Emptiness is CHECKED, not assumed. The minted slot is an ordinary sidebar
        # session from the moment it is created -- the person can open it and type
        # into it -- and the anchor write that decides this retraction can wait out
        # a patient lock acquire measured in seconds. A message sent in that window
        # is somebody's, `delete_session` has no recovery path, and the archive is
        # exactly the right home for it. Any row of the conversation counts: a
        # streamed chunk is a turn already answering.
        if any(m.get("role") in _RETRACT_KEEPING_ROLES for m in thread_slot.messages):
            logger.warning(
                "thread open: retracted slot %s was written to before the anchor "
                "failed (%s), so it is archived rather than deleted",
                thread_slot_key,
                reason,
            )
            return
        try:
            await asyncio.to_thread(log.delete_session, thread_key)
        except Exception:
            logger.warning(
                "thread open: retracted slot %s stayed in history", thread_slot_key, exc_info=True
            )

    anchor_row = {
        "thread_slot": thread_slot_key,
        "title": clean_title,
        # Held to the store's shape HERE rather than trusted: a session key with a
        # character the index does not admit would otherwise make the write raise
        # ValueError after the slot was already minted. An unrecognisable opener
        # records as ``user``, which is the honest floor -- a person is behind
        # every session -- and never a refusal for a thread that is otherwise fine.
        "opened_by": opened_by if _OPENED_BY_RE.match(opened_by) else "user",
        "opened_at": _now_iso(),
        "closed_at": None,
        "summary_mid": None,
        # Where this message sits in the PARENT's crew log, resolved now because it
        # cannot be resolved later: the log carries no ``mid``, so the join is by
        # timestamp, and a parent that compacts drops the row whose timestamp this
        # is. Best effort by contract -- UNKNOWN keeps the thread's
        # context projection to a small recent window instead of refusing the open.
        "parent_log_seq": await asyncio.to_thread(
            thread_projection.parent_log_position,
            thread_projection.slot_log_sid(parent_slot),
            str(parent.get("ts", "") or ""),
        ),
    }
    # The anchor is recorded ONCE on the dashboard, in the parent's index. A second
    # copy on the thread's own metadata was written here for durability -- so a
    # thread could still name what it hangs off if the index were lost -- and nothing
    # read it, which makes it a shape to keep consistent for no reader. Slack keeps
    # its own metadata copy because a Slack thread has no parent transcript to hang
    # an index off, and its writer reads that copy as its idempotency guard.
    try:
        outcome = await asyncio.to_thread(
            log.write_thread_anchor,
            parent_key,
            anchor.mid,
            anchor_row,
            max_anchors=THREADS_MAX_ANCHORS_PER_SIDECAR,
            expected_created_at=identity,
        )
    except (ThreadStoreUnreadable, HistoryLockTimeout, OSError):
        logger.warning("thread anchor write failed for slot=%s", parent_slot.key, exc_info=True)
        await _retract("anchor index")
        raise ThreadOpenError(
            "Threads are unavailable right now.", code="threads_unavailable", status=503
        ) from None
    if outcome != "ok":
        await _retract(outcome)
        raise _refuse_admission(
            outcome,
            thread_slot=await _winning_thread_slot(log, parent_key, anchor, outcome, existing),
        )

    _emit_thread_opened(
        state,
        anchor=anchor,
        thread_slot=thread_slot_key,
        title=clean_title,
        opened_by=opened_by,
        in_flight=in_flight,
    )
    broadcast_thread_anchor(
        state,
        slot_key=parent_slot.key,
        mid=anchor.mid,
        event="opened",
        thread_slot=thread_slot_key,
        title=clean_title,
        opened_by=opened_by,
    )
    seeded = await _deliver_note(state, thread_slot, note, parent_key=parent_key)
    return {
        "thread_slot": thread_slot_key,
        "anchor": anchor.to_dict(),
        "title": clean_title or thread_slot_key,
        "seeded": seeded,
    }


async def _winning_thread_slot(
    log: Any,
    parent_key: str,
    anchor: Anchor,
    outcome: str,
    pre_mint: dict[str, Any],
) -> str:
    """The thread a late ``duplicate`` refusal should name.

    The pre-mint read happened before a round trip, and on a CLOSED anchor the row
    it holds is the ENDED thread: closing releases the message, so two openers can
    race and the loser's stale read names a session the reader already finished.
    Handing that back tells the caller to open the wrong conversation, and the
    caller acts on it -- the dashboard shows that slot and an agent is told to send
    to it.

    So a ``duplicate`` is answered from a FRESH read. That read is not itself racy
    in a way that matters: ``duplicate`` means an open anchor is there, and this
    store refuses to replace an open one, so the row cannot change again until
    someone closes it -- and a reader who then finds it closed mints a replacement
    rather than joining it. Any other outcome keeps the pre-mint answer, which is
    the picture those refusals are about.
    """
    if outcome != "duplicate":
        return str((pre_mint.get(anchor.mid) or {}).get("thread_slot", ""))
    try:
        fresh = await asyncio.to_thread(log.read_thread_anchors, parent_key)
    except (ThreadStoreUnreadable, HistoryLockTimeout, OSError):
        # The refusal still has to reach the caller, and without a slot it is a
        # plain "already open" rather than a pointer at the wrong session.
        logger.warning("could not re-read anchors for slot=%s", parent_key, exc_info=True)
        return ""
    return str((fresh.get(anchor.mid) or {}).get("thread_slot", ""))


async def _deliver_note(
    state: DashboardState,
    thread_slot: _ChatSlot,
    note: str,
    *,
    parent_key: str,
) -> bool:
    """Deliver the opener's note as the thread's first user message.

    The note is the only thing anybody has actually SAID at open time, so it is
    the only thing worth running a turn on. It goes through ``send_to_target`` --
    the same verb ``session_send`` uses, with the parent as the caller, because the
    parent CREATED this slot and the ownership fence therefore admits it without a
    new authorization path. Travelling that ordinary path is the point: the thread
    gets the queue receipt every chat gets, rather than a bespoke first row that
    looks different from every later one.

    ``steer=False`` because the thread has no turn to cut into; it was minted empty
    one moment ago, so this starts the first.

    Best effort: the thread EXISTS once its anchor is recorded, so a note that
    could not be delivered leaves a usable session the person can type into, and
    retracting a real thread over one message would be the worse trade. The return
    value says which happened and the caller reports it.
    """
    if not note.strip():
        return False
    from kiro_crew.dashboard import session_control as sc

    try:
        await sc.send_to_target(
            state,
            caller_session_key=parent_key,
            target=thread_slot.key,
            message=note.strip(),
            steer=False,
        )
        return True
    except Exception:
        logger.warning("thread open: note not delivered to %s", thread_slot.key, exc_info=True)
        return False


def _emit_thread_opened(
    state: DashboardState,
    *,
    anchor: Anchor,
    thread_slot: str,
    title: str,
    opened_by: str,
    in_flight: bool = False,
) -> None:
    """``thread/opened`` in the session-kind crew log.

    The ledger, per the design: no second jsonl store. ``session/opened`` already
    records the thread's LINEAGE (its ``parent`` link, written by the create core),
    so this entry adds the anchor and nothing else. Best-effort: a crew log that
    cannot be written must not cost the person their thread.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    try:
        crew_log_emit.on_thread_opened(
            _parent_sid(state, anchor.conversation),
            anchor=anchor.to_dict(),
            thread_slot=thread_slot,
            title=title,
            opened_by=opened_by,
            in_flight=in_flight,
        )
    except Exception:
        logger.debug("thread/opened not recorded for %s", thread_slot, exc_info=True)


def _parent_sid(state: DashboardState, conversation: str) -> str:
    """The crew-log session id of the conversation the anchor names, or ``""``.

    The crew log is keyed by session id, and an entry about "what hangs off this
    chat" belongs on that chat's own log. ``""`` when nothing here can name one,
    which the emitter reads as "nothing to write" -- a thread is still opened; only
    its ledger line is missing, and the thread's own ``session/opened`` still
    records the lineage.
    """
    from kiro_crew.dashboard import thread_projection

    slot = state.get_slot(conversation)
    if slot is None:
        return ""
    return thread_projection.slot_log_sid(slot)


def _sid_by_slot_key(slot_key: str) -> str:
    """The newest crew log recorded for *slot_key*, or ``""`` -- a store scan.

    The last resort behind :func:`slot_log_sid`, and separate from it because this
    one reads the store: every source there is in memory, so a restart that has not
    yet re-run the parent's turn leaves them all empty while the log itself is sitting
    on disk under that slot. Only the on-demand read path pays for this; the per-turn
    projection does not, which is why it is not folded into the helper above.
    """
    from kiro_crew.crew_log import read as crew_log_read

    try:
        listing = crew_log_read.list_session_units(slot_contains=slot_key, limit=8)
    except Exception:
        logger.warning("thread context: could not scan for the parent's log", exc_info=True)
        return ""
    for row in listing.get("rows") or []:
        # `slot_contains` is a SUBSTRING match, so a sibling slot whose key contains
        # this one's would otherwise answer for it. Rows come newest first, which is
        # the one to take when a slot has been through more than one store.
        if str(row.get("slot") or "") == slot_key:
            unit = row.get("unit")
            if isinstance(unit, str) and unit:
                return unit
    return ""


# ── Close ─────────────────────────────────────────────────────────────────────


async def close_thread(
    state: DashboardState,
    anchor: Anchor,
    *,
    expect_thread_slot: str = "",
) -> dict[str, Any]:
    """Close the thread on *anchor* and post a card in the parent.

    *expect_thread_slot* is the thread the CALLER believes it is ending. Given, a
    row naming a different thread is refused instead of closed: the mid identifies
    the message and the message can carry a succession of threads, so a caller
    holding a stale view would otherwise end whichever one is there now. The
    store-side guard below is a different question -- it catches the row changing
    between this coroutine's own read and its write.

    The card is a durable row in the parent conversation carrying
    ``meta.thread_summary`` -- which is what makes the back-link work and what
    makes the card survive a transcript read like any other row. Its role is
    :data:`ROLE_THREAD_CLOSED`, which is what keeps it out of the parent's replay.
    """
    log = state.conversation_log
    if log is None:
        raise ThreadOpenError(
            "Threads are unavailable right now.", code="threads_unavailable", status=503
        )
    parent_slot = state.get_slot(anchor.conversation)
    if parent_slot is None:
        raise ThreadOpenError("not found", code="slot_not_found", status=404)
    parent_key = slot_history_key(parent_slot)
    try:
        anchors = await asyncio.to_thread(log.read_thread_anchors, parent_key)
    except (ThreadStoreUnreadable, HistoryLockTimeout):
        raise ThreadOpenError(
            "Threads are unavailable right now.", code="threads_unavailable", status=503
        ) from None
    entry = anchors.get(anchor.mid)
    if entry is None:
        raise ThreadOpenError("no thread on that message", code="thread_not_found", status=404)
    if entry.get("closed_at") is not None:
        raise ThreadOpenError("that thread is already closed", code="already_closed", status=409)
    thread_slot_key = str(entry.get("thread_slot", ""))
    if expect_thread_slot and thread_slot_key != expect_thread_slot:
        # The caller is looking at a thread that another one has replaced here.
        raise ThreadOpenError("that thread is already closed", code="already_closed", status=409)
    # Persist the transition BEFORE publishing the card. The card is a durable
    # row in the parent and it is broadcast, so posting it first means a close
    # whose anchor write fails leaves a "thread closed" card standing over a
    # thread the store still reads as open -- and `sidecar_full` fails the same
    # way every time, so each retry appends another card and none converges.
    # Ordered this way the surviving failure is the benign one: a closed anchor
    # whose card did not post, which the row shape already admits.
    try:
        outcome = await asyncio.to_thread(
            log.update_thread_anchor,
            parent_key,
            anchor.mid,
            {"closed_at": _now_iso(), "summary_mid": None},
            expect_open=True,
            # BOTH guards, and `expect_open` alone is not enough. Two closes can read
            # the same open anchor; the first commits, an opener then installs a
            # REPLACEMENT thread on the freed message, and the second close finds an
            # open anchor again -- satisfying `expect_open` -- and ends a live thread
            # the closer never saw, stamping a card for the ended one over it.
            # Naming the thread is what makes the transition the one it was asked for.
            expect_thread_slot=thread_slot_key,
        )
    except (ThreadStoreUnreadable, HistoryLockTimeout, OSError):
        logger.warning("thread close write failed for slot=%s", parent_slot.key, exc_info=True)
        raise ThreadOpenError(
            "Threads are unavailable right now.", code="threads_unavailable", status=503
        ) from None
    if outcome == "already_closed":
        # The read above found it open, so another closer won the lock in between.
        # Same answer as the early refusal, and the point of asking the store under
        # its own lock: two closes must not both go on to post a summary card.
        raise ThreadOpenError("that thread is already closed", code="already_closed", status=409)
    if outcome == "replaced":
        # The thread this close named is gone and another holds the message. Nothing
        # to end, and the same answer the caller needs: the thread they asked about
        # is finished. Refusing here is what leaves the replacement untouched.
        raise ThreadOpenError("that thread is already closed", code="already_closed", status=409)
    if outcome != "ok":
        raise _refuse_admission(outcome, thread_slot=thread_slot_key)
    card_mid = _post_close_card(
        parent_slot,
        thread_slot=thread_slot_key,
        title=str(entry.get("title", "")),
    )
    if card_mid:
        # The card row FIRST, on disk, before the durable pointer names it.
        #
        # ``parent_slot.append`` broadcasts the row and marks the slot dirty; the
        # only writer is the five-second periodic pass, whose contract is that a
        # crash loses at most one interval and which returns on shutdown without a
        # final flush. Storing ``summary_mid`` durably while the row it names is
        # still memory-only therefore admits a crash window that leaves a permanent
        # pointer to a row no transcript carries -- and nothing re-posts it, because
        # the anchor is already ``closed_at`` so every retry answers
        # ``already_closed``. There is no recovery path, which is what makes the
        # ordering worth a blocking write here rather than a comment.
        #
        # Off-loop, and a failure is not fatal: an unflushed card costs the
        # back-link, which is the same cost the best-effort pointer write below
        # already accepts. What it must not do is leave the pointer stored against
        # a row that was never written, so the pointer is skipped when this fails.
        try:
            await asyncio.to_thread(state.flush_slot_now, parent_slot)
        except Exception:
            logger.warning(
                "thread close card not flushed for slot=%s; leaving its pointer unstored "
                "so the anchor cannot name a row the transcript lacks",
                parent_slot.key,
                exc_info=True,
            )
            card_mid = ""
    if card_mid:
        # Second write, and best-effort by design: the thread is already closed,
        # so losing the pointer costs the card's back-link and nothing else. A
        # raise here would report a failed close that did happen.
        #
        # Guarded on the thread it belongs to. The close above freed this message to
        # carry a NEW thread, so between the two writes a reopen can install a
        # different anchor here -- and an unguarded merge would stamp this thread's
        # card onto that one, giving a live thread a back-link to a closed thread's
        # card. `expect_thread_slot` makes the store refuse that instead.
        try:
            pointer = await asyncio.to_thread(
                log.update_thread_anchor,
                parent_key,
                anchor.mid,
                {"summary_mid": card_mid},
                expect_thread_slot=thread_slot_key,
            )
            if pointer == "replaced":
                logger.info(
                    "thread summary pointer dropped: a new thread holds mid=%s on slot=%s",
                    anchor.mid,
                    parent_slot.key,
                )
        except (ThreadStoreUnreadable, HistoryLockTimeout, OSError):
            logger.warning(
                "thread summary pointer not stored for slot=%s", parent_slot.key, exc_info=True
            )
    _emit_thread_closed(
        state,
        anchor=anchor,
        thread_slot=thread_slot_key,
        summary_mid=card_mid,
    )
    broadcast_thread_anchor(
        state,
        slot_key=parent_slot.key,
        mid=anchor.mid,
        event="closed",
        thread_slot=thread_slot_key,
        title=str(entry.get("title", "")),
        opened_by=str(entry.get("opened_by", "")),
        summary_mid=card_mid,
    )
    return {
        "thread_slot": thread_slot_key,
        "anchor": anchor.to_dict(),
        "summary_mid": card_mid,
    }


def _post_close_card(
    parent_slot: _ChatSlot,
    *,
    thread_slot: str,
    title: str,
) -> str:
    """Append the closing card to the parent's window and return its ``mid``.

    An ordinary ``assistant`` row: ``slot.append`` mints the ``mid`` and the row
    persists, is rewound, exported and re-read exactly like every other row. The
    card-ness lives entirely in ``meta.thread_summary``, whose ``thread_slot`` is
    the back-link -- ``/chat/<thread_slot>`` opens the thread as a full page and
    ``/chat/<parent_slot>?thread=<mid>`` opens the same slot in the drawer,
    because a thread is a real slot.

    The card states the close and names the thread. It carries no written summary:
    nothing on any surface composes one, so a body line would be a fixed sentence
    dressed up as a report.

    The row's role is :data:`ROLE_THREAD_CLOSED`, which keeps it out of replay --
    see that constant for why an ``assistant`` row would put the card's text into
    the parent's model-visible history as something the crewmate said.
    """
    # ONE redacted, bounded string for both the body and the metadata. The card the
    # reader actually sees is rendered from ``meta.thread_summary.title`` rather
    # than from the body, so storing the raw title there put an unredacted string
    # on a display surface while the body beside it was redacted -- and a title
    # carrying a credential would be shown. Write-time redaction narrows that but
    # does not close it, because this file treats re-redaction of this field at the
    # output boundary as mandatory; computing it once is also the only way the two
    # copies cannot drift.
    shown_title = redact(title)[:THREAD_ANCHOR_TITLE_MAX_CHARS] if title else ""
    lines = [_CLOSE_CARD_HEADER]
    if shown_title:
        lines.append(shown_title)
    row = parent_slot.append(
        ROLE_THREAD_CLOSED,
        "\n".join(lines),
        "msg msg-a",
        meta={"thread_summary": {"thread_slot": thread_slot, "title": shown_title}},
    )
    mid = row.get("meta", {}).get("mid") if isinstance(row.get("meta"), dict) else ""
    return mid if isinstance(mid, str) else ""


def _emit_thread_closed(
    state: DashboardState,
    *,
    anchor: Anchor,
    thread_slot: str,
    summary_mid: str,
) -> None:
    from kiro_crew.crew_log import emit as crew_log_emit

    try:
        crew_log_emit.on_thread_closed(
            _parent_sid(state, anchor.conversation),
            anchor=anchor.to_dict(),
            thread_slot=thread_slot,
            summary_mid=summary_mid,
        )
    except Exception:
        logger.debug("thread/closed not recorded for %s", thread_slot, exc_info=True)


# ── Route plumbing ────────────────────────────────────────────────────────────


def _deny_foreign_app(request: web.Request, slot: _ChatSlot, operation: str) -> web.Response | None:
    """App tokens see only their own slots (App Kit §5.2), so an app caller
    naming a slot it does not own gets the anti-enumeration 404.

    Owning the slot is NOT enough on these routes, and that is the second check
    below. A thread route addresses the slot's TRANSCRIPT, and a LINKED slot's
    transcript belongs to whatever bound it -- a cron job, a channel thread -- not
    to the slot's creator. An app may preclaim a name a later binding links (the
    reserved prefix is `member-` only), and the binding sets the link without
    asking who made the slot, so ownership survives it and the transcript key
    silently becomes someone else's. Refusing every app caller on a linked slot
    costs an app nothing it legitimately has: its own slots carry no link.
    """
    request_app = request.get("app", "")
    if not request_app:
        return None
    if slot._app and request_app == slot._app and not getattr(slot, "linked_session_key", ""):
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


def _resolve_slot(
    request: web.Request, state: DashboardState, slot_key: str, operation: str
) -> tuple[_ChatSlot | None, web.Response | None]:
    """The slot a thread request names, or the response refusing it.

    No ``not_crewmate_chat`` gate: threads work on every chat surface now, so a
    plain chat, a ``td-*`` session and a crewmate DM are all admitted. The mode
    check version 1 had existed because the turn ran as the crewmate; a thread
    runs as its own session.
    """
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
    return slot, None


async def _read_anchors(
    request: web.Request, state: DashboardState, slot: _ChatSlot, operation: str
) -> tuple[
    tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]] | None, web.Response | None
]:
    """The slot's anchor index and its legacy reply map, or the 503 standing in
    for them. Both halves come out of one sidecar, so they are read together.

    The app check is re-run HERE rather than trusted from the route's entry. A
    binding can link the slot during an await the handler already made, and the
    transcript key is taken after that await -- so a check that ran before it
    describes a shape the slot may have left.
    """
    log = state.conversation_log
    if log is None:
        return None, _unavailable()
    denied = _deny_foreign_app(request, slot, operation)
    if denied is not None:
        return None, denied
    key = slot_history_key(slot)

    def _read() -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        return log.read_thread_anchors(key), log.read_threads(key)

    try:
        return await asyncio.to_thread(_read), None
    except ThreadStoreUnreadable:
        logger.warning("thread sidecar unreadable for slot=%s", slot.key, exc_info=True)
        return None, _unavailable()
    except HistoryLockTimeout:
        logger.warning("thread store lock timeout for slot=%s", slot.key)
        return None, _unavailable()


async def api_chat_threads_summary(request: web.Request) -> web.Response:
    """GET /api/chat/threads?slot=<key> -- one summary per message with a thread.

    Anchors and the legacy v1 fold in one map (:func:`summarize`), so
    ``ThreadFooter`` keeps working unchanged while worker (b) is in flight.
    """
    state: DashboardState = request.app["state"]
    slot, refused = _resolve_slot(
        request, state, request.query.get("slot", ""), "chat.threads_summary"
    )
    if refused is not None:
        return refused
    assert slot is not None
    read, refused = await _read_anchors(request, state, slot, "chat.threads_summary")
    if refused is not None:
        return refused
    assert read is not None
    anchors, legacy = read
    return web.json_response({"threads": summarize(anchors, legacy)})


async def api_chat_thread_detail(request: web.Request) -> web.Response:
    """GET /api/chat/threads/{mid}?slot=<key> -- the anchor, and the legacy fold.

    The thread's own MESSAGES are not here: they are the thread slot's transcript,
    read through the ordinary chat endpoints, which is the whole point of a thread
    being a real session. This answers what hangs off the message -- the anchor
    (with the slot to open) and version 1's replies when the message has them.
    """
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
    parent = _find_parent(rows, mid)
    if parent is None:
        return web.json_response(
            {"error": "That message is no longer in this chat.", "code": "parent_not_found"},
            status=404,
        )
    read, refused = await _read_anchors(request, state, slot, "chat.thread_detail")
    if refused is not None:
        return refused
    assert read is not None
    anchors, legacy = read
    anchor = anchors.get(mid)
    return web.json_response(
        {
            "parent": _parent_payload(parent),
            "anchor": summarize({mid: anchor}).get(mid) if anchor else None,
            "legacy_replies": [_legacy_reply(r) for r in legacy.get(mid, [])],
        }
    )


def _legacy_reply(reply: dict[str, Any]) -> dict[str, Any]:
    """A v1 reply projected to its schema, with the prose re-redacted at the
    output boundary: a sidecar written under an older redactor is re-run through
    the current one on the way out, and a field the store did not write never
    leaves."""
    return {
        "id": str(reply.get("id", "")),
        "role": str(reply.get("role", "")),
        "content": redact(str(reply.get("content", ""))),
        "ts": str(reply.get("ts", "")),
    }


async def api_chat_thread_open(request: web.Request) -> web.Response:
    """POST /api/chat/threads/{mid}/open -- ``{slot_key?, title?, agent?, note?}``.

    BOTH openers land here, which is what keeps them one implementation:

    * the **user** clicks a message, so the body names the conversation
      (``slot_key``) and ``mid`` is that message's id; and
    * an **agent** calls MCP ``thread_open`` on the message it is answering, so
      the body names no slot and the caller's OWN conversation is used --
      resolved from the verified ``X-Session-Key``, never from the body, so a
      caller cannot open a thread on someone else's chat by naming it.

    ``mid`` may be the sentinel ``inflight``, meaning "the reply being written
    right now". A streaming assistant row has no mid of its own -- ids are minted
    post-turn -- so the anchor resolves to the user message that STARTED the turn.
    That is also the agent's default: the message it is answering IS its turn's
    prompt. The partial reply reaches the thread at its first turn, through the
    projector, which reads the parent live and marks that projection ``partial``.
    """
    state: DashboardState = request.app["state"]
    raw_mid = request.match_info["mid"]
    body, body_error = await read_bounded_json(request)
    if body_error is not None:
        return body_error
    assert body is not None
    slot_key = body.get("slot_key")
    if slot_key is not None and not isinstance(slot_key, str):
        return web.json_response(
            {"error": "slot_key must be text", "code": "missing_required_fields"}, status=400
        )
    opened_by = "user"
    if not slot_key:
        # The agent opener. The caller's own slot, from the key the transport
        # verified -- so this is not a way to address another conversation.
        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.dashboard.handlers._shared import _read_session_key

        caller_session = _read_session_key(request)
        slot_key = sc.caller_slot_key(state, caller_session) if caller_session else ""
        if not slot_key:
            return web.json_response(
                {
                    "error": "slot_key is required, or a caller session that names one",
                    "code": "missing_required_fields",
                },
                status=400,
            )
        opened_by = f"agent:{caller_session}"
    title = body.get("title", "")
    note = body.get("note", "")
    agent = body.get("agent", "")
    if not all(isinstance(v, str) for v in (title, note, agent)):
        return web.json_response(
            {"error": "title, note and agent must be text", "code": "missing_required_fields"},
            status=400,
        )
    slot, refused = _resolve_slot(request, state, slot_key, "chat.thread_open")
    if refused is not None:
        return refused
    assert slot is not None
    rows = await _transcript(state, slot)
    # The one place the streaming case is resolved. `inflight` is not a mid and
    # never reaches the store: it means "the reply being written right now", and
    # the anchor becomes the turn's own prompt.
    if raw_mid == "inflight":
        mid = turn_anchor_mid(rows)
        if not mid:
            return web.json_response(
                {
                    "error": "This chat has no message to open a thread on yet.",
                    "code": "parent_not_found",
                },
                status=404,
            )
    else:
        mid = raw_mid
        if not _valid_mid(mid):
            return web.json_response({"error": "invalid mid", "code": "invalid_mid"}, status=400)
    parent = _find_parent(rows, mid)
    if parent is None:
        return web.json_response(
            {"error": "That message is no longer in this chat.", "code": "parent_not_found"},
            status=404,
        )
    try:
        opened = await open_thread(
            state,
            Anchor(SURFACE_DASHBOARD, slot.key, mid),
            title=title or _default_title(parent),
            agent=agent or None,
            opened_by=opened_by,
            # One rule for both entry points: an agent's `thread_open(note=...)`
            # answers its own question, and a bare click -- from either caller --
            # leaves the thread waiting for whoever opened it.
            note=note,
            in_flight=raw_mid == "inflight",
        )
    except ThreadOpenError as exc:
        # Both bodies are written out as literals rather than built into one dict:
        # the error-code contract can only see a `code` it can read at the call
        # site, and a refusal whose status is an expression has to carry the code
        # transparently to be compliant for every status it might take.
        if exc.thread_slot:
            return web.json_response(
                {"error": str(exc), "code": exc.code, "thread_slot": exc.thread_slot},
                status=exc.status,
            )
        return web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)
    return web.json_response(opened, status=201)


def _default_title(parent: dict[str, Any]) -> str:
    """A title from the anchored message's first words, when none was given.

    The sidebar shows this, so an untitled thread must still be findable. Redacted
    like every other stored title.
    """
    text = redact(str(parent.get("content", "") or "")).strip().splitlines()
    first = text[0] if text else ""
    return f"Thread: {first[:80]}" if first else "Thread"


async def api_chat_thread_close(request: web.Request) -> web.Response:
    """POST /api/chat/threads/{mid}/close -- ``{slot_key}``.

    The counterpart of the opener, and the entry point the closing card reaches
    the parent conversation through. ONE caller, unlike the opener: the drawer's
    End thread control, which always names its slot. The opener also serves an
    agent through the ``thread_open`` tool and resolves a caller session into a
    slot for it; there is no ``thread_close`` tool, so this route asks for
    ``slot_key`` outright rather than carrying a branch for a caller that has no
    way to reach it.

    The body is ``{slot_key, thread_slot}`` and carries no free text: the only
    caller sends none, so a ``reason`` or ``summary`` field would be a parameter the
    surface accepts and no surface fills. ``thread_slot`` is the thread the caller
    believes it is ending, and a mismatch is refused -- the mid says which MESSAGE,
    never which thread, so a drawer left open while the message was closed and
    reopened elsewhere would otherwise end the replacement.

    Closing is idempotent from the caller's side in the sense that matters: a
    second close answers ``409 already_closed`` instead of posting a second card.
    """
    state = request.app["state"]
    raw_mid = request.match_info.get("mid", "")
    body, refusal = await read_bounded_json(request)
    if refusal is not None:
        return refusal
    assert body is not None
    slot_key = body.get("slot_key", "")
    if not isinstance(slot_key, str):
        return web.json_response(
            {"error": "slot_key must be text", "code": "missing_required_fields"},
            status=400,
        )
    if not slot_key:
        # No agent branch here. The opener resolves a caller session into a slot
        # because an agent opens threads through `thread_open`; there is no
        # `thread_close` tool, so every close on this surface comes from the drawer
        # and always names its slot. A branch for a caller that does not exist is a
        # second entry point to keep correct for nothing.
        return web.json_response(
            {"error": "slot_key is required", "code": "missing_required_fields"}, status=400
        )
    if not _valid_mid(raw_mid):
        return web.json_response({"error": "invalid mid", "code": "invalid_mid"}, status=400)
    expect_thread = body.get("thread_slot", "")
    if not isinstance(expect_thread, str) or not expect_thread:
        return web.json_response(
            {"error": "thread_slot is required", "code": "missing_required_fields"}, status=400
        )
    slot, refused = _resolve_slot(request, state, slot_key, "chat.thread_close")
    if refused is not None:
        return refused
    assert slot is not None
    try:
        closed = await close_thread(
            state, Anchor(SURFACE_DASHBOARD, slot.key, raw_mid), expect_thread_slot=expect_thread
        )
    except ThreadOpenError as exc:
        # Both bodies are written out as literals rather than built into one dict:
        # the error-code contract can only see a `code` it can read at the call
        # site, and a refusal whose status is an expression has to carry the code
        # transparently to be compliant for every status it might take.
        if exc.thread_slot:
            return web.json_response(
                {"error": str(exc), "code": exc.code, "thread_slot": exc.thread_slot},
                status=exc.status,
            )
        return web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)
    return web.json_response(closed, status=200)


def _context_rows(entries: list[dict[str, Any]], start: int) -> tuple[list[str], int]:
    """The lines a context read returns, and the last seq they actually cover.

    The row cap the caller already applied does not bound the answer on its own:
    forty rows at the read path's wide per-row limit is far more than a context
    block should take. So lines accumulate until :data:`_CONTEXT_READ_MAX_CHARS` is
    met and the covered seq says where the caller pages from. The FIRST kept row is
    always returned, so one enormous message yields its opening instead of nothing.
    """
    from kiro_crew.dashboard import thread_projection

    rows: list[str] = []
    covered = start - 1
    spent = 0
    for row in entries:
        if row.get("type") not in thread_projection.PROJECTED_TYPES or row.get("ignorable"):
            continue
        if rows and spent >= _CONTEXT_READ_MAX_CHARS:
            break
        line = redact(
            thread_projection.digest_row(row, max_chars=thread_projection.MAX_EXACT_ROW_CHARS)
        )
        rows.append(line)
        spent += len(line) + 1
        seq = row.get("seq")
        if isinstance(seq, int):
            covered = max(covered, seq)
    return rows, covered


async def api_chat_thread_context(request: web.Request) -> web.Response:
    """GET /api/chat/threads/context?from=<seq>&to=<seq> -- the parent's exact rows.

    The precise half of the parent-context pair. A thread's turns are given a
    SUMMARY of the conversation they hang off
    (:mod:`~kiro_crew.dashboard.thread_projection`), which is cheap and always on;
    this is how a model that needs the real wording asks for it.

    The caller is the THREAD, resolved from the verified ``X-Session-Key`` and
    never named in the query, so this reads one parent only: the caller's own. A
    session that is not a thread gets 404 ``not_a_thread`` rather than an empty
    page, because "no parent" and "a parent with nothing in range" are different
    answers and a model acting on the second would be acting on a lie.

    ``from`` and ``to`` are the parent's own crew-log seqs, which is what the
    thread's provenance rows and its injected block are numbered in, so a model
    can cite a block and then read exactly the rows behind it. The span is capped
    at :data:`_CONTEXT_READ_MAX_ROWS` rows and :data:`_CONTEXT_READ_MAX_CHARS`
    characters, and ``to`` names the last seq the answer actually carries: this
    answer goes into a model's context, and an uncapped read of a long parent would
    be the verbatim history the whole projection design exists to avoid. Message
    bodies come back at :data:`thread_projection.MAX_EXACT_ROW_CHARS`, which is
    wider than the summarizer's limit because the phrasing is the point here.
    """
    state: DashboardState = request.app["state"]
    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard import thread_projection
    from kiro_crew.dashboard.handlers._shared import _read_session_key

    caller_session = _read_session_key(request)
    slot_key = sc.caller_slot_key(state, caller_session) if caller_session else ""
    slot = state.get_slot(slot_key) if slot_key else None
    if slot is None:
        return web.json_response(
            {"error": "a caller session is required", "code": "missing_required_fields"},
            status=400,
        )
    denied = _deny_foreign_app(request, slot, "chat.thread_context")
    if denied is not None:
        return denied
    log = state.conversation_log
    if log is None:
        return web.json_response(
            {"error": "Threads are unavailable right now.", "code": "threads_unavailable"},
            status=503,
        )

    try:
        start = int(request.query.get("from", "0"))
        end = int(request.query.get("to", "0"))
    except ValueError:
        return web.json_response(
            {"error": "from and to must be whole numbers", "code": "invalid_range"}, status=400
        )
    if start < 1 or end < start:
        return web.json_response(
            {"error": "from must be 1 or more and to must not precede it", "code": "invalid_range"},
            status=400,
        )
    end = min(end, start + _CONTEXT_READ_MAX_ROWS - 1)

    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log import read as crew_log_read

    thread_sid = crew_log_emit.session_id_of(getattr(slot, "_acp_client", None)) or ""
    thread_state = await asyncio.to_thread(thread_projection.read_thread_state, thread_sid)
    parent_slot = (
        state.get_slot(thread_state.parent_slot_key) if thread_state.parent_slot_key else None
    )
    if parent_slot is None:
        return web.json_response(
            {"error": "this session is not a thread", "code": "not_a_thread"}, status=404
        )
    try:
        anchors = await asyncio.to_thread(log.read_thread_anchors, slot_history_key(parent_slot))
    except (ThreadStoreUnreadable, HistoryLockTimeout, OSError):
        return web.json_response(
            {"error": "Threads are unavailable right now.", "code": "threads_unavailable"},
            status=503,
        )
    anchor_mid, anchor_seq = thread_projection.find_anchor(anchors, slot.key)
    if not anchor_mid:
        return web.json_response(
            {"error": "this session is not a thread", "code": "not_a_thread"}, status=404
        )
    # A thread asks between the parent's turns, so the parent's live handle is the
    # source least likely to answer: `slot_log_sid` falls back to what the slot
    # itself records, the lineage edge answers after that, and a store scan by slot
    # key answers when every in-memory source is empty -- which is the shape of a
    # restart. `no_parent_log` means all four found nothing, rather than meaning the
    # parent happens not to be mid-turn.
    parent_sid = thread_projection.slot_log_sid(parent_slot) or thread_state.parent_session_id
    if not parent_sid:
        parent_sid = await asyncio.to_thread(_sid_by_slot_key, parent_slot.key)
    if not parent_sid:
        return web.json_response(
            {"error": "that conversation keeps no readable log", "code": "no_parent_log"},
            status=404,
        )
    page = await asyncio.to_thread(crew_log_read.read_page, parent_sid, start, end)
    rows, covered = _context_rows(page.get("entries") or [], start)
    return web.json_response(
        {
            "anchor": {"mid": anchor_mid, "parent_log_seq": anchor_seq},
            "from": start,
            "to": max(covered, start) if rows else end,
            "last_seq": page.get("last_seq", 0),
            "rows": rows,
        }
    )
