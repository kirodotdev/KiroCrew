"""Pure helpers behind the compaction method ladder.

``session_compaction.CompactionCoordinator`` walks
down from ``session.compaction_method`` when the auto-compact threshold fires. A
rotation method (``soft``, ``shake``) turns on three judgments that are
arithmetic over a row list and a few numbers, never I/O: where the recent tail
ends (:func:`split_tail`), how much of the window a rotation leaves in use
(:func:`project_pct_after`), and what an elided digest of the dropped slice
looks like (:func:`shake_elide`). They live in one dependency-light module so
the coordinator stays a lifecycle module and the surfaces, which own the
transcript rows, run the same arithmetic the ladder ran.

Rows are mappings with ``role`` and ``content`` and, when they come from the
transcript, ``ts``: the shape ``ConversationLog.read_messages_chained`` returns
and ``_ChatSlot.messages`` holds. Callers pass rows already role-filtered
(``context.RECALL_ROLES``); this module does not decide which roles a replay
carries. :func:`walk_tail` is the one budgeted walk: ``build_session_replay``
renders through it and :func:`split_tail` cuts through it, so the tail this
module computes is the tail the successor session is seeded with, and the
budget both sides use is the replay's own (``context_budget.replay_budget_chars``).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from kiro_crew.config.sections import (
    COMPACTION_METHOD_NATIVE,
    COMPACTION_METHOD_SHAKE,
    COMPACTION_METHOD_SOFT,
    COMPACTION_METHODS,
)

# The public surface is the names another module imports; the arithmetic
# helpers below (``elide_fences``, ``replay_line``, the dataclasses) are
# internal and reachable for tests without being advertised here.
__all__ = [
    "COMPACTION_METHODS",
    "COMPACTION_METHOD_NATIVE",
    "COMPACTION_METHOD_SHAKE",
    "COMPACTION_METHOD_SOFT",
    "OUTCOME_INSUFFICIENT",
    "OUTCOME_UNAVAILABLE",
    "SEED_BUDGET_DIVISOR",
    "SEED_META_KIND",
    "SEED_NOTHING",
    "SEED_ROLE",
    "SEED_UNSUPPORTED",
    "SEED_WRITTEN",
    "estimate_tokens",
    "method_sufficient",
    "project_pct_after",
    "rotation_ladder",
    "row_fingerprint",
    "shake_elide",
    "split_tail",
    "walk_tail",
]

# Outcomes that hand the turn to the next name in the ladder. ``unavailable``:
# the method cannot run for this session (no window figure, no seed writer on
# this surface). ``insufficient``: it could run but the projected usage stays
# inside the threshold band. A method that ran returns the coordinator's own
# result string instead, and the walk ends there.
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_INSUFFICIENT = "insufficient"

# What a seed writer answers the coordinator. ``written``: a seed row holds the
# digest, the rotation may proceed. ``nothing``: nothing is older than the tail
# the successor carries, so a tail-only rotation loses no history; the walk may
# reach ``soft``. ``unsupported``: this surface holds no transcript it can
# digest for the key (a channel-born session, a key with no tab, a tab that
# closed under the rotation); the walk hands to ``native``, which keeps the
# history as a summary. Only the literal ``nothing`` lets the walk reach
# ``soft``: any other answer is read as ``unavailable``.
SEED_WRITTEN = "written"
SEED_NOTHING = "nothing"
SEED_UNSUPPORTED = "unsupported"


def rotation_ladder(ceiling: str) -> tuple[str, ...]:
    """Rotation methods the coordinator tries for a ``session.compaction_method`` of *ceiling*.

    Strongest first, down to ``soft``; ``native`` is not in the result because
    the coordinator always runs it last, unconditionally. ``COMPACTION_METHODS``
    is ordered weakest first, so the ladder is its prefix up to the ceiling,
    reversed and without ``native``. An unknown ceiling yields no rotation
    step (the load-time normalizer maps it to the default anyway).
    """
    if ceiling not in COMPACTION_METHODS:
        return ()
    below = COMPACTION_METHODS[: COMPACTION_METHODS.index(ceiling) + 1]
    return tuple(m for m in reversed(below) if m != COMPACTION_METHOD_NATIVE)


#: Transcript role of the row a rotation method leaves behind for the successor
#: session: its content is the digest of the dropped slice, its ``meta`` the
#: record below. Not a conversation role, so every reader that keys on
#: ``context.RECALL_ROLES`` skips it by construction; the replay builder admits
#: it on purpose, and the dashboard's transcript registries claim it undrawn
#: (``COMPACTION_SEED_ROLE`` in ``website/src/pages/chat/groupDisplayItems.ts``):
#: a role no renderer claims falls to the chat page's bubble fallback, which
#: would print the digest. A collapsed marker for it is follow-up work.
SEED_ROLE = "compaction"
#: ``meta.kind`` of a seed row. The other ``meta`` fields: ``method`` (the
#: ladder step that wrote it), ``through_row`` (:func:`row_fingerprint` of the
#: newest row the digest covers: the exact row a replay stops at),
#: ``through_ts`` (that row's ``ts``, the bound rows OLDER than it are dropped
#: by) and ``dropped_rows`` (how many rows went into the digest).
SEED_META_KIND = "compaction_seed"
#: Share of the tail budget a shake digest may take, and the share of the replay
#: budget the successor grants it: one figure on both sides, so the digest a
#: rotation writes is the digest the replay renders whole.
SEED_BUDGET_DIVISOR = 2

#: Characters per token the estimates assume: the 4:1 ratio the replay budget
#: is sized with (80K chars for roughly 20K tokens at the reference window).
CHARS_PER_TOKEN = 4.0

#: A fenced block at or above this size is elided from a shake digest. 2000
#: characters is about 500 tokens, half a screen of code: below it the block is
#: usually the answer itself, above it a dump the digest only has to name.
DEFAULT_SHAKE_ELIDE_MIN_CHARS = 2_000

# Share of a digest budget spent on the OLDEST rows of the dropped slice. The
# first exchanges carry the goal statement, the newest ones the state the tail
# continues from; a digest that keeps only one end loses the other.
_DIGEST_HEAD_DIVISOR = 4

#: Share of a shake digest's budget the digest an EARLIER rotation left (its seed
#: row, admitted by the replay as the oldest kept row) may take when a second
#: shake folds it: carried whole when it fits, clipped with the truncation mark
#: when it does not. A seed row is never a conversation row of the fold, so it
#: can never be demoted into the omitted-rows marker: the compacted history an
#: earlier rotation kept is either present or visibly truncated.
_CARRIED_DIGEST_DIVISOR = 2

_CARRIED_HEADER = "[Digest carried from an earlier compaction, standing in for {count} rows]"

_TRUNCATED_SUFFIX = "…[truncated]"


def _clip_line(line: str, budget_chars: int) -> str:
    """*line* cut to *budget_chars* with the truncation mark; unchanged when it fits.

    The one rule for a line that alone overflows its budget, shared by the
    digest (its newest row) and the walk (the one row it admits unconditionally),
    so neither side can carry more than the budget a rotation is judged against.
    """
    if len(line) <= budget_chars:
        return line
    room = max(0, budget_chars - len(_TRUNCATED_SUFFIX))
    return line[:room] + _TRUNCATED_SUFFIX


# A fenced block runs to its closing fence, or to the end of the row when the
# reply was cut mid-block: an unterminated dump is still a dump.
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)


def estimate_tokens(chars: int) -> int:
    """Token estimate for *chars* characters at ``CHARS_PER_TOKEN``, rounded up so a partial token counts."""
    if chars <= 0:
        return 0
    return math.ceil(chars / CHARS_PER_TOKEN)


def replay_line(row: Mapping[str, Any]) -> str:
    """The line ``build_session_replay`` renders for *row*.

    A seed row left by an earlier rotation is spliced as its content alone: that
    content is already a run of role-labelled lines, so the replay does not label
    it as a speaker. (A second rotation's digest does not fold it through this
    line at all: :func:`shake_elide` carries it under its own share.)
    """
    role = str(row.get("role", ""))
    if role == SEED_ROLE:
        return str(row.get("content", ""))
    return f"{role.title()}: {row.get('content', '')}"


def row_fingerprint(row: Mapping[str, Any]) -> str:
    """Identity of one transcript row, for naming the exact row a digest covers through.

    A timestamp alone cannot name a row: two rows can share one (independently
    written streams merged into one replay, legacy stamps), and a coverage
    bound that stops at "any row with this stamp" drops the tail row that
    shares it without that row ever entering the digest. A delivery id alone
    cannot either: the replay stops at the FIRST row whose fingerprint matches,
    so if two rows ever carried one ``meta.mid`` the newer, undigested row would
    be taken for the boundary and everything older than it silently dropped.
    The fingerprint therefore digests the delivery id (``meta.mid`` /
    ``meta.sendId``, the fields the replay merge keys on) TOGETHER with role,
    stamp and content: only the same row reproduces it. A row whose content
    changed after the digest was written fails the match and falls back to the
    stamp bound, which carries it verbatim (overlap, never loss).
    """
    delivery = ""
    meta = row.get("meta")
    if isinstance(meta, Mapping):
        for field in ("mid", "sendId"):
            value = meta.get(field)
            if isinstance(value, str) and value:
                delivery = f"{field}:{value}"
                break
    raw = "\x00".join(
        (
            delivery,
            str(row.get("role", "")),
            str(row.get("ts", "")),
            str(row.get("content", "")),
        )
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class TailSplit:
    """A row list cut into what a rotation drops and what it carries verbatim."""

    dropped: tuple[Mapping[str, Any], ...]
    tail: tuple[Mapping[str, Any], ...]
    #: Characters the tail costs against a replay budget, separators included.
    tail_chars: int


#: Characters the separator between two replay lines costs.
LINE_SEPARATOR_CHARS = 2


@dataclass(frozen=True, slots=True)
class TailWalk:
    """What one budgeted newest-first walk kept."""

    #: Kept lines, newest first.
    lines: list[str]
    #: Characters the kept lines cost, separators included.
    chars: int
    #: Index in the walked rows of the OLDEST kept row; ``len(rows)`` when
    #: nothing was kept. Rows from here on are the tail, even when a reserved
    #: row inside it was skipped for spilling its share.
    cut: int


def walk_tail(
    rows: Sequence[Mapping[str, Any]],
    *,
    budget_chars: int,
    line_of: Callable[[Mapping[str, Any]], str] = replay_line,
    reserve_role: str | None = None,
    reserve_chars: int = 0,
) -> TailWalk:
    """The ONE budgeted newest-first walk over *rows*.

    ``build_session_replay`` renders through this walk and ``split_tail`` cuts
    through it, so the tail a rotation leaves verbatim is, by construction, the
    tail the successor's replay carries. A row is kept while the running total
    plus its line fits *budget_chars*; the newest row is kept whatever its size
    so a tiny budget still seeds one message, but clipped to *budget_chars* with
    the truncation mark, so the tail never exceeds the budget a rotation is
    judged against.

    Rows of *reserve_role* spend at most *reserve_chars* between them and are
    skipped, not stopped at, once that share is spent: the replay reserves a
    share for ``inject`` breadcrumbs so they cannot evict conversation, and the
    scan keeps looking for conversation rows behind an inject row that spills.
    *line_of* renders a row; the replay passes its own to clip inject content.
    """
    lines: list[str] = []
    total = 0
    reserved = 0
    cut = len(rows)
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        line = line_of(row)
        is_reserved = reserve_role is not None and row.get("role") == reserve_role
        if is_reserved and reserved + len(line) > reserve_chars and lines:
            continue
        if total + len(line) > budget_chars and lines:
            break
        if not lines and len(line) > budget_chars:
            line = _clip_line(line, budget_chars)
        lines.append(line)
        total += len(line) + LINE_SEPARATOR_CHARS
        cut = index
        if is_reserved:
            reserved += len(line) + LINE_SEPARATOR_CHARS
    return TailWalk(lines=lines, chars=total, cut=cut)


def split_tail(
    rows: Sequence[Mapping[str, Any]],
    *,
    budget_chars: int,
    line_of: Callable[[Mapping[str, Any]], str] = replay_line,
    reserve_role: str | None = None,
    reserve_chars: int = 0,
) -> TailSplit:
    """Cut chronological *rows* into the dropped slice and the verbatim tail.

    The cut is :func:`walk_tail`'s, under the same options the caller's replay
    walks with: the tail starts at the oldest row the walk kept. A boundary
    always falls between two rows; callers pass conversation rows only, so it
    never falls between a tool call and its result.
    """
    walk = walk_tail(
        rows,
        budget_chars=budget_chars,
        line_of=line_of,
        reserve_role=reserve_role,
        reserve_chars=reserve_chars,
    )
    return TailSplit(
        dropped=tuple(rows[: walk.cut]), tail=tuple(rows[walk.cut :]), tail_chars=walk.chars
    )


def project_pct_after(
    *, window_tokens: int, carried_tokens: int, overhead_tokens: int = 0
) -> float | None:
    """Usage the successor starts at, as a percentage of *window_tokens*.

    *carried_tokens* is what the rotation seeds (tail plus digest),
    *overhead_tokens* the fixed context every fresh session receives before any
    history. ``None`` when the window is unknown (a provider that reports 0), so
    no ladder step is judged on a figure that does not exist.
    """
    if window_tokens <= 0:
        return None
    used = max(0, carried_tokens) + max(0, overhead_tokens)
    return min(100.0, 100.0 * used / window_tokens)


def method_sufficient(
    pct_after: float | None, *, threshold_pct: float, min_effect_pct_points: float
) -> bool:
    """Whether a projected usage clears the threshold by the required margin.

    Mirrors the in-place verdict: a compaction landing within
    ``min_effect_pct_points`` under the threshold is judged ineffective there,
    so the same figure separates a step that advances the ladder from one that
    ends it. An unknown projection is never sufficient.
    """
    if pct_after is None:
        return False
    return pct_after <= threshold_pct - min_effect_pct_points


def elide_fences(content: str, *, min_chars: int) -> str:
    """Replace every fenced block of at least *min_chars* with a token-count placeholder.

    Blocks under the floor stay: a short snippet is usually the point of the
    message.
    """

    def _placeholder(match: re.Match[str]) -> str:
        block = match.group(0)
        if len(block) < min_chars:
            return block
        return f"[Output elided - {estimate_tokens(len(block))} tokens]"

    return _FENCE_RE.sub(_placeholder, content)


@dataclass(frozen=True, slots=True)
class ShakeDigest:
    """An elided rendering of the dropped slice and the rows it stands in for."""

    text: str
    #: Rows the digest stands in for; the seed's ``meta.dropped_rows``.
    rows_in: int
    #: ``ts`` of the newest input row, so a caller can record which rows the
    #: digest covers; ``None`` when the rows carry no timestamps.
    through_ts: str | None
    #: :func:`row_fingerprint` of the newest input row: the exact row the digest
    #: covers through, which a timestamp alone cannot name.
    through_row: str | None


def _omitted_marker(count: int) -> str:
    return f"[... {count} rows between these left out of this digest ...]"


def _select_within_budget(lines: list[str], budget_chars: int) -> tuple[list[str], int]:
    """Lines to render and how many were left out, both chronological ends kept.

    Everything fits: render everything. Otherwise the oldest rows take a fixed
    share of the budget, the newest rows the rest, and one marker between them
    names the count left out. When even the newest row alone overflows, its
    beginning is carried with a truncation mark rather than nothing.
    """
    cost = [len(line) + 2 for line in lines]
    if sum(cost) <= budget_chars:
        return list(lines), 0
    marker_reserve = len(_omitted_marker(len(lines))) + 2
    head: list[str] = []
    head_used = 0
    head_budget = budget_chars // _DIGEST_HEAD_DIVISOR
    for line, line_cost in zip(lines, cost):
        if head_used + line_cost > head_budget:
            break
        head.append(line)
        head_used += line_cost
    tail: list[str] = []
    tail_used = 0
    tail_budget = budget_chars - head_used - marker_reserve
    for idx in range(len(lines) - 1, len(head) - 1, -1):
        if tail_used + cost[idx] > tail_budget:
            break
        tail.append(lines[idx])
        tail_used += cost[idx]
    tail.reverse()
    if not head and not tail:
        return [_clip_line(lines[-1], budget_chars)], len(lines) - 1
    omitted = len(lines) - len(head) - len(tail)
    if omitted:
        return head + [_omitted_marker(omitted)] + tail, omitted
    return head + tail, 0


def _seed_rows_in(row: Mapping[str, Any]) -> int:
    """How many rows an earlier rotation's seed row stands in for (its ``meta.dropped_rows``)."""
    meta = row.get("meta")
    if isinstance(meta, Mapping):
        try:
            return max(0, int(meta.get("dropped_rows", 0)))
        except (TypeError, ValueError):
            return 0
    return 0


