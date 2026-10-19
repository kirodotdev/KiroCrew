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

    *slot_key* is the CANONICAL spelling — the one a unit header records — and *alias*
    is the caller's own spelling when it differs (see :func:`canonical_slot`), joined
    so a record written under it keeps reading.

    ``()`` when the slot has none: a session that never recorded, or a gateway whose
    crew log is switched off. Every failure to LIST them answers the same way,
    because this runs on the read path of a loop cycle and a listing that cannot be
    made must not raise into one.

    *strict* is for a caller that decides an OBLIGATION from the listing rather than
    reading from it. For a reader, an unlistable slot and a slot with no units are
    the same empty record; for a caller asking "is there a unit a later fact must go
    into", they are opposite answers, and the empty one reports an obligation
    discharged that was never looked for. ``strict`` raises instead, and is carried
    DOWN to both store lookups -- the canonical spelling's and the alias's -- because
    the scan that can fail is theirs: a flag that only re-raised from this function's
    own ``except`` would never fire, since the store swallows its own scan failure
    and answers empty before anything here sees it.

    A session root that does not EXIST is not a failure under ``strict`` either. A
    store nothing has written yet holds no unit for any slot, which is the same
    answer a reader gets, and raising there would make every dismissal on a fresh
    install retryable forever. Only a root that exists and could not be read, or a
    unit that cannot be proved while holding entries, is the indeterminate case.

    One link of this chain is deliberately NOT strict-gated: ``_recorded_unit_order``
    answers ``()`` when its own file cannot be read, which reorders the units and
    cannot drop one. The caller that matters here searches all of them, so an
    unknown order cannot turn a unit that holds a row into "no unit holds it".
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
        if excluded:
            units = tuple(unit for unit in units if unit not in excluded)
        recorded = _recorded_unit_order(slot_key)
        if recorded:
            # The order log keeps the NEWEST ids, so anything absent from it is older
            # than everything in it: a unit that predates the log, one whose append
            # failed, or one evicted when the log filled. All of them therefore apply
            # BEFORE the kept tail. Putting them after it is what would let a slot's
            # 65th-oldest unit land last and write its stale goal and phase over the
            # current ones. A unit nothing recorded into contributes no entries, so its
            # position among them is immaterial; header order is kept for them.
            known = [unit for unit in recorded if unit in units]
            rest = [unit for unit in units if unit not in recorded]
            units = tuple(rest + known)
        if live_session_id and live_session_id in units:
            # The LIVE unit applies LAST, whatever the clock says. Units are ordered by
            # the wall clock their headers carry, and a clock that moves backward before
            # a replacement unit is created sorts that replacement BEFORE its
            # predecessor -- so a retired session's goal and phase would apply over the
            # current ones, which is the one inversion that changes what a resume reads.
            # Pinning the unit being written to the end removes it, and the fold cannot
            # be wrong about which unit that is, because its caller is inside it.
            units = tuple(u for u in units if u != live_session_id) + (live_session_id,)
        return units
    except FileNotFoundError:
        # The session root has never been created, so no slot has a unit and this is
        # not indeterminate: it is the same empty answer a reader gets, and the only
        # one a store nothing has written can give. Raising here under ``strict``
        # would make every dismissal on a fresh install permanently retryable.
        return ()
    except Exception:
        # FAIL CLOSED to no units, which reads as the empty record. An exclusion list
        # that cannot be read is the case this matters for: answering with the units
        # anyway would serve a deleted conversation's state to whoever holds the slot
        # key now, and nothing later takes that back, while an empty record is
        # recovered by the next read that can see the list.
        #
        # A ``strict`` caller is not reading, it is deciding whether a later fact has
        # a unit to go into, and the empty answer would tell it there is none -- so it
        # gets the failure and can report a retryable outcome instead.
        if strict:
            raise
        logger.warning("ledger: could not list the crew logs for this slot", exc_info=True)
        return ()


