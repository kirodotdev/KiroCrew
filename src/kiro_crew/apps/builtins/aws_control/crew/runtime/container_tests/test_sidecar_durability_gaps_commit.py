"""What one backup cycle commits, and what refuses a commit it must not make.

A cycle uploads its authority pair under a writer-unique generation and commits it by moving
the generation pointer under a compare-and-swap. Only a whole pair commits; a pointer that
cannot be published or read refuses the final cycle and makes an interval cycle wait; a
concurrent writer's commit rejects a stale one; and the task incarnation the pointer carries
fences a superseded task without refusing a replacement on a slower clock. The blob keys and
the state map a commit leaves behind are pinned here too. Item 5 of the list in
``test_sidecar_durability_gaps.py`` is the ordering half of the same protocol.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import pytest
from container.common import keys
from container.sidecar import backup as backup_mod
from container.sidecar import generation as generation_mod
from container.sidecar.store import ObjectAbsent

from .sidecar_durability_gaps_helpers import (
    _GEN,
    STEM,
    _authority_keys_in,
    _blob_key,
    _Bucket,
    _Recorder,
    _settings,
    _transcript,
)


def test_a_cycle_that_holds_one_authority_file_commits_no_generation(tmp_path):
    """A generation is committed holding a WHOLE pair or it is not committed at all.

    A generation committed with one file would assert that a complete publication is one
    file, and every later restore would boot from it and let the backend flush its own empty
    view of the other -- reintroducing, through the pointer, the loss the protocol is here to
    prevent. With no pointer committed the file has nowhere a reader looks either, so it is
    withheld rather than published and the cycle says so; see
    :func:`test_one_authority_file_present_is_refused_rather_than_half_published`.
    """
    settings = _settings(tmp_path)
    (settings.config_dir / keys.AUTHORITY_NAMES[0]).unlink()
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert keys.authority_pointer_key(settings) not in store.puts
    assert keys.AUTHORITY_NAMES[0] in caught.value.result.gone


def test_a_pointer_that_cannot_be_published_refuses_the_final_cycle(tmp_path):
    """On the final cycle an unpublished pointer is a refusal, because nothing follows it.

    The withheld path's recovery is "the next cycle commits it", and the final cycle has
    no next cycle. Left withheld, the pair is on disk in a slot no pointer names, the
    cycle reports complete, the sidecar exits zero and the supervisor reports a lossless
    stop -- while the replacement boots from the generation the pointer still names, which
    is the older index without the conversations this cycle just uploaded. Silent, and the
    quietest possible shape of the loss this whole protocol exists to prevent.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerFails(_Recorder):
        def put(
            self,
            key: str,
            body,
            size: int,
            *,
            budget: float | None = None,
            cancel: Callable[[], bool] | None = None,
            if_match: str | None = None,
            if_none_match: str | None = None,
        ) -> None:
            if key == pointer:
                raise RuntimeError("AccessDenied")
            super().put(key, body, size)

    store = _PointerFails()

    with pytest.raises(backup_mod.BackupIncomplete):
        backup_mod.run_cycle(settings, store, state={}, deadline=time.monotonic() + 3600)


