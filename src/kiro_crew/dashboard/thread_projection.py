"""What a thread's model sees of its parent conversation, and when.

A thread is a full session, so the parent's history is not its history: nothing
is injected when the thread opens, and nothing is ever injected verbatim. Both
follow from sessions being perpetual -- a thread opened on a message deep in a
long parent would otherwise carry a copy of that parent's transcript forever,
and every later turn would pay for it again.

What the thread's FIRST turn gets instead is one summary block, built here from
three bands:

* the parent's own compaction summary, reused rather than re-derived, when the
  parent has one -- it is the parent's best existing account of what came before
  the window, and paying a model to summarize a summary buys nothing;
* the anchor window: the rows around the message the thread hangs off, with the
  anchor itself weighted, because that message is the reason the thread exists;
* everything after the anchor, chunk-folded -- summarized in groups so a parent
  that ran for a thousand rows since the anchor still fits the budget.

Every later turn gets only the delta: the rows the parent appended since the
cursor this module recorded. An empty delta injects NOTHING, rather than a block
saying nothing happened -- a thread sitting beside an idle parent must not pay a
block per turn for the privilege.

Addressing is by the parent's crew-log ``seq``, not by message id: the crew log's
``message/received`` entry carries no ``mid`` (its emitter deliberately does not
run where a slot appends, because there is no session id yet), so the anchor's
position cannot be looked up from its id at all. It is resolved ONCE when the
thread opens -- by TIME, the last parent entry at or before the anchor row's
timestamp -- and written into the anchor index, which is the cursor from then on.

Resolving by time rather than by correlating the Nth transcript row with the Nth
``message/*`` entry, because that correlation is broken by exactly the things a
long parent does: compaction rewrites the transcript, rewind and regenerate drop
rows from it, and neither touches the append-only log. Resolving once at open
rather than on each first turn, because a thread opened today may first be
written to after the parent compacted its anchor row away, and a timestamp with
no row to match is not resolvable. Seconds of precision is all the bands
need; the anchor's own text is exact regardless, because it comes from the
parent's TRANSCRIPT -- the log gives the position, the transcript gives the body.

Exact rows stay reachable without any of this: a thread carries a handle (parent
slot key, anchor mid, parent turn id) and the ``thread_context_read`` tool reads
the real rows on demand. The projection is the cheap always-on account; the tool
is the precise one, and a model that needs the exact bytes asks for them.

Model-free by construction: the summarizer is a callable seam, so every band,
budget and cursor rule in here is unit-testable without a model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: What an unresolved anchor position reads as. Treated as "unknown" by every
#: consumer and never as "the start of the log" -- see
#: :data:`kiro_crew.history.THREAD_ANCHOR_PARENT_LOG_SEQ_UNKNOWN`, which is the
#: same value for the same reason on the store side.
ANCHOR_SEQ_UNKNOWN = 0

# Bumped when the shape or the wording of a rendered block changes, and written
# into the provenance row beside the cursor. A reader comparing two threads'
# blocks needs to know they were built by the same rules; a cursor alone cannot
# say that.
PROJECTION_VERSION = "tp1"

# Rows before the anchor that the first turn's window reaches back over. Small
# on purpose: the anchor window exists to make the anchor legible, not to
# reconstruct the parent -- the band before it is what the compaction summary is
# for, and the read tool is what exact history is for.
N_BEFORE = 6

# The whole block's budget, in characters, about a thousand tokens. Chosen as a
# budget rather than a row count because that is what the thread actually pays
# per first turn, and rows vary by three orders of magnitude in size.
BLOCK_BUDGET_CHARS = 4_000

# Post-anchor rows per fold group. A group is one summarizer call, so this trades
# calls against fidelity: eight rows still fold into a sentence that names what
# happened, while one call per row would cost a parent's whole tail in calls.
FOLD_CHUNK_ROWS = 8

# How much of one row's text reaches the summarizer. A single tool result can be
# megabytes, and a row that blew the prompt would take the whole band with it.
MAX_ROW_CHARS = 600

# How much of one row's text the READ path returns. Wider than the summarizer's
# limit on purpose: the summary exists to say what happened, and this path exists
# to give back the phrasing behind it -- a paste, a stack trace, a path -- which a
# 600-character clip would cut off exactly when someone asked for it. Still a cap,
# because a row can be megabytes and the answer lands in a model's context; the
# read route bounds the whole payload as well and reports the rows it covered.
MAX_EXACT_ROW_CHARS = 4_000

# Entry types worth projecting. Everything else in a parent's log -- sampled
# stream bodies, spend accounting, permission bookkeeping -- describes how the
# parent ran rather than what was said, and a thread asking about the
# conversation is not helped by it.
PROJECTED_TYPES = (
    "message/received",
    "message/sent",
    "tool/called",
    "tool/completed",
    "turn/started",
    "turn/completed",
    "thread/opened",
    "thread/closed",
)

# A callable that turns one prompt into one line of text. ``run_bg_oneliner``
# bound to its session manager is the production one; a lambda is the test one.
Summarizer = Callable[[str], str]


@dataclass(frozen=True)
class ThreadHandle:
    """Where a thread hangs off its parent, in the terms each reader needs.

    Three addresses rather than one, because three different things resolve
    them: ``parent_slot_key`` is what the dashboard looks a live slot up by,
    ``anchor_mid`` is what the parent's transcript rows are keyed by, and
    ``parent_log_seq`` is what the crew log is addressed by. Deriving any one
    from another is exactly the lookup this module found it cannot do.
    """

    parent_slot_key: str
    anchor_mid: str
    parent_turn_id: str = ""
    parent_session_id: str = ""
    parent_log_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_slot_key": self.parent_slot_key,
            "anchor_mid": self.anchor_mid,
            "parent_turn_id": self.parent_turn_id,
            "parent_session_id": self.parent_session_id,
            "parent_log_seq": self.parent_log_seq,
        }


@dataclass(frozen=True)
class Window:
    """A half-open-free inclusive range of parent log seqs, and why it is that.

    ``empty`` is a first-class answer and not an error: a thread whose parent has
    appended nothing since the last projection is the common case, and the
    caller's job then is to inject nothing at all.
    """

    start: int
    end: int
    first_turn: bool

    @property
    def empty(self) -> bool:
        return self.end < self.start

    @property
    def rows(self) -> int:
        return 0 if self.empty else self.end - self.start + 1


@dataclass(frozen=True)
class Projection:
    """One rendered block, and everything the provenance row records about it.

    The field names below are the ones ``thread/context_projected`` declares, so
    :meth:`provenance` is a rename-free handover. A projector whose in-memory
    names drifted from the declared ones would put the translation in the emitter
    call site, where the first mismatch is a silently wrong log row rather than a
    type error.
    """

    text: str
    cursor_seq: int
    window_start_seq: int = 0
    summary_version: str = PROJECTION_VERSION
    fold_generation: int = 0
    rows: int = 0
    partial: bool = False
    bands: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    @property
    def block_chars(self) -> int:
        return len(self.text)

    def provenance(self) -> dict[str, Any]:
        """The ``data`` of this thread's ``thread/context_projected`` entry.

        ``bands`` and ``truncated`` are deliberately NOT here. They are how this
        build went, not what the thread was told, and the declared vocabulary is
        the contract a reader folds -- putting an undeclared key in ``data``
        would be the writer extending the schema by writing to it.
        """
        return {
            "cursor_seq": self.cursor_seq,
            "window_start_seq": self.window_start_seq,
            "summary_version": self.summary_version,
            "fold_generation": self.fold_generation,
            "block_chars": self.block_chars,
            "partial": self.partial,
            "rows": self.rows,
        }


def parse_ts(ts: str) -> int | None:
    """An ISO-8601 transcript timestamp as epoch milliseconds, or ``None``.

    The two clocks this module joins are written in different units by different
    writers -- the transcript in ISO-8601, the crew log in epoch ms -- so the join
    happens here and nowhere else. A naive timestamp is read as UTC, which is
    what the transcript writer produces.
    """
    text = (ts or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def resolve_anchor_log_seq(rows: Sequence[dict[str, Any]], anchor_ts: str) -> int:
    """Where *anchor_ts* falls in the parent's log: the last entry at or before it.

    Returns :data:`ANCHOR_SEQ_UNKNOWN` when the timestamp is unparseable or every
    entry is NEWER than it. The second case is the honest answer rather than
    "seq 1": an anchor older than the whole log means the log was rotated or
    started late, and pointing the window at the log's beginning would project a
    stretch of parent that has nothing to do with the anchor.
    """
    anchor_ms = parse_ts(anchor_ts)
    if anchor_ms is None:
        return ANCHOR_SEQ_UNKNOWN
    best = ANCHOR_SEQ_UNKNOWN
    for row in rows:
        seq = row.get("seq")
        when = row.get("time")
        if not isinstance(seq, int) or not isinstance(when, int):
            continue
        if when <= anchor_ms and seq > best:
            best = seq
    return best


def parent_log_position(session_id: str, anchor_ts: str) -> int:
    """The anchor's seq in the parent's crew log, or :data:`ANCHOR_SEQ_UNKNOWN`.

    Reads through :func:`~kiro_crew.crew_log.projection.open_session_log`'s
    iterator rather than :func:`~kiro_crew.crew_log.read.read_page`, which is the
    lighter primitive for this one question: a page resolves refs and builds a
    dict per row, and all this needs is ``seq`` and ``time``. The walk is the same
    either way -- the file has no random access -- so the saving is the whole
    per-row cost over a parent that may hold thousands of entries.

    Never raises. A session with no log, a log that faults mid-scan, a crew log
    turned off entirely: each answers UNKNOWN, and the thread opens. A thread that
    refused to open because its parent's context could not be positioned would
    trade the feature for the summary.
    """
    if not session_id:
        return ANCHOR_SEQ_UNKNOWN
    anchor_ms = parse_ts(anchor_ts)
    if anchor_ms is None:
        return ANCHOR_SEQ_UNKNOWN
    try:
        from kiro_crew.crew_log import projection as projections

        handle = projections.open_session_log(session_id)
        if handle is None:
            return ANCHOR_SEQ_UNKNOWN
        best = ANCHOR_SEQ_UNKNOWN
        for entry in handle.iter_from(1, strict_seq=False):
            if entry.time <= anchor_ms and entry.seq > best:
                best = entry.seq
        return best
    except Exception:
        # ``log_exception_text``, not ``exc_info``: this frame holds a ``CrewLog``
        # handle, whose write lease is released when the handle is dropped. An
        # ``exc_info`` triple keeps the traceback, the traceback keeps this frame,
        # and a handler that retains records would then hold the lease with it.
        from kiro_crew.crew_log.store import log_exception_text

        log_exception_text(
            logger,
            logging.WARNING,
            f"thread projection: could not position the anchor in session {session_id}'s log",
        )
        return ANCHOR_SEQ_UNKNOWN


@dataclass(frozen=True)
class ThreadState:
    """What a thread's OWN log says about where it hangs and what it has been told.

    One walk answers both, because both are entries in the same file: the parent
    edge is written once at ``session/opened``, and every projection appends a
    ``thread/context_projected``. ``cursor`` is ``None`` when no projection has
    been recorded, which is the definition of "this is the first turn" -- durable,
    so a gateway restart between opening a thread and writing to it does not
    replay the first projection or skip it.
    """

    parent_slot_key: str = ""
    parent_session_id: str = ""
    cursor: int | None = None
    fold_generation: int = 0

    @property
    def first_turn(self) -> bool:
        return self.cursor is None


#: Sessions whose own log shows they were not minted as a thread. Every dashboard
#: turn asks, and the answer costs a walk of that session's whole log, so an
#: ordinary chat pays for the thread feature on every turn and pays more the longer
#: it lives. Thread-ness is fixed at mint, in the FIRST ``session/opened`` entry.
#:
#: Populated only from that entry, and only when it was actually seen: a log whose
#: header has not landed is UNKNOWN rather than negative, since caching it would
#: strand a real thread whose lineage write lost the race with its own first turn.
#: A later re-attach entry is not consulted, so a re-attached thread cannot be
#: cached away.
_NOT_A_THREAD: set[str] = set()

#: Cap on the set above. Overflow drops the whole set rather than picking a victim:
#: forgetting costs the walk coming back, and an eviction policy would be machinery
#: guarding nothing. Reads run on worker threads, where losing a concurrent add to a
#: clear costs one more walk and nothing else.
_NOT_A_THREAD_MAX = 4096


def slot_log_sid(slot: Any) -> str:
    """The crew-log session id *slot*'s conversation is written under, or ``""``.

    The live ACP handle is asked first and answers for the narrowest window: it
    carries a session id only while a turn runs on THAT client, and a thread reads
    its parent BETWEEN the parent's turns. So the slot's own records answer after
    it -- the store it writes, then the store it was on before a switch, which is
    the same conversation. All three are in memory; naming the log after a restart
    takes a store lookup, which belongs to callers that can afford one. See the
    naming-the-parent's-log section of ``docs/system-specs/modules/history.md``.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    live = crew_log_emit.session_id_of(getattr(slot, "_acp_client", None))
    if live:
        return live
    for attr in ("_crew_log_opened_sid", "_crew_log_previous_sid"):
        value = getattr(slot, attr, "")
        if isinstance(value, str) and value:
            return value
    return ""