def _fold_checkpoint(slot_key: str, units: "tuple[str, ...]") -> Any:
    """This slot's ledger fold, continued from where the last read left it.

    Folding is O(the log), not O(the record): the fold interprets only
    ``ledger/recorded`` entries but the reader still walks every line of every unit
    to find them, and a session's log carries its message bodies. A loop that reads
    the record on every wake would re-walk its whole history each time, which is the
    one cost the stored document did not have.

    The incremental fold belongs to the crew log and is shared with every other
    slot-keyed reader (:func:`kiro_crew.crew_log.projection.fold_slot_warm`): it keeps
    this fold's cell in memory per slot, continues it over the entries that arrived
    since, and refolds cold for every shape that cannot be carried -- a different unit
    list, a unit whose log was removed and recreated, an earlier unit that grew, a seq
    that went backwards. ONE implementation rather than one per consumer, because each
    of these readers has to enforce the same rules and a rule missing from one of them
    is a wrong record rather than a slow one.

    The cell is in memory, and the fold it holds is ALSO on disk beside the newest
    unit's log, so a second process -- and this one after a cache eviction -- resumes
    over the entries appended since instead of re-walking the history. Measured on a
    644 MB log: 70.2 s to fold cold against 11.6 s to resume. The file is the crew log's
    (``crew_log.checkpoint``'s slot section) and every condition that retires one is
    stated there; a missing, stale or unreadable savepoint folds cold, which is this
    read's own answer to doubt and reaches the same record at more cost.
    """
    return _projection().fold_slot_warm(_FOLD_NAME, units, slot=slot_key)


