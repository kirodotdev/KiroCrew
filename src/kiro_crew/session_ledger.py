"""Per-session work ledger — a PROJECTION of the session's crew log.

Long-horizon sessions (monitor loops, goal loops) accumulate their working state
as prior transcript turns, which the harness-owned compaction then summarizes
lossily. This module gives every session a durable **state record** instead —
goal, phase, next intent, tried approaches, artifact pointers, and a bounded
event tail — so the context window becomes a cache and the record is the
authority.

The record is not stored. It is a FOLD of the session's append-only crew log:
:func:`record` appends exactly one ``ledger/recorded`` entry per call, and every
reader folds those entries back into the record (``crew_log.projection``'s
``ledger`` fold). So there is one authority for the ledger and no second document
beside it. Two paths hold ledger bytes, and only the first is live:

    <data home>/crew-log/sessions/<store name>/log.jsonl   # the entries
    <data home>/ledger/<store name>/state.json             # pre-projection, carried once

The legacy path is read only to be RETIRED, by the upgrade carry-forward: the first
record on a slot whose folded record is empty appends the old document's goal, phase,
next and artifacts as one further entry, then marks it consumed so no later session
can carry it again. That carry is the ONE exception to one entry per call, it happens
at most once per slot, and it is a separate append rather than a merged one -- so
the entry a call is making stays exactly what the caller asked for.

Until that append happens a READ folds the same pending entry in without writing it,
because the first resumed turn after an upgrade reads before it records and would
otherwise run with no goal. Both come from one construction, so the record does not
change shape when the carry lands. Nothing reads the document once it is consumed,
nothing publishes from it, and nothing writes it at all.

Design notes:

- **One entry, one update.** The fields a call set and the event that explains
  them ride on the SAME appended line, so the phase-requires-a-reason rule is a
  property of one entry rather than of two writes a crash can separate. There is
  no ordering in which a reader sees a phase that moved without its reason.
- **Keyed by SLOT, folded across units.** A slot owns one ACP session id at a
  time, not for its whole life — a reset, an agent or model switch and a provider
  swap all cold-start a new id — so a slot's updates are spread over the crew log
  of each id it ran under. The read joins them, oldest unit first
  (``store.session_units_for_slot``); a write only ever needs the unit it is in.
- **Exact-key identity.** :func:`ledger_key` only strips the dashboard prefixes
  (the same strip the permanent-delete funnel applies); it never folds the key's
  charset. One dashboard session is legitimately spelled both
  ``dashboard_chat-X`` and ``chat-X`` and both must reach one ledger, while a
  charset fold would map distinct channel session keys onto one.
- **Purged with the session.** The entries live in the session's crew log, so the
  permanent-delete funnel that removes that unit removes the ledger with it. The
  legacy ``ledger/`` store is left alone by every request path and is collectable
  with ``kirocrew ledger-sweep``.
- **Bounds are re-applied on read.** Every field is clamped when the entry is
  built AND when the fold reads it: a writer's clamp binds the writer, and a
  damaged or planted line is exactly the input that ignores it.

The legacy store's delete machinery stays here (:func:`purge_matching` and the
helpers below), because it is still the one spelling of removing one of those
directories and the sweep is still its caller. ``crew_log.store`` also imports
:func:`_store_name`, :func:`resolved_within`, :func:`is_link` and
:func:`unlink_lock_in_hold` from here.

Callers pass keys through :func:`ledger_key`; this module never imports dashboard
state, and it reaches the crew log lazily so it stays usable from the gateway boot
path.
"""

from __future__ import annotations

import hashlib
import importlib as _importlib
import json
import logging
import os
import re
import shutil
import sys as _sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, NamedTuple

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew.atomic_write import append_line, atomic_write, fsync_dir
from kiro_crew.config.paths import data_home
from kiro_crew.constants import env_file_display
from kiro_crew.platform_compat import (
    IS_WINDOWS,
    make_owner_only_dir,
    release_lock,
    strip_extended_length_prefix,
    try_acquire_lock,
)
from kiro_crew.session_ledger_control import (
    _carry_committed,
    _claim_carry,
    _excluded_units,
    _finish_carry,
    _legacy_document,
    _note_unit_order,
    _recorded_unit_order,
)
from kiro_crew.session_ledger_layout import (
    _APPEND_FLUSH_SECONDS,
    _CARRIED_FILE,
    _CLAIM_BUSY,
    _CLAIM_TAKEN,
    _CONTENT_FIELDS,
    _CONTROL_DIR_NAME,
    _ENTRY_SRC,
    _FOLD_NAME,
    _KEY_FILE,
    _LOCK_FILE,
    _MAX_ARTIFACT_KEY,
    _MAX_ARTIFACTS,
    _MAX_PHASE,
    _SEQ_READ_ATTEMPTS,
    _SEQ_READ_BACKOFF_SECS,
    _STATE_FILE,
    EVENT_KINDS,
    LEDGER_ENTRY_TYPE,
    TERMINAL_PHASES,
    LedgerEntryTooLarge,
    LedgerUnavailable,
    _clamp,
    _control_file,
    _empty_state,
    _ledger_root,
    _locked,
    canonical_slot,
)

logger = logging.getLogger(__name__)


def _projection() -> Any:
    """The fold package, imported the first time a call actually needs it.

    Never at module scope, for two reasons that both matter. This module is on the
    gateway's boot path -- the nudge composer imports it -- and the crew log's
    storage package is optional behind its own flag, so importing it here would put
    that cost on every launch. And the crew log package imports THIS module (for the
    store-name fold and for the ledger's own vocabulary), so a module-scope import
    back would be a cycle.
    """
    from kiro_crew.crew_log import projection

    return projection


