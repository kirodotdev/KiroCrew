"""Live delivery and deferred-context buffers for dashboard chat slots."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew.jsonl_util import bounded_records
from kiro_crew.sel import sel

# Bounds the visible lines a caller can park on one in-flight turn. Matches the
# per-source context cap so neither half of /note outlives the other by much.
# Lives here (not chat_handlers) so the persistence restore path can enforce the
# same cap on notes read back from disk without importing the handler module.
MAX_DEFERRED_NOTES = 10

# Per-string bound on a DEFERRED note's content, enforced AT THE ENQUEUE
# BOUNDARY (the /note handler rejects an over-bound deferred note before any
# 200 is issued). The durable copy is then always written VERBATIM — never
# truncated — because the 200 promises delivery of the accepted content, and a
# restart that replays altered (elided) content breaks that promise as surely
# as dropping the note. The bound is what keeps the metadata line small (it is
# read and rewritten whole under the cross-process history lock on every /note
# POST): 2x the bound per note, at most 2x MAX_DEFERRED_NOTES entries.
MAX_DEFERRED_NOTE_CHARS = 4000

# Hard ceiling on the durable hold's entry count: live notes (<= the cap) plus
# entries retained for delivered-but-unsaved rows (<= the cap under normal
# save cadence). Exceeding it means saves have not landed for multiple full
# turn cycles; the enqueue path REFUSES new holds at that point rather than
# evicting a retained entry, because every retained entry is the only durable
# copy of a 200-acknowledged note.
_MAX_DURABLE_HOLD_ENTRIES = 2 * MAX_DEFERRED_NOTES


class NoteEvidence(NamedTuple):
    """Positive evidence about the acknowledged note, resolved UNDER the lock.

    The /note failure branches answer 200 only on positive evidence: the note
    observably exists somewhere an owner will deliver or
    replay it. ``durable`` — a durable hold entry carries the note's id (on
    disk already, or in the merge this write committed). ``committed`` — a
    delivered row stamped with the id is in the COMMITTED transcript. Both
    are observations made inside :func:`persist_deferred_notes_sync`'s locked
    guard, where they are authoritative; the handler adds the third clause (a
    delivered row in the slot's LIVE message list) itself, because that one
    is in-memory and needs no lock. Absence of all three means the note has
    no owner — the branch's refusal is then the honest answer, never a 200
    inferred from a negative signal, which would misread a rebind-dropped note
    as delivered.
    """

    durable: bool
    committed: bool


_NO_EVIDENCE = NoteEvidence(durable=False, committed=False)


class DeferredHoldOutcome(NamedTuple):
    """Result of :func:`persist_deferred_notes_sync`.

    ``written`` — the merge landed in an existing metadata line. ``evidence``
    is the locked resolver's verdict on the acknowledged note; on a written
    outcome it reflects the merge that was committed, so ``written and not
    evidence.durable`` is meaningful: the write landed but carries no
    representation of THIS note (a flush dropped it at the rebind seam while
    the slot's history key still matched — the channel-origin twin).
    """

    written: bool
    evidence: NoteEvidence


class DeferredHoldFull(RuntimeError):
    """The durable hold cannot admit another entry without evicting one.

    Raised by :func:`persist_deferred_notes_sync` instead of dropping a
    retained entry — a retained entry is the only durable copy of an already
    acknowledged note. The /note handler maps this to the same 429 the live
    cap uses: the slot has too many undelivered/unsaved notes right now.
    Carries the locked resolver's :class:`NoteEvidence` so the handler reads
    the note's state instead of re-probing outside the lock.
    """

    def __init__(self, message: str, evidence: NoteEvidence = _NO_EVIDENCE) -> None:
        super().__init__(message)
        self.evidence = evidence


class DeferredHoldRebound(RuntimeError):
    """The slot was rebound to another session while the hold was persisting.

    Raised by :func:`persist_deferred_notes_sync` when the slot's history key
    no longer matches the one the caller authorized at enqueue — a cron or
    workflow injection can claim an unbound slot's ``linked_session_key`` with
    no running gate, and committing the merge then would write app-authorized
    note content into a FOREIGN transcript's metadata. The /note handler
    discards the note and refuses the request instead. Carries the locked
    resolver's :class:`NoteEvidence` (resolved against the AUTHORIZED
    transcript) so the handler reads the note's state instead of re-probing.
    """

    def __init__(self, message: str, evidence: NoteEvidence = _NO_EVIDENCE) -> None:
        super().__init__(message)
        self.evidence = evidence


def serialize_deferred_notes(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """JSON-safe VERBATIM copies of held /note entries for the metadata line.

    Copies each note (and its embedded context dict) so a later in-memory
    mutation cannot alias into a dict a metadata writer is holding. Content is
    never truncated: the enqueue boundary already bounds a deferred note at
    :data:`MAX_DEFERRED_NOTE_CHARS`, and the durable copy must replay exactly
    what the 200 accepted. The note's ``id`` rides along: it is what lets the
    merge writers tell "this disk entry is still held" from "this disk entry
    was already delivered or dropped in memory".
    """
    out: list[dict[str, Any]] = []
    for note in notes:
        content = note.get("content", "")
        entry: dict[str, Any] = {
            "content": content if isinstance(content, str) else "",
            "cls": note.get("cls", "reconcile-note"),
        }
        note_id = note.get("id")
        if isinstance(note_id, str) and note_id:
            entry["id"] = note_id
        context = note.get("context")
        entry["context"] = dict(context) if isinstance(context, dict) else None
        session = note.get("session")
        if isinstance(session, str):
            entry["session"] = session
        out.append(entry)
    return out


def union_deferred_notes(
    disk_value: object,
    live: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Union the on-disk hold with the live one — merge writers never shrink.

    The durable hold obeys ONE shrink rule: an entry may only be REMOVED by
    the full save, whose locked rebuild commits the message window and the
    metadata line in one atomic file replace. Every OTHER writer goes through
    this union: live entries are written, and disk entries whose ``id`` is not
    held in memory are RETAINED, because "gone from memory" can mean
    "delivered into the still-unsaved window" — and a retained entry is then
    the only durable copy of a 200-acknowledged note. A retained entry that
    was genuinely dropped (rebind seam) is harmless: the restore replays it,
    the first flush re-drops it, and the next full save retires it.
    """
    live_ids = {entry["id"] for entry in live if entry.get("id")}
    retained: list[dict[str, Any]] = []
    if isinstance(disk_value, list):
        for entry in disk_value:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str) and entry_id and entry_id not in live_ids:
                retained.append(entry)
    return retained + live