def test_a_pointer_that_cannot_be_published_only_waits_on_an_interval_cycle(tmp_path):
    """The mirror: an interval cycle withholds, because its next cycle really does follow.

    Refusing here would turn one transient PUT failure into a non-zero exit on a sidecar
    that is going to run again in seconds, and a supervisor that ends the task on it.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerFails(_Recorder):
        def put(
            self,
            key: str,
            body,
            size: int,
            *,
            budget: float | None = None,
            cancel: Callable[[], bool] | None = None,
            if_match: str | None = None,
            if_none_match: str | None = None,
        ) -> None:
            if key == pointer:
                raise RuntimeError("SlowDown")
            super().put(key, body, size)

    store = _PointerFails()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.complete, "an interval cycle has a next cycle, so it waits rather than fails"
    assert pointer in result.withheld
    assert result.refused == []


def test_a_lost_pointer_put_drops_the_cached_belief_so_a_later_cycle_re_commits(tmp_path):
    """An unconfirmed pointer PUT must not leave a belief that pins the generation.

    The commit skips its PUT when ``state`` already says this key holds this slot's
    fingerprint. So a PUT whose response is lost -- the write may have landed, may not, the
    slot is unknown -- must not leave that fingerprint cached: left in place, every later
    cycle sees ``state.get(key) == fingerprint``, skips the re-PUT, records the key as
    unchanged, and reports a COMPLETE stop while the generation the pointer names is frozen
    behind a belief a failed write installed. The except paths therefore drop the key.

    The proof seeds the pointer key with a stale belief, fails the pointer PUT on an
    interval cycle, and asserts the belief is gone afterwards -- so the next cycle re-attempts
    the commit rather than trusting the cache. It fails if the ``state.pop`` is removed.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerFails(_Recorder):
        def put(
            self,
            key: str,
            body,
            size: int,
            *,
            budget: float | None = None,
            cancel: Callable[[], bool] | None = None,
            if_match: str | None = None,
            if_none_match: str | None = None,
        ) -> None:
            if key == pointer:
                raise RuntimeError("RequestTimeout")  # the response is lost, not a refusal
            super().put(key, body, size)

    # A stale belief from an earlier committed cycle: any fingerprint under the key is
    # enough, because the fix drops whatever is there rather than matching a value.
    state = {pointer: backup_mod.Fingerprint(inode=0, size=1, mtime_ns=0)}
    result = backup_mod.run_cycle(settings, _PointerFails(), state=state)

    assert pointer in result.withheld
    assert pointer not in state, (
        "an unconfirmed pointer PUT must invalidate the cached belief, or a later cycle "
        "skips the re-commit and reports the freeze as a complete stop"
    )