def crew_log_units(
    slot_key: str, live_session_id: str = "", alias: str = "", *, strict: bool = False
) -> tuple[str, ...]:
    """Every crew log holding *slot_key*'s ledger entries, oldest unit first.

    *slot_key* is the CANONICAL spelling (the one a unit header records); *alias* is
    the caller's own spelling (see :func:`canonical_slot`), joined so records written
    under it keep reading.

    ``()`` when the slot has none, the crew log is off, or the listing fails: this
    runs on a loop cycle's read path and must not raise into it.

    *strict* is for a caller deciding an OBLIGATION ("is there a unit a later fact
    must go into"), where an unlistable slot and an empty one are opposite answers.
    It raises instead, and is passed DOWN to both store lookups because the store
    swallows its own scan failure before this function's ``except`` sees it. A
    session root that does not exist is still ``()`` under ``strict``, or every
    dismissal on a fresh install would retry forever.

    ``_recorded_unit_order`` is not strict-gated: an unreadable order file only
    reorders the units, never drops one.
    """
    if not slot_key:
        return ()
    try:
        from kiro_crew.crew_log.store import session_units_for_slot

        units = session_units_for_slot(slot_key, strict=strict)
        if alias and alias != slot_key:
            # The caller's own spelling is joined BESIDE the canonical one, so a record
            # written under it keeps reading. Its units come FIRST: the canonical ones
            # are what is being written now, and a later update wins. Every control
            # file is keyed by the canonical spelling alone, so one slot has one
            # exclusion list and one order log however a caller spells its key.
            seen = set(units)
            units = (
                tuple(
                    unit
                    for unit in session_units_for_slot(alias, strict=strict)
                    if unit not in seen
                )
                + units
            )
        excluded = _excluded_units(slot_key)
        if alias and alias != slot_key:
            # A delete records an exclusion under the slot its unit's HEADER names, so
            # an alias unit's exclusion sits under the alias, not the canonical key.
            excluded |= _excluded_units(alias)
        if excluded:
            units = tuple(unit for unit in units if unit not in excluded)
        # The order log keeps the NEWEST ids, so a unit absent from it is older than all
        # of them and applies BEFORE the kept tail; after it, an evicted old unit would
        # write its stale goal and phase over the current ones. The one exception is the
        # slot's newest unit by succession: a reset's new session that has not recorded
        # yet holds no ledger entry, so its place does not change the record, and
        # listing it last keeps a read's unit list the one the writer folds (live unit
        # last). Listed first, its own appends retire the warm cell and the savepoint
        # and every read after a restart folds the slot cold.
        from kiro_crew.session_ledger_control import _causal_units as _ordered_units

        units = _ordered_units(slot_key, units, _recorded_unit_order(slot_key))
        if live_session_id and live_session_id in units:
            # The LIVE unit applies LAST, whatever the clock says: units sort by header
            # wall clock, and a clock that moved backward would put a retired
            # session's goal and phase over the current ones.
            units = tuple(u for u in units if u != live_session_id) + (live_session_id,)
        return units
    except FileNotFoundError:
        # No session root yet: no slot has a unit. Not indeterminate, so ``strict``
        # does not raise (a fresh install's dismissals would retry forever).
        return ()
    except Exception:
        # FAIL CLOSED to no units. With an unreadable exclusion list, answering with
        # the units would serve a deleted conversation's state to the slot's current
        # holder; an empty record recovers on the next good read. A ``strict``
        # caller gets the failure so it can report a retryable outcome.
        if strict:
            raise
        logger.warning("ledger: could not list the crew logs for this slot", exc_info=True)
        return ()


def _fold_checkpoint(slot_key: str, units: "tuple[str, ...]") -> Any:
    """This slot's ledger fold, continued from where the last read left it.

    A cold fold walks every line of every unit (message bodies included), so a loop
    reading on every wake would re-walk its whole history. The shared incremental fold
    (:func:`kiro_crew.crew_log.projection.fold_slot_warm`) continues from an in-memory
    cell, or from its on-disk savepoint beside the newest unit's log (644 MB log:
    70.2 s cold vs 11.6 s resumed), and refolds cold for any shape it cannot carry.
    The retirement rules live in ``crew_log.checkpoint``; a doubtful savepoint folds
    cold and reaches the same record.
    """
    return _projection().fold_slot_warm(_FOLD_NAME, units, slot=slot_key)


def _unit_last_seq(unit_id: str) -> int | None:
    """The newest seq in *unit_id*'s log as the file itself reports it.

    0 when the unit has no log, ``None`` when its log could not be READ. A growth
    check must not read an unreadable log as 0: after an append that reports a
    landed update as lost, before one it counts old entries as this append.

    A refusal that clears on its own (:func:`_clears_on_its_own`, e.g. a Windows
    indexer holding the file) is retried a fixed COUNT of times, so one refusal
    sequence gets one answer on every host. Other failures answer ``None`` at once.
    """
    failure = ""
    attempts = 0
    while attempts < _SEQ_READ_ATTEMPTS:
        if attempts:
            time.sleep(_SEQ_READ_BACKOFF_SECS)
        attempts += 1
        try:
            handle = _projection().open_session_log(unit_id)
        except Exception as exc:
            # Kept as TEXT, never as the exception: its traceback would hold this frame
            # and its callers', and a crew log handle bound in any of them with it.
            failure = f"{type(exc).__name__}: {exc}"
            if _clears_on_its_own(exc):
                continue
            break
        if handle is None:
            return 0
        # ``last_seq`` on a freshly opened handle is read off the file's tail, which is
        # what makes it usable as a growth signal for a reader that never appends.
        return int(getattr(handle, "last_seq", 0) or 0)
    logger.warning(
        "ledger: could not read this unit's crew log (%d attempt(s)): %s", attempts, failure
    )
    return None


