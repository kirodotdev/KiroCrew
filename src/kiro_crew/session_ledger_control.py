"""The per-slot control files that govern what a session ledger's fold reads.

The delete funnel's exclusion transaction (:func:`exclude_units`, :func:`unexclude_units`,
:class:`LedgerExclusionError`), the legacy carry's claim and tombstone, the ledger, work and
panel unit-order logs with their bounded read and rewrite, and the reader of a legacy
``state.json`` document.

An owner module of :mod:`kiro_crew.session_ledger`, the one import path: callers, tests and
documents reach every name here as ``kiro_crew.session_ledger.<name>``, and a patch through
that facade reaches this module (:mod:`kiro_crew._facade_mirror`). It imports
:mod:`kiro_crew.session_ledger_layout`, nothing here imports the facade, and it logs as
``kiro_crew.session_ledger``. New control-file work goes here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew.atomic_write import append_line, atomic_write, fsync_dir
from kiro_crew.session_ledger_layout import (
    _CARRIED_FILE,
    _CARRY_COMMITTED,
    _CARRY_PENDING,
    _CARRY_STALE_SECS,
    _CLAIM_BUSY,
    _CLAIM_DONE,
    _CLAIM_TAKEN,
    _DELETED_UNITS_FILE,
    _MAX_EXCLUDED_BYTES,
    _MAX_EXCLUDED_UNITS,
    _MAX_ORDER_BYTES,
    _MAX_ORDER_READ_BYTES,
    _MAX_ORDERED_UNITS,
    _MAX_STATE_BYTES,
    _MAX_UNIT_ID_BYTES,
    _PANEL_UNIT_ORDER_FILE,
    _STATE_FILE,
    _UNIT_ORDER_FILE,
    _WORK_UNIT_ORDER_FILE,
    LedgerUnavailable,
    _control_file,
    _empty_state,
    _locked,
    canonical_slot,
    control_dir,
    ledger_dir,
)

#: The ledger logs as ``kiro_crew.session_ledger``, the name callers and log filters use.
logger = logging.getLogger("kiro_crew.session_ledger")


class LedgerExclusionError(RuntimeError):
    """A slot's deleted units could not be recorded, so nothing may be deleted."""


# Tracebacks and logs name it by the import path its callers use.
LedgerExclusionError.__module__ = "kiro_crew.session_ledger"


class SlotExclusion(NamedTuple):
    """What one delete's exclusion transaction recorded, and so what it may take back.

    Two facts rather than one, because a rollback has two things to undo and each is
    only ever this transaction's: the unit ids it ADDED (an id a concurrent delete of
    the same slot already recorded is not its to remove) and whether it TOMBSTONED the
    slot's legacy document (a marker already committed when it ran is another call's
    proof that a carry landed, and is never its to withdraw).
    """

    added: "tuple[str, ...]"
    carry_tombstoned: bool