def test_a_concurrent_writer_committing_first_rejects_this_cycles_stale_commit(tmp_path):
    """Two sidecars on one prefix must not clobber each other's committed generation.

    In the task-replacement window the draining old sidecar and the starting new one can both
    write this prefix. Both read the same committed pointer, both pick the other slot, both
    publish a pair into it and both commit the pointer -- and without a guard the second
    overwrites the first's generation. The commit is therefore a compare-and-swap: it reads
    the pointer's ETag and PUTs ``If-Match`` it, so a writer that advanced the pointer since
    this cycle read it defeats this PUT with a 412, and the stale commit is rejected rather
    than winning the race.

    The proof stages a concurrent writer inside the pointer read: `get_with_etag` returns the
    stored ETag but then advances it (as if another sidecar committed) so this cycle's
    conditional PUT carries a stale ``If-Match`` that fails to match. The commit must be
    refused, not landed.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _ConcurrentWriterCommitsFirst(_Recorder):
        def __init__(self) -> None:
            super().__init__()
            # A pointer already committed by an earlier generation, so this cycle reads it and
            # its commit takes the If-Match branch rather than the If-None-Match create. The
            # body is a valid whole-pair pointer so the read accepts it rather than refusing it
            # as unusable.
            self._committed = generation_mod.pointer_body(keys.new_generation_id())
            self.objects[pointer] = self._committed
            self.etags[pointer] = '"etag-original"'
            # A committed generation always carries its transcript index (empty here), written
            # before the pointer; the cycle reads it after the pointer, so the fake serves it
            # or the read refuses the cycle as a deleted-while-referenced index before the CAS
            # path this test exercises is ever reached.
            committed_gen = json.loads(self._committed.decode())["generation"]
            self.objects[keys.transcript_index_key(settings, committed_gen)] = b"{}"

        def get_with_etag(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> tuple[bytes, str | None]:
            if key not in self.objects:
                raise ObjectAbsent(key)
            raw = self.objects[key]
            etag = self.etags.get(key)
            if key == pointer:
                # A concurrent writer commits AFTER this read: the stored ETag moves on, so
                # the validator this read returned fails to match at commit time.
                self.etags[pointer] = '"etag-advanced-by-a-concurrent-writer"'
            return raw, etag

    store = _ConcurrentWriterCommitsFirst()
    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    result = caught.value.result
    assert not result.complete, "a stale commit rejected by CAS must make the cycle incomplete"
    assert any(
        key == pointer for key, _why in result.refused
    ), "the rejected stale commit must be recorded as a refusal, not silently dropped"
    # The concurrent writer's generation stands: our commit did not overwrite the pointer.
    assert store.objects[pointer] == store._committed


def test_a_commit_with_no_cas_validator_fails_closed(tmp_path):
    """A present pointer whose ETag the store could not supply must NOT commit unconditionally.

    The CAS validator is the pointer's ETag read at cycle start. If the pointer exists but the
    store returned no ETag for it, there is no precondition to present -- and committing anyway
    would overwrite a concurrent writer's generation blind, defeating the guard. So the commit
    fails CLOSED: it is refused and the pointer is NOT written. It fails if the commit runs
    unconditionally on a missing validator.

    Exercised directly on ``_commit_generation`` so the fail-closed branch is isolated from the
    rest of the cycle: a committed pointer carrying ``etag=None`` is the missing-validator case.
    """
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    pointer = keys.authority_pointer_key(settings)
    committed = generation_mod.Pointer(
        generation=keys.new_generation_id(), authority=frozenset(keys.AUTHORITY_NAMES), etag=None
    )
    store = _Recorder()
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
        )
    finally:
        plan.close_authority()

    assert any(
        key == pointer for key, _why in result.refused
    ), "a missing CAS validator must be refused, not committed blind"
    assert not result.complete
    # The pointer was never written: no unconditional commit happened.
    assert pointer not in store.objects


def test_two_writers_publish_into_distinct_generations_and_neither_clobbers_the_other(tmp_path):
    """The finding's core: two concurrent writers must not commit a mixed authority pair.

    Under a shared two-slot scheme both writers in the task-replacement window target the same
    slot and interleave their PUTs into it, so the committed pair could be a cross-writer tear.
    Writer-unique immutable generation keys remove the shared object: each cycle mints its own
    ``gen/<id>/`` and writes only there, so a second writer's pair lands under a DIFFERENT id
    and cannot overwrite the first's. This proves two independent cycles (standing in for two
    writers) address disjoint generation directories, and neither writes a key the other did.

    It fails if the pair keys stop carrying a per-cycle-unique id -- e.g. a reversion to a
    fixed slot -- because the two cycles' authority keys would then collide.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")

    # Two cycles, each starting from an EMPTY bucket state so each mints a fresh generation
    # rather than skipping as unchanged -- the concurrent-writer window, where neither has
    # seen the other's commit.
    store_a = _Recorder()
    backup_mod.run_cycle(settings, store_a, state={})
    store_b = _Recorder()
    backup_mod.run_cycle(settings, store_b, state={})

    pair_a = _authority_keys_in(settings, store_a.objects)
    pair_b = _authority_keys_in(settings, store_b.objects)
    assert len(pair_a) == len(keys.AUTHORITY_NAMES)
    assert len(pair_b) == len(keys.AUTHORITY_NAMES)
    # Distinct generation directories: the two writers' pairs share no key, so neither
    # overwrote the other's -- the torn-pair hazard the shared slot allowed cannot occur.
    assert pair_a.isdisjoint(pair_b), "two writers wrote the same generation key"
    gen_a = {key.rsplit("/", 2)[-2] for key in pair_a}
    gen_b = {key.rsplit("/", 2)[-2] for key in pair_b}
    assert len(gen_a) == 1 and len(gen_b) == 1, "each cycle's pair is under one generation id"
    assert gen_a.isdisjoint(gen_b), "the two writers minted the same generation id"


