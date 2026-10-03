"""The metadata line a dashboard save writes, folded from the slot and the disk.

The first line of a session file carries the slot-owned fields (title, folder,
tags, model, mode and the rest), fields other layers own (carried through, a
durable execution record only ever tightened to the line's mode), and the privacy
contract every learning reader gates on (``memory_mode``). This module owns that fold: the memory-mode ratchet and its
worker-to-loop witness, the monotonic newest-human-turn stamp, and the bounded
dismissed source-link line. ``_save_slot_to_history`` decides when it runs.

New slot-owned metadata fields belong here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from itertools import chain
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.slot_buffers import (
    persistable_deferred_notes,
    serialize_deferred_notes,
    union_deferred_notes,
)
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
from kiro_crew.memory_stores import named_store_or_empty

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
) -> None:
    """Commit a forced or closing save of a message-less slot as a metadata merge.

    Raises ``OSError`` when the record is unreadable, so a close or an
    acknowledged edit is never reported durable on a merge that did not happen.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    # A FORCED (or closing) save of a message-less slot is a metadata
    # mutation (folder filing/unfiling, a tag assignment, a pin, a
    # pinned title, a mode switch, a close) -- the full save below has no window to
    # write, but the mutation still has to reach disk. `session_create`
    # persists `folder_id` at birth, so an empty newborn HAS a metadata
    # line and any acknowledged metadata change before its first message must
    # overwrite that line, or a restart resurrects the birth state the
    # user already changed. The merge carries every slot-owned field a
    # force/closed save is responsible for -- not just `folder_id`:
    # the tag routes, the pin route, the recreate PATCH (folder or
    # pinned title) and the close path all persist ONLY through this
    # save, so a folder-only merge would silently drop their
    # acknowledged writes on restart. Merged ONLY into an existing
    # line: a slot with no line at all does not survive a restart, so
    # there is nothing to reconcile, and materializing files for every
    # empty tab here would create transcripts nothing else expects.
    # The existence guard runs INSIDE the same cross-process lock as
    # the merge (`update_metadata_if`): the plain update is an upsert,
    # so a checked-then-written pair would let a permanent deletion
    # land between the two and be resurrected as a fresh file.
    # Clearable fields are written even when empty -- the merge cannot
    # delete a key, and rehydrate treats a falsy value as cleared
    # (unfiled / untagged / unpinned / untitled / default mode). Fails
    # closed on an unreadable record, per `update_metadata_if`'s own
    # contract.
    def _fresh_fields(meta: dict) -> dict:
        # Mirrors the FULL save's slot-owned enumeration (the
        # ``meta_line`` construction below), so a forced save of an
        # empty slot persists exactly what a forced save of a
        # non-empty slot would persist for the metadata line -- the
        # invariant that keeps this branch from silently dropping
        # whichever acknowledged mutation a route happens to persist
        # through it (folder, tags, pin, title, mode, project,
        # artifact binding, ...). Two write classes, matching
        # rehydrate's semantics:
        # - CLEARABLE fields are written even when empty (the merge
        #   cannot delete a key; rehydrate treats a falsy value as
        #   cleared: unfiled / untagged / unpinned / untitled /
        #   default mode / unbound artifact / uncolored).
        # - IDENTITY and MONOTONIC fields are written only when
        #   truthy, exactly like the full save (origin's fail-closed
        #   sentinel and the once-flags must never be erased by a
        #   writer that has not learned them).
        if slot.reasoning_effort:
            cp._remember_reasoning_effort_for_restore(slot.reasoning_effort)
        # ``meta`` is the line as the guard read it under the lock, so
        # the ratchet folds the CURRENT on-disk mode, not a snapshot.
        _mode = stricter_memory_mode(
            line_memory_mode(meta), retained_memory_mode(slot, live_session)
        )
        fields: dict = {
            "folder_id": slot.folder_id or "",
            "tags": list(slot.tags),
            "pinned": bool(slot.pinned),
            "mode": slot.mode or "",
            "artifact": slot._artifact or "",
            "reasoning_effort": slot.reasoning_effort or "",
            "color_index": slot.color_index,
            "color_hex": slot.color_hex or "",
            "color_theme": slot.color_theme or "",
            "memory_mode": _mode,
            "model": slot.model,
            # CLEARABLE: the queued prompts a restore hands back. Written
            # even when empty, so a drain that emptied the queue is not
            # left with the pre-drain set on disk (the merge cannot
            # delete a key, and the restore treats a falsy value as an
            # empty queue). The value is the snapshot taken WITH the
            # window, never a fresh read: a re-read here would be a
            # second, unpaired observation of the queue.
            "queued_prompts": queue_snapshot,
            # None means "follow the global threshold" and is the
            # cleared value (rehydrate reads it with ``is not None``),
            # so the override is CLEARABLE: written even when None,
            # like the other clearable fields above.
            "autocompact_pct": slot.autocompact_pct,
        }
        if slot.title and slot.title != slot.key:
            fields["title"] = slot.title
            # Persist the title's provenance next to it (mirrors the
            # full save): without it rehydration conservatively
            # re-classifies an auto title as "user" and locks the
            # refresh out.
            _origin = getattr(slot, "_title_origin", "")
            if _origin:
                fields["title_origin"] = _origin
            _mark = getattr(slot, "_title_refresh_mark", 0)
            if _mark:
                fields["title_refresh_mark"] = _mark
            # Title-coupled like the two fields above, and written
            # UNCONDITIONALLY: _persist_title is the primary writer but
            # returns False without retry on a transient failure, so a
            # full save must land the CURRENT boolean either direction
            # -- a skipped True loses the turn-one refresh after
            # restart, and a skipped False (flag just cleared by
            # consumption or manual regenerate) re-arms it.
            fields["title_low_signal"] = bool(getattr(slot, "_title_low_signal", False))
        else:
            fields["title"] = ""
        if slot.agent:
            fields["agent"] = slot.agent
        if slot.workspace:
            fields["workspace"] = slot.workspace
        # CLEARABLE, and it has to be: the merge cannot delete a key, so a
        # crew rebound from a silo back to the default store would keep
        # consolidating into the silo it left. The cleared spelling is ""
        # rather than "default" so it reads as falsy everywhere -- the
        # rehydrate mirror and the consolidator's own resolver both treat
        # falsy as "the global store", which is also how a session written
        # before crew stores existed reads.
        # A RESTRICTED line names no store (see the full save below).
        fields["memory_store"] = (
            named_store_or_empty(slot.memory_store) if _mode == "persistent" else ""
        )
        # Clearable like memory_store: a name-only pick after a template
        # pick must not keep advertising the template namespace.
        fields["agent_kind"] = slot.agent_kind
        # Written even when EMPTY: the merge is an upsert that cannot delete a key, so
        # omitting a cleared project leaves the previous directory on disk to be read
        # back as though the clear never happened.
        fields["project"] = slot.project
        if slot._app:
            fields["app"] = slot._app
        if slot._origin:
            fields["origin"] = slot._origin
        if getattr(slot, "_created_by", ""):
            # Creator attribution: the member ownership boundary in
            # session-control authorization reads it, so dropping it here
            # would orphan a member's workers on the next restart.
            fields["created_by"] = slot._created_by
        # `_created_by_sid` is NOT persisted (lineage is process-local; see
        # _ChatSlot._lineage_minted).
        if slot.linked_session_key:
            fields["linked_session_key"] = slot.linked_session_key
        if getattr(slot, "channel_origin", False):
            fields["channel_origin"] = True
        if slot.forked_from is not None:
            fields["forked_from"] = slot.forked_from
            if slot.forked_from_created_at:
                fields["forked_from_created_at"] = slot.forked_from_created_at
        # CLEARABLE: the merge cannot delete a key and rehydrate reads
        # zero as "no local turn outstanding", so the current value is
        # written either way -- a forced save after a turn's teardown
        # must not leave the admitted generation on disk.
        fields["turn_in_flight_generation"] = slot._turn_in_flight_generation
        # Same rule for the opening-row copy: None is the cleared value
        # (rehydrate reads anything but a dict as "no copy").
        fields["turn_in_flight_prompt"] = slot._turn_in_flight_prompt
        if slot.executor == "remote" and slot.instance_id and slot.remote_slot:
            # All three or none, exactly like the full save: a newborn
            # bound to a peer has an EMPTY window until the first relayed
            # row lands, so this merge is the only writer its binding
            # ever sees. Dropping it here means a restart in that window
            # brings the session back as an ordinary local one and the
            # next turn runs on this machine instead of the crew the user
            # picked. The completeness guard keeps the fail-closed
            # invariant: a half-binding is never written, so rehydration
            # never has to repair one.
            fields["executor"] = "remote"
            fields["instance_id"] = slot.instance_id
            fields["remote_slot"] = slot.remote_slot
            if getattr(slot, "_relay_in_flight", False):
                # Only ever written while a turn is mid-flight; the relay
                # clears it when the turn ends, so a persisted True means
                # "crashed mid-turn" on reload. Nested under the binding
                # because it is meaningless without one.
                fields["relay_in_flight"] = True
        if getattr(slot, "_tab_id", None):
            fields["tab_id"] = slot._tab_id
        if getattr(slot, "_auto_tagged", False):
            # Once-flag, monotonic (see the full save): written when
            # set, never cleared.
            fields["auto_tagged"] = True
        if getattr(slot, "_human_seen", False):
            fields["human_seen"] = True
        if slot._channel_folder_filed:
            # Sticky like the full save; the disk-carry half is
            # inherent here since a merge never deletes a key.
            fields["channel_folder_filed"] = True
        if closed:
            # Without this a closed empty newborn's line stays
            # open-shaped and the next restart resurrects a tab the
            # user dismissed.
            fields["closed"] = True
            fields["closed_at"] = closed_at if closed_at is not None else time.time()
        return fields

    # The slot fields are read INSIDE the guard, which
    # `update_metadata_if` evaluates under the cross-process lock at
    # write time -- exactly the contract that method exists for ("the
    # decision is re-made here rather than trusted from before the
    # lock"). A dict snapshotted before the lock could commit out of
    # order: a tag save that snapshotted `pinned=False` before a
    # concurrent pin request committed `pinned=True` would land its
    # stale aggregate second and silently revert the acknowledged pin.
    # The full save has the same shape -- it builds its metadata line
    # from slot state inside the locked block -- so whichever writer
    # commits last writes the newest slot state.
    merged_fields: dict = {}
    guard_state = {"ran": False}

    def _refresh_under_lock(meta: dict) -> bool:
        guard_state["ran"] = True
        if not meta:
            return False
        merged_fields.clear()
        merged_fields.update(_fresh_fields(meta))
        # Serialized dismissed source-link identities, decided under the
        # lock from the on-disk ``meta``. When the slot's set is HYDRATED
        # (reflects disk) write it (sorted, deterministic; empty = nothing
        # dismissed). When it is UNHYDRATED (bound while the set could not
        # be read) the in-memory empty set does not reflect disk, so carry
        # the on-disk line forward rather than erase the real tombstones.
        # And while an unlink transaction holds an uncommitted TENTATIVE
        # dismissal (``_dismissed_txn_depth`` > 0), carry the on-disk line
        # forward too: the tentative set may be rolled back by a failed
        # guarded write, and this provisional flush must not outlive it.
        if slot._dismissed_hydrated and slot._dismissed_txn_depth == 0:
            # UNION with the on-disk line (available here as ``meta``)
            # rather than replacing: a stale off-loop prefetch can bind a
            # hydrated set that predates a concurrent unlink's committed
            # tombstone, and a bare replacement would erase it. Dismissals
            # only grow, so the union can only ADD.
            _disk_prev = meta.get("dismissed_source_links")
            if isinstance(_disk_prev, list):
                # Pass the in-memory set and the raw (untrusted) on-disk
                # list as ONE chained iterable: _capped_dismissed_line
                # validates, dedups and bounds during iteration, so no
                # unbounded merged set is built here before the cap.
                merged_fields["dismissed_source_links"] = _capped_dismissed_line(
                    chain(slot._dismissed_source_links, _disk_prev)
                )
            else:
                merged_fields["dismissed_source_links"] = _capped_dismissed_line(
                    slot._dismissed_source_links
                )
        else:
            _carry = meta.get("dismissed_source_links")
            if isinstance(_carry, list) and _carry:
                merged_fields["dismissed_source_links"] = _capped_dismissed_line(_carry)
        # Held /note lines: a MERGE writer, so it unions
        # with the on-disk hold and never shrinks it. A live-state
        # mirror here could race a turn-end flush that just delivered
        # rows into a window this empty-window save does not write —
        # clearing the only durable copy of a note whose row is
        # unsaved. Retirement belongs to the full save's paired
        # snapshot alone.
        merged_fields["deferred_notes"] = union_deferred_notes(
            meta.get("deferred_notes"),
            serialize_deferred_notes(persistable_deferred_notes(slot._deferred_notes[:])),
        )
        return True

    applied = conv_log.update_metadata_if(
        history_key,
        merged_fields,
        _refresh_under_lock,
        after_commit_under_lock=lambda: _record_pending_memory_mode(
            pending_mode_target, merged_fields["memory_mode"]
        ),
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
    if applied and slot_history_key(slot) == history_key:
        # The queued prompts this merge committed are now durable, so
        # the flush's drift check must stop reporting them as owed. Same
        # routing guard as the full save's witnesses: a slot rebound
        # while the merge was in flight would otherwise be credited for
        # a value written to the OLD transcript.
        _merged_queue = merged_fields.get("queued_prompts")
        if isinstance(_merged_queue, list):
            slot._queue_persisted_sig = queue_persist_signature(_merged_queue)


def _stamp_delivered_merge_cards(entries: Iterable[dict], window_note_ids: set[str]) -> list[dict]:
    """Mark held merge cards whose rows commit in the saved window."""
    return [
        (
            {**entry, "delivered": True}
            if isinstance(entry.get("merged_from"), dict) and entry.get("id") in window_note_ids
            else entry
        )
        for entry in entries
    ]


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
) -> tuple[dict, str, list[dict], set[str], str | None]:
    """The line a full save writes: ``(line, mode, queue, retired note ids, tab_id)``.

    Folded from slot state and *existing_meta*, the on-disk line, both read under
    the transcript lock. The retired note ids are consumed only once the write that
    carries this line commits.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    meta_line: dict = {
        "_type": "metadata",
        "created_at": existing_meta.get("created_at") or slot.created_at,
        "last_consolidated": existing_meta.get("last_consolidated", 0),
    }
    # Preserve history-layer-owned metadata this dashboard save does NOT
    # manage. The save is authoritative only for the slot fields it writes
    # (SLOT_OWNED_META_KEYS), where an absent field means "cleared"; every
    # other key is another layer's durable state, and reconstructing the
    # subset deletes it. That is not hypothetical: it erased the rotation
    # generation (re-opening the consolidation race the generation check
    # closed) and then the consolidation retry accounting (resetting the
    # backoff so billed retries resumed). Carrying unowned keys through by
    # default closes the class instead of enumerating one more field to
    # rescue. Applied after the slot fields below so an inherited value can
    # never shadow the slot's own state.
    if closed:
        meta_line["closed"] = True
        # Epoch stamp of WHEN the tab was closed. The channel-slot
        # reconciler compares channel-side activity against this to
        # decide whether a close still stands: a Discord/Slack message
        # arriving after the close re-surfaces the conversation, while
        # a conversation that stayed idle stays closed.
        #
        # Prefer the caller-supplied instant (captured by
        # note_slot_closed at the moment the user acted): this save
        # runs only after the close handler's awaits (task
        # cancellation, patient lock acquire), and stamping save time
        # here would make channel activity that landed during that
        # teardown window compare as OLDER than the close — hiding a
        # conversation the reactivation rule should surface. The
        # save-time fallback covers callers with no user gesture to
        # anchor to (and legacy call sites).
        meta_line["closed_at"] = closed_at if closed_at is not None else time.time()
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
    meta_line["memory_mode"] = _mode
    if slot.title and slot.title != slot.key:
        meta_line["title"] = slot.title
        # Persist the title's provenance next to it (mirrors
        # _persist_title): without this, the canonical full save would
        # strip the field and rehydration would conservatively
        # re-classify an auto title as "user" after restart —
        # permanently locking the background refresh out.
        _origin = getattr(slot, "_title_origin", "")
        if _origin:
            meta_line["title_origin"] = _origin
        _mark = getattr(slot, "_title_refresh_mark", 0)
        if _mark:
            meta_line["title_refresh_mark"] = _mark
        # Title-coupled like the two fields above, and written
        # UNCONDITIONALLY: _persist_title is the primary writer but
        # returns False without retry on a transient failure, so the
        # full save must land the CURRENT boolean either direction -- a
        # skipped True loses the turn-one refresh after restart, and a
        # skipped False (flag just cleared by consumption or manual
        # regenerate) re-arms it.
        meta_line["title_low_signal"] = bool(getattr(slot, "_title_low_signal", False))
    if slot.agent:
        meta_line["agent"] = slot.agent
    meta_line["model"] = slot.model
    if slot.reasoning_effort:
        cp._remember_reasoning_effort_for_restore(slot.reasoning_effort)
        meta_line["reasoning_effort"] = slot.reasoning_effort
    # Unconditional, matching the empty-window merge mirror: None is
    # the cleared "follow the global" value, not an absent field.
    meta_line["autocompact_pct"] = slot.autocompact_pct
    # Serialized dismissed source-link identities. This path rebuilds the
    # metadata line from scratch, so an omitted key means "no dismissals"
    # on restore -- write it only when non-empty (sorted for a
    # deterministic line). A dismissal is permanent (there is no unlink-
    # undo), so the set only ever grows within a session; the empty case
    # is simply a session that has never dismissed a chip.
    #
    # A slot bound to a transcript whose dismissed set could NOT be read
    # (``_dismissed_hydrated is False``) holds an EMPTY in-memory set that
    # does NOT reflect disk, so serializing it would erase the real
    # tombstones. Carry the on-disk line forward verbatim instead until a
    # readable hydration replaces it.
    #
    # Likewise, while an unlink transaction holds an uncommitted TENTATIVE
    # dismissal (``_dismissed_txn_depth`` > 0), the in-memory set is ahead
    # of the authoritative guarded write and may be rolled back. Carrying
    # the on-disk line forward keeps this provisional flush from
    # persisting a tombstone that a failed DELETE would then be unable to
    # take back (the 409-then-restart-hides-the-chip corruption).
    if not slot._dismissed_hydrated or slot._dismissed_txn_depth > 0:
        _carry = existing_meta.get("dismissed_source_links")
        if isinstance(_carry, list) and _carry:
            meta_line["dismissed_source_links"] = _capped_dismissed_line(_carry)
    elif slot._dismissed_source_links or isinstance(
        existing_meta.get("dismissed_source_links"), list
    ):
        # UNION the in-memory set with the existing on-disk line rather
        # than replacing disk with memory. ``_dismissed_hydrated`` means
        # the set was readable AT BIND, but a stale off-loop prefetch
        # (workflow/cron fallback) can bind a set that predates a
        # concurrent unlink's committed tombstone; a bare replacement
        # would then SHRINK the on-disk set and erase that tombstone
        # (chip reappears after restart). Dismissals are permanent and
        # only grow, so a union can only ADD — it can never drop a
        # committed tombstone, whichever side is momentarily stale, while
        # still persisting a genuinely new in-memory dismissal.
        _disk_prev = existing_meta.get("dismissed_source_links")
        if isinstance(_disk_prev, list):
            # Pass the in-memory set and the raw (untrusted) on-disk list
            # as ONE chained iterable: _capped_dismissed_line validates,
            # dedups and bounds during iteration, so no unbounded merged
            # set is built here before the cap.
            meta_line["dismissed_source_links"] = _capped_dismissed_line(
                chain(slot._dismissed_source_links, _disk_prev)
            )
        elif slot._dismissed_source_links:
            meta_line["dismissed_source_links"] = _capped_dismissed_line(
                slot._dismissed_source_links
            )
    if slot.mode:
        meta_line["mode"] = slot.mode
    if slot.workspace and slot.workspace != "default":
        meta_line["workspace"] = slot.workspace
    # A restricted session's line names no memory store. The store name is
    # what ``read_session_execution`` reads as an owner claim when the
    # line carries no execution carrier -- and a restricted session never
    # writes one (its carrier lives in process and is released on close).
    # A store here would make the restart refuse the chat as a legacy
    # member record with no identity. Left out, the restart reads the
    # session as unbound, and the first turn re-selects the member from
    # ``agent`` under the retained mode, exactly as the live session did.
    # ``agent_kind`` stays: it is a display fact, not an owner claim.
    if _mode == "persistent" and (_named := named_store_or_empty(slot.memory_store)):
        meta_line["memory_store"] = _named
    if slot.agent_kind:
        meta_line["agent_kind"] = slot.agent_kind
    if slot.project:
        meta_line["project"] = slot.project
    # Remote-execution binding. All three are written together or not at
    # all: a half-restored binding (executor="remote" with no peer slot)
    # is the fail-closed refusal case, so persisting the marker without
    # its target would resurrect a session that can never run. Written
    # only when the whole binding is present, and read back the same way.
    if slot.executor == "remote" and slot.instance_id and slot.remote_slot:
        meta_line["executor"] = "remote"
        meta_line["instance_id"] = slot.instance_id
        meta_line["remote_slot"] = slot.remote_slot
        if getattr(slot, "_relay_in_flight", False):
            # See the merge-save site: written only while a turn is
            # in-flight, so a True read back on reload is the crash signal
            # that triggers the interrupted-turn row.
            meta_line["relay_in_flight"] = True
    if slot._turn_in_flight_generation > 0:
        # Written while a local turn is between admission and teardown
        # and omitted otherwise; ``SLOT_OWNED_META_KEYS`` makes that
        # omission the durable clear.
        meta_line["turn_in_flight_generation"] = slot._turn_in_flight_generation
        if slot._turn_in_flight_prompt is not None:
            meta_line["turn_in_flight_prompt"] = slot._turn_in_flight_prompt
    if slot.folder_id:
        meta_line["folder_id"] = slot.folder_id
    if slot._channel_folder_filed or existing_meta.get("channel_folder_filed"):
        # Sticky, and carried forward from disk rather than only from the
        # slot: this function rebuilds the metadata line from scratch, so
        # a restore path that failed to set the in-memory flag would
        # otherwise ERASE the marker on the next save and the
        # conversation would be re-filed. Preserving the on-disk value
        # makes that whole class of omission harmless — same reason
        # rotation_generation is carried forward above.
        meta_line["channel_folder_filed"] = True
    if slot._app:
        meta_line["app"] = slot._app
    # Slot ORIGIN (user / app / cron) must round-trip with ``app``:
    # the rehydrate paths restore ``origin=meta.get("origin", "")`` and an
    # untagged restore falls back to the fail-closed empty sentinel. Without
    # this write every slot would come back unattributed after a restart —
    # ``slots:user`` subscribers would stop seeing user slots, and a cron
    # slot would lose the CRON tag that keeps it out of ``slots:user``.
    if slot._origin:
        meta_line["origin"] = slot._origin
    if getattr(slot, "_created_by", ""):
        # Creator attribution — read by the member ownership boundary in
        # session-control authorization; see the partial-save mirror above.
        meta_line["created_by"] = slot._created_by
    # `_created_by_sid` is NOT persisted -- see the partial-save mirror above.
    # Artifact companion binding — persisted so a bound
    # session restored after a gateway restart (or resumed from the
    # History page) comes back as the artifact's active bound session.
    if slot._artifact:
        meta_line["artifact"] = slot._artifact
    if slot.pinned:
        meta_line["pinned"] = True
    if slot.color_index is not None:
        meta_line["color_index"] = slot.color_index
    if slot.color_hex:
        meta_line["color_hex"] = slot.color_hex
    if slot.color_theme:
        meta_line["color_theme"] = slot.color_theme
    if slot.tags:
        meta_line["tags"] = list(slot.tags)
    if getattr(slot, "_auto_tagged", False):
        # Once-flag for project auto-tagging: without it a restart
        # re-runs maybe_auto_tag and silently re-adds a tag the user
        # removed (see chat_auto_tag.maybe_auto_tag).
        meta_line["auto_tagged"] = True
    if getattr(slot, "_human_seen", False):
        # Once-flag for attendance (state._ChatSlot.unattended). Without
        # it a restart drops an app-owned tab a person is working in from
        # the 2h approval window to the 180s deny-fast — a gateway
        # restart happens on every upgrade and is not evidence the person
        # left. Monotonic like auto_tagged above, so it is written when
        # set and never cleared; both are therefore absent from
        # SLOT_OWNED_META_KEYS and survive via carry_unowned_metadata
        # even on a save by a slot that has not learned the flag yet.
        meta_line["human_seen"] = True
    # Durable copy of the held /note lines. OWNED
    # (in SLOT_OWNED_META_KEYS), so this rebuild decides the key's
    # whole value. Plain notes retire with their committed row. Merge
    # cards retire only when their context drain records the id. Any
    # note dropped at a rebind seam also retires by its recorded id. Everything else —
    # the on-disk hold and the live hold, both read HERE, under the
    # same history lock the merge writers commit under — is kept, so
    # a /note persist that won the lock during this save's patient
    # acquire is unioned rather than overwritten, and a flush
    # interleaving anywhere around the window snapshot leaves the
    # entry to the save that actually writes its row. Row and
    # retirement land in one atomic file replace; a crash on either
    # side re-delivers rather than loses.
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
    if _latest_human_ts:
        meta_line[_META_LAST_USER_AT] = _latest_human_ts
    window_note_ids: set[str] = set()
    for row in window:
        row_meta = row.get("meta")
        if isinstance(row_meta, dict):
            row_note_id = row_meta.get("noteId")
            if isinstance(row_note_id, str) and row_note_id:
                window_note_ids.add(row_note_id)
    dropped_note_ids = set(slot._dropped_note_ids)
    surviving_hold = []
    for entry in _stamp_delivered_merge_cards(
        union_deferred_notes(
            existing_meta.get("deferred_notes"),
            serialize_deferred_notes(persistable_deferred_notes(slot._deferred_notes[:])),
        ),
        window_note_ids,
    ):
        entry_id = entry.get("id")
        if entry_id in dropped_note_ids:
            continue
        if isinstance(entry.get("merged_from"), dict):
            surviving_hold.append(entry)
        elif entry_id not in window_note_ids:
            surviving_hold.append(entry)
    if surviving_hold:
        meta_line["deferred_notes"] = surviving_hold
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
    if _durable_queue:
        meta_line["queued_prompts"] = _durable_queue
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
    # The drop records this write retires are CONSUMED only after the
    # save's atomic_write commits (and never on the rows-only path,
    # which defers the key to the on-disk value): a dropped note's row
    # never exists, so the recorded id is its ONLY retirement path —
    # consuming it before the write commits would leak the entry into
    # the durable hold forever if the write fails.
    retired_drop_ids = dropped_note_ids if not rows_only else set()
    if slot.forked_from is not None:
        meta_line["forked_from"] = slot.forked_from
        if slot.forked_from_created_at:
            meta_line["forked_from_created_at"] = slot.forked_from_created_at
    if slot.linked_session_key:
        # The slot's conversation lives on another session (a channel
        # thread, a cron job). Nothing recreates that binding on
        # restart for a channel slot — no injection re-fires — so
        # without persisting it the slot rehydrates unbound and
        # silently reverts to a dashboard-only copy of the thread.
        meta_line["linked_session_key"] = slot.linked_session_key
    if getattr(slot, "channel_origin", False):
        # Durable provenance. Without it the restore has only the slot
        # name to go on, and a name is not evidence -- persisting the
        # flag is what lets a later boot know this tab was adopted from
        # a channel conversation rather than merely named like one.
        meta_line["channel_origin"] = True
    tab_id = getattr(slot, "_tab_id", None) or existing_meta.get("tab_id")
    if tab_id:
        meta_line["tab_id"] = tab_id
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
    if rewrite:
        meta_line["rotation_generation"] = int(existing_meta.get("rotation_generation", 0) or 0) + 1
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
        carried_hold = meta_line.get("deferred_notes")
        if isinstance(carried_hold, list):
            # This mark commits atomically with the card row, so restore
            # needs no archive read to know the row was delivered.
            meta_line["deferred_notes"] = _stamp_delivered_merge_cards(
                carried_hold, window_note_ids
            )
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
