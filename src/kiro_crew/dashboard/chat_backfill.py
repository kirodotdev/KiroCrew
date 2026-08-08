"""Choosing the history a freshly linked channel thread is seeded with.

When a dashboard session is linked to a Slack thread ("Send to Slack") or to a
configured non-Slack destination, the new thread is seeded with a preview of the
conversation so a human arriving there can orient.

This module decides *which* rows that preview contains. It deliberately does
**not** redact, format, or post them: each call site crosses a different egress
boundary with different formatting rules (Slack mrkdwn split at
``SLACK_MSG_LIMIT`` vs. plain text chunked at the transport's own
``max_message_chars``), and keeping the redactor call in the module that actually
sends is what lets ``security_posture`` account for those two sinks where the
sending happens.

The unit of selection is a **turn**: one ``user`` message plus the ``assistant``
message(s) that answered it. Selecting whole turns is what makes the preview
readable. The previous implementation sliced a fixed number of *raw* rows and
only then filtered by role, so a tail of ``tool`` rows consumed every slot --
five consecutive tool calls seeded nothing at all.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple
from urllib.parse import quote

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.urls import dashboard_origin

logger = logging.getLogger(__name__)

# The only roles a human-readable preview replays. An ALLOW-list, never a
# skip-list: ``_ChatSlot.append`` takes an unconstrained ``str`` role and the
# observed vocabulary already includes tool, error, system, done, chunk,
# streaming, queued, inject, file, compacting and permission. A skip-list would
# also behave differently across the two sources this module reads, because a
# transcript never carries the streaming-only roles (they are dropped by
# ``chat_persistence._build_message_entry``).
_CONVERSATIONAL_ROLES = ("user", "assistant")

DEFAULT_RECENT_TURNS = 5


def backfill_content(row: dict[str, Any]) -> str:
    """The row's text, coerced to ``str``.

    ``append`` does not constrain ``content``, and transcript rows are parsed
    from JSON, so a non-string can reach either source. One coercion point keeps
    both call sites honest.
    """
    return str(row.get("content") or "")


def _is_conversational(row: object) -> bool:
    """True when *row* is a human-readable message worth replaying.

    Excludes compaction notices, which are ``role == "assistant"`` rows carrying
    ``meta["kind"] == "compaction"``. Replaying one would read as a real answer
    and, worse, would count as a turn.

    ``meta`` is checked with ``isinstance`` rather than the ``or {}`` idiom used
    at ``state.py`` -- ``append``'s ``meta: dict | None`` is not enforced at
    runtime, so a truthy non-dict would raise ``AttributeError`` on ``.get``.
    """
    if not isinstance(row, dict):
        return False
    role = row.get("role")
    if role not in _CONVERSATIONAL_ROLES:
        return False
    if not backfill_content(row):
        return False
    meta = row.get("meta")
    if role == "assistant" and isinstance(meta, dict) and meta.get("kind") == "compaction":
        return False
    return True


def group_turns(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group already-filtered conversational rows into turns.

    A ``user`` row opens a turn; every following ``assistant`` row joins it. A
    leading ``assistant`` row with no ``user`` before it (a session that opens
    with an injected or restored reply) opens a turn of its own rather than being
    dropped.
    """
    turns: list[list[dict[str, Any]]] = []
    for row in rows:
        if row.get("role") == "user" or not turns:
            turns.append([row])
        else:
            turns[-1].append(row)
    return turns


def _transcript_prefix(state: Any, slot: Any, disk_older: int) -> list[dict[str, Any]]:
    """The conversational rows that precede the in-memory window, or ``[]``.

    ``slot.messages`` is a *windowed* view: after a restart it holds at most the
    last 500 rows, so a long session's opening turn exists only on disk.
    ``_disk_older_count`` is exactly how many persisted rows fall outside that
    window, so slicing the transcript to it yields the missing prefix with no
    risk of re-including a row memory already has.

    Blocking file I/O -- callers MUST offload this via ``asyncio.to_thread``.
    ``read_messages_chained`` reads and JSON-parses every ``tab_id`` sibling
    file, and rebuilds a glob-backed index when it is stale, so running it on
    the event loop would stall every other chat turn.
    """
    log = getattr(state, "conversation_log", None)
    if log is None:
        return []
    try:
        # slot_history_key, never _history_key_for: the latter prepends
        # "dashboard:" unconditionally, so a channel-born slot would resolve to
        # the nonexistent "dashboard:slack:<ts>" and read an empty file -- a
        # silent zero-turn result rather than an error. It also resolves an
        # UNBOUND channel slot (no mapped session key) onto the channel
        # transcript instead of a phantom "dashboard:slack_<ts>" file.
        rows = log.read_messages_chained(slot_history_key(slot))
    except Exception:
        logger.debug("backfill: could not read transcript for the first turn", exc_info=True)
        return []
    if not rows:
        return []
    # read_messages_chained may hand back the SHARED cached list object (and the
    # row dicts inside it) on a cache hit. Read only -- never mutate either, or
    # the cache is corrupted for every future reader.
    return [row for row in rows[:disk_older] if _is_conversational(row)]


