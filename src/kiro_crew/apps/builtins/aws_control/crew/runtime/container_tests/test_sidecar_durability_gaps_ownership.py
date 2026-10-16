"""Writer ownership: the claim made before restoration, and what ends a superseded writer.

Before it restores, the supervisor claims ownership in shared storage by advancing the
committed pointer's incarnation under a compare-and-swap, and an aborted startup releases that
claim without clobbering a newer one. The sidecar adopts the claimed incarnation instead of
minting its own, refuses to boot on a startup pointer it cannot read, and ends the process when
a newer incarnation supersedes it -- while a newer incarnation that is only a provisional claim
stays a retryable fence.
"""

from __future__ import annotations

import signal
from collections.abc import Callable

import pytest
from container.common import keys
from container.sidecar import __main__ as sidecar_main
from container.sidecar import backup as backup_mod
from container.sidecar import generation as generation_mod
from container.sidecar.store import ObjectAbsent

from .sidecar_durability_gaps_helpers import (
    _Recorder,
    _settings,
    _transcript,
)

# --- F2: writer ownership is claimed in shared storage before restoration --------------------


def test_claim_ownership_bumps_the_committed_incarnation_under_a_cas(tmp_path):
    """`claim_ownership` advances the committed pointer's incarnation in place, by CAS.

    The claim fences a predecessor from the instant of restoration rather than after the
    successor's first commit. It bumps the committed incarnation (same generation) under an
    ``If-Match`` on the ETag it read.

    It fails if the claim does not advance the counter, or writes a new generation rather than
    bumping in place.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    base = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(gen_id, base)
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    claimed = generation_mod.claim_ownership(settings, store).token

    assert keys.incarnation_counter(claimed) == keys.incarnation_counter(base) + 1
    committed = generation_mod.read_pointer(settings, store)
    assert committed is not None and committed.incarnation == claimed
    assert committed.generation == gen_id  # same generation, only the incarnation advanced


def test_claim_ownership_fences_a_predecessors_later_commit(tmp_path):
    """After the successor claims, a predecessor's commit of its older incarnation is refused.

    This is the F2 fix end to end: the predecessor holds the pre-claim incarnation, the
    successor claims (bumping the committed one), and the predecessor's later commit is then
    refused by the incarnation fence rather than deleting the successor's slot.

    It fails if the claim does not raise the committed incarnation above the predecessor's --
    the predecessor's stale pair would then commit.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    generation_mod.claim_ownership(settings, store)

    committed = generation_mod.read_pointer(settings, store)
    assert committed is not None
    # The committed token supersedes the predecessor's, and the predecessor's does not
    # supersede the committed one, so the predecessor's commit is fenced.
    assert keys.incarnation_supersedes(committed.incarnation, predecessor)
    assert not keys.incarnation_supersedes(predecessor, committed.incarnation)


def test_release_ownership_restores_the_prior_pointer_when_its_claim_still_stands(tmp_path):
    """GPT F1: an aborted startup releases its claim, restoring the predecessor's pointer.

    The claim bumps the committed incarnation before restoration. A task that never reaches
    readiness aborts without committing, so leaving the bump raised would fence a still-healthy
    predecessor forever. ``release_ownership`` restores the EXACT pre-claim bytes under a CAS,
    so the committed incarnation returns to the predecessor's own token.

    It fails if the release does not restore the prior incarnation (the permanent-fence bug).
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    claim = generation_mod.claim_ownership(settings, store)
    bumped = generation_mod.read_pointer(settings, store)
    assert bumped is not None and keys.incarnation_supersedes(bumped.incarnation, predecessor)

    released = generation_mod.release_ownership(settings, store, claim)

    assert released is True
    restored = generation_mod.read_pointer(settings, store)
    assert restored is not None
    # The predecessor's own incarnation is committed again, so the fence stops superseding it.
    assert restored.incarnation == predecessor
    assert not keys.incarnation_supersedes(restored.incarnation, predecessor)
    assert restored.generation == gen_id


def test_release_ownership_is_a_no_op_when_a_newer_successor_has_claimed(tmp_path):
    """A release that lost the pointer to a newer successor leaves that live claim in place.

    If a second successor claimed on top of the first before the first aborted, reverting to
    the first's prior bytes would clobber the second's live claim and un-fence the aborting
    task against it. The release is a CAS on the first claim's own ETag, so once the pointer
    has moved on the release is a no-op.

    It fails if the release writes unconditionally and clobbers the newer claim.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    first = generation_mod.claim_ownership(settings, store)
    # A newer successor claims on top before the first task aborts.
    second = generation_mod.claim_ownership(settings, store)
    after_second = generation_mod.read_pointer(settings, store)
    assert after_second is not None and after_second.incarnation == second.token

    released = generation_mod.release_ownership(settings, store, first)

    assert released is False
    # The newer successor's claim is untouched.
    now = generation_mod.read_pointer(settings, store)
    assert now is not None and now.incarnation == second.token