def note_hold_durable(conversation_log: Any, key: str, note_id: str) -> bool:
    """True when *note_id* already has a durable entry in *key*'s hold.

    The failure branches of the enqueue persist use this before answering: the
    merge writers commit the WHOLE live list, so a concurrent sibling POST can
    have durably persisted this note even though this writer's own attempt
    failed — an error answer then would orphan that durable copy into a
    duplicate (the caller re-posts a note the restore will also replay).
    Best-effort read: an unreadable record reads as not-durable, which fails
    toward the retryable error rather than toward a silent unkept promise.
    """
    try:
        meta = conversation_log._read_metadata(key)
    except Exception:  # noqa: BLE001 - unreadable record = not provably durable
        return False
    hold = meta.get("deferred_notes") if isinstance(meta, dict) else None
    if not isinstance(hold, list):
        return False
    return any(isinstance(entry, dict) and entry.get("id") == note_id for entry in hold)


def _note_row_committed(conversation_log: Any, key: str, note_id: str) -> bool:
    """True when a delivered row stamped with *note_id* is in the COMMITTED
    transcript. Caller must hold the history lock for *key*, so the file is
    stable. The cheap substring pre-filter keeps the scan linear and skips
    JSON parsing for every non-matching line. Records are read through the
    bounded reader (an oversized record is skipped, never walked), and an
    unreadable file reads as NOT committed — both fail toward writing the
    durable entry, i.e. toward a possible duplicate-on-replay rather than a
    possible loss.
    """
    try:
        path = Path(conversation_log._path(key))
        with open(path, "rb") as handle:
            for line in bounded_records(handle, path, label="note-committed-probe"):
                if note_id not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                row_meta = row.get("meta")
                if isinstance(row_meta, dict) and row_meta.get("noteId") == note_id:
                    return True
    except OSError:
        return False
    return False