def _clears_on_its_own(exc: Exception) -> bool:
    """Whether an open that raised *exc* is worth trying again.

    ``FileNotFoundError`` is the unit's newest segment vanishing between being listed
    and its tail being read -- a replace, a prune -- and the next listing finds it or
    finds it gone, on every platform. (A header segment that vanishes reads as a
    damaged header instead, which is not retried and fails closed.) ``PermissionError``
    is a sharing or lock violation only on Windows; on POSIX it is a real access fault
    that no retry clears, so it answers at once, the split ``atomic_write`` makes for
    the same error. Nothing else is retried: the store lock's own refusal is a plain
    ``OSError`` raised after its ceiling, on a holder that is stuck, so a retry would
    only wait that ceiling out again, and a damaged header reads the same every time.
    """
    if isinstance(exc, FileNotFoundError):
        return True
    return IS_WINDOWS and isinstance(exc, PermissionError)


def _grew(seq_before: int | None, seq_after: int | None) -> bool:
    """Whether two samples of one unit's newest seq PROVE its log grew between them.

    An unreadable sample proves nothing, so it never counts as growth. That is the
    fail-closed side: everything this gates -- a durable acknowledgement, a published
    precedence, a committed carry -- is a claim that something landed.
    """
    return seq_before is not None and seq_after is not None and seq_after > seq_before


def read_state(slot_key: str, live_session_id: str = "") -> dict[str, Any]:
    """The state record for *slot_key* -- a FOLD of its ``ledger/recorded`` entries.

    *live_session_id* is the caller's ACP session, when it has one: it resolves the
    key to the slot identity unit headers record (:func:`canonical_slot`) and pins
    the unit being written LAST whatever the header clocks say.

    The record is stored nowhere, so no second document can say a phase moved
    without the log saying why. Its shape is ``crew_log.projection._ledger_render``.

    Best-effort: no entries, or a fold that fails, reads as the empty record, since a
    nudge cycle calls this on its way into a turn. A refused fold is logged as a
    warning (a reader older than the writer); an absent ledger is silent.

    Exception: a slot whose state predates the fold has an un-carried document, and
    this read folds its PENDING carry in without appending it
    (:func:`_legacy_preview`), so the first resumed turn after an upgrade has its goal.

    Lock-free: the crew log is append-only, so a reader sees a prefix, never a torn
    record.
    """
    key = slot_key or ""
    canonical = canonical_slot(key, live_session_id)
    units = crew_log_units(canonical, live_session_id, alias=key)
    try:
        # Folded even with NO units: a pre-fold slot has a document and no crew log,
        # and returning early would skip the carry preview it needs.
        base = _fold_checkpoint(canonical, units)
        value = _projection().projection_of(base).value
    except Exception:
        logger.warning("ledger: folding this slot's crew logs failed", exc_info=True)
        # Drop the warm cell: a half-folded state must not answer the next read.
        _projection().forget_slot_folds(canonical, _FOLD_NAME)
        # NOT previewed: the log may hold entries newer than the document that this
        # build could not read.
        return _empty_state()
    if _has_content(value):
        return value
    # An EMPTY folded record is the upgrade trigger, the same one the write path uses,
    # so the two agree on WHEN a carry is owed as well as on what it carries.
    return _legacy_preview(canonical, base, alias=key) or value


def has_ledger(slot_key: str) -> bool:
    """Whether *slot_key* has ever recorded anything.

    A full fold, not a cheap file probe: the record has no file of its own to stat,
    and the honest answer to "has this slot recorded" is whether its entries fold
    to anything. Callers that go on to READ the record should call
    :func:`read_state` once and test it themselves rather than pay for two folds.
    """
    return _has_content(read_state(slot_key))


def _has_content(state: dict[str, Any]) -> bool:
    """Whether *state* holds anything a reader would act on."""
    return any(state.get(field) for field in _CONTENT_FIELDS)


