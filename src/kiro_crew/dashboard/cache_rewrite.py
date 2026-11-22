"""Whether a turn rebuilt the prompt cache instead of reading it.

A warm cache writes only the tokens that are new this turn, so the cache-write
count tracks how much the context grew. A cold cache (an expired idle window, a
changed prefix) writes the whole prefix again, so the write count exceeds the
growth by about the prefix. That excess is the signal: it needs no per-call
breakdown, and it ignores the cache-read count, which a tool loop inflates by
counting the same prefix once per model call.

A first write is not a rewrite, and a backend that reports no cache counts says
nothing either way, so both stay unflagged. With ``dashboard.turn_cache_estimate``
on, such a backend is judged by the idle gap instead (``CACHE_LIKELY``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot

# Below this the prefix is too small to tell a rewrite from the reply's own
# tokens, which land in the next turn's write count.
_MIN_PREFIX_TOKENS = 20_000
# Share of the prefix that must be written again for the turn to count.
_REWRITE_SHARE = 0.5

CACHE_REWRITE = "rewrite"  # read from the cache-write count
CACHE_LIKELY = "likely"  # inferred from the idle gap, no counts reported


def cache_rewritten(prev_ctx: int, ctx: int, cache_write: int) -> bool:
    """True when this turn wrote back most of a prefix a warm cache would have served.

    ``prev_ctx`` is the previous turn's context size (0 when there was none),
    ``ctx`` this turn's. Both count input, output and cache tokens of the last
    model call, so they are comparable across turns. A backend that reports no
    cache counts writes 0, which never reaches the threshold, so it is never
    flagged.
    """
    if prev_ctx <= 0 or ctx <= 0:
        return False
    prefix = min(ctx, prev_ctx)
    if prefix < _MIN_PREFIX_TOKENS:
        return False
    growth = max(0, ctx - prev_ctx)
    return cache_write - growth >= prefix * _REWRITE_SHARE


def cache_likely_rewritten(
    prev_ctx: int, ctx: int, gap_secs: float, lifetime_secs: float | None
) -> bool:
    """True when a turn started past the cache lifetime on a prefix worth caching.

    ``gap_secs`` runs from the end of the previous reply to the start of this turn.
    A context that shrank was compacted while idle, so the cache's age is unknown.
    """
    if lifetime_secs is None or lifetime_secs <= 0 or prev_ctx <= 0 or ctx < prev_ctx:
        return False
    return prev_ctx >= _MIN_PREFIX_TOKENS and gap_secs >= lifetime_secs


def _session_of(source: object) -> str:
    """The backend session *source* is serving, or ``""`` when it names none.

    A provider names its native conversation by ``session_id``; a reset or a
    replaced conversation gets a new one. Never raises: this runs on the turn's
    completion path, which a measurement must not break.
    """
    try:
        session = getattr(source, "session_id", "")
    except Exception:
        return ""
    return session if isinstance(session, str) else ""


def judge_turn_cache(
    slot: "_ChatSlot",
    source: object,
    ctx: int,
    cache_write: int,
    *,
    cache_read: int = 0,
    started_at: float | None = None,
    ended_at: float | None = None,
    lifetime_secs: float | None = None,
) -> str:
    """Judge a finished turn against the previous one and remember its context size.

    Returns ``CACHE_REWRITE`` when the cache-write count shows a rebuilt prefix,
    ``CACHE_LIKELY`` when no cache counts were reported and the turn started at least
    *lifetime_secs* after the previous reply ended (one monotonic clock), else ``""``.

    The size lives in memory on the slot, beside the backend session it was read
    from (*source* is the turn's provider). A size from another session is no
    baseline: a reset or replaced conversation starts its cache empty, so its first
    turn writes everything once and is never flagged. The same holds for the first
    turn after a restart, which has no reading at all. A turn with no context
    reading leaves the remembered size alone. A ``/compact`` turn shrinks the context
    on purpose, so it is never flagged, and its smaller size becomes the new base; the
    caller does the same for a turn that compacted automatically. *ended_at* is kept
    beside the size, or forgotten when not given.
    """
    session = _session_of(source)
    prev = slot._prev_turn_ctx_used if slot._prev_turn_ctx_session == session else 0
    prev_ended = slot._prev_turn_ended_at if slot._prev_turn_ctx_session == session else 0.0
    if ctx > 0:
        slot._prev_turn_ctx_used = ctx
        slot._prev_turn_ctx_session = session
        slot._prev_turn_ended_at = 0.0 if ended_at is None else ended_at
    if slot._compact_command_turn:
        return ""
    if cache_rewritten(prev, ctx, cache_write):
        return CACHE_REWRITE
    if cache_write > 0 or cache_read > 0 or started_at is None or prev_ended <= 0:
        return ""
    if cache_likely_rewritten(prev, ctx, started_at - prev_ended, lifetime_secs):
        return CACHE_LIKELY
    return ""


def note_turn_cache(slot: "_ChatSlot", source: object, ctx: int, cache_write: int) -> bool:
    """Whether the cache-write count alone shows this turn rebuilt the prefix."""
    return judge_turn_cache(slot, source, ctx, cache_write) == CACHE_REWRITE
