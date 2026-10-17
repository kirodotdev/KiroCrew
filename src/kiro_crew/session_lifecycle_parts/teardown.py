"""The per-key teardown verbs: ``reset``, ``remove``, ``remove_if_unclaimed``,
``destroy`` / ``destroy_if`` and ``discard_conversation``.

Each pops the session and forgets the key's per-key state in one registry lock hold,
records the session's end there, and then shuts the provider down, ends the parent's
sub-agent runs and releases its companion runtime. They differ in what survives: ``reset``
recycles the process and keeps the conversation and its queued follow-ups, ``remove``
keeps the session-map entry, ``discard_conversation`` drops only the native sid, and
``destroy`` deletes the map entry. A new way to end one key's session goes here.

Instance fields written: none.

Instance fields read: ``_deps``, ``_owner``, ``state``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging import turn_ceiling
from kiro_crew.metrics.sessions import (
    END_REASON_DESTROYED,
    END_REASON_DISCARDED,
    END_REASON_REMOVED,
    END_REASON_RESET,
    END_REASON_UNCLAIMED,
    record_session_ended,
)
from kiro_crew.session_lifecycle_parts.parked_queue import _park_queue, allocation_identity

if TYPE_CHECKING:
    from kiro_crew.session_lifecycle import SessionLifecycleService, _SessionEntry, _TeardownScope


#: The teardown reason a destroy records when it could NOT establish that the ACP
#: id is globally revoked. Retention authorizes deleting a unit's history only on
#: ``destroyed``, which means "revocation completed", so this word records the same
#: teardown while leaving the log in place. The map delete in ``destroy`` removes
#: exactly one key, and two keys can legitimately point at one sid -- importing a
#: transferred session twice allocates a new slot key each time and leaves the
#: source intact -- so the tidier record is the wrong one to claim.
_END_REASON_SID_RETAINED = "destroyed_sid_retained"


#: Stands in for "a key may still map to this sid, but the map could not be read".
#: Not a real key, and never logged as one: it only has to be non-None so the claim
#: is withheld, because an unreadable map is not evidence of revocation.
_SID_RETENTION_UNKNOWN = "<unreadable session map>"


_ANY_SESSION = object()


def _turn_in_flight(session: Any, *, refuse_only_on_active_turn: bool = False) -> bool:
    """Whether *session* is busy, as this caller's ``skip_if_busy`` means it.

    A held lease is the default answer, and the stricter one: it also covers a turn that has
    acquired but put no prompt in flight yet, which ``has_active_turn`` cannot see, and it is
    what a background sweep needs. A channel member holds its lease for the whole listening
    lifetime and CACHES the provider it was handed, so a sweep that tore that provider down
    would leave every later message driving a dead one with nothing to re-fetch it.

    A caller acting on an explicit user request passes ``refuse_only_on_active_turn`` and gets
    the narrower question instead: refusing a lifecycle holder on the lease alone would refuse
    it for as long as it exists, so the retry-when-idle such a caller offers could never
    succeed.
    """
    if session is None or not session.semaphore.locked():
        return False
    if not refuse_only_on_active_turn or not getattr(session, "lifecycle_lease", False):
        return True
    # The holder's own answer comes first: its turn begins when it dequeues a message, and the
    # setup before the prompt goes out is a window ``has_active_turn`` reports as idle.
    if getattr(session, "lifecycle_turn_active", False):
        return True
    provider = getattr(session, "provider", None)
    has_active_turn = getattr(provider, "has_active_turn", None)
    # An unknown provider shape keeps the strict answer: refusing a teardown is recoverable,
    # tearing down a streaming reply is not.
    return bool(has_active_turn()) if callable(has_active_turn) else True


async def reset(
    self: SessionLifecycleService,
    key: str,
    *,
    expect_session: _SessionEntry | None = None,
    skip_if_busy: bool = False,
    skip_if_injecting: bool = False,
    refuse_only_on_active_turn: bool = False,
    clear_conversation: bool = False,
    ends_conversation: bool = False,
    scope: _TeardownScope | None = None,
) -> bool:
    """Kill a live session while preserving the exact reset semantics.

    Defaults to recycling a PROCESS while the conversation survives. The session-map
    entry keeps its resume sid, so the next turn on the key restores the same native
    conversation through ``session/load`` — which is why this is the verb every
    evict-and-retry path reaches for: a wedged prompt, a failed auto-compaction, a
    provider or model switch, an idle expiry, the channel watchdog, a per-step
    re-prompt. Those must NOT stop this parent's sub-agent runs: the child has a
    conversation to deliver into and is bounded by its own run timeout, so stopping it
    would discard live work belonging to a conversation that is coming back.

    ``scope`` is the caller's hold on the ``tearing_down`` entry this reset records
    at its pop (:class:`_TeardownScope`): the popped session stays readable through
    :meth:`tearing_down` until the scope is released, which the facade does when this
    call ends however it ends. Without a scope nothing is recorded, and the popped
    session is unreachable from the pop on -- a reader that arrives after it (the
    cron reaper, when the run's own finally reset popped the session and hung)
    then has no handle to the process this teardown holds.

    ``ends_conversation=True`` says the caller is ending the conversation, not
    recycling it, and then this parent's runs are stopped like any other parent end.
    Most endings are a different verb (``remove``, ``destroy``,
    ``discard_conversation``, ``remove_if_unclaimed``,
    ``retire_kiro_identity_sessions``), but some reach only this one, so the intent
    has to be sayable here. The callers that pass it are named in
    ``docs/system-specs/modules/session.md``; a test pins that they still do.

    The default is the recycle because that is what the overwhelming majority of the
    ~46 callers are, and the cost of the two mistakes is not symmetric -- but a MISSED
    flag costs MORE than an orphan, which is the number a new caller has to weigh. It
    arms no delivery gate either, so the child's report still reaches ``_on_done``,
    which resolves the parent through ``get_or_create`` -- the call that CREATES a
    session when none is live -- so the conversation the caller ended re-opens, seeded
    with that report. That is the headline defect in full, not a bounded process. A
    wrong ``True`` destroys live work for a conversation that comes right back, which
    is why the default stays the recycle. Nothing structural catches an omission,
    because a keyword is invisible to the AST ratchet, which is exactly why
    ``test_the_conversation_ending_reset_callers_say_so`` pins the callers BY PATH.

    ``refuse_only_on_active_turn`` narrows ``skip_if_busy`` to a DECLARED turn, which a
    caller acting on an explicit user request needs: a channel member holds its lease for
    its whole listening life, so refusing on the lease alone refuses that caller forever
    and the retry-when-idle it offers can never succeed.
    """
    owner = self._owner
    logger = self._deps.logger
    key = owner._fold_key(key)
    async with owner._lock:
        current = owner._sessions.get(key)
        if expect_session is not None and current is not expect_session:
            return False
        if skip_if_busy and _turn_in_flight(
            current, refuse_only_on_active_turn=refuse_only_on_active_turn
        ):
            return False
        # A completion injection commits a turn to this session BEFORE it
        # acquires the semaphore, so the check above cannot see one. A caller
        # that asks the counter itself still races this lock: acquiring it
        # suspends, and an injection beginning in that gap is invisible to
        # any read taken earlier. Asking again HERE is what makes the answer
        # atomic with the pop, which is why identity and the semaphore are
        # re-validated under this lock too. Opt-in, so a user-initiated reset
        # still wins over an injection. Fail closed and locally, so an
        # unreadable counter declines this reset rather than raising through
        # a sweep that has other candidates to visit.
        if skip_if_injecting:
            try:
                injecting = owner._has_pending_injection(key)
            except Exception:
                logger.debug(
                    "Injection probe failed for session %s; keeping it",
                    key,
                    exc_info=True,
                )
                injecting = True
            if injecting:
                return False
        # Recorded in the same lock hold as the pop and before it (``current`` is
        # what the pop removes: no await separates the two reads): from the pop
        # to the end of this method the live map does not name this session,
        # and every await below (the end record, the unlink, the child probes,
        # the provider shutdown) is where a teardown hangs. The scope keeps the
        # popped session readable through ``tearing_down`` for exactly the life
        # of this call -- the facade releases it when this method ends, however
        # it ends -- and tells the caller which session THIS reset popped: its
        # ``on_pop(session)`` hook, run here so a caller's read of the
        # session is atomic with the pop. One entry per teardown in flight
        # under the key, in pop order; no scope, no record.
        if scope is not None and current is not None:
            scope.note_pop(key, current)
        session = owner._sessions.pop(key, None)
        # Snapshotted in the SAME lock hold as the pop, and only when the caller says
        # the conversation is ending: every await below is a window a cold start can
        # register a successor under this key in, and a selection made after one would
        # name the successor's runs. A recycle takes no snapshot at all -- its children
        # keep running and deliver into the resumed conversation.
        teardown_children = self._snapshot_parent_children(key) if ends_conversation else ()
        owner._advance_session_generation(key)
        owner._compact_cooldown_until.pop(key, None)
        # ``clear_conversation`` means the conversation is being THROWN AWAY,
        # and the successor's replay is the one thing that can put it back.
        # Arming suppression here is what stops the reset that critical
        # context escalates to (``_reset_still_critical``, this flag's only
        # ``clear_conversation=True`` caller) from becoming a loop: the
        # successor cold-starts, ``build_session_replay`` re-injects the whole
        # conversation log as ONE prompt, a single prompt is not something
        # ``/compact`` can shrink, so the reading stays above
        # ``_POST_COMPACT_RESET_PCT`` and escalates again -- forever, since
        # the pop above also takes the cooldown that would have damped it.
        # A discard here is right for every OTHER reset (an idle expiry, a
        # provider switch, the watchdog): those keep the conversation, so
        # replaying it is the behaviour that preserves it. The manual
        # ``discard_conversation(replay=False)`` path already arms the flag
        # for exactly this reason; this branch is the one clear that forgot.
        # Gated on ``session is not None`` for the same reason the
        # ``clear_sid`` call below is: with nothing to clear there is no
        # conversation to throw away, so arming the flag would only make the
        # next cold start on an unrelated key amnesiac.
        if clear_conversation and session is not None:
            self._suppress_replay.add(key)
        else:
            self._suppress_replay.discard(key)
        owner._compact_pending_verdict.pop(key, None)
        self._origin_links.pop(key, None)
        # Same lock hold as the pop, so a successor that registers in any of
        # the awaits below already finds these entries parked and adopts them.
        rescued = (
            self._park_reset_queue(key, session)
            if session is not None and not ends_conversation
            else 0
        )
        if session is not None:
            # Order matters: the holder is recorded BEFORE its waiters are
            # woken, so the successor a woken waiter creates is already
            # shielded from the holder's late release.
            self._orphan_turn_holder(key, session)
            self._wake_turn_waiters(session)
            # Same event-loop tick as the pop, for the reason the clear_sid
            # call below documents: the awaits further down let a racing cold
            # start register a SUCCESSOR under this key, and recording the
            # end after them would consume the successor's start instead of
            # this session's. The pop and the sample happen before this call's
            # own suspension point, so that ordering still holds; only the
            # crumb unlink is deferred to a worker.
            # Append-only the session's log (flag-gated, fail-soft). Reset is
            # the teardown that ends a crew log's life, since the successor
            # cold-starts a new ACP session id. Written BEFORE the await
            # below: the emitter hands the entry to its own thread and
            # returns, so this adds no suspension point, while writing it
            # after would let a live turn's entries take a lower seq than the
            # teardown that already happened. Entries from turns that were
            # in flight still follow it -- see `on_session_closed` -- but the
            # teardown's own position stays where the decision was made.
            # Deferred, not module-scope: this module is reached from the gateway
            # boot path, and AUTOSDE's no-new-work-on-gateway-boot-path rule asks
            # for a flag-gated subsystem's IMPORT to be gated, not just its use.
            from kiro_crew.crew_log import emit as crew_log_emit

            crew_log_emit.on_session_closed(
                crew_log_emit.session_id_of(session.provider),
                END_REASON_RESET,
            )
            await record_session_ended(key, end_reason=END_REASON_RESET)
    if clear_conversation and session is not None:
        # The registry lock, not an absence of suspension points, is what makes
        # this safe: the end record above awaits, but it awaits while this
        # coroutine still holds ``owner._lock`` -- the lock a racing cold start
        # must take to register and map a SUCCESSOR -- so no successor SID can
        # be published across that suspension. Releasing an ``asyncio.Lock``
        # wakes its waiter without yielding, so control reaches this line before
        # any of them runs, and this clear cannot erase a successor's pointer.
        owner._session_map.clear_sid(key)
    shutdown_error: BaseException | None = None
    if session:
        await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
        # Capture PID and child tree before shutdown clears them.
        client = getattr(session.provider, "_client", None)
        raw_pid = getattr(client, "_pid", None) if client else None
        if raw_pid is None:
            cc_proc = getattr(session.provider, "_proc", None)
            if cc_proc is not None and cc_proc.returncode is None:
                raw_pid = cc_proc.pid
        if raw_pid is None:
            cc_proc = getattr(session.provider, "_active_proc", None)
            if cc_proc is not None and cc_proc.returncode is None:
                raw_pid = cc_proc.pid
        pid = raw_pid if isinstance(raw_pid, int) else None
        raw_children = getattr(client, "_child_pids", None) if client else None
        child_pids: dict[Any, Any] = dict(raw_children) if isinstance(raw_children, dict) else {}
        capture_child_records, get_child_pids, kill_escaped_children = (
            self._deps.get_child_process_helpers()
        )

        if pid:
            # Snapshot descendants before shutdown; record capture retains
            # process start times so a recycled PID is never killed later.
            loop = asyncio.get_running_loop()
            fresh = await loop.run_in_executor(
                self._deps.get_subprocess_executor(),
                get_child_pids,
                pid,
            )
            new_pids = [candidate for candidate in fresh if candidate not in child_pids]
            if new_pids:
                child_pids.update(
                    await loop.run_in_executor(
                        self._deps.get_subprocess_executor(),
                        capture_child_records,
                        new_pids,
                    )
                )
        try:
            await session.provider.shutdown()
        except BaseException as exc:  # noqa: BLE001 - re-raised below, after the cancel
            # DEFERRED rather than propagated, and re-raised at the end of the method.
            # The cancel that ends this parent's children lives past the bottom of this
            # block, so an exception leaving here skipped it and left children running
            # with no parent to report to -- the outcome this verb exists to prevent,
            # on the one path nobody exercises. A ``finally`` around the whole block
            # would say the same thing; deferring says it without re-indenting two
            # hundred lines of kill-and-sweep logic, which is its own risk.
            shutdown_error = exc
        platform_compat = self._deps.get_platform_compat()
        if pid:
            if platform_compat.pid_exists(pid):
                logger.warning("Reset %s: PID %d survived shutdown, force-killing", key, pid)
                try:
                    await platform_compat.kill_process_tree_async(
                        pid,
                        platform_compat.SIGKILL,
                    )
                except (ProcessLookupError, OSError):
                    try:
                        await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
            if child_pids:
                try:
                    sweep_loop = asyncio.get_running_loop()
                    await sweep_loop.run_in_executor(
                        self._deps.get_subprocess_executor(),
                        kill_escaped_children,
                        child_pids,
                    )
                except Exception:
                    logger.exception("Reset %s: child sweep failed", key)
        logger.debug("Reset session: %s (pid=%s)", key, pid)
    # BEFORE the runtime release below, and outside the ``if session`` block above.
    #
    # Outside, because a live provider is not what makes this an ending: a RECYCLING
    # reset pops the session, so an ENDING reset that follows one arrives with
    # ``session is None`` while the children are still running -- the shape that
    # skipped the cancel in ``remove``. Gated on the INTENT rather than on the id set
    # being empty, because an empty set still reaches the durable-row sweep and a
    # recycle must not touch the store rows of a conversation that will resume.
    #
    # Before, because that is the order every other verb keeps and the helper's own
    # docstring requires: a child is stopped through its own teardown rather than by
    # having the runtime it is multiplexed onto pulled out from under a live turn.
    if ends_conversation:
        await self._cancel_parent_children(key, teardown_children, verb="reset")
    if session is not None and key in owner._subagent_runtimes:
        try:
            await owner.release_subagent_runtime(key)
        except Exception:
            logger.debug("Reset %s: subagent runtime cleanup failed", key, exc_info=True)
    if rescued and session is not None:
        # Nothing else starts a successor for a key whose turn just failed: the
        # next message might never come, and the end-of-turn drain of the failed
        # turn finds no live session. The respawn registers one, which adopts
        # the parked entries, and its release wakes their channels' drains.
        # It starts under the popped session's own identity: a later claim
        # takes a live session as-is, so a default-agent successor would run
        # every follow-up, and every message after them, as the wrong agent.
        task = asyncio.create_task(self._respawn_as(key, allocation_identity(owner, key, session)))
        owner._background_tasks.add(task)
        task.add_done_callback(owner._background_tasks.discard)
    if shutdown_error is not None:
        # The caller still sees what went wrong; it just sees it after this parent's
        # children have been dealt with rather than instead of that.
        raise shutdown_error
    return session is not None


def _park_reset_queue(self: SessionLifecycleService, key: str, session: _SessionEntry) -> int:
    """Park the popped *session*'s live queue entries for the key's successor.

    A recycling reset ends a process, not a conversation, and the queue holds
    follow-ups people sent while the turn ran: acknowledged messages nobody
    asked to drop. Entries a mid-turn cancel marked in ``cancelled`` stay on
    the popped queue, so the teardown's unlink removes their files as before.
    Parking is bounded (``PARKED_QUEUE_MAX``); what does not fit is dropped,
    counted and logged by ``_park_queue``. Returns how many were parked.
    """
    cancelled = session.cancelled
    kept = tuple(entry for entry in session.queue if entry[0] not in cancelled)
    if not kept:
        return 0
    wanted = {id(entry) for entry in kept}
    remaining = [entry for entry in session.queue if id(entry) not in wanted]
    session.queue.clear()
    session.queue.extend(remaining)
    _park_queue(self._owner, key, kept)
    return len(kept)


def _release_turn_ceiling(self: SessionLifecycleService, folded: str, requested: str) -> None:
    """Forget a channel conversation's turn-ceiling latch, beside the other
    per-key state a reset verb forgets.

    A channel conversation that reached its turn ceiling is latched under its
    key, and the key survives every one of these verbs -- channel linkage is
    retained by design. The refusal text tells the user to reset the
    conversation, so each verb the product calls a reset has to be what
    clears it: otherwise the only thing that releases a latched conversation
    is a gateway restart and the notice names a remedy that does nothing.
    Slack's ``!agent default`` answers "Reset to default agent" and reaches
    ``remove``, so a reset on one verb and not another is the same trap with a
    different spelling.

    BOTH spellings, because the ceiling counts under the key the CHANNEL holds
    while ``_fold_key`` resolves an alias onto the live key, so the two can
    differ and clearing only the folded one would leave the latch standing
    under the channel's own spelling.
    """
    ceiling = turn_ceiling.shared_ceiling()
    ceiling.reset(folded)
    if requested != folded:
        ceiling.reset(requested)


async def remove(self: SessionLifecycleService, key: str) -> None:
    """Shut down a session while preserving its session-map entry."""
    owner = self._owner
    requested_key = key
    key = owner._fold_key(key)
    async with owner._lock:
        session = owner._sessions.pop(key, None)
        # Snapshot the runs this key owns in the SAME lock hold as the pop: every
        # await below is a window a cold start can register a successor under
        # this key in, and a selection made after one would name the
        # successor's runs. The cancel itself happens after the teardown.
        teardown_children = self._snapshot_parent_children(key)
        owner._advance_session_generation(key)
        owner._compact_cooldown_until.pop(key, None)
        self._suppress_replay.discard(key)
        owner._compact_pending_verdict.pop(key, None)
        self._origin_links.pop(key, None)
        self.state.stop_requests.pop(key, None)
        self._discard_replay_gap(key)
        self.state.orphaned_holders.pop(key, None)
        self._release_turn_ceiling(key, requested_key)
        if session is not None:
            # Same tick as the pop: see reset for why recording after the
            # teardown awaits would consume a successor's start.
            await record_session_ended(key, end_reason=END_REASON_REMOVED)
    # OUTSIDE the ``if session`` guard, because a live provider is not what makes this
    # a parent end. A reset pops the session and keeps the conversation, so the tab
    # close that follows arrives with ``session is None`` while the children are still
    # running -- and this is the call that ends them. Guarding on the provider skipped
    # exactly the sequence the two verbs make ordinary. It is a no-op when the snapshot
    # is empty, which is what a key this gateway never held produces.
    await self._cancel_parent_children(key, teardown_children, verb="remove")
    if session:
        await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
        await session.provider.shutdown()
        # INSIDE the guard, unlike the cancel above, and the asymmetry is this verb's
        # own contract: ``remove`` on a key it never held must touch nothing
        # (`test_missing_key_teardown_persistence_matrix[remove]` pins zero
        # `clear_sid`, zero `delete`, zero release). ``destroy`` and
        # ``discard_conversation`` release unconditionally because they answer a
        # different question -- they are told to forget the key, whether or not a
        # process is behind it. Cancelling children is safe unconditionally because
        # the snapshot answers empty for an unheld key; releasing is not, because it
        # is the observable that contract names.
        await owner.release_subagent_runtime(key)
        self._deps.logger.info("Removed session (map preserved): %s", key)


async def remove_if_unclaimed(self: SessionLifecycleService, key: str) -> bool:
    """Remove a speculative session only while its first turn is unclaimed
    and it owes no sub-agent work.

    A free semaphore proves only that the parent's own turn is over: children
    it spawned keep running on its runtime, and a finished child still owes
    the parent its report. The decision follows the identity sweep's
    spare-children shape. A tentative teardown fence opens under the lock,
    the off-loop read asks the manager's single pending-work signal
    (``has_pending_work_for_async``) plus the fence-aware store read, and the
    commit re-asks the in-memory half under the lock before promoting the
    fence. A report still running for a child whose task is gone
    (``has_unfinished_report_for``) is asked at both points, because the
    pending-work signal stops seeing a run once its task ends. Any child, an
    unfinished report, a failed read, or a pending injection keeps the parent.
    """
    owner = self._owner
    constants = self._deps.constants()
    requested_key = key
    key = owner._fold_key(key)
    handler = self._child_teardown
    async with owner._lock:
        session = owner._sessions.get(key)
        if (
            session is None
            or session.first_turn is constants.first_turn_nothing_armed
            or session.semaphore.locked()
        ):
            return False
        fence = self._open_teardown_fence(key)
    committed = False
    try:
        if handler is not None:
            try:
                if await self._parent_owes_child_work(key):
                    return False
            except Exception:
                self._deps.logger.warning(
                    "Unclaimed removal: reading the sub-agents of %s failed; keeping it",
                    key,
                    exc_info=True,
                )
                return False
        async with owner._lock:
            if (
                owner._sessions.get(key) is not session
                or session.first_turn is constants.first_turn_nothing_armed
                or session.semaphore.locked()
                or owner._has_pending_injection(key)
            ):
                return False
            if handler is not None:
                if self._parent_owes_child_work_now(key):
                    return False
                if fence is not None and not handler.commit_identity_sweep_retire(key, fence):
                    return False
            committed = True
            del owner._sessions[key]
            # Select the runs this key owns in the SAME lock hold as the pop: every
            # await below is a window a cold start can register a successor under
            # this key in, and a selection made after one would name the
            # successor's runs. The cancel itself happens after the teardown. A
            # promoted fence is already the committed one, so only the selection
            # half runs; re-opening it would drop the admissions it carries.
            if handler is not None and fence is not None:
                teardown_children = tuple(handler.snapshot_teardown_children_only(key))
            else:
                teardown_children = self._snapshot_parent_children(key)
            owner._advance_session_generation(key)
            owner._compact_cooldown_until.pop(key, None)
            self._suppress_replay.discard(key)
            owner._compact_pending_verdict.pop(key, None)
            self._origin_links.pop(key, None)
            self.state.stop_requests.pop(key, None)
            self._discard_replay_gap(key)
            self.state.orphaned_holders.pop(key, None)
            self._release_turn_ceiling(key, requested_key)
            # Same tick as the removal: see reset.
            await record_session_ended(key, end_reason=END_REASON_UNCLAIMED)
    finally:
        # A kept parent (or a cancellation mid-read) runs no cancel sweep, so its
        # tentative fence has no consumer; a commit promoted it instead.
        if not committed and handler is not None and fence is not None:
            handler.release_teardown_fence(key, fence)
    await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
    try:
        await session.provider.shutdown()
    finally:
        # See ``destroy``: the entry is already gone, so a shutdown that raises must
        # not carry the exception past the cancel and leave orphaned children behind.
        await self._cancel_parent_children(key, teardown_children, verb="remove_if_unclaimed")
        await owner.release_subagent_runtime(key)
    self._deps.logger.info(
        "Removed unclaimed speculative session (map preserved): %s",
        key,
    )
    return True


async def destroy(
    self: SessionLifecycleService,
    key: str,
    *,
    should_destroy: Callable[[], bool] | None = None,
    expect_generation: object = _ANY_SESSION,
    skip_if_busy: bool = False,
    preserve_autocompact_override: bool = False,
) -> bool:
    """Destroy *key* only while every synchronous under-lock guard allows it."""
    owner = self._owner
    constants = self._deps.constants()
    requested_key = key
    async with owner._lock:
        key = owner._fold_key(key)
        if expect_generation is not _ANY_SESSION and owner._has_allocation_reservation(key):
            return False
        current = owner._sessions.get(key)
        if (
            expect_generation is not _ANY_SESSION
            and owner.session_generation(key) != expect_generation
        ):
            return False
        if skip_if_busy and current is not None and current.semaphore.locked():
            return False
        if should_destroy is not None:
            try:
                allowed = should_destroy()
            except Exception:
                self._deps.logger.warning(
                    "Conditional session destroy guard failed for %s; skipping",
                    key,
                    exc_info=True,
                )
                return False
            if not allowed:
                return False
        session = owner._sessions.pop(key, None)
        # Snapshot the runs this key owns in the SAME lock hold as the pop: every
        # await below is a window a cold start can register a successor under
        # this key in, and a selection made after one would name the
        # successor's runs. The cancel itself happens after the teardown.
        teardown_children = self._snapshot_parent_children(key)
        owner._advance_session_generation(key)
        owner._compact_cooldown_until.pop(key, None)
        self._suppress_replay.discard(key)
        owner._compact_pending_verdict.pop(key, None)
        self.state.stop_requests.pop(key, None)
        self._discard_replay_gap(key)
        self.state.orphaned_holders.pop(key, None)
        self._release_turn_ceiling(key, requested_key)
        # Ordinary permanent destroy starts a new conversation on reuse and
        # therefore clears the old threshold. History deletion can race a
        # same-key transcript claim in another process, so its explicit
        # conditional mode preserves this independently owned sidecar.
        if not preserve_autocompact_override:
            owner.set_autocompact_pct(key, None)
        # _origin_links deliberately survives destroy; existing callers
        # rely on the historical asymmetry with reset/remove.
        # The map delete is the destructive persistence linearization point.
        # It must run before record_session_ended can suspend: dashboard slot
        # publication does not take this registry lock and could otherwise
        # adopt the predecessor's still-visible binding during that await.
        owner._session_map.delete(
            key,
            reason=constants.unbind_reason_session_destroyed,
        )
        if session is not None:
            # Append-only the session's log (flag-gated, fail-soft). Destroy is a
            # teardown and has to record itself, for the same reason the reset
            # route above does -- and for one more: without this entry the
            # unit is never collected by EITHER half of retention. The emitter
            # holds this session's cached handle, and the write lease that
            # handle carries, so a removal claiming the lease `sole` answers
            # `owned`; the sweep is blocked from the other side, because a unit
            # whose newest lifecycle entry is not a close reads as OPEN
            # whatever its age. One missing entry, both paths defeated.
            #
            # The REASON asserts how far the teardown got, and retention treats
            # only `destroyed` as authorization to delete the unit's history.
            # That word means the ACP id is globally revoked, so it is claimed
            # only after checking that no OTHER key still maps to this sid: the
            # map delete above removes exactly ONE key, and a second key
            # pointing at the same sid still resolves it, which keeps the
            # conversation resumable and its log needed. Two keys on one sid is
            # a state the system itself produces -- importing a transferred
            # session twice allocates a new slot key each time and deliberately
            # leaves the source intact (see dashboard/session_transfer.py) --
            # so this is not only an adversarial shape and the other holder must
            # not be revoked to make this record tidy.
            #
            # Reading the map to WITHHOLD the claim is safe in a way that
            # reading it to grant one is not: a forged or emptied map can only
            # make this assert less than the truth, never more, and the sweep
            # itself still reads nothing but the ledger.
            #
            # Placed here, before the await below, for the reason the reset
            # site documents: the emitter hands the entry to its own thread and
            # returns, so this adds no suspension point, while writing it after
            # the await would let a live turn's entries take a lower seq than
            # the teardown that already happened. Entries from turns that were
            # in flight still follow it by design -- see `on_session_closed`.
            #
            # It cannot start a crew log for a session that has none: `_handle`
            # never creates one, so a close for an unopened session writes
            # nothing rather than leaving a header behind for a conversation
            # that is being destroyed.
            #
            # Deferred, not module-scope: this module is reached from the
            # gateway boot path, and AUTOSDE's no-new-work-on-gateway-boot-path
            # rule asks for a flag-gated subsystem's IMPORT to be gated too.
            from kiro_crew.crew_log import emit as crew_log_emit

            crew_log_sid = crew_log_emit.session_id_of(session.provider)
            retained_key: str | None = None
            if crew_log_sid:
                try:
                    retained_key = owner._session_map.find_key_by_sid(crew_log_sid)
                except Exception:
                    # An unreadable map is not evidence that the id is revoked.
                    retained_key = _SID_RETENTION_UNKNOWN
            if retained_key is not None:
                self._deps.logger.info(
                    "Session destroy: %s still maps to session id of %s; recording a "
                    "non-terminal teardown so its crew log is retained",
                    retained_key,
                    key,
                )
            crew_log_emit.on_session_closed(
                crew_log_sid,
                END_REASON_DESTROYED if retained_key is None else _END_REASON_SID_RETAINED,
            )
            # Still under the same lock as the pop; a manager successor cannot
            # register until its predecessor's end record is sampled.
            await record_session_ended(key, end_reason=END_REASON_DESTROYED)
    try:
        if session:
            await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
            await session.provider.shutdown()
    finally:
        # In the FINALLY, because the parent has already been removed by the time the
        # provider is asked to stop. A shutdown that raises must not carry the
        # exception past both of these: that leaves children running with no parent to
        # report to and the runtime they share still held -- the one outcome this verb
        # exists to prevent, reached by the one path nobody exercises. Cancel first,
        # then release: a child is stopped through its own teardown rather than by
        # having the runtime pulled out from under a live turn.
        await self._cancel_parent_children(key, teardown_children, verb="destroy")
        await owner.release_subagent_runtime(key)
        self._deps.logger.info("Destroyed session (map deleted): %s", key)
    return True


async def destroy_if(
    self: SessionLifecycleService,
    key: str,
    expected_generation: int,
    should_destroy: Callable[[], bool],
    *,
    preserve_autocompact_override: bool = False,
) -> bool:
    """Destroy the captured idle generation if its slot guard stays true."""
    return await self.destroy(
        key,
        should_destroy=should_destroy,
        expect_generation=expected_generation,
        skip_if_busy=True,
        preserve_autocompact_override=preserve_autocompact_override,
    )


async def discard_conversation(
    self: SessionLifecycleService,
    key: str,
    *,
    replay: bool = True,
    skip_if_busy: bool = False,
    refuse_only_on_active_turn: bool = False,
) -> bool:
    """Drop only the native conversation while preserving channel linkage.

    Returns whether a session was actually torn down. False means
    ``skip_if_busy`` made it a no-op; nothing was changed, including the
    replay flag and the session map.

    ``skip_if_busy`` refuses the teardown when the session has a turn in
    flight, and is enforced HERE, atomically with the pop, for the same
    reason :meth:`reset` enforces its own: a caller that probes busy-ness
    first and calls this second leaves a window between the two in which a
    turn can be admitted — a channel message acquiring the session's
    semaphore, say — and the teardown then removes the provider from under a
    reply that has started. The probe is the SEMAPHORE rather than
    ``provider.has_active_turn()``, which is deliberately stricter: a turn
    that holds the semaphore but has not yet put a prompt in flight is
    invisible to ``has_active_turn`` and is exactly the case a caller-side
    pre-check cannot close.

    The sid clear runs in the SAME event-loop tick as the pop, with no await
    between them, so a cold start racing this teardown cannot have mapped a
    replacement sid for the key by the time it runs — the clear can never
    erase a successor's pointer. Clearing it after the shutdown awaits would
    do exactly that, since the shutdown is the window a concurrent channel
    turn needs to create and map a new session under the same key.
    """
    owner = self._owner
    requested_key = key
    key = owner._fold_key(key)
    async with owner._lock:
        current = owner._sessions.get(key)
        if skip_if_busy and _turn_in_flight(
            current, refuse_only_on_active_turn=refuse_only_on_active_turn
        ):
            return False
        session = owner._sessions.pop(key, None)
        # Snapshot the runs this key owns in the SAME lock hold as the pop: every
        # await below is a window a cold start can register a successor under
        # this key in, and a selection made after one would name the
        # successor's runs. The cancel itself happens after the teardown.
        teardown_children = self._snapshot_parent_children(key)
        owner._advance_session_generation(key)
        owner._compact_cooldown_until.pop(key, None)
        owner._compact_pending_verdict.pop(key, None)
        self._release_turn_ceiling(key, requested_key)
        # Store replay suppression atomically with the pop. Origin-link
        # state intentionally survives this operation.
        if replay:
            self._suppress_replay.discard(key)
        else:
            self._suppress_replay.add(key)
        if session is not None:
            # Same lock hold as the pop, exactly like the clear_sid below.
            await record_session_ended(key, end_reason=END_REASON_DISCARDED)
    # The registry lock, not an absence of suspension points, is what keeps a
    # cold start racing this teardown from registering a replacement sid for the
    # key in between, so this clear cannot erase a SUCCESSOR's pointer. The end
    # record above awaits, but it does so while ``owner._lock`` is still held --
    # the lock that cold start must take to register and map a successor -- and
    # releasing an ``asyncio.Lock`` wakes its waiter without yielding, so control
    # reaches this line before any of them runs.
    # Deferring it past the shutdown awaits below is exactly that bug: the
    # provider shutdown is slow, a concurrent channel turn creates and maps a
    # new session under the same key while it runs, and a clear in the
    # ``finally`` then wipes the new session's sid. Mirrors ``reset``'s
    # ``clear_conversation``, which clears in this same position for this
    # same reason. Outside the lock rather than inside it because
    # ``clear_sid`` persists to disk, and the lock must not span blocking IO.
    owner._session_map.clear_sid(key)
    try:
        if session:
            await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
            await session.provider.shutdown()
    finally:
        # See ``destroy``: in the finally because a shutdown that raises must not
        # carry the exception past the cancel, and cancel before release.
        await self._cancel_parent_children(key, teardown_children, verb="discard_conversation")
        await owner.release_subagent_runtime(key)
        self._deps.logger.info(
            "Discarded native conversation (sid cleared, map entry kept): %s",
            key,
        )
    return True