def read_thread_state(thread_session_id: str) -> ThreadState:
    """The thread's parent edge and projection cursor, from its own log.

    Deliberately NOT a second copy of the anchor on the thread's metadata. The
    dashboard keeps ONE record of an anchor -- the index beside the parent's
    transcript, guarded by the row it hangs off -- and Slack keeps its own only
    because a channel has no such row. This reads the lineage edge the mint
    already wrote (``session/opened.parent.slot``), which is enough to reach that
    one record; a duplicate handle here would be a shape to keep in step, and the
    first thing to drift would be a closed thread still looking open.

    Never raises: an unreadable log answers a state with no parent and no cursor,
    and the caller's answer to that is to inject nothing.
    """
    if not thread_session_id or thread_session_id in _NOT_A_THREAD:
        return ThreadState()
    parent_slot = ""
    parent_sid = ""
    cursor: int | None = None
    fold_generation = 0
    minted_as_thread: bool | None = None
    try:
        from kiro_crew.crew_log import projection as projections

        handle = projections.open_session_log(thread_session_id)
        if handle is None:
            return ThreadState()
        for entry in handle.iter_from(1, strict_seq=False):
            if entry.type == "session/opened":
                parent = entry.data.get("parent")
                if isinstance(parent, dict):
                    slot = parent.get("slot")
                    sid = parent.get("sid")
                    parent_slot = slot if isinstance(slot, str) else ""
                    parent_sid = sid if isinstance(sid, str) else ""
                if minted_as_thread is None:
                    minted_as_thread = bool(parent_slot)
            elif entry.type == "thread/context_projected":
                seq = entry.data.get("cursor_seq")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    # LAST one wins, not the highest: the cursor is a position the
                    # projector chose, and a rebuild may legitimately move it back.
                    cursor = seq
                generation = entry.data.get("fold_generation")
                if isinstance(generation, int) and not isinstance(generation, bool):
                    fold_generation = generation
    except Exception:
        # Same reason as ``parent_log_position``: this frame holds a ``CrewLog``
        # handle, and a rendered string keeps nothing where a traceback would keep
        # the handle's lease alive for as long as a handler keeps the record.
        from kiro_crew.crew_log.store import log_exception_text

        log_exception_text(
            logger,
            logging.WARNING,
            f"thread projection: could not read thread {thread_session_id}'s own log",
        )
        return ThreadState()
    if minted_as_thread is False:
        if len(_NOT_A_THREAD) >= _NOT_A_THREAD_MAX:
            _NOT_A_THREAD.clear()
        _NOT_A_THREAD.add(thread_session_id)
    return ThreadState(
        parent_slot_key=parent_slot,
        parent_session_id=parent_sid,
        cursor=cursor,
        fold_generation=fold_generation,
    )