def _finite_number(value: int | float) -> bool:
    """True for a finite numeric value. An arbitrary-precision int can
    OverflowError inside ``math.isfinite``'s float conversion — that is not
    finite for TTL purposes either."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def committed_filtered_note_ids(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> set[str]:
    """Ids that :func:`drop_committed_restored_notes` filtered from *before*.

    The restore records these in ``slot._dropped_note_ids`` so the next full
    save retires their durable entries ROW-LESSLY. Without that record, an
    entry whose delivered row was committed by a rows-only handover save can
    never be retired: the row lives in the transcript's frozen prefix, so no
    later save's own window carries it, and the union writer retains the
    id-bearing disk entry forever — each occurrence permanently consumes one
    of the durable hold's ceiling slots until every mid-turn /note POST on
    the session answers 429. A committed entry has the same retirement shape
    as a dropped one: no remaining delivery obligation (the transcript owns
    the row), and a durable copy only a save can remove.
    """
    kept = {entry.get("id") for entry in after}
    filtered: set[str] = set()
    for entry in before:
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id and entry_id not in kept:
            filtered.add(entry_id)
    return filtered


def drop_committed_restored_notes(
    messages: list[dict[str, Any]] | None, notes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop restored hold entries whose delivered row is already in *messages*.

    The rows-only handover save writes the delivered row (``meta.noteId``)
    while deferring the metadata rewrite, so a restart in that window restores
    a hold whose note the transcript permanently carries — replaying it would
    deliver the acknowledged note a second time. A row-committed entry has an
    owner (the transcript); everything else is kept.

    PURE and loop-safe: it scans the message rows the restore already loaded
    off-loop (never re-opens the transcript — both restore paths run ON the
    event loop, and a synchronous file rescan there is exactly what the
    no-blocking-call-on-event-loop rule forbids). A row missing from the
    loaded window reads as NOT committed, i.e. toward a possible duplicate
    rather than a possible loss.
    """
    if not notes or not messages:
        return notes
    committed: set[str] = set()
    for row in messages:
        if not isinstance(row, dict):
            continue
        row_meta = row.get("meta")
        if isinstance(row_meta, dict):
            note_id = row_meta.get("noteId")
            if isinstance(note_id, str) and note_id:
                committed.add(note_id)
    if not committed:
        return notes
    return [entry for entry in notes if entry.get("id") not in committed]


def _sanitize_restored_context(raw: object) -> dict[str, Any] | None:
    """Validate a persisted context half against the pending-context schema.

    A context entry that fails validation is dropped (``None``) while the
    visible note is kept: a malformed ``maxAge``/``injectedAt`` would raise
    ``TypeError`` inside ``context_entry_expired`` at promotion, and a missing
    ``content`` would ``KeyError`` at drain -- turning the restored hold into a
    poison pill that re-raises at every flush seam. Rebuilt with exactly the
    known keys, so a stale ``noteSession`` stamp is discarded (the flush stamps
    the live session at delivery).
    """
    if not isinstance(raw, dict):
        return None
    content = raw.get("content")
    source = raw.get("source")
    injected_at = raw.get("injectedAt")
    max_age = raw.get("maxAge")
    ephemeral = raw.get("ephemeral", True)
    if not isinstance(content, str) or not content or len(content) > MAX_DEFERRED_NOTE_CHARS:
        return None
    if not isinstance(source, str) or not source or len(source) > 64:
        return None
    if isinstance(injected_at, bool) or not isinstance(injected_at, (int, float)):
        return None
    if not _finite_number(injected_at) or injected_at < 0:
        # NaN/Infinity pass the isinstance and sign checks (NaN comparisons are
        # all False) and would make ``injected_at + max_age`` non-comparable at
        # promotion — context that never expires. Same boundary rule as the
        # endpoint's _validate_max_age: reject, fail closed.
        return None
    if max_age is not None:
        if isinstance(max_age, bool) or not isinstance(max_age, (int, float)):
            return None
        if not _finite_number(max_age) or max_age <= 0:
            return None
    if not isinstance(ephemeral, bool):
        return None
    entry: dict[str, Any] = {
        "content": content,
        "source": source,
        "ephemeral": ephemeral,
        "injectedAt": injected_at,
    }
    if max_age is not None:
        entry["maxAge"] = max_age
    return entry


