"""Consolidation progress on a transcript's metadata line.

The content generation, the ``last_consolidated`` marker and its sub-offset, their
atomic snapshot, and the retry ledger that bounds billed attempts per span.

Each function here whose first parameter is ``self`` is a ``ConversationLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_meta_cache``.

Instance fields read: none.
"""

from __future__ import annotations

import json
import logging
import math
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.history_consolidation import (
    _CONSOLIDATION_META_KEYS,
    _CONSOLIDATION_SUBOFFSET_META_KEYS,
    AttemptedSpan,
)
from kiro_crew.history_files import transcript_lock_stems
from kiro_crew.history_rewrite import rewrite_metadata_line
from kiro_crew.history_schema import metadata_now_iso

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: Moved code logs under the facade's own logger name.
logger = logging.getLogger("kiro_crew.history")


def rotation_generation(self: ConversationLog, key: str) -> int:
    """Return the session's content-identity counter for *key*.

    Advanced by every write that changes the message content: a rotation
    (:meth:`_maybe_rotate`), a dashboard rewrite save (regenerate / rewind /
    fork), and a channel transcript merge. :meth:`mark_consolidated` refuses an
    offset snapshotted under an older generation, which catches an edit that
    leaves the count untouched. It is also the span identity of the retry
    accounting (:meth:`_attempts_describe_current_span`), so a bump releases a
    budget charged against superseded content. An absent field reads as ``0``.
    """
    return int(self._read_metadata(key).get("rotation_generation", 0) or 0)


def snapshot_for_consolidation(
    self: ConversationLog, key: str, *, withhold_restricted: bool = False
) -> tuple[list[dict], int, int, int]:
    """Atomically snapshot ``(unconsolidated_messages, total, generation, sub_offset)``.

    With ``withhold_restricted=True`` the metadata line's ``memory_mode`` is
    read under the SAME lock hold and validated with the rows: an incognito or
    temporary line -- or one that cannot be read -- raises
    :class:`TranscriptWithheld` instead of returning rows (the derivation
    seam, see :meth:`derive_messages`). A writer can tighten the line after the
    consolidator's own privacy check (a same-key hand-over landing a restricted
    tab's rows), so rows and contract are taken in one hold.

    All four values are read under one :meth:`_locked` hold. Read as separate
    calls, an append can rotate between them and pair pre-rotation rows and
    offset with the post-rotation generation; ``mark_consolidated`` would then
    apply the shifted offset and mark unread messages consolidated.

    ``sub_offset`` is the durable offset into the FIRST unconsolidated message
    (see :data:`~kiro_crew.history_consolidation._CONSOLIDATION_SUBOFFSET_META_KEYS`),
    returned only when its stored generation matches; otherwise ``0``, so the
    head is re-sliced from its start. Returns
    ``(messages[offset:], len(messages), generation, sub_offset)``; the list is
    a fresh slice, never the shared ``_read_messages`` cache object.
    """
    hold = (
        self.derivation_hold(transcript_lock_stems(key))
        if withhold_restricted
        else self._locked(key)
    )
    with hold:
        if withhold_restricted:
            self._withhold_if_restricted(key)
        messages = self._read_messages(key)
        meta = self._read_metadata(key)
        generation = int(meta.get("rotation_generation", 0) or 0)
        offset, sub_offset = _live_position(meta, generation)
        return list(messages[offset:]), len(messages), generation, sub_offset


def _live_position(meta: dict, generation: int) -> tuple[int, int]:
    """``(row marker, sub-offset)`` as the consolidator reads them, for *generation*.

    The one reading of the pair: the snapshot slices by it, and the retry-span
    check compares a charged attempt against it, so the two cannot disagree. A
    sub-offset is a character position in the row at the stored marker, so a
    marker that had to be repaired (unreadable or negative) pairs with 0.
    """
    offset, repaired = _read_consolidated_offset(meta)
    if repaired:
        return 0, 0
    return offset, _sub_offset_for_generation(meta, generation)