def find_anchor(anchors: dict[str, dict[str, Any]], thread_slot_key: str) -> tuple[str, int]:
    """The ``(mid, parent_log_seq)`` of the anchor naming *thread_slot_key*.

    Answers ``("", ANCHOR_SEQ_UNKNOWN)`` when no anchor names this slot. An OPEN
    anchor wins over a closed one carrying the same slot: a slot key is reused by
    nothing, but a retracted mint followed by a successful one can leave both, and
    the live thread is the one asking.
    """
    closed: tuple[str, int] | None = None
    for mid, anchor in anchors.items():
        if anchor.get("thread_slot") != thread_slot_key:
            continue
        seq = anchor.get("parent_log_seq")
        found = (mid, seq if isinstance(seq, int) and not isinstance(seq, bool) else 0)
        if anchor.get("closed_at") is None:
            return found
        closed = found
    return closed or ("", ANCHOR_SEQ_UNKNOWN)


def select_window(
    *,
    anchor_log_seq: int,
    latest_log_seq: int,
    cursor: int | None,
) -> Window:
    """The parent rows this turn should account for.

    First turn (``cursor is None``): back over ``N_BEFORE`` rows from the anchor,
    forward to whatever the parent has now, so the anchor arrives with enough
    around it to be legible and the thread is not already behind. An UNRESOLVED
    anchor reaches back over ``N_BEFORE`` rows from the parent's TAIL instead --
    a small recent window, which is the conservative reading of "we do not know
    where the anchor is"; reaching back from seq 1 would project the whole parent
    for precisely the threads whose position is least trustworthy.

    Later turns: strictly after the cursor. Never back over ground already
    projected -- a sliding window that re-summarized its own history would drift,
    and the thread would be told the same thing in different words each turn.
    """
    if cursor is None:
        base = anchor_log_seq if anchor_log_seq > ANCHOR_SEQ_UNKNOWN else latest_log_seq
        start = max(1, base - N_BEFORE)
    else:
        start = cursor + 1
    return Window(start=start, end=latest_log_seq, first_turn=cursor is None)