def exclude_units(
    slot_key: str, unit_ids: "tuple[str, ...]", *, refusable: bool = True
) -> SlotExclusion:
    """Record that *unit_ids* must never again be folded into *slot_key*'s record.

    Returns what this call recorded -- see :class:`SlotExclusion`. A delete with NO
    units is NOT nothing to do: the slot's legacy pre-projection document survives the
    delete (the age-based sweep is the only thing that removes it, and it may never
    run), so leaving its carry marker unsettled lets the next session on the recycled
    slot key claim it and carry the DELETED conversation's goal and phase into its own
    record. Only an empty *slot_key* returns early.

    Called by the permanent-delete funnel with every unit that delete proved belongs
    to the slot. The funnel removes the conversation's own unit; this covers the
    earlier units of the same slot, which survive it.

    Recording ids rather than deleting those units is deliberate. A unit is inside the
    fenced crew log tree and may still be open to a writer, and a slot key is recycled,
    so removing everything under the slot would take a live successor's log with it.
    An exclusion is additive, provable from the delete's own evidence, and reversible
    by hand if it is ever wrong.

    SERIALIZED on the slot's own lock and written as one deduplicated set, so two
    concurrent deletes of the same slot cannot lose each other's ids, and the file
    cannot accumulate repeats. The set is BOUNDED, and overflow FAILS CLOSED -- the
    exclusion is refused and so is the delete -- because dropping an id to stay under
    a bound is precisely the resurrection this file exists to prevent.

    RAISES :class:`LedgerExclusionError` when the record cannot be made, which aborts
    the delete before the transcript is unlinked. An undeleted conversation is visible
    and can be deleted again, while state resurrected into a stranger's session is
    neither. The carry tombstone is part of "the record": a slot whose marker is absent
    or still ``pending`` is a CLAIMABLE one, so a delete that proceeded on a failed
    tombstone would leave exactly the resurrection this function exists to prevent.

    A refusal LEAVES THE SLOT AS IT FOUND IT, and does it INSIDE the hold. The exclusion
    write, the tombstone and the take-back are ONE transaction on the slot's own lock,
    which is what makes the take-back safe rather than merely present: settling the
    tombstone after the release would publish the exclusion first, and a concurrent
    delete of the same slot would then reuse those ids, add none of its own, and UNLINK
    its transcript on the strength of an exclusion this call was about to withdraw --
    resurrecting the state that delete removed. From inside the hold no other delete can
    observe the intermediate state, so there is no reliance to break.

    *refusable* is False for a caller already past the transcript's unlink, which has
    nothing left to refuse. Its directory sync is then best-effort, because taking the
    ids back over a failed sync would leave the deleted conversation's unit neither
    excluded nor removed.
    """
    if not slot_key:
        return SlotExclusion((), False)
    added: "tuple[str, ...]" = ()
    try:
        path = _control_file(slot_key, _DELETED_UNITS_FILE, create=True)
        carried = _control_file(slot_key, _CARRIED_FILE)
        with _locked(control_dir(slot_key)):
            current = _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
            added = tuple(
                _retainable_unit(unit) for unit in dict.fromkeys(unit_ids) if unit not in current
            )
            # An EMPTY ``added`` is not nothing to do. It is the retry of a delete that
            # already recorded these units and then failed further on, so the work below
            # is done -- but the carry claim still has to be consumed, which is why that
            # happens after this block for both paths rather than behind an early return.
            if added:
                merged = (*current, *added)
                if len(merged) > _MAX_EXCLUDED_UNITS:
                    raise OSError(
                        f"slot would retain {len(merged)} excluded units, over the "
                        f"{_MAX_EXCLUDED_UNITS} bound"
                    )
                _rewrite_lines(path, merged)
                # Read back INSIDE the hold. A write that is buffered, short, or on a full
                # or read-only filesystem would otherwise pass silently.
                missing = set(added) - set(
                    _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
                )
                if missing:
                    raise OSError(f"exclusion did not persist for {sorted(missing)}")
            # The carry claim is CONSUMED by the delete, whatever state it was in. The
            # take-over rule rests on "a carry that landed makes the folded record
            # non-empty", and a delete removes the unit that held the carried entry -- so
            # after it the record is empty again and a marker left `pending` by a crash
            # between the append and its commit would let a later session take the claim
            # over and carry the preserved document into the recycled slot. That is the
            # resurrection the marker exists to prevent, so the delete settles the marker
            # rather than leaving evidence its own removal invalidated. A delete with no
            # units reaches here too, and MUST: the legacy document it tombstones is the
            # slot's, not any one unit's.
            #
            # INSIDE the hold, which is what makes this one transaction. Settling it
            # after the release would publish the exclusion first, and a concurrent
            # delete of the same slot would then reuse those ids, add none of its own,
            # settle the marker, and UNLINK its transcript on the strength of an
            # exclusion this call is about to take back -- resurrecting the state it
            # deleted. Nobody can observe the intermediate state from in here.
            tombstoned = False
            # Whether the take-back may withdraw the marker is decided from what the
            # marker WAS, not from ``tombstoned``: a commit whose read-back raises has
            # already renamed the marker into place without ever returning True.
            committed_before = True
            try:
                committed_before = _carry_committed(carried)
                tombstoned = _settle_carry_locked(carried, landed=True)
                # ONE sync of the control directory for both renames above. The caller
                # unlinks the transcript next, and a crash that kept that unlink while
                # losing the exclusion's rename would fold the deleted units into the
                # next session. A control directory this call created relies on the
                # filesystem to order its own entry in the parent.
                fsync_dir(path.parent, best_effort=not refusable)
            except (ValueError, OSError):
                # ROLLED BACK to the set this transaction FOUND, not by subtracting the
                # ids it added: inside the hold those are the same thing, and writing
                # back what was read cannot express anything else. The delete is refused
                # either way, so a rollback that does not persist is reported rather
                # than raised over the original failure.
                took_back = False
                if not committed_before:
                    # The tombstone THIS call committed goes too, or a refused delete
                    # would silence the still-live session's earlier state for good.
                    try:
                        took_back = _withdraw_tombstone_locked(carried)
                    except OSError:
                        logger.error(
                            "ledger: could not withdraw slot %r's carry tombstone after "
                            "the delete was refused; remove %r by hand to restore that "
                            "session's earlier state",
                            slot_key,
                            _CARRIED_FILE,
                            exc_info=True,
                        )
                if added:
                    _rewrite_lines(path, current)
                    took_back = True
                    if set(added) & set(
                        _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
                    ):
                        logger.error(
                            "ledger: could not take back slot %r's exclusions %s after "
                            "the delete was refused; a live session's record will read "
                            "empty until they are removed from %r by hand",
                            slot_key,
                            list(added),
                            _DELETED_UNITS_FILE,
                        )
                if took_back:
                    # The take-back is synced like the writes it undoes: a crash that
                    # kept their renames and lost this one would leave the live
                    # session's record excluded with nothing to say so.
                    try:
                        fsync_dir(path.parent)
                    except OSError:
                        logger.error(
                            "ledger: could not confirm slot %r's take-back reached disk "
                            "after the delete was refused; after a crash, check %r and "
                            "%r by hand",
                            slot_key,
                            _DELETED_UNITS_FILE,
                            _CARRIED_FILE,
                            exc_info=True,
                        )
                raise
        return SlotExclusion(added, tombstoned)
    except (ValueError, OSError) as exc:
        logger.error(
            "ledger: could NOT record slot %r's deleted units %s, so the delete is "
            "refused rather than leaving them foldable; exclude them by hand in %r",
            slot_key,
            list(unit_ids),
            _DELETED_UNITS_FILE,
            exc_info=True,
        )
        raise LedgerExclusionError(str(exc)) from exc