def test_release_ownership_un_fences_the_predecessors_next_commit(tmp_path):
    """End to end: after a failed successor releases, the predecessor's commit is accepted again.

    This is the F1 fix proven at the fence: the predecessor holds its pre-claim incarnation, a
    successor claims (fencing it) then aborts and releases, and the predecessor's later commit
    -- which the raised bump would have refused as superseded -- is accepted.

    It fails if the release does not restore the predecessor's standing.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    claim = generation_mod.claim_ownership(settings, store)
    committed_after_claim = generation_mod.read_pointer(settings, store)
    assert committed_after_claim is not None
    # While claimed, the predecessor would be fenced.
    assert keys.incarnation_supersedes(committed_after_claim.incarnation, predecessor)

    generation_mod.release_ownership(settings, store, claim)

    committed_after_release = generation_mod.read_pointer(settings, store)
    assert committed_after_release is not None
    # The predecessor is not superseded, so its next commit is accepted.
    assert not keys.incarnation_supersedes(committed_after_release.incarnation, predecessor)


def test_a_claim_whose_put_commits_but_loses_its_ack_is_released_not_stranded(tmp_path):
    """GPT F2: a claim PUT that commits on S3 but whose ack is lost does not strand the claim.

    ``store.put`` can land the bytes and still raise -- a degraded link dropping the response
    after the SDK's retries. ``claim_ownership`` would then raise before returning a
    ClaimedOwnership, so the supervisor's abort has no claim object to release, yet the
    committed incarnation was bumped, fencing a live predecessor for good. The recovery
    re-reads the pointer on an ambiguous failure, sees this claim's token committed, and
    CAS-releases it back to the committed owner before re-raising the original failure.

    It fails if the ambiguous PUT failure is not recovered: the committed-but-unacked claim's
    token stays on the pointer, and the predecessor stays fenced.
    """

    class _LostAckOnClaim(_Recorder):
        def __init__(self) -> None:
            super().__init__()
            self._raised = False

        def put(self, key, body, size, **kw):
            # The claim write (if_match on the pointer, claim body) commits, then its ack is
            # lost exactly once -- the recovery's own release PUT is allowed through.
            is_claim_put = (
                key == keys.authority_pointer_key(settings)
                and kw.get("if_match") is not None
                and not self._raised
            )
            etag = super().put(key, body, size, **kw)  # the bytes land
            if is_claim_put:
                self._raised = True
                raise backup_mod.StoreUnusable("the claim PUT response was lost after it committed")
            return etag

    settings = _settings(tmp_path)
    store = _LostAckOnClaim()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    predecessor_body = generation_mod.pointer_body(gen_id, predecessor)
    store.objects[keys.authority_pointer_key(settings)] = predecessor_body
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    # The original failure still propagates: the task refuses to start, it could not confirm
    # ownership.
    with pytest.raises(backup_mod.StoreUnusable):
        generation_mod.claim_ownership(settings, store)

    # But the committed-but-unacked claim was released: the pointer is back to the
    # predecessor's own incarnation, so the predecessor is not left fenced.
    committed = generation_mod.read_pointer(settings, store)
    assert committed is not None
    assert committed.incarnation == predecessor
    assert not committed.is_claim


def test_claim_disables_rollback_when_a_concurrent_write_lands_before_the_reread(tmp_path):
    """GPT F1: the release validator comes from the claim's own PUT, not a fallible reread.

    The release's CAS validator is the ETag the claim's own write RETURNS (S3's PutObject
    response), never a follow-up GET. A confirmation GET after the claim can fail or time out
    on a degraded link even though the PUT already committed -- and that exception would escape
    before any ClaimedOwnership exists to release, leaving a live predecessor fenced for good.
    So this test makes every GET after the claim's PUT raise, and asserts the claim still
    returns with a usable release validator and the abort path then reverts the predecessor.

    It fails if claim_ownership performs a post-PUT confirmation read (the F1 crash: an
    exception there escapes and no release is possible).
    """
    settings = _settings(tmp_path)
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()

    class _NoReadAfterClaimPut(_Recorder):
        """Any GET of the pointer AFTER the claim's PUT raises, as a degraded link would."""

        def __init__(self) -> None:
            super().__init__()
            self._claim_written = False

        def put(self, key, body, size, **kw):
            etag = super().put(key, body, size, **kw)
            if key == keys.authority_pointer_key(settings):
                self._claim_written = True
            return etag

        def get(self, key, *, limit, deadline=None, cancel=None):
            if self._claim_written and key == keys.authority_pointer_key(settings):
                raise ObjectAbsent(key)  # stand-in for a GET that fails on a degraded link
            return super().get(key, limit=limit, deadline=deadline)

    store = _NoReadAfterClaimPut()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    # The claim must NOT read the pointer back after its PUT; it returns a usable validator.
    claim = generation_mod.claim_ownership(settings, store)
    assert claim.prior_etag is not None
    assert claim.prior_body == generation_mod.pointer_body(gen_id, predecessor)

    # The abort path can therefore revert, restoring the live predecessor's own incarnation.
    assert generation_mod.release_ownership(settings, store, claim) is True
    store._claim_written = False  # let the read through to verify the restored state
    restored = generation_mod.read_pointer(settings, store)
    assert restored is not None and restored.incarnation == predecessor
    assert not keys.incarnation_supersedes(restored.incarnation, predecessor)