def projected_rows(rows: Sequence[dict[str, Any]], window: Window) -> list[dict[str, Any]]:
    """Rows inside *window* that are worth projecting, in log order.

    Filters by type and by the reader's own contract: an ``ignorable`` row is the
    writer's promise that nothing depends on it having been read, which makes it
    exactly the row a budget should drop first.
    """
    keep: list[dict[str, Any]] = []
    for row in rows:
        seq = row.get("seq")
        if not isinstance(seq, int) or seq < window.start or seq > window.end:
            continue
        if row.get("ignorable"):
            continue
        if row.get("type") not in PROJECTED_TYPES:
            continue
        keep.append(row)
    return keep


def digest_row(row: dict[str, Any], *, max_chars: int = MAX_ROW_CHARS) -> str:
    """One line naming what a row was, short enough to put many in a prompt.

    ``max_chars`` bounds the message bodies. The default suits a fold prompt; the
    read path passes :data:`MAX_EXACT_ROW_CHARS`, since its whole job is handing
    back wording the summary had to compress.
    """
    kind = str(row.get("type") or "")
    data = row.get("data")
    data = data if isinstance(data, dict) else {}
    seq = row.get("seq")
    if kind == "message/received":
        who = str(data.get("role") or data.get("source") or "user")
        return f"[{seq}] {who}: {_clip(data.get('text'), max_chars)}"
    if kind == "message/sent":
        return f"[{seq}] assistant: {_clip(data.get('text'), max_chars)}"
    if kind == "tool/called":
        return f"[{seq}] tool call {data.get('name') or data.get('tool') or '?'}"
    if kind == "tool/completed":
        status = data.get("status") or data.get("outcome") or "done"
        return f"[{seq}] tool {data.get('name') or data.get('tool') or '?'} -> {status}"
    if kind == "turn/started":
        return f"[{seq}] turn {data.get('turn')} started"
    if kind == "turn/completed":
        return f"[{seq}] turn {data.get('turn')} completed"
    if kind == "thread/opened":
        return f"[{seq}] a thread was opened on this conversation"
    if kind == "thread/closed":
        return f"[{seq}] a thread on this conversation was closed"
    return f"[{seq}] {kind}"


def _clip(value: Any, limit: int = MAX_ROW_CHARS) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + " …"


@dataclass(frozen=True)
class Fold:
    """What one banded fold produced, and how far through its rows it got."""

    summaries: list[str]
    #: Seq of the last row actually summarized, 0 when the fold produced nothing.
    last_seq: int
    #: Every row handed in was summarized. False means the budget stopped it.
    complete: bool


def fold_chunks(
    rows: Sequence[dict[str, Any]],
    summarize: Summarizer,
    *,
    chunk_rows: int = FOLD_CHUNK_ROWS,
    budget_chars: int | None = None,
) -> Fold:
    """Summaries of *rows* in groups, one summarizer call per group.

    A failed call degrades that ONE group to its own digest lines rather than
    losing the band: a summarizer is a model call over the network, and a thread
    that gets a rougher account of eight rows is better served than one that gets
    no account of two hundred.

    ``budget_chars`` bounds the summarizer calls this fold may make. Each call is
    a model round trip awaited inside the turn, and text past the caller's budget
    is discarded when the block is rendered, so a parent that gained two hundred
    rows would otherwise pay for two dozen calls to produce output nobody reads.
    The FIRST group always runs whatever the budget says: that is what makes the
    cursor advance by at least one group per turn, so a thread facing a long
    backlog catches up over several turns instead of stalling on a spent budget.
    Rows the fold did not reach stay in the next turn's delta, which is why the
    caller must take ``last_seq`` as its cursor when ``complete`` is false.
    """
    out: list[str] = []
    spent = 0
    last_seq = 0
    for index in range(0, len(rows), chunk_rows):
        if index and budget_chars is not None and spent >= budget_chars:
            return Fold(summaries=out, last_seq=last_seq, complete=False)
        group = rows[index : index + chunk_rows]
        lines = "\n".join(digest_row(row) for row in group)
        prompt = (
            "Summarize what happened in this slice of a conversation log, in at "
            "most two sentences. Name decisions, questions and outcomes; skip "
            "mechanics.\n\n" + lines
        )
        try:
            text = summarize(prompt).strip()
        except Exception:
            logger.warning("thread projection: fold group failed, using digests", exc_info=True)
            text = lines
        summary = text or lines
        out.append(summary)
        spent += len(summary) + 1
        last_seq = max(last_seq, _seq_of(group[-1]))
    return Fold(summaries=out, last_seq=last_seq, complete=True)


