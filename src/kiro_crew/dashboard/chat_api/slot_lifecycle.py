"""A slot's lifecycle after create: the shared close path and delete, the idle
cleanup sweep, and the fresh-conversation reset.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _GUARDED_WRITE_WAIT_SECS,
        DashboardState,
        NudgeLoop,
        SlotCloseError,
        SlotRegistry,
        _app_cancel_denied,
        _ChatSlot,
        _history_key_for,
        _persist_handover_tail,
        _replacement_shares_transcript,
        _resettle_restricted_key,
        _subagents_attached_response,
        _sync_dashboard_slots,
        _teardown_drain_basis,
        _unblock_pending_waits,
        deny_app_slot_access,
        effective_session_key,
        logger,
        loop_slot_keys,
        note_slot_closed,
        read_bounded_json,
        save_slot_off_loop,
        sel,
        select_idle_slot_keys,
        session_deletion_confirmed,
        slot_not_found,
        time,
    )


def _slot_still_ours(state: DashboardState, name: str, slot: _ChatSlot) -> bool:
    """Return True iff no OTHER slot object has taken over ``name`` in ``_slots``.

    A close pops the slot, then awaits (task cancel, ``save_slot_off_loop``,
    ``sessions.remove``). A concurrent same-key recreate (POST /api/chat, or the
    session_close MCP verb) can mint a REPLACEMENT slot for the same key inside
    that window, and only THAT is what the destructive teardown steps must yield
    to. So the discriminator is "a DIFFERENT object owns the key", not "our object
    owns the key": an absent key is the ORDINARY post-pop state of every close, so
    ``None`` counts as still ours. Reading it the other way would make the guard
    fire on every close and skip the teardown it guards.

    Synchronous and purely read-only: no side effects, and it touches neither the
    loop, the session map, nor history. Callers use it to decide whether the
    KEY-SCOPED steps (``sessions.remove`` on ``dashboard:{name}``, the failure-arm
    ``_slots`` restore) would clobber a live replacement, and skip them if so. The
    archival history write is NOT key-scoped — see
    :func:`_replacement_shares_transcript`.
    """
    current = state._slots.get(name)
    return current is None or current is slot


async def _restore_original_after_failed_close(
    state: DashboardState, name: str, slot: "_ChatSlot", retired_loop: "NudgeLoop | None"
) -> bool:
    """Put a popped original back after its close could not persist it; True if it came back.

    The compensation the plain save-failure arm of :func:`_close_slot` performs,
    for every arm that reaches the same state -- an original popped from the
    registry whose unsaved tail did not reach disk. Restored only while the key
    is still free or still this slot's (:func:`_slot_still_ours`): a replacement
    already published owns the name, and clobbering it would lose ITS state for
    the original's. With the tab back, the loop retired for the close comes back
    too (a restored session with no clock is an abandoned unattended worker),
    and the app's record of the dismissal is taken back -- only when the tab did
    come back, since with a replacement on the key the dismissal DID happen for
    the original. The key-scoped restricted marker is re-derived from whoever
    holds the name afterwards. The caller reports the close as failed and, for
    a restored tab, releases the admission fence (``close_slot``'s outer
    ``finally`` does it for the single-tab close; the sweep does it itself).
    """
    restored = _slot_still_ours(state, name, slot)
    if restored:
        state._slots[name] = slot
    _resettle_restricted_key(state, name)
    await _restore_slot_nudge_loop(retired_loop, lambda: state.get_slot(name) is slot)
    if slot._app and restored:
        from kiro_crew.apps.teardown import (
            notify_slot_close_undone,  # circular: apps.teardown -> apps.bridges
        )

        if not await notify_slot_close_undone(slot._app, name):
            logger.error(
                "Could not take back the dismissal for app %r on %r; it may still "
                "consider this slot closed",
                slot._app,
                name,
            )
    elif slot._app:
        logger.warning(
            "Slot %s was taken over while its close was persisting, so app %r keeps "
            "the dismissal: the original tab is gone and resuming its worker would "
            "target the replacement now holding this key",
            name,
            slot._app,
        )
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return restored


class _NudgeRetireFailed(Exception):
    """A slot close could not retire the slot's auto-nudge loop.

    Carries the loop so the caller can put it back in MEMORY, which is the point:
    the failure happens between ``remove()``'s in-memory drop and its registry
    write, so memory and disk disagree until one of them is corrected. Restoring
    memory re-agrees with the still-armed disk, leaving the session open and
    still driven rather than open and abandoned.
    """

    def __init__(self, loop: "NudgeLoop | None") -> None:
        super().__init__("autonudge loop removal on slot close failed")
        self.loop = loop


async def _retire_slot_nudge_loop(name: str) -> "NudgeLoop | None":
    """Retire *name*'s auto-nudge loop and return it (None if it had none).

    Retire this slot's loop at the moment the user dismissed the tab.
    "Respect the close" cannot rest on the fire path's rehydrate miss, because
    the fire path adopts THROUGH that miss (see ``_fire_dashboard_nudge``'s
    ``adopt_closed``) or idle archival kills loops terminally. Making the user's
    ✕ the explicit retirement keeps the rule intact without relying on a cache
    miss to enforce it.

    The initial call MUST happen BEFORE the close path's first await, and the
    app-owned path calls it again after its close hook. Two reasons, both of
    which resurrect a session the user closed:

    * The loop's timer can EXPIRE during an await of the close (the turn-cancel
      wait, the history persist, the session teardown). The slot is already out
      of ``state._slots`` by then, so the fire path takes its rehydrate branch
      and restores the transcript with ``adopt_closed=True`` — the very
      transcript the persist is marking closed.
    * Cancelling ``slot.task`` runs ``_run_chat``'s finally, which re-arms the
      timer through ``notify_turn_complete``. Disarming without removing is
      therefore not enough: the clock comes straight back mid-close.

    ``remove_by_slot()`` is what makes this generation-safe: it acquires the
    maintenance transaction before resolving the current loop, so a queued arm
    either lands first and is removed or runs after the synchronous slot pop.
    Its uncontended acquire does not yield, so the initial retirement also
    cancels a scheduled timer before the fire callback gets another turn.
    Legacy loops are removed. Structured monitors instead retain their durable
    outcome and clear their timer, so terminal history remains inspectable.

    The returned loop lets the persist-failure path put the clock back (see
    :func:`_restore_slot_nudge_loop`).

    A removal that FAILS raises :exc:`_NudgeRetireFailed` rather than logging and
    carrying on. Removal drops the loop from memory first and only then writes
    the registry, so a write that raises leaves memory retired while the DISK
    still lists the loop. Swallowing that let the close finish and persist
    the slot as closed, and the next start read the surviving record back: the
    fire path answers the missing slot with ``adopt_closed=True``, so the loop
    rebuilt the dismissed session and ran an unattended turn in it. Locating a
    session the user closed is exactly the outcome this function exists to
    prevent, so the close must not proceed on a half-applied retirement.
    """
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_handlers
        )

        svc = _autonudge_get()
        if svc is None:
            return None
    except Exception:
        # Only the LOOKUP is tolerated: no service and no loop both legitimately
        # mean "nothing to retire", and neither can leave state half-applied.
        logger.warning("autonudge loop lookup on slot close failed", exc_info=True)
        return None
    try:
        return await svc.remove_by_slot(name)
    except Exception as exc:
        logger.warning("autonudge loop removal on slot close failed", exc_info=True)
        loop = svc.get_by_slot(name)
        raise _NudgeRetireFailed(loop) from exc


async def _restore_slot_nudge_loop(
    loop: "NudgeLoop | None", admission_check: Callable[[], bool]
) -> None:
    """Give a session its clock back after a close that failed to persist.

    The close retires the loop before persisting, so a persist that raises would
    otherwise leave the restored session live with nothing driving it — an
    unattended babysit abandoned by a disk error, with no trace but a log line.

    The replacement carries the REMAINING budget, never a fresh one. ``add()``
    mints a new id and a new ``created_ts``, so the spent allowance is subtracted
    here instead: a failed close must not buy unattended cycles the user never
    granted. A loop whose cycle cap or wall-clock budget is already spent is not
    restored at all (it was one tick from terminal), and neither is a paused one
    — reviving that would override an explicit stop.
    """
    if loop is None:
        return
    monitor = getattr(loop, "monitor", None)
    if monitor is not None:
        if (
            not loop.active
            and monitor.outcome is not None
            and monitor.outcome.value == "session_close"
        ):
            try:
                from kiro_crew.autonudge import (
                    get_instance as _autonudge_get,  # circular: autonudge -> dashboard
                )

                svc = _autonudge_get()
                if svc is not None:
                    await svc.restore_monitor_after_failed_session_close(
                        loop.id,
                        admission_check=admission_check,
                    )
            except Exception:
                logger.warning(
                    "structured monitor restore after failed slot close failed",
                    exc_info=True,
                )
        return
    if not loop.active:
        return
    try:
        from kiro_crew import autonudge  # circular: autonudge -> dashboard.chat -> chat_handlers

        svc = autonudge.get_instance()
        if svc is None:
            return
        cycles_left = loop.max_cycles
        if loop.max_cycles:
            cycles_left = loop.max_cycles - loop.cycle_count
            if cycles_left <= 0:
                return
        runtime_left = loop.max_runtime_secs
        if loop.max_runtime_secs and loop.created_ts:
            if autonudge.runtime_budget_exceeded(loop):
                return
            # >=1: a budget of 0 means UNLIMITED, so a spent-to-the-second
            # remainder must not round into "no budget at all".
            runtime_left = max(1, int(loop.max_runtime_secs - (time.time() - loop.created_ts)))
        await svc.add(
            loop.slot_key,
            loop.message,
            idle_secs=loop.idle_secs,
            max_cycles=cycles_left,
            stop_sentinel_path=loop.stop_sentinel_path,
            max_runtime_secs=runtime_left,
            # Configuration, so it is replayed WHOLE — unlike the two budgets
            # above, which are deliberately reduced. Omitting it fails silently:
            # ``add()`` defaults it to "", the loop keeps running, and only the
            # transcript rows change — back to the full multi-KB message every
            # cycle, which is the harm the banner exists to remove. The blank is
            # then persisted, so one failed close would discard the setting for
            # good.
            banner=loop.banner,
            admission_check=admission_check,
        )
    except Exception:
        # Same wedged disk that failed the persist most likely fails this write
        # too. The 500 already tells the caller the close did not happen; the
        # retired loop is visible as gone in the dashboard, not silently dead.
        logger.warning("autonudge loop restore after failed slot close failed", exc_info=True)


async def api_chat_slot_reset_conversation(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/reset-conversation — a fresh conversation, same slot.

    Drops the slot's resume pointer, so its next turn cold-starts a new native
    conversation instead of ``session/load``-ing the accumulated one. Everything
    else survives: the slot stays open, its transcript stays on disk, and the
    session-map ENTRY keeps its channel linkage.

    This capability existed internally with no way to ask for it. Resume is
    key-driven — ``resume_sid = self._session_map.get(key)`` — and a slot key is
    stable by design, so reopening one continues where it left off. That is the
    point for a tab the user closed and came back to. It is NOT what a caller
    wants after a long-lived conversation has drifted, filled up, or outlived the
    thing it was about; and until now the only way to break the link was to
    DELETE the session from history, which destroys the record to reset the
    pointer. This separates the two.

    ``discard_conversation``, not ``destroy``: the entry also carries the Slack
    thread/channel linkage and the reverse index built from it, so dropping the
    row would silently unlink a mirrored session. The dropped value is stashed as
    ``discarded_sid``, so this is diagnosable and reversible by hand.

    It is nonetheless a FULL teardown — it shuts the provider down and releases
    the shared sub-agent runtime — so it takes the same guards the sibling
    teardown route does, through the same shared helpers rather than a third
    policy of its own: authorization on the SESSION (not merely the slot),
    ``provider.has_active_turn()``, ``running``, and the sub-agent gate. Each of
    the three protects work the caller cannot see from the outside: a turn
    running on the session with no dashboard task behind it (an inbound channel
    message), a turn mid-write, and children still running after their parent's
    turn ended.

    The ``has_active_turn()`` check is a best-effort fast path; the
    authoritative guard is the discard's ``skip_if_busy``, which probes the
    per-session SEMAPHORE atomically with the session pop (see
    :meth:`SessionManager.discard_conversation`). The fast path has a known
    edge — a turn holding the semaphore but not yet having a prompt in flight
    is invisible to it — and the atomic guard is what closes it, the same
    contract the sibling reload route rests on, so the two teardowns keep one
    notion of "busy". Of the refusal paths, only the atomic guard's decline is
    SEL-recorded (``outcome="denied"``): it is the one refusal that happens
    after the route has committed to the teardown, while the fast-path 409s
    are pre-checks and stay unlogged, as they are on the sibling.

    The transcript is deliberately left in place, which means the tab still shows
    the earlier messages while the model does not remember them. That is the
    honest rendering of what happened — the record is the user's, the context was
    the conversation's — and it is why this is a deliberate action rather than
    something the gateway does on its own.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # Resolved before authorization, because what has to be authorized is the
    # SESSION this will clear, not the slot it was reached through.
    key = effective_session_key(slot)

    # Slot ownership does not imply ownership of that session:
    # ``get_or_create_slot`` resolves ``linked_session_key`` from the session map
    # for a name shaped like a channel stem, so an app that names a live channel
    # thread ends up owning a slot bound to a conversation it has no claim on.
    # ``_app_cancel_denied`` is the shared policy for exactly that, and it tests
    # the key the caller will actually act on. Answers an indistinguishable 404,
    # and runs BEFORE the 409s below so a refusal cannot confirm the slot exists.
    denied = _app_cancel_denied(request, slot, "slot_reset_conversation", key)
    if denied is not None:
        return denied

    # Read the body HERE — after authorization, before the busy guards. Reading it
    # is an await the CLIENT controls the duration of, and every guard below
    # protects work that can START during a suspension: a turn admitted after
    # ``has_active_turn()`` answered False is torn down mid-write by the discard.
    # Parsing after the guards would widen that window from one event-loop hop to
    # however long a slow body takes to arrive. The guards must be the last thing
    # that happens before the teardown.
    #
    # An absent body is not an error: this route took no body before, so
    # refusing one would break every existing caller for a parameter they do
    # not send. A present-but-malformed body IS refused — "sent nothing" and
    # "sent garbage" are different facts, and only the first can be defaulted.
    replay = True
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    if "replay" in body:
        replay = bool(body.get("replay"))

    # A turn in flight on the SESSION, which ``slot.running`` cannot see: that
    # flag tracks this slot's own task, while an inbound channel message runs a
    # turn on the linked session with no dashboard task at all. Tearing the
    # provider down under it loses that turn's output. Same probe, same order as
    # the sibling reload route — one policy for one teardown.
    provider = state.sessions.get_provider(key)
    if provider is not None and provider.has_active_turn():
        return web.json_response(
            {"error": "a turn is in flight", "code": "turn_in_flight", "slot": name},
            status=409,
        )

    if slot.running:
        return web.json_response(
            {
                "error": "a turn is running on this slot",
                "code": "turn_in_flight",
                "slot": name,
            },
            status=409,
        )
    # ``discard_conversation`` is a full teardown: it also releases the shared
    # sub-agent runtime the parent's children run on. ``slot.running`` is False
    # while they keep going — the parent turn ends first — so nothing above
    # catches it, and the same guard the reload route uses is what does.
    attached = await _subagents_attached_response(state, slot, key, "slot_reset_conversation")
    if attached is not None:
        return attached

    # ``skip_if_busy``: the fast paths above cannot see a turn that holds the
    # per-session semaphore but has not yet put a prompt in flight (an inbound
    # channel message between the lease and its first stream event). The discard
    # probes the semaphore atomically with the session pop, so a turn admitted
    # after the guards above answered False is refused here instead of being
    # torn down mid-lease.
    discarded = await state.sessions.discard_conversation(key, replay=replay, skip_if_busy=True)
    if not discarded:
        sel().log_api_access(
            caller=request.get("app", "") or "dashboard",
            operation="slot_reset_conversation",
            outcome="denied",
            resources=f"slot={name} replay={replay}",
        )
        return web.json_response(
            {"error": "a turn is in flight", "code": "turn_in_flight", "slot": name},
            status=409,
        )
    # The fresh conversation will advertise its own model list, so the previous
    # one's withhold verdict does not describe this slot. Only on a performed
    # discard: a refusal above leaves the old conversation (and its verdict) in
    # place.
    slot.forget_session_model_state()
    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="slot_reset_conversation",
        outcome="completed",
        resources=f"slot={name} replay={replay}",
    )
    return web.json_response({"slot": name, "reset": True, "replay": replay})


def _release_closed_execution(
    state: DashboardState, slot: "_ChatSlot", session_key: str, execution
) -> None:
    """Release a restricted identity after its last consumer and provider stop.

    A PERSISTENT session's vouch is deliberately retained across this close. The
    close is non-destructive -- the conversation is saved and recreated from the
    warm pool when the tab is resumed -- and the ordinary turn-start rebind
    publishes nothing when the selection is unchanged, so withdrawing here leaves
    a resumed member session unvouched and refuses its own-store dispatch until
    its owner re-selects the agent. That refusal belongs to a restart, which
    empties the map wholesale, not to closing a tab.

    The retained population is bounded by the count cap rather than by a
    withdrawal here, and eviction falls on the least recently USED entry, so a
    closed session is the first entry reclaimed instead of a permanent row.
    """
    from kiro_crew.execution_context import clear_session_execution

    if execution is not None and execution.memory_mode != "persistent":
        # A queued-prompt executor may start after the live carrier is released.
        # Keep the retired slot restricted so that late flush still cannot write.
        slot.memory_mode = execution.with_mode(slot.memory_mode).memory_mode
        closing_tasks = tuple(
            task
            for task in (slot.task, getattr(slot, "_eager_spawn_task", None))
            if isinstance(task, asyncio.Task)
        )

        def release_closed_execution(_finished=None) -> None:
            if any(not task.done() for task in closing_tasks):
                return
            if any(effective_session_key(live) == session_key for live in state._slots.values()):
                return
            if state.sessions.get_provider(session_key) is not None:
                return
            clear_session_execution(session_key, expected=execution)

        # Late cancellation completion retains the old identity until the final
        # consumer stops. CAS prevents this close from erasing a newer choice.
        for task in closing_tasks:
            if not task.done():
                task.add_done_callback(release_closed_execution)
        release_closed_execution()


def _pending_guarded_history_writes(slot: "_ChatSlot") -> set:
    """This slot's guarded history writes that have not finished yet.

    Read SYNCHRONOUSLY, which is what lets a caller decide against a fenced slot
    with no suspension between the read and its decision. Anything that is not a
    real set is an absent registry, the same rule the writer applies when it
    registers a future.
    """
    writes = getattr(slot, "_guarded_history_writes", None)
    if type(writes) is not set:
        return set()
    return {write for write in writes if not write.done()}


async def _await_guarded_history_write(slot: "_ChatSlot", name: str) -> bool:
    """Wait for this slot's guarded history writes to finish; True when none is left.

    ``save_slot_off_loop`` runs a guarded write (one carrying an authorized
    transcript key, which is every truncating save) on a worker thread, and that
    write re-reads the slot map INSIDE the transcript lock to confirm it still
    owns the key. The map is event-loop state, so a retraction landing between
    that re-read and the write's rename commits a truncated snapshot onto
    whatever adopts the key next, and a same-name replacement that resumes the
    same transcript inherits the truncation durably.

    Waiting here inverts the guard: the retraction waits for the write instead of
    the write trying to observe the retraction.

    The wait is on the writes' executor FUTURES, which complete with the worker
    thread. ``_metadata_persist_inflight`` cannot serve here even though it
    counts the same writes: it is released in the awaiting coroutine's
    ``finally``, so a handler cancelled mid-write reads as zero while its thread
    runs on -- precisely the case this wait exists for.

    Callers must fence the slot before calling, or a fresh write can be admitted
    into the wait. A ``False`` return means writes are still outstanding, and the
    caller must NOT retract the name.
    """
    deadline = time.monotonic() + _GUARDED_WRITE_WAIT_SECS
    waited = False
    while True:
        pending = _pending_guarded_history_writes(slot)
        if not pending:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                "Slot %s close refused: %d guarded history write(s) still running "
                "after %.1fs, so retracting the name now could commit a truncated "
                "snapshot onto whatever adopts this key next",
                name,
                len(pending),
                _GUARDED_WRITE_WAIT_SECS,
            )
            return False
        if not waited:
            waited = True
            logger.debug("Slot %s close waiting for a guarded history write", name)
        # Wait on the futures themselves rather than polling a counter: a future
        # already awaited elsewhere admits further waiters, and this returns the
        # moment the worker finishes instead of on the next poll tick.
        await asyncio.wait(pending, timeout=remaining)


async def _wake_conductor_for_closed_worker(name: str) -> None:
    """Pull *name*'s conductor's work-ledger tick forward, if *name* was a bound worker.

    NEVER RAISES. A close is a user action with rollback paths for its own four failure
    modes; "the conductor was not told early" is not one of them, because the conductor's
    scheduled tick reads the same ledger a cadence later. So every failure here is a
    DEBUG line and the close proceeds.

    The import is function-local: ``conductor_wake`` reaches the work-ledger store, and
    every gateway runs this close path whether or not any conductor has ever opened a
    ledger.

    WHEN IT IS CALLED. TRIGGER TWO of the crew-log wake, and only from the two exits
    where the close is COMMITTED: after the archival save succeeds, and after the
    hand-over exit's tail drain landed. Never between the pop and the save. The
    conductor's probe answers "is this worker closed" by looking the slot up, and a
    ``worker_closed`` answer persists a stall observation that no rollback retracts; a
    tick fired in that window would read the popped slot as gone, and the save's
    failure arm would then put the slot back under a permanent false "worker gone".

    Awaited rather than detached: the binding read is offloaded inside and ``fire_now``
    has no suspension point, so this adds one executor hop to a teardown that has
    already awaited several -- and a detached task would outlive the close and could
    fire after a same-key recreate.
    """
    try:
        from kiro_crew import conductor_wake

        await conductor_wake.fire_for_worker_slot(name)
    except Exception:  # noqa: BLE001 - a push must never fail a close
        logger.debug("conductor wake on close failed for %s", name, exc_info=True)


async def close_slot(
    state: DashboardState,
    slot: "_ChatSlot",
    name: str,
    *,
    pre_pop_check: Callable[[], None] | None = None,
) -> None:
    """Close a slot while releasing its admission fence on every aborted path."""
    try:
        await _close_slot(state, slot, name, pre_pop_check=pre_pop_check)
    finally:
        if state.get_slot(name) is slot:
            slot.cancel_close()


async def _close_slot(
    state: DashboardState,
    slot: "_ChatSlot",
    name: str,
    *,
    pre_pop_check: Callable[[], None] | None = None,
) -> None:
    """Close (archive) one live slot the way the tab ✕ does: tombstone it, retire
    its auto-nudge loop, notify its owning app, persist it as closed, and tear
    down the per-tab session.

    Non-destructive: the conversation is saved to history (``closed=True``) and
    recreated from the warm pool if the tab is resumed later — nothing is
    permanently deleted here.

    Shared by :func:`api_chat_slot_delete` and ``session_control.close_target``
    so neither can diverge on the ordering invariants that keep a nudge or an
    app watchdog from resurrecting the very tab being dismissed. The four
    failure paths raise :class:`SlotCloseError`, leaving the slot open with every
    partial step rolled back; the caller renders the refusal in its own idiom
    (an HTTP response, or a ``SessionControlError``). The APP-OWNERSHIP check is
    deliberately NOT here — it is DELETE-endpoint policy (App Kit isolation for
    app tokens) and stays in that handler; session control scopes the caller
    through ``authorize_target`` instead.

    ``pre_pop_check`` runs SYNCHRONOUSLY at the point of no return — immediately
    before the slot is popped, after the nudge-retirement and app-hook awaits. It
    exists for a caller (``close_target``) that authorized the target BEFORE this
    coroutine and must re-assert that authorization against state those awaits
    could have changed: a target unmirrored/unlinked at admission can gain a
    channel mirror or link while the AutoNudge lock and the app hook are awaited,
    and archiving a now-channel-backed session it was never allowed to reach is
    the boundary the target guards exist to hold. It is SYNCHRONOUS on purpose —
    an awaited check would put a suspension back between the last retirement and
    the pop (reopening the retired-loop window) and between the re-authorization
    and the pop (reopening the mirror window); a synchronous check has neither, so
    nothing can change between the final authorization and the archival. It must
    therefore do no blocking I/O (``close_target`` passes ``skip_enabled_check=True``
    so its ``authorize_target`` never reads config on the loop). It raises
    :class:`SlotCloseError` to abort; the abort unwinds the teardown so far (the
    retired nudge loop is restored, an app notification is taken back) and
    re-raises, exactly like the persist-failure path. The human ✕ path passes
    ``None`` — the person owns the tab and closes it unconditionally.
    """
    # Synchronous tombstone, BEFORE any await: a channel-slot reconcile pass
    # whose snapshot predates this close reads these after its last await, so
    # it cannot re-surface the tab this handler is dismissing (see
    # channel_slots._RECENT_CLOSES). The returned instant is persisted as
    # closed_at below — the save runs after the cancellation awaits, and
    # stamping save time would make channel activity landing in that window
    # compare as older than the close.
    # Fence monitor admission for this exact slot generation before retirement:
    # terminal replacement is otherwise allowed and could commit after this
    # close observed the already-terminal record, leaving an active orphan.
    slot.begin_close()
    closed_at = note_slot_closed(state, name)
    # The fence above is what makes this wait sound: it refuses a NEW truncating
    # save for the duration of the teardown, so the writes this drains cannot be
    # re-armed behind it. Placed here, after the synchronous tombstone and before
    # the retirement awaits, it adds no suspension near the pop: the pre-pop
    # re-check and the retraction stay adjacent, which is what keeps a retired
    # nudge loop and a late channel mirror from slipping between them.
    #
    # A breach REFUSES the close instead of proceeding. Proceeding would retract
    # the name with a worker thread still short of its rename, which is the
    # unrecoverable case this whole wait exists to prevent: the replacement
    # resumes from the file that worker rewrites, and nothing retries or
    # self-corrects on that path. Refusing is recoverable by contrast -- the tab
    # stays open with every step so far rolled back, and the person can close it
    # again -- and it returns within the ceiling, so a stuck write delays the
    # close rather than hanging it.
    if not await _await_guarded_history_write(slot, name):
        raise SlotCloseError(
            "a history write for this conversation is still running; the tab stays "
            "open, close it again in a moment",
            code="history_write_running",
        )
    from kiro_crew.execution_context import read_live_session_execution

    closing_key = effective_session_key(slot)
    closing_execution = read_live_session_execution(closing_key)
    # Retire the auto-nudge loop BEFORE the awaits below, so no nudge can expire
    # into the session being closed and resurrect it. See
    # _retire_slot_nudge_loop for why disarming alone does not hold.
    try:
        retired_loop = await _retire_slot_nudge_loop(name)
    except _NudgeRetireFailed as exc:
        # The loop could not be retired, so the close CANNOT proceed: persisting
        # the slot as closed while the registry still lists the loop is what lets
        # the next start rebuild this session and nudge it. Put the in-memory
        # loop back so memory agrees with the armed disk, and report the failure
        # the same way a failed history save does — the tab stays open and driven,
        # which is a state the user can see and retry, unlike a closed tab that
        # quietly wakes up later.
        await _restore_slot_nudge_loop(exc.loop, lambda: state.get_slot(name) is slot)
        logger.error("Failed to retire nudge loop for slot %s, close aborted", name)
        _sync_dashboard_slots(state)
        state.push_slots_update()
        raise SlotCloseError("failed to retire nudge loop", code="nudge_retire_failed")
    # Remove from the registry only AFTER the loop is retired, because the ORDER
    # is what decides whether a nudge landing in between is harmless or fatal.
    # Retiring takes the AutoNudge lock, so it awaits; with the pop first, a
    # timer expiring inside that await finds the slot already gone from `_slots`,
    # and the fire path's response to a missing slot is
    # `rehydrate_slot_from_history_async(..., adopt_closed=True)` — it rebuilds
    # the session and adopts it DESPITE the closed flag (deliberately, so
    # idle-archived workers survive). Popping first therefore turns "the user
    # dismissed this tab" into "the tab comes back".
    #
    # With the loop retired first there is no timer left to fire, so the removal
    # below cannot be undone. A nudge that fires BEFORE the retire begins still
    # runs a turn, but that is the ordinary race with the ✕ click itself and it
    # resurrects nothing.
    # Tell the app BEFORE anything durable happens. For a crew this hook is the
    # write that pauses the worker, so it has to succeed for the dismissal to
    # mean anything — and it must be undoable if it does not. Sequenced here, a
    # failure costs nothing: the slot is still in `_slots`, history still says
    # open, and the only thing to put back is the loop. Sequenced after the
    # persist there is nothing to abort INTO — the close is already committed, so
    # a lost pause leaves a live auto-approved crew whose watchdog relaunches the
    # tab, with only a log line to say so.
    #
    # Stopping the worker first is also the right order on its own terms: quiet
    # the thing, then dismantle its surface. The reverse opens exactly the window
    # this hook exists to close.
    #
    # Deliberately NOT in the bulk idle-archive path below: that one closes a slot
    # for quietness, and an app worker stopped by idleness alone is a silent
    # failure. Which call site fires IS the signal.
    if slot._app:
        from kiro_crew.apps.teardown import (
            notify_slot_closed,  # circular: apps.teardown -> apps.bridges -> dashboard
        )

        if not await notify_slot_closed(slot._app, name):
            # The app could not record the dismissal. Refuse the close rather
            # than leave a worker running behind a tab the user believes is gone.
            await _restore_slot_nudge_loop(retired_loop, lambda: state.get_slot(name) is slot)
            logger.error("Slot-close hook for app %r failed on %r, close aborted", slot._app, name)
            _sync_dashboard_slots(state)
            state.push_slots_update()
            raise SlotCloseError("failed to notify the app", code="app_close_hook_failed")
        # The app hook awaits external work while the slot is still visible.
        # Re-arbitrate the nudge registry after it returns: an arm that committed
        # during that await must be retired before the synchronous pop below.
        # There is no await between a successful second retirement and the pop,
        # so a later queued arm revalidates against the now-missing slot.
        try:
            late_retired_loop = await _retire_slot_nudge_loop(name)
        except _NudgeRetireFailed as exc:
            await _restore_slot_nudge_loop(exc.loop, lambda: state.get_slot(name) is slot)
            from kiro_crew.apps.teardown import (
                notify_slot_close_undone,  # circular: apps.teardown -> apps.bridges
            )

            if not await notify_slot_close_undone(slot._app, name):
                logger.error(
                    "Could not take back the dismissal for app %r on %r after "
                    "late nudge retirement failed",
                    slot._app,
                    name,
                )
            logger.error("Late nudge retirement failed for slot %s; close aborted", name)
            _sync_dashboard_slots(state)
            state.push_slots_update()
            raise SlotCloseError("failed to retire nudge loop", code="nudge_retire_failed")
        if late_retired_loop is not None:
            retired_loop = late_retired_loop
    if pre_pop_check is not None:
        # Point of no return: re-assert authorization that the awaits above could
        # have staled (nudge retirement takes the AutoNudge lock; the app hook
        # awaits external work). Called SYNCHRONOUSLY so there is NO suspension
        # between the last retirement above, this re-check, and the pop below —
        # nothing can change between the final authorization and the archival, and
        # the retirement stays adjacent to the removal. A raised SlotCloseError
        # unwinds the teardown so far — restore the retired nudge loop, take back
        # an app notification — and re-raises, exactly like a failed persist.
        try:
            pre_pop_check()
        except SlotCloseError:
            await _restore_slot_nudge_loop(retired_loop, lambda: state.get_slot(name) is slot)
            if slot._app:
                from kiro_crew.apps.teardown import (
                    notify_slot_close_undone,  # circular: apps.teardown -> apps.bridges
                )

                if not await notify_slot_close_undone(slot._app, name):
                    logger.error(
                        "Could not take back the dismissal for app %r on %r after a "
                        "pre-pop re-check aborted the close",
                        slot._app,
                        name,
                    )
            _sync_dashboard_slots(state)
            state.push_slots_update()
            raise
    # ── Teardown takeover watch ─────────────────────────────────────────
    # Opened BEFORE the pop, because the pop is what frees the key for a
    # same-name recreate: every await in the teardown below (the task-cancel
    # wait, the app notifications, the saves' executor hops) is a window a
    # resume can begin in, and a resume that begins mid-teardown fires its
    # takeover note before any of this frame's saves could open a watch of
    # their own. Carrying ONE watch across the whole teardown — the archive
    # save, the hand-over drains, the failure arms — is what lets a
    # rewrite-shaped write (``_pending_rewrite``) lose to that takeover no
    # matter which of those writes ends up carrying it. Closed in the
    # ``finally``, on every exit.
    _teardown_basis = SlotRegistry.open_takeover_basis(state, name)
    try:
        state._slots.pop(name, None)
        # Release any blocking wait before cancelling the task: a question pending on
        # the blocking POST /api/ask-question path holds an MCP worker on an open
        # HTTP request, and the slot is going away, so nobody will answer its card.
        _unblock_pending_waits(state, slot)
        # Cancel any pending speculative session creation. Without this, an
        # eager task mid-debounce or mid-handshake outlives the slot; combined
        # with the task's own post-create liveness re-check this closes both
        # halves of the delete/recreate race.
        _eager = getattr(slot, "_eager_spawn_task", None)
        if _eager is not None and not _eager.done():
            _eager.cancel()
        # A pending resume-prefetch TTL timer is deliberately NOT cancelled here:
        # its removal is conditional (no-ops once the slot is gone or the session
        # was claimed), while a cancel landing mid-removal would interrupt
        # provider.shutdown() after the registry entry was already popped and
        # leak the process holding kiro-cli's native session lock.
        _teardown_tasks = {task for task in (slot.task,) if task is not None and not task.done()}
        if _teardown_tasks:
            for task in _teardown_tasks:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(*_teardown_tasks, return_exceptions=True)),
                    timeout=2.0,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        # Post-pop teardown race: across the awaits above (and the app-notify awaits
        # before the pop) a concurrent same-key recreate — a POST /api/chat or the
        # session_close MCP verb — can mint a REPLACEMENT slot under `name`. When that
        # replacement writes the SAME transcript, writing THIS (original) slot as
        # closed=True would stamp the archive flag on a conversation the replacement is
        # still using, and the sessions.remove below would tear down the session it now
        # uses. The original was already popped and its task cancelled, so the close is
        # effectively complete for it; leave the replacement's slot and session
        # untouched and report success.
        #
        # The gate is TRANSCRIPT sharing, not key ownership, because those are two
        # different questions and the save answers to the transcript: a linked slot
        # (channel-, cron- or workflow-born) writes its `linked_session_key`, while an
        # unbound same-name replacement writes `dashboard:{name}`. Yielding the archive
        # on that pair would leave the ORIGINAL's transcript with no `closed` flag, and
        # the channel reconcile reads an absent flag as "never dismissed" and resurfaces
        # the tab. So a replacement that shares nothing takes nothing: the archive below
        # runs normally on the original's own file, and only the KEY-SCOPED steps
        # (`sessions.remove`, the failure-arm restore) still yield to it.
        #
        # Yielding the archive is NOT the same as discarding the original's content. Its
        # own unsaved rows still belong on its own transcript — what must not happen is
        # the `closed` stamp and the session teardown, not the write.
        # `_persist_handover_tail` is that distinction made explicit: the same window
        # this frame was about to save, saved OPEN instead of closed.
        #
        # Scope, precisely: this closes the WIDE window — the app-notify awaits before
        # the pop and the up-to-2.0s task cancel above — and NOT the durable write
        # itself. `save_slot_off_loop` reaches its commit through the process-wide
        # default executor, so a recreate can still land between this synchronous
        # check and the in-lock write. That residual is the one an unguarded close
        # carries too, and the row it leaves is what a plain sequential
        # close-then-reopen of a reused key already produces: `closed`/`closed_at` are
        # slot-owned metadata, so the replacement's next full save drops them, and
        # `api_chat_slot_resume` compensates a stale flag with an in-lock
        # compare-and-clear. Closing it AT the commit needs an ownership predicate
        # evaluated inside `_locked(history_key)` — on the write and on the resume's
        # read-then-clear both — which is a durable-metadata contract change, not a
        # loop-side ordering one.
        if _replacement_shares_transcript(state, name, slot):
            # Yielding the archive must not silently discard what this slot never got to
            # disk. The original is out of `_slots` and about to be unreferenced, and
            # the periodic flush only ever visits `_slots`, so this frame is the last
            # thing that can persist its tail — as an OPEN-key write, which is the
            # single difference from the archival save this exit declines.
            drained = await _persist_handover_tail(
                state,
                name,
                slot,
                takeover_basis=_teardown_drain_basis(state, name, slot, _teardown_basis),
            )
            # The key belongs to the replacement now, and so does every KEY-SCOPED
            # marker sitting on it. Hand the restricted flag over before letting go:
            # the discard below the save is the only thing that would have cleared the
            # original's, and this exit skips it. After the drain above, so the marker
            # is derived from the newest observation of who holds the key.
            _resettle_restricted_key(state, name)
            _sync_dashboard_slots(state)
            state.push_slots_update()
            if slot._app:
                # Same decision the failure arm below takes, and it must be as visible:
                # this is the MORE common hand-over, so a silent one would hide every
                # ordinary occurrence of an app worker left paused.
                logger.warning(
                    "Slot %s was recreated while its close was tearing down, so app %r "
                    "keeps the dismissal: the original tab is gone and resuming its "
                    "worker would target the replacement now holding this key",
                    name,
                    slot._app,
                )
            if not drained.rows_committed:
                # The drain was this frame's last chance at those rows, so a close that
                # reported success here would be reporting durability it does not have —
                # and unlike the arm below there is nothing to roll back and nothing that
                # will retry, so the report IS the whole remedy. Same code as the
                # ordinary save failure: from the caller's side this is one thing, a
                # close whose history write did not land.
                raise SlotCloseError("failed to save history", code="history_save_failed")
            # Otherwise return, do not raise: for the ORIGINAL the close is complete
            # (popped, task cancelled, tail durable), so every caller — the DELETE
            # handler and session-control's close_target — must read this as success.
            # Committed, so the conductor may be told now and not before.
            await _wake_conductor_for_closed_worker(name)
            return
        try:
            archived_ok = await save_slot_off_loop(
                state,
                slot,
                closed=True,
                closed_at=closed_at,
                best_effort=False,
                # Discriminated, not raw: this archive writes the ORIGINAL's
                # routed transcript, and a divergent same-name takeover (an
                # unbound recreate over a linked tab) took the key without
                # touching that file — its moved watch must not refuse the
                # archive the divergent branch of this close deliberately runs.
                takeover_basis=_teardown_drain_basis(state, name, slot, _teardown_basis),
            )
        except Exception:
            # Save failed — restore slot so data isn't lost
            logger.error("Failed to save slot %s to history, restoring", name, exc_info=True)
            # ...but only if the key is still free or still ours. A recreate that
            # landed while save_slot_off_loop was in flight now owns `name`; blindly
            # writing `state._slots[name] = slot` would clobber that live replacement
            # with the failed original. Restore only when the slot is genuinely still
            # ours (or the key is now empty).
            restored = _slot_still_ours(state, name, slot)
            if restored:
                state._slots[name] = slot
            else:
                # Not restored means not referenced: the periodic flush that would have
                # retried this write only visits `_slots`, so without this the failure
                # arm's own stated invariant — "restores the slot so data isn't lost" —
                # is not met for the hand-over case. Re-attempt the write as the
                # open-key save the state actually is, which also clears a failure that
                # was only lock contention with the recreate instead of treating one as
                # permanent, and reports the row count when it is not.
                #
                # Its answer needs no branch HERE — unlike the pre-save exit above, this
                # arm already ends in `SlotCloseError`, so a lost tail is reported to the
                # caller either way. The drain only decides whether the rows survived.
                await _persist_handover_tail(
                    state,
                    name,
                    slot,
                    takeover_basis=_teardown_drain_basis(state, name, slot, _teardown_basis),
                )
            # Whichever way that went, the key-scoped restricted marker has to describe
            # whoever holds `name` when this frame ends — the restored original, or the
            # replacement that kept the key. This arm never reaches the discard below
            # the save, so it settles the marker itself.
            _resettle_restricted_key(state, name)
            # Keep monitor admission fenced until every rollback await completes.
            # ``close_slot`` releases the fence in its outer finally.
            # The close did not happen, so the loop retired for it must come back —
            # a restored session with no clock is an abandoned unattended worker.
            await _restore_slot_nudge_loop(retired_loop, lambda: state.get_slot(name) is slot)
            # ...and the app's record of the dismissal has to come back too — but ONLY
            # if the tab did. The notification above already SUCCEEDED, which for a crew
            # means the worker is durably paused; a close that puts the tab back and
            # leaves the worker stopped hands the user an error AND a silently disabled
            # worker. Unwound in reverse order of commitment, which is the only
            # arrangement that leaves no pair of the three stores disagreeing.
            #
            # The undo is COUPLED TO THE RESTORE, not to `_app` alone, because with a
            # replacement on the key there is no tab to put back: the original is
            # popped, cancelled, and not coming back, so the dismissal DID happen for it
            # and taking it back would be a lie with teeth. Resuming a crew re-arms an
            # autonomous worker whose `slot_key` its watchdog resolves straight through
            # `state.get_slot(...)` with no ownership test — so the auto-approve grant,
            # and then an unbounded nudge clock, would land on the USER-owned
            # replacement now sitting on that key. Leaving the pause is the same answer
            # the pre-save guard above gives from the identical state, and it is a
            # first-class visible one (a paused_reason row with a resume control), not a
            # silent stop.
            if slot._app and restored:
                from kiro_crew.apps.teardown import (
                    notify_slot_close_undone,  # circular: apps.teardown -> apps.bridges
                )

                if not await notify_slot_close_undone(slot._app, name):
                    logger.error(
                        "Could not take back the dismissal for app %r on %r; it may still "
                        "consider this slot closed",
                        slot._app,
                        name,
                    )
            elif slot._app:
                logger.warning(
                    "Slot %s was recreated while its close was persisting, so app %r keeps "
                    "the dismissal: the original tab is gone and resuming its worker would "
                    "target the replacement now holding this key",
                    name,
                    slot._app,
                )
            _sync_dashboard_slots(state)
            state.push_slots_update()
            raise SlotCloseError("failed to save history", code="history_save_failed")
        else:
            if not archived_ok and await asyncio.to_thread(session_deletion_confirmed, state, slot):
                # Definitive evidence only (a missing file, or a readable line
                # contradicting the recorded identity): the session was
                # permanently deleted while this save awaited the lock, and the
                # delete already disposed of what this frame was archiving. An
                # unverifiable probe lands in the retry arm below instead —
                # discarding a tail on a transient stat failure is not a
                # deletion outcome. Probed BEFORE the takeover branch: a
                # delete-won refusal can coincide with a moved watch (a resume
                # noted the key, then the delete landed), and the takeover
                # drain writes with no deletion guard — its append recreates
                # the deleted transcript with a fresh metadata line, silently
                # un-deleting what the user permanently removed. Deletion is
                # the stronger verdict: there is no transcript left for a
                # takeover to own.
                logger.info(
                    "Slot %s close archive skipped: the session was deleted while "
                    "the archive awaited the lock",
                    name,
                )
            elif not archived_ok and SlotRegistry.basis_moved(
                state, _teardown_drain_basis(state, name, slot, _teardown_basis)
            ):
                # A refusal is not a commit, and this one names its cause: the
                # teardown watch moved, so a same-key takeover — published, or a
                # resume whose read is still queued behind the history lock —
                # won the ordering and the archive wrote nothing. The transcript
                # belongs to the takeover now, so take the same exit the
                # published-replacement branch above takes: drain the original's
                # own tail (the same watch rides along, so a rewrite-shaped
                # drain loses to the takeover exactly like the archive did),
                # yield every key-scoped step, and report the close honestly.
                # The DISCRIMINATED basis, the same one the archive save was
                # handed: a divergent same-key takeover that never touched this
                # slot's routed transcript maps to ``None`` (which
                # ``basis_moved`` answers ``False`` for), so an unrelated
                # refusal — the stale-queue guard, say — coinciding with such a
                # takeover routes to the retry arm below rather than being
                # mislabeled a settled takeover.
                drained = await _persist_handover_tail(
                    state,
                    name,
                    slot,
                    takeover_basis=_teardown_drain_basis(state, name, slot, _teardown_basis),
                )
                if not drained.rows_committed:
                    # The drain was the popped original's last writer and it did
                    # not land -- and unlike the published-replacement exit above,
                    # the takeover here may still be UNPUBLISHED (a resume whose
                    # read waits on this teardown's watch), so the key can be
                    # free. Dropping the original then loses its only copy of the
                    # tail with nothing left to retry. Same compensation as a save
                    # that raised: the tab comes back while the key is still
                    # free (the pending resume then dedups to it and the flush
                    # retries the dirty window); a replacement already on the key
                    # keeps it, and the failure is reported as the drain's own
                    # arm already did for the rows.
                    logger.error(
                        "Slot %s: the hand-over to a takeover did not land the original's "
                        "tail; restoring the original where the key allows",
                        name,
                    )
                    await _restore_original_after_failed_close(state, name, slot, retired_loop)
                    raise SlotCloseError("failed to save history", code="history_save_failed")
                _resettle_restricted_key(state, name)
                _sync_dashboard_slots(state)
                state.push_slots_update()
                if slot._app:
                    logger.warning(
                        "Slot %s was taken over while its close was persisting, so "
                        "app %r keeps the dismissal: the original tab is gone and "
                        "resuming its worker would target the takeover now holding "
                        "this key",
                        name,
                        slot._app,
                    )
                # Committed, so the conductor may be told now and not before.
                await _wake_conductor_for_closed_worker(name)
                return
            elif not archived_ok:
                # A refusal is not a commit, and this one is neither a takeover
                # (the watch did not move) nor a verified deletion — the
                # stale-queue guard, for one, refuses when another writer
                # committed a newer queued-prompt value while this save held an
                # older snapshot. The state is retryable and the popped slot
                # holds the only copy of its unsaved tail, so this is the same
                # outcome as a save that raised: restore the tab when the key
                # is still free, otherwise drain the tail, and report the close
                # as failed either way rather than discarding acknowledged rows
                # as if they were archived.
                logger.warning(
                    "Slot %s close archive refused (not a deletion, not a takeover); "
                    "treating the close as failed",
                    name,
                )
                if not _slot_still_ours(state, name, slot):
                    await _persist_handover_tail(
                        state,
                        name,
                        slot,
                        takeover_basis=_teardown_drain_basis(state, name, slot, _teardown_basis),
                    )
                await _restore_original_after_failed_close(state, name, slot, retired_loop)
                raise SlotCloseError("failed to save history", code="history_save_failed")
            # Through the shared postcondition rather than a bare discard: on the
            # ordinary close the key is gone and this drops the marker, and a recreate
            # that landed during the save gets the marker re-derived from ITSELF instead
            # of inheriting the original's.
            _resettle_restricted_key(state, name)
            # Record the DELIBERATE close as a ``session/closed`` crew-log edge with
            # ``END_REASON_REMOVED`` so a later ``session_status`` gone row for this
            # worker reads ``closed`` ("finished, nothing to re-dispatch") rather than
            # ``lost``. This lives HERE, in the one deliberate-close caller, not in
            # ``SessionManager.remove``: ``remove`` has many callers (archive sweeps,
            # mid-handshake teardowns, Slack command paths) that are NOT a worker
            # finishing, and stamping ``removed`` from inside it would make a reaped or
            # torn-down worker read ``closed`` too. The sid is resolved from the session
            # MAP (``mapped_sid``, in-memory, no prune), not from a live provider,
            # because a worker that finished and sat idle may already have been
            # process-recycled by ``reset`` -- which pops the live session and wrote a
            # ``reset`` close -- before its tab was closed; the map still names the sid,
            # and this ``removed`` edge, written after that ``reset``, is the newest
            # lifecycle entry, so the gone row reads ``closed``.
            #
            # AWAITED before the wake below, not fire-and-forget: the conductor is woken
            # on the strength of this close, and a ``session_status`` it runs then folds
            # this worker's lifecycle. If the ``removed`` edge had not yet landed, that
            # fold would read the earlier ``reset`` and report ``lost`` -- a redundant
            # re-dispatch of a worker that is actually finished. So we hand the append to
            # ``awaiting_commit`` and only wake once it has committed. Key-scoped
            # ``_slot_still_ours`` guard first: a recreate can land between the save and
            # here, and ``_history_key_for(name)`` is the session an unbound replacement
            # runs on, so emitting for it would mis-stamp a live replacement. Fail-soft
            # and bounded: a drop or timeout from ``awaiting_commit`` leaves the edge
            # unwritten and the row reads ``lost`` -- the fail-safe direction (a transient
            # redundant re-dispatch, never a lost worker), so we wake regardless.
            if _slot_still_ours(state, name, slot):
                close_sid = state.sessions.mapped_sid(_history_key_for(name))
                if close_sid:
                    from kiro_crew.crew_log import emit as crew_log_emit
                    from kiro_crew.metrics.sessions import END_REASON_REMOVED

                    await crew_log_emit.awaiting_commit(
                        lambda on_settled: crew_log_emit.on_session_closed(
                            close_sid, END_REASON_REMOVED, on_settled=on_settled
                        ),
                        what=f"session/closed(removed) for {close_sid}",
                    )
            # Durable, so no rollback can retract this frame — a client pruning its
            # per-slot cards on it can never be pruning a slot that comes back.
            state.push_slot_removed(name)
            # Committed, so the conductor may be told now and not before.
            await _wake_conductor_for_closed_worker(name)
    finally:
        SlotRegistry.close_takeover_basis(state, _teardown_basis)
    # The app was already told, and compensated if the persist above failed — see
    # the notify block before the pop and the rollback in the except branch.
    # Kill the per-tab session to free resources. Re-check identity ONE more
    # time, and KEY-scoped here rather than transcript-scoped: a recreate can land
    # between the save above and this remove, and `_history_key_for(name)` is the
    # session an unbound replacement runs on, so removing it would tear down a live
    # replacement's session no matter which transcript that replacement writes. Skip
    # it unless the key is still ours.
    if _slot_still_ours(state, name, slot):
        await state.sessions.remove(_history_key_for(name))
    _release_closed_execution(state, slot, closing_key, closing_execution)
    _sync_dashboard_slots(state)
    state.push_slot_removed(name)
    state.push_refresh("history")


async def api_chat_slot_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot} — stop and remove a UI slot.

    Kills the per-tab kiro-cli session and saves history.  The session
    will be recreated from the warm pool if the tab is resumed later.

    The close sequence itself lives in :func:`close_slot`, shared with
    session-control's ``close_target``; this handler adds only the
    DELETE-endpoint's App Kit ownership check and maps the outcome to a
    response.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    # App ownership check (App Kit §5.2): app can only delete slots it created.
    # Unscoped slots (empty _app) cannot be deleted by app tokens.
    # Dashboard users (empty request_app) can delete anything.
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_delete")
    if denied is not None:
        return denied

    try:
        await close_slot(state, slot, name)
    except SlotCloseError as exc:
        # Every failure `close_slot` raises is a server-side 500 (history write
        # running / nudge retire / app hook / history save); a literal status keeps
        # the error-code contract gate able to verify the `code` statically (a
        # `status=<expr>` would read as an un-verifiable dynamic-status response).
        # The pre-pop re-check that raises other statuses is session-control's
        # path, not this handler's.
        return web.json_response({"error": exc.message, "code": exc.code}, status=500)
    return web.json_response({"ok": True})


async def api_chat_slots_cleanup(request: web.Request) -> web.Response:
    """POST /api/chat/slots/cleanup — bulk-archive inactive sessions to history.

    Body: ``{"max_inactive_days": 3, "active_slot": "chat-1-123"}``
    Skips the active slot and pinned sessions.
    """
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    max_days = 3
    try:
        max_days = max(1, int(body.get("max_inactive_days", 3)))
    except (ValueError, TypeError):
        pass
    active_slot = body.get("active_slot", "")
    dry_run = body.get("dry_run", False)
    request_app = request.get("app", "")
    cutoff = time.time() - max_days * 86400
    # Slots owning an ARMED auto-nudge loop are exempt from idle archival.
    # Archiving one marks it closed, and the nudge fire path then cannot reach
    # it and REMOVES the loop — terminally. An unattended worker is idle by
    # nature between cycles (a 6h CI wait looks exactly like abandonment), so
    # the 3-day idle heuristic would shoot the longest-running loops. Resolved
    # once, outside the per-slot loop, so a large registry costs one pass.
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_handlers
        )

        _svc = _autonudge_get()
        _looped = loop_slot_keys(_svc.list_all()) if _svc is not None else set()
    except Exception:
        # Fail CLOSED for the loops: if the registry cannot be read we do not
        # know which slots are protected, so archive nothing this pass rather
        # than risk destroying a loop. Cleanup is a convenience; the loop is not.
        logger.warning("Cleanup: auto-nudge registry unreadable; skipping this pass", exc_info=True)
        return web.json_response(
            {"ok": True, "archived": 0, "keys": [], "failed": [], "skipped": "autonudge_unknown"}
        )
    # One selection for this button and the background idle sweep: pinned slots,
    # loop-driven slots, other apps' slots and slots of unknown activity are kept.
    stale_keys, active_is_stale = select_idle_slot_keys(
        state,
        looped=_looped,
        cutoff=cutoff,
        request_app=request_app,
        active_slot=active_slot,
    )
    # Dry-run: return the exact list without archiving
    if dry_run:
        sel().log_api_access(
            caller="dashboard",
            operation="chat.cleanup_dry_run",
            outcome="allowed",
            source="dashboard",
            resources=f"count={len(stale_keys)} threshold={max_days}d",
        )
        return web.json_response(
            {
                "ok": True,
                "dry_run": True,
                "keys": stale_keys,
                "count": len(stale_keys),
                "active_is_stale": active_is_stale,
            }
        )
    archived: list[str] = []
    failed: list[str] = []
    _tasks_to_cancel: list[asyncio.Task] = []
    from kiro_crew.execution_context import read_live_session_execution

    for name in stale_keys:
        candidate = state._slots.get(name)
        if candidate is None:
            continue
        closing_key = effective_session_key(candidate)
        closing_execution = read_live_session_execution(closing_key)
        if candidate.is_closing:
            # Another retraction already owns this slot -- a close the person
            # asked for, suspended inside its own wait for guarded writes. Leave
            # it alone: it is being archived anyway, and two retractions racing
            # one name is what the fence exists to prevent, not something to join.
            continue
        # Fence, then decide SYNCHRONOUSLY, then pop -- with no await in
        # between. A sweep has no obligation to finish this retraction, unlike a
        # close the person asked for, so it does not wait for a guarded write: it
        # defers the slot to the next sweep.
        #
        # Waiting here would be worse than useless. The handlers that produce a
        # guarded write publish a task on the same slot in the same breath, so a
        # pending write and a live turn co-occur by construction; a wait would
        # hold the sweep open exactly while the tab is being edited, and the pop
        # after it would cancel that turn. Deferring removes the window rather
        # than re-checking for it, and a pending guarded write is itself proof the
        # tab is not idle, whatever its last recorded activity says.
        #
        # The fence is what makes the synchronous read sound: with it up, no NEW
        # guarded write can be dispatched (the saver refuses one, and rewind
        # refuses at admission and again at its dispatch seam), so an empty
        # reading stays empty through the pop below.
        candidate.begin_close()
        if _pending_guarded_history_writes(candidate) or state._slots.get(name) is not candidate:
            candidate.cancel_close()
            if state._slots.get(name) is candidate:
                logger.info(
                    "Cleanup: slot %s has a history write in flight, so it is not idle; "
                    "leaving it for the next sweep",
                    name,
                )
                failed.append(name)
            continue
        removed = state._slots.pop(name, None)
        if not removed:
            candidate.cancel_close()
            continue
        # Same teardown takeover watch as the single-tab close, for the same
        # window: opened before any teardown await so a resume beginning while
        # this pass cancels the task or waits on the archive is observed by
        # every save this iteration dispatches. Closed on every exit of the
        # iteration, ``continue`` included.
        _teardown_basis = SlotRegistry.open_takeover_basis(state, name)
        try:
            # Same tombstone as the single-tab close: the archive pass must not
            # race a concurrent channel reconcile into resurrecting the slot. Its
            # instant is persisted as closed_at for the same teardown-window
            # reason as the single-tab path.
            closed_at = note_slot_closed(state, name)
            # Cancel BEFORE the flush, mirroring the single-tab close at :3271-3276.
            # The flush promotes a held note's context half into ``_pending_context``,
            # and the save below is an await a still-running turn resumes across: it
            # drains and CLEARS that queue, then is cancelled, so the context reaches
            # nobody. Bounded and shielded; a task outliving the timeout still leaves
            # ``running`` true, so the collect branch below hands it to the one
            # batched wait rather than serialising a hung turn's full teardown here.
            _turn_killed = False
            if removed.running and removed.task is not None:
                removed.task.cancel()
                _turn_killed = True
                try:
                    await asyncio.wait_for(asyncio.shield(removed.task), timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            # Post-pop teardown race (same as the single-tab close, same gate): across
            # the cancel await above a concurrent same-key recreate can mint a
            # REPLACEMENT slot under `name`. When that replacement writes the SAME
            # transcript, saving THIS (original) slot as closed=True would stamp a
            # conversation it is still using, and the sessions.remove below would tear
            # down the session it now uses. Skip the ARCHIVE — the closed stamp, the
            # session teardown, and the ``archived`` report, which must never claim a
            # live replacement was archived over — while still persisting the original's
            # own unsaved rows and held notes onto the transcript the two share.
            #
            # A replacement that writes a DIFFERENT transcript (an unbound recreate over
            # a channel-, cron- or workflow-linked tab) takes none of that: leaving the
            # linked transcript unarchived is what makes the reconcile pass resurface it,
            # so the archive below runs on the original's own file and only the
            # key-scoped steps yield.
            if _replacement_shares_transcript(state, name, removed):
                # Same obligation as the single-tab hand-over, and here it also covers
                # the notes: this exit skips the ``flush_deferred_notes()`` below, and
                # held notes live nowhere but this popped object. The drain flushes them
                # into the window and writes the whole tail as an OPEN-key save.
                drained = await _persist_handover_tail(
                    state,
                    name,
                    removed,
                    takeover_basis=_teardown_drain_basis(state, name, removed, _teardown_basis),
                )
                # Hand the KEY-SCOPED restricted marker to the replacement on the way
                # out: this exit skips the discard below the save, which is the only
                # thing that would otherwise have cleared the original's.
                _resettle_restricted_key(state, name)
                if not drained.rows_committed:
                    # This frame was the last reference to those rows, so a pass that
                    # said nothing here would report a clean sweep over a slot whose
                    # tail it dropped. ``failed`` is the honest column: the key is not
                    # in ``archived`` either way, and the two together say "not
                    # archived, and something was lost" rather than "nothing to do".
                    failed.append(name)
                continue
            try:
                # Order is unchanged and load-bearing: the cancel above, then the
                # flush, then the save. What the guard adds is failure handling, and
                # the flush shares the save's ``except`` arm rather than logging and
                # falling through. ``_deferred_notes`` has a durable copy in the
                # slot's metadata line, so a note put back by a partial flush is
                # not held ONLY by this popped object — a restart replays the
                # persisted hold. The restore below still
                # matters for THIS gateway lifetime: falling through would write
                # the transcript WITHOUT that note, discard the slot, and still
                # report the key in ``archived`` — delivery deferred to the next
                # restart and reported as success. Sharing the arm restores the
                # slot with its notes still held and reports the key in ``failed``
                # instead.
                removed.flush_deferred_notes()
                archived_ok = await save_slot_off_loop(
                    state,
                    removed,
                    closed=True,
                    closed_at=closed_at,
                    best_effort=False,
                    # Discriminated for the same reason as the single-tab close:
                    # a divergent takeover's moved watch must not refuse the
                    # archive of a transcript it never touches.
                    takeover_basis=_teardown_drain_basis(state, name, removed, _teardown_basis),
                )
            except Exception:
                logger.error(
                    "Cleanup: failed to flush held notes or archive slot %s", name, exc_info=True
                )
                # Restore only if the key is still free or still ours: a recreate that
                # landed while save_slot_off_loop was in flight now owns `name`, and
                # blindly writing `state._slots[name] = removed` would clobber that
                # live replacement with the failed original. Skip the restore in that
                # case; the error-row / dead-task handling below still applies to the
                # original object we hold.
                if _slot_still_ours(state, name, removed):
                    state._slots[name] = removed
                    # The slot is live under its own name again, so its close is
                    # over: release the admission fence or the restored tab refuses
                    # every regenerate, edit-resend and rewind for good.
                    removed.cancel_close()
                else:
                    # The restore is what this arm's own comment relies on to keep the
                    # flushed notes reachable ("restores the slot with its notes still
                    # held"). Skipping it for a live replacement removes that guarantee,
                    # so drain the tail — flushed notes included — onto the slot's own
                    # transcript instead of dropping the only object holding it.
                    #
                    # Deliberately BEFORE the error row appended below, and that row is
                    # deliberately left in memory on this branch: "the tab was kept" is
                    # false here (the replacement's tab is the one on screen), so
                    # persisting it would put a lie on a transcript a live slot may hold.
                    # The ``failed`` report is what carries the outcome instead — which
                    # is also why the drain's answer needs no branch here, unlike at the
                    # pre-save exit above: this key reaches ``failed`` regardless.
                    await _persist_handover_tail(
                        state,
                        name,
                        removed,
                        takeover_basis=_teardown_drain_basis(state, name, removed, _teardown_basis),
                    )
                # Either way the key-scoped restricted marker must describe whoever holds
                # `name` now — the restored original, or the replacement that kept it.
                # This arm never reaches the discard below, so it settles it here.
                _resettle_restricted_key(state, name)
                # Restoring the slot does not undo the cancel above, and ``running`` is
                # derived from the task, so a cancel that already completed reads False:
                # the tab returns looking idle and dispatchable with that turn's output
                # silently gone. Report it as an error row instead, and drop the dead
                # task so nothing downstream treats it as this slot's live turn. A task
                # that outlived the shielded wait is still running, so the restore loses
                # nothing there and this stays quiet.
                if _turn_killed and removed.task is not None and removed.task.done():
                    removed.task = None
                    removed.append(
                        "error",
                        "⚠️ Archiving this tab failed after its running turn was "
                        "cancelled. The tab was kept, but that turn did not finish "
                        "-- re-send to continue.",
                        "msg msg-err",
                    )
                failed.append(name)
                continue
            else:
                if not archived_ok and await asyncio.to_thread(
                    session_deletion_confirmed, state, removed
                ):
                    # Definitive evidence only, same rule as the single-tab
                    # close: the delete already disposed of what this pass was
                    # archiving, while an unverifiable probe lands in the retry
                    # arm below. Probed BEFORE the takeover branch, also as in
                    # the single-tab close: a delete-won refusal coinciding
                    # with a moved watch must not enter the drain, whose
                    # unguarded append recreates the deleted transcript.
                    logger.info(
                        "Cleanup: archive for slot %s skipped, the session was "
                        "deleted while the archive awaited the lock",
                        name,
                    )
                elif not archived_ok and SlotRegistry.basis_moved(
                    state, _teardown_drain_basis(state, name, removed, _teardown_basis)
                ):
                    # A refusal is not a commit: the teardown watch moved, so a
                    # same-key takeover won the ordering and the archive wrote
                    # nothing. Same exit as the published-replacement branch
                    # above — drain the original's own tail under the same
                    # watch, hand over the key-scoped marker, and report the
                    # key in ``failed`` when the rows did not land; ``archived``
                    # must not name a conversation whose archive never
                    # committed. The DISCRIMINATED basis, matching the one the
                    # archive save was handed: a divergent takeover that never
                    # touched this slot's routed transcript maps to ``None``,
                    # so an unrelated refusal routes to the retry arm instead
                    # of being mislabeled a settled takeover.
                    drained = await _persist_handover_tail(
                        state,
                        name,
                        removed,
                        takeover_basis=_teardown_drain_basis(state, name, removed, _teardown_basis),
                    )
                    if not drained.rows_committed:
                        # Same as the single-tab close: the takeover may be
                        # unpublished and the key free, so the popped original
                        # -- the only holder of its unsaved tail -- comes back
                        # rather than being dropped, with its close over
                        # (``cancel_close`` releases the admission fence, as
                        # the raised arm below does). ``failed`` carries the
                        # outcome either way.
                        if _slot_still_ours(state, name, removed):
                            state._slots[name] = removed
                            removed.cancel_close()
                        _resettle_restricted_key(state, name)
                        failed.append(name)
                        continue
                    _resettle_restricted_key(state, name)
                    continue
                elif not archived_ok:
                    # Neither a takeover nor a verified deletion: a retryable
                    # refusal (the stale-queue guard, for one). Same handling
                    # as a save that raised — restore the tab when the key is
                    # still free (the periodic flush then retries the write),
                    # otherwise drain the tail — and report the key in
                    # ``failed``, never in ``archived``.
                    logger.warning(
                        "Cleanup: archive for slot %s refused (not a deletion, not a "
                        "takeover); reporting it failed",
                        name,
                    )
                    if _slot_still_ours(state, name, removed):
                        state._slots[name] = removed
                        # Live under its own name again, so its close is over:
                        # release the admission fence, as the raised arm does.
                        removed.cancel_close()
                    else:
                        await _persist_handover_tail(
                            state,
                            name,
                            removed,
                            takeover_basis=_teardown_drain_basis(
                                state, name, removed, _teardown_basis
                            ),
                        )
                    _resettle_restricted_key(state, name)
                    failed.append(name)
                    continue
                # Through the shared postcondition rather than a bare discard, for the
                # same reason as the single-tab close: an archive that succeeded onto a
                # key a recreate has since taken must leave the marker describing the
                # REPLACEMENT, not the original it just wrote out.
                _resettle_restricted_key(state, name)
        finally:
            SlotRegistry.close_takeover_basis(state, _teardown_basis)
        # Re-check identity ONE more time, and KEY-scoped here rather than
        # transcript-scoped: `_history_key_for(name)` is the session an unbound
        # replacement runs on, so a recreate landing between the save above and here
        # would have its session torn down. Skip the teardown, and do NOT report the
        # key archived — ``archived`` names SLOT KEYS, and this one has a live holder
        # whatever became of the transcript, so listing it would tell the UI a tab on
        # screen was swept. Move on without touching the replacement's session or its
        # running task.
        if not _slot_still_ours(state, name, removed):
            continue
        # Session cleanup is best-effort — history is already written.
        try:
            await state.sessions.remove(_history_key_for(name))
        except Exception:
            logger.warning("Cleanup: session remove failed for %s", name, exc_info=True)
        else:
            _release_closed_execution(state, removed, closing_key, closing_execution)
        archived.append(name)
        # Collect running tasks for concurrent cancellation after the loop
        if removed.running and removed.task is not None:
            removed.task.cancel()
            _tasks_to_cancel.append(removed.task)
    # Await all cancelled tasks concurrently with a single bounded timeout
    if _tasks_to_cancel:
        await asyncio.wait(_tasks_to_cancel, timeout=5.0)
    if archived:
        _sync_dashboard_slots(state)
        state.push_slots_update()
        state.push_refresh("history")
    if not failed:
        cleanup_outcome = "ok"
    elif archived:
        cleanup_outcome = "partial"
    else:
        cleanup_outcome = "error"
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slots_cleanup",
        outcome=cleanup_outcome,
        source="dashboard",
        resources=f"archived={len(archived)} failed={len(failed)} threshold={max_days}d keys={','.join(archived[:10])}",
    )
    return web.json_response(
        {"ok": True, "archived": len(archived), "keys": archived, "failed": failed}
    )