def test_release_after_a_newer_claim_is_a_no_op_cas(tmp_path):
    """GPT F1: a release whose claim a newer successor has superseded is a no-op via CAS.

    If another successor claims on top of this one, this claim's release CAS (on the ETag its
    own write left) fails to match the committed pointer, so the revert is refused rather
    than clobbering the newer, live claim.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    predecessor = keys.new_incarnation()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-1"'

    claim = generation_mod.claim_ownership(settings, store)
    # A newer successor claims on top, moving the pointer's ETag past this claim's.
    newer = generation_mod.claim_ownership(settings, store)
    assert newer.token != claim.token

    # This claim's release cannot match the committed ETag now: no-op, newer claim stands.
    assert generation_mod.release_ownership(settings, store, claim) is False
    now = generation_mod.read_pointer(settings, store)
    assert now is not None and now.incarnation == newer.token


def test_release_restores_the_carried_committed_owner_through_a_nested_claim(tmp_path):
    """GPT F3: a nested claim restores the LIVE committed owner, not a dead claimant's token.

    When this claim bumps from a pointer that was ITSELF a provisional claim (an overlapping
    task that also claimed before readiness and is also aborting), the committed owner the
    first claim bumped from is carried forward in ``committed_incarnation``. The release then
    restores THAT real predecessor, so the live writer is un-fenced -- not the dead first
    claimant's token.

    It fails if the release restores the immediate prior pointer (the dead-claimant-token bug)
    or refuses to revert at all (leaving the live predecessor fenced).
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    gen_id = keys.new_generation_id()
    live_predecessor = keys.new_incarnation()
    dead_claim_token = keys.next_incarnation(live_predecessor)
    # The committed pointer is a CLAIM by a prior overlapping (also aborting) task, and it
    # carries the real committed owner it bumped from.
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        gen_id, dead_claim_token, claim=True, committed_incarnation=live_predecessor
    )
    store.etags[keys.authority_pointer_key(settings)] = '"ptr-claim"'

    claim = generation_mod.claim_ownership(settings, store)
    # The claim carried the live committed owner forward, not the dead claimant's token.
    assert claim.prior_body == generation_mod.pointer_body(gen_id, live_predecessor)

    released = generation_mod.release_ownership(settings, store, claim)

    # The release restores the LIVE predecessor, not the dead claimant's token.
    assert released is True
    now = generation_mod.read_pointer(settings, store)
    assert now is not None and now.incarnation == live_predecessor
    assert now.is_claim is False
    assert not keys.incarnation_supersedes(now.incarnation, live_predecessor)