def unexclude_units(
    slot_key: str, unit_ids: "tuple[str, ...]", *, restore_carry: bool = False
) -> None:
    """Undo :func:`exclude_units` for *unit_ids*, for a delete that did not happen.

    Takes ONLY the ids that call reported adding. A rollback that dropped every id it
    was asked about would take back a concurrent delete's exclusions too, and that
    delete's units would then fold into the next session on the recycled slot key.

    With *restore_carry* -- passed only when that transaction reported TOMBSTONING this
    slot's legacy document -- the tombstone comes back out as well, because the session
    it was meant to silence still exists and its pre-projection state is still owed to
    it. A committed marker is otherwise permanent, so leaving one behind for a delete
    that did not happen would silence that live session's earlier state for good.

    The tombstone is lifted ONLY when the rewrite above leaves the slot with no
    exclusions at all. An id still recorded there is another delete's evidence that it
    proceeded, and the legacy document is the SLOT's rather than any one conversation's,
    so it must stay tombstoned. RESIDUE, named rather than implied: two concurrent
    deletes of one recycled slot key that both record no units, one of them rolling
    back, cannot be told apart this way, and the rollback lifts the other's tombstone.

    The exclusion is written BEFORE the transcript is unlinked, because that is the
    only point where a failure can still refuse something. When the delete then does
    not proceed, those units belong to a session that still exists and folding them is
    correct, so the exclusion has to come back out. The rewrite and the tombstone
    withdrawal share ONE hold on the slot's lock, and the rollback runs on the rare
    path only. Two holds would leave a gap in which a concurrent delete commits its own
    tombstone, and the withdrawal cannot tell that marker from the one it is undoing.

    A rollback that itself fails is reported at ERROR naming the file, and leaves a
    live slot's record reading empty until an operator edits it -- recoverable by hand,
    which the alternative ordering is not. A failed WITHDRAWAL is raised for the same
    reason rather than logged and dropped: the marker standing means the spared
    session's earlier state is never carried, and a raise is a retryable answer.
    """
    if not slot_key or not (unit_ids or restore_carry):
        return
    drop = set(unit_ids)
    try:
        if not control_dir(slot_key).exists():
            # Nothing was ever written for this slot, so there is nothing to take back
            # and no marker to withdraw. Returning here also keeps the lock acquire
            # below from bringing the directory into being on a pure rollback.
            return
        path = _control_file(slot_key, _DELETED_UNITS_FILE)
        carried = _control_file(slot_key, _CARRIED_FILE)
        # ONE hold over both writes, because two would leave a gap in which a concurrent
        # delete commits its OWN tombstone -- which the withdrawal cannot tell from the
        # one it is undoing, since a marker says `committed` and never says whose.
        # Removing that marker carries the other delete's legacy state into a recycled
        # slot, silently, with nothing left to put it back.
        with _locked(control_dir(slot_key), create=False):
            current = _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
            kept = tuple(unit for unit in current if unit not in drop)
            changed = kept != current
            if changed:
                _rewrite_lines(path, kept)
            if restore_carry and not kept:
                # NOT ``_finish_carry(landed=False)``: that releases a claim still
                # reading ``pending`` and deliberately leaves a committed marker alone,
                # so it would be inert against the very tombstone this is undoing.
                changed = _withdraw_tombstone_locked(carried) or changed
            if changed:
                # Best-effort: the rollback has already landed, so a failed sync must
                # not report a record that folds normally as one that reads empty.
                fsync_dir(path.parent, best_effort=True)
    except (ValueError, OSError) as exc:
        logger.error(
            "ledger: could NOT roll back slot %r's exclusion of %s after a delete that "
            "did not proceed; that session's record reads empty until %r is edited",
            slot_key,
            list(unit_ids),
            _DELETED_UNITS_FILE,
            exc_info=True,
        )
        # PROPAGATED: the caller answers retryable rather than 200, because a request
        # that reports success has told the person their session is intact while its
        # record reads empty.
        raise LedgerExclusionError(str(exc)) from exc