def _unit_last_seq(unit_id: str) -> int | None:
    """The newest seq in *unit_id*'s log as the file itself reports it.

    0 when the unit has no log, and ``None`` when its log could not be READ. The two
    are kept apart because a growth check needs opposite answers from them. A unit
    with no log has written nothing, so 0 is a fact about it. A log that refused to
    open says nothing about how far it has grown, and reading that as 0 makes the
    check wrong both ways: sampled after an append it reads as "did not grow", so a
    landed update is reported lost and its unit's precedence is never published;
    sampled before one it reads as "was empty", so the entries already there count as
    this append landing.

    A refusal that clears on its own (:func:`_clears_on_its_own`) is retried first,
    because Windows refuses an open for as long as another handle -- an indexer, a
    scanner -- holds the file in a way that excludes it, and that window is short.
    Bounded by a COUNT rather than by a clock, so one sequence of refusals gets one
    answer on every host. Any other failure answers ``None`` at once.
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

    *live_session_id* is the ACP session the caller is serving on, when it has one. It
    does two things a read cannot do without it: it resolves the caller's key to the
    slot identity the unit headers record (:func:`canonical_slot`), and it pins the
    unit being written LAST whatever the header clocks say.

    The record is stored nowhere: it is what the entries fold to. That is what makes
    the phase-requires-a-reason rule unbreakable rather than merely enforced -- there
    is no second document that can say a phase moved while the log says why it did
    not.

    The shape is the one every reader already expected of the stored document, so
    the MCP tool, the route and the injected snapshot did not have to learn a new
    one (see ``crew_log.projection._ledger_render``).

    Best-effort by contract, as this function has always been: a slot with no
    entries reads as the empty record, and so does a fold that cannot be made. A
    nudge cycle asks this on its way into a turn, and raising there would stop the
    loop instead of telling anyone anything. The two cases are distinguished in the
    LOG rather than in the answer -- an absent ledger is silent, a refused fold is a
    warning, because that one means a reader older than the writer.

    One exception to "no entries reads as empty", and it is the upgrade path: a slot
    whose state was written before the record became a fold has a document that no
    entry describes yet, and this read folds that document's PENDING carry in without
    appending it (:func:`_legacy_preview`). Without that, the first resumed turn after
    an upgrade -- which asks this before it records anything -- would run with no goal,
    and the carry that a later write performs cannot undo a turn already taken.

    Lock-free, and safe because the crew log is append-only: a reader sees a prefix
    of the truth, never a torn record.
    """
    key = slot_key or ""
    canonical = canonical_slot(key, live_session_id)
    units = crew_log_units(canonical, live_session_id, alias=key)
    try:
        # Folded even with NO units, which costs a zero-unit fold and answers the
        # question the branch below has to ask: a slot that ran before the record
        # became a fold has a document and no crew log at all, so returning early on
        # an absent unit would skip the carry preview in exactly the case it is for.
        base = _fold_checkpoint(canonical, units)
        value = _projection().projection_of(base).value
    except Exception:
        logger.warning("ledger: folding this slot's crew logs failed", exc_info=True)
        # The warm cell is not trusted after a failed fold: the failure may have been a
        # log this build cannot read, and a half-folded state must not become the answer
        # to the next read. Dropping it costs the next read a cold fold.
        _projection().forget_slot_folds(canonical, _FOLD_NAME)
        # NOT previewed here. A refused fold means a reader older than the writer, so
        # the log may hold entries newer than the document -- answering with the
        # document would assert something about a log this build could not read.
        return _empty_state()
    if _has_content(value):
        return value
    # An EMPTY folded record is the upgrade trigger, the same one the write path uses,
    # so the two agree on WHEN a carry is owed as well as on what it carries.
    return _legacy_preview(canonical, base) or value


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

    *session_id* is the crew log the entry lands in -- the ACP session the slot is
    serving on right now. A slot owns one such id at a time rather than for its
    whole life, so a slot's updates are spread over the units it ran under and the
    read joins them; the WRITE only ever needs the one it is in.

    Enforces the ledger discipline exactly as before: passing *phase* without
    *event* and a recognized *event_kind* is refused with ``ValueError``. The
    enforcement is now stronger than a rule, because a phase and its reason ride on
    the SAME entry -- there is no ordering in which a reader can see one without the
    other, and nothing for a crash to separate.

    Returns the record the appended entry produces, folded by the same fold every
    reader uses: the state on disk, advanced over this one entry. So the answer does
    not depend on the writer having already drained, and it is not a second
    implementation of the update rules -- it is the fold, applied to an entry that is
    on its way to the file.

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
    # The caller's own spelling is kept as an ALIAS and the unit header's slot becomes
    # the key everything else uses, so the append and every later read agree on one
    # identity. Without this a caller keyed `cron:<id>` or `hook:<id>` appends into a
    # unit headed by the real slot key and can never read its own record back.
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
    # Read BEFORE the append, and fold the entry in below rather than re-reading
    # after it: the writer is asynchronous, so a read-back would race the drain and
    # answer with the record as it was a moment ago -- reporting a phase the caller
    # just set as unset.
    base = _fold_checkpoint(slot_key, units)
    # An upgrade CARRY-FORWARD, at most once per slot. A slot with state written
    # before the record became a fold has a document no entry describes, and a loop
    # resuming into this build would otherwise find its goal, phase and next step
    # gone. Carried as an ENTRY rather than read as a fallback, so the log stays the
    # single authority and the document is consumed instead of consulted.
    #
    # The trigger is an EMPTY FOLDED RECORD, not an absent unit: a session's crew log
    # is created on its first turn, so a slot that has recorded nothing still has a
    # unit, and keying on the unit would never carry anything at all.
    if not _has_content(projection.projection_of(base).value) and _carry_legacy_forward(
        slot_key, session_id
    ):
        units = crew_log_units(slot_key, session_id)
        base = _fold_checkpoint(slot_key, units)
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.schema import Entry

    # Sampled BEFORE the append, so both checks bracket it. A second gateway owning
    # this unit makes the append inline rather than queued, and an inline refusal
    # increments the counter during the call below -- sampling afterwards folds that
    # increment into the baseline and reports the refused write as durable, which is
    # the one false positive this check exists to catch.
    refused_before = crew_log_emit.dropped_writes()
    seq_before = _unit_last_seq(session_id)
    # REFUSED here rather than reported as recorded. The writer's own ceiling refusal
    # counts the entry and drops it, which arrives as ``durable=False`` -- the same
    # answer a writer that is merely still busy gives, and that one IS recorded and
    # does land. Telling them apart matters because they need opposite answers: a busy
    # writer's update is taken and the caller may proceed, while an entry over the
    # ceiling can never land however often it is retried, so a success would promise a
    # record that never exists and vanishes from the slot at the next read. Only the
    # size is knowable before the append, so only the size is decided here.
    if not crew_log_emit.ledger_entry_fits(data):
        raise LedgerEntryTooLarge(
            "this update does not fit one crew log entry; record fewer or shorter "
            "fields (artifacts are the usual cause)"
        )
    # The stamp the record below is DERIVED from, sampled here rather than after the
    # wait. The store stamps the entry it writes from its own clock, so these are two
    # samples either way -- this one cannot be the log's value. What the position buys
    # is the direction and the size of the gap: taken before the append it is never
    # LATER than the entry the log holds, and the gap is the queue-to-write latency
    # instead of the whole drain, which on a slow writer is the flush budget.
    recorded_ms = int(time.time() * 1000)
    crew_log_emit.on_ledger_recorded(session_id, data)
    # WAIT for the append, and say precisely what the wait proves. ``record`` answers
    # a caller that is about to act on the record, so a queued entry is not good
    # enough: the writer is asynchronous and an unclean death inside the
    # queue-to-flush window would lose an update this call already reported as taken.
    # The route runs this on a worker thread, so the wait costs no event-loop time.
    #
    # A drained queue is NOT the same claim as "this entry landed". ``flush`` answers
    # False only when the writer is still busy past the budget, in which case the
    # entry is queued rather than lost; a PERMANENTLY REFUSED append drains the job
    # and is counted instead, so it takes the True branch. Both are reported, and the
    # refusal count is sampled around this append to see it at all.
    #
    # Neither is raised. The entry is either already queued (a retry would append the
    # same update twice) or the counter is process-wide and a concurrent session's
    # refusal would be attributed here, so refusing on it would reject a good write.
    # The loss is logged, surfaced by ``dropped_writes()``, and superseded by the next
    # update, which re-reads the base from disk.
    drained = crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    # THIS UNIT's own log has to have grown, which is the part the process-wide
    # refusal counter cannot say. A counter that did not move proves only that no
    # append anywhere was refused; a file whose newest seq did not move proves that
    # nothing was written HERE, whatever the counter says. Both are required, so the
    # remaining false positive needs a concurrent append into the SAME unit -- the
    # same conversation writing twice at once -- rather than any session anywhere.
    # A sample that could not be read proves nothing either way, so it is NOT landed.
    seq_after = _unit_last_seq(session_id)
    landed = _grew(seq_before, seq_after)
    unread = seq_before is None or seq_after is None
    durable = drained and landed and crew_log_emit.dropped_writes() == refused_before
    # PUBLISH PRECEDENCE ONLY ONCE THIS UNIT'S LOG HAS ACTUALLY GROWN. This call must
    # stay AFTER ``landed`` is computed, and the order is load-bearing rather than
    # stylistic: the order file's whole claim is "among units that have recorded, the
    # one recording right now holds the newest entry", and a refused or still-queued
    # append means this unit did NOT record. Moving it last on that path pins a
    # RETIRED unit ahead of its successor, and the rewrite is fsynced immediately, so
    # no crash is needed for the wrong order to persist -- a successor resuming with
    # no ledger content of its own then folds the retired unit's stale goal and phase
    # as current and acts on it. That is the inversion this file exists to prevent.
    #
    # Gated on ``landed`` rather than ``durable`` deliberately. ``durable`` also
    # requires ``drained`` and an unmoved refusal counter, and that counter is
    # PROCESS-WIDE, so a concurrent session's refusal would suppress a precedence
    # note this unit had genuinely earned. ``landed`` is exactly the fact the file
    # asserts: this unit's own newest seq moved.
    #
    # A log that could not be read on either side leaves the move unproved, and the
    # note is withheld then too: it is a claim that this unit recorded, and an unproved
    # claim is not made. A missing note is also the state a crash between an entry and
    # its note already leaves, which this unit's next proved record corrects.
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
    # RETURNED, never stashed. A module-level flag keyed by slot is a second piece of
    # state to keep in step with this call: the caller reads it under ITS spelling
    # while this writes the canonical one, a missing key has to mean something, and a
    # concurrent update on the same slot lands between the write and the read. Handing
    # it back with the record it describes removes all three questions. It rides BESIDE
    # the record rather than inside it, because every reader of the record expects its
    # ten fields and durability is a fact about one call, not about the workstream.
    return state, durable


