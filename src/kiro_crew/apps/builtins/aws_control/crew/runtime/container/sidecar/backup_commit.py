"""The authority phase: the pair, the transcript index and the pointer that commits them.

A cycle that uploaded every transcript publishes the two authority files and the
``stem -> blob`` transcript index into a writer-unique generation, then commits that
generation with ONE pointer write, a compare-and-swap fenced by this task's incarnation. The
incarnation itself is minted here at startup, from the committed pointer rather than a clock.

Part of :mod:`container.sidecar.backup`, which re-exports every name here and is the import
path callers use. :func:`run_cycle` stays in that facade: it decides whether this phase runs
and calls its steps in order, and this module holds the steps. :func:`_commit_authority` reads
``_commit_one`` through the facade at call time, because the data phase that shares it stays
there. New publication or fence rules go here.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from typing import Callable

from ..common import Settings, keys
from ..common.config import BACKUP_ATTEMPT_COST_SECS

# One spelling of the transcript filename, rather than a second copy of the prefix rule
# here: the index names a conversation by its slot key and the file carries a
# ``dashboard_`` prefix plus a character substitution, so a local re-derivation would be a
# second definition of the same mapping and would drift from the one that names the file.
from ..front.transcript import transcript_stem
from . import generation
from .backup_durable import Fingerprint, Snapshot, _slots_named_by, _time_for_one_more
from .backup_record import BackupSet, CycleResult
from .store import ObjectStore, PreconditionFailed, StoreUnusable

log = logging.getLogger("smc.sidecar.backup")


#: The task-incarnation minter, re-exported so the sidecar entrypoint reaches it through the
#: one module it already drives (``backup_mod``). It lives in ``keys`` with the other
#: id minting; the fence that consumes it lives here in :func:`_commit_generation`.
new_incarnation = keys.new_incarnation


def startup_incarnation(settings: Settings, store: ObjectStore) -> str:
    """This task's incarnation, derived from the committed pointer at startup.

    The incarnation orders tasks for the commit fence, and that ordering must NOT come from a
    host wall clock -- cross-host skew would let a replacement on a slower clock mint an older
    token and be refused as superseded forever, and one future-dated commit would poison the
    pointer for every later task. So the ordering is derived from SHARED STORAGE: read the
    committed pointer once at startup and mint the NEXT incarnation after the one it carries
    (:func:`keys.next_incarnation`), so this task's commits sort strictly after the committed
    generation's, independent of any clock. A missing or legacy pointer (``None``) carries no
    incarnation to follow, so this task starts at the base counter -- the same tolerance the
    restore applies, and the pointer's own compare-and-swap still settles a genuine race.

    An UNUSABLE pointer is NOT treated that way, and is NOT swallowed into a base token. A
    base token minted from an unreadable pointer is strictly LESS than whatever a predecessor
    actually committed, so :func:`_commit_generation`'s fence would then refuse every commit
    this task ever attempts -- for the task's whole life, with no in-process recovery -- while
    its blobs upload unreferenced and the replacement boots the predecessor's generation
    holding none of the turns this task served. A single transient GET (a throttle, a 503, a
    reset) is enough to reach here, because the sidecar's own read is ``BACKUP_MAX_ATTEMPTS``
    = 1 and a throttle is not a permanent code. So :class:`generation.PointerUnusable`
    propagates: the writer refuses to START on it -- the same posture ``restore_authority``
    already takes and the same posture this process takes for :class:`StoreUnusable` -- so the
    fault is reported loudly at boot rather than minting a token that silently fences the task
    forever. The read is retried once at the base attempt count; a transient answer that
    clears on the next task boot is exactly what the restart recovers.

    When the supervisor already CLAIMED ownership before restoration (:func:`generation.
    claim_ownership`), it exports the claimed token in :data:`generation.ENV_CLAIMED_INCARNATION`
    and this ADOPTS it rather than reading the pointer and bumping again. Re-deriving here would
    bump a SECOND time, leaving the committed pointer (which the supervisor set to the claimed
    token) naming a token no live task holds, so the writer's own first commit would be fenced
    against its own claim. Adopting the exported token is what makes the pre-restore claim and
    the writer one ownership rather than two.
    """
    claimed = os.environ.get(generation.ENV_CLAIMED_INCARNATION) or ""
    if claimed:
        return claimed
    pointer = generation.read_pointer(settings, store)
    previous = "" if pointer is None else pointer.incarnation
    return keys.next_incarnation(previous)


def _record_is_due(plan: BackupSet) -> bool:
    """Whether this cycle can commit a generation at all.

    True only when the plan holds EVERY authority file. A cycle that found one of them
    missing locally has no whole pair to publish, and a generation containing one file
    would be a committed generation the restore boots from while the backend flushes its
    own empty view of the other. Such a cycle leaves the pointer alone, so the bucket
    keeps saying that the last committed generation is the one before it.
    """
    published = {key.rsplit("/", 1)[-1] for key, _snapshot in plan.authority}
    return published == set(keys.AUTHORITY_NAMES)


def _evict_superseded_generation(
    *,
    settings: Settings,
    state: dict[str, Fingerprint],
    superseded: generation.Pointer | None,
    committed_now: str,
    incarnation: str,
) -> None:
    """Drop the just-superseded generation's control-plane keys from the lifetime *state*.

    Each committing cycle mints a FRESH generation id and records its two authority keys and
    its transcript-index key in *state* so an unchanged pair/index is not re-PUT. Those keys
    are per-generation, so without this the one ``DurableState`` the sidecar holds for its
    whole life gains three permanent entries every cycle (~2880/day at a 60s interval) --
    the superseded generation's entries, which ``_pair_unchanged`` only ever reads for the
    CURRENTLY committed generation, so they are dead the moment the pointer moves off them.
    The data objects are NOT evicted here, and are bounded elsewhere: an archive segment's
    path key is held under the archive FIFO cap (:func:`_record_durable`), and a live
    transcript's content-addressed blob key is bounded to ONE per stem by
    :func:`_evict_prior_live_blob`, which drops a stem's superseded key as its new one is
    recorded. This function only drops the superseded GENERATION's three control-plane keys.

    Guarded two ways. It runs only when THIS cycle's pointer actually landed -- the pointer
    key in *state* now carries this generation's body fingerprint -- so a rejected or
    withheld commit (where the old generation is still the committed one) evicts nothing. And
    it never touches the generation just committed, only the one it replaced.
    """
    if superseded is None or superseded.generation == committed_now:
        return
    pointer_key = keys.authority_pointer_key(settings)
    committed_body = generation.pointer_body(committed_now, incarnation)
    committed_fp = Fingerprint(
        inode=0,
        size=len(committed_body),
        mtime_ns=int.from_bytes(hashlib.sha256(committed_body).digest()[:8], "big"),
    )
    if state.get(pointer_key) != committed_fp:
        # This cycle's pointer did not land, so the superseded generation is still the
        # committed one and its entries must stay for the next cycle's _pair_unchanged.
        return
    for name in keys.AUTHORITY_NAMES:
        state.pop(keys.authority_generation_key(settings, superseded.generation, name), None)
    state.pop(keys.transcript_index_key(settings, superseded.generation), None)


def _merge_transcript_index(
    plan: BackupSet, committed_index: dict[str, str], new_index: dict[str, str]
) -> dict[str, str]:
    """The generation's transcript index: committed mappings the pair still names, then local.

    *new_index* holds only the stems THIS cycle enumerated on local disk. On a replacement
    task that is a subset of the conversations the committed authority pair names -- the
    front fetches a transcript lazily, on the turn that continues it, so a conversation this
    task has not served yet has its bytes only in the bucket. Publishing *new_index* alone
    would drop every such stem from the committed generation and the front, reading the new
    index, would miss it and serve an empty history.

    So the committed index is carried forward for every stem the CAPTURED authority pair
    still names (its blob is immutable and still in the bucket), and *new_index* overlays
    it so a conversation that gained turns takes its fresh blob. A stem the pair does not
    name is NOT carried: that conversation was deleted, and forgetting its blob is right.
    When the captured pair cannot be read, nothing is carried -- the local view is the only
    one that can be trusted, which is the conservative direction.
    """
    named = _slots_named_by(plan.authority)
    merged: dict[str, str] = {}
    if named is not None:
        for slot in named:
            stem = transcript_stem(slot)
            if stem and stem in committed_index:
                merged[stem] = committed_index[stem]
    merged.update(new_index)
    return merged


def _pair_unchanged(
    plan: BackupSet,
    *,
    settings: Settings,
    state: dict[str, Fingerprint],
    committed: generation.Pointer | None,
    committed_index: dict[str, str],
    new_index: dict[str, str],
) -> bool:
    """Whether the committed generation already holds exactly this cycle's pair AND index.

    Without this the protocol republishes on every interval: each cycle mints a fresh
    generation id, so the pair's keys differ from the ones last uploaded and every cycle
    looks like a change. The comparison is therefore made against the COMMITTED generation's
    keys, which is where the last published bytes actually went, via the fingerprints this
    process recorded for them.

    The TRANSCRIPT INDEX is compared too, and it is why a cycle can be a real change even
    when the pair's own bytes did not move: a conversation that gained turns keeps the same
    slot, so ``session_map.json`` / ``open_slots.json`` are byte-identical, but the
    transcript's blob digest changed -- so the committed index names a DIFFERENT blob than
    its current one. Republishing is then required, or the committed generation would point
    the front at the stale blob. Equal index AND equal pair is the only true "nothing to
    publish".

    False whenever anything is unknown -- no pointer, a name the commitment does not
    cover, a fingerprint this process never recorded -- because republishing a pair that
    was already there costs one cycle's bandwidth, while skipping one that was not costs
    the index.
    """
    if committed is None or not _record_is_due(plan):
        return False
    if new_index != committed_index:
        return False
    for key, snapshot in plan.authority:
        name = key.rsplit("/", 1)[-1]
        if name not in committed.authority:
            return False
        if (
            state.get(keys.authority_generation_key(settings, committed.generation, name))
            != snapshot.fingerprint
        ):
            return False
    return True


def _referenced_by_captured_index(
    plan: BackupSet, result: CycleResult, *, settings: Settings
) -> None:
    """Record which vanished-and-undurable entries the captured pair would send a reader to.

    Records directly into *result* through :meth:`CycleResult.record_gone_referenced`, so the
    verdict is a capped sample plus an exact ``gone_referenced_count`` -- the count is what the
    withhold decision reads, and it stays exact where the sample is truncated.

    An unreadable index answers with EVERY candidate rather than none: the pair is withheld,
    the cycle is incomplete, and the next cycle republishes -- where guessing "names nothing"
    would commit an index this cycle could not read against bytes it knows are absent. Because
    ``gone_undurable`` is itself a capped sample, the count of that candidate set (exact) is
    what drives the unreadable-index verdict, not the retained names: every undurable candidate
    is referenced, so the exact count carries the decision even past the sample.
    """
    if result.gone_undurable_count == 0:
        return
    named = _slots_named_by(plan.authority)
    if named is None:
        # Unreadable index: every undurable candidate is referenced. Record the sample we have
        # for the log, then set the count to the exact candidate total so the withhold decision
        # is not fooled by a truncated sample.
        for name, _key in result.gone_undurable:
            result.record_gone_referenced(name)
        result.gone_referenced_count = result.gone_undurable_count
        return
    # The two sides live in DIFFERENT namespaces, and comparing them directly is how this
    # check silently matched nothing: the index names a conversation by its SLOT KEY
    # (``cust-8831``) while its transcript is ``dashboard_cust-8831.jsonl``. So the slot keys
    # are mapped FORWARD through the front's own ``transcript_stem`` -- the function that
    # decides the real filename -- rather than the prefix being stripped off the filename
    # here. Stripping by hand would also miss the character substitution that function does,
    # so a key holding an unsafe character would map to a name this comparison never made.
    named_files = {
        f"{stem}{keys.TRANSCRIPT_SUFFIX}"
        for stem in (transcript_stem(slot) for slot in named)
        if stem
    }
    for name, _key in result.gone_undurable:
        if name in named_files:
            result.record_gone_referenced(name)


def _commit_transcript_index(
    index: dict[str, str],
    *,
    settings: Settings,
    store: ObjectStore,
    state: dict[str, Fingerprint],
    result: CycleResult,
    generation_id: str,
    deadline: float | None = None,
    cancel: Callable[[], bool] | None = None,
) -> None:
    """Publish this generation's ``stem -> blob-digest`` index, before the pointer commits it.

    Written into ``gen/<id>/transcript_index.json``, under the SAME writer-unique id the
    pair goes under, so it is part of one immutable generation the pointer either names
    whole or does not name at all. The front reads it to turn a slot stem into the blob it
    fetches, so it must be in the bucket before the pointer that commits the generation --
    which is why it is published here, in the authority phase, ahead of
    :func:`_commit_generation`.

    Recorded in *result* like any other object. A refusal here keeps the pointer where it
    is -- :func:`_commit_generation` returns on any refusal -- so a cut index leaves the
    previous generation committed and whole rather than naming a generation whose index
    never landed. Written ``If-None-Match: *``: the id is fresh this cycle, so nothing holds
    the key, and a defeated precondition would mean a collision that cannot occur.

    An EMPTY index is still published, so the committed generation always carries one and a
    reader never has to tell "no transcripts yet" apart from "index lost": absent means
    generation 0, present-and-empty means a committed generation with no live transcripts.
    """
    key = keys.transcript_index_key(settings, generation_id)
    body = json.dumps(index, sort_keys=True).encode("utf-8")
    fingerprint = Fingerprint(
        inode=0,
        size=len(body),
        mtime_ns=int.from_bytes(hashlib.sha256(body).digest()[:8], "big"),
    )
    if state.get(key) == fingerprint:
        result.record_unchanged(key)
        return
    if deadline is not None and not _time_for_one_more(deadline, BACKUP_ATTEMPT_COST_SECS):
        log.error(
            "backup: the drain window cannot fit the transcript index, so generation %s is "
            "NOT committed and this cycle is refused. The generation the pointer still names "
            "is whole, but it is the older one and no later cycle follows this.",
            generation_id,
        )
        result.record_refused(key, "the drain window could not fit the transcript index")
        return
    try:
        # The index is NOT a few hundred bytes: it can hold up to 50,000 stems or
        # MAX_OBJECT_BYTES // 2 of JSON (the caps ``backup.py`` defines), so on a slow
        # link its upload can outlast the drain window and be SIGKILLed mid-write, losing
        # the newest history with nothing to recover it. So it is budgeted and cancellable
        # exactly like the pair-index PUT in the same phase: one attempt's worth of time
        # when a deadline is set, and the cycle's yield check so it stops at the boundary
        # rather than running past it.
        store.put(
            key,
            io.BytesIO(body),
            len(body),
            if_none_match="*",
            budget=None if deadline is None else BACKUP_ATTEMPT_COST_SECS,
            cancel=cancel,
        )
    except StoreUnusable:
        raise
    except PreconditionFailed:
        # The id is minted fresh this cycle, so a key already there is not a legitimate
        # state -- it would mean two cycles minted the same id, which the random suffix
        # makes vanishingly unlikely. Treated as a refusal (not success) because, unlike a
        # content-addressed blob, this key is NOT its content's digest: an object already
        # there might be a DIFFERENT index, so adopting it would commit a mismatched map.
        state.pop(key, None)
        log.error(
            "backup: the transcript index key for generation %s already exists, which should "
            "be impossible for a freshly minted id; refusing rather than committing a "
            "possibly mismatched index.",
            generation_id,
        )
        result.record_refused(key, "the transcript index key already existed; commit refused")
        return
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        state.pop(key, None)
        log.error("backup: the transcript index could not be published (%s)", exc)
        result.record_refused(key, f"the transcript index could not be published ({exc})")
        return
    state[key] = fingerprint
    result.record_uploaded(key)


def _commit_generation(
    plan: BackupSet,
    *,
    settings: Settings,
    store: ObjectStore,
    state: dict[str, Fingerprint],
    result: CycleResult,
    generation_id: str,
    committed: "generation.Pointer | None" = None,
    incarnation: str = "",
    deadline: float | None = None,
) -> None:
    """Publish the pointer naming *generation_id* as the committed generation. The LAST step.

    Last is the property the restore depends on, not a preference. A cycle interrupted
    anywhere in the authority phase therefore leaves the pointer naming the PREVIOUS
    generation, whose objects are all still there and were never rewritten -- so the
    replacement boots from a coherent older pair instead of a torn newer one. Committing
    first would invert that: the pointer would name a generation the crash never finished
    writing, and the restore would refuse a bucket whose previous generation was fine.

    The commit is fenced two ways, and both are required. The pointer PUT is a COMPARE-AND-
    SWAP on the ETag this cycle read, which rejects a commit whose read went stale. On top of
    that the pointer carries this task's INCARNATION token, and the commit refuses to publish
    when the committed pointer's incarnation is NEWER than this one's. The CAS alone cannot
    catch the overlap the two fences together close: a predecessor task whose cycle BEGAN
    AFTER its replacement committed reads the replacement's pointer, so it holds a FRESH ETag
    and its ``If-Match`` would succeed -- rolling the committed generation back to the
    predecessor's older, local-only state. The incarnation is minted once per task at process
    start and is strictly greater for a later-started task, so a predecessor whose incarnation
    is older than what is committed is refused: a superseded task cannot publish.

    Skipped, not failed, when the pair was not whole or anything in this cycle was
    refused: a generation is committed only when it contains one.

    Sent once per published generation through the same *state* map the objects use, so an
    idle cycle that re-published nothing does not re-PUT the pointer. The fingerprint is
    derived from the pointer's own bytes, because it is the one object with no file behind
    it, and it is written only after the PUT returns, so a failed commit is retried.

    A pointer that cannot be written is recorded in ``withheld`` on an interval cycle, and
    that cycle stays complete: the previous generation is still committed and still whole,
    so nothing is lost -- this cycle's newer pair simply is not adopted yet, and the next
    cycle commits it.

    On the FINAL cycle the same failure is a refusal instead, because the recovery above
    is a later cycle and the final cycle has none. Left withheld it would exit zero and
    report a lossless stop while the replacement adopts the generation the pointer still
    names -- the older index, without the conversations this cycle wrote. ``deadline`` is
    what tells the two apart: only the final cycle sets one.
    """
    if not _record_is_due(plan) or result.refused or result.withheld:
        return
    key = keys.authority_pointer_key(settings)
    # The INCARNATION FENCE. A predecessor whose cycle began after its replacement committed
    # holds a fresh pointer ETag, so the compare-and-swap below would admit its stale commit;
    # refusing when the committed incarnation SUPERSEDES this task's is what stops a superseded
    # task from rolling the committed generation back to its older local state.
    # :func:`keys.incarnation_supersedes` is true when the committed counter is strictly
    # greater OR -- the case a bare counter comparison missed -- the counters are equal but the
    # tokens differ: two tasks that both started while the pointer carried counter N both mint
    # N+1 (the read-plus-one is not atomic), so a task that finds a DIFFERENT N+1 already
    # committed cannot prove it is the later one and steps aside rather than overwrite it. A
    # task re-committing its OWN token across cycles sees the identical token and is NOT
    # superseded, so its ordinary cycles are never refused. Ordered BEFORE the
    # fingerprint/unchanged check so a superseded task refuses rather than reporting a no-op
    # success. A committed pointer written before this field carries counter 0, which never
    # supersedes, so a bucket from an earlier writer stays committable.
    if committed is not None and keys.incarnation_supersedes(committed.incarnation, incarnation):
        if committed.is_claim:
            # The newer incarnation that fences this task is a PROVISIONAL CLAIM, not a real
            # commit: a replacement bumped the pointer BEFORE restoration and has not yet
            # committed anything (it may never -- its backend can fail readiness and abort,
            # releasing the claim). Treating that as a permanent supersession is the outage
            # Opus named: a healthy predecessor serving turns at its 60s interval would read
            # the claim, raise SidecarSuperseded and be torn down LONG before the failed
            # replacement's abort path (gated on a 180s readiness timeout) releases the claim
            # -- killing the only live task and losing its turns since the last commit. So a
            # claim-fenced commit is refused as RETRYABLE (``superseded`` stays unset): the
            # predecessor keeps serving and re-attempts next interval, and it steps aside only
            # once a replacement makes a REAL, unmarked commit carrying a newer incarnation --
            # which is proof the replacement is live, not merely claiming.
            log.warning(
                "backup: the committed pointer carries a newer incarnation that is a "
                "PROVISIONAL CLAIM (a replacement that bumped the pointer before restoration "
                "and has not committed). This task's commit of generation %s is refused this "
                "cycle but RETRIED next interval rather than ending the process -- a claim "
                "that never becomes a real commit (its claimant aborts) must not fence a live "
                "predecessor, so the predecessor keeps serving until a real commit supersedes "
                "it.",
                generation_id,
            )
            result.record_refused(
                key, "a newer incarnation is claimed but not yet committed; retried next cycle"
            )
            return
        log.error(
            "backup: the committed generation carries a newer task incarnation than this "
            "task's, so this task has been superseded by a replacement and its commit of "
            "generation %s is refused rather than rolling the committed generation back to "
            "this task's older state.",
            generation_id,
        )
        result.record_refused(
            key, "a newer task incarnation is committed; superseded commit refused"
        )
        # Distinct from an ordinary refusal: this task is SUPERSEDED and can never commit
        # again, so run_cycle raises SidecarSuperseded (not a plain BackupIncomplete) and the
        # process ends rather than retrying a commit the fence will refuse every future cycle.
        result.superseded = True
        return
    body = generation.pointer_body(generation_id, incarnation)
    # The fingerprint distinguishes one committed generation from the next so a pointer
    # naming a NEW generation is re-PUT rather than skipped as unchanged. The pointer is the
    # one object with no file behind it, so the fingerprint is derived from its own bytes:
    # ``mtime_ns`` carries a stable digest of the whole body -- the generation id -- which
    # differs whenever it does, and every body is the same length so ``size`` alone could
    # not tell two commitments apart.
    fingerprint = Fingerprint(
        inode=0,
        size=len(body),
        mtime_ns=int.from_bytes(hashlib.sha256(body).digest()[:8], "big"),
    )
    if state.get(key) == fingerprint:
        result.record_unchanged(key)
        return
    final = deadline is not None
    if deadline is not None and not _time_for_one_more(deadline, BACKUP_ATTEMPT_COST_SECS):
        log.error(
            "backup: the drain window cannot fit the generation pointer, so generation %s "
            "is NOT committed and this cycle is refused. The generation the pointer still "
            "names is whole, but it is the older one and no later cycle follows this.",
            generation_id,
        )
        result.record_refused(key, "the drain window could not fit the generation pointer")
        return
    # The commit is a COMPARE-AND-SWAP against the pointer's state AS THIS CYCLE READ IT, so a
    # second sidecar writing this prefix in the task-replacement window cannot clobber the
    # generation this one publishes. The validator is the ETag captured by ``read_pointer`` at
    # the START of the cycle -- NOT a fresh HEAD here -- for two reasons the review named: a
    # HEAD at commit time is unbudgeted work the final cycle's drain window could be SIGKILLed
    # inside, and reading the validator at cycle start widens the CAS window to cover the
    # authority writes this commit blesses, not just the pointer PUT. An existing pointer commits
    # ``If-Match`` its captured ETag; no pointer yet commits ``If-None-Match: *``. A writer
    # that advanced the pointer since this read changes the ETag, so this PUT is rejected
    # rather than overwriting -- the stale commit steps aside.
    #
    # FAIL CLOSED on a missing validator: a pointer that was present but whose ETag the store
    # could not supply has NO precondition available, and committing unconditionally would
    # defeat the guard against a concurrent writer. So the commit is refused rather than run
    # blind -- the generation the pointer still names is whole, and a later cycle re-reads a
    # validator and commits.
    if_match: str | None = None
    if_none_match: str | None = None
    if committed is None:
        if_none_match = "*"
    elif committed.etag is not None:
        if_match = committed.etag
    else:
        log.error(
            "backup: the committed generation pointer carried no ETag validator, so "
            "generation %s cannot be committed under a compare-and-swap and this cycle is "
            "refused rather than overwriting a concurrent writer's generation blind.",
            generation_id,
        )
        result.record_refused(key, "the generation pointer had no CAS validator; commit refused")
        return
    try:
        # Neither bounded nor cancellable, and both for the same reason: the body is a
        # few hundred bytes the transport reads in ONE call, so a predicate asked between
        # chunks has no second chunk to refuse and a deadline enforced by the body is
        # never re-consulted. What bounds this request is the client's own
        # ``connect_timeout`` and ``read_timeout`` at one attempt, which is a real bound
        # precisely because there is no transmission here to outgrow them.
        store.put(key, io.BytesIO(body), len(body), if_match=if_match, if_none_match=if_none_match)
    except StoreUnusable:
        raise
    except PreconditionFailed:
        # Another writer committed a newer generation since this cycle read the pointer, so
        # this commit is stale and is rejected rather than overwriting it. The cached belief
        # is dropped so a later cycle re-reads the now-current pointer and rebuilds on it, and
        # this cycle is recorded incomplete: it did not publish the pair it opened, and the
        # pointer names another writer's generation, not this one's.
        state.pop(key, None)
        log.warning(
            "backup: the generation pointer moved under this cycle before it could commit "
            "generation %s, so a concurrent writer's generation stands and this commit is "
            "rejected rather than clobbering it.",
            generation_id,
        )
        result.record_refused(
            key,
            "a concurrent writer committed a newer generation; this stale commit was rejected",
        )
        return
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        # The PUT's response was lost, so whether the pointer landed is UNKNOWN -- it may
        # hold the new generation, the old one, or nothing coherent. Any cached belief that
        # this key already holds *fingerprint* is therefore untrustworthy: left in place
        # it would make the early ``state.get(key) == fingerprint`` return above skip the
        # re-PUT on every later cycle and report the unpublished commit as an unchanged,
        # complete stop -- the generation frozen behind a belief a failed write installed.
        # Dropping it forces the next cycle to attempt the commit again.
        state.pop(key, None)
        if final:
            log.error(
                "backup: the generation pointer could not be published (%s), so generation "
                "%s is not committed and this cycle is refused. No later cycle follows this "
                "one, so the replacement would silently adopt the older generation.",
                exc,
                generation_id,
            )
            result.record_refused(key, f"the generation pointer could not be published ({exc})")
            return
        log.error(
            "backup: the generation pointer could not be published (%s), so generation %s "
            "is not committed. The generation it still names is whole, so nothing is lost; "
            "this cycle's pair is simply not adopted yet.",
            exc,
            generation_id,
        )
        result.withheld.append(key)
        return
    state[key] = fingerprint
    result.record_uploaded(key)


def _commit_authority(
    items: list[tuple[str, Snapshot]],
    *,
    settings: Settings,
    store: ObjectStore,
    state: dict[str, Fingerprint],
    result: CycleResult,
    deadline: float | None = None,
    yield_when: Callable[[], bool] | None = None,
) -> None:
    """Upload the already-open authority snapshots, recording each verdict in *result*.

    Separate from the transcript phase because these descriptors are opened by the plan,
    before the enumeration, and are closed by the caller whether or not this runs. Bounded
    by the same deadline: an index PUT that cannot finish inside the window would be killed
    mid-request, and publishing one of the pair without the other is the torn index the
    phase order exists to prevent.

    *yield_when* bounds this phase on an interval cycle the way the deadline bounds it on
    the final one. These uploads are as capable of outliving a stop as a transcript's, and
    a refusal here keeps the pointer where it is -- :func:`_commit_generation` returns on
    any refusal -- so a cut index leaves the previous generation committed and whole rather
    than half-replaced.
    """
    # ``_commit_one`` is the data phase's too and stays in the facade, which imports this
    # module, so it is read there at call time rather than bound here.
    from . import backup

    for index, (key, snapshot) in enumerate(items):
        if deadline is not None and not _time_for_one_more(deadline, BACKUP_ATTEMPT_COST_SECS):
            unreached = [k.rsplit("/", 1)[-1] for k, _s in items[index:]]
            log.error(
                "backup: the drain window cannot fit another upload, so %d authority "
                "file(s) are NOT published: %s. The pair in the bucket stays at the last "
                "complete cycle rather than being left half new.",
                len(unreached),
                ", ".join(unreached),
            )
            result.withheld.extend(k for k, _s in items[index:])
            for name in unreached:
                result.record_refused(
                    name, "not attempted: the drain window could not fit another upload"
                )
            return
        backup._commit_one(
            key,
            snapshot,
            name=key.rsplit("/", 1)[-1],
            settings=settings,
            store=store,
            state=state,
            result=result,
            # This phase's gate reserves one attempt per index object, so that is what
            # its PUT is bounded by -- the reservation and the bound are one number here
            # too, and an index PUT cannot eat the window the rest of the pair needs.
            budget=None if deadline is None else BACKUP_ATTEMPT_COST_SECS,
            cancel=yield_when,
        )


def _reserve_for_authority(deadline: float | None, count: int) -> float | None:
    """Pull *deadline* in by what the index needs, so the data phase leaves it room.

    Without this the transcripts can spend the whole window and the authority PUTs start
    with nothing left: they run past it and are killed mid-request, which publishes one
    file and not the other. That torn pair is the state the two-phase order exists to
    avoid, so the reservation is part of the ordering rather than a tuning choice.

    One attempt per file, not a whole retry budget per file. The budgets are what the drain
    window has to cover, and reserving the worst case for two small JSON files and a pointer
    would consume most of ``SIDECAR_DRAIN_SECS`` before a single transcript moved. The index phase's
    own deadline check is what covers a retry eating into the rest.
    """
    if deadline is None:
        return None
    return deadline - count * BACKUP_ATTEMPT_COST_SECS