def _retainable_unit(unit_id: str) -> str:
    """*unit_id* if it is within :data:`_MAX_UNIT_ID_BYTES`, else RAISE.

    The bound is enforced HERE, at the boundary, rather than trusted of the input.
    Every control-file bound is derived from it, so an id past it makes a file outgrow
    the read sized for it -- and the order file's read does not reject an oversized
    file, it comes back short, while its caller rewrites what it read. So an unchecked
    id does not cost a bounded read: it costs the NEWEST ids permanently, and the
    caller then folds a retired session's record over a later one's.

    Refusing is the safe direction at both call sites. An exclusion turns it into a
    refused delete, which is what that file's count bound already does. The order log
    turns it into a missing line, which falls back to header order -- the same state a
    crash between the entry and the line leaves.
    """
    if len(unit_id.encode("utf-8")) > _MAX_UNIT_ID_BYTES:
        raise ValueError(
            f"unit id is {len(unit_id.encode('utf-8'))} bytes, over the "
            f"{_MAX_UNIT_ID_BYTES}-byte bound every control-file bound is derived from"
        )
    return unit_id


def _read_lines(
    path: Path, *, limit: int = _MAX_ORDER_READ_BYTES, reject_oversized: bool = False
) -> "tuple[str, ...]":
    """The file's non-empty lines, DEDUPLICATED, oldest first, bounded by one read.

    *limit* must be sized for the FILE being read, not shared between files with
    different bounds. With *reject_oversized* a file past the limit raises instead of
    coming back short: a caller that rewrites what it reads would otherwise persist the
    truncation, which is a silent loss rather than a bounded read.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            text = fh.read(limit + 1)
    except FileNotFoundError:
        return ()
    if len(text) > limit:
        if reject_oversized:
            raise OSError(f"{path.name} is larger than its {limit}-byte bound")
        text = text[:limit]
    return tuple(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))


def _excluded_units(slot_key: str) -> "frozenset[str]":
    """Unit ids a permanent delete excluded from this slot.

    RAISES when the list cannot be read. An empty answer would mean "nothing is
    excluded", which is the one wrong thing to say here: the caller would fold the
    units a delete excluded and serve a deleted conversation's goal and phase to
    whoever holds the slot key now, and nothing later undoes that. The caller turns a
    failure into an EMPTY RECORD instead, which is recoverable by the next read and
    tells the person nothing rather than telling them someone else's state.
    """
    try:
        return frozenset(
            _read_lines(
                _control_file(slot_key, _DELETED_UNITS_FILE),
                limit=_MAX_EXCLUDED_BYTES,
                reject_oversized=True,
            )
        )
    except (ValueError, OSError) as exc:
        raise LedgerExclusionError(f"slot {slot_key!r}'s exclusion list is unreadable") from exc


def _create_exclusive(path: Path, text: str) -> bool:
    """Create *path* holding *text*, or answer False because it already exists.

    ``O_EXCL`` is the whole point: the create is the claim, and the kernel picks one
    winner among however many callers race for it. A ``write_text`` would truncate an
    existing file instead of refusing, which is not a claim at all.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    return True