def test_a_superseded_task_cannot_commit_over_its_replacement(tmp_path):
    """The incarnation fence: a predecessor the CAS alone would admit is still refused.

    A predecessor task whose cycle began AFTER its replacement committed reads the
    replacement's pointer, so it holds a FRESH ETag and its ``If-Match`` would succeed --
    rolling the committed generation back to the predecessor's older local state. The
    incarnation closes that: the committed pointer carries the replacement's (newer) token,
    and a commit whose own incarnation is older is refused. This stages exactly that -- a
    committed pointer with a NEWER incarnation and a valid ETag -- and drives a commit with an
    OLDER incarnation, asserting it is refused and the pointer is left untouched.

    It fails if the fence is removed: with only the CAS, the fresh ETag passes and the stale
    commit lands.
    """
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    pointer = keys.authority_pointer_key(settings)
    store = _Recorder()
    # The replacement's committed pointer: a NEWER incarnation, with a live ETag the
    # predecessor's cycle-start read would carry.
    newer = "99999999999999999999-ffffffffffffffff"
    committed_body = generation_mod.pointer_body(keys.new_generation_id(), newer)
    store.objects[pointer] = committed_body
    store.etags[pointer] = '"etag-live"'
    committed = generation_mod.Pointer(
        generation=json.loads(committed_body.decode())["generation"],
        authority=frozenset(keys.AUTHORITY_NAMES),
        incarnation=newer,
        etag='"etag-live"',
    )
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
            # This task's incarnation is OLDER than the committed one: it was superseded.
            incarnation="00000000000000000001-0000000000000000",
        )
    finally:
        plan.close_authority()

    assert any(
        key == pointer and "superseded" in why for key, why in result.refused
    ), "a superseded task's commit must be refused with a superseded reason"
    # The pointer still carries the replacement's generation, not this task's.
    assert store.objects[pointer] == committed_body


def test_the_incarnation_is_derived_from_the_committed_pointer_not_the_clock(tmp_path):
    """A task's incarnation follows the committed pointer's counter, independent of any clock.

    The GPT finding: a wall-clock incarnation is unsound across hosts -- a replacement on a
    clock BEHIND its predecessor mints an OLDER token and is refused as superseded forever,
    and a single future-dated commit poisons the pointer. The fix derives the counter from
    SHARED STORAGE: ``startup_incarnation`` reads the committed pointer and mints the next
    counter after it. This proves a replacement reading a committed counter N mints N+1 --
    with no reference to the wall clock -- so it is strictly greater than what it read.

    It fails if the incarnation reverts to a clock read: then the counter would not track the
    committed pointer's value.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    # A committed pointer carrying counter 7.
    committed = "00000000000000000007-abcdefabcdefabcd"
    body = generation_mod.pointer_body(keys.new_generation_id(), committed)
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'

    minted = backup_mod.startup_incarnation(settings, store)

    # The next counter after 7 is 8, regardless of what any clock reads.
    assert keys.incarnation_counter(minted) == 8
    # And it is strictly greater than the committed one, so this task is never self-refused.
    assert keys.incarnation_counter(minted) > keys.incarnation_counter(committed)


def test_a_replacement_on_a_slower_clock_is_not_refused_as_superseded(tmp_path):
    """The exact harm the finding named: a behind-the-clock replacement must still commit.

    Under the old wall-clock scheme a replacement whose host clock lagged its predecessor's
    minted an OLDER token and the fence refused its every commit permanently -- silent loss of
    all its turns. With a store-derived counter the replacement reads the predecessor's
    committed counter and mints the next one, so it commits regardless of clock skew.

    It fails if a replacement that read a committed counter can mint one not greater than it.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    # Predecessor committed counter 5 (whatever its host clock was).
    predecessor = "00000000000000000005-1111111111111111"
    body = generation_mod.pointer_body(keys.new_generation_id(), predecessor)
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'

    # The replacement derives its incarnation from the pointer, not its (lagging) clock.
    replacement = backup_mod.startup_incarnation(settings, store)
    assert keys.incarnation_counter(replacement) > keys.incarnation_counter(predecessor)

    # Driving a real commit with the derived incarnation is NOT fenced out.
    plan = backup_mod.objects_to_back_up(settings)
    committed = generation_mod.Pointer(
        generation=json.loads(body.decode())["generation"],
        authority=frozenset(keys.AUTHORITY_NAMES),
        incarnation=predecessor,
        etag='"etag"',
    )
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
            incarnation=replacement,
        )
    finally:
        plan.close_authority()
    assert not any("superseded" in why for _k, why in result.refused)