def record(slot_key: str, **kwargs: Any) -> dict[str, Any]:
    """:func:`record_update`'s record alone, for a caller that cannot act on durability.

    A caller that ignores durability is no worse off than one that never asked: the
    update is folded either way and the next one supersedes it.
    """
    return record_update(slot_key, **kwargs)[0]


def _legacy_carry_payload(slot_key: str, legacy: dict[str, Any]) -> dict[str, Any]:
    """The entry a carry appends for *slot_key*, built from its *legacy* document.

    Shared by the carry and by the read below, so the record a reader sees before the
    carry has run and the record it sees afterwards come out of ONE construction. Two
    spellings of this would drift, and the drift would be invisible: the two answers
    are never compared, they are seen minutes apart by the same loop.

    What it carries is the record a resume needs -- goal, phase, next, artifacts, and
    the newest rejected approach -- plus an event that says where it came from and
    names what it could not bring: the entry shape holds ONE ``tried``, so an older
    document's earlier approaches and its event tail are counted in that event rather
    than dropped in silence. Carrying them all would mean an append per row, which is
    unbounded work on a path that runs inside a turn.

    The phase rides with that event, so the carried entry keeps the invariant the
    whole record rests on: a phase never appears without a logged reason.
    """
    data: dict[str, Any] = {"slot": _clamp(slot_key)}
    for field, key in (("goal", "goal"), ("next", "next")):
        value = legacy.get(key)
        if isinstance(value, str) and value:
            data[field] = _clamp(value)
    phase = legacy.get("phase")
    if isinstance(phase, str) and phase:
        data["phase"] = _clamp(phase, _MAX_PHASE)
    artifacts = legacy.get("artifacts")
    if isinstance(artifacts, dict) and artifacts:
        data["artifacts"] = {
            _clamp(k, _MAX_ARTIFACT_KEY): _clamp(v)
            for k, v in artifacts.items()
            if isinstance(k, str) and isinstance(v, str)
        }
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
    left = []
    if dropped_tried > 0:
        left.append(f"{dropped_tried} earlier rejected approach(es)")
    if dropped_events > 0:
        left.append(f"{dropped_events} event(s)")
    note = "carried forward from this slot's pre-projection ledger document"
    if left:
        note += "; not carried: " + " and ".join(left)
    data["event"] = _clamp(note)
    data["event_kind"] = "note"
    return data