def test_claim_ownership_pre_mints_a_base_token_at_generation_zero_without_writing(tmp_path):
    """GPT F4: generation 0 mints a base token for the sidecar to adopt but writes no pointer.

    There is no committed pointer to CAS-bump, and writing one would commit an empty
    generation and strand the first real commit's legacy migration. But the task still mints
    its base token (counter 1) BEFORE restoration and returns it so the supervisor exports it
    and the sidecar ADOPTS it -- rather than the sidecar deriving a HIGHER token from a
    predecessor's first commit that lands in the restore-to-sidecar window and then
    superseding and overwriting it. Nothing is written, so there is nothing to release.

    It fails if the claim writes a pointer at generation 0, or returns an empty token the
    sidecar would then derive past.
    """
    settings = _settings(tmp_path)
    store = _Recorder()

    claimed = generation_mod.claim_ownership(settings, store)

    # A base token (counter 1) is minted and will be exported for the sidecar to adopt.
    assert claimed.token != ""
    assert keys.incarnation_counter(claimed.token) == 1
    # Nothing was written and there is nothing to release.
    assert claimed.prior_body is None
    assert claimed.prior_etag is None
    assert keys.authority_pointer_key(settings) not in store.objects


def test_the_sidecar_adopts_the_claimed_incarnation_from_the_environment(tmp_path, monkeypatch):
    """The sidecar uses the supervisor's claimed token rather than minting a second one.

    If it re-derived its own, it would bump a second time and leave the committed pointer
    naming a token no live task holds -- the writer's own first commit fenced against its own
    claim. Adopting the exported token is what makes the pre-restore claim and the writer one
    ownership.

    It fails if ``startup_incarnation`` ignores the env var and derives from the pointer.
    """
    settings = _settings(tmp_path)
    store = _Recorder()
    claimed = keys.next_incarnation(keys.new_incarnation())
    monkeypatch.setenv(generation_mod.ENV_CLAIMED_INCARNATION, claimed)

    assert backup_mod.startup_incarnation(settings, store) == claimed


# --- F3: a transient startup pointer read propagates rather than minting a base token --------