def _sub_offset_for_generation(meta: dict, generation: int) -> int:
    """The durable sub-message offset, honoured only for *generation*.

    Returns the stored ``consolidation_sub_offset`` when its stamped
    ``consolidation_sub_offset_generation`` equals *generation*, else ``0``, so
    after a rotation or rewrite the head is re-sliced from its start. Any
    unreadable or negative value also reads as ``0``.
    """
    try:
        stamped = int(meta.get("consolidation_sub_offset_generation", -1))
    except (TypeError, ValueError, OverflowError):
        return 0
    if stamped != generation:
        return 0
    try:
        value = int(meta.get("consolidation_sub_offset", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return value if value > 0 else 0


def mark_consolidated(
    self: ConversationLog, key: str, offset: int, generation: int | None = None
) -> None:
    """Rewrite metadata line with updated ``last_consolidated`` offset.

    *offset* is an absolute message index and *generation* the
    :meth:`rotation_generation`, both captured BEFORE the slow LLM call. A
    rotation shifts every surviving index; a dashboard edit (regenerate /
    rewind / fork) can keep count and marker unchanged but replace the tail.
    Applying the offset after either would mark unread content consolidated.

    So ``last_consolidated`` is reset to 0 when the generation changed, or when
    *offset* exceeds the current message count (fallback for callers without a
    generation). Redoing the retained tail is harmless; clamping to EOF would
    drop new messages from extraction. Otherwise the offset is applied as-is.
    """

    # rewrite_metadata_line runs this on an unlocked read, then renames under
    # the cross-process lock only if the file (and the path for this key) is
    # still exactly what it read; otherwise it runs it again under the lock, so
    # a concurrent append or restore cannot be lost or redirect the write.
    def build(path: Path, text: str | None) -> bytes | None:
        if text is None:
            return None
        lines = text.splitlines(keepends=True)
        if not lines:
            return None
        meta = json.loads(lines[0])
        # Reconcile the caller's absolute *offset* against the messages
        # actually present now. Line 0 is the metadata line; every other
        # non-blank line is a message.
        msg_count = sum(1 for ln in lines[1:] if ln.strip())
        current_generation = int(meta.get("rotation_generation", 0) or 0)
        if generation is not None and current_generation != generation:
            # Content changed since the snapshot (rotation or rewrite). The
            # count check below misses both when ``offset <= msg_count``.
            logger.warning(
                "mark_consolidated: rotation generation changed %s->%d for "
                "%s (rotation or transcript edit during consolidation); "
                "resetting last_consolidated to 0 to avoid marking content "
                "no consolidation turn read",
                generation,
                current_generation,
                key,
            )
            safe_offset = 0
        elif offset > msg_count:
            # A rotation truncated the file during the await. Clamping to
            # ``msg_count`` would mark new, unread messages consolidated.
            logger.warning(
                "mark_consolidated: offset %d exceeds current message count "
                "%d for %s (rotation during consolidation); resetting "
                "last_consolidated to 0 to avoid skipping post-rotation "
                "messages",
                offset,
                msg_count,
                key,
            )
            safe_offset = 0
        else:
            safe_offset = offset
        meta["last_consolidated"] = safe_offset
        meta["updated_at"] = metadata_now_iso()
        # A marked span releases its retry accounting in the same locked write,
        # so the next span is not charged for its failures. Only when the
        # offset was APPLIED: the abandon-at-cap path also calls this, and
        # clearing on a reset would hand a capped span a fresh budget.
        if safe_offset == offset:
            for _acct_key in _CONSOLIDATION_META_KEYS:
                meta.pop(_acct_key, None)
        # The marker moved past the head or was reset, so no partly extracted
        # head remains; a stale sub-offset would seek into the next message.
        for _sub_key in _CONSOLIDATION_SUBOFFSET_META_KEYS:
            meta.pop(_sub_key, None)
        lines[0] = json.dumps(meta) + "\n"
        # The helper fsyncs the whole file (an unsynced rename can come back
        # empty after power loss), keeps the mtime so housekeeping does not
        # reorder list_sessions, and invalidates the cache under the lock.
        # Bytes, so Windows does not turn LF into CRLF.
        return "".join(lines).encode("utf-8")

    rewrite_metadata_line(self, key, build)


def advance_consolidation_sub_offset(
    self: ConversationLog, key: str, offset: int, sub_offset: int, generation: int
) -> None:
    """Record extraction PART-WAY through the head message at *offset*.

    Called after a non-final slice of an over-budget head message succeeds
    (see :func:`~kiro_crew.history_consolidation._slice_head_for_budget`). The
    ``last_consolidated`` marker stays put until the last slice succeeds
    (:meth:`mark_consolidated`); the next pass resumes at *sub_offset*, an
    absolute character offset stamped with *generation*.

    Nothing is written unless, on the exact bytes the locked rename replaces
    (:func:`~kiro_crew.history_rewrite.rewrite_metadata_line`), the session
    still exists (an upsert would resurrect a deleted one), the generation
    still equals *generation*, and ``last_consolidated`` still equals *offset*.
    """

    def build(path: Path, text: str | None) -> bytes | None:
        if text is None:
            return None
        lines = text.splitlines(keepends=True)
        if not lines:
            return None
        meta = json.loads(lines[0])
        current_generation = int(meta.get("rotation_generation", 0) or 0)
        if current_generation != generation:
            logger.info(
                "advance_consolidation_sub_offset: generation changed %s->%d "
                "for %s; discarding sub-offset so the head re-slices from its "
                "start under the new content",
                generation,
                current_generation,
                key,
            )
            return None
        current_offset, repaired = _read_consolidated_offset(meta)
        if current_offset != offset:
            logger.info(
                "advance_consolidation_sub_offset: marker moved %d->%d for "
                "%s; the sliced head is no longer at the marker, dropping "
                "sub-offset",
                offset,
                current_offset,
                key,
            )
            return None
        if repaired:
            # Saved with the sub-offset it pairs with: a snapshot reads a marker
            # it had to repair as 0 with no sub-offset, so leaving the stored
            # value in place would restart this head's first slice every pass.
            meta["last_consolidated"] = 0
        meta["consolidation_sub_offset"] = max(0, int(sub_offset))
        meta["consolidation_sub_offset_generation"] = generation
        # The next slice is new content with a fresh budget: clear the retry
        # accounting in the same locked write, as mark_consolidated does.
        for _acct_key in _CONSOLIDATION_META_KEYS:
            meta.pop(_acct_key, None)
        meta["updated_at"] = metadata_now_iso()
        lines[0] = json.dumps(meta) + "\n"
        # Same synced, mtime-preserving bytes write as mark_consolidated.
        return "".join(lines).encode("utf-8")

    rewrite_metadata_line(self, key, build)


def unconsolidated_count(self: ConversationLog, key: str) -> int:
    """Count messages not yet processed by the consolidator.

    Counts without materializing the transcript (see :meth:`_count_messages`):
    the startup idle-seed scan calls this for every recent transcript.
    """
    total = self._count_messages(key)
    return max(0, total - self.consolidated_offset(key))


def consolidation_counts(self: ConversationLog, key: str) -> tuple[int, int]:
    """Return ``(total_messages, unconsolidated)`` from a SINGLE read.

    The consolidator's entry points need both: the unconsolidated count to
    decide there is anything to do, and the total as the extent
    :meth:`consolidation_retry_state` compares the charged span against. Both
    come from one ``_read_messages`` call so the eligibility check costs no
    additional transcript read on the event loop.

    This reads through the message cache on purpose, unlike
    :meth:`unconsolidated_count`: the idle sweep calls it on the event loop
    every tick for each tracked session, so a warm entry keeps that a cache
    hit instead of a full stream of the file per tick.
    """
    total = len(self._read_messages(key))
    return total, max(0, total - self.consolidated_offset(key))


def consolidated_offset(self: ConversationLog, key: str) -> int:
    """Rows already consolidated, from the metadata line; 0 when unreadable."""
    return _consolidated_offset_of(self._read_metadata(key))


def _consolidated_offset_of(meta: dict) -> int:
    """``last_consolidated`` as a row count: 0 when unreadable or negative."""
    return _read_consolidated_offset(meta)[0]


def _read_consolidated_offset(meta: dict) -> tuple[int, bool]:
    """``(row count, repaired)`` for ``last_consolidated``.

    The value is caller-supplied JSON. A negative one used as a slice start would
    count from the END of the transcript and skip the rows before it, and a
    non-integer one would raise in the slice or the subtraction, so both read as
    0 with ``repaired`` true.
    """
    try:
        value = int(meta.get("last_consolidated", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0, True
    if value < 0:
        return 0, True
    return value, False


def consolidation_retry_state(
    self: ConversationLog, key: str, message_count: int | None = None
) -> tuple[int, float]:
    """Return ``(failed_attempts, next_eligible_at)`` for *key*.

    Both live on the metadata line, so they survive a restart and are cleared
    by :meth:`mark_consolidated`. ``(0, 0.0)`` means no failure on record.

    Read UNCACHED: other processes (gateway sweep, CLI, subagents) record
    failures for the same session, and can update the count between a warm read
    and the next increment.

    Metadata is caller-supplied JSON: ``1e309`` parses to ``inf`` and
    ``int(inf)`` raises ``OverflowError``, and a ``NaN`` deadline makes
    ``now >= retry_at`` always false. Non-finite or unconvertible values reset
    to the eligible zero state.
    """
    self._meta_cache.pop(key, None)
    meta = self._read_metadata(key)
    try:
        attempts = int(meta.get("consolidation_attempts", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        attempts = 0
    try:
        retry_at = float(meta.get("consolidation_retry_at", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        retry_at = 0.0
    if not math.isfinite(retry_at):
        retry_at = 0.0
    if attempts and not self._attempts_describe_current_span(meta, message_count):
        # The counter belongs to a span that is gone; carrying a capped count
        # onto new content would silence the session forever. The deadline is
        # kept, so a failing session still waits out its backoff (4 h at cap).
        attempts = 0
    return max(0, attempts), retry_at


def _attempts_describe_current_span(
    self: ConversationLog, meta: dict, message_count: int | None
) -> bool:
    """True when the recorded attempts belong to the span in front of us now.

    A span is the ``(rotation_generation, last_consolidated)`` pair it was
    charged against, the message count at the time, and how far the prompt
    reached. None of these move while a span keeps failing, so the cap holds.
    A rotation or dashboard rewrite advances the generation (a regenerate can
    keep count, marker and extent identical), a stale-offset rewrite resets the
    marker, and new messages push past the attempted extent.

    The extent test matters because the cap is reachable from
    :meth:`HistoryConsolidator.retry_eligible` only when the abandon-marker
    write failed; without it the session would be refused forever.

    *message_count* comes from the caller and is never read from disk here:
    this runs on the gateway event loop. ``None`` skips the extent test and
    keeps the cap.

    Growth releases the cap only when the attempt reached the end of its
    transcript (``consolidation_attempts_prompted`` equal to
    ``consolidation_attempts_count``). A bounded attempt
    (:func:`~kiro_crew.history_consolidation._consolidation_chunk` cut it at
    the budget) tried a prefix that appends cannot change; releasing it would
    re-bill an over-budget head on every idle window. Growth uses ``>``, not
    ``!=``: a shrink is a rotation or compaction, which already moves the
    generation or marker.

    Unstamped accounting is treated as the current span, keeping the cap. The
    stamp is the ATTEMPTED span captured before the turn
    (:class:`AttemptedSpan`); re-read after the turn it would always match.
    """
    # The live marker is read as the snapshot reads it (``_live_position``), so an
    # attempt charged against a repaired marker matches the 0 it was sliced from.
    try:
        live_generation = int(meta.get("rotation_generation", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        live_generation = None
    live_offset, live_sub_offset = (
        _live_position(meta, live_generation) if live_generation is not None else (None, None)
    )
    for span_key, live in (
        ("consolidation_attempts_generation", live_generation),
        ("consolidation_attempts_offset", live_offset),
    ):
        if span_key not in meta or live is None:
            continue
        try:
            if int(meta.get(span_key, 0) or 0) != live:
                return False
        except (TypeError, ValueError, OverflowError):
            # Unreadable stamp — fall back to keeping the cap.
            continue
    # A moved sub-offset means the attempt covered a different slice: fresh
    # budget. Compare against the generation-honoured value, not the raw one:
    # a rotation leaves the raw ``consolidation_sub_offset`` nonzero, which
    # would reset the cap on every failure so it never abandons.
    if "consolidation_attempts_sub_offset" in meta and live_sub_offset is not None:
        try:
            if int(meta.get("consolidation_attempts_sub_offset", 0) or 0) != live_sub_offset:
                return False
        except (TypeError, ValueError, OverflowError):
            pass
    # A slice tried a prefix of the head that appends cannot change, so growth
    # must not release it, even when the head is the only unconsolidated
    # message and ``prompted == count`` holds.
    try:
        sub_bounded = int(meta.get("consolidation_attempts_sub_bounded", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        sub_bounded = 0
    if sub_bounded:
        return True
    if message_count is not None and "consolidation_attempts_count" in meta:
        try:
            attempted = int(meta.get("consolidation_attempts_count", 0) or 0)
            prompted = int(meta.get("consolidation_attempts_prompted", attempted) or attempted)
        except (TypeError, ValueError, OverflowError):
            return True
        if prompted >= attempted and message_count > attempted:
            return False
    return True


def _current_span_fields(self: ConversationLog, span: AttemptedSpan) -> dict:
    """The span-identity stamp to store beside a freshly charged attempt.

    Every field comes from *span*, the pre-turn snapshot (:class:`AttemptedSpan`);
    nothing is re-read from the metadata line, which a rotation or rewrite
    during the turn may already have changed. Negative values are clamped.

    ``consolidation_attempts_sub_offset`` is where in an over-budget head the
    failed slice began. ``consolidation_attempts_sub_bounded`` is 1 for a slice
    of the head, so :meth:`_attempts_describe_current_span` keeps its cap
    through message growth.
    """
    sub_offset = max(0, span.sub_offset)
    # A slice is in play when the prompt advanced past its start; a span that
    # never sub-sliced carries both as 0.
    sub_bounded = 1 if span.sub_prompted > sub_offset else 0
    return {
        "consolidation_attempts_generation": span.generation,
        "consolidation_attempts_offset": max(0, span.offset),
        "consolidation_attempts_count": max(0, span.total),
        "consolidation_attempts_prompted": max(0, span.prompted),
        "consolidation_attempts_sub_offset": sub_offset,
        "consolidation_attempts_sub_bounded": sub_bounded,
    }


def record_consolidation_failure(
    self: ConversationLog,
    key: str,
    base_secs: float,
    max_secs: float,
    span: AttemptedSpan,
    now: float | None = None,
) -> tuple[int, float]:
    """Durably count one failed consolidation attempt for *key*.

    *span* is what the failing turn attempted, from the pre-turn snapshot
    (:class:`AttemptedSpan`). It is both the stamp written beside the counter
    and the span the existing counter is compared against, so a different span
    starts a fresh budget. It is never re-read from the file.

    Returns the new ``(attempts, next_eligible_at)``; the wait doubles per
    attempt from *base_secs*, capped at *max_secs*.

    The read-increment-write runs under one :meth:`_locked` hold, uncached, so
    two processes (gateway sweep and CLI) cannot both write the same count.

    Returns ``(0, 0.0)`` without writing when the transcript is gone:
    ``_update_metadata_locked`` upserts and would resurrect it. Blocking file
    IO; call it off the event loop.
    """
    stamp = _time.time() if now is None else now
    with self._locked(key):
        if not self._path(key).exists():
            # Deleted mid-consolidation; an upsert would resurrect it.
            logger.info(
                "Skipping consolidation retry accounting for %s: session deleted",
                key,
            )
            return 0, 0.0
        attempts = self.consolidation_retry_state(key, span.total)[0] + 1
        # The exponent comes from caller-supplied metadata, so clamp it before
        # shifting: an absurd stored count would otherwise make ``2 ** n``
        # allocate a huge int (or raise) instead of returning a wait. The
        # backoff saturates at *max_secs* far below the clamp, so no reachable
        # attempt count is affected.
        retry_at = stamp + min(max_secs, base_secs * (2 ** min(attempts - 1, 64)))
        self._update_metadata_locked(
            key,
            {
                "consolidation_attempts": attempts,
                "consolidation_retry_at": retry_at,
                # The span stamp, in the same locked update as the counter
                # (see _attempts_describe_current_span).
                **self._current_span_fields(span),
            },
        )
    return attempts, retry_at


def record_consolidation_environment_failure(
    self: ConversationLog,
    key: str,
    base_secs: float,
    max_secs: float,
    now: float | None = None,
) -> tuple[int, float]:
    """Arm the backoff for a consolidation that never reached the provider.

    Counted separately from :meth:`record_consolidation_failure` because the
    two failures have different costs. A spent turn costs money, so its
    counter feeds a hard abandon cap. A pre-dispatch failure — no session
    manager, or kiro-cli failing to start — costs nothing, so it must never
    abandon a span: doing so would write the durable marker over messages no
    LLM has ever read. It still needs a deadline, or a permanently broken host
    re-attempts on every 60s heartbeat tick forever, so the count drives the
    same widening wait up to *max_secs* and then holds there.

    Returns the new ``(environment_failures, next_eligible_at)``. Same single
    locked read-increment-write and same deleted-session guard as the billed
    path. Blocking file IO — call it off the event loop.
    """
    stamp = _time.time() if now is None else now
    with self._locked(key):
        if not self._path(key).exists():
            return 0, 0.0
        meta = self._read_metadata(key)
        try:
            failures = int(meta.get("consolidation_env_failures", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            failures = 0
        failures = max(0, failures) + 1
        retry_at = stamp + min(max_secs, base_secs * (2 ** min(failures - 1, 64)))
        self._update_metadata_locked(
            key,
            {
                "consolidation_env_failures": failures,
                "consolidation_retry_at": retry_at,
            },
        )
    return failures, retry_at