def test_an_equal_counter_different_token_commit_is_refused_as_superseded(tmp_path):
    """GPT F1: two tasks sharing a counter must not roll each other back.

    ``next_incarnation`` is a non-atomic read-plus-one, so two tasks that both start while the
    pointer carries counter N both mint N+1 with different random suffixes. If one commits and
    the other then re-reads a FRESH pointer ETag, a strictly-greater-only fence would admit the
    second's stale commit (equal counters) and roll back the first's history. The fence must
    treat an equal counter with a DIFFERENT token as a supersession: a task that finds a
    different N+1 already committed cannot prove it is the later one and steps aside.

    It fails if the fence compares the counter alone: then the equal-counter commit lands and
    rolls back the committed generation.
    """
    settings = _settings(tmp_path)
    plan = backup_mod.objects_to_back_up(settings)
    # Same counter 3, different suffixes: a DIFFERENT task's N+1 is already committed.
    committed_token = "00000000000000000003-ffffffffffffffff"
    this_token = "00000000000000000003-0000000000000000"
    body = generation_mod.pointer_body(keys.new_generation_id(), committed_token)
    store = _Recorder()
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    committed = generation_mod.Pointer(
        generation=json.loads(body.decode())["generation"],
        authority=frozenset(keys.AUTHORITY_NAMES),
        incarnation=committed_token,
        etag='"etag"',
    )
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
            incarnation=this_token,
        )
    finally:
        plan.close_authority()
    pointer = keys.authority_pointer_key(settings)
    # Equal counter, different token: refused as superseded, and the committed pointer is
    # left as the other task wrote it (not rolled back to this task's generation).
    assert any(k == pointer and "superseded" in why for k, why in result.refused)
    assert result.superseded, "the fence must flag the result superseded so run_cycle can end"
    assert store.objects[pointer] == body


def test_a_task_recommitting_its_own_token_across_cycles_is_not_superseded(tmp_path):
    """The equal-token case is NOT a supersession: one task's ordinary cycles must commit.

    A task mints its incarnation once and passes the SAME token every cycle, so a later cycle
    sees its OWN token committed. That is the identical token, not a different N+1, so the
    fence does not refuse it -- otherwise a single task could never commit twice.
    """
    settings = _settings(tmp_path)
    own = "00000000000000000003-0000000000000000"
    plan = backup_mod.objects_to_back_up(settings)
    body = generation_mod.pointer_body(keys.new_generation_id(), own)
    store = _Recorder()
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    committed = generation_mod.Pointer(
        generation=json.loads(body.decode())["generation"],
        authority=frozenset(keys.AUTHORITY_NAMES),
        incarnation=own,
        etag='"etag"',
    )
    result = backup_mod.CycleResult()
    try:
        backup_mod._commit_generation(
            plan,
            settings=settings,
            store=store,
            state={},
            result=result,
            generation_id=plan.generation_id,
            committed=committed,
            incarnation=own,
        )
    finally:
        plan.close_authority()
    # Its own token: not superseded, so no superseded refusal.
    assert not any("superseded" in why for _k, why in result.refused)


def test_the_pointer_carries_the_task_incarnation_round_trip(tmp_path):
    """``read_pointer`` reads back the incarnation ``pointer_body`` wrote, so the fence works."""
    settings = _settings(tmp_path)
    token = "01790000000000000000-abcdefabcdefabcd"
    body = generation_mod.pointer_body(_GEN, token)
    pointer = generation_mod.read_pointer(settings, _Bucket({keys.AUTHORITY_POINTER_NAME: body}))

    assert pointer is not None
    assert pointer.incarnation == token
    assert pointer.generation == _GEN


