"""The metadata line a dashboard save writes, folded from the slot and the disk.

The first line of a session file carries the slot-owned fields (title, folder,
tags, model, mode and the rest), fields other layers own (carried through, a
durable execution record only ever tightened to the line's mode), and the privacy
contract every learning reader gates on (``memory_mode``). The slot-owned fields
and their key order are ``metadata_codec``'s; this module owns what a save folds
against the line on disk before it encodes them: the memory-mode ratchet and its
worker-to-loop witness, the monotonic newest-human-turn stamp, the bounded
dismissed source-link line, the held-note retirement, and the rows-only deferral.
``_save_slot_to_history`` decides when it runs.

New rules about what a save folds from the line it replaces belong here; new
slot-owned fields belong in ``metadata_codec``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from itertools import chain
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.slot_buffers import serialize_deferred_notes, union_deferred_notes
from kiro_crew.dashboard.slot_queue_repository import queue_persist_signature
from kiro_crew.dashboard.state import _MAX_DISMISSED_SOURCE_LINKS
from kiro_crew.execution_context import (
    EXECUTION_CONTEXT_KEY,
    MEMORY_MODES,
    canonical_memory_mode,
    read_session_execution,
    stricter_memory_mode,
)
from kiro_crew.history import (
    HUMAN_TURN_META_KEY,
    ROWS_ONLY_DEFERRED_META_KEYS,
    ROWS_ONLY_OWNED_META_KEYS,
    SLOT_OWNED_META_KEYS,
    carry_unowned_metadata,
    latest_transcript_ts,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.history import ConversationLog

logger = logging.getLogger("kiro_crew.dashboard.chat_persistence")


#: Metadata-line field holding the instant of a session's newest HUMAN turn.
#: Deliberately absent from :data:`SLOT_OWNED_META_KEYS`: the window this save
#: serializes is BOUNDED, so a save whose window has scrolled past the last user
#: row derives nothing, and an owned key's absence would erase a valid stamp.
#: Unowned means ``carry_unowned_metadata`` keeps the stored value instead.
_META_LAST_USER_AT = "last_user_at"


def _latest_stamp(*candidates: str) -> str:
    """The latest usable transcript stamp among *candidates*, or ``""``.

    :func:`latest_transcript_ts` compares through ``transcript_sort_key``, which
    resolves a NAIVE value with ``astimezone()`` -- and that raises at the
    representable boundary, measured: ``ValueError: year 0 is out of range`` for
    ``0001-01-01T00:00:00``, and ``year 10000`` for ``9999-12-31T23:59:59``. Such
    a stamp PARSES, so the unparseable path does not catch it, and the raise would
    abort the whole slot save rather than cost one row its stamp.

    Folded one candidate at a time so a single unusable value costs only itself
    instead of discarding every stamp passed beside it.
    """
    best = ""
    for candidate in candidates:
        if not candidate:
            continue
        try:
            best = latest_transcript_ts(best, candidate) or best
        except (ValueError, OverflowError, OSError):
            continue
    return best


def _newest_human_turn_ts(rows: "list[dict] | tuple[dict, ...]") -> str:
    """The ``ts`` of the newest row a PERSON authored in *rows*, or ``""``.

    Gated on :data:`~kiro_crew.history.HUMAN_TURN_META_KEY`, which the send paths
    a human reaches set on the row. ``role == "user"`` is NOT enough on its own:
    the gateway drives agent turns through the same shape, and
    ``_ChatSlot.enqueue_or_run_prompt`` appends ``("user", prompt, "msg msg-u")``
    for an Issue Radar wake -- identical in role and in presentation class to a
    typed message. Requiring the marker rather than excluding the machine callers
    we happen to know about is what keeps the NEXT such caller from silently
    advancing a session's human-activity stamp.

    An unmarked row therefore does not count, which is the safe direction: a
    session whose human turns predate the marker keeps ranking by ``st_mtime``,
    exactly as it does today.

    The marker is the WHOLE gate. There is deliberately no ``role == "user"``
    check beside it: only a human send path sets the marker, so the role is
    implied, and a second condition no input can distinguish is dead weight that
    reads as defence.

    Ordering goes through :func:`_latest_stamp`, never string comparison: rows
    carry both naive and offset-aware stamps, so ``"a" > "b"`` on the raw text
    compares two different domains and can pick the earlier row.
    """
    stamps: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_meta = row.get("meta")
        if not isinstance(row_meta, dict) or row_meta.get(HUMAN_TURN_META_KEY) is not True:
            continue
        ts = row.get("ts")
        if isinstance(ts, str) and ts:
            stamps.append(ts)
    return _latest_stamp(*stamps)


def _capped_dismissed_line(keys: Iterable[str]) -> list[str]:
    """The bounded, sorted ``dismissed_source_links`` value for a metadata write.

    A bound must be applied at the point a field is RETAINED, not only where it
    is read back: the restore path already caps the in-memory set, but the
    save/merge paths union the slot's set with the on-disk line and write the
    result, so an oversized on-disk line (tampered, or grown by an older build)
    would round-trip an unbounded set straight back to disk. Re-validate each key
    against the canonical serialized-identity grammar (the carry-forward callers
    pass the raw on-disk list, so a single tampered/oversized string must be
    dropped here, matching the union paths' filter), sort for a stable line, then
    keep only the first ``_MAX_DISMISSED_SOURCE_LINKS`` — dropping the tail only
    ever fails toward SHOWING a chip, never toward hiding an unrelated one — and
    log when a write is truncated so the drop is observable.
    """
    from kiro_crew.dashboard.source_providers.contract import is_valid_source_identity_key

    # Bound RETENTION during iteration: never hold more than the cap. Keep a
    # bounded ``kept`` set of the cap-smallest unique valid keys seen so far —
    # once it is at capacity, a newly-seen key replaces the current largest kept
    # key (found via ``max(kept)``) only when it sorts before it, so ``kept``
    # converges to the cap-smallest keys. At most _MAX_DISMISSED_SOURCE_LINKS
    # keys are ever resident regardless of how oversized/tampered the input is.
    # The result is the deterministic sorted prefix — identical to
    # ``sorted(unique)[:cap]`` — so the written line is stable and matches what a
    # smaller input would produce. Dropping the tail only ever fails toward
    # SHOWING a chip, never toward hiding an unrelated one.
    cap = _MAX_DISMISSED_SOURCE_LINKS
    kept: set[str] = set()
    truncated = 0
    for key in keys:
        if not is_valid_source_identity_key(key) or key in kept:
            continue
        if len(kept) < cap:
            kept.add(key)
        else:
            # At capacity: keep this key only if it sorts BEFORE the current
            # largest kept key, so ``kept`` converges to the cap-smallest unique
            # keys — the same set ``sorted(all_unique)[:cap]`` would select — while
            # never holding more than cap keys resident.
            largest = max(kept)
            if key < largest:
                kept.discard(largest)
                kept.add(key)
            truncated += 1
    if truncated:
        logger.warning(
            "dismissed_source_links write truncated by %d to the %d cap",
            truncated,
            cap,
        )
    return sorted(kept)


def _tighten_carried_execution(meta_line: dict, mode: str) -> None:
    """Fold *mode* into a carried durable execution record on ``meta_line``.

    A persistent session's execution carrier is DURABLE: ``bind_session_execution``
    writes it into the metadata line under ``execution_context``, with a
    ``memory_mode`` of its own, and ``read_session_execution`` answers from that
    record when no live carrier exists. The save carries the record rather than
    owning it, so when the line's ``memory_mode`` is ratcheted to a stricter value
    -- a restricted original's rows landing under a persistent same-key
    replacement's line -- the record would keep saying ``persistent`` and every
    reader of the session's execution would take the looser mode from it. The
    record is a ratchet like the line: it is only ever tightened, never rebuilt,
    so the identity it carries is untouched and a record that is already at least
    as strict is left exactly as it was. A malformed record is left alone too --
    the read path refuses it on its own terms, and a save is not where to judge it.
    """
    if mode == "persistent":
        return
    payload = meta_line.get(EXECUTION_CONTEXT_KEY)
    if not isinstance(payload, dict):
        return
    record_mode = canonical_memory_mode(payload.get("memory_mode"))
    retained = stricter_memory_mode(record_mode, mode)
    if retained == record_mode:
        return
    meta_line[EXECUTION_CONTEXT_KEY] = {**payload, "memory_mode": retained}


# One lock for every save thread's pending-mode fold. Two saves of one slot can
# complete on different worker threads (the full save records inside the
# transcript lock, the empty-window merge after ``update_metadata_if`` returns),
# and the fold is a read-then-write: without the lock the later, stricter value
# could be overwritten by an earlier completion's looser one. The value itself
# is monotonic -- it only ever moves to a stricter mode and the loop-side
# consumer never clears it -- so holding this lock for the fold is all the
# ordering the seam needs.
_PENDING_MEMORY_MODE_LOCK = threading.Lock()


def _record_pending_memory_mode(slot: _ChatSlot, memory_mode: str) -> None:
    """Record a committed line tightening without mutating loop-affine state."""
    with _PENDING_MEMORY_MODE_LOCK:
        current = canonical_memory_mode(getattr(slot, "memory_mode", "persistent"))
        pending = getattr(slot, "_pending_memory_mode", None)
        folded = stricter_memory_mode(current, canonical_memory_mode(memory_mode))
        if pending is not None:
            folded = stricter_memory_mode(folded, canonical_memory_mode(pending))
        if folded != "persistent":
            slot._pending_memory_mode = folded


def pending_slot_memory_mode(slot: _ChatSlot) -> str | None:
    """Read the monotonic worker-to-loop privacy witness under its lock."""
    with _PENDING_MEMORY_MODE_LOCK:
        pending = getattr(slot, "_pending_memory_mode", None)
        return canonical_memory_mode(pending) if pending is not None else None


def retained_memory_mode(slot: _ChatSlot, live_session: str) -> str:
    """The ``memory_mode`` this save records: the strictest one known.

    Every mode writes its transcript -- an incognito or temporary chat is
    one the user can reopen from History; what the mode withholds is
    learning FROM it (consolidation, lessons, memory injection), and every
    one of those readers gates on the ``memory_mode`` this line carries. So
    the field is the privacy contract of the file, and it must never be
    looser than what the session is actually running under. The slot's own
    mode and the live execution carrier can disagree for a moment (a mode
    switch publishes the carrier first; a queued-prompt flush can outlive
    the slot's own tightening), and stricter-wins closes that window: a
    restart re-reads this line, so a looser value here would be exactly
    the weaker mode a restart must never recover.
    """
    from kiro_crew.memory_stores import MissingExecutionIdentity

    slot_mode = canonical_memory_mode(getattr(slot, "memory_mode", "persistent"))
    try:
        execution = read_session_execution(live_session)
    except MissingExecutionIdentity as exc:
        # Only this refusal is let through: a readable record that names a
        # member or private store but has no ``execution_context`` field at
        # all -- a session written before that field existed. This write is
        # the TRANSCRIPT, not private memory: the carrier only decides
        # retention here, and with none to read the slot's own mode is the
        # only retention there is. Refusing would fail every save and every
        # close of such a session, which then restores itself to the list
        # forever. Every other ``UnknownMemoryStore`` (unreadable record, a
        # present-but-malformed carrier, an undeclared store) propagates:
        # its retention is unknown, so the save fails rather than record a
        # mode it cannot vouch for. Authorizing private memory from the
        # store name stays refused where it is read for that purpose.
        logger.debug(
            "Slot %s predates canonical execution identity (%s); saving its "
            "transcript under the slot's own retention mode",
            slot.key,
            exc,
        )
        return slot_mode
    if execution is None:
        return slot_mode
    return stricter_memory_mode(slot_mode, execution.memory_mode)


def tighten_line_for_appends(
    conv_log: ConversationLog, slot: _ChatSlot, history_key: str, *, live_session: str
) -> str | None:
    """Make the header an append-safe write lands rows under carry *slot*'s privacy.

    The full save rebuilds the metadata line and so records ``memory_mode``
    on it; an append-shaped write goes through ``ConversationLog.append``,
    which mints a header with no mode at all when the file is absent and
    leaves an existing header exactly as it found it. A restricted slot's rows
    landing that way -- the hand-over drain of a popped slot whose truncation
    lost its key, the commit gate's own fallback for the same write -- would
    sit under a line that reads ``persistent``, and every reader that learns
    from the transcript (consolidation, lessons, injection) gates on that
    line. So the line is settled FIRST, under the per-session lock the appends
    take: created when absent, carrying the slot's retained mode; tightened
    when present, to the stricter of its own mode and the retained one, with
    the carried execution record folded to match and the store a restricted
    line must not name cleared. A merge, never a rebuild -- every other field
    (a replacement's title, folder, tag, pin) stays as the line holds it,
    exactly as the rows-only save defers them. A persistent slot over an absent
    line writes nothing: ``append`` mints the mode-less header a persistent
    session has always had.

    Returns the mode the line carries afterwards, CONFIRMED by re-reading the
    header under the lock -- or ``None``, and then the caller must not append:
    the header could not be read (the merge defers on a transient read failure
    without running its guard, and a corrupt first line that the merge did not
    rewrite stays unreadable), or it still reads looser than the slot's
    retained mode. Fail closed on purpose: a row appended under a header whose
    privacy contract is unverified is exactly the breach this settles, and
    every caller has a refusal arm that reports or keeps the rows owed.

    Caller holds ``_locked(history_key)`` (or ``atomic_appends``); the merge
    and the confirming read re-enter it on this thread.
    """
    retained = retained_memory_mode(slot, live_session)
    fields: dict = {}
    mode = {"value": retained}
    guard_ran = {"value": False}

    def _tighten_under_lock(meta: dict) -> bool:
        guard_ran["value"] = True
        fields.clear()
        line_mode = line_memory_mode(meta)
        mode["value"] = stricter_memory_mode(line_mode, retained)
        if not meta:
            if mode["value"] == "persistent":
                return False
            fields["memory_mode"] = mode["value"]
            return True
        if mode["value"] != line_mode:
            fields["memory_mode"] = mode["value"]
            # The merge codec's spelling for "names no store" (see the
            # ``memory_store`` field in ``metadata_codec``): a merge cannot
            # drop a key, and every reader treats an empty name as absent.
            fields["memory_store"] = ""
        carried = dict(meta)
        _tighten_carried_execution(carried, mode["value"])
        if carried.get(EXECUTION_CONTEXT_KEY) != meta.get(EXECUTION_CONTEXT_KEY):
            fields[EXECUTION_CONTEXT_KEY] = carried[EXECUTION_CONTEXT_KEY]
        return bool(fields)

    if conv_log.update_metadata_if(history_key, fields, _tighten_under_lock):
        logger.info(
            "Slot %s: tightened the %s line to %s ahead of an append-safe write",
            slot.key,
            history_key,
            mode["value"],
        )
    elif not guard_ran["value"]:
        logger.warning(
            "Slot %s: the %s header could not be read; its privacy contract is "
            "unverified, so no row is appended under it",
            slot.key,
            history_key,
        )
        return None
    # The confirmation, not the merge's answer: what the header carries NOW,
    # under this lock, is what the appended rows will sit under.
    meta, readable = conv_log.get_metadata_status(history_key)
    if not readable:
        logger.warning(
            "Slot %s: the %s header is unreadable after the tightening merge; no row "
            "is appended under it",
            slot.key,
            history_key,
        )
        return None
    line_mode = line_memory_mode(meta)
    if stricter_memory_mode(line_mode, retained) != line_mode:
        logger.warning(
            "Slot %s: the %s header still reads %s against a retained %s; no row is "
            "appended under it",
            slot.key,
            history_key,
            line_mode,
            retained,
        )
        return None
    return line_mode


def line_memory_mode(meta: dict) -> str:
    """The ``memory_mode`` the on-disk line already carries, canonicalised.

    The line is a RATCHET: a later writer on the same key folds this value
    in with :func:`stricter_memory_mode` and can only tighten it. The rows a
    restricted slot committed stay in the file after that slot is popped,
    and a persistent slot recreated on the freed key rebuilds the line from
    its own state -- without the fold it would relabel those rows
    persistent and hand them to every learning reader. An absent or
    unrecognised value after case canonicalisation reads as persistent, so
    it never tightens anything.
    """
    line_mode = str(meta.get("memory_mode") or "persistent").lower()
    return line_mode if line_mode in MEMORY_MODES else "persistent"


def _merge_dismissed_line(slot: _ChatSlot, meta: dict) -> list[str] | None:
    """The ``dismissed_source_links`` an empty-window merge writes, or ``None``.

    Decided under the lock from the on-disk ``meta``. When the slot's set is
    HYDRATED (reflects disk) it is written (sorted, deterministic; empty =
    nothing dismissed). When it is UNHYDRATED (bound while the set could not be
    read) the in-memory empty set does not reflect disk, so the on-disk line is
    carried forward rather than erasing the real tombstones. And while an unlink
    transaction holds an uncommitted TENTATIVE dismissal
    (``_dismissed_txn_depth`` > 0) the on-disk line is carried forward too: the
    tentative set may be rolled back by a failed guarded write, and this
    provisional flush must not outlive it.
    """
    if slot._dismissed_hydrated and slot._dismissed_txn_depth == 0:
        # UNION with the on-disk line rather than replacing: a stale off-loop
        # prefetch can bind a hydrated set that predates a concurrent unlink's
        # committed tombstone, and a bare replacement would erase it. Dismissals
        # only grow, so the union can only ADD. The in-memory set and the raw
        # (untrusted) on-disk list go in as ONE chained iterable:
        # _capped_dismissed_line validates, dedups and bounds during iteration,
        # so no unbounded merged set is built before the cap.
        disk_prev = meta.get("dismissed_source_links")
        if isinstance(disk_prev, list):
            return _capped_dismissed_line(chain(slot._dismissed_source_links, disk_prev))
        return _capped_dismissed_line(slot._dismissed_source_links)
    carry = meta.get("dismissed_source_links")
    if isinstance(carry, list) and carry:
        return _capped_dismissed_line(carry)
    return None


def merge_empty_window(
    conv_log: ConversationLog,
    slot: _ChatSlot,
    history_key: str,
    *,
    live_session: str,
    queue_snapshot: list[dict],
    closed: bool,
    closed_at: float | None,
    pending_mode_target: _ChatSlot,
    refusal_under_lock: Callable[[dict], str | None] | None = None,
    mutes_opened_override: bool | None = None,
    after_commit_under_lock: Callable[[], None] | None = None,
    queue_deferred_under_lock: Callable[[dict], bool] | None = None,
) -> str | None:
    """Commit a forced or closing save of a message-less slot as a metadata merge.

    ``refusal_under_lock`` is evaluated against the line read under the
    cross-process lock, after the existence check. A non-``None`` reason skips
    the write and is returned, so the caller can keep the slot owed; ``None``
    is returned on a commit and on the by-design skip of a line-less tab.

    ``queue_deferred_under_lock`` is evaluated at the same point. ``True``
    commits every other field but leaves the line's queued prompts as they are
    on disk and does not move the queue witness, so a snapshot older than a
    queue another writer committed cannot overwrite it, and the queue stays
    owed for the next save.

    Raises ``OSError`` when the record is unreadable, so a close or an
    acknowledged edit is never reported durable on a merge that did not happen.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    # A FORCED (or closing) save of a message-less slot is a metadata
    # mutation (folder filing/unfiling, a tag assignment, a pin, a
    # pinned title, a mode switch, a close) -- the full save has no window to
    # write, but the mutation still has to reach disk. `session_create`
    # persists `folder_id` at birth, so an empty newborn HAS a metadata
    # line and any acknowledged metadata change before its first message must
    # overwrite that line, or a restart resurrects the birth state the
    # user already changed. The merge carries every slot-owned field a
    # force/closed save is responsible for -- the tag routes, the pin route,
    # the recreate PATCH (folder or pinned title) and the close path all
    # persist ONLY through this save. Merged ONLY into an existing line: a
    # slot with no line at all does not survive a restart, so there is nothing
    # to reconcile, and materializing files for every empty tab here would
    # create transcripts nothing else expects. The existence guard runs INSIDE
    # the same cross-process lock as the merge (`update_metadata_if`): the
    # plain update is an upsert, so a checked-then-written pair would let a
    # permanent deletion land between the two and be resurrected as a fresh
    # file. Fails closed on an unreadable record, per `update_metadata_if`'s
    # own contract.
    #
    # The merge writes the codec's MERGE form: the same slot-owned fields a
    # full save writes, with clearable fields written even when empty (the
    # merge cannot delete a key; a read treats a falsy value as cleared) and
    # identity and monotonic fields written only when set (origin's fail-closed
    # sentinel and the once-flags must never be erased by a writer that has not
    # learned them).
    #
    # The slot fields are read INSIDE the guard, which `update_metadata_if`
    # evaluates under the cross-process lock at write time -- exactly the
    # contract that method exists for ("the decision is re-made here rather
    # than trusted from before the lock"). A dict snapshotted before the lock
    # could commit out of order: a tag save that snapshotted `pinned=False`
    # before a concurrent pin request committed `pinned=True` would land its
    # stale aggregate second and silently revert the acknowledged pin. The
    # full save has the same shape -- it builds its metadata line from slot
    # state inside the locked block -- so whichever writer commits last writes
    # the newest slot state.
    merged_fields: dict = {}
    guard_state: dict = {"ran": False, "refusal": None}

    def _refresh_under_lock(meta: dict) -> bool:
        guard_state["ran"] = True
        if not meta:
            # A line-less tab has no transcript, so no restart can restore its
            # queue and nothing on disk is owed. Credit the witness, or the
            # flush would re-run this save for the same drift on every tick.
            # The tab's first line comes from a full save, which writes the
            # live queue itself.
            if slot_history_key(slot) == history_key:
                slot._queue_persisted_sig = queue_persist_signature(queue_snapshot)
            return False
        if refusal_under_lock is not None:
            refusal = refusal_under_lock(meta)
            if refusal is not None:
                guard_state["refusal"] = refusal
                return False
        merged_fields.clear()
        if slot.reasoning_effort:
            cp._remember_reasoning_effort_for_restore(slot.reasoning_effort)
        # ``meta`` is the line as the guard read it under the lock, so the
        # ratchet folds the CURRENT on-disk mode, not a snapshot.
        mode = stricter_memory_mode(
            line_memory_mode(meta), retained_memory_mode(slot, live_session)
        )
        merged_closed_at: float | None = None
        if closed:
            merged_closed_at = closed_at if closed_at is not None else time.time()
        folds = cp._metadata_codec.SaveFolds(
            memory_mode=mode,
            closed=closed,
            # Without the close a closed empty newborn's line stays open-shaped
            # and the next restart resurrects a tab the user dismissed.
            closed_at=merged_closed_at,
            # The value is the snapshot taken WITH the window, never a fresh
            # read: a re-read here would be a second, unpaired observation of
            # the queue.
            queued_prompts=queue_snapshot,
            dismissed_source_links=_merge_dismissed_line(slot, meta),
            # Held /note lines: a MERGE writer, so it unions with the on-disk
            # hold and never shrinks it. A live-state mirror here could race a
            # turn-end flush that just delivered rows into a window this
            # empty-window save does not write — clearing the only durable copy
            # of a note whose row is unsaved. Retirement belongs to the full
            # save's paired snapshot alone.
            deferred_notes=union_deferred_notes(
                meta.get("deferred_notes"),
                serialize_deferred_notes(slot._deferred_notes[:]),
            ),
            # Staged mute value: persist the endpoint's NEW value here while the
            # live ``slot.mutes_opened`` still holds the committed value, so a
            # newborn's empty-window save reaches disk with the new value even
            # though the live flag has not flipped yet (the round-1 F1 gap, where
            # this branch ignored the staged value and wrote the stale live flag).
            mutes_opened=mutes_opened_override,
        )
        merged_fields.update(cp._metadata_codec.encode(slot, merge=True, folds=folds))
        if queue_deferred_under_lock is not None and queue_deferred_under_lock(meta):
            # A merge never deletes a key, so dropping it keeps the on-disk
            # queue, and the witness below then has nothing to credit.
            merged_fields.pop("queued_prompts", None)
        return True

    def _after_commit_under_lock() -> None:
        _record_pending_memory_mode(pending_mode_target, merged_fields["memory_mode"])
        # The queued prompts this merge committed are now durable, so the
        # flush's drift check must stop reporting them as owed. Set inside the
        # lock, like the full save's witness: a writer that commits a newer
        # queue after this lock is released must not have its witness
        # overwritten by this merge's older value. Same routing guard as the
        # full save's witnesses: a slot rebound while the merge was in flight
        # would otherwise be credited for a value written to the OLD transcript.
        if slot_history_key(slot) != history_key:
            return
        merged_queue = merged_fields.get("queued_prompts")
        if isinstance(merged_queue, list):
            slot._queue_persisted_sig = queue_persist_signature(merged_queue)
        # Flip the live mute flag HERE, under the transcript lock this write
        # holds, after the merge committed -- a concurrent flush cannot
        # interleave because it needs the same lock, so it never serializes the
        # still-prior flag over this committed value. Guarded by the same
        # rebind check above: a slot rebound while the merge was in flight must
        # not have its live flag flipped for a value written to the OLD transcript.
        if after_commit_under_lock is not None:
            after_commit_under_lock()

    applied = conv_log.update_metadata_if(
        history_key,
        merged_fields,
        _refresh_under_lock,
        after_commit_under_lock=_after_commit_under_lock,
    )
    if not applied and not guard_state["ran"]:
        # `update_metadata_if` fails CLOSED on an unreadable record
        # WITHOUT invoking the guard -- that is a failed write, not the
        # by-design skip for a line-less tab (where the guard runs and
        # sees an empty record). Returning True here would report a
        # merge that never happened as durable: a close would remove
        # the tab while the on-disk line stays open-shaped and the
        # next restart resurrects it. Raise instead, matching the save
        # contract: best-effort callers log + mark the slot dirty, and
        # archival callers (close, best_effort=False) roll back and
        # keep the slot.
        raise OSError(f"empty-window metadata merge skipped: record unreadable for {history_key}")
    return guard_state["refusal"]


def _full_dismissed_line(slot: _ChatSlot, existing_meta: dict) -> list[str] | None:
    """The ``dismissed_source_links`` a full save writes, or ``None`` to omit it.

    The full save rebuilds the line from scratch, so an omitted key means "no
    dismissals" on restore; a dismissal is permanent (there is no unlink-undo),
    so the set only ever grows within a session.

    A slot bound to a transcript whose dismissed set could NOT be read
    (``_dismissed_hydrated is False``) holds an EMPTY in-memory set that does NOT
    reflect disk, so serializing it would erase the real tombstones: the on-disk
    line is carried forward verbatim instead until a readable hydration replaces
    it. Likewise, while an unlink transaction holds an uncommitted TENTATIVE
    dismissal (``_dismissed_txn_depth`` > 0), the in-memory set is ahead of the
    authoritative guarded write and may be rolled back; carrying the on-disk line
    forward keeps this provisional flush from persisting a tombstone that a
    failed DELETE would then be unable to take back.
    """
    if not slot._dismissed_hydrated or slot._dismissed_txn_depth > 0:
        carry = existing_meta.get("dismissed_source_links")
        if isinstance(carry, list) and carry:
            return _capped_dismissed_line(carry)
        return None
    disk_prev = existing_meta.get("dismissed_source_links")
    # UNION the in-memory set with the on-disk line rather than replacing disk
    # with memory. ``_dismissed_hydrated`` means the set was readable AT BIND,
    # but a stale off-loop prefetch (workflow/cron fallback) can bind a set that
    # predates a concurrent unlink's committed tombstone; a bare replacement
    # would then SHRINK the on-disk set and erase that tombstone. A union can
    # only ADD, whichever side is momentarily stale, while still persisting a
    # genuinely new in-memory dismissal. The two go in as ONE chained iterable
    # so no unbounded merged set is built before the cap.
    if isinstance(disk_prev, list):
        return _capped_dismissed_line(chain(slot._dismissed_source_links, disk_prev))
    if slot._dismissed_source_links:
        return _capped_dismissed_line(slot._dismissed_source_links)
    return None


def build_full_line(
    slot: _ChatSlot,
    existing_meta: dict,
    *,
    live_session: str,
    closed: bool,
    closed_at: float | None,
    window: list[dict],
    queue_snapshot: list[dict],
    queue_candidates: int,
    rewrite: bool,
    rows_only: bool,
    mutes_opened_override: bool | None = None,
) -> tuple[dict, str, list[dict], set[str], str | None]:
    """The line a full save writes: ``(line, mode, queue, retired note ids, tab_id)``.

    Folded from slot state and *existing_meta*, the on-disk line, both read under
    the transcript lock. The retired note ids are consumed only once the write that
    carries this line commits.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    # Epoch stamp of WHEN the tab was closed. The channel-slot reconciler
    # compares channel-side activity against this to decide whether a close
    # still stands: a Discord/Slack message arriving after the close
    # re-surfaces the conversation, while a conversation that stayed idle stays
    # closed. Prefer the caller-supplied instant (captured by note_slot_closed
    # at the moment the user acted): this save runs only after the close
    # handler's awaits (task cancellation, patient lock acquire), and stamping
    # save time here would make channel activity that landed during that
    # teardown window compare as OLDER than the close — hiding a conversation
    # the reactivation rule should surface. The save-time fallback covers
    # callers with no user gesture to anchor to.
    if closed:
        closed_at = closed_at if closed_at is not None else time.time()
    # Read INSIDE the lock, like every other slot field on this line: a
    # mode switch that committed while this save waited must land here,
    # or the restart re-reads the looser value. Folded with the mode the
    # line already carries (``existing_meta``, read under this same lock)
    # because the line is a ratchet: the rows a restricted slot committed
    # outlive that slot, and a persistent slot recreated on the freed key
    # must not relabel them by rebuilding the line from its own state.
    _mode = stricter_memory_mode(
        line_memory_mode(existing_meta), retained_memory_mode(slot, live_session)
    )
    if slot.reasoning_effort:
        cp._remember_reasoning_effort_for_restore(slot.reasoning_effort)
    dismissed = _full_dismissed_line(slot, existing_meta)
    # Ranking signal for every "recent sessions" list: the instant of
    # this session's newest HUMAN turn. Derived here because this save
    # already rebuilds the metadata line and already holds the window,
    # so it costs no extra I/O and reads no transcript.
    #
    # MONOTONIC, via the later of the derived value and the one on disk.
    # The window is BOUNDED, so a save can legitimately see no user row
    # (the last one has scrolled out of it) or an older one than the
    # stamp already stored (a rewind, a fork). Folding the stored value
    # in means a save can only move this forward, which is what makes
    # the field safe to leave unowned: nothing here can retract a turn
    # that really happened.
    _stored_human_ts = existing_meta.get(_META_LAST_USER_AT)
    _latest_human_ts = _latest_stamp(
        _stored_human_ts if isinstance(_stored_human_ts, str) else "",
        _newest_human_turn_ts(window),
    )
    # Durable copy of the held /note lines. OWNED (in SLOT_OWNED_META_KEYS),
    # so this rebuild decides the key's whole value — and retirement is
    # ROW-DERIVED: an entry is retired exactly when the window THIS save
    # writes contains its delivered row (``meta.noteId``, stamped by the
    # flush) or its id was recorded as dropped at the rebind seam.
    # Everything else — the on-disk hold and the live hold, both read HERE,
    # under the same history lock the merge writers commit under — is kept,
    # so a /note persist that won the lock during this save's patient acquire
    # is unioned rather than overwritten, and a flush interleaving anywhere
    # around the window snapshot leaves the entry to the save that actually
    # writes its row. Row and retirement land in one atomic file replace; a
    # crash on either side re-delivers rather than loses.
    window_note_ids: set[str] = set()
    for row in window:
        row_meta = row.get("meta")
        if isinstance(row_meta, dict):
            row_note_id = row_meta.get("noteId")
            if isinstance(row_note_id, str) and row_note_id:
                window_note_ids.add(row_note_id)
    dropped_note_ids = set(slot._dropped_note_ids)
    surviving_hold = [
        entry
        for entry in union_deferred_notes(
            existing_meta.get("deferred_notes"),
            serialize_deferred_notes(slot._deferred_notes[:]),
        )
        if entry.get("id") not in window_note_ids and entry.get("id") not in dropped_note_ids
    ]
    # Durable copy of the queued user prompts. OWNED, and the whole
    # value is decided here, so an emptied queue is cleared by absence.
    #
    # Correctness rests on ONE property of this save: the message window
    # and this queue value are ONE paired observation (see
    # ``write_guards.paired_window_snapshot``), and they land in a single atomic file replace. The
    # drain removes an entry from ``_queue`` and appends its user row in
    # the same event-loop step, so a pair taken with no drain between its
    # two halves shows them agreeing — either the entry is queued and its
    # row is not there, or the row is there and the entry is gone. A
    # crash between the drain and this save loses the row too, so the
    # replayed entry is a prompt the transcript never recorded, never a
    # second copy of one it did.
    #
    # Restored entries are handed back as QUEUE CARDS, not dispatched:
    # nothing drains an idle slot on boot. That is deliberate — an
    # indeterminate send must never be auto-resent (sendTurn.ts), and a
    # prompt whose turn may have run un-persisted is exactly that case.
    _durable_queue = queue_snapshot
    # Both halves of this subtraction come from the SAME queue read (see
    # ``durable_queue_view``). Counting the live queue here instead would
    # report a prompt that merely arrived after the snapshot as one the
    # bounds refused, which is a different fact than the one measured.
    _queue_shortfall = queue_candidates - len(_durable_queue)
    if _queue_shortfall > 0:
        # Named here, once per save, so an over-cap queue is a visible
        # operational fact rather than a silent omission. The send is not
        # refused for it: see ``durable_queue_view``.
        logger.warning(
            "Slot %s: %d queued prompt(s) exceed the durable queue "
            "bounds and are not persisted; %d carried",
            slot.key,
            _queue_shortfall,
            len(_durable_queue),
        )
    # Sticky, and carried forward from disk rather than only from the slot: a
    # restore path that failed to set the in-memory flag would otherwise ERASE
    # the marker on the next save and the conversation would be re-filed.
    channel_folder_filed = bool(
        slot._channel_folder_filed or existing_meta.get("channel_folder_filed")
    )
    tab_id = getattr(slot, "_tab_id", None) or existing_meta.get("tab_id")
    # ``rewrite`` is the structural signal for "this save EDITS the
    # conversation": the regenerate / rewind / fork paths pass an explicit
    # window snapshot (or leave ``_pending_rewrite`` set), while a steady
    # flush re-serializes the same window it already persisted.
    #
    # An edit swaps the live window's tail for content no consolidation
    # turn has read, so it advances the rotation generation — the
    # session's content-identity counter. That single write covers both
    # halves of the invariant that a consolidation marker and its retry
    # budget are bound to the content they measured:
    #
    # * An attempt already IN FLIGHT snapshotted the pre-edit generation,
    #   so its ``mark_consolidated`` write is rejected as stale
    #   (``ConversationLog.mark_consolidated``) instead of marking the
    #   REPLACEMENT tail consolidated without ever extracting it. A
    #   regenerate lands at the same message count, the same generation
    #   and the same marker, so nothing else about the save distinguishes
    #   it and the completion write would otherwise apply.
    # * A charged (or capped) budget stamped against the pre-edit
    #   generation stops describing the current span, so the replacement
    #   content earns a fresh budget rather than inheriting an exhausted
    #   one (``ConversationLog._attempts_describe_current_span``).
    #
    # This is the same release a rotation gets, and deliberately the same
    # in both directions: the armed backoff deadline survives, so a user
    # repeatedly regenerating a reply cannot re-bill a failing
    # consolidation turn on each gesture.
    rotation = int(existing_meta.get("rotation_generation", 0) or 0) + 1 if rewrite else None
    # Preserve history-layer-owned metadata this dashboard save does NOT
    # manage: the save is authoritative only for the slot fields it writes
    # (SLOT_OWNED_META_KEYS), where an absent field means "cleared"; every
    # other key is another layer's durable state, carried below after the
    # slot fields so an inherited value can never shadow the slot's own state.
    meta_line = cp._metadata_codec.encode(
        slot,
        folds=cp._metadata_codec.SaveFolds(
            memory_mode=_mode,
            created_at=existing_meta.get("created_at") or slot.created_at,
            last_consolidated=existing_meta.get("last_consolidated", 0),
            closed=closed,
            closed_at=closed_at,
            dismissed_source_links=dismissed,
            deferred_notes=surviving_hold,
            queued_prompts=_durable_queue,
            last_user_at=_latest_human_ts,
            channel_folder_filed=channel_folder_filed,
            tab_id=tab_id,
            rotation_generation=rotation,
            # Staged mute value (see merge_empty_window): the full save honors
            # the same override, so the committed value reaches disk whether a
            # session has a window or not.
            mutes_opened=mutes_opened_override,
        ),
    )
    # The drop records this write retires are CONSUMED only after the
    # save's atomic_write commits (and never on the rows-only path,
    # which defers the key to the on-disk value): a dropped note's row
    # never exists, so the recorded id is its ONLY retirement path —
    # consuming it before the write commits would leak the entry into
    # the durable hold forever if the write fails.
    retired_drop_ids = dropped_note_ids if not rows_only else set()
    # ``rows_only`` DEFERS to the line on disk, so it owes evidence that the
    # line is somebody ELSE's. ``tab_id`` is that evidence and the only
    # per-writer mark the line carries: it is minted per slot OBJECT
    # (``get_or_create_slot`` assigns a fresh uuid; a rehydrate adopts the
    # file's), and every save stamps the writer's own onto the line. A line
    # still carrying THIS slot's id was published by this slot and describes
    # nothing that needs protecting, so the ordinary rebuild must run —
    # metadata edits are acknowledged to the user the instant they land in
    # memory (``_dirty``, persisted by a later flush), and the slot a
    # rows-only write carries has been popped, so deferring here would drop
    # a title, folder, tag set or pin the user already saw applied with
    # nothing left to retry it.
    #
    # Unprovable ownership defers, because the two errors cost differently.
    # Deferring this slot's own edit loses fields that were never committed;
    # rebuilding over a live holder's committed line reverts fields it
    # already published, and for a replacement nobody types in again nothing
    # rewrites them, so that loss is permanent.
    line_is_this_slots = cp._line_is_this_slots(slot, existing_meta)
    # Whether the line this save writes carries THIS slot's queue. A
    # deferring rows-only write carries the live holder's value instead,
    # so the popped slot must not be credited with having persisted its
    # own queue — its entries stay owed, which is the conservative side.
    # Decided at the save's stale-queue guard, which needs the same answer
    # to know whether this save is deciding the queue at all.
    if rows_only and existing_meta and not line_is_this_slots:
        # A rows-only write does not own the slot-owned fields: the line
        # describes whichever OTHER live slot published it, and this one is
        # only here to get its messages down. Drop the rebuild for every
        # field outside the file-identity subset and let the carry below
        # restore the on-disk value verbatim, so a title, folder, tag set or
        # pin another holder acknowledged is not reverted by a write that was
        # never about it. ``closed``/``closed_at`` are deferred with the
        # rest: on this line they are the other holder's own dismissal, and
        # an open-shaped write that erased them would resurface a tab the
        # user put away with that holder already popped. Gated on an existing
        # line because with none there is no other writer to defer to and
        # the slot's own state is all there is — and that is the branch below,
        # where the open-shaped write still clears a stale ``closed``.
        for meta_key in ROWS_ONLY_DEFERRED_META_KEYS:
            meta_line.pop(meta_key, None)
        carry_unowned_metadata(meta_line, existing_meta, ROWS_ONLY_OWNED_META_KEYS)
        # ``memory_mode`` is deferred with the rest, but it is the line's
        # privacy contract -- every reader that learns from the file gates
        # on it -- and the contract is a RATCHET that any writer may
        # tighten. A restricted original handing its unsaved tail to a
        # PERSISTENT same-key replacement (one that published its own line
        # before the original ever committed one) must not file private
        # rows under a line that says persistent, and it must not drop
        # them either: the rows are the reply the user was watching, and
        # a refused write here has no retry path (the slot is popped).
        # So the carried mode is folded with the retained one and the
        # line is tightened to the stricter value, exactly as the
        # replacement's own save would have been ratcheted had the
        # original's line landed first. The two race orders then reach
        # the same file: private rows and the replacement's rows under
        # one restricted line, the outcome the turn-start binder already
        # produces for a persistent slot on a restricted record. A
        # restricted line names no store (see the full save above), so a
        # carried ``memory_store`` is dropped with the loosening. Only a
        # STRICTER source changes anything: ``_mode`` already folds the
        # line's value in, so it differs from the line exactly when the
        # source is stricter; a persistent original over a restricted
        # replacement's line keeps the line's value untouched.
        line_mode = line_memory_mode(existing_meta)
        if _mode != line_mode:
            logger.info(
                "Slot %s save tightened another holder's %s line to %s: %d "
                "unsaved %s row(s) land under the stricter mode",
                slot.key,
                line_mode,
                _mode,
                len(window),
                _mode,
            )
            meta_line["memory_mode"] = _mode
            meta_line.pop("memory_store", None)
        # The carried durable execution record must never read looser
        # than the line it sits on; fold the (possibly just ratcheted)
        # mode into it. A no-op on a record already at least as strict.
        _tighten_carried_execution(meta_line, _mode)
    else:
        carry_unowned_metadata(meta_line, existing_meta, SLOT_OWNED_META_KEYS)
        # The durable execution record is carried, not owned, and it
        # holds a ``memory_mode`` of its own. The line's mode was just
        # ratcheted above; the record must never read looser than it.
        _tighten_carried_execution(meta_line, _mode)
    return meta_line, _mode, _durable_queue, retired_drop_ids, tab_id