def sanitize_restored_deferred_notes(raw: object) -> list[dict[str, Any]]:
    """Validate a persisted ``deferred_notes`` value back into hold entries.

    On-disk metadata is a trust boundary (the file can be edited or corrupted
    outside the gateway), so every field is re-checked rather than trusted:

    - a note without a non-empty string ``session`` is DROPPED, not delivered
      unconditionally — ``flush_deferred_notes`` treats a missing session as
      "deliver regardless of rebind", which is exactly the cross-session leak
      the authorization stamp exists to prevent;
    - content over :data:`MAX_DEFERRED_NOTE_CHARS` is DROPPED, never truncated:
      the enqueue boundary rejects oversized deferred notes before any 200, so
      an over-bound entry can only be tampering or corruption — and altering
      accepted content on replay is as much a broken promise as losing it;
    - the context half is validated against the pending-context schema by
      :func:`_sanitize_restored_context` and dropped alone when malformed, so
      a corrupted entry cannot crash the flush or the next turn's drain;
    - the result is capped at :data:`_MAX_DURABLE_HOLD_ENTRIES` — the SAME
      ceiling the persist path admits, NOT the live enqueue cap: the durable
      hold legitimately carries up to 2x the cap (retained
      delivered-but-unsaved entries plus live ones), every one of them a
      200-acknowledged note whose caller was told not to re-post, so a
      restore that kept only the live cap's worth would silently discard
      acknowledged content. The live cap still binds NEW enqueues, and the
      first flush drains the restored surplus immediately.
    """
    if not isinstance(raw, list):
        return []
    notes: list[dict[str, Any]] = []
    for item in raw:
        if len(notes) >= _MAX_DURABLE_HOLD_ENTRIES:
            break
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        session = item.get("session")
        if not isinstance(content, str) or not content or len(content) > MAX_DEFERRED_NOTE_CHARS:
            continue
        if not isinstance(session, str) or not session:
            continue
        cls = item.get("cls")
        note_id = item.get("id")
        notes.append(
            {
                # A missing or invalid id gets a fresh one so the entry stays
                # addressable by the enqueue persist's merge after the restore.
                "id": note_id if isinstance(note_id, str) and note_id else uuid.uuid4().hex[:12],
                "content": content,
                "cls": cls if isinstance(cls, str) and cls else "reconcile-note",
                "context": _sanitize_restored_context(item.get("context")),
                "session": session,
            }
        )
    return notes