def record_update(
    slot_key: str,
    *,
    session_id: str,
    goal: str | None = None,
    phase: str | None = None,
    next_step: str | None = None,
    tried_approach: str | None = None,
    tried_rejected_because: str | None = None,
    artifacts: dict[str, str] | None = None,
    event: str | None = None,
    event_kind: str | None = None,
) -> "tuple[dict[str, Any], bool]":
    """Append ONE ``ledger/recorded`` entry for *slot_key*; return its record and
    whether the append reached disk.

    *session_id* is the crew log (the slot's current ACP session) the entry lands
    in; reads join every unit the slot ran under.

    *phase* without *event* and a recognized *event_kind* raises ``ValueError``. A
    phase and its reason ride on the SAME entry, so no reader or crash can split them.

    Returns the on-disk state folded over this one entry by the readers' own fold, so
    the answer does not depend on the writer having drained.

    Raises :class:`LedgerUnavailable` when the session has no crew log to append to.
    """
    if phase is not None:
        if not (event and event.strip()):
            raise ValueError("phase change requires an event: pass event + event_kind")
        if (event_kind or "").strip() not in EVENT_KINDS:
            kinds = ", ".join(sorted(EVENT_KINDS))
            raise ValueError(f"phase change requires event_kind (one of: {kinds})")
    if not slot_key or "\0" in slot_key:
        raise ValueError(f"Invalid slot key for ledger: {slot_key!r}")
    if not session_id:
        raise LedgerUnavailable(
            "this session has no live crew log, so its ledger cannot be recorded"
        )
    # Key on the unit header's slot and keep the caller's spelling as an ALIAS, or a
    # caller keyed `cron:<id>` or `hook:<id>` could never read its own record back.
    alias = slot_key
    slot_key = canonical_slot(slot_key, session_id)
    data = _entry_data(
        slot_key,
        goal=goal,
        phase=phase,
        next_step=next_step,
        tried_approach=tried_approach,
        tried_rejected_because=tried_rejected_because,
        artifacts=artifacts,
        event=event,
        event_kind=event_kind,
    )
    projection = _require_crew_log(session_id)
    units = crew_log_units(slot_key, session_id, alias=alias)
    # Read BEFORE the append and fold the entry in below: the writer is async, so a
    # read-back would race the drain and report a phase just set as unset.
    base = _fold_checkpoint(slot_key, units)
    # Upgrade CARRY-FORWARD, at most once per slot: a pre-fold document becomes an
    # ENTRY, so the log stays the single authority. Triggered by an EMPTY FOLDED
    # RECORD, not an absent unit: the crew log exists from the first turn.
    if not _has_content(projection.projection_of(base).value) and _carry_legacy_forward(
        slot_key, session_id, alias
    ):
        units = crew_log_units(slot_key, session_id)
        base = _fold_checkpoint(slot_key, units)
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.schema import Entry

    # Sampled BEFORE the append: when a second gateway owns this unit the append is
    # inline and a refusal bumps the counter during the call, so a later sample
    # would report the refused write as durable.
    refused_before = crew_log_emit.dropped_writes()
    seq_before = _unit_last_seq(session_id)
    # Refuse an oversized entry up front. The writer's ceiling refusal looks like
    # ``durable=False``, the same as a busy writer whose entry does land, but an
    # oversized entry never lands however often it is retried.
    if not crew_log_emit.ledger_entry_fits(data):
        raise LedgerEntryTooLarge(
            "this update does not fit one crew log entry; record fewer or shorter "
            "fields (artifacts are the usual cause)"
        )
    # Sampled BEFORE the append, so it is never LATER than the stamp the store
    # writes, and the gap is queue-to-write latency rather than the whole drain.
    recorded_ms = int(time.time() * 1000)
    crew_log_emit.on_ledger_recorded(session_id, data)
    # WAIT for the append (on a worker thread, so no event-loop cost): the caller is
    # about to act on the record, and an unclean death before the flush would lose
    # an update already reported as taken. ``flush`` is False only when the writer
    # is still busy (entry queued, not lost); a PERMANENTLY REFUSED append drains and
    # is counted, hence the counter sampled around it. Neither raises: a retry would
    # append twice, and the counter is process-wide. The loss is logged and the next
    # update re-reads the base from disk.
    drained = crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    # THIS UNIT's own log must also have grown: the process-wide counter cannot say
    # that. An unreadable sample proves nothing, so it is NOT landed.
    seq_after = _unit_last_seq(session_id)
    landed = _grew(seq_before, seq_after)
    unread = seq_before is None or seq_after is None
    durable = drained and landed and crew_log_emit.dropped_writes() == refused_before
    # PUBLISH PRECEDENCE ONLY ONCE THIS UNIT'S LOG HAS ACTUALLY GROWN, so keep this
    # AFTER ``landed``. The order file claims "the unit recording now holds the
    # newest entry"; noting a unit whose append was refused or is still queued pins a
    # RETIRED unit ahead of its successor (fsynced at once), and the successor then
    # folds stale goal and phase as current. Gated on ``landed``, not ``durable``:
    # the process-wide refusal counter would let another session's refusal suppress
    # an earned note. An unproved move withholds the note; the next proved record
    # corrects a missing one.
    if landed:
        _note_unit_order(slot_key, session_id)
    if unread:
        logger.warning(
            "ledger: this unit's crew log could not be read %s the append, so the update "
            "is not proved to have landed; it is reported not durable and this unit's "
            "precedence is not published",
            " and ".join(
                side for side, seq in (("before", seq_before), ("after", seq_after)) if seq is None
            ),
        )
    if not drained:
        logger.warning(
            "ledger: the crew log writer did not drain within %.1fs; this update is "
            "queued and counted, not yet durable",
            _APPEND_FLUSH_SECONDS,
        )
    elif not durable and not unread:
        logger.warning(
            "ledger: the crew log refused an append while this update was in flight; "
            "the update may not have landed and the next one supersedes it"
        )
    pending = Entry(
        type=LEDGER_ENTRY_TYPE,
        # One past what the fold consumed, which is all ``advance`` asks of it. The
        # real seq is assigned by the store under its lock and is not knowable here;
        # this entry is never written from this object, only folded.
        seq=base.last_seq + 1,
        time=recorded_ms,
        src=_ENTRY_SRC,
        data=data,
    )
    state = projection.projection_of(projection.advance(base, (pending,))).value
    # Durability is RETURNED beside the record, never stashed in a slot-keyed flag
    # (alias vs canonical spelling, concurrent updates), and not inside the record,
    # whose readers expect its ten fields.
    return state, durable


def record(slot_key: str, **kwargs: Any) -> dict[str, Any]:
    """:func:`record_update`'s record alone, for a caller that cannot act on durability.

    A caller that ignores durability is no worse off than one that never asked: the
    update is folded either way and the next one supersedes it.
    """
    return record_update(slot_key, **kwargs)[0]