def _legacy_preview(slot_key: str, base: Any) -> dict[str, Any] | None:
    """The record *slot_key*'s pending carry WOULD fold to, or ``None`` for no carry.

    The read half of the upgrade carry. A carry appends, so it can only run from a
    write: it needs a crew log to append into, it takes an exclusive claim, and it
    refuses its caller when another holder is mid-carry. A READ has none of those --
    :func:`read_state` may be asked with no live session at all, and it must not
    raise -- so the first read after an upgrade would answer the empty record while
    the document sat there waiting for a write to consume it. That read is the
    auto-nudge snapshot, so the first resumed turn would act with no goal, no phase
    and no next step, and the carry that runs later cannot undo a turn already taken.

    So the read FOLDS the pending entry in without appending it, over the same base,
    through the same projection, from the same payload the carry builds. The answer is
    the post-carry record by construction rather than by agreement, and the carry
    stays the only thing that makes it PERMANENT -- which is why this is not a second
    authority: it publishes nothing, decides nothing, and stops answering the moment
    the log has the entry.

    Three things stop it resurrecting state the log has settled. A COMMITTED marker
    ends it, which is what a finished carry leaves and also what a permanent delete
    leaves when it tombstones the slot, so a recycled slot key cannot see a deleted
    conversation's preserved document. A caller only reaches here with an EMPTY folded
    record, so this can never override anything the log recorded. And it folds ONTO
    the base rather than replacing it, so entries that carry events but no content --
    a slot that recorded a reason and no state -- keep them.

    Never raises and never grows past the document: :func:`_legacy_document` refuses a
    file over the size ceiling, and every failure here answers ``None``, which is the
    empty record the caller already had.
    """
    try:
        if _carry_committed(_control_file(slot_key, _CARRIED_FILE)):
            return None
        legacy = _legacy_document(slot_key)
    except LedgerUnavailable:
        # UNREADABLE, which is not empty -- but a read cannot do anything about it and
        # must not raise into a nudge cycle. Warned rather than silent, because the
        # carry is still owed and the next write will report it properly.
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