def test_a_pointer_written_before_the_incarnation_field_reads_as_empty(tmp_path):
    """A bucket from a writer that predates the incarnation has no key: read it as "".

    "" never compares as newer than a real token, so a bucket an earlier writer made stays
    committable -- the fence does not wedge a predecessor-free bucket.
    """
    settings = _settings(tmp_path)
    legacy = json.dumps({"generation": _GEN, "authority": sorted(keys.AUTHORITY_NAMES)}).encode()
    pointer = generation_mod.read_pointer(settings, _Bucket({keys.AUTHORITY_POINTER_NAME: legacy}))

    assert pointer is not None
    assert pointer.incarnation == ""


@pytest.mark.parametrize("bad", ["z", "not-a-token", "123", "01790000000000000000", 5, 1.5, None])
def test_a_malformed_incarnation_is_refused_rather_than_freezing_the_fence(tmp_path, bad):
    """A non-empty incarnation that is not a well-formed token must be refused, not trusted.

    Tokens are always ``<20-digit counter>-<16 hex>`` and the fence reads the leading COUNTER
    from them. A non-conforming non-empty value -- a bare ``"z"``, a bad shape, a non-string --
    has no counter to read, and if ``read_pointer`` passed it through the pointer would carry
    a value the fence cannot order, so ``read_pointer`` refuses it by the same shape check that
    guards the generation id. ``read_pointer`` exists to distrust bucket bytes. "" alone is the
    accepted non-token value (back-compat, counter 0), proven by the test above.

    It fails if a malformed incarnation is accepted.
    """
    settings = _settings(tmp_path)
    body = json.dumps(
        {"generation": _GEN, "incarnation": bad, "authority": sorted(keys.AUTHORITY_NAMES)}
    ).encode()
    with pytest.raises(generation_mod.PointerUnusable):
        generation_mod.read_pointer(settings, _Bucket({keys.AUTHORITY_POINTER_NAME: body}))


def test_two_writers_differing_transcript_bytes_take_distinct_blob_keys(tmp_path):
    """The F1 fix for the DATA objects: a stale transcript PUT cannot overwrite a newer one.

    The hazard the finding named is a transcript object: two sidecars overlapping in the
    task-replacement window wrote one mutable ``data/sessions/<stem>`` key, so the old
    writer's PUT could land second and overwrite the replacement's newer bytes, and the new
    writer's own fingerprint then answered already-durable, so no re-upload, no error. Content
    addressing removes the shared key: a transcript's key IS the digest of its bytes, so two
    writers of DIFFERENT bytes take DIFFERENT keys and neither overwrites the other; the
    committed index names whichever digest its cycle recorded. This proves two cycles writing
    different bytes for the same stem land at two distinct blob keys, both present.

    It fails if the transcript key stops being content-addressed -- a reversion to the mutable
    per-stem key would collide the two writes on one key.
    """
    settings = _settings(tmp_path)

    _transcript(settings, b"older bytes\n")
    store = _Recorder()
    backup_mod.run_cycle(settings, store, state={})

    # The replacement serves a newer turn for the SAME slot: different bytes, same stem.
    _transcript(settings, b"newer bytes\n")
    backup_mod.run_cycle(settings, store, state={})

    old_blob = _blob_key(settings, b"older bytes\n")
    new_blob = _blob_key(settings, b"newer bytes\n")
    assert old_blob != new_blob
    # Both are present: the newer write did not overwrite the older at a shared key.
    assert store.objects[old_blob] == b"older bytes\n"
    assert store.objects[new_blob] == b"newer bytes\n"