def _legacy_carry_payload(slot_key: str, legacy: dict[str, Any]) -> dict[str, Any]:
    """The entry a carry appends for *slot_key*, built from its *legacy* document.

    Shared by the carry and the read preview so both see ONE construction.

    Carries goal, phase, next, artifacts and the newest rejected approach. The entry
    holds ONE ``tried``, so earlier approaches and the event tail are counted in the
    event note, not appended per row (unbounded work inside a turn). The phase rides
    with that event, so it never appears without a logged reason.

    The entry must fit one crew log line, or the carry can never land and every
    record on the slot is refused. Fields within their own clamps can still overflow
    together, so the newest :data:`_MAX_ARTIFACTS` artifacts are kept, then the
    oldest artifacts and then the rejected approach are dropped until the entry
    fits. What was dropped is counted in the note. Goal, next and phase alone always
    fit.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    data: dict[str, Any] = {"slot": _clamp(slot_key)}
    for field, key in (("goal", "goal"), ("next", "next")):
        value = legacy.get(key)
        if isinstance(value, str) and value:
            data[field] = _clamp(value)
    phase = legacy.get("phase")
    if isinstance(phase, str) and phase:
        data["phase"] = _clamp(phase, _MAX_PHASE)
    artifacts = legacy.get("artifacts")
    dropped_artifacts = 0
    if isinstance(artifacts, dict) and artifacts:
        pointers = {
            _clamp(k, _MAX_ARTIFACT_KEY): _clamp(v)
            for k, v in artifacts.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        # The fold keeps the newest pointers when it trims, so the carry does too.
        while len(pointers) > _MAX_ARTIFACTS:
            pointers.pop(next(iter(pointers)))
            dropped_artifacts += 1
        if pointers:
            data["artifacts"] = pointers
    tried = legacy.get("tried")
    dropped_tried = 0
    if isinstance(tried, list) and tried:
        newest = tried[-1]
        if isinstance(newest, dict) and isinstance(newest.get("approach"), str):
            data["tried"] = {
                "approach": _clamp(newest["approach"]),
                "rejected_because": _clamp(newest.get("rejected_because", "")),
            }
            dropped_tried = len(tried) - 1
        else:
            dropped_tried = len(tried)
    events = legacy.get("events")
    dropped_events = len(events) if isinstance(events, list) else 0
    dropped_newest_tried = False

    def _note() -> None:
        left = []
        if dropped_newest_tried:
            left.append(f"{dropped_tried} rejected approach(es)")
        elif dropped_tried > 0:
            left.append(f"{dropped_tried} earlier rejected approach(es)")
        if dropped_artifacts > 0:
            left.append(f"{dropped_artifacts} artifact(s)")
        if dropped_events > 0:
            left.append(f"{dropped_events} event(s)")
        note = "carried forward from this slot's pre-projection ledger document"
        if left:
            note += "; not carried: " + " and ".join(left)
        data["event"] = _clamp(note)

    data["event_kind"] = "note"
    _note()
    while not crew_log_emit.ledger_entry_fits(data):
        kept = data.get("artifacts")
        if kept:
            kept.pop(next(iter(kept)))
            dropped_artifacts += 1
            if not kept:
                del data["artifacts"]
        elif "tried" in data:
            del data["tried"]
            dropped_tried += 1
            dropped_newest_tried = True
        else:
            break
        _note()
    return data


def _legacy_source(slot_key: str, alias: str) -> str:
    """The key whose legacy document and ``carried`` marker a carry for *slot_key* uses.

    *slot_key* itself, except for a messaging-channel *alias* folded to *slot_key*
    (:func:`canonical_slot`): a document written before the fold sits under the raw
    ``slack:<ts>`` spelling, so it is used when the canonical slot has none. A
    committed canonical marker ends both, and the raw key's own marker ends the raw
    document, so a delete that tombstoned either spelling never brings it back. The
    carried entry still names *slot_key*.
    Raises :class:`LedgerUnavailable` when the canonical document is unreadable.
    """
    if not alias or alias == slot_key or canonical_slot(alias, "") == alias:
        return slot_key
    if _carry_committed(_control_file(slot_key, _CARRIED_FILE)):
        return slot_key
    if _has_content(_legacy_document(slot_key)):
        return slot_key
    return alias


def _legacy_preview(slot_key: str, base: Any, alias: str = "") -> dict[str, Any] | None:
    """The record *slot_key*'s pending carry WOULD fold to, or ``None`` for no carry.

    The read half of the upgrade carry. Only a write can carry (it appends and takes
    an exclusive claim), but the first read after an upgrade is the auto-nudge
    snapshot, which must not raise and would otherwise give the resumed turn no goal.
    So this FOLDS the carry's own payload over the same base without appending it;
    the carry stays the only thing that makes it permanent.

    It cannot resurrect settled state: a COMMITTED marker (a finished carry, or a
    permanent delete's tombstone, so a recycled slot key never sees a deleted
    conversation) ends it; callers reach it only with an EMPTY fold; and it folds
    ONTO the base, keeping event-only entries.

    Never raises: every failure answers ``None`` (the empty record), and
    :func:`_legacy_document` refuses files over the size ceiling.
    """
    try:
        source = _legacy_source(slot_key, alias)
        if _carry_committed(_control_file(source, _CARRIED_FILE)):
            return None
        legacy = _legacy_document(source)
    except LedgerUnavailable:
        # Unreadable is not empty, but a read must not raise into a nudge cycle.
        # Warned, since the carry is still owed and the next write reports it.
        logger.warning("ledger: could not read this slot's legacy document", exc_info=True)
        return None
    except Exception:
        logger.warning("ledger: could not check this slot's legacy document", exc_info=True)
        return None
    if not _has_content(legacy):
        return None
    try:
        from kiro_crew.crew_log.schema import Entry

        projection = _projection()
        pending = Entry(
            type=LEDGER_ENTRY_TYPE,
            # One past what the fold consumed, which is all ``advance`` asks. This entry
            # is never written from this object, only folded -- the carry's own append
            # takes its seq from the store under the store's lock.
            seq=base.last_seq + 1,
            time=int(time.time() * 1000),
            src=_ENTRY_SRC,
            data=_legacy_carry_payload(slot_key, legacy),
        )
        return dict(projection.projection_of(projection.advance(base, (pending,))).value)
    except Exception:
        logger.warning("ledger: could not fold this slot's pending carry", exc_info=True)
        return None


def _carry_legacy_forward(slot_key: str, session_id: str, alias: str = "") -> bool:
    """Append the pre-projection document as this slot's first entry. Once.

    Returns whether anything was carried. The caller takes this only when the slot's
    folded record is EMPTY, which is exactly the upgrade path: state written while
    the record was a file of its own, for a workstream still in flight. Once the
    carried entry lands the record has content, so it is never taken again.

    What it appends is :func:`_legacy_carry_payload`'s entry, which a READ also folds
    in unappended so the record does not go dark between the upgrade and this call.
    The claim, the document and the marker are *source*'s (:func:`_legacy_source`).
    """
    source = _legacy_source(slot_key, alias)
    claim = _claim_carry(source)
    if claim == _CLAIM_BUSY:
        # Another caller is carrying right now. Recording past it would append this
        # update FIRST and let the carry's legacy goal and phase apply over it, so the
        # caller is sent back instead; the retry finds the carry committed.
        raise LedgerUnavailable(
            "this slot's earlier ledger state is being carried into its crew log; "
            "try the update again"
        )
    if claim != _CLAIM_TAKEN:
        return False
    try:
        legacy = _legacy_document(source)
    except LedgerUnavailable:
        # RELEASED before propagating: the claim is this call's, and leaving it standing
        # would make the retry see a fresh claim, answer BUSY, and refuse again until the
        # marker went stale.
        _finish_carry(source, landed=False)
        raise
    if not _has_content(legacy):
        # RELEASED, not committed: empty may be a transient read failure, and
        # committing would skip real state for good.
        _finish_carry(source, landed=False)
        return False
    data = _legacy_carry_payload(slot_key, legacy)

    from kiro_crew.crew_log import emit as crew_log_emit

    refused_before = crew_log_emit.dropped_writes()
    seq_before = _unit_last_seq(session_id)
    crew_log_emit.on_ledger_recorded(session_id, data)
    drained = crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    # THIS unit's own log must have grown before the claim commits; the process-wide
    # counter alone could mark the document consumed when it was not carried.
    seq_after = _unit_last_seq(session_id)
    landed = _grew(seq_before, seq_after)
    if not drained or not landed or crew_log_emit.dropped_writes() != refused_before:
        # Refuse: folding a base missing the carry and writing over the gap would
        # order this update ahead of the state it extends.
        if drained and not landed and seq_before is not None and seq_after is not None:
            # PROVED it never lands (queue empty, seq read twice and unmoved), so
            # RELEASE and let the retry carry at once.
            _finish_carry(source, landed=False)
        # Otherwise the append may still be QUEUED (or a seq was unreadable), so the
        # claim stays ``pending``: releasing would let the retry carry a SECOND time,
        # and two carried entries are PERMANENT. Retries get BUSY until the append
        # lands (the fold is then non-empty) or the claim goes stale after
        # _CARRY_STALE_SECS and a take-over carries.
        raise LedgerUnavailable(
            "this slot's earlier ledger state is still being carried into its crew log; "
            "try the update again"
        )
    try:
        _finish_carry(source, landed=True)
    except (ValueError, OSError):
        # TOLERATED, unlike in a delete: the append is proved landed, so the
        # non-empty fold refuses a second carry and the ``pending`` claim goes stale.
        logger.warning(
            "ledger: carried the document but could not commit the marker; the claim "
            "will go stale and the non-empty record refuses a second carry",
            exc_info=True,
        )
    logger.info("ledger: carried a pre-projection document into the crew log")
    return True


def _require_crew_log(session_id: str) -> Any:
    """The fold package, once this session is known to HAVE a crew log.

    The emitter silently no-ops for a session with no crew log, which is wrong for an
    explicit ledger update, so the log's existence is checked here where the caller
    can be told.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    if not crew_log_emit.enabled():
        raise LedgerUnavailable(
            "the session ledger is recorded in this session's crew log, which is "
            f"switched off because {crew_log_emit.CREW_LOG_ENV} is set to 0, false, no, off or an "
            f"unrecognised value; unset it (or remove it from {env_file_display()}) and "
            "restart the gateway to record one"
        )
    projection = _projection()
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import CrewLog

    try:
        present = CrewLog.exists(KIND_SESSION, session_id)
    except Exception as exc:
        raise LedgerUnavailable(f"this session's crew log could not be read: {exc}") from exc
    if not present:
        raise LedgerUnavailable(
            "this session has no crew log yet, so there is nothing to record into; "
            "it is created on the session's first turn"
        )
    return projection


