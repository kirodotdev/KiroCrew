"""Consolidation progress on a transcript's metadata line.

The content generation, the ``last_consolidated`` marker and its sub-offset, their
atomic snapshot, and the retry ledger that bounds billed attempts per span.

Each function here whose first parameter is ``self`` is a ``ConversationLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_meta_cache``.

Instance fields read: ``_read_projection``.
"""

from __future__ import annotations

import json
import logging
import math
import time as _time
from typing import TYPE_CHECKING

from kiro_crew.atomic_write import atomic_write
from kiro_crew.history_consolidation import (
    _CONSOLIDATION_META_KEYS,
    _CONSOLIDATION_SUBOFFSET_META_KEYS,
    AttemptedSpan,
)
from kiro_crew.history_files import _restore_mtime, _safe_mtime, transcript_lock_stems
from kiro_crew.history_schema import metadata_now_iso

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: Moved code logs under the facade's own logger name.
logger = logging.getLogger("kiro_crew.history")


def rotation_generation(self: ConversationLog, key: str) -> int:
    """Return the session's content-identity counter for *key*.

    Advanced by every write that makes the transcript's messages a DIFFERENT
    body of content than a consolidation may have snapshotted: a rotation
    (:meth:`_maybe_rotate`), a dashboard rewrite save (regenerate / rewind /
    fork), and a channel transcript merge. A consolidator snapshots it
    alongside the message offset before its (slow) LLM call and passes it back
    to :meth:`mark_consolidated`, which refuses to apply the offset whenever
    the generation changed — closing the change-during-await race for ANY
    retained count, including an edit that leaves the count untouched. It is
    also the span identity the retry accounting is stamped against
    (:meth:`_attempts_describe_current_span`), so the same bump releases a
    budget charged against the superseded content.

    Named for the rotation that first needed it; the field is now the general
    content-identity counter. Absent field (legacy metadata / never advanced)
    reads as ``0``.
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
    seam, see :meth:`derive_messages`). The
    consolidator's own privacy check reads the line before this snapshot, and
    a writer can tighten it in between (a same-key hand-over landing a
    restricted tab's rows under a line that was persistent a moment ago);
    rows and contract taken in one hold cannot disagree, so nothing the
    consolidator sends to a model or writes to memory is ever rows a
    restricted line already governed.

    The consolidator needs the unconsolidated tail, the total message count
    (the absolute offset it later passes to :meth:`mark_consolidated`), and
    the rotation generation counter to reflect the SAME point in time. Read
    as three separate calls (``get_unconsolidated`` + ``rotation_generation``)
    an append can trigger a rotation *between* them, pairing pre-rotation
    messages/offset with the post-rotation generation. ``mark_consolidated``
    then sees matching generations and applies the stale (shifted) offset —
    and when the retained count is >= that offset the count fallback misses
    it too — silently marking never-processed messages as consolidated and
    dropping them from memory/history extraction.

    ``sub_offset`` is the durable sub-message extraction offset into the
    FIRST unconsolidated message (see
    :data:`~kiro_crew.history_consolidation._CONSOLIDATION_SUBOFFSET_META_KEYS`),
    returned ONLY when its stored generation matches the generation read in
    the same hold; a stale or absent sub-offset reads as ``0``, so the head
    message is re-sliced from its start under a new generation rather than
    resumed at an offset that describes different content. Taken in
    the same lock as the rows and generation so it cannot pair with a
    different content identity.

    Holding :meth:`_locked` across all reads makes the snapshot atomic: no
    append/rotation can interleave, so the returned offset, generation and
    sub-offset are guaranteed consistent. Returns
    ``(messages[offset:], len(messages), generation, sub_offset)``. The
    returned list is a fresh slice (never the shared ``_read_messages`` cache
    object), so the caller may treat it as owned.
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
        offset = meta.get("last_consolidated", 0)
        generation = int(meta.get("rotation_generation", 0) or 0)
        sub_offset = self._sub_offset_for_generation(meta, generation)
        return list(messages[offset:]), len(messages), generation, sub_offset


def _sub_offset_for_generation(meta: dict, generation: int) -> int:
    """The durable sub-message offset, honoured only for *generation*.

    Returns the stored ``consolidation_sub_offset`` when its stamped
    ``consolidation_sub_offset_generation`` equals *generation*, else ``0``.
    A rotation or rewrite bumps the generation, which is exactly what the
    issue requires to discard a sub-offset: the head is then re-sliced from
    its start rather than resumed mid-way under new content. Any unreadable
    or negative stamp reads as ``0`` — the conservative direction, re-slicing
    from the start rather than resuming at an unverifiable position.
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

    *offset* is an absolute message index captured by the caller BEFORE a
    (potentially slow) LLM consolidation call. *generation* is the rotation
    generation counter (:meth:`rotation_generation`) captured at the same
    moment. It advances on anything that changes the content under a
    consolidation in flight, and each case makes the caller's *offset*
    meaningless in a different way:

    * A **rotation** truncated the file to its newest messages and reset
      ``last_consolidated`` to 0, so every surviving index shifted by the
      number of dropped lines and applying the offset would mark
      never-consolidated retained messages as processed.
    * A **transcript edit** (the dashboard regenerate / rewind / fork save)
      replaced the live window's tail with content this turn never read. The
      message count, the marker and the extent can all be unchanged, so the
      offset still *looks* applicable — and applying it would mark the
      REPLACEMENT tail consolidated without ever extracting it.

    Both are silent memory loss, and the generation is what distinguishes
    them from a turn whose span is still intact.

    Detection uses two independent signals:

    1. **Generation change** (primary, when *generation* is supplied):
       anything that changes the content between snapshot and write bumps the
       counter, so a mismatch resets ``last_consolidated`` to 0. This closes
       both cases a pure offset-vs-count heuristic misses — a rotation that
       keeps >= *offset* messages, and an edit that keeps the count identical,
       each leave ``offset <= msg_count`` true.
    2. **Offset exceeds current count** (fallback, always): the file shrank
       below the captured offset (rotation truncated it). Retained if
       *generation* is unavailable (legacy callers) or as defense-in-depth.

    In either case ``last_consolidated`` is reset to 0 and the retained tail
    is reconsolidated rather than clamping the offset to EOF (which would
    silently mark post-rotation messages as already consolidated and drop
    them from memory/history extraction). When neither trips, the offset is
    applied as-is.
    """
    # Serialize behind the cross-process lock and resolve/re-read under it so
    # a concurrent append or restore cannot redirect this key after its path
    # was chosen.
    with self._locked(key):
        path = self._path(key)
        if not path.exists():
            return
        prev_mtime = _safe_mtime(path)
        before = path.stat()
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not lines:
            return
        old_head = lines[0]
        meta = json.loads(lines[0])
        # Reconcile the caller's absolute *offset* against the messages
        # actually present now. Line 0 is the metadata line; every other
        # non-blank line is a message.
        msg_count = sum(1 for ln in lines[1:] if ln.strip())
        current_generation = int(meta.get("rotation_generation", 0) or 0)
        if generation is not None and current_generation != generation:
            # PRIMARY signal: the content under this consolidation changed
            # between the caller's snapshot and now (the generation counter
            # advanced) — a rotation, or a dashboard rewrite that swapped the
            # live window's tail. Either way the offset cannot be applied.
            # After a rotation it is in the stale PRE-rotation numbering
            # (every surviving index shifted by the number of dropped lines);
            # after an edit the numbering still fits but the messages it would
            # mark are the REPLACEMENT tail, which no turn has read. Neither
            # is caught by the count heuristic below: a rotation that kept
            # >= *offset* messages and an edit that kept the count identical
            # both leave ``offset <= msg_count`` true, and marking either
            # would drop never-consolidated content from memory/history
            # extraction. Reset to 0 and reconsolidate the current tail
            # (harmless, idempotent) rather than risk that loss.
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
            # The file shrank below the captured offset — a rotation fired
            # during the (slow) LLM await, truncating to the newest messages
            # and resetting ``last_consolidated`` to 0. The caller's offset
            # is in the PRE-rotation numbering and is now meaningless.
            # Clamping it to ``msg_count`` would mark the retained tail —
            # which now includes brand-new, never-consolidated messages that
            # arrived after the snapshot — as consolidated, permanently
            # skipping them (silent history/memory loss). Reset to 0 and let
            # the retained tail be reconsolidated instead: redoing a handful
            # of already-processed messages is harmless and idempotent,
            # whereas dropping new ones is a data-integrity failure.
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
        # The marker is the success signal for the retry accounting written
        # by record_consolidation_failure: once a span is marked, its failed
        # attempts and backoff deadline describe a finished span, and leaving
        # them behind would charge the NEXT span for this
        # one's failures. Dropped in the same locked write so no window
        # exists where the marker is applied but the budget is not released.
        #
        # Only when the offset was actually APPLIED, though. Both branches
        # above reset to 0 without advancing anything, so the span is still
        # unconsolidated — and the abandon-at-cap path calls this method
        # precisely to stop spending on it. Clearing the accounting there
        # would hand a capped span a fresh budget every time a rotation
        # raced the marker write, so the cap would never actually hold.
        if safe_offset == offset:
            for _acct_key in _CONSOLIDATION_META_KEYS:
                meta.pop(_acct_key, None)
        # The sub-message offset only ever describes extraction PART-WAY
        # through the message now at ``last_consolidated``. mark_consolidated
        # is reached in exactly the two cases that make it meaningless: the
        # marker advanced past that head (its last slice succeeded, or a
        # whole-message prefix was marked), or it was reset to 0 (rotation /
        # edit). Both leave no partially-extracted head for it to resume, so
        # drop it in the same locked write — a stale sub-offset left behind
        # would otherwise seek into whatever message next lands at the marker.
        for _sub_key in _CONSOLIDATION_SUBOFFSET_META_KEYS:
            meta.pop(_sub_key, None)
        lines[0] = json.dumps(meta) + "\n"
        # Reduce lock hold for this one-line metadata rewrite: skip the
        # fsync (fsync=False). ``last_consolidated`` is recoverable
        # bookkeeping — if a crash loses it we simply re-consolidate a few
        # messages — so paying a disk flush while holding the cross-process
        # lock (blocking every other writer of this session) isn't worth it.
        # ``newline=""``: the lines hold the LF ends the transcript is written with,
        # and a translating write would turn every one into CRLF on Windows.
        atomic_write(path, "".join(lines), fsync=False, newline="")
        # Housekeeping bookkeeping — must not advance the session's mtime
        # (see _restore_mtime). Otherwise consolidation floats stale sessions
        # to the top of list_sessions on every gateway restart.
        _restore_mtime(path, prev_mtime)
        # A first-line rewrite: move the page index onto the new file so the next
        # history page does not frame the whole transcript again.
        self._read_projection.carry_index_over_head_rewrite(key, before, old_head, lines[0])
        # Invalidate while still holding the lock. Outside it there is a
        # window where the file is already rewritten but the generation has
        # not moved, so a concurrent fold / snippet / metadata read could
        # publish before the local invalidation. Every other preserved-mtime
        # writer already invalidates inside its locked section;
        # _invalidate_cache is pure in-memory work, so this adds no I/O
        # under the cross-process flock.
        self._invalidate_cache(key)


def advance_consolidation_sub_offset(
    self: ConversationLog, key: str, offset: int, sub_offset: int, generation: int
) -> None:
    """Record extraction PART-WAY through the head message at *offset*.

    Called after a non-final slice of an over-budget head message succeeds
    (see :func:`~kiro_crew.history_consolidation._slice_head_for_budget`):
    the durable ``last_consolidated`` marker must NOT move yet, because the
    head is only partly extracted, so this advances the sub-message offset
    instead. The next pass resumes slicing at *sub_offset*; the marker moves
    past the whole message only once its last slice succeeds, through
    :meth:`mark_consolidated`.

    Three guards, all under the one lock, keep the sub-offset from ever
    resuming into content it does not describe:

    * The session must still exist (``_update_metadata_locked`` upserts, so
      writing a deleted session would resurrect it as metadata-only).
    * The rotation generation must still equal *generation* — a rotation or
      rewrite during the (slow) slice await changed the content under the
      head, so the sub-offset is meaningless and is NOT written (the stale
      read in :meth:`_sub_offset_for_generation` would reject it anyway, but
      not writing keeps the line clean).
    * ``last_consolidated`` must still equal *offset* — the head the slice
      came from must still be the one at the marker. A marker that moved
      (another process consolidated) means a different head sits there now.

    *sub_offset* is the absolute character offset into the head's content the
    slice reached; it is stamped with *generation* so a later rotation
    invalidates it the same way the attempt stamps are invalidated.
    """
    with self._locked(key):
        path = self._path(key)
        if not path.exists():
            return
        prev_mtime = _safe_mtime(path)
        before = path.stat()
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not lines:
            return
        old_head = lines[0]
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
            return
        try:
            current_offset = int(meta.get("last_consolidated", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            current_offset = 0
        if current_offset != offset:
            logger.info(
                "advance_consolidation_sub_offset: marker moved %d->%d for "
                "%s; the sliced head is no longer at the marker, dropping "
                "sub-offset",
                offset,
                current_offset,
                key,
            )
            return
        meta["consolidation_sub_offset"] = max(0, int(sub_offset))
        meta["consolidation_sub_offset_generation"] = generation
        # A slice that reached its durable sub-offset is a span that ended —
        # extracted on the success path, or abandoned at the cap. Either way
        # the next pass attempts the NEXT slice, which is different content
        # and must start with a fresh attempt budget. Clear the retry
        # accounting in the SAME locked write so no window exists where the
        # sub-offset advanced but the previous slice's failed attempts still
        # stand (they would otherwise be charged against the next slice and
        # abandon it early). This mirrors mark_consolidated clearing the
        # accounting when it applies a message advance.
        for _acct_key in _CONSOLIDATION_META_KEYS:
            meta.pop(_acct_key, None)
        meta["updated_at"] = metadata_now_iso()
        lines[0] = json.dumps(meta) + "\n"
        # Same cheap, mtime-preserving, no-fsync write as mark_consolidated:
        # a lost sub-offset only re-slices the head from its start, which is
        # correct behaviour, so it is not worth a disk flush under the lock.
        # ``newline=""``: the lines hold the LF ends the transcript is written with,
        # and a translating write would turn every one into CRLF on Windows.
        atomic_write(path, "".join(lines), fsync=False, newline="")
        _restore_mtime(path, prev_mtime)
        self._read_projection.carry_index_over_head_rewrite(key, before, old_head, lines[0])
        self._invalidate_cache(key)


def unconsolidated_count(self: ConversationLog, key: str) -> int:
    """Count messages not yet processed by the consolidator.

    Counts without materializing the transcript (see :meth:`_count_messages`):
    the startup idle-seed scan calls this for every recent transcript.
    """
    total = self._count_messages(key)
    offset = self._read_metadata(key).get("last_consolidated", 0)
    return max(0, total - offset)


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
    offset = self._read_metadata(key).get("last_consolidated", 0)
    try:
        return int(offset or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def consolidation_retry_state(
    self: ConversationLog, key: str, message_count: int | None = None
) -> tuple[int, float]:
    """Return ``(failed_attempts, next_eligible_at)`` for *key*.

    Both live on the metadata line beside ``last_consolidated``, so the
    accounting shares the marker's lifetime: it survives a gateway restart
    (the consolidator's own throttle is in-memory only) and is cleared by
    :meth:`mark_consolidated` when the span finally lands. ``(0, 0.0)`` means
    no failed attempt is on record — the common case — so callers can treat a
    missing entry as "eligible now".

    Read UNCACHED. The accounting is cross-process (a gateway sweep, the CLI,
    a subagent all record failures for the same session), and every writer of
    these fields restores the file's pre-write mtime so housekeeping does not
    reorder ``list_sessions``. The metadata cache identity detects the normal
    atomic rewrite, but this accounting path deliberately reads uncached: a
    second process can update its durable count between a warm read and the
    following increment. Dropping the entry first costs one first-line read.

    Metadata is caller-supplied JSON, so every conversion is defensive:
    ``1e309`` parses to ``inf`` and ``int(inf)`` raises ``OverflowError``
    (which would surface as a 500 from the manual trigger or break the idle
    sweep), while a ``NaN`` deadline makes every ``now >= retry_at``
    comparison false and disables consolidation for the session forever.
    Non-finite and unconvertible values therefore reset to the eligible zero
    state rather than propagating.
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
        # The counter belongs to a span that is gone: a rotation
        # archived those messages (generation bumped), a rewrite reset the
        # marker they started from, or the transcript has grown past the
        # extent that was charged. The cap ABANDONS a span, so carrying a
        # capped count onto different content would silence consolidation for
        # this session permanently — every message written after that point
        # would stay ineligible forever. The new span gets a fresh budget.
        #
        # The deadline is deliberately kept: a fresh budget is not a free
        # immediate turn, so a session that keeps failing still waits out the
        # backoff already armed (4 h at the cap) rather than re-billing the
        # instant one more message lands.
        attempts = 0
    return max(0, attempts), retry_at


def _attempts_describe_current_span(
    self: ConversationLog, meta: dict, message_count: int | None
) -> bool:
    """True when the recorded attempts belong to the span in front of us now.

    A span is identified by where it starts AND how far it reaches: the
    ``(rotation_generation, last_consolidated)`` pair it was charged against,
    the message count the transcript held at the time, and the boundary the
    prompt actually reached. While a span keeps
    failing none of the three move — the marker is only advanced on success —
    so the cap holds across attempts. Anything that changes the CONTENT under
    the counter moves one of them: a rotation and a dashboard rewrite
    (regenerate / rewind / fork) both advance the generation, a rewrite that
    cannot apply a stale offset resets the marker, and new messages push the
    transcript past the extent that was attempted.

    The generation is what covers the edit that moves nothing else. A
    regenerate replaces the assistant tail with a reply the failing turns
    never saw and lands at the same message count, the same marker and the
    same extent, so without the bump a capped budget would sit over brand-new
    content and refuse it forever.

    The extent matters because the cap is what ABANDONS a span, and it is only
    reachable from :meth:`HistoryConsolidator.retry_eligible` when the
    abandon-marker write itself failed. Without it, that one transient write
    failure would refuse the session forever: appended messages leave the
    generation and marker untouched, so every entry point would keep rejecting
    a transcript that is not the one that failed, and the session's
    history would never be consolidated again.

    *message_count* is the transcript's CURRENT total, supplied by the caller.
    It is never read from disk here: this predicate runs inside
    ``retry_eligible`` on the gateway event loop, and a synchronous full-file
    read there stalls every other gateway task on a large transcript. Callers
    that already hold a count pass it (see :meth:`consolidation_counts`);
    ``None`` means "no count available", which skips the extent test and keeps
    the cap — the conservative direction, since the alternative is spending a
    billed turn on an unverified premise.

    Growth releases the cap only for an attempt that reached the END of the
    transcript it was charged against — ``consolidation_attempts_prompted``
    equal to ``consolidation_attempts_count``. A bounded attempt
    (``prompted`` short of the total, because
    :func:`~kiro_crew.history_consolidation._consolidation_chunk` cut the
    prompt at the budget) attempted a PREFIX of the unconsolidated tail, and
    appending messages cannot change a prefix: the next pass starts at the
    same marker, renders the same budget, and puts the same content in front
    of the provider. Counting that as new content is what would let a
    permanently over-budget head message be re-billed on every idle window
    for as long as the session keeps receiving turns — the attempts would
    reset before they could ever reach the cap that abandons it. Whether the
    attempt was bounded is read from the stamp, so this stays one metadata
    read with no transcript access.

    The cost is that a bounded span whose abandon-marker write ALSO failed
    keeps its cap until a rotation, a rewrite, or a later successful marker
    write moves it. That span is genuinely unchanged, so refusing it is
    correct in itself; what it loses is the growth rescue an unbounded span
    keeps. Re-billing identical content forever is the worse of the two.

    Growth is compared with ``>`` rather than ``!=`` on purpose. A count that
    SHRANK is a rotation or compaction, which already moves the generation or
    the marker; treating a shrink as new content on its own would hand a
    budget to a span with nothing added to it.

    Unstamped accounting (written before a given field existed) is treated as
    belonging to the current span, so an unknown provenance keeps the cap
    rather than granting an unbounded supply of billed retries.

    The stamp this compares against is the ATTEMPTED span, captured before the
    turn (see :class:`AttemptedSpan`), which is what makes all three tests
    meaningful: if the stamp were re-read after the turn it would describe the
    transcript in front of us by construction, and every test here would
    trivially match.
    """
    for meta_key, span_key in (
        ("rotation_generation", "consolidation_attempts_generation"),
        ("last_consolidated", "consolidation_attempts_offset"),
    ):
        if span_key not in meta:
            continue
        try:
            if int(meta.get(span_key, 0) or 0) != int(meta.get(meta_key, 0) or 0):
                return False
        except (TypeError, ValueError, OverflowError):
            # Unreadable stamp — fall back to keeping the cap.
            continue
    # A sub-slice of an over-budget head starts at a durable sub-offset. When
    # the sub-offset moved between the charge and now, the attempt covered a
    # DIFFERENT slice of the same head (an earlier slice succeeded and
    # advanced it), so the cap it was charged against describes different
    # content than what is in front of us — a fresh budget is correct.
    #
    # The live sub-offset must be read through ``_sub_offset_for_generation``,
    # not the raw stored value: a rotation resets ``last_consolidated`` and
    # bumps the generation but leaves ``consolidation_sub_offset`` at its old
    # nonzero value, while the snapshot path (``_sub_offset_for_generation``)
    # correctly reads 0 for the new generation. Comparing the attempt's stamp
    # (0, re-sliced from the start under the new generation) against that
    # stale raw value would read every failure on the new span as a "different
    # slice" and reset the cap each time, so the abandon path would never be
    # reached — the exact indefinite-retry the cap exists to prevent. Reading
    # the generation-honoured value makes the comparison see the same 0 the
    # slicer used, so a genuinely-advanced earlier slice still resets the cap
    # while a bare rotation does not.
    if "consolidation_attempts_sub_offset" in meta:
        try:
            current_generation = int(meta.get("rotation_generation", 0) or 0)
            live_sub_offset = self._sub_offset_for_generation(meta, current_generation)
            if int(meta.get("consolidation_attempts_sub_offset", 0) or 0) != live_sub_offset:
                return False
        except (TypeError, ValueError, OverflowError):
            pass
    # A non-final slice of a head is bounded WITHIN the message: it attempted
    # a prefix of the head's content, and appending whole messages behind the
    # head cannot change that prefix (the next pass re-slices the same head at
    # the same sub-offset and renders the same budget). So message growth must
    # NOT release it, exactly as a prompted-short-of-total message prefix is
    # not released below. Without this a head that is the ONLY unconsolidated
    # message — where ``prompted == count`` holds for the slice — would have
    # its cap reset by every appended turn and never reach the abandon path.
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

    Every field comes from *span* — the consolidation's own pre-turn snapshot
    (see :class:`AttemptedSpan`) — and NOTHING is re-read from the metadata
    line here. The stamp must describe the span the turn actually attempted,
    and the metadata line at failure time describes the transcript as it is
    *now*, which a rotation or a marker-resetting rewrite during the turn has
    already changed.
    The clamps are the only normalization: *span* is typed, and its values come
    from a snapshot's own ``len()``/generation reads, so there is nothing to
    coerce — but a negative offset or count would be nonsense to stamp.

    ``consolidation_attempts_sub_offset`` records where in an over-budget head
    the failed slice began, so a later pass that advanced the sub-offset
    (an earlier slice succeeded) is recognised as a different slice.
    ``consolidation_attempts_sub_bounded`` is 1 when the failed attempt was a
    NON-FINAL slice of the head — a prefix of the message content — so
    :meth:`_attempts_describe_current_span` keeps its cap through message
    growth the same way it does for a prompt cut short of the message total.
    """
    sub_offset = max(0, span.sub_offset)
    # Any slice of an over-budget head is bounded WITHIN the message: it
    # renders a budget-sized window of the head's content, and appending
    # whole messages behind the head cannot change that window. A slice is in
    # play whenever the prompt advanced past where it started
    # (``sub_prompted > sub_offset``); a span that never sub-sliced carries
    # both as 0, leaving the ordinary message-aligned accounting unchanged.
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

    *span* is the identity of what the failing turn actually attempted, taken
    from the pre-turn snapshot (see :class:`AttemptedSpan`). It serves twice:
    as the stamp written beside the counter, and as the span the existing
    counter is compared against — so a turn that attempted a DIFFERENT span
    starts a fresh budget while one attempting the same span increments toward
    the cap. Nothing about the span is re-read from the file here, so a
    rotation or rewrite that landed during the turn cannot relabel this charge
    as belonging to content it never measured.

    Returns the new ``(attempts, next_eligible_at)``. The wait doubles per
    attempt from *base_secs*, capped at *max_secs*, so a persistently broken
    span costs a geometrically shrinking number of billed LLM turns instead
    of one per heartbeat tick.

    The read-increment-write runs under a single :meth:`_locked` hold: two
    processes consolidating the same session (gateway sweep and CLI) would
    otherwise both read the same count and write the same value, letting the
    span consume unbounded attempts while the counter sits still. The read
    inside is uncached for the same reason (see
    :meth:`consolidation_retry_state`) — an mtime-preserving write by the
    other process is invisible to a warm cache.

    Returns ``(0, 0.0)`` without writing when the session's transcript is gone
    (deleted while the consolidation was in flight): ``_update_metadata_locked``
    upserts, so writing would resurrect the session as an empty metadata-only
    file. Blocking file IO — call it off the event loop.
    """
    stamp = _time.time() if now is None else now
    with self._locked(key):
        if not self._path(key).exists():
            # The session was deleted while this consolidation was in flight.
            # _update_metadata_locked upserts, so writing here would recreate
            # the transcript as a metadata-only file — resurrecting a deleted
            # session as empty history in list_sessions. Nothing to account
            # for: the span it described is gone.
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
                # Bind the count to the span it was charged against — where it
                # starts and how far it reaches — so a rotation, a rewrite, or
                # a grown transcript cannot leave a capped counter sitting over
                # content it never measured (see
                # _attempts_describe_current_span). Every field comes from the
                # pre-turn snapshot, never from the file as it stands now, and
                # is written in the SAME locked update as the counter, so no
                # window exists where the count is charged but its span is
                # unidentified or misidentified.
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