def _claim_carry(slot_key: str) -> str:
    """Claim the right to carry this slot's pre-projection document.

    Answers ``_CLAIM_TAKEN`` (this caller carries), ``_CLAIM_BUSY`` (another caller
    holds a live claim, so this one must not record yet) or ``_CLAIM_DONE`` (carried
    already, or there is nothing to carry).

    The marker is CREATED EXCLUSIVELY and holds ``pending`` until
    :func:`_finish_carry` proves the append reached disk. Creating it is the claim,
    not a flag set afterwards: checking a marker and then carrying is a plain race,
    and two first records on the same slot -- a resumed loop and its own dashboard
    tab -- would both read no marker and both carry, appending the legacy goal and
    phase twice. An exclusive create has exactly one winner however many callers
    arrive together.

    A ``pending`` marker older than :data:`_CARRY_STALE_SECS` is ABANDONED and may be
    taken over. Its holder's window is bounded by its own append wait, so past that
    the holder either committed -- in which case the marker says so and no take-over
    happens -- or died. Taking over cannot duplicate a carry: the caller reaches here
    only with an empty folded record, and a carry that landed makes it non-empty.
    """
    try:
        if not ledger_dir(slot_key).exists():
            # Nothing to carry, and nothing to claim: a slot with no legacy directory
            # never used the legacy layout.
            return _CLAIM_DONE
        path = _control_file(slot_key, _CARRIED_FILE, create=True)
        # READ, DECIDE and REPLACE under the slot's own lock. An exclusive create alone
        # picks one winner among callers arriving together, but the take-over path below
        # unlinks first -- and two callers that both saw one stale marker would each
        # unlink and each create, so both would carry and the later carry would apply the
        # legacy goal and phase over a newer update.
        with _locked(control_dir(slot_key)):
            if _create_exclusive(path, _CARRY_PENDING):
                return _CLAIM_TAKEN
            if _carry_committed(path):
                return _CLAIM_DONE
            if time.time() - path.stat().st_mtime < _CARRY_STALE_SECS:
                # Someone else's LIVE attempt, and this caller must not simply carry
                # on: its own update would be appended while that carry is still
                # queued, and the carry would then apply the legacy goal and phase
                # over it.
                return _CLAIM_BUSY
            logger.warning(
                "ledger: taking over slot %r's abandoned legacy-document claim; its "
                "holder neither committed the carry nor released the claim",
                slot_key,
            )
            path.unlink(missing_ok=True)
            return _CLAIM_TAKEN if _create_exclusive(path, _CARRY_PENDING) else _CLAIM_BUSY
    except (ValueError, OSError) as exc:
        # PROPAGATED, not answered DONE. Answering DONE lets this update be appended,
        # and an appended update makes the folded record non-empty -- after which the
        # carry is never attempted again and the legacy state is lost for good on a
        # transient filesystem error. Refusing sends the caller back with the carry
        # still owed.
        logger.warning("ledger: could not claim this slot's legacy document", exc_info=True)
        raise LedgerUnavailable(
            "this slot's earlier ledger state could not be claimed for carry; "
            "try the update again"
        ) from exc


def _carry_committed(path: Path) -> bool:
    """True when *path* holds a COMMITTED carry marker.

    One reader for both the claim and the release, so the two cannot come to
    different conclusions about the same bytes. A marker that is absent, empty, or
    half-written is NOT committed: the claim treats it as takeable-when-stale, and
    the release is then free to remove it.
    """
    try:
        return path.read_text(encoding="utf-8").strip() == _CARRY_COMMITTED
    except FileNotFoundError:
        return False


def _settle_carry_locked(path: Path, *, landed: bool) -> bool:
    """:func:`_finish_carry`'s decision, for a caller ALREADY holding the slot's lock.

    Split out so a delete can settle the marker inside the SAME hold as its exclusion
    write. The lock is not re-entrant -- a second acquire from this process blocks on
    its own holder -- so a caller inside the hold cannot go through
    :func:`_finish_carry`, and being inside it is what makes the two writes one
    transaction: a concurrent delete cannot observe a half-finished one, so a rollback
    can never take back an id another delete has already proceeded on.

    Raises on a commit that does not reach disk, for the reason :func:`_finish_carry`
    documents: ``False`` already means "already committed by another call".
    """
    if landed:
        # READ BEFORE WRITING, inside the hold: the answer is what makes a
        # rollback's take-back exact, and a blind write cannot tell "this call
        # tombstoned the slot" from "it was already committed".
        if _carry_committed(path):
            return False
        # FSYNCED and renamed into place, like every other control write here.
        # A marker that is only in the page cache is not a tombstone: the crash
        # it guards against is exactly the one that would lose it.
        _rewrite_lines(path, (_CARRY_COMMITTED,))
        # Read back INSIDE the hold, like the exclusion write. A short write on
        # a full filesystem would otherwise leave the slot claimable while this
        # call reported it tombstoned.
        if not _carry_committed(path):
            raise OSError("the carry marker did not persist as committed")
        return True
    if not _carry_committed(path):
        path.unlink(missing_ok=True)
    return False