def persist_deferred_notes_sync(
    conversation_log: Any,
    slot: Any,
    ensure: dict[str, Any],
    authorized_history_key: str,
) -> DeferredHoldOutcome:
    """Write the slot's held notes into its metadata line, merged under lock.

    A MERGE writer: goes through :func:`union_deferred_notes`, so it never
    shrinks the durable hold (only the full save's row-derived rebuild retires
    entries). ``ensure`` pins the note being acknowledged into the write even
    when a concurrent flush drained it from the live list first — without it,
    a POST racing the turn-end flush could return 200 with no durable copy
    anywhere. The pin covers exactly ONE state: not held, not durable, not
    dropped, and not committed. Under the lock, the note is skipped when its
    id is already in the merge (held live or retained on disk), when the
    flush recorded it dropped at the rebind seam, or when its delivered row
    is already in the COMMITTED transcript — a late worker re-adding a hold
    whose row a flush+save pair already committed and retired would make the
    restore replay a second copy of a line the transcript permanently
    carries.

    ``authorized_history_key`` pins the write to the transcript the caller
    authorized at enqueue: the target key is that key, never re-derived, and
    the guard re-checks the slot's CURRENT resolution under the store lock —
    a rebind in the window (a cron or workflow injection claiming an unbound
    ``linked_session_key``) raises :class:`DeferredHoldRebound` instead of
    committing app-authorized content into a foreign transcript's metadata.
    Both parameters are REQUIRED: the one production caller always has both,
    and a re-derived fallback key would silently reopen the rebind hazard the
    pin exists to close.

    The guard resolves :class:`NoteEvidence` for the ``ensure`` note FIRST,
    while the lock makes the observation authoritative — a durable entry
    already on disk, a delivered row in the committed transcript — and every
    exit carries it: the return value's ``evidence`` field, and the
    ``evidence`` attribute on both :class:`DeferredHoldRebound` and
    :class:`DeferredHoldFull`. The handler answers 200 only on that positive
    evidence (or a delivered row in the slot's live list); it never re-probes
    and never infers state from a negative signal.

    Everything is read INSIDE the guard that ``update_metadata_if`` evaluates
    under the cross-process history lock — never from an outside-lock
    snapshot, which could commit out of order against a concurrent force-save
    and clobber its newer state.

    Raises :class:`DeferredHoldFull` when the union would exceed
    ``_MAX_DURABLE_HOLD_ENTRIES``: evicting a retained entry would delete the
    only durable copy of an acknowledged note, so the new hold is refused
    instead. Reaching the ceiling means row-committing saves have not landed
    for multiple turn cycles.

    Merges only into an EXISTING metadata line (``written=False`` when there
    is none): a slot with no line does not survive a restart at all, so there
    is no durable identity for the hold to outlive — and upserting here could
    resurrect a session a concurrent deletion just removed.

    An UNREADABLE record is a different outcome from an absent one:
    ``update_metadata_if`` returns ``False`` for both, but for an unreadable
    record the guard never runs — the slot's file exists and its tab will come
    back after a restart, so treating it as "no durable identity" would hand
    the caller a 200 with no durable copy behind it. That case RAISES instead,
    so the enqueue path discards the note and answers a retryable 503.
    """
    from kiro_crew.dashboard.chat_utils import slot_history_key

    target_key = authorized_history_key
    ensure_id_raw = ensure.get("id")
    ensure_id = ensure_id_raw if isinstance(ensure_id_raw, str) and ensure_id_raw else None
    fields: dict[str, Any] = {}
    guard_ran = {"value": False}
    evidence_box = {"value": _NO_EVIDENCE}

    def _current_under_lock(meta: dict) -> bool:
        guard_ran["value"] = True
        # Evidence resolver — FIRST, while the lock makes it authoritative.
        # This is the single copy of the note's state table; the ensure pin
        # below and the handler's answer both read from it. Two properties:
        #
        # A RECORDED DROP DOMINATES DURABLE EVIDENCE. The save retires a
        # dropped id from the durable hold ROW-LESSLY, so a durable entry
        # whose id the flush recorded dropped is already scheduled to vanish
        # without a delivered row — counting it as evidence would back a 200
        # with an entry the next save destroys (loss, the exact class this
        # resolver exists to close). Only a committed/live row, or a durable
        # entry NOT eligible for row-less retirement, justifies the 200.
        #
        # THE COMMITTED-ROW PROBE IS LAZY. It scans the transcript under the
        # cross-process history lock, so it must not run on the hot path (a
        # fresh note enqueued normally). It is consulted only in the states
        # that already need adjudication: the pin's not-held/not-durable/
        # not-dropped race, a rebind or hold-full refusal, and a written
        # merge that carries no representation of the note.
        dropped = ensure_id is not None and ensure_id in getattr(slot, "_dropped_note_ids", set())
        durable_now = False
        if ensure_id is not None and not dropped:
            disk_hold = meta.get("deferred_notes") if isinstance(meta, dict) else None
            durable_now = isinstance(disk_hold, list) and any(
                isinstance(entry, dict) and entry.get("id") == ensure_id for entry in disk_hold
            )
        committed_box: dict[str, bool | None] = {"value": None}

        def _committed() -> bool:
            if ensure_id is None:
                return False
            if committed_box["value"] is None:
                committed_box["value"] = _note_row_committed(
                    conversation_log, target_key, ensure_id
                )
            return bool(committed_box["value"])

        def _evidence_now() -> NoteEvidence:
            # Short-circuit: durable evidence already answers, so the
            # committed probe (a locked transcript scan) is skipped.
            return NoteEvidence(
                durable=durable_now, committed=False if durable_now else _committed()
            )

        if slot_history_key(slot) != authorized_history_key:
            raise DeferredHoldRebound(
                f"slot {getattr(slot, 'key', '?')} no longer routes to the transcript "
                "the note was authorized against; refusing the durable write",
                evidence=_evidence_now(),
            )
        if not meta:
            evidence_box["value"] = NoteEvidence(durable=False, committed=_committed())
            return False
        merged = union_deferred_notes(
            meta.get("deferred_notes"),
            serialize_deferred_notes(slot._deferred_notes[:]),
        )
        if ensure_id is not None:
            present = {entry.get("id") for entry in merged if entry.get("id")}
            if ensure_id not in present and not dropped and not _committed():
                # Not held, not durable, not dropped, not committed: the
                # one state where the racing flush left the acknowledged
                # note with no representation anywhere. Every other state
                # already has an owner — the merge carries held/retained
                # entries, the save retires dropped ids, and a COMMITTED
                # row is final: re-adding its hold would make the restore
                # replay a second copy of a line the transcript
                # permanently carries.
                merged.append(serialize_deferred_notes([ensure])[0])
        if len(merged) > _MAX_DURABLE_HOLD_ENTRIES:
            raise DeferredHoldFull(
                f"slot {getattr(slot, 'key', '?')} durable hold has {len(merged)} entries "
                f"(ceiling {_MAX_DURABLE_HOLD_ENTRIES}); refusing to evict a retained entry",
                evidence=_evidence_now(),
            )
        fields["deferred_notes"] = merged
        if ensure_id is not None:
            # Post-merge: durable means "in the list this write commits" AND
            # not scheduled for row-less retirement. A note the flush dropped
            # is deliberately not pinned in (and its disk copy, if a sibling
            # wrote one, is retired by the next save without a row), so a
            # written outcome with durable=False is the drop made observable —
            # the handler refuses instead of acknowledging a note nothing
            # will ever deliver or replay.
            durable_final = (not dropped) and any(entry.get("id") == ensure_id for entry in merged)
            evidence_box["value"] = NoteEvidence(
                durable=durable_final,
                committed=False if durable_final else _committed(),
            )
        return True

    written = conversation_log.update_metadata_if(target_key, fields, _current_under_lock)
    if written:
        # The merge landed in an EXISTING metadata line: record the durable
        # identity monotonically so a later failure branch can tell a
        # delete-won race from a never-persisted slot.
        with contextlib.suppress(AttributeError):
            slot._disk_meta_observed = True
        return DeferredHoldOutcome(written=True, evidence=evidence_box["value"])
    if not guard_ran["value"]:
        raise RuntimeError(
            f"metadata record for slot {getattr(slot, 'key', '?')} is unreadable; "
            "the deferred-note hold was not persisted"
        )
    return DeferredHoldOutcome(written=False, evidence=evidence_box["value"])


