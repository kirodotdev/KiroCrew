"""A slot's transcript window: append, update, the lazy window and the summary payload.

The in-memory row buffer and its front trim, the disk-window bookkeeping a save
relies on, loading a restored row's window on first read, and ``to_dict`` with
its OPTIONS parsing.

Instance fields of ``_ChatSlot`` this module reads and writes:

Writes: ``_channel_window_mtime``, ``_dirty_flag``, ``_dirty_gen``,
``_disk_older_count``, ``_disk_older_durable_count``, ``_disk_tail_ts``,
``_disk_window_len``, ``_foreign_reported``, ``_frozen_prefix_cache``,
``_guarded_history_writes``, ``_lazy_tail``, ``_lazy_window``, ``_messages``,
``_metadata_persist_inflight``, ``_pending_rewrite``, ``_question_pending``,
``_resumed_count``, ``_unknown_row_fields_bytes``, ``_window_loader_thread``,
``total_messages``.
Reads only: ``_buffers``, ``_coordinator_approvals``, ``_on_card_event``,
``_on_message``, ``_on_question_retired``, ``_on_row``, ``_pending``, ``_projection``,
``event``, ``key``.

A function whose first parameter is ``self`` is a ``_ChatSlot`` method: the
class binds it under the same name (``state_parts/chat_slot.py``), so it reads
and writes the slot's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import resolve_effective_agent
from kiro_crew.constants import OPTIONS_RE_LINE
from kiro_crew.dashboard.state_parts.source_links import (
    _budgeted_source_links,
    _project_source_links,
)
from kiro_crew.dashboard.state_parts.transcript_rows import (
    _TRANSIENT_ROLES,
    _WIRE_ONLY_ROLES,
    durable_row_count,
    is_turn_interrupted,
    parse_cls_meta,
)
from kiro_crew.dashboard.system_notices import is_system_notice
from kiro_crew.history import latest_transcript_ts, mint_row_mid, monotonic_transcript_ts

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.state")


_MAX_SLOT_MESSAGES = 10000  # Keep all messages — virtual scrolling handles performance


# Serializes the replay of a lazily restored slot's window, never its disk read:
# ``_ChatSlot.load_window`` reads before it takes this, so the lock is held only
# for the in-memory replay. Re-entrant because the replay appends through
# ``_ChatSlot.messages`` itself; one lock for every slot because a replay is rare
# (once per restored row) and short.
_LAZY_WINDOW_LOCK = threading.RLock()


async def load_window_off_loop(slot: Any) -> None:
    """Load a restored sidebar row's pending window with its disk reads off the loop.

    Every async path that acts on a live slot it did not just create awaits this
    before it reads the slot's rows: the per-slot routes, a send, a session-control
    delivery or read, and a result delivered into an existing tab. The reads run
    on a worker thread and the apply runs back on the loop, so the first reader
    never stalls the gateway on a large transcript. A slot that carries its window
    (every slot but a pending restored row) returns at once, without suspending.
    When something reads ``messages`` during the await, that read loads the window
    inline and this result is dropped.
    """
    if getattr(slot, "window_pending", False) is not True:
        return
    read = getattr(slot._lazy_window, "read", None)
    if not callable(read):
        return
    prefetched = await asyncio.to_thread(read, slot)
    slot.load_window(*prefetched)


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


#: Roles whose LIVE append IS the next message an unanswered stateless question
#: card was waiting on, and so retires it. Only ``user`` qualifies: the card's
#: answer arrives as the user's next message, so only the human can spend it.
#: ``nudge`` deliberately does NOT: an auto-nudge cycle wakes the SAME agent in
#: the SAME conversation, so the answer channel survives it, and retiring there
#: destroys both the card and the record a reload rehydrates from while the
#: question is still open. A card whose question is genuinely dead is retired by
#: its own Dismiss control, which clears this record through
#: ``/api/ask-question/dismiss``.
#: Mirrors the frontend's ``QUESTION_RETIRING_ROLES``: the two must agree, or a
#: session reports needs_input with no card on screen (client retired, server did
#: not) or renders a card whose answer channel is already gone (server retired,
#: client did not). Widening coverage is a data edit here.
_QUESTION_RETIRING_ROLES = frozenset({"user"})


#: Roles that carry an inbound PROMPT -- the rows that ask this session to do
#: something, as opposed to the rows produced while it works. ``user`` is a human
#: send from any surface; ``inject`` is automation delivering a cron notification
#: or a subagent completion event. They rank a session by when its work was
#: requested while the answer is still streaming (``to_dict``'s ``last_turn_ts``).
_PROMPT_ROLES = frozenset({"user", "inject"})


# [OPTIONS: a | b | c] — the marker ends a LINE here, so use the MULTILINE/
# single-line canonical parser. Defined once in constants.py (shared with
# slack/format.py and the renderer surfaces) so the ReDoS-hardened grammar can
# never drift between copies; see OPTIONS_RE_LINE for the full rationale
# (tempered body, ``\n`` exclusion under MULTILINE). Per-choice whitespace is
# stripped by the caller; dashboard pills and Slack buttons parse OPTIONS
# identically because they share this exact object.
_OPTIONS_RE = OPTIONS_RE_LINE


def _redact(text: str) -> str:
    """Sanitise LLM output before surfacing to dashboard.

    The display battery (exfiltration URLs, then credentials) behind the
    slot-detail render's exact-content cache, ``_redact_for_display``: a sidebar
    read or slots broadcast re-redacts every open tab's title on the event loop,
    and a hit costs one keyed digest instead of both passes.
    """
    # circular import: chat_utils_display imports dashboard.state, which imports this module.
    from kiro_crew.dashboard.chat_utils_display import _redact_for_display

    return _redact_for_display(text)


#: The sidebar's one-line preview of a tab's last message is cut to this many
#: characters, plus "…" when it was longer.
_LAST_MESSAGE_PREVIEW_CHARS = 80


def _last_message_preview(text: str) -> str:
    """The sidebar's redacted one-line preview of a message *text*.

    Markdown is stripped before the redaction, then the result is cut. Memoized
    by ``_redacted_preview`` under a keyed digest of the raw text, so a tab whose
    last message has not changed costs one digest per read.
    """
    # circular import: chat_utils_display imports dashboard.state, which imports this module.
    from kiro_crew.dashboard.chat_utils_display import _redacted_preview

    return _redacted_preview(text, _LAST_MESSAGE_PREVIEW_CHARS)


#: Every match of ``_OPTIONS_RE`` contains this literal (the pattern is
#: case-sensitive), so a text without it has no marker and the per-position
#: regex scan can be skipped with the same answer.
_OPTIONS_HEAD = "[OPTIONS:"


def _strip_options(text: str) -> str:
    """*text* without its ``[OPTIONS: ...]`` marker lines, stripped."""
    if _OPTIONS_HEAD not in text:
        return text.strip()
    return _OPTIONS_RE.sub("", text).strip()


def _parse_options(text: str) -> list[str]:
    """Extract pipe-separated choices from the LAST [OPTIONS: A | B | C] in text."""
    if _OPTIONS_HEAD not in text:
        return []
    matches = list(_OPTIONS_RE.finditer(text))
    if not matches:
        return []
    parts = [p.strip() for p in matches[-1].group("labels").split("|")]
    return [p for p in parts if p]


def init_transcript_fields(self: _ChatSlot) -> None:
    """Set this module's slot fields to their defaults (``_ChatSlot.__init__``)."""
    self._messages = []
    # A restored sidebar row whose transcript window has not been loaded yet:
    # the loader to run on first read of ``messages``, and the bounded tail
    # the sidebar summary projects from meanwhile. Both None on every slot
    # that carries its window. See ``chat_persistence`` lazy restore.
    self._lazy_window = None
    self._lazy_tail = None
    # The thread replaying that window right now, so the replay's own reads
    # of ``messages`` see the list it is filling while every other thread's
    # read waits for the replay to finish.
    self._window_loader_thread = None
    self.total_messages = 0  # lifetime count (survives trimming)
    self._resumed_count = 0  # messages loaded from history on resume
    self._dirty_flag = False  # True when messages changed since last flush
    # Bumped by the _dirty setter on every True. Lets the periodic flush tell
    # "the True I started this save under" from "a NEW True set during it".
    self._dirty_gen = 0
    # A guarded metadata write has changed this live slot but has not yet
    # committed.  The periodic writer must not serialize that provisional
    # state to an unpinned transcript while the guarded write waits.
    self._metadata_persist_inflight = 0
    # The executor futures of this slot's guarded history writes, held until
    # the WORKER finishes. ``_metadata_persist_inflight`` above answers a
    # different question and cannot answer this one: it is released in the
    # awaiting coroutine's ``finally``, so a handler cancelled mid-write
    # drops the count while its worker thread runs on to the rename. A
    # retraction of this slot's name must order itself after the real write,
    # so it waits on these futures, which complete with the worker.
    self._guarded_history_writes = set()
    # Transcript mtime the in-memory window was last brought up to date
    # against. Only meaningful for a slot bound to a channel session, whose
    # transcript the channel also writes to (see channel_slots).
    self._channel_window_mtime = 0.0
    self._disk_older_count = (
        0  # count of disk messages OLDER than in-memory window (stable, set at restore/resume)
    )
    # Bytes of newer-build row fields the messages keep (an upper bound; see
    # slot_persistence.message_entries.MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES).
    self._unknown_row_fields_bytes = 0
    # Durable-only frozen-prefix counter: how many durable rows (role not
    # in ``_TRANSIENT_ROLES``) have LEFT the in-memory window off the front.
    # Absolute message positions (``session_control.read_messages``) are
    # built over durable rows, so they base on THIS counter;
    # ``_disk_older_count`` keeps its save-model contract (the frozen
    # prefix saves must not rewrite) untouched. The two also draw the
    # unpersisted-overflow line differently — see the trim path below.
    # Maintained at every site that sets or advances ``_disk_older_count``,
    # always via :func:`durable_row_count`. Never persisted — every restore
    # path recomputes it from the messages on disk.
    self._disk_older_durable_count = 0
    # Count of in-memory window messages the LAST save persisted to disk
    # (the on-disk window region). Trimming may only fold a leading window
    # message into the frozen prefix once it is known to be on disk; this
    # watermark is what makes the #8 trim credit safe. It is NOT a fragile
    # "what to append" counter — saves always re-serialize the WHOLE window.
    self._disk_window_len = 0
    # The newest ``ts`` seen on disk at the last save, INCLUDING rows this
    # slot never observed. A subagent, cron, or CLI appending to a session a
    # live tab also has open writes rows that ``_save_slot_to_history``
    # preserves as "foreign" without ever folding them into ``messages`` --
    # so the window is not a superset of the file, and flooring the next
    # append on the window tail alone can tie a foreign row's timestamp.
    # Cached rather than read per append: consulting the file here would put
    # a stat plus a bounded read on the event loop, which AUTOSDE's
    # no-blocking-call-on-event-loop rule forbids. Refreshed at the save
    # boundary, where the lock is already held and the foreign lines are
    # already parsed.
    self._disk_tail_ts = None  # Cached frozen-prefix bytes for the append-safe save model.
    # The session file is FROZEN-PREFIX (the first _disk_older_count on-disk
    # message lines, OLDER than the in-memory window) + a fresh re-serialize
    # of the whole window. The prefix is never rewritten, so a restart that
    # loaded only a recent window cannot destroy older history. This
    # caches the prefix bytes keyed by (path-mtime, path-size,
    # _disk_older_count) so a 5s flush is O(window), not O(file). The
    # (mtime, size) pair also doubles as the "did another process write this
    # file since we last saved?" signal that gates the cross-process
    # foreign-append merge. See chat_persistence._save_*.
    self._frozen_prefix_cache = None
    # Hashes of kept foreign lines already logged, so a re-scan stays quiet.
    self._foreign_reported = frozenset()
    # Set by rewind/regenerate after they TRUNCATE the window. While set,
    # _save_slot_to_history takes the archive-safe rewrite path so the
    # dropped tail is archived — even if the inline rewrite save failed:
    # the next 5s flush then retries the rewrite instead of silently
    # overwriting (the default save skips archiving). Cleared on a
    # successful rewrite save.
    self._pending_rewrite = False


