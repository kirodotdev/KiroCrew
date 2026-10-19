"""Account-identity retirement: retiring the processes that authenticated as another account.

``retire_kiro_identity_sessions`` is the sweep that runs after an identity-store change,
behind the cold-start permit barrier, sparing holders that provably spawned under the live
account and deferring a parent that still owes sub-agent work;
``flag_identity_stamp_mismatches`` is the per-turn gate that flags a session whose child's
spawn stamp names another account. Companion runtimes are retired or displaced by the two
idle-runtime reapers. A new rule for retiring wrong-account processes goes here.

Instance fields written: none.

Instance fields read: ``_deps``, ``_owner``, ``state``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, AbstractSet, Any

from kiro_crew.kiro_prerequisite import (
    identity_component_dropped,
    identity_park_grace_remaining,
    identity_stamp_mismatch,
    mark_identity_parked,
    spawn_identity_of,
    spawned_under,
)
from kiro_crew.metrics.sessions import END_REASON_RETIRED, record_sessions_ended

if TYPE_CHECKING:
    from kiro_crew.session_lifecycle import SessionLifecycleService


async def flag_identity_stamp_mismatches(self: SessionLifecycleService, live: str) -> list[str]:
    """Mark sessions -- and retire idle companion runtimes -- whose child
    PROVABLY spawned under a different account.

    The consumer of the spawn-identity stamp
    (``kiro_prerequisite.stamp_spawn_identity``): each kiro-backed provider
    records the account the store held as its process started, and this
    compares those records against *live* -- the fresh fingerprint the turn
    gate just read. A mismatch means the child authenticated as an account
    other than the one the store currently names, even when the baseline and the interim
    latch both compare equal because no read ever observed the interim
    (the A->B->A round trip a gateway-wide baseline is inherently blind
    to).

    Flag-only for sessions: it sets the existing ``retire_on_identity_change``
    eviction flag -- the next acquire on that key reports the session
    invalid and the stale-provider path recycles it -- and never touches
    the sweep baseline, and never records anything sticky. That is the
    loop guard: a wrong observation
    here costs one targeted recycle of one session, not a
    retire-until-complete sweep, and on a healthy host every stamp equals
    *live* so this is a no-op per turn. Sessions with no stamp are skipped
    (``identity_stamp_mismatch`` refuses them), keeping every unstamped
    child on the pre-stamping protections rather than guessing.

    Companion runtimes are not sessions: the background runtime and the
    per-parent subagent runtimes hold their own kiro-backed processes,
    carry the same spawn stamp, and never appear in ``owner._sessions``,
    so the session scan cannot reach them. They have no per-session
    eviction flag either, so a PROVEN mismatch retires an idle one through
    the sweep's own idle-runtime reapers, narrowed by a predicate -- this
    gate never calls ``release_subagent_runtime`` itself, keeping those
    reapers the module's only such path outside the paired parent-end
    sites. A busy runtime is never killed mid-turn (killing live
    work is the defect this change exists to remove); instead the reapers
    DISPLACE it out of the claimable slot -- popped from the registry (or
    the ``_bg`` slot) and parked on a drain list -- so a new acquisition
    spawns a replacement under the live account rather than demuxing
    fresh sessions onto the wrong-account process, and the parked
    runtime is killed by a later pass once its in-flight work drains.
    """

    if not live:
        return []
    owner = self._owner
    flagged: list[str] = []
    async with owner._lock:
        for key, sess in owner._sessions.items():
            if sess.retire_on_identity_change:
                continue
            provider = sess.provider
            if not self._deps.provider_uses_kiro_identity_store(provider):
                continue
            if identity_stamp_mismatch(spawn_identity_of(provider), live):
                sess.retire_on_identity_change = True
                flagged.append(key)
    # Drop the durable resume pointer for every flagged key, mirroring
    # the sweep's clear_sid over its invalidated keys. Without this, the
    # eviction that recycles the flagged session leaves the old sid in
    # the map: get_or_create's re-entry reads it as resume_sid and the
    # replacement child -- authenticated under the CURRENT account --
    # issues session/load on the FLAGGED account's conversation, whose
    # signed thinking blocks its provider then rejects wholesale. And
    # close_all skips retire-flagged sessions by design, so the stale
    # pointer would survive a gateway restart too. Outside owner._lock:
    # clear_sid persists to disk, and the worst interleaving (a successor
    # publishing its fresh sid between the flag and this clear) costs one
    # lost resume pointer, never a wrong-account load. Session keys only:
    # the runtime pseudo-keys appended below name processes, not map rows.
    for key in flagged:
        owner._session_map.clear_sid(key)
    # Companion runtimes, via the dedicated idle-runtime reapers rather
    # than releasing here: those reapers are the module's only
    # retirement path outside the paired parent-end sites, so the
    # parent-end ratchet stays a single-exemption contract. The predicate
    # narrows their reap to PROVEN wrong-account runtimes; busy runtimes
    # are skipped inside the reapers exactly as the sweep skips them.

    def _mismatched(runtime: object) -> bool:
        return identity_stamp_mismatch(getattr(runtime, "spawn_identity", ""), live)

    retired: list[str] = []
    await self._retire_kiro_subagent_runtimes(should_retire=_mismatched, retired=retired)
    flagged.extend(f"subagent-runtime:{parent_key}" for parent_key in retired)
    bg_retired: list[str] = []
    await self._retire_kiro_bg_runtime(
        should_retire=_mismatched,
        retired=bg_retired,
        reason="spawn identity mismatch retirement",
    )
    flagged.extend(bg_retired)
    if flagged:
        self._deps.logger.info(
            "Flagged %d holder(s) whose child spawned under a different "
            "account than the live one: %s",
            len(flagged),
            ", ".join(sorted(flagged)),
        )
    return flagged


async def retire_kiro_identity_sessions(
    self: SessionLifecycleService, fingerprint: str = "", *, spare_children: bool = False
) -> tuple[list[str], bool]:
    """Retire idle Kiro-backed processes after an identity-store change.

    The start-permit barrier is acquired before the registry scan, making
    that scan authoritative over cold starts that began before the account
    change. Busy sessions are marked for retirement on their next turn and
    keep the sweep incomplete; the session map and pending compaction
    verdicts intentionally survive.

    *fingerprint* is the live identity the caller observed. It keys the
    generation fence below, so a retry for the same pending change skips
    successors that already restarted under the new account, while a sweep
    under a different fingerprint captures afresh. It is also the spare
    test: a holder whose spawn-identity stamp EQUALS it provably
    authenticated as the live account and is left alone -- neither retired
    nor counted against completeness -- because retiring it buys nothing
    and costs its in-flight children (see the loop). Empty means the store
    could not be read: nothing is spared (and see the comment at the fence).

    *spare_children* is the per-turn identity-change path opting an idle
    parent with live ``spawn_run`` children OUT of retirement: the sweep
    there fires on every chat turn, so cancelling those children as a
    "provider shutdown" each turn is a regression, and deferring the parent
    lets them finish. It is False by default so the sign-out path
    (``_retire_runtimes_after_sign_out``) keeps the cancel behaviour: on
    sign-out the parent and its children MUST be retired, or the children
    keep running on the signed-out account's in-memory credential. The
    chat-turn caller passes ``bool(live) and not identity_component_dropped``:
    it defers only a whole switch between two real accounts; an empty live
    fingerprint (an external logout, which never reaches the sign-out
    handler) and a PARTIAL loss (a source present in the retire baseline but
    gone from live) both keep the cancel path.

    When ``spare_children`` is set the retire-vs-defer choice is NOT made
    here under the registry lock: a candidate is left registered, its
    teardown fence is opened (so a child the admission path accepts while the
    decision runs is seen, not raced), and the choice is made at the ASYNC
    teardown off the loop -- any child (live, queued, fenced or durable in the
    store), or a failed store read, defers; a childless parent is retired
    under a re-taken lock. One per-holder exception stays synchronous: a
    parent whose OWN spawn stamp lost a component is retired and its children
    cancelled (the fail-safe for an external logout the baseline missed).
    """
    owner = self._owner
    doomed: list[tuple[str, Any]] = []
    # The spare_children subset committed at the async teardown: (key, provider).
    # Torn down in the method-level ``finally``, shielded, so a cancellation of
    # the sweep cannot leave a committed parent half torn down (its children
    # still running on a logged-out credential).
    committed_teardown: list[tuple[str, Any]] = []
    retired: list[str] = []
    teardown_children_by_key: dict[str, tuple[str, ...]] = {}
    # Candidates whose retire-vs-defer is resolved at the async teardown, which
    # reads the store off the loop under the teardown fence: (key, session,
    # fence) -- the fence handle is kept so a defer releases exactly its own.
    maybe_spare: list[tuple[str, Any, "AbstractSet[str] | None"]] = []
    skipped = False
    waiting_on: list[str] = []
    # One sweep at a time: the permit barrier below (``_start_sem.drain()``) is
    # not re-entrant (a second concurrent drain raises), so a peer sweep waits
    # here for this one.
    # Wrapped in a try whose finally drains the committed retirements OUTSIDE
    # ``identity_sweep_lock`` (so a slow child shutdown cannot hold the lock
    # against a following account change) AND cancellation-safe (so a cancel
    # of the sweep -- the chat closing mid-sweep -- cannot leave an
    # already-unregistered parent's children running on the logged-out
    # credential). Both conditions must hold at once, which is why the drain
    # is a method-level finally after the lock rather than under it.
    try:
        async with self._identity_sweep_lock:
            # Before the barrier, and before any key becomes claimable: every
            # provider already queued in the warm pool authenticated as the
            # previous account, and a pool claim takes no cold-start permit, so
            # the barrier below cannot hold one back. Stamping the pool here is
            # what stops a cleared key from being handed one of those providers
            # while ``_retire_kiro_warm_pool`` (which must run outside the
            # barrier -- see ``mark_identity_epoch``) is still pending.
            owner._mark_identity_epoch()
            # The barrier: every cold-start permit, collected ahead of both start
            # priorities (``PrioritySemaphore.drain``), so a stream of person-started
            # cold starts cannot keep taking the permits this sweep waits for.
            async with owner._start_sem.drain():
                async with owner._lock:
                    # An outstanding sweep is its own retirement trigger, so the
                    # pending fingerprint is recorded before anything is retired
                    # and survives until a sweep COMPLETES. Without it, a switch
                    # back to the reconciled account compares equal and the
                    # holders started under the interim one keep serving turns on
                    # its credential.
                    self.state.identity_sweep_fingerprint = fingerprint
                    # Selection and unregistering share one lock hold so a
                    # chosen idle object cannot start a turn before its pop.
                    #
                    # Telling a new-account successor from an old-account holder
                    # needs the identity each session authenticated under, and
                    # registration order does not carry it: a cold start that
                    # began before the switch registers after it. The spawn
                    # stamp does carry it, so a session whose stamp EQUALS the
                    # live fingerprint is spared -- not retired, not flagged,
                    # and not counted against completeness -- while an
                    # unstamped or differently-stamped one is retired as before.
                    #
                    # The spare is what lets a sweep on a busy host FINISH. Its
                    # completeness is what advances the baseline and clears the
                    # pending fingerprint; without the spare it required every
                    # kiro-backed session to be idle at once, which a gateway
                    # with a dozen live chats never is, so every turn re-swept,
                    # recycled every idle session -- and a parent that ended
                    # its turn with ``spawn_run`` children still running IS
                    # idle by the semaphore test, so its retirement cancelled
                    # them ("provider shutdown") on every turn any chat took.
                    # A retired live-account session also costs a fresh native
                    # conversation for nothing. The spare needs exact equality
                    # (``spawned_under``); sparing the wrong one is the
                    # signature rejection this sweep exists to prevent, and an
                    # unstamped child keeps the pre-stamping treatment.
                    retired_keys: list[str] = []
                    invalidated_keys: list[str] = []
                    for key in list(owner._sessions):
                        sess = owner._sessions[key]
                        if not self._deps.provider_uses_kiro_identity_store(sess.provider):
                            continue
                        if spawned_under(sess.provider, fingerprint):
                            continue
                        if sess.semaphore.locked():
                            sess.retire_on_identity_change = True
                            invalidated_keys.append(key)
                            skipped = True
                            holder = (
                                "channel member"
                                if getattr(sess, "lifecycle_lease", False)
                                else "busy"
                            )
                            waiting_on.append(f"{key} ({holder})")
                            continue
                        # An idle parent that ended its turn with ``spawn_run``
                        # children still running passes the semaphore test above.
                        # Whether to retire it (cancelling those children) or defer
                        # it is NOT decided here under the lock: a durable-only child
                        # lives in the task store, and reading the store on the loop
                        # under this lock waits on the store's busy wait and freezes
                        # the gateway. Instead the decision is made at the ASYNC
                        # teardown, which reads the store off the loop under the
                        # teardown fence -- the same ``_teardown_fence_lock`` the
                        # admission path records a new child into
                        # (``note_teardown_store_accept``), so a child admitted while
                        # the decision runs is seen or ordered rather than raced.
                        #
                        # The one case decided synchronously is a parent whose OWN
                        # spawn stamp lost a credential component: that is the
                        # identity its children loaded, so a component gone from it
                        # means they may be running on a credential that is gone.
                        # That parent is retired and its children cancelled, exactly
                        # as the sign-out path does -- the fail-safe direction. An
                        # unstamped parent (empty spawn identity) proves no drop, so
                        # it takes the ordinary defer-at-teardown path.
                        #
                        # ``spare_children`` is the per-turn identity-change opt-in
                        # (it fires every turn, so cancelling live children each turn
                        # is the regression). The sign-out path and an empty live
                        # fingerprint leave it False and keep the retire-and-cancel
                        # path, because there the account is gone.
                        spawn_source_dropped = identity_component_dropped(
                            spawn_identity_of(sess.provider), fingerprint
                        )
                        if spare_children and not spawn_source_dropped:
                            # Defer the COMMIT to the async teardown. Open the fence
                            # now (synchronously, under the lock) so an admission
                            # between here and the off-loop read is recorded, and keep
                            # the session registered -- nothing irreversible happens
                            # for it under the lock, so "defer" there is simply not
                            # retiring, never a rollback. The in-memory snapshot is
                            # NOT taken here: it arms the delivery gate and tears down
                            # follow-ups, which the parent must keep if it is spared;
                            # the async decision takes it only if it commits.
                            opened_fence = self._open_teardown_fence(key)
                            maybe_spare.append((key, sess, opened_fence))
                            continue
                        del owner._sessions[key]
                        owner._advance_session_generation(key)
                        owner._compact_cooldown_until.pop(key, None)
                        self._suppress_replay.discard(key)
                        self._origin_links.pop(key, None)
                        self.state.stop_requests.pop(key, None)
                        self._discard_replay_gap(key)
                        self.state.orphaned_holders.pop(key, None)
                        retired_keys.append(key)
                        invalidated_keys.append(key)
                        teardown_children_by_key[key] = self._snapshot_parent_children(key)
                        # Do not clear _compact_pending_verdict: the identity
                        # recycle preserves that deferred verdict.
                        doomed.append((key, sess.provider))
                    # Same lock hold as the removals, not down in the shutdown
                    # loop below: that loop awaits, and a replacement session can
                    # register under a retired key while it does. Recorded as one
                    # set so the awaited unlink cannot be cancelled between two
                    # keys and leave the rest behind as fabricated crashes. The
                    # spare_children candidates committed at the async teardown
                    # below record their ends there, under their own lock hold.
                    await record_sessions_ended(retired_keys, end_reason=END_REASON_RETIRED)
                # Drop the pointer to each identity-changed session's NATIVE
                # conversation, the same reason a provider switch drops it: the
                # account that minted it is not the account that would reload it.
                # An extended-thinking model's stored thinking blocks carry a
                # provider signature bound to the conversation they were minted
                # in, so replaying them under the new account is rejected whole
                # ("Invalid `signature` in `thinking` block") and every later turn
                # on that key fails the same way -- the recycle replaces the
                # process but the successor's ``session/load`` walks straight back
                # into the previous account's history. Only the pointer goes; the
                # conversation stays on disk under ``discarded_sid``, and the
                # dashboard transcript is a separate record that survives.
                #
                # Inside the permit barrier and after ``owner._lock`` is released:
                # every cold-start permit is still held here, so no successor can
                # publish a sid for these keys, while ``clear_sid`` persists to
                # disk and must not run under the registry lock.
                for key in invalidated_keys:
                    owner._session_map.clear_sid(key)

                # Resolve the deferred spare_children candidates HERE, still inside
                # the permit barrier (no successor can register a sid) but off the
                # registry lock, so the store read runs on the writer thread. For
                # each, ask whether the retirement would drop sub-agent work the
                # parent is still owed -- a child live or queued in memory, held
                # only by the store, or admitted into the fence since it opened
                # under the lock above, a finished child whose report is not
                # registered yet, or a report still delivering after its run's
                # task ended (``_parent_owes_child_work``).
                #
                #   * Work is owed (or the store read FAILS): DEFER. Flag the
                #     session for retirement on its next turn, cancel nothing, drop
                #     the fence, and leave the sweep incomplete so the next sweep
                #     re-checks. Nothing irreversible happened for this key under
                #     the lock, so a defer is simply not retiring -- no rollback.
                #   * Nothing owed: COMMIT the retirement now. Re-take the registry lock
                #     to unregister (a successor that registered under the key while
                #     the store read ran is left alone -- the pop is guarded on the
                #     session identity), then shut the provider down and run the
                #     cancel, which stops nothing because there is no child.
                # Fences still held by candidates this loop has not resolved yet, so
                # a cancellation (the chat closing mid-sweep) releases them in the
                # ``finally`` rather than leaking a tentative fence that would keep
                # recording every later child admitted under the key.
                unresolved_fences: dict[str, "AbstractSet[str] | None"] = {
                    key: fence for key, _sess, fence in maybe_spare
                }
                handler = self._child_teardown
                try:
                    for key, sess, fence in maybe_spare:
                        try:
                            has_child = await self._parent_owes_child_work(key)
                        except Exception:
                            self._deps.logger.warning(
                                "Identity sweep: reading the children of %s failed; "
                                "deferring its retirement",
                                key,
                                exc_info=True,
                            )
                            has_child = True
                        if has_child:
                            sess.retire_on_identity_change = True
                            if handler is not None:
                                handler.release_teardown_fence(key, fence)
                            unresolved_fences.pop(key, None)
                            # Drop the old-account sid the same way a retired key
                            # does: a deferred parent is evicted and cold-started on
                            # its next turn, and reloading the previous account's
                            # conversation is the signed-thinking rejection the
                            # sweep's sid drop prevents. Inside the permit barrier.
                            owner._session_map.clear_sid(key)
                            skipped = True
                            waiting_on.append(f"{key} (subagents)")
                            continue
                        # No child so far: commit under the registry lock.
                        async with owner._lock:
                            if owner._sessions.get(key) is not sess:
                                # A successor registered under the key while the
                                # read ran; not ours to retire.
                                if handler is not None:
                                    handler.release_teardown_fence(key, fence)
                                unresolved_fences.pop(key, None)
                                skipped = True
                                waiting_on.append(f"{key} (successor)")
                                continue
                            if sess.semaphore.locked():
                                # A turn took the semaphore during the off-loop read
                                # (a channel or cron path, not the dashboard gate).
                                # Retiring now would ``provider.shutdown()`` a live
                                # turn, so defer and let the next sweep catch it idle.
                                sess.retire_on_identity_change = True
                                if handler is not None:
                                    handler.release_teardown_fence(key, fence)
                                unresolved_fences.pop(key, None)
                                owner._session_map.clear_sid(key)
                                skipped = True
                                waiting_on.append(f"{key} (busy)")
                                continue
                            # In-memory work that appeared during the read defers:
                            # a child, a pending-work term, or a report that began
                            # delivering while the read was suspended.
                            if handler is not None and self._parent_owes_child_work_now(key):
                                sess.retire_on_identity_change = True
                                handler.release_teardown_fence(key, fence)
                                unresolved_fences.pop(key, None)
                                owner._session_map.clear_sid(key)
                                skipped = True
                                waiting_on.append(f"{key} (subagents)")
                                continue
                            # The final commit-vs-defer, ATOMIC against admission:
                            # under the fence lock, defer if the tentative fence
                            # recorded any child since it opened, else promote it to
                            # the committed teardown fence so a concurrent /api/spawn
                            # is carried into the cancel's accepted_since rather than
                            # cancelled. ``fence`` None (no store) commits.
                            committed = (
                                handler is None
                                or fence is None
                                or handler.commit_identity_sweep_retire(key, fence)
                            )
                            if not committed:
                                sess.retire_on_identity_change = True
                                if handler is not None:
                                    handler.release_teardown_fence(key, fence)
                                unresolved_fences.pop(key, None)
                                owner._session_map.clear_sid(key)
                                skipped = True
                                waiting_on.append(f"{key} (subagents)")
                                continue
                            del owner._sessions[key]
                            owner._advance_session_generation(key)
                            owner._compact_cooldown_until.pop(key, None)
                            self._suppress_replay.discard(key)
                            self._origin_links.pop(key, None)
                            self.state.stop_requests.pop(key, None)
                            self._discard_replay_gap(key)
                            self.state.orphaned_holders.pop(key, None)
                            # Selection only -- the committed fence is already open
                            # (promoted above); re-opening it would discard the
                            # concurrent admissions it carries.
                            if handler is not None:
                                teardown_children_by_key[key] = (
                                    handler.snapshot_teardown_children_only(key)
                                )
                            unresolved_fences.pop(key, None)
                            # Record the end HERE, under the same lock hold that
                            # removed the session -- not batched after the loop. A
                            # later candidate's off-loop store probe awaits, and a
                            # same-key successor can register through
                            # ``open_task_session`` during that await; a bulk record
                            # that ran after the await would consume the successor's
                            # live-start entry and delete its crash crumb. Recording
                            # under this key's lock, before any later probe can
                            # suspend the sweep, keeps it bound to this retirement.
                            await record_sessions_ended([key], end_reason=END_REASON_RETIRED)
                        owner._session_map.clear_sid(key)
                        # Teardown is deferred to the method-level finally, not this loop body,
                        # so a cancellation between here and the drain cannot skip
                        # it: the finally always runs and drains it shielded.
                        committed_teardown.append((key, sess.provider))
                finally:
                    # The committed retirements' provider teardown runs OUTSIDE
                    # this lock (and outside ``identity_sweep_lock``), in the
                    # method-level finally below -- a slow child shutdown must not
                    # hold the sweep lock, or a following account change cannot
                    # acquire it to recapture the fence. Here, under the lock, we
                    # only release any tentative fence this loop never resolved.
                    if handler is not None:
                        for key, fence in unresolved_fences.items():
                            handler.release_teardown_fence(key, fence)

    finally:
        # Runs after ``identity_sweep_lock`` is released (so a slow child
        # shutdown here never holds the lock against a following account
        # change) AND on cancellation (so the chat closing mid-sweep cannot
        # abandon an already-unregistered parent). Both the parents retired
        # synchronously under the registry lock (``doomed``: every idle
        # candidate, or only the stamp-dropped ones when ``spare_children`` is
        # set) and the spare candidates committed at the teardown
        # (``committed_teardown``) are drained here: each is already
        # unregistered, so leaving its children on the logged-out credential
        # is exactly what this prevents.
        #
        # A cancel landing on one entry's drain must not abandon the
        # entries after it: each is already unregistered and recorded
        # ended, so skipping it leaves exactly the children this finally
        # exists to end. Record the first ``CancelledError``, keep draining
        # every remaining entry (each step already shielded below), and
        # re-raise it after the loop so the cancel still propagates.
        pending_cancel: asyncio.CancelledError | None = None
        for key, provider in (*doomed, *committed_teardown):
            children = teardown_children_by_key.get(key, ())
            try:
                try:
                    await asyncio.shield(provider.shutdown())
                finally:
                    # See ``destroy``: a shutdown that raises must not skip
                    # the cancel. Shielded so a cancel of the sweep still
                    # ends the children and releases the runtime before the
                    # CancelledError propagates (we never swallow it).
                    await asyncio.shield(
                        self._cancel_parent_children(
                            key,
                            children,
                            verb="retire_kiro_identity_sessions",
                        )
                    )
                    await asyncio.shield(owner.release_subagent_runtime(key))
                retired.append(key)
            except asyncio.CancelledError as exc:
                # Remember it and carry on draining the rest; this entry's
                # own steps already completed under their shields above.
                if pending_cancel is None:
                    pending_cancel = exc
                retired.append(key)
            except Exception:
                self._deps.logger.warning(
                    "Failed to retire session %s after an identity change",
                    key,
                    exc_info=True,
                )
                skipped = True
                waiting_on.append(f"{key} (shutdown failed)")
        if pending_cancel is not None:
            raise pending_cancel

    # Warm-pool policy remains owned by the pool service; route through the
    # facade to retain direct manager monkeypatches and its fill-lock policy.
    if not await owner._retire_kiro_warm_pool():
        skipped = True
        waiting_on.append("warm pool")
    # Same spare for the companion runtimes: one that provably spawned
    # under the live account is neither reaped nor a reason to re-sweep.
    if not await owner._retire_kiro_subagent_runtimes(live=fingerprint):
        skipped = True
        waiting_on.append("sub-agent runtimes")
    if not await owner._retire_kiro_bg_runtime(live=fingerprint):
        skipped = True
        waiting_on.append("background runtime")
    if owner._starting_pids:
        # With every cold-start permit held above, residue here means a
        # producer bypassed the barrier; fail toward another sweep.
        skipped = True
        waiting_on.append("cold starts in flight")
    complete = not skipped
    if complete:
        # Only the sweep that OWNS the pending fingerprint may retire it. The
        # shutdowns above run outside ``identity_sweep_lock``, so a second
        # account change can acquire that lock meanwhile and record its own.
        # This sweep finishing its own work says nothing about that newer
        # change: clearing the marker would leave the caller free to advance
        # its baseline while the newer sweep's busy holders still serve turns
        # on the account it is retiring, with nothing left to retry from.
        if self.state.identity_sweep_fingerprint == fingerprint:
            self.state.identity_sweep_fingerprint = ""
        else:
            complete = False
            waiting_on.append("a newer identity change")
    self.state.identity_sweep_waiting_on = tuple(waiting_on)
    return retired, complete


async def _retire_kiro_subagent_runtimes(
    self: SessionLifecycleService,
    should_retire: Callable[[object], bool] | None = None,
    retired: list[str] | None = None,
    *,
    live: str = "",
) -> bool:
    """Retire idle Kiro-backed companion runtimes.

    This method is the module's ONLY companion-runtime retirement path
    outside the paired parent-end sites: it reaps exclusively IDLE
    runtimes, so it has no running child to end (the fact its ratchet
    exemption in ``test_every_parent_end_release_site_ends_its_children``
    rests on). Callers that need a narrower reap -- the spawn-identity
    stamp gate retires only PROVEN wrong-account runtimes -- pass
    ``should_retire`` rather than calling ``release_subagent_runtime``
    themselves, which would widen that exemption.

    ``should_retire`` filters which idle runtimes are reaped (``None``
    retires every kiro-backed one -- identity-sweep semantics). The
    sweep-quiescence post-conditions (spawn locks held, runtimes still
    registered) only apply to the unfiltered sweep, since under a filter
    the surviving runtimes are the expected outcome, not incompleteness.
    ``retired`` collects the parent keys actually released. ``live`` is
    the unfiltered sweep's spare: a REGISTERED runtime whose spawn stamp
    equals it (``spawned_under``) provably authenticated as the live
    account, so it is neither released nor counted by the post-conditions
    -- the same spare the session loop applies, without which a sweep on a
    host with any subagent in flight could never complete. Ignored under
    a filter, and not consulted by the parked-runtime reaper that runs
    first in the body: a parked runtime is a proven wrong-account holder
    and still drains. The release itself is pinned to the runtime this
    pass looked at (``expected=``): the sweep waits for the per-parent
    lock behind any respawn in flight, and that respawn installs a
    live-stamped replacement under the same key before letting go, which
    a pop by key alone would kill. A skipped release leaves the
    replacement to the post-condition, which spares it if live-stamped.

    Under a filter, a runtime the predicate proves wrong-account is
    DISPLACED rather than killed, busy and idle alike: popped from the
    claimable registry (its per-parent spawn lock is dropped with it, so
    a waiter retries against the live map and spawns a replacement under
    the live account) and parked on ``_draining_subagent_runtimes`` until
    its in-flight work finishes. Leaving it registered is one hole this
    closes -- the registry is what ``get_subagent_runtime`` hands to NEW
    demuxed sessions, so "busy, catch it later" kept the wrong-account
    process claimable for the whole drain. Killing an IDLE-looking one on
    the same pass is the other: the busy probe races a claim that was
    handed the runtime but has not yet opened its init scope, so the kill
    is always deferred to the drain reap at the top of this method, which
    fires only on a later pass, only once the park grace has elapsed, and
    only when the runtime still has no active or initializing sessions
    (probes fail toward busy, preserving work). The unfiltered sweep
    keeps its skip-busy contract untouched: its completeness signal is
    what drives retries, and its busy sessions are already flagged for
    next-turn retirement.
    """
    owner = self._owner
    logger = self._deps.logger
    complete = True
    # Reap parked displaced runtimes whose work has drained. Runs on
    # every caller (the per-turn gate and the sweep), mirroring the bg
    # drain reap at the top of _retire_kiro_bg_runtime. A runtime the
    # spawn-identity gate parked keeps a kill grace on top of the busy
    # probe: the probe can read a just-claimed runtime as idle for the
    # sub-second stretch before the claim opens its init scope, and the
    # grace outlasts that window (see identity_park_grace_remaining).
    now = self._deps.monotonic()
    # Iterate a snapshot but REMOVE entries individually: the kill below
    # awaits, and a concurrent turn can park a new displaced runtime on
    # the list during that suspension. Rebuilding the list from this
    # snapshot would overwrite that park -- a live child already popped
    # from _subagent_runtimes, referenced nowhere, invisible to close_all
    # and the PID shield alike. Removing only what this pass actually
    # killed leaves concurrent parks untouched.
    for parked in list(owner._draining_subagent_runtimes):
        if identity_park_grace_remaining(parked, now) > 0.0:
            continue
        try:
            busy = parked.is_alive() and parked.has_active_or_initializing_sessions()
        except Exception:
            busy = True  # fail toward preserving work
        if busy:
            continue
        try:
            await parked.kill(expected=True, reason="drained identity displacement teardown")
            logger.info("Reaped a drained displaced subagent runtime")
        except Exception:
            logger.warning(
                "Failed to reap a drained displaced subagent runtime; will retry",
                exc_info=True,
            )
            continue
        try:
            owner._draining_subagent_runtimes.remove(parked)
        except ValueError:
            pass
    for parent_key in list(owner._subagent_runtimes):
        runtime = owner._subagent_runtimes.get(parent_key)
        if runtime is None or not self._deps.provider_uses_kiro_identity_store(runtime):
            continue
        if should_retire is None and spawned_under(runtime, live):
            continue
        if should_retire is not None:
            if not should_retire(runtime):
                continue
            # Proven wrong-account: make it unclaimable NOW and drain it
            # in the park -- busy or idle alike. Killing on the same pass
            # that proved the mismatch is the race this closes: the idle
            # probe can read a runtime just handed to a new claim as idle
            # until the claim opens its init scope, so the kill always
            # waits for a LATER pass (plus the park grace) rather than
            # trusting one probe. Pop under the per-parent lock so an
            # in-flight get_subagent_runtime waiter re-checks the
            # canonical map instead of racing the displacement (mirrors
            # release_subagent_runtime's dance, minus the kill).
            lock = owner._subagent_runtime_locks.get(parent_key)
            if lock is not None:
                async with lock:
                    displaced = owner._subagent_runtimes.pop(parent_key, None)
                    owner._subagent_runtime_locks.pop(parent_key, None)
            else:
                displaced = owner._subagent_runtimes.pop(parent_key, None)
            if displaced is not None:
                mark_identity_parked(displaced, now)
                owner._draining_subagent_runtimes.append(displaced)
                if retired is not None:
                    retired.append(parent_key)
                logger.info(
                    "Parked the subagent runtime for %s to drain — "
                    "spawn identity mismatch displacement",
                    parent_key,
                )
            continue
        if runtime.has_active_or_initializing_sessions():
            complete = False
            continue
        try:
            released = await owner.release_subagent_runtime(parent_key, expected=runtime)
            if released and retired is not None:
                retired.append(parent_key)
        except Exception:
            logger.warning(
                "Failed to retire subagent runtime for %s after an identity change",
                parent_key,
                exc_info=True,
            )
            complete = False
    if should_retire is None:
        if any(lock.locked() for lock in owner._subagent_runtime_locks.values()):
            complete = False
        # This post-condition catches a runtime installed after the snapshot but
        # before its per-parent spawn lock was released. A spared
        # live-account runtime is the expected survivor, not a leftover.
        if any(
            runtime is not None
            and self._deps.provider_uses_kiro_identity_store(runtime)
            and not spawned_under(runtime, live)
            for runtime in list(owner._subagent_runtimes.values())
        ):
            complete = False
        # A parked displaced runtime is still a live process authenticated
        # under the old account; the sweep is not done until it drains.
        if any(
            self._deps.provider_uses_kiro_identity_store(runtime)
            and not spawned_under(runtime, live)
            for runtime in owner._draining_subagent_runtimes
        ):
            complete = False
    return complete


async def _retire_kiro_bg_runtime(
    self: SessionLifecycleService,
    should_retire: Callable[[object], bool] | None = None,
    retired: list[str] | None = None,
    reason: str = "deliberate logout teardown",
    *,
    live: str = "",
) -> bool:
    """Retire the idle Kiro-backed background runtime and drained holders.

    ``should_retire`` filters the reap the same way as
    :meth:`_retire_kiro_subagent_runtimes` (``None`` retires
    unconditionally -- identity-sweep semantics); ``retired`` collects
    ``"background-runtime"`` when the kill lands; ``reason`` labels the
    kill for the process record; ``live`` is the unfiltered sweep's spare
    (``spawned_under``), as on the companion reaper.
    """
    owner = self._owner
    logger = self._deps.logger
    async with owner._bg_runtime_lock:
        await owner._reap_drained_bg_runtimes_locked()
        complete = not any(
            self._deps.provider_uses_kiro_identity_store(runtime)
            and not spawned_under(runtime, live)
            for runtime in owner._draining_bg_runtimes
        )
        runtime = owner._bg_runtime
        if runtime is None or not self._deps.provider_uses_kiro_identity_store(runtime):
            return complete
        if should_retire is None and spawned_under(runtime, live):
            return complete
        if should_retire is not None and not should_retire(runtime):
            return complete
        if should_retire is not None:
            # Proven wrong-account: free the slot NOW so the next
            # acquisition spawns under the live account, and park the
            # runtime on the existing drain list -- busy or idle alike.
            # Skipping a busy one is the hole this closes (``_bg_runtime``
            # is the claimable slot, so "busy, catch it later" kept the
            # wrong-account process serving NEW background sessions for
            # the whole drain), and killing an idle-looking one on this
            # same pass is the race it avoids: the busy probe can read a
            # runtime just handed to a ``get_bg_session`` claim as idle
            # until the claim opens its init scope, so the kill is
            # deferred to the drain reap, which fires on a later pass
            # once the park grace has elapsed.
            mark_identity_parked(runtime, self._deps.monotonic())
            await owner._detach_bg_runtime_locked(
                runtime, "spawn identity mismatch displacement", park_only=True
            )
            if retired is not None:
                retired.append("background-runtime")
            return False
        if runtime.has_active_or_initializing_sessions():
            return False
        try:
            await runtime.kill(expected=True, reason=reason)
        except Exception:
            logger.warning(
                "Failed to retire the background runtime after an identity change",
                exc_info=True,
            )
            return False
        # Clear only after kill succeeds; otherwise retain the live-process
        # reference for the next retirement attempt.
        owner._bg_runtime = None
        if retired is not None:
            retired.append("background-runtime")
        logger.info("Retired the background runtime started under the previous account")
        return complete
