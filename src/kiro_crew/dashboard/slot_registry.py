"""Owner-driven registry operations for dashboard chat slots."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _transcript_identity(transcript: str) -> frozenset[str]:
    """The filename stems *transcript* resolves to: the ONE identity two spellings share.

    A transcript key arrives in more than one spelling -- a Slack thread as
    ``slack:<ts>`` or ``slack_<ts>``, a legacy bare ``<ts>`` -- and every
    spelling resolves to one file. Comparing the raw strings would let a
    takeover that declared one spelling slip past a teardown that writes the
    other, so declared targets are recorded and matched as stem sets, the same
    identity ``replacement_shares_transcript`` compares by. Call-time import:
    ``kiro_crew.history`` is the memory facade and this module is a leaf of the
    dashboard's state layer.
    """
    from kiro_crew.history import transcript_stems

    return frozenset(transcript_stems(transcript))


@dataclass(frozen=True)
class SlotCreationPlan:
    """Registry-owned facts needed to finish constructing a new slot."""

    key: str
    requested_name: str
    minted_new: bool


# Hard ceiling on the transcript keys ONE takeover watch records. A resume's
# takeover note names the transcript it is about to hydrate, and that name comes
# from the request body (the resume endpoint's ``key``), so without a stated cap
# a caller could grow one watch's set without bound for as long as a save is in
# dispatch for the key. The cap is enforced at ADMISSION: a note that would push
# the set past it is refused before anything about the takeover is recorded, and
# the resume behind it is refused with a retryable error. A legitimate teardown
# has one or two distinct targets (the key's own transcript and, for a linked
# slot, the linked one), so the cap sits two orders of magnitude above use.
_MAX_NOTED_TRANSCRIPTS_PER_WATCH = 64


class TruncationClaim:
    """Arbitration token between one truncating save and same-key takeover.

    A truncating history rewrite decides its window on a snapshot of one slot
    object, then commits on a worker thread. A same-name close-and-recreate is
    not serialized against the per-session file lock, so the map can change
    owner between the save's commit-boundary identity check and its file
    replace — and a replacement that RESUMES the transcript then reads the
    truncated file as its live window. The claim is the ordering contract
    between those two layers: the save registers one for the duration of the
    write, every same-key takeover (publication or resume read) marks it, and
    the save commits only when it wins a single atomic decision.

    ``outcome`` is written only under the owner's claim mutex, one transition
    each way and never back:

    - ``""`` — undecided; the save has not reached its commit decision.
    - ``"committed"`` — the save won; the file replace is happening or done.
      A takeover arriving now proceeds and reads the post-rewrite transcript,
      which is the durable truth — the sequential order "rewrite, then
      reopen".
    - ``"overtaken"`` — a same-key takeover began first; the save must refuse
      and leave the file untouched, so the takeover's read (serialized behind
      the per-session lock) returns the transcript the rewrite never changed
      — the sequential order "reopen, then the rewrite is refused".

    Every interleaving therefore resolves to one of those two sequential
    histories; there is no schedule in which the replacement adopts a
    truncated window it did not knowingly resume.
    """

    __slots__ = ("outcome", "transcript")

    def __init__(self, transcript: frozenset[str] = frozenset()) -> None:
        self.outcome = ""
        # The stems of the transcript this save writes. A takeover note marks
        # the claim only when its declared read target names this file (or
        # either side is unknown): a same-name publication that resumes a
        # DIFFERENT transcript touches nothing this write replaces, and marking
        # it overtaken would fail a close whose archive the takeover never
        # raced -- without writing ``closed``.
        self.transcript = transcript


class TakeoverBasis:
    """One in-flight truncating dispatch's watch on a slot map key.

    Opened at the dispatcher's last synchronous instant — where it can still
    vouch its slot object is the live occupant of the key — and closed by the
    same dispatcher once the save's outcome is in hand. ``value`` snapshots the
    key's takeover count at open time; the claim registration compares the
    watch's current count against it, so a takeover that fires while the save
    is still in dispatch (no claim exists yet, nothing to mark) is observed as
    a moved count and the claim is born overtaken.

    The watch table retains an entry only while at least one basis is open for
    the key: a takeover with no in-flight truncating save has nobody left to
    observe it, so nothing is recorded — which is what bounds the table by the
    number of in-flight saves rather than by every key the process ever
    published.
    """

    __slots__ = ("name", "value", "closed", "deferred", "truncating")

    def __init__(self, name: str, value: int, truncating: bool = True) -> None:
        self.name = name
        self.value = value
        # Whether the save this basis vouches for replaces the file (a
        # rewrite-shaped write: a truncated snapshot, ``_pending_rewrite``, a
        # teardown's archive) rather than appending to it. Only a truncating
        # holder makes the key's ordering PENDING for a reader
        # (:meth:`SlotRegistry.takeover_ordering_pending`): a refused rewrite
        # is what lands rows append-safely under its lock behind the reader's
        # cached view, while a plain append is a superset of the file a reader
        # around it can lose nothing from. The dispatcher states it; a worker
        # that finds its append-shaped dispatch turned truncating in the gap
        # raises it (:meth:`SlotRegistry.mark_basis_truncating`) before it
        # registers its claim.
        self.truncating = truncating
        # Closing is idempotent through this flag: dispatch ownership can pass
        # from the event loop to a worker (an executor save closes its own
        # basis so a cancelled await cannot retire the watch under it), and
        # the loop keeps a close for the paths where the worker never runs, so
        # both sides may call close and only the first one releases a holder.
        self.closed = False
        # Ownership marker for a CALLER-opened basis whose caller was
        # cancelled after its save was already submitted: the caller's own
        # unwind reaches its ``finally`` close immediately, but the worker is
        # still pre-registration under this watch, and retiring it there
        # leaves a takeover in that window unrecorded — the orphaned
        # truncation then commits over the takeover's transcript. Set under
        # the claim mutex by ``defer_basis_close``; a deferred basis ignores
        # the ordinary close and is released by ``close_transferred_basis``
        # from the settled worker future instead.
        self.deferred = False


@dataclass(frozen=True)
class TakeoverNote:
    """The handle a noter holds on the takeover note it opened.

    Names the note by the identity of the watch that admitted it
    (``watch_id``, never reused across the process) and its sequence on that
    watch, so a retraction can only ever withdraw the note it was issued for:
    a watch retires with its last holder and a new one on the same key starts
    its sequence over, and a noter refused after that must not withdraw the
    new watch's first note. ``NO_TAKEOVER_NOTE`` is the handle for a note the
    key had no watch to record (nothing to retract).
    """

    watch_id: int
    seq: int


NO_TAKEOVER_NOTE = TakeoverNote(0, 0)


@dataclass(frozen=True)
class DeferredDismissal:
    """A close whose archive lost to a takeover that has not yet published or given up.

    The close reported the hand-over, drained the original's tail, and left the
    key to a taker whose read was still pending; its ``closed`` marker was never
    written (the archive was refused). The dismissal is owed until the taker
    PUBLISHES (then the key is live and the marker would close the taker's own
    tab) or RETRACTS as the last one standing (then nobody holds the key, and the
    marker must land or the recent-session restore brings back a tab the user
    dismissed). ``basis`` is the teardown's own watch holder, kept open past the
    close so the standing notes stay tracked; whoever settles the dismissal
    releases it.

    A dismissal is settled -- cleared from its watch, holder released -- only
    once that is FINAL: by a publication that owns the key, or by a confirmed
    marker write (:meth:`SlotRegistry.settle_written`). Until then it stays
    owed: a retraction that returns it for writing leaves it in place, so a
    write that fails, or is overtaken by a publication while queued, is retried
    by the next exit on the key rather than lost.
    """

    name: str
    transcript: str
    closed_at: float
    basis: "TakeoverBasis"
    # The INCARNATION the close dismissed: the transcript's ``created_at`` as
    # the closed slot last observed it on disk (``_disk_meta_created_at``). A
    # session can be permanently deleted and the same name-derived key created
    # again while the dismissal is owed; the marker belongs to the conversation
    # the user closed, not to the one that now lives under its key, so the
    # write compares this witness against the line it is about to stamp and a
    # mismatch satisfies the dismissal without writing. Empty when the closed
    # slot recorded no identity (a legacy line), which the write treats as
    # unidentifiable: it never stamps a line it cannot match.
    created_at: str = ""


@dataclass(frozen=True)
class OwedUnstamp:
    """A ``closed`` stamp of ours that a publication won against and that must come off.

    An owed dismissal's marker committed in the same instant a same-name
    publication took the key; the dismissal is settled on the registry, but the
    stamp sits on the line of a tab that is live and hides it at the next
    restart. The un-stamp is a compare-and-clear keyed on the ``closed_at`` we
    wrote, so a later legitimate close of the new tab is never removed. When it
    fails (a transient write error) the record is parked on the key's watch and
    the next exit on that key retries it; it is dropped only once the clear
    lands or the line provably does not carry our stamp.
    """

    name: str
    transcript: str
    closed_at: float


# Watch identities, process-wide and never reused: ``id(obj)`` is recycled
# once a retired watch is collected, which is exactly when a stale handle
# could otherwise match a fresh watch on the same key.
_watch_ids = itertools.count(1)


class TakeoverWatch:
    """Refcounted per-key takeover counter; exists only while bases are open."""

    __slots__ = (
        "id",
        "holders",
        "truncating_holders",
        "count",
        "noted_transcripts",
        "standing",
        "publication_notes",
        "dismissal",
        "reads_in_flight",
        "unstamp",
    )

    def __init__(self) -> None:
        self.id = next(_watch_ids)
        self.holders = 0
        # A close that handed this key over while a taker was still unpublished
        # and is owed its ``closed`` marker should every taker give up
        # (:class:`DeferredDismissal`). Set by the close's hand-over arm, cleared
        # by the publication that owns the key or by the last standing noter's
        # retraction, which writes the marker. ``None`` otherwise.
        self.dismissal: DeferredDismissal | None = None
        # Explicit user resumes between their close-metadata snapshot and their
        # publish or refusal (``SlotRegistry.begin_resume_read`` /
        # ``end_resume_read``). An owed marker must not land while one is in
        # flight: the deferred holder is non-truncating on purpose (readers are
        # never held behind a dismissal), so such a resume records no note,
        # waits on nothing, and keeps the open header it read -- a marker that
        # committed between its snapshot and its publish would stay on the live
        # tab's line and hide it at the next restart. The resume's own exit
        # settles the dismissal (publish) or retracts and writes it (refusal).
        self.reads_in_flight = 0
        # A stamp of ours that must come off a live tab's line and could not be
        # cleared yet (:class:`OwedUnstamp`); the holder keeps the watch alive
        # until an exit on the key clears it. ``None`` otherwise.
        self.unstamp: OwedUnstamp | None = None
        # Holders whose basis is ``truncating``: the saves a reader of this key
        # must let settle. Zero with claims retired means the key's ordering is
        # not pending, however many append-shaped saves still hold the watch.
        self.truncating_holders = 0
        # Notes ever admitted on this watch; also the sequence the next note
        # takes. A basis snapshots it at open, so "a takeover since the basis
        # opened" is "a note with a higher sequence".
        self.count = 0
        # The notes whose noter still STANDS behind the key, by sequence, each
        # with the stems of the transcript it declared as its read target
        # (empty: undeclared, matched as unknown). A note enters here when
        # admitted and leaves when its noter is refused without publishing (a
        # deletion landing during its read, a session already resuming
        # elsewhere, an app recheck) and retracts it.
        # A refused noter cannot un-mark the claim it marked (the save it
        # overtook is already refused), but it can withdraw its own note, and
        # a teardown whose every post-basis note is withdrawn takes its retry
        # arm rather than leaving the key to nobody. Per note, never a net
        # counter: a retraction removes exactly the note it names, so one
        # noter's earlier refusal can neither hide nor fabricate a later
        # taker's note against any basis. A publication's note is never
        # retracted and stays for the watch's life, which is bounded by the
        # saves holding it open.
        self.standing: dict[int, frozenset[str]] = {}
        # The sequences in ``standing`` that a PUBLICATION recorded (``put_slot``)
        # rather than a resume's read. A publication never retracts, but it is
        # evidence of a taker only while its slot holds the key: a noter whose
        # own ``get_or_create_slot`` published, then refused and popped, leaves
        # one behind that stands for nobody.
        self.publication_notes: set[int] = set()
        # Transcript STEMS the takeovers that moved this watch declared as
        # their read target (resume paths know it; a bare publication notes
        # none), so any spelling of one file matches. Lets a basis holder whose own write routes to a LINKED
        # transcript tell "a takeover is coming for the file I write" from
        # "the key changed hands but my file is not involved" — the map
        # cannot answer that while the takeover's publication is still
        # pending behind its read. Dies with the watch, and holds at most
        # ``_MAX_NOTED_TRANSCRIPTS_PER_WATCH`` entries: a note past that is
        # refused at admission, so a request-controlled name cannot grow it.
        self.noted_transcripts: set[str] = set()


def _transcripts_meet(declared: frozenset[str], written: frozenset[str]) -> bool:
    """Whether a takeover's declared read target names the file a claim writes.

    Either side empty means UNKNOWN -- a publication of a slot whose routing
    cannot be read, a claim registered without a transcript -- and unknown
    fails closed: the takeover counts. Two known, disjoint stem sets are two
    files, and a takeover reading one is no event for a write to the other.
    """
    if not declared or not written:
        return True
    return not declared.isdisjoint(written)


def _standing_since(watch: TakeoverWatch, basis_value: int, written: frozenset[str]) -> bool:
    """Whether a note newer than *basis_value* still stands on *watch* for *written*.

    Caller holds the claim mutex. A standing note's sequence exceeds the
    basis snapshot exactly when its noter took the key over after the basis
    opened; notes older than the basis, standing or withdrawn, are not this
    basis's evidence, and neither is a newer note whose declared target is a
    different file (:func:`_transcripts_meet`; an empty *written* asks about
    the KEY and is met by every standing note).
    """
    return any(
        seq > basis_value and _transcripts_meet(declared, written)
        for seq, declared in watch.standing.items()
    )


def _slot_transcript_identity(slot: Any) -> frozenset[str]:
    """The stems of the transcript *slot* writes, or empty when they cannot be read.

    ``slot_history_key`` resolves a slot's routing (``linked_session_key``,
    channel provenance, the bare name); a double without that shape answers
    empty, which the matching treats as unknown (fail closed). Call-time
    import: ``chat_utils`` sits above this leaf of the state layer.
    """
    try:
        from kiro_crew.dashboard.chat_utils import slot_history_key

        return _transcript_identity(slot_history_key(slot))
    except Exception:
        return frozenset()


def _ordering_pending_locked(owner: Any, name: str) -> bool:
    """:meth:`SlotRegistry.takeover_ordering_pending` under the caller's mutex."""
    claims = getattr(owner, "_truncation_claims", None)
    watches = getattr(owner, "_takeover_watches", None)
    if isinstance(watches, dict):
        watch = watches.get(name)
        if watch is not None and watch.truncating_holders > 0:
            return True
    if isinstance(claims, dict):
        return any(claim.outcome == "overtaken" for claim in claims.get(name, []))
    return False