def note_disk_tail(self: _ChatSlot, *candidates: str | None) -> None:
    """Record the newest ``ts`` known to be ON DISK for this session.

    The save boundary is the only place a slot can learn about a row it never
    observed (see ``_disk_tail_ts``), so it calls this with whatever it just
    wrote -- foreign rows included. Keeping the update here rather than
    assigning the attribute from the persistence module means the **monotone**
    rule lives with the field it guards: the floor may only ever move FORWARD.
    A save that moved it backwards would re-open the same-``ts`` tie the floor
    exists to prevent, and unparseable candidates are skipped rather than
    ranked (``latest_transcript_ts``), so one corrupt row cannot capture it.
    """
    self._disk_tail_ts = latest_transcript_ts(self._disk_tail_ts, *candidates)


def append(
    self: _ChatSlot,
    role: str,
    content: str,
    cls: str = "",
    ts: str = "",
    *,
    broadcast: bool = True,
    broadcast_user: bool = False,
    meta: dict | None = None,
    mint_mid: bool = True,
) -> dict[str, Any]:
    # A durable row ends the segment a permission request held open, so the
    # reply's text lands above it. Cleared first: the cut appends a row too.
    owed_cut = getattr(self, "_owed_segment_cut", None)
    if owed_cut is not None and role not in _WIRE_ONLY_ROLES:
        self._owed_segment_cut = None
        try:
            owed_cut()
        except Exception:
            logger.warning("owed segment cut failed for slot %s", self.key, exc_info=True)
    # A live row the person typed makes any in-flight return summary stale.
    # The local import avoids a state -> warm_task_brief -> state cycle, and
    # replay appends (broadcast=False) deliberately do not cancel current work.
    if broadcast and role == "user":
        try:
            from kiro_crew.dashboard.warm_task_brief import cancel_for_live_user_row

            owner = getattr(self._on_message, "__self__", None)
            if owner is not None:
                cancel_for_live_user_row(owner, self.key, content)
        except Exception:
            logger.debug("warm task brief cancellation failed for %s", self.key, exc_info=True)
    # A LIVE user row retires every unanswered STATELESS question: that row
    # IS the next message the card's answer was contracted to arrive as.
    # Retiring here rather than at the composer covers every entrance —
    # queued dispatch, a channel row relayed from Slack — instead of the one
    # send site that happens to be in front of the user. An auto-nudge cycle
    # is NOT one of them: see ``_QUESTION_RETIRING_ROLES``.
    #
    # The role set mirrors the frontend's `QUESTION_RETIRING_ROLES`, which
    # drops the card on the same frames. They must agree: a role the client
    # retires but the server keeps leaves a session reporting needs_input with
    # no card on screen, and a later rehydration re-renders a card whose
    # answer channel is gone.
    #
    # Gated on *broadcast*, which is what separates a live append from a
    # REPLAY: `channel_slots._rebuild_window` (transcript rotation recovery),
    # `chat_fork` and `session_transfer` all re-append historical rows with
    # `broadcast=False`. Replaying an old row says nothing about the question
    # asked a moment ago, and clearing on it would retire a live card's status
    # — and broadcast the retirement — for a message sent hours earlier.
    #
    # BLOCKING records are left alone either way: nothing a turn-consuming row
    # can do resolves a parked wait, so clearing one would report the agent as
    # working while its tool call is still stuck on the answer. Those are
    # owned by the round-trip in request_question, which retires its own entry
    # on every exit.
    if role in _QUESTION_RETIRING_ROLES and broadcast and self._question_pending:
        retired = [cid for cid, rec in self._question_pending.items() if not rec.get("blocking")]
        self._question_pending = {
            cid: rec for cid, rec in self._question_pending.items() if rec.get("blocking")
        }
        # Announce it: a retirement that only mutates state is invisible to a
        # second window and to a /pending response already in flight, either
        # of which would then re-render a card whose answer was just sent —
        # and submitting that card appends a duplicate turn.
        if retired and self._on_question_retired:
            try:
                self._on_question_retired(self.key, retired)  # type: ignore[operator]
            except Exception:
                pass
    msg: dict[str, Any] = {
        "role": role,
        "content": content,
        "cls": cls,
        # This window is re-serialized into the SAME transcript file that
        # ConversationLog.append writes, so it owes the reader the same
        # ordering guarantee: strictly after the row before it, even when
        # the clock does not tick between two appends. An explicit *ts*
        # (a row replayed from a channel transcript) is preserved verbatim
        # -- rewriting it would reorder the replay it came from.
        #
        # The floor is the later of the window tail and the last on-disk tail
        # this slot was told about, because the window is NOT a superset of
        # the file: a row written by another process is preserved as a
        # foreign line without entering ``messages``, so flooring on the
        # window alone leaves it un-ordered-against. Both candidates are
        # in-process reads -- no file I/O on the event loop.
        "ts": ts
        or monotonic_transcript_ts(
            latest_transcript_ts(
                self.messages[-1].get("ts") if self.messages else None,
                self._disk_tail_ts,
            ),
            datetime.now(timezone.utc),
        ),
    }
    if meta:
        msg["meta"] = meta
    # Stamp a per-row delivery identity. A client sees the SAME row through
    # two doors — the slot-detail HTTP rebuild and the live `chat_message`
    # broadcast — and must be able to tell "this row again" from "another row
    # that happens to look identical". `ts` cannot answer that: a coarse OS
    # clock stamps two rows appended in the same tick identically (the same
    # collision mergePreservedClientTs already guards), and content cannot
    # either, since two identical messages are legitimate. So identity is an
    # explicit id, minted once here, carried on the message dict, and thus
    # present on every path that ships it: persisted by _build_message_entry,
    # restored with the rest of `meta`, broadcast as `payload["meta"]`, and
    # returned by _prepare_messages.
    #
    # Random rather than a per-slot counter deliberately: a counter rebased
    # after a restore could reissue an id a restored row already holds, and a
    # colliding id makes a client DROP a real message. There is no such
    # failure mode for a random id.
    #
    # A caller-supplied `mid` (a row replayed from disk) is preserved — the
    # id must survive the round trip or a post-restart redelivery of that row
    # would not be recognisable.
    #
    # A restore caller passes ``mint_mid=False`` for a durable row whose disk
    # representation has no id. Minting one only in the in-memory window would
    # advertise an identity the full-history readers cannot resolve; features
    # such as response-level Fork would then bypass their legacy pagination
    # guard and fail against the still-id-less transcript. A supplied disk id
    # remains in ``meta`` regardless of this flag.
    #
    # Skipped for the wire-only roles: `chunk` is appended once per streamed
    # token and `done`/`streaming` are internal markers. None of them is ever
    # broadcast as a `chat_message` (the broadcast below excludes them) or
    # persisted (`_TRANSIENT_ROLES`), so an id would buy nothing and cost a
    # uuid4 plus a dict on the hottest path in the runner.
    if (
        mint_mid
        and role not in _WIRE_ONLY_ROLES
        and not (isinstance(msg.get("meta"), dict) and msg["meta"].get("mid"))
    ):
        existing = msg.get("meta")
        msg["meta"] = {
            **(existing if isinstance(existing, dict) else {}),
            "mid": mint_row_mid(),
        }
    self.messages.append(msg)
    self.total_messages += 1
    # A wire-only row (``chunk``/``done``/``streaming``) is never persisted
    # (``_build_message_entry`` returns ``None`` for it) and carries no
    # source link, so neither marking the slot dirty nor invalidating the
    # source-link cache buys anything for it -- and both are expensive during
    # a stream. ``chunk`` is appended once per streamed token, so dirtying the
    # slot makes the 5-second flush re-serialize and fsync the WHOLE transcript
    # every cycle a reply is streaming, putting the same durable bytes back on
    # disk (a 6-8 MB transcript is tens of ms of JSON on a GIL-holding worker),
    # and invalidating per token makes the next slots broadcast rescan the
    # whole window for PR/issue links. The durable row ``_flush_segment`` writes
    # when the segment closes still marks the slot dirty, so the reply is
    # persisted exactly once. ``chat_fork`` and ``_save_slot_to_history``'s
    # resumed-slot guard read ``_dirty`` as "unpersisted DURABLE state exists",
    # which a wire-only row is not -- a fork taken mid-stream drops nothing,
    # because the dropped rows were never going to reach disk.
    if role not in _WIRE_ONLY_ROLES:
        self.invalidate_source_links()
        self._dirty = True
    self._pending.append(msg)
    self.event.set()
    if broadcast and self._on_card_event and role in {"user", "assistant", "error", "done"}:
        self._on_card_event(self, role)  # type: ignore[operator]
    # Broadcast via global SSE when no HTTP stream reader is active
    # Skip: chunk (too noisy), done (internal). A "user" row is skipped by
    # DEFAULT because the composer that submitted it already rendered it
    # optimistically -- but that is only true of a message typed in this
    # dashboard. A row replayed from a CHANNEL transcript was typed in
    # Slack, so nothing rendered it here; those callers pass
    # ``broadcast_user=True`` or the message stays invisible until a full
    # transcript reload, arriving AFTER the reply it came before.
    if (
        broadcast
        and self._on_message
        and role not in ("chunk", "done")
        and (role != "user" or broadcast_user)
        and not self._has_reader
    ):
        self._on_message(self.key, msg)  # type: ignore[operator]
    # Record the row, which is a different question from delivering it. The
    # gate above answers "does anything still need to render this?"; this
    # one answers "did this row happen?", so the only conditions it may
    # share are the two that make a row not a row at all:
    #
    #  * the wire-only roles — `chunk` is one streamed token, `done` and
    #    `streaming` are markers — none of which is ever persisted, and
    #  * `broadcast`, which is what separates a live append from a REPLAY
    #    (`channel_slots._rebuild_window`, `chat_fork`, `session_transfer`
    #    all re-append historical rows with broadcast=False). Recording a
    #    replay would stamp a member as active just now for a message sent
    #    hours ago and reorder the roster on a rotation recovery.
    #
    # Notably NOT shared: `role != "user" or broadcast_user` and
    # `_has_reader`. A user row IS the event the roster's recency exists to
    # order by, and the composer having drawn its own bubble is not a reason
    # to forget it happened.
    if broadcast and self._on_row and role not in _WIRE_ONLY_ROLES:
        try:
            self._on_row(self.key, msg)  # type: ignore[operator]
        except Exception:
            logger.debug("slot row hook failed", exc_info=True)
    # Trim old messages to bound memory usage
    if len(self.messages) > _MAX_SLOT_MESSAGES:
        excess = len(self.messages) - _MAX_SLOT_MESSAGES
        # A trimmed leading window message may only join the frozen prefix
        # once it is actually on disk. Credit _disk_older_count only
        # for the persisted portion; the unpersisted overflow (should not
        # happen between 5s flushes) is logged rather than silently counted
        # as on-disk, which would have stranded those turns.
        persisted_trim = min(excess, self._disk_window_len)
        # The durable counter counts the WHOLE evicted slice, including the
        # unpersisted overflow the disk counter excludes. The two draw
        # different lines because they answer different questions:
        # ``_disk_older_count`` claims on-disk lines (its save contract), so
        # counting a row that never reached disk would corrupt the frozen
        # prefix. ``_disk_older_durable_count`` is a POSITION base with no
        # disk contract — if a durable row leaves the window uncounted,
        # every later absolute position shifts down and a poller's cursor
        # silently skips rows. Counting the lost rows instead makes a cursor
        # that pointed at them refuse loudly (``since < base``), which is
        # the recoverable outcome. Counted BEFORE the ``del`` below —
        # afterwards the slice is gone.
        durable_trim = durable_row_count(self.messages[:excess])
        del self.messages[:excess]
        self._resumed_count = max(0, self._resumed_count - excess)
        self._disk_older_count += persisted_trim
        self._disk_older_durable_count += durable_trim
        self._disk_window_len = max(0, self._disk_window_len - excess)
        if persisted_trim < excess:
            logger.warning(
                "Slot %s trimmed %d messages not yet flushed to disk; "
                "they will not be recoverable from history",
                self.key,
                excess - persisted_trim,
            )
        # The frozen prefix grew → its cached bytes are stale.
        self._frozen_prefix_cache = None
    # Hand back the row as appended (id included): a dual-writer that also
    # persists this message through ``ConversationLog.append`` needs the
    # ``meta.mid`` minted above so BOTH copies carry the same identity —
    # re-minting at the durable copy would give the reconciliation walk two
    # ids for one logical message. Read the id off the return with
    # :func:`row_mid`, never an inline ``meta`` poke.
    return msg