def _entry_data(
    slot_key: str,
    *,
    goal: str | None,
    phase: str | None,
    next_step: str | None,
    tried_approach: str | None,
    tried_rejected_because: str | None,
    artifacts: dict[str, str] | None,
    event: str | None,
    event_kind: str | None,
) -> dict[str, Any]:
    """One entry's ``data``: only the fields this call set, each clamped.

    An omitted field is left OUT rather than written empty, because the fold reads
    presence as "changed" -- writing every field on every entry would make a call
    that set only ``next`` also assert an empty goal, and the record would lose the
    goal it never touched.
    """
    data: dict[str, Any] = {"slot": _clamp(slot_key)}
    if goal is not None:
        data["goal"] = _clamp(goal)
    if phase is not None:
        data["phase"] = _clamp(phase, _MAX_PHASE)
    if next_step is not None:
        data["next"] = _clamp(next_step)
    if tried_approach:
        data["tried"] = {
            "approach": _clamp(tried_approach),
            "rejected_because": _clamp(tried_rejected_because or ""),
        }
    if artifacts:
        # Bounded HERE as well as in the fold. The fold trims the merged mapping to
        # the same limit, which bounds the RECORD -- but not the line this appends,
        # and one call handing over ten thousand pointers would write a line every
        # future fold of this slot has to parse forever. An append-only log cannot
        # take that back, so the count is cut before the entry is built.
        pointers = {
            _clamp(key, _MAX_ARTIFACT_KEY): _clamp(value)
            for key, value in list(artifacts.items())[:_MAX_ARTIFACTS]
            if isinstance(key, str) and isinstance(value, str)
        }
        if pointers:
            data["artifacts"] = pointers
    if event and event.strip():
        kind = (event_kind or "").strip()
        data["event"] = _clamp(event.strip())
        # Coerced here, at the writer, so the declared vocabulary can be a CLOSED
        # enum the append path enforces: no call site can widen it, and an
        # unrecognized kind costs the event its filter rather than its record.
        data["event_kind"] = kind if kind in EVENT_KINDS else "note"
    return data