def _finish_carry(slot_key: str, *, landed: bool) -> bool:
    """Mark this slot's carry claim ``committed``, or RELEASE it when it did not land.

    Answers whether THIS call turned a not-committed marker into a committed one. Only
    that caller may ever take it back: a marker already committed when this ran is
    another call's proof, and one written later is another call's tombstone.

    Committing only after the append is proved is what keeps a crashed carry
    retryable: a marker published first would make the loss permanent, because the
    claim it left behind would refuse every later attempt at state that was never
    carried. Releasing an unlanded claim is the same property, taken immediately
    rather than waiting out the staleness window.

    A release only ever removes a marker that still reads ``pending``, and both
    branches run under the slot's own lock so that check cannot be raced. A COMMITTED
    marker is another call's proof that the carry landed, and it is never this call's
    to withdraw: an absent marker is a claimable one, so removing it would let the
    next session on a recycled slot key carry a deleted conversation's document in.
    Unserializing either branch would defeat the guard -- a release that read
    ``pending`` while an unlocked commit was in flight would unlink the marker that
    commit had just written.

    RAISES when a COMMIT does not reach disk, because ``False`` cannot carry that: it
    already means "already committed by another call", which is a slot that IS
    tombstoned, while a failed write leaves one that is still claimable. A caller that
    cannot tell those apart proceeds on the wrong one, so the two answers are split
    between a return value and an exception. A failed RELEASE is swallowed as before:
    it only leaves the claim ``pending``, which goes stale and is taken over.
    """
    try:
        path = _control_file(slot_key, _CARRIED_FILE)
        with _locked(control_dir(slot_key)):
            committed = _settle_carry_locked(path, landed=landed)
            if committed:
                # Best-effort: the marker already reads committed, and the non-empty
                # fold refuses a second carry whether or not this rename survives.
                fsync_dir(path.parent, best_effort=True)
            return committed
    except (ValueError, OSError):
        if landed:
            # NOT swallowed: ``False`` already means "committed by another call", so a
            # commit that did not reach disk has to be told apart from it. The carry
            # path tolerates the raise at its own call site, where the append has
            # already landed and the stale claim is safe to leave. A delete settles
            # inside its own hold through ``_settle_carry_locked`` instead.
            logger.error(
                "ledger: could NOT commit slot %r's legacy-document marker", slot_key, exc_info=True
            )
            raise
        # The claim stays ``pending`` and goes stale, which a later call takes over.
        logger.warning(
            "ledger: could not finish slot %r's legacy-document claim", slot_key, exc_info=True
        )
    return False


def _withdraw_tombstone_locked(path: Path) -> bool:
    """Remove a COMMITTED carry marker, for a caller ALREADY holding the slot's lock.

    Answers whether a marker was removed. The exact inverse of the tombstone
    :func:`exclude_units` writes, and the only thing in this module that removes a
    committed marker: :func:`_finish_carry` refuses to, because a committed marker is
    normally another call's proof that a carry landed. Two callers earn it, in
    different ways. :func:`unexclude_units` has been told the delete wrote it, and
    lifts it only when no exclusion is left on the slot. :func:`exclude_units`'s
    take-back removes a marker it committed in the SAME hold, whatever else is
    recorded, because that marker is its own write. A ``pending`` claim it overwrote
    is not put back, the residual the carry restore in :func:`unexclude_units` accepts.

    Split out for the same reason :func:`_settle_carry_locked` is: the lock is not
    re-entrant, and the withdrawal has to happen in the SAME hold as the exclusion
    rewrite that earns it. Two separate holds leave a window in which a concurrent
    delete commits its own tombstone, and this cannot tell that marker from the one it
    is undoing -- ``_carry_committed`` answers what the marker SAYS, never whose it is.

    RAISES rather than swallowing, because the marker standing means the spared
    session's earlier goal and phase are never carried. :func:`unexclude_units` turns
    the raise into a retryable answer, which can actually succeed. The take-back in
    :func:`exclude_units` LOGS it instead: that delete is already refused, and its
    original failure is the one that propagates.
    """
    if not _carry_committed(path):
        return False
    path.unlink(missing_ok=True)
    if _carry_committed(path):
        raise OSError("the carry tombstone did not withdraw")
    return True


