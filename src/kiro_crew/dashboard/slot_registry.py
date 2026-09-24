"""Owner-driven registry operations for dashboard chat slots."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SlotCreationPlan:
    """Registry-owned facts needed to finish constructing a new slot."""

    key: str
    requested_name: str
    minted_new: bool


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

    __slots__ = ("outcome",)

    def __init__(self) -> None:
        self.outcome = ""


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

    __slots__ = ("name", "value", "closed", "deferred")

    def __init__(self, name: str, value: int) -> None:
        self.name = name
        self.value = value
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


class TakeoverWatch:
    """Refcounted per-key takeover counter; exists only while bases are open."""

    __slots__ = ("holders", "count", "noted_transcripts")

    def __init__(self) -> None:
        self.holders = 0
        self.count = 0
        # Transcript keys the takeovers that moved this watch declared as
        # their read target (resume paths know it; a bare publication notes
        # none). Lets a basis holder whose own write routes to a LINKED
        # transcript tell "a takeover is coming for the file I write" from
        # "the key changed hands but my file is not involved" — the map
        # cannot answer that while the takeover's publication is still
        # pending behind its read. Dies with the watch, so it is bounded the
        # same way the watch is.
        self.noted_transcripts: set[str] = set()


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
        # announcement is never observable later than the slot itself.
        SlotRegistry.note_same_key_takeover(owner, name)
        owner._slots[name] = slot
        return slot

    @staticmethod
    def begin_truncation_claim(
        owner: Any, name: str, *, basis: TakeoverBasis | None = None
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
        mark, so the registration compares the watch's takeover count against
        the basis snapshot: a moved count means the key changed hands while
        the save was still in dispatch, and the claim is born already
        overtaken. ``None`` means the dispatcher anchored to no earlier
        instant, and the claim observes only takeovers from registration on.

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
        claim = TruncationClaim()
        with mutex:
            if basis is not None:
                watches = getattr(owner, "_takeover_watches", None)
                watch = watches.get(basis.name) if isinstance(watches, dict) else None
                if watch is not None and watch.count != basis.value:
                    claim.outcome = "overtaken"
            claims.setdefault(name, []).append(claim)
        return claim

    @staticmethod
    def open_takeover_basis(owner: Any, name: str) -> TakeoverBasis | None:
        """Open a takeover watch on *name* and snapshot its current count.

        Called at a dispatcher's last synchronous instant before it hands a
        truncating save off, paired with :meth:`close_takeover_basis` in the
        same dispatcher's ``finally`` once the save's outcome is in hand.
        Owners without the tables answer ``None``, and the paired claim
        registration on such an owner returns no claim either.
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
            return TakeoverBasis(name, watch.count)

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
            if watch.holders <= 0:
                del watches[basis.name]

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
        with mutex:
            watch = watches.get(basis.name)
            return watch is not None and transcript in watch.noted_transcripts

    @staticmethod
    def basis_moved(owner: Any, basis: TakeoverBasis | None) -> bool:
        """Report whether *basis*'s key was taken over since the basis opened.

        The holder's post-hoc question: a refused save's caller cannot tell a
        delete-won refusal (the session is gone, nothing to compensate) from a
        takeover refusal (the key belongs to a replacement whose read must not
        find this slot's window) out of the bare ``False`` alone, but it still
        holds the basis it opened before dispatch, and a moved count is
        exactly the takeover evidence. Only meaningful while the basis is
        open; answers ``False`` for ``None`` and for ownerless doubles.
        """
        if basis is None:
            return False
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(watches, dict) or mutex is None:
            return False
        with mutex:
            watch = watches.get(basis.name)
            return watch is not None and watch.count != basis.value

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

    @staticmethod
    def takeover_ordering_pending(owner: Any, name: str) -> bool:
        """Whether a truncating save that must lose to a takeover of *name* is unsettled.

        Two shapes, one question. An OVERTAKEN CLAIM is registered by a save
        the commit gate has refused or will refuse, and it is retired only
        after the save exits the per-session lock — which for a hand-over
        write is AFTER the gate's append-safe fallback landed the popped
        slot's unsaved rows. An OPEN WATCH is a truncating save still in
        dispatch: its claim does not exist yet, but the takeover that just
        bumped this watch's count leaves that claim born overtaken at
        registration, so the same refused-save append follows — invisibly to
        a claims-only check. A takeover that reads *name*'s transcript while
        either stands can observe the file without those rows: the read side
        serves cached projections and best-effort lock holds, so the
        per-session lock alone cannot order the read behind the refused
        save's append. Waiting for this predicate to clear — watches and all
        resulting claims, through retirement — is the ordering the lock
        cannot give. The watch table holds a key only while a dispatcher's
        basis is open, so the wait is bounded by in-flight saves. ``False``
        for owners without real tables, matching every other helper here.
        """
        claims = getattr(owner, "_truncation_claims", None)
        watches = getattr(owner, "_takeover_watches", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(claims, dict) or mutex is None:
            return False
        with mutex:
            if isinstance(watches, dict) and name in watches:
                return True
            return any(claim.outcome == "overtaken" for claim in claims.get(name, []))

    @staticmethod
    def note_same_key_takeover(owner: Any, name: str, *, transcript: str | None = None) -> None:
        """Mark every undecided truncating claim on *name* as overtaken.

        Called at the two points where a replacement takes over a map key: a
        publication (``put_slot``) and the start of a resume's transcript read.
        Fire-and-forget by design — the caller never branches on the outcome.
        A claim still undecided loses (its save refuses, leaving the file for
        this takeover to read); a claim already committed stays committed, and
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
        """
        claims = getattr(owner, "_truncation_claims", None)
        mutex = getattr(owner, "_truncation_claim_mutex", None)
        if not isinstance(claims, dict) or mutex is None:
            return
        with mutex:
            watches = getattr(owner, "_takeover_watches", None)
            if isinstance(watches, dict):
                watch = watches.get(name)
                if watch is not None:
                    watch.count += 1
                    if transcript:
                        watch.noted_transcripts.add(transcript)
            for claim in claims.get(name, ()):
                if not claim.outcome:
                    claim.outcome = "overtaken"

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
        """Count published slots attributed to one non-empty creator key."""
        if not creator_key:
            return 0
        # Construction reservations carry no creator attribution, so charging
        # them here would assign one caller another caller's in-flight slot.
        return sum(
            1 for slot in owner._slots.values() if getattr(slot, "_created_by", "") == creator_key
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