@dataclass
class _Band:
    label: str
    lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        body = "\n".join(line for line in self.lines if line.strip())
        return f"{self.label}\n{body}" if body else ""


def build_projection(
    *,
    handle: ThreadHandle,
    window: Window,
    rows: Sequence[dict[str, Any]],
    summarize: Summarizer,
    anchor_text: str = "",
    parent_compaction_summary: str = "",
    budget_chars: int = BLOCK_BUDGET_CHARS,
    partial: bool = False,
    window_unread: bool = False,
) -> Projection:
    """The block to inject for this turn, or an empty projection for nothing.

    ``anchor_text`` and ``parent_compaction_summary`` are passed in rather than
    read here, because neither lives in the crew log: the anchor's body is a
    parent transcript row, and a compaction summary is an assistant row the
    parent tagged ``compaction`` -- the log records only that a compaction
    happened and by how much.
    """
    kept = projected_rows(rows, window)
    nothing = Projection(
        text="",
        # The cursor does NOT advance over a window that produced no block. It
        # stands where the last real projection left it, so the rows this window
        # skipped stay in the next delta rather than being silently consumed by a
        # turn that was told nothing about them.
        cursor_seq=max(window.start - 1, 0),
        window_start_seq=window.start,
        partial=partial,
    )
    # ``window_unread`` means the caller ASKED for this window and the read failed,
    # which is a different thing from a window that held nothing. Publishing here
    # would commit ``cursor_seq = window.end`` over rows nobody read, and because
    # every later window starts after the cursor those rows would be skipped
    # silently and with no delta that could ever recover them. So a failed read
    # publishes nothing and leaves the cursor where it was, and the next turn asks
    # for the same window again. The first turn is the dangerous case precisely
    # because it has a preamble to render and would otherwise look productive.
    if window_unread:
        return nothing
    # A first turn still has something to say with no projectable log rows at all:
    # the anchor is the reason the thread exists, and its text comes from the
    # parent's transcript rather than the log, so a parent whose crew log is off
    # or whose window holds only bookkeeping must not cost the thread the one
    # message it is about. Every LATER turn with no rows is a no-op by definition.
    # Emptiness is not its own exit, because an EMPTY window on a first turn is the
    # case the preamble exists for: a parent whose crew log has no tail yet still
    # has the message this thread hangs off. Exiting on it published
    # ``cursor_seq`` anyway, and a cursor of any value makes the next window a
    # later turn -- so the anchor band would be skipped on turn one and unreachable
    # after it, for the thread that has the least other context. An empty window
    # keeps ``kept`` empty either way, so the preamble is the only thing this admits.
    has_preamble = window.first_turn and bool(anchor_text.strip() or handle.anchor_mid)
    if not kept and not has_preamble:
        return nothing
    bands: list[_Band] = []
    # What the folds may spend on summarizer calls, and how far they got. The
    # preamble bands are charged against the same budget the render trims to, so a
    # long anchor buys fewer folds rather than folds whose output gets cut off.
    fold_budget = budget_chars
    folded_through: int | None = None

    if window.first_turn and parent_compaction_summary.strip():
        bands.append(
            _Band(
                "Earlier in the parent conversation (the parent's own summary):",
                [_clip(parent_compaction_summary, budget_chars // 3)],
            )
        )

    if window.first_turn:
        anchor_band = _Band("The message this thread was opened on:")
        if anchor_text.strip():
            anchor_band.lines.append(_clip(anchor_text, budget_chars // 3))
        else:
            anchor_band.lines.append(f"(parent message {handle.anchor_mid})")
        bands.append(anchor_band)
        fold_budget -= sum(len(line) + 1 for band in bands for line in band.lines)

        # An UNRESOLVED anchor puts every row on the "since" side rather than
        # inventing a split: ``parent_log_seq`` is 0 then, and a before-band
        # labelled "just before it" around a position nobody knows would read as
        # fact. The window is already the small recent one select_window chose.
        before = [row for row in kept if _seq_of(row) < handle.parent_log_seq]
        if before:
            fold = fold_chunks(before, summarize, budget_chars=fold_budget)
            if fold.summaries:
                bands.append(_Band("Just before it:", fold.summaries))
            fold_budget -= sum(len(text) + 1 for text in fold.summaries)
            if not fold.complete:
                folded_through = fold.last_seq
        after = [row for row in kept if _seq_of(row) >= handle.parent_log_seq]
        label = "Since then in the parent conversation:"
    else:
        after = kept
        label = "New in the parent conversation since the last update:"

    # A fold that stopped early owns the cursor: the rows past it were never read
    # to the thread, so this turn must not claim them and the later band must not
    # jump over them either.
    if after and folded_through is None:
        fold = fold_chunks(after, summarize, budget_chars=fold_budget)
        if fold.summaries:
            bands.append(_Band(label, fold.summaries))
        if not fold.complete:
            folded_through = fold.last_seq

    rendered = [text for text in (band.render() for band in bands) if text]
    if not rendered:
        return nothing

    text = "\n\n".join(rendered)
    truncated = False
    if len(text) > budget_chars:
        text = text[:budget_chars].rstrip() + "\n\n(trimmed to fit the context budget)"
        truncated = True

    # A stopped fold leaves the cursor on the last row it summarized, never at the
    # window's end: the rest were not accounted for to the thread, and the cursor
    # is the only record of that. ``window.start - 1`` is the floor, so a fold that
    # summarized nothing cannot walk the cursor backwards over settled ground.
    cursor_seq = window.end
    if folded_through is not None:
        cursor_seq = max(folded_through, window.start - 1)

    return Projection(
        text=text,
        cursor_seq=cursor_seq,
        window_start_seq=window.start,
        rows=len(kept),
        partial=partial,
        bands=tuple(band.label for band in bands if band.render()),
        truncated=truncated,
    )


def _seq_of(row: dict[str, Any]) -> int:
    seq = row.get("seq")
    return seq if isinstance(seq, int) else 0


#: How long one fold group's model call may take before the band degrades to its
#: own digest lines. A turn waits on this, so it is a user-visible delay and not a
#: background budget: a slow summarizer must cost the thread a rougher account of
#: its parent, never a turn that appears to hang.
SUMMARIZE_TIMEOUT_S = 25.0

#: How long a turn waits for its own cursor entry to reach disk before going ahead
#: without the block. Short because a healthy writer settles in milliseconds and
#: this sits directly in the turn's path: the number bounds how long a wedged disk
#: can delay a thread's first answer, and the block is not lost when it expires --
#: it arrives on the next turn instead.
CURSOR_SETTLE_TIMEOUT_S = 5.0

#: How long a first turn waits for its own ``session/opened`` to reach disk before
#: projecting. That entry carries the parent edge :func:`read_thread_state` resolves
#: a thread by, so a read that beats the write sees a thread as an ordinary chat and
#: the first reply silently carries no account of its parent. Shorter than the cursor
#: bound because this one blocks the turn BEFORE any work is done, and the writer's
#: own batch deadline is milliseconds: a wait this long already means the writer is
#: wedged, and then the delta on the next turn is the honest recovery.
LINEAGE_SETTLE_TIMEOUT_S = 2.0


def loop_bridged_summarizer(
    sessions: Any,
    loop: Any,
    *,
    model: str | None = None,
    crew_log_session_key: str = "",
    timeout: float = SUMMARIZE_TIMEOUT_S,
) -> Summarizer:
    """A SYNC :data:`Summarizer` that runs the async model call on *loop*.

    The projector is synchronous on purpose -- every band, budget and cursor rule
    in it is then decidable without a running loop, which is what lets the whole
    thing be unit-tested with a lambda. The model call underneath is async. This
    is the one place that gap is bridged, rather than making the entire projector
    async and pushing the colour of the function into its tests.

    Safe because of WHERE it runs: the caller drives the projector inside
    ``asyncio.to_thread``, so *loop* is free to service the submitted coroutine
    while this worker thread blocks on the result. Calling it ON the loop thread
    would deadlock, so it refuses that rather than hanging.
    """
    import asyncio as _asyncio

    from kiro_crew.llm_helpers import run_bg_oneliner

    def _summarize(prompt: str) -> str:
        try:
            running = _asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            raise RuntimeError(
                "loop_bridged_summarizer must run off the event loop; "
                "drive build_projection inside asyncio.to_thread"
            )
        future = _asyncio.run_coroutine_threadsafe(
            run_bg_oneliner(
                sessions,
                prompt,
                model=model,
                sel_source="thread_context_projection",
                crew_log_kind="thread_context_projection",
                crew_log_session_key=crew_log_session_key,
                max_output_bytes=2_000,
                timeout=timeout,
            ),
            loop,
        )
        return future.result(timeout=timeout + 5.0)

    return _summarize


#: What the injected block is attributed to in its frame header. Named for the
#: reader rather than for the module, because it renders as ``[Background context
#: from "the parent conversation"]`` in front of a model.
CONTEXT_SOURCE = "the parent conversation"


def render_context_entry(projection: Projection, handle: ThreadHandle) -> dict[str, Any]:
    """The ``append_pending_context`` entry carrying this block into the next turn.

    ``content`` and ``source`` are the queue's own contract -- the drain frames
    them as ``[Background context from "<source>"]`` with its silent-consumption
    line -- so a producer that spelled them anything else would be dropped with a
    ``KeyError`` at drain time rather than at write time.

    No ``maxAge``: the queue expires entries so a stale app payload does not
    surface turns later, and this payload cannot go stale in that sense. It is
    written immediately before the turn that consumes it, and the thing that
    WOULD make it stale -- the parent moving on -- is what the next turn's delta
    is for. An expiry here would silently drop the first turn's whole context if a
    person typed slowly.

    ``authorized`` is deliberately absent: this is the system's own account of the
    parent, not a user-authored note, and marking it authorized would let it pass
    checks written for content a human stands behind.
    """
    return {
        "source": CONTEXT_SOURCE,
        "content": (
            "This thread was opened on a message in another conversation. What "
            "follows is a SUMMARY of that conversation, never its transcript. To "
            "read the messages behind it, call thread_context_read with the "
            "sequence numbers above; it returns their wording, trimming only a "
            "very long one.\n\n" + projection.text
        ),
        # Carried for the emitter that writes the provenance row after the turn is
        # queued, and for a reader debugging a slot's queue. The drain ignores
        # every key but ``content`` and ``source``, so these ride along harmlessly.
        "thread_handle": handle.to_dict(),
        "projection_version": projection.summary_version,
        "cursor_seq": projection.cursor_seq,
    }


#: Intents from the parent's stored summary that reach the first band. The band
#: exists to say what the parent conversation is ABOUT, and the payload is already
#: ordered most-recently-touched first, so the head of it is the useful part.
PARENT_INTENTS_SHOWN = 5


def render_parent_summary(payload: dict[str, Any] | None, *, stale: bool = False) -> str:
    """The parent's own stored summary, as the first band's text.

    This is the band Raymond asked to be REUSED rather than re-derived, and the
    artifact it reuses is the session intent summary the dashboard already
    maintains per conversation -- NOT a compaction summary, which does not exist
    as stored text anywhere: the crew log's ``on_compaction_applied`` records
    percentages, and nothing writes a compaction summary row. Reusing this one
    costs no model call and carries no verbatim transcript.

    A STALE payload is still used, and said to be stale. The parent moved on since
    it was generated, which is exactly what the window and the delta cover; an
    empty band because the summary is one append behind would be worse than a
    slightly old one labelled as old.
    """
    if not isinstance(payload, dict):
        return ""
    intents = payload.get("intents")
    if not isinstance(intents, list):
        return ""
    lines: list[str] = []
    for raw in intents[:PARENT_INTENTS_SHOWN]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        status = str(raw.get("status") or "").strip()
        lines.append(f"- {title} ({status})" if status else f"- {title}")
    if not lines:
        return ""
    head = "What that conversation has been about"
    if stale:
        head += " (as of its last summary; it has moved on since)"
    return head + ":\n" + "\n".join(lines)


async def _parent_summary_band(state: Any, parent_key: str) -> str:
    """The parent's stored summary for the first band, best effort."""
    import asyncio

    log = getattr(state, "conversation_log", None)
    if log is None or not parent_key:
        return ""
    try:
        payload, stale = await asyncio.to_thread(log.read_intent_summary, parent_key)
    except Exception:
        logger.warning("thread projection: parent intent summary unreadable", exc_info=True)
        return ""
    return render_parent_summary(payload, stale=stale)


async def project_for_turn(
    state: Any,
    slot: Any,
    *,
    parent_compaction_summary: str = "",
) -> Projection | None:
    """Queue this thread's parent-context block for the turn about to run.

    Returns the projection, or ``None`` when this slot is not a thread or the
    window produced nothing. Best effort throughout: a thread whose parent has
    been deleted, whose crew log is off, or whose summarizer is unreachable runs
    its turn with no block rather than not running.

    Ordering matters and is the reason this is called from ``_run_chat`` rather
    than from the open route: the block must be in the queue BEFORE
    :func:`~kiro_crew.dashboard.chat_runner.drain_pending_context` reads it, and it
    must describe the parent as of NOW rather than as of whenever the thread was
    opened -- a thread opened this morning and first written to this evening wants
    the evening's parent.

    The provenance row is written even for a projection that injected nothing
    (``rows=0``). That is what makes a quiet turn distinguishable from a turn the
    projector never ran on, and it is also how the cursor stays recorded.
    """
    import asyncio

    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log import read as crew_log_read
    from kiro_crew.dashboard.chat_utils import slot_history_key

    thread_sid = crew_log_emit.session_id_of(getattr(slot, "_acp_client", None)) or ""
    if not thread_sid:
        return None
    thread_state = await asyncio.to_thread(read_thread_state, thread_sid)
    if not thread_state.parent_slot_key:
        return None  # not a thread, or its lineage edge is unreadable

    parent_slot = state.get_slot(thread_state.parent_slot_key)
    if parent_slot is None:
        return None
    log = getattr(state, "conversation_log", None)
    if log is None:
        return None

    try:
        anchors = await asyncio.to_thread(log.read_thread_anchors, slot_history_key(parent_slot))
    except Exception:
        logger.warning("thread projection: parent anchor index unreadable", exc_info=True)
        return None
    anchor_mid, anchor_seq = find_anchor(anchors, slot.key)
    if not anchor_mid:
        return None

    # Not the live handle alone: a parent between turns has no session id on its
    # client, and reading that as "no log" is what leaves a correctly anchored
    # thread with an empty summary band. See :func:`slot_log_sid`.
    parent_sid = slot_log_sid(parent_slot) or thread_state.parent_session_id
    handle = ThreadHandle(
        parent_slot_key=thread_state.parent_slot_key,
        anchor_mid=anchor_mid,
        parent_session_id=parent_sid,
        parent_log_seq=anchor_seq,
    )

    rows: list[dict[str, Any]] = []
    latest = 0
    window_unread = False
    if parent_sid:
        try:
            # ``1`` to ``0`` reads no rows and reports the true tail, which is what
            # the window's end must be before the window can say which rows to ask
            # for. One extra walk of an append-only file, against summarizing a
            # window whose end was a stale cached figure.
            probe = await asyncio.to_thread(crew_log_read.read_page, parent_sid, 1, 0)
            latest = int(probe.get("last_seq") or 0)
        except Exception:
            logger.warning("thread projection: parent log tail unreadable", exc_info=True)
    window = select_window(
        anchor_log_seq=anchor_seq, latest_log_seq=latest, cursor=thread_state.cursor
    )
    if parent_sid and not window.empty:
        try:
            page = await asyncio.to_thread(
                crew_log_read.read_page, parent_sid, window.start, window.end
            )
            rows = list(page.get("entries") or [])
        except Exception:
            # Recorded, not swallowed: the projection must not advance its cursor
            # over a window it asked for and did not get.
            window_unread = True
            logger.warning("thread projection: parent window unreadable", exc_info=True)

    anchor_text = ""
    if window.first_turn:
        anchor_text = await _anchor_text(state, parent_slot, anchor_mid)
        if not parent_compaction_summary:
            parent_compaction_summary = await _parent_summary_band(
                state, slot_history_key(parent_slot)
            )

    # A reply the parent is still writing is not in its crew log yet -- the log gets
    # one ``message/sent`` when the turn ENDS -- so the window cannot see it and the
    # thread would be told nothing about the very reply it was opened beside. Read
    # it from the parent slot, which is where the live chunks are, and mark the
    # projection ``partial``: the summary then describes an unfinished answer, and
    # the next turn's delta carries the rest once the log has it.
    live_text, partial = _parent_live_text(parent_slot)
    if live_text:
        anchor_text = (
            f"{anchor_text}\n\nThe reply being written in that conversation right now:\n"
            f"{_clip(live_text, MAX_ROW_CHARS * 2)}"
        ).strip()

    summarize = loop_bridged_summarizer(
        state.sessions,
        asyncio.get_running_loop(),
        crew_log_session_key=slot.key,
    )
    projection = await asyncio.to_thread(
        build_projection,
        handle=handle,
        window=window,
        rows=rows,
        summarize=summarize,
        anchor_text=anchor_text,
        parent_compaction_summary=parent_compaction_summary,
        partial=partial,
        window_unread=window_unread,
    )

    # PERSIST THE CURSOR, THEN PUBLISH THE BLOCK -- in that order, and the order is
    # the whole point.
    #
    # The cursor in this entry is the durable authority the next window starts from
    # (`read_thread_state` reads it, `select_window` starts at `cursor + 1`). The
    # emitter hands the append to a buffered writer and returns without writing, so
    # a block queued first can be DRAINED into this turn while its cursor is still
    # in the buffer. Lose that write and the next turn reads the old cursor and
    # re-injects rows the thread was already told -- the same parent summary twice,
    # with nothing on any surface saying so.
    #
    # Publishing inside the write's own completion hook makes the ordering hold
    # without this coroutine touching the writer: the hook runs when the entry
    # lands, `_dropped` is set first on the path where it never will, and the wait
    # below suspends only THIS turn -- the event loop keeps serving everything else,
    # which is the emitter's own never-block-the-loop rule.
    loop = asyncio.get_running_loop()
    settled = asyncio.Event()
    dropped = False
    entry = render_context_entry(projection, handle) if not projection.empty else None

    def _on_drop() -> None:
        nonlocal dropped
        dropped = True

    def _on_settled() -> None:
        # The writer's thread. Both hops go through the loop: the pending queue is
        # the turn's own structure, and nothing else writes it off-loop.
        if entry is not None and not dropped:
            loop.call_soon_threadsafe(slot.append_pending_context, entry)
        loop.call_soon_threadsafe(settled.set)

    crew_log_emit.on_thread_context_projected(
        thread_sid,
        anchor={
            "surface": "dashboard",
            "conversation": handle.parent_slot_key,
            "mid": handle.anchor_mid,
        },
        cursor_seq=projection.cursor_seq,
        window_start_seq=projection.window_start_seq,
        summary_version=projection.summary_version,
        fold_generation=projection.fold_generation,
        block_chars=projection.block_chars,
        partial=projection.partial,
        rows=projection.rows,
        after=_on_settled,
        on_permanent_drop=_on_drop,
    )
    try:
        await asyncio.wait_for(settled.wait(), timeout=CURSOR_SETTLE_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        # The writer is behind, not broken. The hook still fires when the entry
        # lands, and the block it queues then rides the NEXT turn -- one turn late
        # against a cursor that already accounts for those rows, so the thread is
        # told about them exactly once either way. Waiting longer here would trade
        # that for a turn that appears to hang.
        logger.warning(
            "thread projection: cursor for thread %s not settled in %.0fs; "
            "its context block will arrive on a later turn",
            thread_sid,
            CURSOR_SETTLE_TIMEOUT_S,
        )
    return projection


def _parent_live_text(parent_slot: Any) -> tuple[str, bool]:
    """The parent's still-streaming reply, and whether it was streaming at all.

    ``("", False)`` for an idle parent, and ``("", True)`` for one that IS mid-turn
    but whose text could not be read -- the honest fallback, because a projection
    that reported ``partial=False`` there would claim the parent had finished.
    Non-consuming: :func:`~kiro_crew.dashboard.chat_threads.in_flight_snapshot`
    copies the chunk rows rather than draining them, so reading here cannot take
    bytes away from the parent's own transcript.
    """
    try:
        from kiro_crew.dashboard.chat_threads import in_flight_snapshot

        text = in_flight_snapshot(parent_slot)
    except Exception:
        logger.warning("thread projection: parent live text unreadable", exc_info=True)
        return "", False
    if text:
        return text, True
    return "", bool(getattr(parent_slot, "running", False))


async def _anchor_text(state: Any, parent_slot: Any, anchor_mid: str) -> str:
    """The anchor message's own text, from the parent's TRANSCRIPT.

    The transcript rather than the log because the log's message rows are the
    thing that carries no ``mid``; this is the same read the thread drawer already
    does to render the quoted parent, so the two cannot show different text.
    """
    try:
        from kiro_crew.dashboard.chat_threads import _transcript

        rows = await _transcript(state, parent_slot)
    except Exception:
        logger.warning("thread projection: parent transcript unreadable", exc_info=True)
        return ""
    from kiro_crew.dashboard.state import row_mid

    for row in rows:
        if row_mid(row) == anchor_mid:
            return str(row.get("content", "") or "")
    return ""