def _apply_message_patch(slot: Any, message: dict, content: str | None, meta: dict | None) -> dict:
    """Write a resolved row's new content/meta and mark the slot for persistence."""
    if content is not None:
        message["content"] = content
        slot.invalidate_source_links()
    if meta is not None:
        message["meta"] = meta
    slot._dirty = True
    return message


class SlotBufferCoordinator:
    """Operate on the current facade-owned slot containers without aliasing them."""

    @staticmethod
    def push_wire_frame(slot: Any, cls: str, content: str) -> None:
        slot._pending.append({"role": cls, "content": content, "cls": cls, "ts": ""})
        slot.event.set()

    @staticmethod
    def drain(slot: Any) -> list[dict[str, str]]:
        pending = slot._pending[:]
        slot._pending.clear()
        slot.event.clear()
        return pending

    @staticmethod
    def pending_has_consumer(slot: Any) -> bool:
        return slot._pending_consumers > 0 or slot._has_reader

    @staticmethod
    def retry_deferred_release(slot: Any) -> int:
        if not slot._pending_release_deferred:
            return 0
        return slot.release_pending_chunks()

    @staticmethod
    @contextlib.contextmanager
    def pending_consumer(slot: Any) -> Iterator[None]:
        slot._pending_consumers += 1
        try:
            yield
        finally:
            slot._pending_consumers = max(0, slot._pending_consumers - 1)
            slot._retry_deferred_release()

    @staticmethod
    def release_pending_chunks(slot: Any) -> int:
        # A live SSE/OpenAI reader owns these rows until it detaches.  Remember a
        # refused release so the final detaching consumer can reclaim them.
        if slot.pending_has_consumer:
            slot._pending_release_deferred = True
            return 0
        slot._pending_release_deferred = False
        before = len(slot._pending)
        if not before:
            return 0
        slot._pending = [message for message in slot._pending if message.get("role") != "chunk"]
        return before - len(slot._pending)

    @staticmethod
    def purge_chunks(slot: Any) -> int:
        slot.messages = [message for message in slot.messages if message.get("role") != "chunk"]
        return slot.release_pending_chunks()

    @staticmethod
    def append_pending_context(
        slot: Any,
        entry: dict[str, Any],
        *,
        max_pending_context: int,
        entry_expired: Callable[[dict[str, Any], float], bool],
    ) -> None:
        now = time.time()
        if entry_expired(entry, now):
            return
        slot._pending_context[:] = [
            current for current in slot._pending_context if not entry_expired(current, now)
        ]
        while len(slot._pending_context) >= max_pending_context:
            slot._pending_context.pop(0)
        slot._pending_context.append(entry)

    @staticmethod
    def drop_foreign_authorized_notes(
        slot: Any,
        *,
        authorized_elsewhere: Callable[[object, str], bool],
        logger: logging.Logger,
    ) -> int:
        # Local import avoids a module cycle: chat_utils imports the state facade.
        from kiro_crew.dashboard.chat_utils import effective_session_key

        live_session = effective_session_key(slot)
        kept_context = [
            entry
            for entry in slot._pending_context
            if not authorized_elsewhere(entry, live_session)
        ]
        dropped = len(slot._pending_context) - len(kept_context)
        if dropped:
            slot._pending_context[:] = kept_context

        kept_messages = [
            message
            for message in slot.messages
            if not authorized_elsewhere(message.get("meta"), live_session)
        ]
        if len(kept_messages) != len(slot.messages):
            dropped += len(slot.messages) - len(kept_messages)
            slot.messages[:] = kept_messages
        if dropped:
            sel().log_api_access(
                caller="dashboard",
                operation="note_rebind_drop",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot.key} dropped={dropped}",
                error="slot was rebound to another session after the note was written",
            )
            logger.warning(
                "Slot %s dropped %d note item(s): authorized elsewhere, slot now routes to %s",
                slot.key,
                dropped,
                live_session,
            )
        return dropped

    @staticmethod
    def deferred_context_count(slot: Any) -> int:
        return sum(1 for note in slot._deferred_notes if note.get("context") is not None)

    @staticmethod
    def flush_deferred_notes(slot: Any, *, logger: logging.Logger) -> int:
        """Flush held notes in order, restoring the unwritten suffix on failure.

        Purely an in-memory drain: the flush NEVER writes the durable hold.
        Each delivered inject row carries its note id in
        ``meta.noteId``, and the full save retires a durable entry exactly
        when the window it writes contains that id — so the delivered row and
        the retirement land in one atomic file write, whatever the
        flush/save/enqueue interleaving is. A note dropped at the rebind
        seam records its id in ``slot._dropped_note_ids`` instead, and the
        next save retires it from there. A crash before the save re-delivers
        the note on restore: at-least-once, the correct failure direction for
        a delivery promise.
        """
        if not slot._deferred_notes:
            return 0
        from kiro_crew.dashboard.chat_utils import effective_session_key

        held = slot._deferred_notes[:]
        slot._deferred_notes.clear()
        live_session = effective_session_key(slot)
        written = 0
        for index, note in enumerate(held):
            authorized_session = note.get("session")
            if authorized_session is not None and authorized_session != live_session:
                sel().log_api_access(
                    caller="dashboard",
                    operation="note_flush",
                    outcome="denied",
                    source="app_isolation",
                    resources=f"slot={slot.key}",
                    error="slot was rebound to another session while the note was held",
                )
                logger.warning(
                    "Slot %s dropped a held note: authorized for %s, slot now routes to %s",
                    slot.key,
                    authorized_session,
                    live_session,
                )
                dropped_id = note.get("id")
                if isinstance(dropped_id, str) and dropped_id:
                    # A dropped note has no remaining delivery obligation, but
                    # its durable entry can only be retired by a save — record
                    # the id so the next full save retires it instead of the
                    # entry replaying (and re-dropping) after every restart
                    # until the hold's ceiling fills with garbage.
                    slot._dropped_note_ids.add(dropped_id)
                continue

            # Pop is a retry marker: if the visible row fails after the context
            # was queued, the restored note must not enqueue that context twice.
            context = note.pop("context", None)
            note_id = note.get("id")
            row_meta: dict[str, Any] = {"noteSession": live_session}
            if isinstance(note_id, str) and note_id:
                # The delivered row carries its note id, and the full save
                # retires a durable entry exactly when the window it writes
                # contains that id — row and retirement land in one atomic
                # file write, whatever the flush/save interleaving was.
                row_meta["noteId"] = note_id
            try:
                if context is not None:
                    context["noteSession"] = live_session
                    slot.append_pending_context(context)
                slot.append(
                    role="inject",
                    content=note["content"],
                    cls=note["cls"],
                    broadcast=True,
                    meta=row_meta,
                )
            except Exception:
                # New arrivals stay after this older, unwritten suffix. The
                # durable copy still holds every note (delivered rows
                # included) and the next full save trues it up against live
                # state; see the docstring for why no metadata write happens
                # here.
                slot._deferred_notes[:0] = held[index:]
                raise
            written += 1
        return written

    @staticmethod
    def mark_permission_resolved(slot: Any, approval_id: str, decision: str) -> None:
        for message in slot.messages:
            if message.get("role") != "permission":
                continue
            try:
                cls_data = json.loads(message.get("cls", ""))
                if isinstance(cls_data, dict) and cls_data.get("request_id") == approval_id:
                    cls_data["resolved"] = decision
                    message["cls"] = json.dumps(cls_data)
                    return
            except (json.JSONDecodeError, TypeError):
                pass

    @staticmethod
    def update_message(
        slot: Any,
        ts: str,
        *,
        content: str | None,
        meta: dict | None,
        mid: str | None = None,
    ) -> dict | None:
        # `mid` is the row's server-minted identity, stamped once per row by
        # _ChatSlot.append. Prefer it: `ts` is NOT an identity -- an explicitly
        # supplied one is preserved verbatim for a row replayed from a channel
        # transcript, and a coarse OS clock stamps two same-tick rows identically
        # -- so a ts lookup resolves the FIRST match and can patch the wrong row.
        # `ts` remains the fallback for a legacy row written before the id existed,
        # where it is the only handle available.
        if mid:
            for message in slot.messages:
                if (message.get("meta") or {}).get("mid") == mid:
                    return _apply_message_patch(slot, message, content, meta)
            return None
        if not ts:
            return None
        for message in slot.messages:
            if message.get("ts") != ts:
                continue
            return _apply_message_patch(slot, message, content, meta)
        return None