def purge_matching(exact_keys: set[str], *, guard: Any) -> int:
    """Purge the ledgers whose breadcrumb holds one of *exact_keys*, guarded.

    Best-effort maintenance API (one production caller, ``ledger_sweep.purge``).
    EXACT keys only, with no fold parameter: a caller-supplied fold could remove a
    ledger it never listed. Callers must establish every key is safe to remove.

    *guard* is REQUIRED and is called as ``guard(dir_path)`` INSIDE that ledger's
    :func:`_locked` hold; the store goes only if it answers true (pass
    ``lambda _dir: True`` explicitly to skip). Selection outside the lock is a
    snapshot, and a resumed session can write a live phase before the delete, so the
    re-check and the removal share one hold.

    Removal order: other entries, then ``state.json`` and ``slot_key`` only once all
    else went (a failure never leaves a store without its record or name), then the
    lock file while still held (:func:`unlink_lock_in_hold`), so a queued writer
    refuses (:func:`require_lock_inode`) and no second inode is handed out. Windows
    unlinks after release instead. A partly removed store stays identifiable and is
    not counted.
    """
    removed = 0
    try:
        root = _ledger_root()
        if not root.is_dir():
            return 0
        children = list(root.iterdir())
    except OSError:
        return 0
    for child in children:
        try:
            if child.name == _CONTROL_DIR_NAME:
                # Not a store: it holds the files that govern folds, and a purge that
                # removed a slot's exclusions would let a recycled slot key fold the
                # units that purge was called to make unreadable. It carries no
                # breadcrumb either, so this only makes the skip explicit.
                continue
            if not child.is_dir() or is_link(child):
                # A linked store directory names somewhere else; the delete
                # would land there. Never followed, whatever its breadcrumb says.
                continue
            key = (child / _KEY_FILE).read_text(encoding="utf-8").strip()
            if not key:
                continue
            if key not in exact_keys:
                continue
            try:
                lock_cm = _locked(child, create=False)
                lock_cm.__enter__()
            except FileNotFoundError:
                # Removed between the listing and the lock -- by a concurrent
                # sweep, or by the writer that owned it. Nothing to do, and
                # nothing must be created in its place.
                continue
            try:
                if not guard(child):
                    continue
                if not _remove_store_contents(child):
                    # Something survived. When it was ordinary content, both
                    # identity files were kept; when it was one of the identity
                    # files themselves, whichever still exists is what names the
                    # store to the next sweep. Say exactly which, so the log is
                    # true in both cases.
                    surviving = [n for n in (_STATE_FILE, _KEY_FILE) if (child / n).exists()]
                    logger.warning(
                        "ledger purge: %s not fully removed; kept: %s",
                        child.name,
                        ", ".join(surviving) or "(no identity file survived)",
                    )
                    continue
                lock_gone = unlink_lock_in_hold(child / _LOCK_FILE)
            finally:
                lock_cm.__exit__(None, None, None)
            _remove_store_shell(child, lock_gone=lock_gone)
            removed += 1
        except Exception:
            continue
    return removed


#: The two files that make a store a ledger and let a purge NAME it. They go
#: last, and only when everything else is gone.
_IDENTITY_FILES = frozenset({_STATE_FILE, _KEY_FILE})


def _remove_store_contents(dir_path: Path) -> bool:
    """Delete *dir_path*'s contents except the lock file. Returns whether all went.

    Record and breadcrumb go LAST. Every failure is counted, because
    ``rmtree(ignore_errors=True)`` hides a subtree left standing (a Windows sharing
    violation); ``state.json`` and ``slot_key`` go only at zero failures, so a
    partial removal stays addressable by key for the next sweep. Call under the hold.
    """
    failures = 0

    def _count(_fn: object, _path: object, _exc: object) -> None:
        nonlocal failures
        failures += 1

    try:
        children = list(dir_path.iterdir())
    except OSError:
        return False
    for child in children:
        if child.name == _LOCK_FILE or child.name in _IDENTITY_FILES:
            continue
        # A linked entry is unlinked as a NAME, never followed: ``is_dir`` is true
        # through a link to a directory, and walking it would delete the target.
        if child.is_dir() and not is_link(child):
            shutil.rmtree(child, onerror=_count)
        else:
            try:
                child.unlink()
            except OSError:
                failures += 1
    if failures:
        return False
    for name in (_STATE_FILE, _KEY_FILE):
        try:
            (dir_path / name).unlink(missing_ok=True)
        except OSError:
            return False
    return True


def is_link(path: Path) -> bool:
    """Whether *path* is a symbolic link or a Windows junction -- a name that
    points somewhere else.

    A delete primitive must never FOLLOW one: a store directory, or an ``items/``
    inside one, that is a link would send the removal at whatever the link names,
    and nothing about the store's own records could tell. Both stores' purges
    refuse a linked store and a linked ``items/``, and both content walkers unlink
    a linked entry itself rather than descending into it.
    """
    return path.is_symlink() or path.is_junction()