def test_startup_incarnation_propagates_an_unreadable_pointer(tmp_path):
    """A present-but-unreadable pointer is NOT swallowed into a base token.

    A base token minted from an unreadable pointer is strictly less than a predecessor's
    committed one, so the fence would refuse every commit for the task's life. Propagating
    ``PointerUnusable`` makes the writer refuse to start instead -- a restart re-reads the
    pointer, which a transient fault clears.

    It fails if the handler returns a base token (the permanent-fence regression).
    """
    settings = _settings(tmp_path)

    class _PointerUnreadable(_Recorder):
        def get_with_etag(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> tuple[bytes, str | None]:
            if key == keys.authority_pointer_key(settings):
                raise RuntimeError("SlowDown reading the pointer")
            return super().get_with_etag(key, limit=limit, deadline=deadline)

    with pytest.raises(generation_mod.PointerUnusable):
        backup_mod.startup_incarnation(settings, _PointerUnreadable())


def test_an_unreadable_startup_pointer_refuses_the_boot_with_a_distinct_exit_code(
    tmp_path, monkeypatch
):
    """`main` ends non-zero on an unreadable startup pointer rather than running a doomed writer.

    The exit code is the only signal the supervisor reads, so the refusal has to reach it --
    and distinctly from the bucket-unusable code, since the cause and remedy differ.

    It fails if the process mints a base token and runs, committing nothing for its whole life.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    monkeypatch.setattr(sidecar_main.common, "load", lambda: settings)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)

    class _PointerUnreadable(_Recorder):
        def get_with_etag(
            self,
            key: str,
            *,
            limit: int,
            deadline: float | None = None,
            cancel: Callable[[], bool] | None = None,
        ) -> tuple[bytes, str | None]:
            if key == keys.authority_pointer_key(settings):
                raise RuntimeError("SlowDown reading the pointer")
            return super().get_with_etag(key, limit=limit, deadline=deadline)

    monkeypatch.setattr(sidecar_main, "S3ObjectStore", lambda bucket: _PointerUnreadable())

    code = sidecar_main.main([])
    assert code != 0
    assert code != 3  # distinct from the bucket-unusable exit


# --- GPT F1: a superseded sidecar ends the process instead of serving uncommittable state ----


def test_run_cycle_raises_sidecar_superseded_on_the_incarnation_fence(tmp_path):
    """A superseded commit raises SidecarSuperseded, a distinct cause from BackupIncomplete.

    The incarnation fence records a refusal when a newer task's incarnation is committed.
    That refusal must surface as SidecarSuperseded, not a plain BackupIncomplete, so the loop
    can tell a never-clearing supersession apart from an ordinary retryable incomplete cycle.

    It fails if the superseded refusal is an undifferentiated BackupIncomplete: the loop would
    retry it forever while the front keeps accepting turns no cycle can commit.
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    # A committed pointer carrying a NEWER incarnation than this task's.
    committed_token = "00000000000000000009-ffffffffffffffff"
    this_token = "00000000000000000003-0000000000000000"
    store = _Recorder()
    committed_gen = keys.new_generation_id()
    body = generation_mod.pointer_body(committed_gen, committed_token)
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    # The committed generation's (empty) transcript index must exist, or the cycle refuses on
    # an unreadable index BEFORE it reaches the commit fence -- the fence is what this tests.
    store.objects[keys.transcript_index_key(settings, committed_gen)] = b"{}"

    with pytest.raises(backup_mod.SidecarSuperseded):
        backup_mod.run_cycle(
            settings, store, state=backup_mod.DurableState(), incarnation=this_token
        )


def test_a_superseded_sidecar_ends_the_process_non_zero_rather_than_looping(tmp_path):
    """run() ends non-zero on a supersession instead of retrying it every interval.

    A superseded task can never commit again, so looping is the appearance of durability
    while its front accepts lost turns. run() must let SidecarSuperseded end the process --
    the same posture it takes for StoreUnusable.

    It fails if the loop swallows the supersession as an ordinary failed cycle and keeps
    running (max_cycles would be reached and run() would return 0).
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    committed_token = "00000000000000000009-ffffffffffffffff"
    store = _Recorder()
    committed_gen = keys.new_generation_id()
    body = generation_mod.pointer_body(committed_gen, committed_token)
    store.objects[keys.authority_pointer_key(settings)] = body
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    store.objects[keys.transcript_index_key(settings, committed_gen)] = b"{}"
    # The sidecar adopts a token OLDER than the committed one, so every commit is fenced.
    import os as _os

    _os.environ[generation_mod.ENV_CLAIMED_INCARNATION] = "00000000000000000003-0000000000000000"
    try:
        with pytest.raises(backup_mod.SidecarSuperseded):
            sidecar_main.run(settings, store, max_cycles=5)
    finally:
        _os.environ.pop(generation_mod.ENV_CLAIMED_INCARNATION, None)


def test_a_claim_fence_is_retryable_not_a_supersession(tmp_path):
    """Opus: a newer incarnation that is a PROVISIONAL CLAIM must not kill a live predecessor.

    A replacement bumps the pointer's incarnation BEFORE restoration (claim_ownership) and may
    then fail readiness and abort, releasing the claim. If a healthy predecessor serving turns
    read that claim at its interval and raised SidecarSuperseded, it would be torn down long
    before the failed replacement's 180s-gated abort released the claim -- an outage, and the
    predecessor's turns since its last commit lost. So a commit fenced by a CLAIM pointer is
    refused as RETRYABLE (BackupIncomplete, superseded unset), and the predecessor steps aside
    only once a REAL, unmarked commit carries a newer incarnation.

    It fails if a claim fence sets result.superseded (the outage: run_cycle would raise
    SidecarSuperseded and the predecessor's process would end).
    """
    settings = _settings(tmp_path)
    _transcript(settings, b"turn\n")
    committed_gen = keys.new_generation_id()
    # A newer incarnation, but written as a provisional CLAIM (claim=True), as a replacement's
    # pre-restore ownership bump is. The predecessor adopts an OLDER token.
    claim_token = "00000000000000000009-ffffffffffffffff"
    predecessor = "00000000000000000003-0000000000000000"
    store = _Recorder()
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        committed_gen, claim_token, claim=True
    )
    store.etags[keys.authority_pointer_key(settings)] = '"etag"'
    store.objects[keys.transcript_index_key(settings, committed_gen)] = b"{}"
    state = backup_mod.DurableState()

    # A claim fence is refused RETRYABLY: run_cycle raises BackupIncomplete, NOT
    # SidecarSuperseded, so the predecessor's loop keeps it alive and retries next interval.
    with pytest.raises(backup_mod.BackupIncomplete) as caught:
        backup_mod.run_cycle(settings, store, state=state, incarnation=predecessor)
    assert not isinstance(caught.value, backup_mod.SidecarSuperseded)

    # A REAL (unmarked) newer commit, by contrast, DOES supersede the predecessor terminally.
    store.objects[keys.authority_pointer_key(settings)] = generation_mod.pointer_body(
        committed_gen, claim_token
    )
    store.etags[keys.authority_pointer_key(settings)] = '"etag2"'
    with pytest.raises(backup_mod.SidecarSuperseded):
        backup_mod.run_cycle(settings, store, state=state, incarnation=predecessor)
