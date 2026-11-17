"""Whether a turn rebuilt the prompt cache instead of reading it.

A warm cache writes only the tokens that are new this turn, so the cache-write
count tracks how much the context grew. A cold cache (an expired idle window, a
changed prefix) writes the whole prefix again, so the write count exceeds the
growth by about the prefix. That excess is the signal: it needs no per-call
breakdown, and it ignores the cache-read count, which a tool loop inflates by
counting the same prefix once per model call.

A first write is not a rewrite, and a backend that reports no cache counts says
nothing either way, so both stay unflagged.
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


def note_turn_cache(slot: "_ChatSlot", source: object, ctx: int, cache_write: int) -> bool:
    """Judge a finished turn against the previous one and remember its context size.

    The size lives in memory on the slot, beside the backend session it was read
    from (*source* is the turn's provider). A size from another session is no
    baseline: a reset or replaced conversation starts its cache empty, so its first
    turn writes everything once and is never flagged. The same holds for the first
    turn after a restart, which has no reading at all. A turn with no context
    reading leaves the remembered size alone. A ``/compact`` turn shrinks the context
    on purpose, so it is never flagged, and its smaller size becomes the new base; the
    caller does the same for a turn that compacted automatically.
    """
    session = _session_of(source)
    prev = slot._prev_turn_ctx_used if slot._prev_turn_ctx_session == session else 0
    if ctx > 0:
        slot._prev_turn_ctx_used = ctx
        slot._prev_turn_ctx_session = session
    if slot._compact_command_turn:
        return False
    return cache_rewritten(prev, ctx, cache_write)