def _carry_legacy_forward(slot_key: str, session_id: str) -> bool:
    """Append the pre-projection document as this slot's first entry. Once.

    Returns whether anything was carried. The caller takes this only when the slot's
    folded record is EMPTY, which is exactly the upgrade path: state written while
    the record was a file of its own, for a workstream still in flight. Once the
    carried entry lands the record has content, so it is never taken again.

    What it appends is :func:`_legacy_carry_payload`'s entry, which a READ also folds
    in unappended so the record does not go dark between the upgrade and this call.
    """
    claim = _claim_carry(slot_key)
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
        legacy = _legacy_document(slot_key)
    except LedgerUnavailable:
        # RELEASED before propagating: the claim is this call's, and leaving it standing
        # would make the retry see a fresh claim, answer BUSY, and refuse again until the
        # marker went stale.
        _finish_carry(slot_key, landed=False)
        raise
    if not _has_content(legacy):
        # RELEASED, not committed. An empty answer here is either a document with
        # nothing in it or one that could not be read, and those are indistinguishable
        # from here -- committing would permanently skip real state on a transient read
        # failure, which is the loss this two-phase claim exists to prevent.
        _finish_carry(slot_key, landed=False)
        return False
    data = _legacy_carry_payload(slot_key, legacy)

    from kiro_crew.crew_log import emit as crew_log_emit

    refused_before = crew_log_emit.dropped_writes()
    seq_before = _unit_last_seq(session_id)
    crew_log_emit.on_ledger_recorded(session_id, data)
    drained = crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    # THIS unit's own log has to have grown, for the same reason the ordinary update
    # checks it: a process-wide refusal counter cannot say whether this append landed,
    # and committing the claim on a weaker signal marks the document consumed when it
    # was not carried.
    seq_after = _unit_last_seq(session_id)
    landed = _grew(seq_before, seq_after)
    if not drained or not landed or crew_log_emit.dropped_writes() != refused_before:
        # The carry is owed rather than lost, but this call must not go on to fold a
        # base that is missing it and then write an update over the gap: that would
        # order the update ahead of the state it is meant to extend. Refusing sends
        # the caller back.
        if drained and not landed and seq_before is not None and seq_after is not None:
            # PROVED it can never land: the queue emptied and this unit's own seq, read
            # both times, did not move, so nothing of this carry is still in flight.
            # RELEASING is what lets the retry carry at once.
            _finish_carry(slot_key, landed=False)
        # Otherwise the append may still be QUEUED -- a flush that ran out of budget
        # leaves work behind, and the process-wide refusal counter cannot say whose
        # write it dropped. Releasing there would let the retry, which still reads an
        # empty fold, carry a SECOND time, and two carried entries are PERMANENT: the
        # fold applies both and nothing dedups them. So the claim stays ``pending``.
        # The retry is answered BUSY until either the queued append lands -- after
        # which the fold is not empty and no carry is attempted at all -- or the claim
        # goes stale after _CARRY_STALE_SECS and a take-over carries, which is safe for
        # that same reason. A refusal bounded by the staleness window is the cheaper
        # side of the trade against history that cannot be repaired.
        #
        # A seq that could not be READ is held the same way, since it proves neither
        # side: the carry may have landed in the very log that would not open, and a
        # retry whose fold cannot read that log either may see an empty record and
        # carry again.
        raise LedgerUnavailable(
            "this slot's earlier ledger state is still being carried into its crew log; "
            "try the update again"
        )
    try:
        _finish_carry(slot_key, landed=True)
    except (ValueError, OSError):
        # TOLERATED here, unlike in a delete. The append is already proved landed, so
        # the folded record is non-empty and the take-over rule refuses a second carry
        # on its own evidence; the claim left ``pending`` goes stale and is reclaimed.
        # Refusing the update instead would fail a carry that actually succeeded.
        logger.warning(
            "ledger: carried the document but could not commit the marker; the claim "
            "will go stale and the non-empty record refuses a second carry",
            exc_info=True,
        )
    logger.info("ledger: carried a pre-projection document into the crew log")
    return True


