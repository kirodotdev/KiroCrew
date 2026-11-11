"""Per-slot memos behind the sidebar row projection (``SlotProjection.to_dict``).

Every slots frame and every ``GET /api/chat/slots`` re-projects every open
slot on the event loop. Most of a row is plain attribute reads, but the
transcript scan (last message, options, activity stamps), the display redaction of the title and preview, and the channel-link
rows cost tens of microseconds a slot, and on a sidebar of thousands of tabs
where one slot changed that is almost all wasted work.

Each memo here is keyed on the inputs of the value it stands for:

- the transcript summary on the row count plus a copy of every row the scan
  READ, each row's ``meta`` included (``RowsProbe`` records how far from the
  end it looked); the scan is a pure function of those rows, so equal rows
  give an equal answer even after an in-place edit that bumped nothing. A
  scan that read more than ``MAX_MEMO_TAIL`` rows stores nothing, so a slot
  mid tool-heavy turn or mid stream is recomputed every pass, as before;
- redacted strings on the text plus ``display_redaction_stamp()``, the same
  material the display caches fold into their own keys;
- the channel-link rows on the session map's mutation counter, the slot's
  own legacy Slack fields and the registered transports. The counter is the
  one key a writer must move; a source guard checks that every map writer does;
- the bare audience's source-link chips (``slot_transcript.to_dict``) on the
  identity of the extracted link list, which extraction rebuilds on every
  rerun. The owner and dashboard-user chips read the check-status and
  public-repo caches and audit, so they are rebuilt every pass.

The summary and title memos are not stored when what they would keep counts
more than ``MAX_MEMO_TEXT``, so a very long reply or title is recomputed each
pass instead of staying pinned on the slot. The other two hold bounded values:
the chips memo keeps the slot's own extracted link list (at most 64 links) and
at most three chips, and the link memo one row per channel binding.

Memos are consulted only inside a pass that set :data:`ROW_MEMO`
(``serialize_slots``), which reads the global parts of the keys once per pass.
Any other caller projects from scratch, exactly as before.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import chain
from typing import Any, NamedTuple


@dataclass(frozen=True)
class RowMemoContext:
    """The per-pass halves of the memo keys, read once by ``serialize_slots``."""

    #: ``display_redaction_stamp()``; a pass inside a battery-changing scope
    #: sets no context at all.
    redaction: tuple[object, ...]
    #: Session-map generation plus registered transports, or ``None`` when the
    #: session manager offers no generation (a test double): links unmemoized.
    links: tuple[object, ...] | None
    #: Hits and misses per memo in this pass, logged at debug level when it ends.
    counts: Counter[str] = field(default_factory=Counter, compare=False)


ROW_MEMO: ContextVar[RowMemoContext | None] = ContextVar("slot_row_memo", default=None)


class RowsProbe(Sequence[Any]):
    """A read-only view of a row list that records the oldest row a reader saw.

    ``low`` is the index of the oldest row read: a newest-first walk lowers it
    one row at a time, and anything else (forward iteration, indexing) reads as
    the whole list, so ``rows[low:]`` always covers every row that was read.
    """

    __slots__ = ("_rows", "low")

    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = rows
        self.low = len(rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __bool__(self) -> bool:
        return bool(self._rows)

    def __reversed__(self) -> Iterator[Any]:
        rows = self._rows
        for index in range(len(rows) - 1, -1, -1):
            if index < self.low:
                self.low = index
            yield rows[index]

    def __iter__(self) -> Iterator[Any]:
        self.low = 0
        return iter(self._rows)

    def __getitem__(self, index: Any) -> Any:
        self.low = 0
        return self._rows[index]


class RowSummary(NamedTuple):
    """What the sidebar row reads from the transcript, as a pure function of the rows."""

    last_ts: Any
    last_msg: str
    has_options: bool
    options_ts: str
    #: Already redacted.
    options: tuple[str, ...]
    prompt_preview: str
    last_conv_role: str
    last_activity_ts: str
    last_agent_ts: str


def summarize_rows(
    rows: Sequence[Any],
    *,
    transient_roles: frozenset[str],
    redact: Callable[[str], str],
    parse_options: Callable[[str], list[str]],
    strip_options: Callable[[str], str],
    is_system_notice: Callable[[str, dict], bool],
    last_message_preview: Callable[[str], str],
) -> RowSummary:
    """Scan *rows* newest-first for every transcript-derived field of the row."""
    # The newest DURABLE row: a transient row (the turn-end ``done`` row
    # among them) is never persisted, so a slot rebuilt from disk after a
    # gateway restart lacks it. Projecting from one would move ``last_ts``
    # backwards across the restart, and the dashboard stores ``last_ts`` as
    # the unread watermark it can only clear with a covering ``last_ts``.
    last_ts = next(
        (
            message.get("ts", "")
            for message in reversed(rows)
            if message.get("role") not in transient_roles
        ),
        "",
    )
    last_msg = ""
    has_options = False
    options_ts = ""
    options: list[str] = []
    prompt_preview = ""
    last_conv_role = ""
    # When the session last moved: the newest durable row of any role that is
    # not a system notice (a reload card is not activity). The scan runs
    # newest-first, so the first match IS the newest row by position; no
    # timestamp text is compared (rows mix naive and offset-aware formats).
    last_activity_ts = ""
    # When the AGENT last acted: the newest assistant / tool row. Kept apart
    # for readers that ask "did the agent act after X", which a user prompt
    # must not answer (the artifact freshness nudge).
    last_agent_ts = ""
    found_conv = False
    # A goal-loop cycle's prompt is a ``nudge`` row, and a cycle that ends with
    # ``nothing_to_do`` writes no reply after it. The loop has run past an
    # ``[OPTIONS:]`` ask older than that row, so that ask does not hold it.
    cycle_after_reply = False
    for message in reversed(rows):
        role = message.get("role")
        msg_meta = message.get("meta") or {}
        notice = is_system_notice(role, msg_meta)
        if role == "nudge" and not found_conv and not notice:
            cycle_after_reply = True
        if not last_activity_ts and role not in transient_roles and not notice:
            last_activity_ts = message.get("ts") or ""
        if not last_agent_ts and role in ("tool_call", "tool_result", "assistant") and not notice:
            last_agent_ts = message.get("ts") or ""
        if role in ("user", "assistant") and not notice:
            text = message.get("content") or ""
            if text:
                if not found_conv:
                    found_conv = True
                    last_conv_role = role
                    if role == "assistant" and not cycle_after_reply:
                        options = parse_options(text)
                        has_options = bool(options)
                        if has_options:
                            options_ts = str(message.get("ts") or "")
                            stripped = redact(strip_options(text))
                            prompt_preview = (
                                stripped[:240] + "…" if len(stripped) > 240 else stripped
                            )
                if not last_msg:
                    # Stripped, redacted and cut by the caller, which strips
                    # before redacting so markdown cannot split a credential
                    # signature and then rejoin it on the wire.
                    last_msg = last_message_preview(text)
        # An agent row is durable and not a notice, so once `last_agent_ts`
        # is set `last_activity_ts` is too.
        if found_conv and last_msg and last_agent_ts:
            break
    return RowSummary(
        last_ts=last_ts,
        last_msg=last_msg,
        has_options=has_options,
        options_ts=options_ts,
        options=tuple(redact(option) for option in options),
        prompt_preview=prompt_preview,
        last_conv_role=last_conv_role,
        last_activity_ts=last_activity_ts,
        last_agent_ts=last_agent_ts,
    )


#: The most rows a summary memo keeps a copy of. The scan breaks within a row or
#: two of the newest user or assistant row, but tool and chunk rows match none
#: of the roles it stops on, so during a tool-heavy turn or a long streamed
#: reply it reads back to the previous assistant row. Past this many rows the
#: copy and the compare cost more than the scan they would save, so the pass
#: computes and stores nothing; it is also what bounds the memory a slot holds.
MAX_MEMO_TAIL = 32

#: The most text a memo may keep, counted over everything the memo stores: every
#: value counts one, and a string or bytes value counts its length when longer. A memo outlives
#: the pass that built it, and some of what it stores is allocated for it alone
#: (an option label, the redacted title), so without a bound one long reply would
#: stay pinned on its slot. Past the budget the value is returned and nothing is
#: stored, so that slot is recomputed every pass, as before the memo.
MAX_MEMO_TEXT = 64 * 1024

_END = object()


def _within_budget(values: Iterable[Any], budget: int) -> bool:
    """Whether *values*, walked to their leaves, count at most *budget* (see above).

    The walk stops as soon as the count passes the budget, so its cost is bounded
    by the budget, not by the size of what it walks.
    """
    stack: list[Iterator[Any]] = [iter(values)]
    total = 0
    while stack:
        value = next(stack[-1], _END)
        if value is _END:
            stack.pop()
            continue
        if isinstance(value, (str, bytes)):
            # At least one, so a container of empty strings is counted too and
            # the walk stops after at most ``budget + 1`` values.
            total += max(1, len(value))
        else:
            total += 1
            if isinstance(value, dict):
                stack.append(chain.from_iterable(value.items()))
            elif isinstance(value, (list, tuple, set, frozenset)):
                stack.append(iter(value))
        if total > budget:
            return False
    return True


def _copy_row(row: dict) -> dict:
    """*row* and its ``meta`` dict copied; a row without ``meta`` keeps that shape."""
    copied = dict(row)
    meta = copied.get("meta")
    if isinstance(meta, dict):
        copied["meta"] = dict(meta)
    return copied


def memo_row_summary(
    slot: Any, rows: Sequence[Any], compute: Callable[[Sequence[Any]], RowSummary]
) -> RowSummary:
    """``compute(rows)``, reused while the rows it read are unchanged."""
    ctx = ROW_MEMO.get()
    if ctx is None:
        return compute(rows)
    size = len(rows)
    memo = slot._row_summary_memo
    if memo is not None:
        stamp, memo_size, tail, summary = memo
        if memo_size == size and stamp == ctx.redaction and tail == list(rows[size - len(tail) :]):
            ctx.counts["summary_hit"] += 1
            return summary
    ctx.counts["summary_miss"] += 1
    probe = RowsProbe(rows)
    summary = compute(probe)
    if size - probe.low > MAX_MEMO_TAIL:
        slot._row_summary_memo = None
        return summary
    # Each row and its ``meta`` are copied, so an in-place edit of either (a
    # streamed chunk's content, ``meta["done"]`` on a finished stream) shows as
    # a difference at the next compare. No writer edits anything deeper.
    tail = [_copy_row(row) for row in rows[probe.low :]]
    if not _within_budget((tail, summary), MAX_MEMO_TEXT):
        slot._row_summary_memo = None
        return summary
    slot._row_summary_memo = (ctx.redaction, size, tail, summary)
    return summary


def memo_redacted(
    slot: Any, texts: tuple[str, ...], redact: Callable[[str], str]
) -> tuple[str, ...]:
    """``redact`` of each of *texts* (the row's title fields), memoized per slot."""
    ctx = ROW_MEMO.get()
    if ctx is None:
        return tuple(redact(text) for text in texts)
    memo = slot._row_redacted_memo
    if memo is not None and memo[0] == ctx.redaction and memo[1] == texts:
        ctx.counts["redacted_hit"] += 1
        return memo[2]
    ctx.counts["redacted_miss"] += 1
    out = tuple(redact(text) for text in texts)
    if not _within_budget((texts, out), MAX_MEMO_TEXT):
        slot._row_redacted_memo = None
        return out
    slot._row_redacted_memo = (ctx.redaction, texts, out)
    return out