def update_message(
    self: _ChatSlot,
    ts: str,
    *,
    content: str | None = None,
    meta: dict | None = None,
    mid: str | None = None,
) -> dict | None:
    """Replace fields on an already appended message.

    Identified by ``mid`` (this row's server-minted identity) when one is
    given, falling back to ``ts``. Prefer ``mid``: two rows can carry the same
    ``ts``, so a ts lookup resolves the first match and can patch the wrong
    row.
    """
    return self._buffers.update_message(
        self,
        ts,
        content=content,
        meta=meta,
        mid=mid,
    )


def load_window(self: _ChatSlot, *prefetched: Any) -> None:
    """Run a pending window load now; a no-op once the window is loaded.

    *prefetched* are the loader's disk reads when the caller already made
    them off the loop (:func:`load_window_off_loop`). Without them the reads
    run here, before the load lock is taken: the lock only ever covers the
    replay, so a load on one thread never holds a reader on another (the
    loop's among them) behind a transcript read. The row stays pending
    until the replay has finished, so a reader on another thread waits for
    the whole window instead of seeing the list half filled.
    """
    loader = getattr(self, "_lazy_window", None)
    if loader is None or getattr(self, "_window_loader_thread", None) == threading.get_ident():
        # Loaded, or the replay's own read of the list it is filling.
        return
    if not prefetched:
        if _on_event_loop():
            # The load still runs (a reader must never see the empty stub),
            # but it reads the transcript on the loop: the path that got
            # here should have awaited ``load_window_off_loop`` first.
            logger.warning(
                "Loading the transcript window of %s on the event loop; "
                "the reader did not await load_window_off_loop first",
                self.key,
                stack_info=True,
            )
        read = getattr(loader, "read", None)
        if callable(read):
            # A failed read leaves the row pending and raises, so the next
            # read retries.
            prefetched = read(self)
    with _LAZY_WINDOW_LOCK:
        if getattr(self, "_lazy_window", None) is not loader:
            # Another reader's load landed while this one read, or the
            # window was replaced wholesale; that answer stands.
            return
        self._window_loader_thread = threading.get_ident()
        try:
            loader(self, *prefetched)
        except BaseException:
            # Fail closed: the row stays pending and the read raises, so
            # nothing can append to (and a later save rewrite the
            # transcript from) a window that never loaded. The next read
            # retries.
            self._messages = []
            self._lazy_window = loader
            raise
        else:
            self._lazy_window = None
            self._lazy_tail = None
        finally:
            self._window_loader_thread = None