def _note_unit_order(slot_key: str, session_id: str, *, order_file: str = _UNIT_ORDER_FILE) -> None:
    """Record that *session_id* recorded into *slot_key*, and that it recorded LAST.

    The fold reads units in this order and applies a later update over an earlier one,
    so what it needs from this file is "which unit holds the newest entry". That is why
    the order is written by the call that records rather than derived from a header's
    clock -- but it is also why merely being PRESENT is not enough.

    Appending on first record alone orders units by their FIRST record, which is not
    causal for a unit whose first record arrives late: a request from a session the slot
    has since replaced can reach here after the successor has already recorded, and it
    would then be appended AFTER the successor and stay there, because the presence
    check never revisits a known id. The retired unit's phase and next step would win
    every later fold, permanently.

    So the recording unit is moved to the END when it is not already there. The file
    then orders units by their NEWEST record, which is exactly the question the fold
    asks, and the answer is an observation rather than a claim: among units that have
    recorded, the one recording right now holds the newest entry, whatever any clock
    says. The ordinary case -- one live unit recording repeatedly -- is already last,
    so it stays a bare read.

    SERIALIZED on the slot's lock, because a move is a read-modify-write and an
    unlocked one would drop a concurrent first record's append.

    Best-effort: a slot whose order cannot be written falls back to header order, which
    is what every slot did before this existed.
    """
    if not slot_key or not session_id:
        return
    try:
        path = _control_file(slot_key, order_file, create=True)
        with _locked(control_dir(slot_key)):
            known = _recorded_unit_order(slot_key, order_file=order_file)
            if known and known[-1] == session_id:
                return
            if session_id in known:
                # MOVED, not left where its first record put it. Rewritten rather than
                # appended, because appending a known id would put the same unit in the
                # fold's order twice and fold its entries twice.
                _rewrite_lines(
                    path,
                    tuple(unit for unit in known if unit != session_id)
                    + (_retainable_unit(session_id),),
                )
                return
            # FSYNCED before the update is acknowledged. The fallback for a missing
            # line is header order, and a backward clock step is exactly what that
            # fallback gets wrong -- so a crash that keeps the ledger entry and loses
            # this line restores a retired session's goal and phase over a later
            # one's.
            append_line(path, _retainable_unit(session_id), fsync=True)
            # COMPACTED when the file outgrows the window, so the bound bounds the disk
            # and the read rather than only the answer. The dedup check sees the window
            # alone, so a slot past the cap re-appends ids that fell out of it and the
            # file would otherwise grow without limit. Rewriting on a threshold rather
            # than on every record keeps the ordinary path a bare append: the rewrite is
            # one file in every `_MAX_ORDERED_UNITS` writes at worst.
            if path.stat().st_size > _MAX_ORDER_BYTES:
                kept = (*known[-(_MAX_ORDERED_UNITS - 1) :], session_id)
                _rewrite_lines(path, kept)
    except (ValueError, OSError):
        logger.warning("ledger: could not record this slot's unit order", exc_info=True)


def note_work_unit_recorded(slot_key: str, session_id: str) -> None:
    """Publish that *session_id* just appended a work record under its slot."""
    _note_unit_order(
        canonical_slot(slot_key, session_id),
        session_id,
        order_file=_WORK_UNIT_ORDER_FILE,
    )


def newest_work_unit(slot_key: str) -> str:
    """*slot_key*'s CURRENT newest unit, the one a report or relay for it is written into.

    The newest unit the slot has, whether or not it holds a work record yet: after a
    conductor restart the new unit has none, and writing into the unit that last held
    one would grow a unit the slot has moved on from. Read by succession
    (``crew_log.projection_readers.newest_unit_in_succession``, cached against the
    store root). Falls back to the unit that last recorded (the work-order file) when
    the store lists no unit for the slot; ``""`` when neither names a live unit.
    """
    if not slot_key:
        return ""
    try:
        from kiro_crew.crew_log.projection_readers import newest_unit_in_succession
        from kiro_crew.crew_log.schema import KIND_SESSION
        from kiro_crew.crew_log.store import CrewLog

        unit = newest_unit_in_succession(slot_key)
        if unit:
            return unit
        for unit in reversed(_recorded_unit_order(slot_key, order_file=_WORK_UNIT_ORDER_FILE)):
            if CrewLog.exists(KIND_SESSION, unit):
                return unit
    except Exception as exc:
        logger.debug("work ledger: could not resolve this slot's newest unit: %s", exc)
    return ""


def work_crew_log_units(slot_key: str) -> tuple[str, ...]:
    """Work-record units for *slot_key* in causal append order, oldest first.

    Units absent from the bounded order tail predate every retained unit and fold
    first. Listing failures fail closed because this runs on a loop-cycle read path.
    """
    if not slot_key:
        return ()
    try:
        from kiro_crew.crew_log.store import session_units_for_slot

        units = session_units_for_slot(slot_key)
        recorded = _recorded_unit_order(slot_key, order_file=_WORK_UNIT_ORDER_FILE)
        if recorded:
            known = [unit for unit in recorded if unit in units]
            rest = [unit for unit in units if unit not in recorded]
            units = tuple(rest + known)
        return units
    except Exception:
        logger.warning("work ledger: could not list this slot's crew logs", exc_info=True)
        return ()


def note_panel_unit_recorded(slot_key: str, session_id: str) -> None:
    """Publish that *session_id* just appended a panel record under its slot."""
    _note_unit_order(
        canonical_slot(slot_key, session_id),
        session_id,
        order_file=_PANEL_UNIT_ORDER_FILE,
    )