def test_a_commit_drops_the_superseded_generations_state_entries(tmp_path):
    """The lifetime state map must not gain three permanent entries per committing cycle.

    Each cycle mints a fresh generation id and records its two authority keys and its
    transcript-index key in *state* so an unchanged pair/index is not re-PUT. Those keys are
    per-generation, read only for the CURRENTLY committed generation, so once the pointer
    moves off a generation its three entries are dead. Without eviction the one DurableState
    the sidecar holds for its whole life grows by three every cycle. This drives two
    committing cycles and asserts the first generation's three keys are gone from state after
    the second commits, while the second generation's are present.

    It fails if the superseded generation's entries are left in state (unbounded growth).
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()
    state = backup_mod.DurableState()

    backup_mod.run_cycle(settings, store, state=state)
    pointer_body = store.objects[keys.authority_pointer_key(settings)]
    gen_one = json.loads(pointer_body.decode())["generation"]

    # Change the pair so a NEW generation is committed (an unchanged pair would be skipped).
    (settings.config_dir / "open_slots.json").write_bytes(b'{"keys": ["cust-new"]}')
    backup_mod.run_cycle(settings, store, state=state)
    gen_two = json.loads(store.objects[keys.authority_pointer_key(settings)].decode())["generation"]
    assert gen_one != gen_two

    # The superseded generation's control-plane keys are evicted from the lifetime map.
    for name in keys.AUTHORITY_NAMES:
        assert keys.authority_generation_key(settings, gen_one, name) not in state
    assert keys.transcript_index_key(settings, gen_one) not in state
    # The newly committed generation's keys are retained for the next cycle's unchanged check.
    for name in keys.AUTHORITY_NAMES:
        assert keys.authority_generation_key(settings, gen_two, name) in state
    assert keys.transcript_index_key(settings, gen_two) in state


def test_a_pointer_that_cannot_be_read_refuses_the_final_cycle(tmp_path):
    """The read twin of the pin above: an unreadable pointer is the same loss as an unsent one.

    A transient GetObject failure on the pointer is not a permanent code, so it arrives as
    PointerUnusable rather than StoreUnusable and does not end the process. Without a
    committed slot the cycle cannot choose where to write, so the pair is withheld -- and on
    the final cycle that withholding is the whole loss: the pointer still names the older
    generation, the pair this drain flush produced is never published, and the task reports
    a clean stop. The transcripts are in the bucket and nothing references them.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerUnreadable(_Recorder):
        def get(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> bytes:
            assert key == pointer, "the cycle reads nothing but the pointer"
            raise RuntimeError("InternalError")

    with pytest.raises(backup_mod.BackupIncomplete):
        backup_mod.run_cycle(
            settings, _PointerUnreadable(), state={}, deadline=time.monotonic() + 3600
        )


def test_a_pointer_that_cannot_be_read_only_waits_on_an_interval_cycle(tmp_path):
    """The mirror again: an interval cycle re-reads the pointer next time, so it waits."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    pointer = keys.authority_pointer_key(settings)

    class _PointerUnreadable(_Recorder):
        def get(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> bytes:
            assert key == pointer, "the cycle reads nothing but the pointer"
            raise RuntimeError("InternalError")

    result = backup_mod.run_cycle(settings, _PointerUnreadable(), state={})

    assert result.complete, "an interval cycle re-reads the pointer, so it waits rather than fails"
    assert result.refused == []
    assert set(result.withheld) == _authority_keys_in(settings, result.withheld)
    assert len(result.withheld) == len(keys.AUTHORITY_NAMES)


# --- 16. some of the authority pair is not the same as none of it --------------------


def test_one_authority_file_present_is_refused_rather_than_half_published(tmp_path):
    """No whole pair means no generation, so the file it has has nowhere readable to go.

    With no pointer committed, a restore reads the legacy keys, which this writer never
    writes -- so uploading the one file that exists puts it in a generation nothing can
    reach while the cycle reports itself complete and exits zero. The two files have
    independent writers, so a one-file window is ordinary timing skew rather than an extreme
    state, which is why it must not be the quiet path.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    (settings.config_dir / "open_slots.json").unlink()
    store = _Recorder()

    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state={})

    assert "open_slots.json" in [name for name, _why in caught.value.result.refused]
    # The file that IS there is withheld rather than published into an unreachable generation.
    assert not any("session_map.json" in key for key in store.objects)
    assert any("session_map.json" in key for key in caught.value.result.withheld)
    assert not any(key.endswith("generation.json") for key in store.objects)
    # The transcript still uploads: the index is what is waiting, not the conversation.
    assert _blob_key(settings, b"a turn\n") in store.objects