def loaded_messages(self: _ChatSlot) -> list[dict[str, Any]]:
    """A copy of the window as it is in memory, without loading a pending one.

    Empty for a row whose window was never loaded, which is exactly what the
    save path needs: such a row holds no rows that are not already on disk,
    so its save is the metadata-only merge and the transcript is untouched.
    Copied under the load lock, which a replay holds throughout, so a save
    in the flush thread snapshots the window from before a replay or after
    it, never one the replay is still filling in place.
    """
    with _LAZY_WINDOW_LOCK:
        return list(self._messages)


def sidebar_messages(self: _ChatSlot) -> list[dict[str, Any]]:
    """The rows the sidebar summary projects from, without forcing a load.

    For a row whose window is still pending this is the bounded tail read at
    restore, which holds everything the summary looks at: the newest row's
    timestamp, the last conversational message and its options, and whether
    the last turn was interrupted.
    """
    if self.window_pending:
        return self._lazy_tail or []
    return self.messages


def to_dict(
    self: _ChatSlot, *, include_check_status: bool = False, dashboard_user: bool = False
) -> dict:
    source_links = self._summary_source_links()
    coordinator_approvals = self._coordinator_approvals
    coordinator_pending: list[dict] = (
        coordinator_approvals(self.key) if coordinator_approvals is not None else []
    )
    return self._projection.to_dict(
        self,
        include_check_status=include_check_status,
        source_links=source_links,
        coordinator_pending=coordinator_pending,
        prompt_roles=_PROMPT_ROLES,
        transient_roles=_TRANSIENT_ROLES,
        redact=_redact,
        parse_options=_parse_options,
        strip_options=_strip_options,
        parse_cls_meta=parse_cls_meta,
        is_turn_interrupted=is_turn_interrupted,
        is_system_notice=is_system_notice,
        latest_transcript_ts=latest_transcript_ts,
        last_message_preview=_last_message_preview,
        resolve_effective_agent=resolve_effective_agent,
        budget_source_links=_budgeted_source_links,
        # Bind the PUBLIC-only gate through the projection's positional
        # (links, include_check_status) callable so slot_projection.py needs
        # no change: the owner gate rides include_check_status as before, and
        # dashboard_user (a non-owner authenticated dashboard user) unlocks
        # status ONLY for links whose repo is known public.
        project_source_links=(
            lambda links, incl: _project_source_links(links, incl, dashboard_user=dashboard_user)
        ),
    )