def panel_crew_log_units(slot_key: str) -> tuple[str, ...]:
    """Panel units for *slot_key* in causal append order, oldest first.

    Units absent from the bounded order tail predate every retained unit and fold
    first. Listing failures fail closed, which for the panel costs the history and not
    the panel: the file is the durable record and the read falls back to it.
    """
    if not slot_key:
        return ()
    try:
        from kiro_crew.crew_log.store import session_units_for_slot

        units = session_units_for_slot(slot_key)
        recorded = _recorded_unit_order(slot_key, order_file=_PANEL_UNIT_ORDER_FILE)
        if recorded:
            known = [unit for unit in recorded if unit in units]
            rest = [unit for unit in units if unit not in recorded]
            units = tuple(rest + known)
        return units
    except Exception:
        logger.warning("panel: could not list this slot's crew logs", exc_info=True)
        return ()


def _rewrite_lines(path: Path, lines: "tuple[str, ...]") -> None:
    """Replace a control file with *lines*, one per line, atomically and fsynced.

    A torn rewrite would lose the causal order the fold depends on, so a reader must
    see the whole old file or the whole new one. The caller holds the slot's lock,
    and every control-file writer does, so a temp already in the directory is one a
    killed writer left behind: it is swept here, or every SIGKILL mid-rewrite would
    leave a uniquely named temp that nothing ever removes.

    The directory is NOT synced here. The callers whose next step depends on the
    rename surviving a crash sync it once for the whole transaction.
    """
    for stale in path.parent.glob("*.tmp"):
        try:
            stale.unlink()
        except OSError:
            pass
    atomic_write(path, "".join(f"{line}\n" for line in lines), fsync=True)


def _recorded_unit_order(slot_key: str, *, order_file: str = _UNIT_ORDER_FILE) -> "tuple[str, ...]":
    """The units this slot recorded into, oldest first. Empty when there is no log.

    DEDUPLICATED, and truncated to the newest :data:`_MAX_ORDERED_UNITS` distinct ids
    after that. The check above this write is not atomic, so two concurrent first
    records on one unit can both append its id; a repeated id would put the same unit
    in the fold's order twice, and folding one unit twice replays entries the fold has
    already consumed.

    Reads at most :data:`_MAX_ORDER_BYTES` plus one compaction's worth, so a file that
    grew before this build's compaction existed cannot make a loop cycle read
    unboundedly.
    """
    try:
        path = _control_file(slot_key, order_file)
        if not path.exists():
            return ()
        with path.open("r", encoding="utf-8") as fh:
            text = fh.read(_MAX_ORDER_READ_BYTES)
        seen: list[str] = []
        known: set[str] = set()
        for line in text.splitlines():
            unit = line.strip()
            if unit and unit not in known:
                known.add(unit)
                seen.append(unit)
        return tuple(seen[-_MAX_ORDERED_UNITS:])
    except (ValueError, OSError):
        return ()


def _legacy_document(slot_key: str) -> dict[str, Any]:
    """The pre-projection ``state.json`` document for *slot_key*, or the empty record.

    Read by the carry above and by its read-side preview, and by nothing else. Both
    exist to RETIRE the file rather than to consult it: the preview answers only while
    the carry is still owed and only when the folded record is empty, so this file can
    never contradict something the log recorded, and it stops being read the moment
    the carry's marker commits. Size ceiling before parse, like every other reader of
    it, so a damaged or hostile file cannot make an upgrade allocate its size.
    """
    path = ledger_dir(slot_key) / _STATE_FILE
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        # ABSENT is the empty record: there is nothing to carry and never was.
        return _empty_state()
    except OSError as exc:
        # UNREADABLE is not empty. Answering empty here reads as "nothing to carry", the
        # update then lands, the folded record stops being empty -- and the carry, which
        # only ever runs on an empty record, is never attempted again. One transient
        # error would lose the state permanently.
        raise LedgerUnavailable(f"slot {slot_key!r}'s legacy document is unreadable") from exc
    if size > _MAX_STATE_BYTES:
        logger.warning("ledger: legacy document over the size ceiling; not carried")
        return _empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LedgerUnavailable(f"slot {slot_key!r}'s legacy document is unreadable") from exc
    except (ValueError, UnicodeDecodeError):
        # DAMAGED content is not transient: no retry parses it, so it is the empty record
        # and the claim is consumed rather than left to be retried forever.
        logger.warning("ledger: legacy document is not readable JSON; not carried")
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    state = _empty_state()
    state.update({k: v for k, v in raw.items() if k in state})
    return state