def test_no_authority_file_at_all_is_still_a_clean_first_boot(tmp_path):
    """The over-strict direction. A task that has served no turn has no index to preserve.

    Refusing here would make every first boot a failing cycle and a non-zero drain, which is
    the case the module's contract calls out as explicitly not a fault.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    for name in keys.AUTHORITY_NAMES:
        (settings.config_dir / name).unlink()
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.complete
    assert result.refused == []
    assert sorted(result.gone) == sorted(keys.AUTHORITY_NAMES)
    assert _blob_key(settings, b"a turn\n") in store.objects


def test_a_whole_pair_still_publishes_and_commits(tmp_path):
    """The third direction: the refusal is conditional on the pair being PARTIAL."""
    settings = _settings(tmp_path)
    _transcript(settings, b"a turn\n")
    store = _Recorder()

    result = backup_mod.run_cycle(settings, store, state={})

    assert result.complete
    assert len(_authority_keys_in(settings, store.objects)) == len(keys.AUTHORITY_NAMES)


# --- Opus: a failed replacement-blob PUT must not erase the prior committed blob's record ----


def test_a_refused_new_blob_put_keeps_the_prior_committed_blob_recorded_durable(tmp_path):
    """A changing transcript whose new blob PUT fails must NOT drop the prior durable key.

    A transcript that gains turns takes a fresh content key; the prior key is genuinely
    durable (its blob is in the bucket and the committed index names it). The eviction of
    that prior key must happen only once the NEW key is recorded durable -- never before the
    PUT. If the new PUT is refused (a transport fault), the prior key must stay in *state* so
    a conversation the owner deletes next cycle is still judged durable against its committed
    blob, rather than wrongly withholding the pair on a lossless cycle.

    It fails if the eviction runs before the PUT (the original ordering): the prior key is
    popped and the refused new key is never recorded, so neither is in *state*.
    """
    settings = _settings(tmp_path)
    path = _transcript(settings, b"turn 1\n")
    store = _Recorder()
    state = backup_mod.DurableState()
    # Cycle 1: the first blob lands and is recorded durable.
    backup_mod.run_cycle(settings, store, state=state)
    prior_key = state.live_blobs[STEM]
    assert prior_key in state

    # The transcript gains a turn -> a new content key. The store refuses the NEW blob's PUT
    # (a transport fault), but the pointer/authority objects still succeed.
    path.write_bytes(b"turn 1\nturn 2\n")
    new_digest = __import__("hashlib").sha256(b"turn 1\nturn 2\n").hexdigest()
    new_key = keys.blob_key(settings, new_digest)

    class _RefusesTheNewBlob(_Recorder):
        def __init__(self, seed: _Recorder) -> None:
            super().__init__()
            self.objects = dict(seed.objects)
            self.etags = dict(seed.etags)

        def put(self, key: str, body, size: int, **kw) -> str | None:
            if key == new_key:
                raise RuntimeError("SlowDown writing the new blob")
            return super().put(key, body, size, **kw)

    store2 = _RefusesTheNewBlob(store)
    try:
        backup_mod.run_cycle(settings, store2, state=state)
    except backup_mod.BackupIncomplete:
        pass  # the refused blob makes the cycle incomplete, which is expected

    # The new key was refused, so it is NOT durable -- and the prior key must STILL be
    # recorded, because the eviction only runs once the new key lands.
    assert new_key not in state, "a refused PUT must not record the new key durable"
    assert prior_key in state, (
        "the prior committed blob key was evicted before its replacement landed -- a failed "
        "PUT erased the proof the still-committed blob is durable"
    )