def _require_crew_log(session_id: str) -> Any:
    """The fold package, once this session is known to HAVE a crew log.

    The ledger writes through the emitter, which treats a session with no crew log
    as a policy no-op -- correct for a turn entry nobody asked for, and wrong for an
    update a person's agent explicitly recorded. So the existence of the log is
    established here, where the caller can be told, instead of being discovered as
    silence.
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

    This is an explicit best-effort maintenance API with one production caller
    (``ledger_sweep.purge``). It matches EXACT keys only, deliberately: a
    caller-supplied fold is exactly the one way a caller could remove a ledger
    it never listed, so there is no fold parameter, not even a defaulted one.
    Callers must establish that every key is safe to remove before invoking it.

    Every removal is locked, ordered and identity-last -- there is one spelling
    of deletion in this module. *guard* is REQUIRED: it is called as
    ``guard(dir_path)`` INSIDE that ledger's own :func:`_locked` hold, and the
    store is removed only if it answers true. A caller that truly wants the match
    alone to decide passes ``lambda _dir: True`` and says so at the call site;
    the store does not offer a default that skips the re-decision.
    That is what lets a caller re-read the record and stand down on one that
    came back to life: a selection made outside the lock is a snapshot, and
    between the snapshot and the delete a session can be resumed and write a
    live phase into the very record the caller decided was finished. Selecting
    under the lock is not enough on its own -- the removal has to happen in the
    same hold, which is why the guard is a callback rather than a filter the
    caller applies first.

    The removal is ORDERED and the lock inode goes inside the hold where the OS
    allows it: other entries first, then ``state.json`` and ``slot_key`` only
    once every other removal succeeded (a failure never leaves a store without
    its record or its name), then the lock file itself while the lock is still
    held (:func:`unlink_lock_in_hold`) -- so a writer queued on that inode
    finds the path gone when it acquires and refuses (:func:`require_lock_inode`)
    rather than publishing into the removed store, and no later writer can be
    handed a second inode while a first is still held. Windows refuses the
    in-hold unlink and gets it after release instead, which is safe there
    because it fails whenever a writer still holds a handle. A store that could
    not be fully removed is left identifiable and is not counted as removed.
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

    Ordered, with the record and the breadcrumb LAST. Everything else is removed
    first and every failure is counted -- ``rmtree(ignore_errors=True)`` would
    report success over a subtree it silently left standing, and on Windows a
    sharing violation on one held entry is exactly that case. ``state.json`` and
    ``slot_key`` are unlinked only once the count is zero, so a failed removal
    never leaves a store that has lost its record or its name: it stays a
    ledger, stays addressable by key, and reads as damaged to the next sweep
    rather than as a residue nothing can aim at. Call under the hold.
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

    The one order that keeps lock identity stable through a purge. Unlinked
    inside the hold, the inode a queued writer is waiting on is already detached
    from the path by the time that writer acquires it, so its
    :func:`require_lock_inode` check sees the path gone and refuses. Unlinked
    AFTER release there is a window in which a queued writer acquires the old
    inode, validates it against a path that still exists, and proceeds -- and the
    late unlink then detaches the very inode it holds, so the next writer creates
    a new one and the two are not serialised against each other.

    POSIX permits the unlink under an open descriptor and this returns ``True``.
    Windows refuses it and this returns ``False``; there the caller unlinks after
    release instead, which is safe on Windows precisely because it fails whenever
    any writer still holds a handle -- the OS keeps the identity stable, and a
    successful late unlink proves nobody was queued.
    """
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _remove_store_shell(dir_path: Path, *, lock_gone: bool) -> None:
    """Remove the now-empty directory, and the lock file ONLY if the hold could not.

    Call AFTER releasing. *lock_gone* is :func:`unlink_lock_in_hold`'s answer.
    When it is true the lock path was unlinked inside the hold and MUST NOT be
    touched again here: by now a writer that refused on the detached inode may
    have retried, recreated the directory and taken a FRESH lock at the same path
    -- a second unlink would detach that fresh inode under its holder, and the
    next writer would take a third, un-serialised against the second. Only the
    empty-directory ``rmdir`` is attempted, and it simply fails if a writer has
    rebuilt the store. When *lock_gone* is false the OS refused the in-hold unlink
    (Windows), and the late unlink is safe there because it fails whenever any
    writer holds a handle.
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
        _create_exclusive,
        _read_lines,
        _retainable_unit,
        _rewrite_lines,
        _settle_carry_locked,
        _withdraw_tombstone_locked,
        exclude_units,
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