def _settle_waiters(owner: Any) -> dict[str, list[tuple[Any, Any]]] | None:
    """The owner's settle-waiter table, or ``None`` for a double without one."""
    waiters = getattr(owner, "_takeover_settle_waiters", None)
    return waiters if isinstance(waiters, dict) else None


def _wake_settle_waiters(owner: Any, name: str) -> None:
    """Resolve the loop-side waiters on *name* once its ordering stops being pending.

    Caller holds the claim mutex. Runs on whatever thread retired the basis or
    claim, so each future is resolved through its own loop.
    """
    if _ordering_pending_locked(owner, name):
        return
    waiters = _settle_waiters(owner)
    if waiters is None:
        return
    pending = waiters.pop(name, None)
    if not pending:
        return
    for loop, future in pending:
        try:
            loop.call_soon_threadsafe(_resolve_settled, future)
        except RuntimeError:
            # The loop is closed: its waiter was abandoned with it and cannot
            # be served.
            continue


def _resolve_settled(future: Any) -> None:
    if not future.done():
        future.set_result(True)


class SlotRegistry:
    """Operate on the current containers owned by ``DashboardState``.

    The facade's restore, cleanup, and rollback paths replace registry
    containers wholesale.  Consequently this component never retains a
    reference to ``_slots``, ``_slots_under_construction``, or
    ``_slack_to_slot``; every operation reads them from its owner at call time.
    Helpers that remain monkeypatch seams are likewise supplied per call.
    """

    @staticmethod
    def get_slot(owner: Any, name: str) -> Any | None:
        """Return the slot currently registered under *name*, if any."""
        return owner._slots.get(name)

    @staticmethod
    def has_slot(owner: Any, name: str) -> bool:
        """Return whether *name* is present in the current slot registry."""
        return name in owner._slots

    @staticmethod
    def put_slot(owner: Any, name: str, slot: Any) -> Any:
        """Publish *slot* under *name* and return the identical object."""
        # Publication IS a same-key takeover: a truncating save mid-flight for
        # this key snapshotted a slot object the map is about to stop holding,
        # so its commit must lose to this publication rather than land on the
        # transcript the new slot resumes. Noted before the insert so the
        # announcement is never observable later than the slot itself. The
        # note declares the file the published slot writes, so a save for a
        # DIFFERENT transcript under the same key (a linked tab's archive
        # beside an unbound same-name publication) is not marked overtaken.
        SlotRegistry.open_same_key_takeover_note(
            owner, name, transcript_stems=_slot_transcript_identity(slot), publication=True
        )
        owner._slots[name] = slot
        # A publication owns the key: a close that handed it over owes its
        # marker to nobody (writing it would close the tab just published).
        # Settled here only when NO takeover note stands on the key -- a
        # publication by a path that noted nothing (a plain create). A noter's
        # publication is not final at the insert: both noters insert BEFORE
        # their last refusal point (a rehydrate loads its window after
        # ``get_or_create_slot`` and pops on failure; a resume with a
        # containment hook registers, pops for its reserved tail, and can still
        # refuse or be cancelled), so settling on their insert would release the
        # watch their later retraction needs to write the marker. Their wrapper
        # settles at its success exit (:meth:`settle_dismissal_on_publish`),
        # and every refusal path flows through the retraction instead.
        SlotRegistry._settle_dismissal(owner, name, publish=True, only_if_unnoted=True)
        return slot

    @staticmethod
    def settle_dismissal_on_publish(owner: Any, name: str) -> None:
        """A noter's publication is final: clear the dismissal its key's close left owed.

        Called from the noted wrappers' SUCCESS exit -- ``resume_slot_from_history``
        with ``outcome.refusal is None``, ``rehydrate_slot_from_history_async``
        with a published slot -- after their last refusal point, so the tab now
        holding the key is never closed under. Idempotent; a no-op when nothing
        is owed and for ownerless doubles.
        """
        SlotRegistry._settle_dismissal(owner, name, publish=True)

    @staticmethod
    def begin_truncation_claim(
        owner: Any,
        name: str,
        *,
        basis: TakeoverBasis | None = None,
        transcript: str | None = None,
    ) -> TruncationClaim | None:
        """Register one truncating save's claim on *name* for its whole write.

        Called by the save before it takes the per-session file lock, so the
        claim also covers the lock wait — a takeover landing while the save is
        still queued behind another writer must win too. Claims for one key are
        held as a list: two truncating writes can be in flight for one map key
        when they target different transcripts (a popped slot's hand-over drain
        beside its replacement's own rewrite), and a single-entry table would
        let the second registration orphan the first.

        *basis* is the dispatcher's open :class:`TakeoverBasis`. A takeover
        that fires between the dispatch and this registration has no claim to
        mark, so the registration asks the watch for a note still standing
        (not retracted, :meth:`retract_same_key_takeover`) that postdates the
        basis snapshot: one means the key changed hands while the save was
        still in dispatch, and the claim is born already overtaken. ``None``
        means the dispatcher anchored to no earlier
        instant, and the claim observes only takeovers from registration on.
        *transcript* is the key the save writes; recorded as stems on the
        claim, so only a takeover whose declared read target names that file
        marks it (:func:`_transcripts_meet`). ``None`` records no identity and
        every takeover of the key counts.

        Returns ``None`` when *owner* carries no real claim table — partial
        and MagicMock doubles in tests construct owners attribute by
        attribute or answer every getattr with a fresh mock, so the container
        must BE a dict, not merely non-``None``. The save then behaves as if
        unguarded, which is the pre-contract shape.
        """
        claims = getattr(owner, "_truncation_claims", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(claims, dict) or mutex is None:
            return None
        claim = TruncationClaim(_transcript_identity(transcript) if transcript else frozenset())
        with mutex:
            if basis is not None:
                watches = getattr(owner, "_takeover_watches", None)
                watch = watches.get(basis.name) if isinstance(watches, dict) else None
                if watch is not None and _standing_since(watch, basis.value, claim.transcript):
                    claim.outcome = "overtaken"
            claims.setdefault(name, []).append(claim)
        return claim

    @staticmethod
    def open_takeover_basis(
        owner: Any, name: str, *, truncating: bool = True
    ) -> TakeoverBasis | None:
        """Open a takeover watch on *name* and snapshot its current count.

        Called at a dispatcher's last synchronous instant before it hands a
        save off, paired with :meth:`close_takeover_basis` in the same
        dispatcher's ``finally`` once the save's outcome is in hand.
        *truncating* says whether that save replaces the file; an
        append-shaped save holds the watch (so a takeover in its dispatch gap
        is still observed should it turn truncating) without making the key's
        ordering pending for readers. The default is the conservative one: a
        dispatcher that does not know states truncating. Owners without the
        tables answer ``None``, and the paired claim registration on such an
        owner returns no claim either.
        """
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return None
        with mutex:
            watch = watches.get(name)
            if watch is None:
                watch = watches[name] = TakeoverWatch()
            watch.holders += 1
            if truncating:
                watch.truncating_holders += 1
            return TakeoverBasis(name, watch.count, truncating)

    @staticmethod
    def mark_basis_truncating(owner: Any, basis: TakeoverBasis | None) -> None:
        """Raise *basis* to truncating: its save turned out to replace the file.

        Called by a save worker that finds ``_pending_rewrite`` set on a slot
        whose dispatch was append-shaped (a regenerate whose inline save failed
        between the dispatch and the worker's read), BEFORE it registers its
        claim, so a reader asking whether the key's ordering is pending sees
        this save from that instant on. Idempotent; a no-op for ``None``, for a
        closed basis and for ownerless doubles.
        """
        if basis is None or basis.truncating:
            return
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            basis.truncating = True
            return
        with mutex:
            if basis.closed or basis.truncating:
                return
            basis.truncating = True
            watch = watches.get(basis.name)
            if watch is not None:
                watch.truncating_holders += 1

    @staticmethod
    def close_takeover_basis(owner: Any, basis: TakeoverBasis | None) -> None:
        """Release a watch holder; the last one out retires the key's entry.

        Idempotent per basis: dispatch ownership can pass between the event
        loop and a worker, and each side closes on its own exit paths, so a
        basis records that it was already released and the second close
        changes nothing. A basis marked ``deferred`` is not released here at
        all — its caller was cancelled after submitting a save that is still
        running under this watch, and ownership passed to that worker's
        settlement (:meth:`close_transferred_basis`).
        """
        if basis is None:
            return
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return
        with mutex:
            if basis.closed or basis.deferred:
                return
            basis.closed = True
            watch = watches.get(basis.name)
            if watch is None:
                return
            watch.holders -= 1
            if basis.truncating:
                watch.truncating_holders = max(0, watch.truncating_holders - 1)
            if watch.holders <= 0:
                del watches[basis.name]
            _wake_settle_waiters(owner, basis.name)

    @staticmethod
    def defer_basis_close(owner: Any, basis: TakeoverBasis | None) -> bool:
        """Transfer *basis* ownership to a still-running worker; True on transfer.

        Called from a dispatcher's cancellation unwind AFTER its save was
        submitted: the unwind reaches the dispatcher's ``finally`` close
        immediately, but the worker may not have registered its claim yet, and
        retiring the watch in that gap leaves a takeover unrecorded — the
        orphaned truncation then commits over the takeover's transcript. A
        transferred basis makes the ordinary close a no-op; the transferee
        releases the holder through :meth:`close_transferred_basis` once the
        worker settles. ``False`` when there is nothing to transfer (already
        closed, or ownerless doubles), in which case the caller's own close
        path remains correct as it stands.
        """
        if basis is None:
            return False
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if mutex is None:
            return False
        with mutex:
            if basis.closed or basis.deferred:
                return False
            basis.deferred = True
            return True

    @staticmethod
    def close_transferred_basis(owner: Any, basis: TakeoverBasis | None) -> None:
        """Release a basis whose ownership :meth:`defer_basis_close` transferred."""
        if basis is None:
            return
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if mutex is not None:
            with mutex:
                basis.deferred = False
        SlotRegistry.close_takeover_basis(owner, basis)

    @staticmethod
    def takeover_noted_transcript(owner: Any, basis: TakeoverBasis | None, transcript: str) -> bool:
        """Report whether a takeover on *basis*'s key declared *transcript* as its read.

        For a basis holder whose own write routes to a linked transcript: a
        same-key takeover that is still unpublished (its read pending behind
        the history lock) is invisible to every map check, but its takeover
        note named the file it is about to hydrate. Naming a match means the
        holder's truncating write must keep the ordering claim; a takeover
        that named some other file — or none, a bare publication — leaves the
        holder's file uncontested.
        """
        if basis is None or not transcript:
            return False
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return False
        wanted = _transcript_identity(transcript)
        with mutex:
            watch = watches.get(basis.name)
            return watch is not None and not wanted.isdisjoint(watch.noted_transcripts)

    @staticmethod
    def retract_same_key_takeover(
        owner: Any, name: str, note: TakeoverNote
    ) -> DeferredDismissal | None:
        """Withdraw *note*, a takeover note on *name* whose noter publishes nothing.

        Called from a resume's or rehydrate's exit when it recorded a note
        (:meth:`open_same_key_takeover_note`) and then refused for its own
        reasons. The claims the note marked stay marked -- the save that lost
        to it is already refused, and a refusal is the safe side -- but the
        note itself leaves the watch's standing set, so a teardown holding a
        basis on the key sees ``basis_moved`` fall back to ``False`` once no
        note newer than its basis stands, and restores its original rather
        than yielding the key to a taker that gave up. Withdraws exactly the
        note named, never "one note": a noter's refusal says nothing about any
        other taker. The handle is bound to the watch that issued it: when the
        key's current watch is a different one -- the issuing watch retired
        with its last holder and a later save opened a fresh one, whose first
        note carries the same sequence -- nothing is withdrawn. A no-op on a
        retired watch, for a note not standing, for ``NO_TAKEOVER_NOTE``, and
        for ownerless doubles.

        Returns the :class:`DeferredDismissal` a close left owed on this key
        when this retraction was the LAST standing note and no slot holds the
        key: the caller must then write the ``closed`` marker
        (``takeover_ordering.record_deferred_dismissal``), because the close
        reported its hand-over to a taker that has now given up and nothing
        else records the dismissal. ``None`` otherwise.
        """
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return None
        with mutex:
            watch = watches.get(name)
            if watch is None or watch.id != note.watch_id:
                return None
            watch.standing.pop(note.seq, None)
            if watch.dismissal is None:
                return None
            slots = getattr(owner, "_slots", None)
            if isinstance(slots, dict) and slots.get(name) is not None:
                # A slot holds the key: the dismissal is that publication's to
                # settle (:meth:`settle_dismissal_on_publish`), not this exit's.
                return None
            if any(seq not in watch.publication_notes for seq in watch.standing):
                # Another resume still stands behind the key; its exit decides.
                return None
            if watch.reads_in_flight > 0:
                # An explicit resume is between its header snapshot and its
                # publish: its exit settles or writes the dismissal.
                return None
            # No resume stands and nobody holds the key. A publication note left
            # standing (a noter's own ``get_or_create_slot`` noted the key
            # before it refused and popped its slot) stands for nobody.
            return SlotRegistry._settle_dismissal(owner, name, publish=False)

    @staticmethod
    def hand_basis_to_dismissal(
        owner: Any,
        basis: TakeoverBasis | None,
        *,
        transcript: str,
        closed_at: float,
        created_at: str = "",
    ) -> DeferredDismissal | None:
        """A close hands its key over with its marker unwritten; keep the dismissal owed.

        Called from the close's hand-over arm when its archive was refused (a
        note moved the watch), the original's tail was drained, and NO slot is
        published on the key: the taker is still reading. The teardown's
        *basis* stays open -- marked ``deferred`` so the close's own ``finally``
        releases nothing, and made non-truncating so readers of the key are no
        longer held behind it -- which keeps the standing notes tracked past
        the close's return. The dismissal then settles one of two ways: a
        publication takes the key (:meth:`put_slot`, nothing to write) or the
        last standing noter retracts (:meth:`retract_same_key_takeover`, which
        returns the dismissal for its caller to write).

        Both outcomes go through the same owed-until-written protocol: the
        record is REGISTERED on the watch and the holder kept in every case, so
        the write-time guard (:meth:`dismissal_still_owed`) admits it and only
        a confirmed write (:meth:`settle_written`) or a publication clears it.
        What differs is who writes: with a resume still standing the dismissal
        is deferred and ``None`` is returned (that noter's exit writes it);
        with nobody standing the record is returned and the caller writes it
        now -- and a write that fails leaves it owed on a watch that outlives
        the close, for the next exit on the key to retry. ``None`` for
        ownerless doubles (nothing can be recorded there) and for a basis
        already released or already handed over.
        """
        if basis is None:
            return None
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return None
        record = DeferredDismissal(basis.name, transcript, closed_at, basis, created_at)
        with mutex:
            watch = watches.get(basis.name)
            if watch is None or basis.closed or basis.deferred:
                return None
            if watch.dismissal is not None:
                # Two closes of one key cannot both be owed: the earlier one's
                # holder is released and the newer dismissal takes its place.
                earlier = watch.dismissal
                watch.dismissal = None
                earlier.basis.deferred = False
                SlotRegistry._release_holder_locked(owner, watches, earlier.basis)
            basis.deferred = True
            if basis.truncating:
                basis.truncating = False
                watch.truncating_holders = max(0, watch.truncating_holders - 1)
            watch.dismissal = record
            _wake_settle_waiters(owner, basis.name)
            if any(seq not in watch.publication_notes for seq in watch.standing):
                return None
            # No resume stands behind the key (a publication note with no slot
            # on the key stands for nobody): the caller writes the marker now,
            # against the same registered record.
            return record

    @staticmethod
    def _settle_dismissal(
        owner: Any, name: str, *, publish: bool, only_if_unnoted: bool = False
    ) -> DeferredDismissal | None:
        """Clear the dismissal owed on *name* and release the holder it kept open.

        Caller holds the claim mutex (``put_slot`` calls this outside it and
        takes it here). *publish* True means a slot now owns the key: nothing
        to write, the dismissal is cleared and its holder released, ``None``.
        False means the last noter retracted: the record is RETURNED for the
        caller to write but stays owed on the watch -- it is cleared only by
        :meth:`settle_written` once the write is confirmed, so a write that
        fails or is overtaken by a publication is retried, not lost.
        *only_if_unnoted* leaves the dismissal in place while a resume's
        takeover note stands: that noter's own exit settles it, by success or by
        retraction.
        """
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return None

        def _settle() -> DeferredDismissal | None:
            watch = watches.get(name)
            if watch is None or watch.dismissal is None:
                return None
            if only_if_unnoted and any(
                seq not in watch.publication_notes for seq in watch.standing
            ):
                return None
            record = watch.dismissal
            if not publish:
                return record
            watch.dismissal = None
            record.basis.deferred = False
            SlotRegistry._release_holder_locked(owner, watches, record.basis)
            return None

        if publish:
            with mutex:
                return _settle()
        return _settle()

    @staticmethod
    def settle_written(owner: Any, record: DeferredDismissal) -> None:
        """The marker for *record* is on disk: clear the dismissal and release its holder.

        Called by ``takeover_ordering.record_deferred_dismissal`` after a
        CONFIRMED write, never before. A no-op when the watch has already
        settled it another way (a publication took the key meanwhile and
        cleared it; a later record replaced it), and for ownerless doubles.
        """
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return
        with mutex:
            watch = watches.get(record.name)
            if watch is None or watch.dismissal is not record:
                return
            watch.dismissal = None
            record.basis.deferred = False
            SlotRegistry._release_holder_locked(owner, watches, record.basis)

    @staticmethod
    def dismissal_still_owed(owner: Any, record: DeferredDismissal) -> bool:
        """Whether *record* is still the dismissal owed on its key, with the key unpublished.

        The write-time guard for ``record_deferred_dismissal``: a publication
        that landed while the write was queued has settled the dismissal
        (``put_slot``) or holds the key, and stamping its live transcript closed
        would hide the tab the user just reopened. An explicit resume IN FLIGHT
        on the key (:meth:`begin_resume_read`) answers ``False`` too: it read
        the header before this write and will publish against it, so the
        marker yields and the resume's own exit settles or writes the
        dismissal. ``False`` for ownerless doubles, which cannot be written for.
        """
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return False
        with mutex:
            watch = watches.get(record.name)
            if watch is None or watch.dismissal is not record:
                return False
            if watch.reads_in_flight > 0:
                return False
            slots = getattr(owner, "_slots", None)
            return not (isinstance(slots, dict) and slots.get(record.name) is not None)

    @staticmethod
    def park_owed_unstamp(owner: Any, record: OwedUnstamp) -> None:
        """Keep *record* owed on its key's watch (taking a holder) until an exit clears it.

        Idempotent per key: a second record for the same key replaces the first
        (both name the same line; the newest stamp is the one to remove). A
        no-op for ownerless doubles.
        """
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return
        with mutex:
            watch = watches.get(record.name)
            if watch is None:
                watch = watches[record.name] = TakeoverWatch()
            if watch.unstamp is None:
                watch.holders += 1
            watch.unstamp = record

    @staticmethod
    def owed_unstamp(owner: Any, name: str) -> OwedUnstamp | None:
        """The un-stamp still owed on *name*, for an exit on the key to retry."""
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return None
        with mutex:
            watch = watches.get(name)
            return None if watch is None else watch.unstamp

    @staticmethod
    def settle_unstamped(owner: Any, record: OwedUnstamp) -> None:
        """The stamp is off (or provably gone): drop *record* and release its holder."""
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return
        with mutex:
            watch = watches.get(record.name)
            if watch is None or watch.unstamp is not record:
                return
            watch.unstamp = None
            watch.holders -= 1
            if watch.holders <= 0:
                del watches[record.name]
            _wake_settle_waiters(owner, record.name)

    @staticmethod
    def key_has_takeover_watch(owner: Any, name: str) -> bool:
        """Whether *name* carries a watch: a save in flight, an owed dismissal, or a reader."""
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return False
        with mutex:
            return name in watches

    @staticmethod
    def resume_read_in_flight(owner: Any, name: str) -> bool:
        """Whether an explicit resume is between its header snapshot and its exit on *name*."""
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return False
        with mutex:
            watch = watches.get(name)
            return watch is not None and watch.reads_in_flight > 0

    @staticmethod
    def begin_resume_read(owner: Any, name: str) -> bool:
        """Mark an explicit resume of *name* in flight; ``True`` when a watch recorded it.

        Called by the resume endpoint BEFORE its close-metadata snapshot, so a
        deferred marker write that reaches its guard afterwards yields to this
        resume (:meth:`dismissal_still_owed`). Only a key with an open watch can
        carry the mark -- a key with no in-flight save or owed dismissal has
        nothing for the mark to order against -- and the caller pairs a ``True``
        with exactly one :meth:`end_resume_read`. Holds the watch open through a
        holder of its own, so the mark cannot outlive its watch.
        """
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return False
        with mutex:
            watch = watches.get(name)
            if watch is None:
                return False
            watch.reads_in_flight += 1
            watch.holders += 1
            return True

    @staticmethod
    def end_resume_read(owner: Any, name: str) -> None:
        """Clear a mark :meth:`begin_resume_read` set, releasing the holder it took."""
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return
        with mutex:
            watch = watches.get(name)
            if watch is None:
                return
            watch.reads_in_flight = max(0, watch.reads_in_flight - 1)
            watch.holders -= 1
            if watch.holders <= 0:
                del watches[name]
            _wake_settle_waiters(owner, name)

    @staticmethod
    def owed_dismissal(owner: Any, name: str) -> DeferredDismissal | None:
        """The dismissal still owed on *name* with nobody standing and nobody holding the key.

        The retry hook for an owed write that failed earlier: an exit on the key
        -- a later retraction, a hand-over, a close -- asks for it and writes it
        again. ``None`` when nothing is owed, a resume note still stands, a slot
        holds the key, or for ownerless doubles.
        """
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return None
        with mutex:
            watch = watches.get(name)
            if watch is None or watch.dismissal is None:
                return None
            if any(seq not in watch.publication_notes for seq in watch.standing):
                return None
            if watch.reads_in_flight > 0:
                return None
            slots = getattr(owner, "_slots", None)
            if isinstance(slots, dict) and slots.get(name) is not None:
                return None
            return watch.dismissal

    @staticmethod
    def _release_holder_locked(owner: Any, watches: dict, basis: TakeoverBasis) -> None:
        """:meth:`close_takeover_basis`'s body, for a caller already holding the mutex."""
        if basis.closed:
            return
        basis.closed = True
        watch = watches.get(basis.name)
        if watch is None:
            return
        watch.holders -= 1
        if basis.truncating:
            watch.truncating_holders = max(0, watch.truncating_holders - 1)
        if watch.holders <= 0:
            del watches[basis.name]
        _wake_settle_waiters(owner, basis.name)

    @staticmethod
    def basis_moved(owner: Any, basis: TakeoverBasis | None) -> bool:
        """Report whether *basis*'s key was taken over since the basis opened.

        The holder's post-hoc question: a refused save's caller cannot tell a
        delete-won refusal (the session is gone, nothing to compensate) from a
        takeover refusal (the key belongs to a replacement whose read must not
        find this slot's window) out of the bare ``False`` alone, but it still
        holds the basis it opened before dispatch, and a note newer than the
        basis that still STANDS is exactly the takeover evidence: a noter
        refused after noting withdraws its note and stands behind nothing, so
        a key moved only by such noters is no takeover to yield to -- while a
        retraction of any OTHER note, older or newer, leaves this evidence
        untouched. Only meaningful while the basis is open; answers ``False``
        for ``None`` and for ownerless doubles.
        """
        if basis is None:
            return False
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return False
        with mutex:
            watch = watches.get(basis.name)
            if watch is None:
                return False
            return _standing_since(watch, basis.value, frozenset())

    @staticmethod
    def commit_truncation_claim(owner: Any, claim: TruncationClaim) -> bool:
        """Decide *claim* atomically; ``True`` means the save may replace the file.

        This is the single decision point the whole contract hangs on: the
        transition undecided→committed and a takeover's undecided→overtaken
        are both made under the claim mutex, so exactly one side wins and the
        other observes it. The save calls this immediately before its
        file-replacing write and refuses on ``False`` — nothing else may sit
        between the decision and the replace, or a takeover could slip into
        the gap and read a file this decision already promised was settled.
        """
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if mutex is None:
            return True
        with mutex:
            if not claim.outcome:
                claim.outcome = "committed"
            return claim.outcome == "committed"

    @staticmethod
    def retire_truncation_claim(owner: Any, name: str, claim: TruncationClaim | None) -> None:
        """Forget a settled claim; tolerates absent tables and repeated calls."""
        if claim is None:
            return
        claims = getattr(owner, "_truncation_claims", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(claims, dict) or mutex is None:
            return
        with mutex:
            held = claims.get(name)
            if held is None:
                return
            if claim in held:
                held.remove(claim)
            if not held:
                del claims[name]
            _wake_settle_waiters(owner, name)

    @staticmethod
    def takeover_ordering_pending(owner: Any, name: str) -> bool:
        """Whether a truncating save that must lose to a takeover of *name* is unsettled.

        Two shapes, one question. An OVERTAKEN CLAIM is registered by a save
        the commit gate has refused or will refuse, and it is retired only
        after the save exits the per-session lock — which for a hand-over
        write is AFTER the gate's append-safe fallback landed the popped
        slot's unsaved rows. A TRUNCATING HOLDER of the key's watch is a
        rewrite-shaped save still in dispatch or in flight: its claim may not
        exist yet, but the takeover that just bumped this watch leaves that
        claim born overtaken at registration, so the same refused-save append
        follows — invisibly to a claims-only check. A takeover that reads
        *name*'s transcript while either stands can observe the file without
        those rows: the read side serves cached projections and best-effort
        lock holds, so the per-session lock alone cannot order the read behind
        the refused save's append. Waiting for this predicate to clear is the
        ordering the lock cannot give.

        An APPEND-SHAPED holder does not make the key pending: a plain append
        is a superset of the file, so a reader around it loses nothing, and
        its save registers no claim to be refused. It still holds the watch,
        so a takeover in its dispatch gap is recorded should the save turn
        truncating on the worker (:meth:`mark_basis_truncating`, which makes
        the key pending from that instant). The one schedule this leaves open
        -- a reader that checked before that upgrade -- reads the file the
        refused save then appends behind, exactly as a reader beside any
        in-flight append does on the lock alone; the rows reach disk under the
        refused save's lock either way. ``False`` for owners without real
        tables, matching every other helper here.
        """
        claims = getattr(owner, "_truncation_claims", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(claims, dict) or mutex is None:
            return False
        with mutex:
            return _ordering_pending_locked(owner, name)

    @staticmethod
    async def await_takeover_ordering_settled(owner: Any, name: str, timeout: float) -> bool:
        """Wait ON THE EVENT LOOP until :meth:`takeover_ordering_pending` clears.

        The read half of the takeover-ordering contract, for the resume and
        rehydrate paths. Holds no executor thread: the saves this waits for run
        on the default executor, and a waiter parked there would take the very
        thread one of them needs, so a burst of resumes could starve the saves
        they wait for into this wait's own timeout. The waiter registers a
        future under the claim mutex, atomically with the pending check, and
        every transition that can clear the predicate -- a basis closing, a
        claim retiring -- resolves it through the loop (``call_soon_threadsafe``,
        since those transitions happen on save workers). Bounded by *timeout*:
        ``True`` once settled, ``False`` on expiry, which the caller must treat
        as fail-closed (a resume that reads anyway hydrates a window whose
        missing tail a later rewrite deletes).
        """
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if mutex is None:
            return True
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[bool] = loop.create_future()
        with mutex:
            if not _ordering_pending_locked(owner, name):
                return True
            waiters = _settle_waiters(owner)
            if waiters is None:
                settled_now = False
            else:
                waiters.setdefault(name, []).append((loop, waiter))
                settled_now = True
        if not settled_now:
            # An owner without the waiter table (a partial double): fall back
            # to a loop-side poll, which still holds no thread.
            deadline = loop.time() + timeout
            while SlotRegistry.takeover_ordering_pending(owner, name):
                if loop.time() >= deadline:
                    return False
                await asyncio.sleep(0.002)
            return True
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout)
        except asyncio.TimeoutError:
            with mutex:
                held = _settle_waiters(owner)
                if held is not None and name in held:
                    held[name] = [entry for entry in held[name] if entry[1] is not waiter]
                    if not held[name]:
                        del held[name]
                # The predicate may have cleared in the instant between the
                # expiry and this removal; answer what is true now.
                return not _ordering_pending_locked(owner, name)
        return True

    @staticmethod
    def note_same_key_takeover(owner: Any, name: str, *, transcript: str | None = None) -> bool:
        """Mark every undecided truncating claim on *name* as overtaken.

        The fire-and-forget form of :meth:`open_same_key_takeover_note` for a
        noter that never retracts (a publication): ``True`` when the note was
        admitted, ``False`` when it was refused at admission and nothing was
        recorded.
        """
        return (
            SlotRegistry.open_same_key_takeover_note(owner, name, transcript=transcript) is not None
        )

    @staticmethod
    def open_same_key_takeover_note(
        owner: Any,
        name: str,
        *,
        transcript: str | None = None,
        transcript_stems: frozenset[str] | None = None,
        publication: bool = False,
    ) -> TakeoverNote | None:
        """Mark every undecided truncating claim on *name* as overtaken.

        Called at the two points where a replacement takes over a map key: a
        publication (``put_slot``) and the start of a resume's transcript read.
        Fire-and-forget by design — the caller never branches on the outcome.
        A claim still undecided loses (its save refuses, leaving the file for
        this takeover to read) -- when the takeover's declared read target
        names the file the claim writes (:func:`_transcripts_meet`; either
        side unknown counts): a same-key claim writing a DIFFERENT transcript
        is not raced by this takeover and stays undecided. A claim already
        committed stays committed, and
        this takeover's read — serialized behind the per-session lock the save
        holds across the replace — returns the post-rewrite transcript, which
        is by then the durable truth. The key's open takeover watch, when one
        exists, has its count bumped in the same transition, so a truncating
        save still in dispatch — its claim not yet registered, therefore
        unmarkable — observes this takeover through the basis its
        registration compares against. A key with no open watch records
        nothing: no truncating save is in flight for it, so nobody is left to
        observe the takeover, and skipping the record is what keeps the watch
        table bounded by in-flight saves rather than by every key the process
        ever published.
        Never blocks beyond the mutex, which guards in-memory transitions only
        and is never held across I/O.

        Returns ``None`` -- and records NOTHING, neither the note nor a claim
        mark -- when *transcript* would push the key's watch past
        :data:`_MAX_NOTED_TRANSCRIPTS_PER_WATCH`. The name is request-controlled
        (a resume's ``key``), so the set it lands in is bounded at admission and
        the resume behind an overflowing note is refused by its caller, which
        is the fail-closed answer: a takeover whose declared target the
        teardown cannot retain is a takeover the ordering cannot vouch for.
        A publication passes its slot's resolved stems as *transcript_stems*
        (empty when the routing could not be read, matched as unknown); a
        resume passes the key it reads as *transcript*. A note naming a
        transcript the watch already holds is always admitted. Otherwise returns the note's
        handle (:class:`TakeoverNote`: the admitting watch's identity and the
        sequence it assigned), which the noter hands to
        :meth:`retract_same_key_takeover` should it refuse after noting, or
        :data:`NO_TAKEOVER_NOTE` when the key has no open watch (nothing to
        retract) or the owner has no tables.
        """
        claims = getattr(owner, "_truncation_claims", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(claims, dict) or mutex is None:
            return NO_TAKEOVER_NOTE
        # The declared target is recorded as the stems its spelling resolves
        # to, never as the raw request string: the bound counts stems, and a
        # holder's check matches any spelling of the same file.
        if transcript_stems is not None:
            declared = transcript_stems
        else:
            declared = _transcript_identity(transcript) if transcript else frozenset()
        with mutex:
            watches = getattr(owner, "_takeover_watches", None)
            watch = watches.get(name) if isinstance(watches, dict) else None
            if watch is not None and declared:
                new_stems = declared - watch.noted_transcripts
                if (
                    new_stems
                    and len(watch.noted_transcripts) + len(new_stems)
                    > _MAX_NOTED_TRANSCRIPTS_PER_WATCH
                ):
                    logger.warning(
                        "Takeover note for %s refused: its watch already retains %d declared "
                        "transcript stems, the most one teardown admits",
                        name,
                        len(watch.noted_transcripts),
                    )
                    return None
            note = NO_TAKEOVER_NOTE
            if watch is not None:
                watch.count += 1
                note = TakeoverNote(watch.id, watch.count)
                watch.standing[note.seq] = declared
                if publication:
                    watch.publication_notes.add(note.seq)
                watch.noted_transcripts |= declared
            for claim in claims.get(name, ()):
                if not claim.outcome and _transcripts_meet(declared, claim.transcript):
                    claim.outcome = "overtaken"
            return note

    @staticmethod
    def pop_slot(owner: Any, name: str) -> Any | None:
        """Retract *name* without rebuilding or otherwise touching its slot."""
        return owner._slots.pop(name, None)

    @staticmethod
    def live_slot_count(owner: Any) -> int:
        """Count published and allocated-but-unpublished slots, each once.

        An under-construction slot stays REGISTERED in ``_slots`` throughout its
        hydration (so a concurrent same-key resume dedups against it), so it is in
        BOTH ``_slots`` and ``_slots_under_construction`` at once. Counting the two
        lengths naively double-counts every in-flight resume/import, which would
        refuse admissible imports/forks/creates near the live-slot ceiling with
        fewer than that many real slots. Subtract the overlap so each slot counts
        once: published slots, plus any construction reservation not yet in
        ``_slots`` (a reservation taken before registration, if one ever exists).
        """
        under = getattr(owner, "_slots_under_construction", None) or set()
        return len(owner._slots) + len(under - owner._slots.keys())

    @staticmethod
    def creator_slot_count(owner: Any, creator_key: str) -> int:
        """Count published slots charged to one non-empty caller key under the
        per-caller slot cap: the slots it CREATED (``_created_by``) and the slots
        it REVIVED from history (``_revived_by``, which a revive stamps while
        preserving the target's own creator). A slot that is both counts once.
        """
        if not creator_key:
            return 0
        # Construction reservations carry no creator attribution, so charging
        # them here would assign one caller another caller's in-flight slot.
        return sum(
            1
            for slot in owner._slots.values()
            if getattr(slot, "_created_by", "") == creator_key
            or getattr(slot, "_revived_by", "") == creator_key
        )

    @staticmethod
    def begin_slot_construction(owner: Any, key: str) -> None:
        """Mark *key* as allocated but not yet published."""
        owner._slots_under_construction.add(key)

    @staticmethod
    def end_slot_construction(owner: Any, key: str) -> None:
        """Forget an allocation marker; repeated cleanup is harmless."""
        owner._slots_under_construction.discard(key)

    @staticmethod
    def running_session_keys(
        owner: Any,
        effective_session_key: Callable[[Any], str],
    ) -> frozenset[str]:
        """Return session keys whose current slots have turns in flight."""
        # Storage inventory calls this from a worker thread while the event loop
        # may mutate the registry.  Snapshotting prevents a read-only scan from
        # failing with ``RuntimeError: dictionary changed size``.
        return frozenset(
            effective_session_key(slot) for slot in list(owner._slots.values()) if slot.turn_running
        )

    @staticmethod
    def spend_slot_by_session(
        owner: Any,
        effective_session_key: Callable[[Any], str],
    ) -> dict[str, str]:
        """Map each live session identity to the slot key holding its spend."""
        aliases: dict[str, str] = {}
        for slot in list(owner._slots.values()):
            try:
                session_key = effective_session_key(slot)
            except Exception:  # pragma: no cover - defensive during teardown
                continue
            if session_key:
                # Deliberately last-writer-wins for duplicate session owners,
                # matching insertion-order traversal of the facade registry.
                aliases[session_key] = slot.key
        return aliases

    @staticmethod
    def find_slot_by_session(
        owner: Any,
        session_key: str,
        effective_session_key: Callable[[Any], str],
    ) -> Any | None:
        """Return the first current slot whose effective identity matches."""
        for slot in owner._slots.values():
            if effective_session_key(slot) == session_key:
                return slot
        return None

    @staticmethod
    def get_linked_slot(owner: Any, session_key: str) -> Any | None:
        """Resolve a Slack link and prune its reverse-index row when stale."""
        slot_key = owner._slack_to_slot.get(session_key)
        if not slot_key:
            return None
        slot = owner._slots.get(slot_key)
        if not slot or not slot._slack_linked or slot._slack_thread_ts != session_key:
            owner._slack_to_slot.pop(session_key, None)
            return None
        return slot

    @staticmethod
    def resolve_slot(
        owner: Any,
        name: str,
        short_label_matches: Callable[[str], object | None],
    ) -> Any | None:
        """Resolve an exact key or the newest timestamped bare ``chat-N`` key."""
        slot = owner._slots.get(name)
        if slot is not None:
            return slot
        if not short_label_matches(name):
            return None

        # The separator is part of the prefix so chat-2 cannot match chat-20.
        prefix = name + "-"
        best_timestamp = -1
        best_slot: Any | None = None
        for key, candidate in owner._slots.items():
            if not key.startswith(prefix):
                continue
            tail = key[len(prefix) :]
            try:
                timestamp = int(tail)
            except ValueError:
                timestamp = -1
            # A genuine timestamp tie keeps the first insertion-order match.
            if best_slot is None or timestamp > best_timestamp:
                best_timestamp, best_slot = timestamp, candidate
        return best_slot

    @staticmethod
    def prepare_creation(
        owner: Any,
        name: str | None,
        *,
        mode: str,
        memory_mode: str | None,
        normalize_key: Callable[[str], str],
        mint_key: Callable[[str, int, int], str],
        timestamp_provider: Callable[[], float],
    ) -> tuple[Any | None, SlotCreationPlan | None]:
        """Reuse an existing slot or reserve the key facts for a new one.

        ``(existing, None)`` means construction must stop and return the exact
        registered object.  ``(None, plan)`` means the facade may construct and
        fully configure a slot, then publish it with :meth:`put_slot`.

        Construction, security tagging, session hydration, Slack indexing,
        active-slot sync, and client publication intentionally remain outside
        this registry primitive: those effects form one order-sensitive facade
        transaction and must finish before the slot becomes observable.
        """
        requested_name = ""
        if name:
            requested_name = name
            name = normalize_key(name)
            if not name:
                # A degenerate normalized key follows the ordinary mint path and
                # must not seed a display title from the unusable input.
                requested_name = ""

        # Reuse precedes the reserved-name check.  This permits callers to fetch
        # an already-valid member slot without becoming a member-slot creator.
        if name and name in owner._slots:
            existing = owner._slots[name]
            if memory_mode is not None and memory_mode != existing.memory_mode:
                raise ValueError(
                    f"Slot {name!r} already exists with memory_mode={existing.memory_mode!r}"
                )
            return existing, None

        if name and name.casefold().startswith("member-") and mode != "member":
            raise ValueError("member thread slots are created only via the member thread endpoint")

        minted_new = not name
        if not name:
            # Counter consumption is intentionally not rolled back: even a clock,
            # key-provider, or later slot-construction failure must not reuse it.
            owner._slot_counter += 1
            timestamp = int(timestamp_provider())
            name = mint_key("chat", owner._slot_counter, timestamp)

        return None, SlotCreationPlan(
            key=name,
            requested_name=requested_name,
            minted_new=minted_new,
        )

    @staticmethod
    def reseed_slot_counter(
        owner: Any,
        slot_index_from_key: Callable[[str], int | None],
        logger_provider: Callable[[], logging.Logger],
    ) -> None:
        """Advance the mint counter past every parseable current slot key."""
        max_index = owner._slot_counter
        for name in owner._slots:
            index = slot_index_from_key(name)
            if index is not None and index > max_index:
                max_index = index
        if max_index != owner._slot_counter:
            logger_provider().info(
                "Reseeded slot counter %d -> %d past highest restored slot index",
                owner._slot_counter,
                max_index,
            )
        owner._slot_counter = max_index