def _carried_lines(prior: Mapping[str, Any], budget_chars: int) -> list[str]:
    """The lines a second shake carries for an earlier seed row, within its share of *budget_chars*."""
    header = _CARRIED_HEADER.format(count=_seed_rows_in(prior))
    share = budget_chars // _CARRIED_DIGEST_DIVISOR
    body = str(prior.get("content", ""))
    room = share - len(header) - LINE_SEPARATOR_CHARS
    if room <= len(_TRUNCATED_SUFFIX):
        # A share too small for any of the body: the header alone still names
        # what stood here, clipped to the share when even that overflows.
        return [_clip_line(header, max(0, share))]
    return [header, _clip_line(body, room)]


def shake_elide(
    rows: Sequence[Mapping[str, Any]],
    *,
    budget_chars: int,
    elide_min_chars: int = DEFAULT_SHAKE_ELIDE_MIN_CHARS,
) -> ShakeDigest:
    """Digest of the dropped slice: the conversation with large fenced blocks named, not carried.

    Every row keeps its role label and prose; a fenced block at or above
    *elide_min_chars* becomes ``[Output elided - N tokens]``. When the elided
    rows still exceed *budget_chars*, the oldest rows get a quarter of the budget
    and the newest rows the rest, with one marker naming how many rows between
    them were left out. The digest ends within *budget_chars* whenever the
    budget holds at least one row; a budget smaller than that carries the
    newest row's beginning with a truncation mark.

    A seed row an earlier rotation left (``SEED_ROLE``) is not a conversation
    row of this fold. Its digest is carried first, whole when it fits in its
    ``_CARRIED_DIGEST_DIVISOR``-th of the budget and clipped with the truncation
    mark when it does not, and the rows it stood in for are added to
    ``rows_in``; the conversation is digested into the budget that remains. So a
    second shake can shorten the compacted history an earlier one kept, visibly,
    but can never leave it out.
    """
    prior: Mapping[str, Any] | None = None
    conversation: list[Mapping[str, Any]] = []
    through_ts: str | None = None
    through_row: str | None = None
    for row in rows:
        if str(row.get("role", "")) == SEED_ROLE:
            prior = row
        else:
            conversation.append(row)
        ts = row.get("ts")
        if isinstance(ts, str) and ts:
            through_ts = ts
        through_row = row_fingerprint(row)
    if prior is None and not conversation:
        return ShakeDigest("", 0, None, None)
    carried = _carried_lines(prior, budget_chars) if prior is not None else []
    carried_rows = _seed_rows_in(prior) if prior is not None else 0
    remaining = budget_chars - sum(len(line) + LINE_SEPARATOR_CHARS for line in carried)
    lines = [
        replay_line(
            {
                "role": row.get("role", ""),
                "content": elide_fences(str(row.get("content", "")), min_chars=elide_min_chars),
            }
        )
        for row in conversation
    ]
    chosen = _select_within_budget(lines, max(1, remaining))[0] if lines else []
    text = "\n\n".join(carried + chosen)
    return ShakeDigest(
        text=text,
        rows_in=carried_rows + len(conversation),
        through_ts=through_ts,
        through_row=through_row,
    )
