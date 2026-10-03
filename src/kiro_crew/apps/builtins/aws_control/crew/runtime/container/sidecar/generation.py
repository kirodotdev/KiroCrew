"""The committed-generation pointer, read the same way by both processes.

Each cycle publishes its two authority files as a PAIR into a WRITER-UNIQUE generation --
``gen/<id>/session_map.json`` and ``gen/<id>/open_slots.json``, where ``<id>`` is minted
fresh by :func:`keys.new_generation_id` and never rewritten. A single object -- the
pointer -- names the generation id whose pair is committed, and the commit is a
compare-and-swap on that one object. Three consequences follow, and they are the whole
reason the protocol has this shape:

* Two writers racing in the task-replacement window each mint a DISTINCT id and write a
  distinct generation, so neither overwrites the other's pair and no committed pair is a
  cross-writer tear. The compare-and-swap on the pointer settles which one generation is
  the committed one.
* A cycle interrupted between the pair's two PUTs damages only its own generation, which
  no pointer references. The pointer still names the previous generation, whose objects
  are immutable and were never rewritten, so a replacement boots from a coherent older
  pair rather than a torn newer one.
* Commitment is a single object, and a single object is either there or it is not. There
  is no state in which half a commitment is visible.

The pointer's ABSENCE is meaningful rather than an error. A bucket written before this
protocol holds the authority objects at their ``data/`` keys with no pointer, and that is
read as GENERATION 0. Those objects are never deleted, moved or rewritten, so a bucket
does not have to be migrated to be read, and a writer that predates the protocol keeps
producing buckets this one understands.

The interpretation lives here and neither process keeps its own copy, for the reason
``keys.py`` gives for key derivation: the writer and the reader agreeing with each other
while both disagree with the contract is the failure this shape makes unrepresentable.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from ..common import Settings, keys
from ..common.config import MAX_OBJECT_BYTES
from .store import ObjectAbsent, ObjectStore, PreconditionFailed, StoreUnusable

log = logging.getLogger("smc.sidecar.generation")

__all__ = [
    "Pointer",
    "PointerUnusable",
    "read_pointer",
    "pointer_body",
    "ClaimedOwnership",
    "claim_ownership",
    "release_ownership",
    "ENV_CLAIMED_INCARNATION",
    "TranscriptIndexUnusable",
    "read_transcript_index",
]

#: The environment variable the supervisor sets to the incarnation it claimed in shared
#: storage (:func:`claim_ownership`) BEFORE restoration, and the sidecar reads at startup to
#: ADOPT rather than mint its own. One definition here, imported by both the supervisor (which
#: writes it) and the sidecar entrypoint (which reads it), so the two cannot drift on the name.
#: Empty or unset means no claim was made (generation 0, or no bucket), and the sidecar mints
#: its base token as before.
ENV_CLAIMED_INCARNATION = "SMC_CLAIMED_INCARNATION"


class PointerUnusable(RuntimeError):
    """The pointer is present and cannot be used.

    Distinct from absent, and the distinction decides a boot. Absent means no generation
    has been committed, so the legacy keys are generation 0 and a task starts from them.
    Present-but-unusable means a generation may well be committed and this task cannot
    tell which -- reading that as absence would boot from objects the pointer was steering
    away from.
    """


class TranscriptIndexUnusable(RuntimeError):
    """The committed generation's transcript index is present and cannot be trusted.

    Distinct from absent for the same reason :class:`PointerUnusable` is: an absent index
    (generation 0, or a generation that committed no transcripts) means the front falls
    back to the legacy per-stem key, while an index that is present and will not parse means
    the mapping this task needs to find a blob exists and cannot be read -- and reading a
    stem's blob from the wrong place, or from a legacy key a newer writer never wrote, would
    serve an empty or stale history. So it is refused rather than treated as absent.
    """


@dataclass(frozen=True)
class Pointer:
    """The committed generation: which generation id, and which files it was committed with."""

    generation: str
    authority: frozenset[str]
    #: The INCARNATION of the task that committed this pointer: a lexically-sortable token
    #: whose leading field is a MONOTONIC COUNTER derived from the committed pointer at task
    #: startup (:func:`keys.next_incarnation`), strictly greater for a later-started task. It
    #: is NOT a wall clock -- ordering tasks by a host clock is unsound across hosts, so the
    #: counter is read from shared storage and incremented instead. It fences a SUPERSEDED task
    #: from rolling the committed generation backward. The compare-and-swap on the ETag below
    #: rejects a commit whose read of the pointer went stale, but it cannot catch one overlap:
    #: a predecessor whose cycle BEGAN AFTER its replacement committed reads the replacement's
    #: pointer, so it holds a FRESH ETag and its ``If-Match`` would succeed, publishing its
    #: older local-only state over the replacement's. :func:`~..backup._commit_generation`
    #: refuses to commit when the committed incarnation is strictly newer than its own, so that
    #: predecessor steps aside. A pointer written before this field carries ``""``, which never
    #: compares as newer -- a bucket an earlier writer made stays committable.
    incarnation: str = ""
    #: The object's ETag when this pointer was read, or ``None`` when the store could not
    #: supply one. It is the compare-and-swap validator the commit re-presents as
    #: ``If-Match``: a writer that advanced the pointer since this read changes the ETag, so a
    #: stale commit is rejected rather than overwriting. ``None`` is a MISSING validator, and
    #: the commit fails CLOSED on it -- committing unconditionally would defeat the guard.
    #:
    #: The ETag and the incarnation are two independent fences and BOTH are required: the
    #: ETag catches a concurrent write to the pointer object, and the incarnation catches a
    #: superseded task whose stale commit the ETag check alone would admit (it read a fresh
    #: pointer). The objects they bless are immutable and writer-unique -- a pair at
    #: ``gen/<id>/``, a transcript at ``data/blob/<digest>`` -- so there is no mutable object
    #: a slower writer could tear; what remains to settle is only WHICH already-written
    #: generation the pointer names, and by WHICH task.
    etag: str | None = None
    #: True when this pointer is a PROVISIONAL ownership claim written by
    #: :func:`claim_ownership` before a task became ready, rather than a real generation
    #: commit. Defaults false, so a pointer written by any commit path (or by a writer that
    #: predates the field) reads as not-a-claim. Only :func:`release_ownership` reads it, to
    #: refuse reverting to another aborting task's claim; the commit fence never consults it,
    #: so a claim pointer orders by incarnation exactly like a committed one.
    is_claim: bool = False
    #: On a CLAIM pointer, the incarnation of the last REAL committed generation this claim
    #: chain bumped from -- the live predecessor a release must restore, carried verbatim
    #: through a nest of provisional claims so a second claimant that bumped from a first
    #: claimant still restores the ORIGINAL committed owner rather than the dead first
    #: claimant's token (F1/F3). On a committed pointer it is ``""`` and unused: the committed
    #: owner there IS :attr:`incarnation`. A reader that predates the field reads ``""``.
    committed_incarnation: str = ""


def pointer_body(
    generation_id: str,
    incarnation: str = "",
    *,
    claim: bool = False,
    committed_incarnation: str = "",
) -> bytes:
    """The pointer's bytes for a commitment of *generation_id* by task *incarnation*.

    One function so the writer's bytes and the reader's expectations cannot drift; the
    reader's own parsing is the other half and lives in :func:`read_pointer`. *incarnation*
    is the committing task's freshness token, carried so a later read can refuse a commit
    from a task a replacement has superseded.

    *claim* marks a PROVISIONAL ownership bump written by :func:`claim_ownership` before a task
    is ready, as opposed to a real generation commit. It defaults false, so every commit path
    writes an unmarked pointer and the field is invisible to readers that predate it. The
    release path reads it to tell a committed predecessor's pointer (safe to revert to) apart
    from another aborting task's provisional claim (which must NOT be reverted to -- reverting
    to a dead claimant's token leaves the live predecessor fenced; see
    :func:`release_ownership`). A reader that does not know the field ignores it, and the fence
    never reads it, so a marked pointer orders exactly like an unmarked one.

    *committed_incarnation* rides a claim pointer only: it names the last REAL committed owner
    the claim chain bumped from, so a release can restore THAT owner even when the claim was
    bumped from an earlier (also aborting) claim. It defaults ``""`` and is written only when
    non-empty, so a committed pointer never carries it.
    """
    doc: dict[str, object] = {
        "generation": generation_id,
        "incarnation": incarnation,
        "authority": sorted(keys.AUTHORITY_NAMES),
    }
    if claim:
        doc["claim"] = True
    if committed_incarnation:
        doc["committed_incarnation"] = committed_incarnation
    return json.dumps(doc, sort_keys=True).encode("utf-8")


def read_pointer(
    settings: Settings,
    store: ObjectStore,
    *,
    deadline: float | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Pointer | None:
    """The committed generation, or ``None`` when no pointer has been published.

    ``None`` is the generation-0 answer: the bucket either holds the legacy authority keys
    or holds nothing at all, and both are states a task may boot from.

    Raises :class:`PointerUnusable` for every other way this can go -- a read that fails,
    bytes that do not parse, a slot this writer does not publish into, a missing file list.
    A pointer that exists and cannot be trusted is not permission to look elsewhere.

    *deadline* bounds the GET on the final cycle: the pointer read runs inside the drain
    window, and a trickling connection that never trips the socket timeout would otherwise
    outlive the window and be SIGKILLed before the final commit. Passed through so the read
    is cut as a verdict instead.

    *cancel* is the stop predicate an INTERVAL cycle threads through this preflight read: an
    interval has no deadline (it has a next interval) but its pointer GET must still be cut
    the instant a stop arrives, or a trickling read would run across the SIGTERM and eat the
    drain window the final cycle needs (GPT F2).
    """
    key = keys.authority_pointer_key(settings)
    try:
        raw, etag = store.get_with_etag(
            key, limit=MAX_OBJECT_BYTES, deadline=deadline, cancel=cancel
        )
    except ObjectAbsent:
        return None
    except StoreUnusable:
        # Not translated: the bucket itself cannot be read, so every later read meets the
        # same answer and the process must end on it rather than report a pointer problem.
        raise
    except Exception as exc:  # noqa: BLE001 - translated, never swallowed
        raise PointerUnusable(
            f"the generation pointer could not be read from the bucket ({exc}). This is "
            "not the same as it being absent: absent means no generation is committed and "
            "the legacy keys are this bucket's truth, while unreadable means one may be "
            "committed and this task cannot tell which."
        ) from exc
    # ``etag`` rode the SAME ``get_object`` as the bytes (one GET), so it is the validator for
    # exactly the pointer this cycle read -- no second HEAD a concurrent commit could slip
    # between (which would let a stale ``If-Match`` pass) and nothing unbudgeted for the final
    # cycle's drain window to be killed inside. A store that supplies no ETag leaves it None,
    # and the commit fails CLOSED on a missing validator.
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PointerUnusable(
            f"the generation pointer in the bucket does not parse ({exc}), so it cannot "
            "say which generation is committed."
        ) from exc
    if not isinstance(parsed, dict):
        raise PointerUnusable(
            f"the generation pointer in the bucket is a JSON {type(parsed).__name__}, not "
            "an object, so it names no generation."
        )
    generation_id = parsed.get("generation")
    if not keys.is_generation_id(generation_id):
        raise PointerUnusable(
            f"the generation pointer names generation {generation_id!r}, which is not a "
            "generation id this writer mints (a zero-padded nanosecond timestamp, a hyphen "
            "and hex). No cycle of this writer published it, so the objects it points at are "
            "not a generation this task can read."
        )
    assert isinstance(generation_id, str)  # narrowed by is_generation_id above
    # The task incarnation. A pointer written before this field existed has no 'incarnation'
    # key, read as "" -- the same tolerance the missing-but-newer-field rules below rely on,
    # and "" never compares as newer than a real token, so a bucket an earlier writer made
    # stays committable. A PRESENT incarnation must be a well-formed token (``new_incarnation``
    # mints the generation-id grammar: 20-digit ns, a hyphen, hex). The commit fence compares
    # incarnations LEXICALLY, and a non-conforming non-empty value -- a bare ``"z"``, say --
    # sorts GREATER than every real token, so it would make ``committed.incarnation > this``
    # true for every future task and freeze the commit permanently, restoring stale history.
    # So it is validated by the SAME shape check that guards the generation id (this function
    # exists to distrust bucket bytes), unusable rather than coerced. "" is the one accepted
    # non-token value, for the back-compat reason above.
    raw_incarnation = parsed.get("incarnation", "")
    if raw_incarnation != "" and not keys.is_generation_id(raw_incarnation):
        raise PointerUnusable(
            f"the generation pointer carries incarnation {raw_incarnation!r}, which is "
            "neither empty nor a well-formed task incarnation (a zero-padded nanosecond "
            "timestamp, a hyphen and hex). A malformed value would freeze the commit fence "
            "permanently, so the pointer is unusable rather than trusted."
        )
    # The carried committed owner rides a claim pointer only (see :attr:`Pointer.
    # committed_incarnation`). It is validated by the same shape check as the incarnation --
    # a malformed carried token would, on a later restore, write a pointer that freezes the
    # fence -- and "" (a committed pointer, or a claim that bumped straight from a committed
    # owner) is the accepted empty value.
    raw_committed = parsed.get("committed_incarnation", "")
    if raw_committed != "" and not keys.is_generation_id(raw_committed):
        raise PointerUnusable(
            f"the generation pointer carries committed_incarnation {raw_committed!r}, which "
            "is neither empty nor a well-formed task incarnation; a malformed carried owner "
            "would freeze the commit fence on a later restore, so the pointer is unusable."
        )
    listed = parsed.get("authority")
    if not isinstance(listed, list) or not all(isinstance(name, str) for name in listed):
        raise PointerUnusable(
            "the generation pointer has no 'authority' list of names, so it cannot say "
            "which files the committed generation contains."
        )
    named = frozenset(listed)
    missing = [name for name in keys.AUTHORITY_NAMES if name not in named]
    if missing:
        raise PointerUnusable(
            f"the generation pointer commits a generation without {', '.join(missing)}, "
            "and this writer commits the authority pair whole or not at all. Read as a "
            "partial generation it would present a name this task knows as legitimately "
            "absent, and the backend would flush its own empty view over it -- so the "
            "pointer is unusable rather than a generation missing a member."
        )
    # Names this version does not know are dropped, not refused: a bucket written by a
    # newer writer that commits a third authority file still names a generation whose
    # pair this one can read, and refusing it would make a rollback unbootable. The
    # check above is what keeps that tolerance from also admitting a pointer that
    # under-lists a name this version DOES know.
    return Pointer(
        generation=generation_id,
        authority=frozenset(n for n in named if n in keys.AUTHORITY_NAMES),
        incarnation=raw_incarnation,
        etag=etag,
        is_claim=parsed.get("claim") is True,
        committed_incarnation=raw_committed,
    )


@dataclass(frozen=True)
class ClaimedOwnership:
    """The result of a writer-ownership claim, carrying what :func:`release_ownership` needs.

    ``token`` is the claimed incarnation the sidecar adopts (``""`` for generation 0 / no
    bucket, where nothing was written). ``prior_body`` is the committed OWNER's pointer bytes
    -- a real generation commit, carried through any nest of provisional claims so it is the
    LIVE predecessor's state and never a dead claimant's -- which :func:`release_ownership`
    restores on an aborted startup so the predecessor's own committed incarnation stands again
    and the fence stops superseding it. ``prior_etag`` is the ETag the claim's OWN write LEFT
    on the pointer (taken from the ``PutObject`` response itself): it is the release's
    compare-and-swap validator, so a release matches only while this claim is still the
    committed pointer and is a no-op once a newer successor has claimed on top. Both are
    ``None`` when nothing was claimed (nothing to release) or when the claim's write returned
    no ETag (no safe validator, so rollback is disabled).
    """

    token: str
    prior_body: bytes | None = None
    prior_etag: str | None = None


def claim_ownership(settings: Settings, store: ObjectStore) -> ClaimedOwnership:
    """Claim writer ownership in SHARED STORAGE before restoration, returning this task's token.

    The handoff hazard this closes: restoration reads the authority pair before any writer
    ownership exists, and the successor's own incarnation is not minted until its sidecar
    starts -- after the backend and front. In that window an overlapping predecessor can add
    a slot, and the successor then captures its now-stale pair and commits it under a newer
    incarnation, deleting the predecessor's slot from committed history. The commit fence only
    refuses the predecessor AFTER the successor has committed, which is too late.

    So the successor claims ownership FIRST, at the moment of this call -- before restoration
    -- by bumping the committed pointer's incarnation IN PLACE: the generation id and the
    authority list are preserved (this is not a new commit, it names the same generation), and
    only the incarnation advances to :func:`keys.next_incarnation` of what was committed. The
    bump is a COMPARE-AND-SWAP on the pointer's ETag, so two successors racing to claim settle
    on one; the loser re-reads and claims from the winner's token. From this instant the
    committed incarnation is strictly greater than any predecessor's, so a predecessor's later
    commit is refused by :func:`keys.incarnation_supersedes` -- the fence now bites from BEFORE
    restoration rather than after the successor's first commit.

    The returned token is this task's incarnation, which the sidecar MUST adopt (rather than
    re-deriving its own, which would bump a second time and leave the committed pointer naming
    a token no live task holds). The successor's own commits carry exactly this token, so when
    its sidecar reads the pointer it finds its own incarnation committed -- an equal, identical
    token the fence does not treat as superseding -- and commits normally.

    GENERATION 0 (no committed pointer) returns a :class:`ClaimedOwnership` whose ``token`` is
    ``""`` and whose prior bytes are ``None`` and writes NOTHING: there is no
    committed generation for a predecessor to roll back to, so there is nothing to fence and
    no pointer to bump -- publishing one here would commit an empty generation and strand the
    first real commit's legacy migration. Two generation-0 racers are settled by the commit's
    own equal-counter rule and ETag CAS, unchanged. The sidecar mints its base token as before.

    :class:`PointerUnusable` PROPAGATES: a pointer that is present but unreadable must not be
    claimed from (minting a base token would be strictly LESS than a predecessor's committed
    one and fence this task forever), so the task refuses to start -- the same posture the
    restore and the sidecar's startup incarnation take.
    """
    pointer = read_pointer(settings, store)
    if pointer is None:
        # Generation 0: no committed pointer to CAS-bump, and writing one here would commit an
        # empty generation and strand the first real commit's legacy migration -- so nothing is
        # written and there is nothing to release. But the task still MINTS its base token now,
        # before restoration, and exports it for the sidecar to ADOPT. Without that the sidecar
        # would derive its token from the committed pointer AFTER restoration: a predecessor
        # whose own first commit lands in the window between this task restoring empty tables
        # and its sidecar starting would push that derived token to a higher counter, and the
        # sidecar would then supersede the predecessor and overwrite its just-committed slots.
        # Adopting a pre-restore base token (counter 1) instead leaves the predecessor's own
        # first commit (also counter 1, distinct token) to be settled by the commit's
        # equal-counter rule and the pointer CAS -- the stale replacement cannot auto-win.
        return ClaimedOwnership(token=keys.new_incarnation())
    claimed = keys.next_incarnation(pointer.incarnation)
    key = keys.authority_pointer_key(settings)
    if pointer.etag is None:
        # No CAS validator for the claim write. Fail closed rather than claiming blind, for
        # the same reason the commit does: an unconditional write could clobber a concurrent
        # claimer. The task refuses to start; a restart re-reads a pointer with an ETag.
        raise PointerUnusable(
            "the committed pointer carried no ETag validator, so writer ownership cannot be "
            "claimed under a compare-and-swap before restoration; the task refuses to start "
            "rather than claim ownership blind over a concurrent writer."
        )
    # The CARRIED COMMITTED OWNER (F1/F3). A claim must be able to restore the LIVE predecessor
    # on abort, not merely the pointer it happened to bump from -- which, in a nest of
    # overlapping aborting replacements, is ANOTHER claim whose task is also dying. So each
    # claim records the last REAL committed owner its chain descends from: when it bumps from
    # a committed pointer that owner is that pointer's own incarnation; when it bumps from a
    # claim the owner is carried forward from that claim's ``committed_incarnation``. The claim
    # WRITES this owner into its own pointer (so the next claimant carries it on), and builds
    # ``prior_body`` as that owner's committed pointer bytes -- so release restores the real
    # predecessor whether this is the first claim or the tenth in a crash loop.
    committed_owner = pointer.committed_incarnation if pointer.is_claim else pointer.incarnation
    body = pointer_body(
        pointer.generation, claimed, claim=True, committed_incarnation=committed_owner
    )
    # The release restores the committed OWNER's pointer bytes -- a real generation commit
    # (unmarked), never a provisional claim -- so a revert can never reinstate a dead
    # claimant's token. The release's CAS validator is the ETag the claim's OWN write returns:
    # :meth:`ObjectStore.put` hands back the ETag from its own ``PutObject`` response, the one
    # request known to have committed exactly these bytes. Taking it from there rather than
    # from a follow-up GET closes F1: a confirmation GET after the claim can fail or time out
    # on a degraded link even though the PUT already committed -- and that exception would
    # escape before any ``ClaimedOwnership`` exists to release, leaving a live predecessor
    # fenced for good -- and it can also read a NEWER writer's ETag if one committed in the
    # window, binding the revert to someone else's write. The response ETag has neither hazard.
    # ``None`` (no ETag on the response) DISABLES rollback: the claim stands but the abort path
    # will not revert, because it has no safe validator; a restart re-reads and settles.
    prior_body = pointer_body(pointer.generation, committed_owner)
    try:
        release_etag = store.put(key, io.BytesIO(body), len(body), if_match=pointer.etag)
    except PreconditionFailed:
        # A concurrent successor claimed first since this read. Re-read its token and claim
        # from it: one more bump settles the two, and a persistent fight is not possible
        # because each claim strictly advances the counter. One retry, not a loop -- a second
        # defeat means genuine contention a restart resolves without burning the boot here.
        current = read_pointer(settings, store)
        if current is None or current.etag is None:
            raise PointerUnusable(
                "a concurrent writer changed the pointer while ownership was being claimed, "
                "and the re-read did not yield a pointer with a CAS validator; the task "
                "refuses to start rather than claim ownership blind."
            ) from None
        claimed = keys.next_incarnation(current.incarnation)
        committed_owner = current.committed_incarnation if current.is_claim else current.incarnation
        body = pointer_body(
            current.generation, claimed, claim=True, committed_incarnation=committed_owner
        )
        prior_body = pointer_body(current.generation, committed_owner)
        release_etag = store.put(key, io.BytesIO(body), len(body), if_match=current.etag)
    if release_etag is None:
        log.info(
            "ownership: rollback disabled -- the claim's write returned no ETag validator, so "
            "a later release cannot revert under a compare-and-swap; a restart re-reads and "
            "settles."
        )
    log.info(
        "sidecar: claimed writer ownership before restoration at incarnation counter %s so a "
        "predecessor's later commit is fenced from this instant rather than after the first "
        "commit.",
        keys.incarnation_counter(claimed),
    )
    return ClaimedOwnership(
        token=claimed,
        prior_body=prior_body,
        prior_etag=release_etag,
    )


def release_ownership(settings: Settings, store: ObjectStore, claim: ClaimedOwnership) -> bool:
    """Undo a writer-ownership claim whose task is aborting before it ever became ready.

    The claim bumps the committed incarnation BEFORE restoration so a predecessor is fenced
    from that instant. That is correct for a task that goes on to run -- but a task whose
    backend never reaches readiness aborts without ever committing anything, and the bumped
    incarnation it leaves behind outlives it: a still-healthy predecessor's every later commit
    is then refused as superseded by :func:`keys.incarnation_supersedes`, and the turns it
    serves are silently never durable. A failed replacement must not permanently fence a live
    predecessor, so the abort path releases the claim here.

    The release restores the committed OWNER's pointer bytes the claim captured -- a real
    generation commit, carried through any nest of provisional claims so it is the LIVE
    predecessor's state, never a dead claimant's -- under an ``If-Match`` on the ETag the
    claim's own write produced, so it is a compare-and-swap against the pointer the claim left,
    not a blind write:

    * If the committed pointer is STILL the one this claim wrote, the CAS matches and the
      predecessor's own incarnation is committed again verbatim; the fence stops superseding
      it. Restoring the exact committed-owner token (not merely a lower counter) is what
      keeps the equal-counter tie-break from fencing the predecessor by identity.
    * If another successor has since claimed on top of this one, the ETag does not match,
      the CAS fails, and the release is a NO-OP -- reverting would clobber that newer, live
      claim and un-fence this aborting task against it. The newer successor's own abort path
      (or its first commit) owns the pointer now, so leaving it is correct.

    Because ``prior_body`` is the carried COMMITTED OWNER rather than the immediate pointer the
    claim bumped from, a nest of overlapping aborting claims each restore the SAME real
    predecessor rather than reinstating one another's dead tokens (F1/F3): whichever release
    wins the CAS lands the live predecessor, and the rest no-op.

    Returns ``True`` when the prior pointer was restored, ``False`` when there was nothing to
    release (generation 0 / no bucket, or the claim's write returned no ETag validator) or a
    newer claim held the pointer. Never raises for a lost CAS -- the abort is already in
    progress and the original failure must propagate; a best-effort release that could not run
    leaves the pointer for the newer owner or a restart to settle, which is the same posture
    as not having claimed.
    """
    if claim.prior_body is None or claim.prior_etag is None:
        # Nothing was written (generation 0, or no bucket), or the claim's write returned no
        # ETag so rollback was disabled: nothing to release.
        return False
    key = keys.authority_pointer_key(settings)
    try:
        store.put(
            key,
            io.BytesIO(claim.prior_body),
            len(claim.prior_body),
            if_match=claim.prior_etag,
        )
    except PreconditionFailed:
        # A newer successor claimed on top of ours since we wrote it. Reverting would clobber
        # that live claim, so leave the pointer where it is -- the newer owner settles it.
        log.info(
            "ownership: release skipped -- a newer writer claimed the pointer after this "
            "aborted task did; its claim stands and this task's is already superseded."
        )
        return False
    except StoreUnusable:
        # The bucket itself is unreadable/unwritable; the abort is ending the process anyway
        # and a restart re-reads the pointer. Do not mask the original startup failure.
        log.warning("ownership: release could not be written (store unusable); aborting anyway")
        return False
    log.info(
        "ownership: released the pre-readiness claim; the predecessor's committed incarnation "
        "stands again so a failed replacement does not fence a live predecessor."
    )
    return True


def read_transcript_index(
    settings: Settings,
    store: ObjectStore,
    generation_id: str,
    *,
    deadline: float | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, str]:
    """The committed generation's ``stem -> blob-digest`` map. Never treats absence as empty.

    Called only with a COMMITTED generation id, and every committing cycle writes this index
    object before it commits the pointer -- an EMPTY index when the generation has no live
    transcript, never no object at all. So an object that is ABSENT here is one that external
    retention DELETED while the committed pointer still references it, not a generation that
    legitimately published none. Treating that absence as ``{}`` would read every stem as a
    fresh conversation and the next cycle would overwrite the real history with no recovery
    -- so :class:`ObjectAbsent` is refused as :class:`TranscriptIndexUnusable` rather than
    returning empty. A generation-0 bucket (no committed pointer) never reaches here: the
    caller resolves that before asking for an index.

    Raises :class:`TranscriptIndexUnusable` when the object is present and will not parse,
    or maps a stem to something that is not a well-formed blob digest -- because a value that
    is not a digest cannot be turned into a blob key, and guessing past it would send the
    front to the wrong object. A read that fails for a transport reason is unusable too; a
    denial is never read as absence, for the reason the pointer read gives.

    The values are validated to be blob digests HERE so the front, which turns them into
    keys, cannot be handed a value that carries a path -- the same containment the pointer's
    generation-id check provides.

    *deadline* bounds the GET on the final cycle, for the same reason the pointer read is
    bounded: this index can be multi-megabyte and its read runs inside the drain window, so a
    trickling connection the socket timeout never trips is cut as a verdict rather than
    SIGKILLed before the final commit.

    *cancel* is the stop predicate an INTERVAL cycle threads through this preflight read: the
    index can be multi-megabyte and an interval read has no deadline, so without the stop a
    trickling read would run across the SIGTERM and eat the drain window the final cycle needs
    (GPT F2). With it the interval read ends at the stop.
    """
    key = keys.transcript_index_key(settings, generation_id)
    try:
        raw = store.get(key, limit=MAX_OBJECT_BYTES, deadline=deadline, cancel=cancel)
    except ObjectAbsent as exc:
        raise TranscriptIndexUnusable(
            f"the transcript index for the committed generation {generation_id} is absent. "
            "Every committing cycle writes this object (empty when there is no transcript) "
            "before it commits the pointer, so an absent index under a committed pointer is "
            "one retention deleted while it was still referenced -- not a generation that "
            "published none. Reading it as empty would serve every conversation's history as "
            "a fresh one and overwrite it, so the task refuses rather than treat absence as "
            "empty."
        ) from exc
    except StoreUnusable:
        raise
    except Exception as exc:  # noqa: BLE001 - translated, never swallowed
        raise TranscriptIndexUnusable(
            f"the transcript index for generation {generation_id} could not be read "
            f"({exc}). This is not the same as it being absent, so the front refuses to "
            "serve a stale or empty history rather than fall back to a legacy key a newer "
            "writer never wrote."
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TranscriptIndexUnusable(
            f"the transcript index for generation {generation_id} does not parse ({exc})."
        ) from exc
    if not isinstance(parsed, dict):
        raise TranscriptIndexUnusable(
            f"the transcript index for generation {generation_id} is a JSON "
            f"{type(parsed).__name__}, not an object mapping stems to blob digests."
        )
    for stem, digest in parsed.items():
        if not isinstance(stem, str) or not keys.is_blob_digest(digest):
            raise TranscriptIndexUnusable(
                f"the transcript index for generation {generation_id} maps {stem!r} to "
                f"{digest!r}, which is not a sha256 blob digest. A value that is not a "
                "digest cannot name a blob, and steering a fetch past it could serve the "
                "wrong conversation, so the index is refused rather than partially read."
            )
    return parsed