class BackfillSelection(NamedTuple):
    """The history a fresh thread is seeded with, split at the gap.

    ``first_turn`` and ``recent`` are kept apart rather than concatenated
    because the caller has to place the gap marker *between* them, and only the
    caller knows the destination's markup. ``recent`` stays grouped **by turn**
    so a caller under a delivery budget can drop whole turns and fold them into
    ``skipped_turns`` rather than cutting a reply in half.
    """

    first_turn: list[dict[str, Any]]
    recent: list[list[dict[str, Any]]]
    skipped_turns: int

    @property
    def recent_rows(self) -> list[dict[str, Any]]:
        """The recent window flattened into posting order."""
        return [row for turn in self.recent for row in turn]

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Every selected row in posting order, gap marker excluded."""
        return [*self.first_turn, *self.recent_rows]


def select_backfill_messages(
    state: Any,
    slot: Any,
    *,
    recent_turns: int = DEFAULT_RECENT_TURNS,
    include_first_turn: bool = True,
) -> BackfillSelection:
    """Pick the messages a fresh channel thread should be seeded with.

    Returns the opening turn, the last ``recent_turns`` turns (grouped), and how
    many turns sit between the two ranges. ``skipped_turns`` is 0 when the ranges
    are contiguous or overlap, which is the caller's signal to omit the gap
    marker.

    Blocking: reads the transcript when the opening turn is off-window, so
    callers MUST invoke this through ``asyncio.to_thread`` rather than awaiting
    it on the event loop. It is kept synchronous precisely so the offload
    decision belongs to the caller.

    The off-window prefix and the live window are grouped into turns *together*,
    as one sequence, so the turn count is exact and a turn split across the flush
    boundary (a question on disk, its answer still in memory) is joined rather
    than counted twice.
    """
    rows = [row for row in slot.messages if _is_conversational(row)]
    prefix: list[dict[str, Any]] = []
    if include_first_turn:
        disk_older = getattr(slot, "_disk_older_count", 0) or 0
        if disk_older > 0:
            prefix = _transcript_prefix(state, slot, disk_older)

    turns = group_turns([*prefix, *rows])
    recent = turns[-recent_turns:] if recent_turns > 0 else []

    first_turn: list[dict[str, Any]] = []
    skipped = 0
    if include_first_turn and len(turns) > len(recent):
        first_turn = list(turns[0])
        skipped = len(turns) - len(recent) - 1

    return BackfillSelection(first_turn, [list(turn) for turn in recent], max(0, skipped))


def gap_summary(skipped: int) -> str:
    """``"4 earlier turns"`` / ``"1 earlier turn"``.

    Surface-agnostic on purpose: each call site wraps this in its own markup,
    because Slack's link syntax (``<url|text>``) is not the plain text a
    Telegram or Discord thread wants.
    """
    return f"{skipped} earlier turn{'' if skipped == 1 else 's'}"


def session_deep_link(dashboard_url: str, slot_key: str) -> str:
    """A browser link to *slot_key*'s dashboard tab, or ``""``.

    ``/chat?sid=<key>`` is the shape the SPA reads (``?slot=`` is a legacy
    alias). Returns ``""`` when no usable origin is configured -- the caller
    omits the link rather than emitting a broken one. ``dashboard_origin``
    already yields ``""`` for an empty, malformed, or non-HTTP URL.
    """
    if not slot_key:
        return ""
    origin = dashboard_origin(dashboard_url or "")
    if not origin:
        return ""
    return f"{origin}/chat?sid={quote(slot_key, safe='')}"