def unlink_lock_in_hold(lock_path: Path) -> bool:
    """Unlink *lock_path* while its lock is still HELD. Returns whether it went.

    Unlinked inside the hold, a queued writer's :func:`require_lock_inode` check
    finds the path gone and refuses. Unlinked AFTER release, a queued writer could
    validate the old inode, then lose it to the late unlink, and the next writer
    would take a new inode un-serialised against it.

    POSIX allows this (``True``). Windows refuses (``False``) and the caller unlinks
    after release, which is safe there because it fails while any handle is held.
    """
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _remove_store_shell(dir_path: Path, *, lock_gone: bool) -> None:
    """Remove the now-empty directory, and the lock file ONLY if the hold could not.

    Call AFTER releasing; *lock_gone* is :func:`unlink_lock_in_hold`'s answer. When
    true the lock path MUST NOT be touched again: a refused writer may already hold a
    FRESH lock there, and a second unlink would detach it. Only ``rmdir`` runs, which
    fails if the store was rebuilt. When false (Windows), the late unlink is safe
    because it fails while any handle is held.
    """
    if not lock_gone:
        try:
            (dir_path / _LOCK_FILE).unlink(missing_ok=True)
        except OSError:
            logger.debug("ledger purge: lock file still held; leaving it")
    try:
        dir_path.rmdir()
    except OSError:
        logger.debug("ledger purge: ledger directory not fully removed")


#: Ceiling for the injected snapshot block. A nudge turn carries this every
#: cycle, so it must stay small even against a clamped-but-full record.
_SNAPSHOT_MAX_CHARS = 1600
_SNAPSHOT_FIELD_MAX = 300
_SNAPSHOT_TRIED_TAIL = 3


def render_snapshot(slot_key: str) -> str:
    """Compact ``[work ledger]`` block for per-cycle injection, or ``""``.

    Empty when the session has recorded nothing, or when the workstream is
    finished — a terminal ledger has nothing to steer. ONE fold, not a probe
    followed by a read: the record has no file to stat, so asking whether it
    exists and asking what it says are the same question. Callers on an event loop
    must dispatch this to a worker thread — it reads files.
    """
    state = read_state(slot_key)
    if not _has_content(state):
        return ""
    if state["phase"] in TERMINAL_PHASES:
        return ""

    def _field(v: str) -> str:
        v = " ".join(v.split())
        return v[:_SNAPSHOT_FIELD_MAX]

    lines = [
        "[work ledger — durable state for this session; authoritative over memory of prior cycles]"
    ]
    if state["goal"]:
        lines.append(f"goal: {_field(state['goal'])}")
    if state["phase"]:
        lines.append(f"phase: {_field(state['phase'])}")
    if state["next"]:
        lines.append(f"next: {_field(state['next'])}")
    for item in state["tried"][-_SNAPSHOT_TRIED_TAIL:]:
        why = f" (rejected: {_field(item['rejected_because'])})" if item["rejected_because"] else ""
        lines.append(f"tried: {_field(item['approach'])}{why}")
    for k, v in state["artifacts"].items():
        lines.append(f"artifact {k}: {_field(v)}")
    block = "\n".join(lines)
    if len(block) > _SNAPSHOT_MAX_CHARS:
        block = block[: _SNAPSHOT_MAX_CHARS - 1] + "…"
    return block


# ── The split-out owners ──
# This module keeps the record API: the fold and read, the write, the upgrade carry, the
# legacy-store purge and the snapshot. The vocabulary, layout and store lock live in
# ``session_ledger_layout`` and the per-slot control files in ``session_ledger_control``.
# Every name an owner defines still resolves as ``kiro_crew.session_ledger.<name>``, and a
# write through this module (``monkeypatch.setattr``, ``mock.patch``) reaches every owner
# holding the name (``kiro_crew._facade_mirror``). No owner imports this module; the control
# owner imports the layout owner. The owners log as ``kiro_crew.session_ledger``.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.session_ledger_layout",
    "kiro_crew.session_ledger_control",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.session_ledger_control import (  # noqa: F401
        LedgerExclusionError,
        SlotExclusion,
        _causal_units,
        _create_exclusive,
        _read_lines,
        _retainable_unit,
        _rewrite_lines,
        _settle_carry_locked,
        _withdraw_tombstone_locked,
        exclude_units,
        newest_work_unit,
        note_panel_unit_recorded,
        note_work_unit_recorded,
        panel_crew_log_units,
        unexclude_units,
        work_crew_log_units,
    )
    from kiro_crew.session_ledger_layout import (  # noqa: F401
        _CARRY_COMMITTED,
        _CARRY_PENDING,
        _CARRY_STALE_SECS,
        _CLAIM_DONE,
        _DELETED_UNITS_FILE,
        _LOCK_POLL_SECS,
        _LOCK_TIMEOUT_SECS,
        _MAX_EVENT_TAIL,
        _MAX_EVENTS,
        _MAX_EXCLUDED_BYTES,
        _MAX_EXCLUDED_UNITS,
        _MAX_ORDER_BYTES,
        _MAX_ORDER_READ_BYTES,
        _MAX_ORDERED_UNITS,
        _MAX_STATE_BYTES,
        _MAX_TEXT,
        _MAX_TRIED,
        _MAX_UNIT_ID_BYTES,
        _PANEL_UNIT_ORDER_FILE,
        _STORE_NAME_READABLE_MAX,
        _STORE_NAME_UNSAFE,
        _UNIT_ORDER_FILE,
        _WORK_UNIT_ORDER_FILE,
        SCHEMA_VERSION,
        _store_name,
        control_dir,
        ledger_dir,
        ledger_key,
        require_lock_inode,
        resolved_within,
    )
