"""Recovery policy for a dashboard turn: empty-turn continuation, error cards, retry delays and stop gates."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _COMPACTION_CONTINUE_MSG,
        _PROMISE_ONLY_CONTINUE_MSG,
        _SYNTHETIC_RECOVERY_MSGS,
        AUTH_REQUIRED_KIND,
        FALSE_TOOL_BLOCKER_REPLAY_KIND,
        MODEL_UNENTITLED_KIND,
        SESSION_START_FAILED_KIND,
        SUBAGENT_COMPLETION_KIND,
        SUBAGENT_SYNTHESIS_PROMPT,
        SYNTHETIC_RECOVERY_KIND,
        TRANSIENT_RETRY_KIND,
        USAGE_LIMIT_KIND,
        DashboardState,
        KiroCrewConfig,
        ResetCause,
        _ChatSlot,
        _has_user_queued_followup,
        _remove_queued_by_id,
        build_recovery_requeue,
        effective_session_key,
        is_synthetic_payload_item,
        logger,
        model_is_unusable,
    )
    from kiro_crew.dashboard.recovery_replays import ConsumePhase, LiveSlot, ReplayFamily


def _empty_auto_continue_enabled() -> bool:
    """Config gate for the empty-response auto-continue rung (default ON —
    the recovery is bounded by :func:`_empty_max_auto_continues` nudges per
    user message and always transcript-visible). Fail-open to the default: a
    config-load hiccup must not disable self-healing mid-incident."""
    try:
        return bool(KiroCrewConfig.load().session.empty_response_auto_continue)
    except Exception:  # pragma: no cover — config load must not break recovery
        return True


def _empty_max_auto_continues() -> int:
    """How many synthetic continue nudges the ladder may queue for one user
    message before the give-up rung (``session.empty_response_max_continues``).

    Default 1 — exactly the pre-knob behavior. Raising it helps during a
    provider-instability window where each continuation makes real forward
    progress before dying the same way: one continuation abandons a
    task that three finish. The loader clamps the persisted value to a sane
    range; fail-open to the default here for the same reason as the gate
    above — a config-load hiccup must not disable self-healing mid-incident.
    """
    try:
        return int(KiroCrewConfig.load().session.empty_response_max_continues)
    except Exception:  # pragma: no cover — config load must not break recovery
        return 1


def _answer_text_only(segment_text: str, notice_chunks: list[str]) -> str:
    """*segment_text* with the turn's recorded backend control notices removed.

    A control notice (the claude adapter's "Compacting...") arrives as ordinary
    assistant text and is deliberately left in the text path: it streams,
    flushes and persists like any other chunk, so the user still sees what the
    backend said. Two earlier shapes tried to keep it out of the accumulator
    instead, and each was defeated by a different layer -- the rolling redactor
    withholds a trailing run until the next feed, and the one terminal flush is
    itself guarded on this same accumulator being non-empty, so a notice-only
    turn emitted nothing at all.

    Only the post-compaction gate needs to distinguish a notice from the turn's
    own ANSWER, so the subtraction happens here, at that single call site, and
    nowhere else. Each recorded chunk is removed ONCE: they are the exact
    strings that were appended, so this reconstructs what the turn contributed
    of its own rather than pattern-matching prose.
    """
    answer = segment_text
    for chunk in notice_chunks:
        if chunk:
            answer = answer.replace(chunk, "", 1)
    return answer


def _model_unentitled_meta(exc: BaseException) -> dict[str, object] | None:
    """Row ``meta`` for a terminal error caused by a model the account cannot use.

    ``_raise_acp_error`` tags a prompt-time model rejection with the rejected id
    and the session's advertised list. The rejection is an ENTITLEMENT failure
    (rather than a transient capacity blip on an advertised model) exactly when
    the id is missing from that list — the same test ``_model_is_unentitled``
    applies when it words the message, so the tag and the prose cannot disagree.
    Returns None for every other error, so callers can pass the result straight
    to ``slot.append(meta=...)``.

    Only the ``kind`` is persisted — the same shape every ``TRANSIENT_RETRY_KIND``
    append uses. The frontend reads nothing else: the rejected id and the served
    list are already in the row's prose, and the picker shows the live list.
    """
    rejected = getattr(exc, "rejected_model", None)
    if not isinstance(rejected, str) or not rejected.strip():
        return None
    # ONE shared predicate (see its docstring): an empty/None advertised list is
    # "unknowable" and answers False, which is the None this path wants — the
    # formatter treats that case as transient wording too, so no fix affordance.
    if not model_is_unusable(rejected, getattr(exc, "advertised", None)):
        return None
    return {"kind": MODEL_UNENTITLED_KIND}


def _note_cycle_start_failure(slot_key: str, exc: BaseException, *, self_wake: bool) -> None:
    """Report a cycle that never obtained a model session to its nudge loop.

    ``self_wake`` is the fire path's own marker: only the loop's OWN cycle may
    spend its stand-down budget, or a human turn that happened to be starved on a
    slot that also carries a loop would stop one the human never drove.

    This does not ask whose failure the death was, so a shared runtime dying
    during a session start advances the streak of every loop on that process.
    Asking requires the runtime the allocation was attempting, and nothing in
    scope at a start failure names it: the provider does not exist yet -- that is
    what failed -- and the exception carrying the tag carries no runtime identity.
    Closing it belongs at the raise sites, which would have to carry that
    identity.

    The tag is read with ``getattr`` because the two ACP exception families carry
    it independently and share no base -- which is also why this is a helper
    rather than one inline block: a session start on the shared runtime raises an
    ``AcpRuntimeError`` subclass and lands in a different terminal branch from an
    ``AcpError``, and a report present in only one of them misses whichever
    population the other serves.

    Best-effort: a monitoring convenience never changes how this turn's failure is
    reported.
    """
    if not self_wake or getattr(exc, "session_start_failed", False) is not True:
        return
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_start_fail_get,  # circular: autonudge -> dashboard.chat -> chat_runner
        )

        svc = _autonudge_start_fail_get()
        if svc is not None:
            svc.notify_cycle_start_failed(slot_key)
    except Exception:
        logger.debug("autonudge.notify_cycle_start_failed failed", exc_info=True)


async def _note_cycle_failure(
    slot_key: str,
    exc: BaseException,
    *,
    self_wake: bool,
    loop_id: str,
    expected_generation: int,
    err_meta: object = None,
) -> None:
    """Report a cycle that reached a model session and then DIED to its loop.

    The generic superset the three narrow bounds miss: a self-wake cycle that
    dispatched and then died terminally -- a backend error after retries were
    spent, a persistent tool error, a prompt timeout -- rather than one of the
    specific deterministic rejections, approval stalls or never-got-a-session
    streaks that already have their own stand-down. Called from BOTH terminal
    arms (an ``AcpError`` and the generic ``except Exception`` that catches the
    ``AcpRuntimeError`` timeout family and plain errors), which is why this is a
    helper and not an inline block: a report in only one arm would miss whichever
    death the other serves, and the gate below -- the part with the regression
    risk -- is then tested once instead of twice.

    The gate, in the one place it is defined:

    * ``self_wake`` -- only the loop's OWN delivered cycle counts; a human turn
      that happened to error on a slot that also carries a loop must not spend
      the loop's stand-down budget. Same marker ``_note_cycle_start_failure``
      relies on.
    * NOT a session-start failure -- that has its own streak
      (``notify_cycle_start_failed``); the ``session_start_failed`` tag is read
      with ``getattr`` because the two ACP families carry it independently.
    * NOT a structural rejection -- that is one deterministic turn with its own
      terminal stop (``structural_terminal`` tag); counting it here would
      double-charge the same fault.
    * NOT a pre-dispatch fault -- a memory store that would not open
      (``memory_unavailable``) or a member agent file changed out of band
      (``materialization_changed``) never reached a session at all, so the
      "reached a session and then died" stand-down (and the operator notice that
      names a backend/tool/timeout cause) must not fire for them. Identified by
      the already-resolved row meta, so no new classification is needed. A plain
      internal bug is NOT excluded: a self-wake cycle that dispatches and raises
      the same bug every interval is exactly the no-progress waste this bound
      ends.

    Scoped to the fired loop by BOTH its id (``loop_id``) AND its config
    generation (``expected_generation``), captured at fire time, so neither a
    stale completion of a since-revised loop (A->B->A) nor a stale completion of
    a loop since REPLACED by a fresh one on the same slot and generation can
    charge the loop live on the slot now -- the same ``(id, generation)`` fence
    the structural verdict uses. The service matches both under its lock.

    Awaited, not fire-and-forget: the service records the charge and awaits its
    durable write before this returns, so the stand-down never reads a count the
    store has not accepted. This is possible because the terminal arm is async;
    the sync turn-lifecycle hooks that reassign a deadline cannot await and keep
    their detached write. Best-effort: a monitoring convenience never changes how
    this turn is reported, so any failure here is swallowed.
    """
    if not self_wake:
        return
    if getattr(exc, "session_start_failed", False) is True:
        return
    if getattr(exc, "structural_terminal", False):
        return
    if isinstance(err_meta, dict) and err_meta.get("code") in (
        "memory_unavailable",
        "materialization_changed",
    ):
        return
    if not loop_id:
        # No fired loop identity (not a real self-wake fire): nothing to scope
        # the charge to, so there is nothing to record.
        return
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_failed_get,  # circular: autonudge -> dashboard.chat -> chat_runner
        )

        svc = _autonudge_failed_get()
        if svc is not None:
            await svc.notify_cycle_failed(
                slot_key, loop_id=loop_id, expected_generation=expected_generation
            )
    except Exception:
        logger.debug("autonudge.notify_cycle_failed failed", exc_info=True)


def _terminal_error_meta(exc: BaseException) -> dict[str, object] | None:
    """Row-level kind for a terminal ACP error, or None for a plain error row.

    Four structural tags, read here without looking at the prose. Three are set
    by ``_raise_acp_error`` from the raw frame: a model-entitlement rejection
    (``rejected_model`` / ``advertised``), a sign-in failure (``auth_required``)
    and a spent plan allowance (``usage_limit``). The entitlement verdict wins
    when it is set, because its fix (pick a served model) is the one the prose
    describes; the other two are exclusive at raise time. The fourth,
    ``session_start_failed``, is set at the ACP session-start timeout raise
    sites on BOTH exception families (``AcpError`` on the dedicated client,
    ``AcpRequestTimeout`` on the shared runtime, which share no base -- hence
    ``getattr``) and ranks last: a start that failed because the process is
    signed out has a fix, and that fix is what the card should show.
    """
    unentitled = _model_unentitled_meta(exc)
    if unentitled is not None:
        return unentitled
    if getattr(exc, "auth_required", False):
        return {"kind": AUTH_REQUIRED_KIND}
    if getattr(exc, "usage_limit", False):
        return {"kind": USAGE_LIMIT_KIND}
    if getattr(exc, "session_start_failed", False) is True:
        return {"kind": SESSION_START_FAILED_KIND}
    return None


async def _recovery_delay(secs: float) -> None:
    """Sleep before a recovery re-queue; a module seam so tests replace the wait.

    Every caller's delay is a multi-second floor (the L1 ladder hint, the
    transient backoff curve), so a pin that must deliver an interrupt DURING the
    wait needs a seam here rather than a process-wide ``asyncio.sleep`` patch.
    """
    if secs > 0:
        await asyncio.sleep(secs)


def _shared_dependency_delay(exc: BaseException, local_delay: float, *, slot_key: str) -> float:
    """The delay a transient provider error should wait: the LOCAL backoff, floored
    by the dependency coordinator's shared schedule for the error's scope.

    The main chat holds no task row, so it never joins the coordinator's
    schedule (that would persist wait events for a row that does not exist);
    it reads the scope's ``retry_at`` so five sessions throttled by one
    provider wait out ONE cooldown instead of five. A typed throttle is also
    reported to the adaptive controller (``record_provider_throttle``): a
    provider 429 is a per-provider signal there, never a host signal.
    """
    from kiro_crew.taskq.dependency import (
        KIND_CONCURRENCY_EXCEEDED,
        KIND_RATE_LIMITED,
        classify_exception,
        shared_retry_at,
    )

    try:
        signal = classify_exception(exc)
    except Exception:
        return local_delay
    if signal is None or signal.terminal:
        return local_delay
    if signal.kind in (KIND_RATE_LIMITED, KIND_CONCURRENCY_EXCEEDED):
        try:
            from kiro_crew.adaptive.controller import current as _current_controller

            controller = _current_controller()
            if controller is not None:
                controller.record_provider_throttle(signal.dependency_scope)
        except Exception:
            logger.debug("provider throttle report failed for slot %s", slot_key, exc_info=True)
    delay = float(local_delay)
    if signal.retry_at is not None:
        delay = max(delay, float(signal.retry_at) - time.time())
    shared = shared_retry_at(signal.dependency_scope)
    if shared is not None:
        delay = max(delay, shared - time.time())
    return max(0.0, delay)


def _should_suppress_requeue(slot) -> bool:
    """Return True if a stop is active and re-queue should be suppressed."""
    if slot._stop_state != "idle":
        logger.info("Suppressing re-queue — stop in progress (state=%s)", slot._stop_state)
        return True
    return False


def _current_turn_carries_image_ref(message: str) -> bool:
    """Whether *message* itself names a local image the builder would inline.

    The unsupported-image recovery must only fire for an image retained in
    NATIVE history, because it clears a healthy conversation's resume SID and
    replays the text verbatim. Empty dashboard attachment lists are not that
    proof: a channel turn (``slack/events.py`` appends its attachment paths to
    the text) and a dashboard turn that simply types a path both carry the image
    as a bare path INSIDE the message, and ``build_prompt_blocks`` inlines it as
    a real image block for the CURRENT turn. So read the builder's own scanner on
    the builder's own haystack -- ``_PATH_RE`` over the raw message, image
    suffixes only -- rather than re-deriving the grammar here; a match means the
    rejection may be of the image the user just sent, and the recovery declines.

    Imported inside the function on purpose: ``kiro_crew.image_refs`` documents a
    load-bearing import rule, and this is the shape its other consumers use.
    """
    from kiro_crew.image_refs import _PATH_RE

    return bool(_PATH_RE.search(message or ""))


def _session_stop_generation_for(sessions: Any, session_key: str) -> int:
    """The session manager's Stop count for *session_key*, read defensively.

    ``SessionManager.stop_turn`` bumps it before the provider cancel is
    awaited, on every surface that can stop the session: the dashboard's own
    Stop handler, a linked channel's stop command, a transport's stop verb.
    Test doubles for ``state.sessions`` may lack the method or answer with a
    non-int; both read as 0 so the slot's own Stop signal still decides.
    """
    reader = getattr(sessions, "stop_generation", None)
    if not callable(reader):
        return 0
    try:
        value = reader(session_key)
    except Exception:  # pragma: no cover - a broken double, not a stop
        return 0
    if inspect.iscoroutine(value):
        value.close()
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _replay_live(state: DashboardState, slot: _ChatSlot) -> LiveSlot:
    """The slot as a replay re-check reads it now: binding, Stop state and user input."""
    # The ledger is its own module, imported where it is used: a composed owner's
    # functions read the runner's globals, never this module's.
    from kiro_crew.dashboard.recovery_replays import LiveSlot

    sessions = getattr(state, "sessions", None)
    return LiveSlot(
        session_key=effective_session_key(slot),
        stop_generation=getattr(slot, "_stop_generation", 0),
        stopping=slot._stopping,
        user_input=bool(getattr(slot, "_pending_steers", None)) or _has_user_queued_followup(slot),
        session_stop_generation=lambda key: _session_stop_generation_for(sessions, key),
    )


def _rearm_allowances(slot: _ChatSlot, refunds: tuple[tuple[str, Any], ...]) -> None:
    """Re-arm the one-shot allowances a cancelled replay had spent."""
    for attr, value in refunds:
        setattr(slot, attr, value)


def _drop_queued_replay(state: DashboardState, slot: _ChatSlot, queue_id: str) -> None:
    """Remove a revoked recovery replay's queue entry and its placeholder row."""
    slot.queue_remove_by_id(queue_id)
    if _remove_queued_by_id(slot.messages, queue_id):
        state.broadcast_ws("queue_pop", {"slot": slot.key, "content": "", "queue_id": queue_id})


def _forget_swept_replays(slot: _ChatSlot) -> None:
    """Forget, at the head of the drain, the replays whose entry already left the queue.

    The admission sweep, a merge or any other removal can take a replay's entry
    without touching the ledger: a containment change is not a stop, a rebind or
    user input, so no revocation names it, and an empty queue would return before
    the drain's re-checks run. Done before the queue is read, so a later entry
    cannot inherit the record or its spent allowance.
    """
    from kiro_crew.dashboard.recovery_replays import ENTRY_FAMILIES, POLICIES, replays_of

    head = tuple(f for f in ENTRY_FAMILIES if POLICIES[f].swept_at == "head")
    for family, entry_id in replays_of(slot).sweep((q.get("id") for q in slot._queue), head):
        _rearm_allowances(slot, POLICIES[family].sweep_refunds)
        logger.info(POLICIES[family].sweep_log, slot.key, entry_id)


async def _drop_revoked_replays(state: DashboardState, slot: _ChatSlot) -> bool:
    """Drop each queued replay a Stop, a rebind or user input revoked; True if the queue emptied.

    The drain's re-check, family by family in registry order. A record whose entry
    is gone is forgotten; a revoked one loses its entry and placeholder row, re-arms
    the allowances its family's policy names, and explains itself in the transcript.
    A standing model swap is never unwound here: the next genuine turn's restore
    probe owns that. Nothing suspends -- this coroutine never awaits -- so each
    decision and its dequeue are atomic on the event loop, and a True answer is the
    drain's "nothing left to start".
    """
    from kiro_crew.dashboard.recovery_replays import (
        ENTRY_FAMILIES,
        POLICIES,
        cancel_notice,
        log_args,
        replays_of,
    )

    replays = replays_of(slot)
    for family in ENTRY_FAMILIES:
        entry_id = replays.entry_id(family)
        if not entry_id:
            continue
        policy = POLICIES[family]
        if policy.swept_at == "step" and replays.sweep(
            (q.get("id") for q in slot._queue), (family,)
        ):
            _rearm_allowances(slot, policy.sweep_refunds)
            continue
        revocation = replays.revalidate(family, _replay_live(state, slot))
        if not revocation.revoked:
            continue
        _drop_queued_replay(state, slot, entry_id)
        replays.disarm(family)
        _rearm_allowances(slot, policy.drain_refunds)
        slot.append("notice", cancel_notice(family, revocation), "msg msg-info")
        logger.info(policy.drain_log, slot.key, *log_args(family, revocation))
        if not slot._queue:
            return True
    return False


async def _replay_vetoed_at_consume(
    state: DashboardState,
    slot: _ChatSlot,
    claimed: frozenset[ReplayFamily],
    phase: ConsumePhase,
) -> bool:
    """Whether a claimed replay the consume seam checks in *phase* must not run after all.

    The drain checked it before spawning the task; this re-checks the same record at
    the consume seam, family by family in registry order. True means the replay was
    cancelled -- explained in the transcript -- and the turn returns above its main
    ``try``, so the exit guard runs its tail. A family's record is consumed here, so
    it runs once per dispatch, unless its policy keeps an accepted replay's record
    for the turn's episode settlement.
    """
    from kiro_crew.dashboard.recovery_replays import (
        POLICIES,
        cancel_notice,
        consumed_in,
        log_args,
        replays_of,
    )

    replays = replays_of(slot)
    for family in consumed_in(phase):
        if family not in claimed:
            continue
        policy = POLICIES[family]
        revocation = replays.revalidate(family, _replay_live(state, slot))
        if revocation.revoked or not policy.consume_keeps_record:
            replays.disarm(family)
        if revocation.revoked:
            _rearm_allowances(slot, policy.consume_refunds)
            slot.append("notice", cancel_notice(family, revocation), "msg msg-info")
            logger.info(policy.consume_log, slot.key, *log_args(family, revocation))
            return True
    return False


async def _refresh_genuine_turn_allowances(
    slot: _ChatSlot, message: str, *, _model_access_replay: bool
) -> None:
    """Re-arm the per-turn recovery one-shots for a GENUINE turn.

    A synthetic recovery turn inherits the allowances its recovery already spent.
    The model-access swap's own replay is told apart by ``_model_access_replay``:
    the queue drain claims the model-access family only when the drained entry's id
    matches the replay id the swap recorded.
    """
    if message not in _SYNTHETIC_RECOVERY_MSGS:
        slot._posttoken_retry_used = False
        # Clear the last turn's structural-terminal verdict at the START of a
        # GENUINE new turn. The auto-nudge fire path reads this to refuse
        # re-firing an identical context the backend rejected for its shape;
        # a real new turn (a human /clear then a fresh message, or any turn
        # whose context differs) is exactly the event that should let the loop
        # re-arm, so the flag must not outlive it. A SYNTHETIC recovery turn
        # re-runs the SAME message on a reset session, so it deliberately does
        # NOT clear the flag -- the context that tripped the parser is unchanged.
        slot._last_turn_structural_terminal = False
        slot._last_turn_structural_terminal_loop_id = ""
        slot._last_turn_structural_terminal_loop_gen = 0
        # Same one-shot discipline for the reactive model-access swap. Its
        # recovery replays the user's ORIGINAL message (their words, so it is
        # NOT a synthetic marker and would reset the flag here like any fresh
        # turn), which would re-open the swap on a still-unentitled candidate.
        # Preserve the True flag for exactly that replay, so a genuine later
        # user turn can still earn one swap.
        if not _model_access_replay:
            slot._model_access_fallback_used = False


async def _rearm_turn_episode(
    slot: _ChatSlot,
    message: str,
    *,
    _is_refusal_retry_turn: bool,
    _synthetic_recovery_turn: bool,
    _synthetic_payload: bool,
) -> None:
    """Settle the refusal-retry record and the empty-response episode for this dispatch."""
    from kiro_crew.dashboard.recovery_replays import ReplayFamily, replays_of

    if _is_refusal_retry_turn:
        replays_of(slot).disarm(ReplayFamily.CONTENT_FILTER)
    elif message not in _SYNTHETIC_RECOVERY_MSGS and not _synthetic_recovery_turn:
        # Re-arm the one-retry allowance only for GENUINE user-origin
        # dispatches. A runner requeue of the user's own words (kind-tagged
        # synthetic recovery after a pre-output failure) is the SAME turn
        # retried -- re-arming on it would let a crashing fallback replay
        # re-arm and cycle indefinitely.
        replays_of(slot).disarm(ReplayFamily.CONTENT_FILTER)
        slot._refusal_fallback_attempted = False
    # The episode ends on a dispatch the drain did not re-queue as recovery.
    # `_synthetic_payload` is true for a CONTINUATION-tagged entry and for an untagged
    # one (`is_synthetic_payload_item` falls through to the kind), which is why the
    # auth-required and poisoned-conversation requeues count despite carrying the
    # user's own text. It is false for an ORIGINAL-tagged verbatim replay, which only
    # rung 1 queues -- and rung 1 needs the counter below 1 while the flag is only set
    # with it at 2 or more, so the flag is already clear there.
    # Keyed here, not on the continuation bodies, because three controls discard a
    # queued continuation WITHOUT resetting the counter (the hard-kill Stop's queue
    # clear, a plan Cancel's owner-scoped discard, a rewind commit's rebuild).
    if not _synthetic_payload:
        slot._empty_episode_productive = False


async def _purge_superseded_continuations(state: DashboardState, slot: _ChatSlot) -> bool:
    """Purge auto-continuations a stop, a rebind or user input superseded; True if the queue emptied.

    Every continuation-shaped entry shares the continuation record, so all of them
    go together. Nothing between the decision and the dequeue suspends -- this
    coroutine never awaits -- so the decision is atomic on the event loop. A True
    answer is the drain's "nothing left to start".
    """
    from kiro_crew.dashboard.recovery_replays import (
        POLICIES,
        ReplayFamily,
        cancel_notice,
        replays_of,
    )

    live = _replay_live(state, slot)
    replays = replays_of(slot)
    # A cron injection may rebind an idle slot while a queued continuation waits.
    # It belongs to the session that admitted it, never the new binding; the
    # record's empty binding (nothing queued since the slot was built) is never a
    # rebind signal.
    revocation = replays.revalidate(ReplayFamily.CONTINUATION, live)
    if _should_suppress_requeue(slot) or revocation.revoked:
        # Both auto-continuations carry the same hazard and the same fix: the
        # post-compaction resume would re-drive a request the user has since
        # stopped or replaced. Purge either one, and reset whichever one-shot
        # budget was spent (both resets are idempotent, so no need to tell them
        # apart per item).
        _purgeable = (_PROMISE_ONLY_CONTINUE_MSG, _COMPACTION_CONTINUE_MSG)
        superseded = [
            q
            for q in slot._queue
            if q.get("kind") == FALSE_TOOL_BLOCKER_REPLAY_KIND
            or (is_synthetic_payload_item(q) and q.get("content") in _purgeable)
        ]
        if superseded:
            for q in superseded:
                _drop_queued_replay(state, slot, q["id"])
            # The one-shot budget was spent at enqueue but never dispatched — the
            # episode was aborted; reset it so the user's own next turn keeps its
            # first legitimate recovery. Re-arm the record at the live counters and
            # binding too, so a stale snapshot cannot re-trigger this on a later drain.
            _rearm_allowances(slot, POLICIES[ReplayFamily.CONTINUATION].drain_refunds)
            replays.arm(
                ReplayFamily.CONTINUATION,
                entry_id="",
                session_key=live.session_key,
                stop_gen=live.stop_generation,
                session_stop_gen=live.stop_count_for(live.session_key),
            )
            # The earlier "auto-continuing once" notice and the card's "continuing
            # automatically" detail now stand uncorrected; append a one-line
            # correction so the transcript matches what actually ran.
            slot.append(
                "notice", cancel_notice(ReplayFamily.CONTINUATION, revocation), "msg msg-info"
            )
            logger.info(
                "Purged %d superseded promise-only continuation(s) before dispatch "
                "for slot %s (user_input=%s stop_since_enqueue=%s rebound=%s)",
                len(superseded),
                slot.key,
                revocation.superseded,
                revocation.stop_moved,
                revocation.rebound,
            )
        # A model-access-denial swap re-queues the user's ORIGINAL message, which
        # is not one of the two continuation constants purged above, so a soft
        # Stop (first press, which does NOT clear the queue) or a user follow-up
        # landing after the swap enqueued would otherwise let the cancelled prompt
        # replay from the queue head. ``_drop_revoked_replays``, the next gate,
        # drops it, and runs on the rebind signal too (not just this block's
        # stop/user-input triggers).
        if not slot._queue:
            return True
    return False


async def _requeue_auth_retry(
    slot: _ChatSlot,
    message: str,
    *,
    _synthetic_recovery_turn: bool,
    _synthetic_payload: bool,
    _turn_actor: str,
    _consumed_reported: bool,
    _current_message: dict | None,
    _queue_recovery: Callable[..., str],
) -> None:
    """Put a runner-authored input that hit a signed-out CLI back at the queue head.

    The queue is held intact for after sign-in. A synthetic-recovery, synthesis or
    sub-agent input this turn already popped is restored once, at the queue head,
    with its delivery provenance.
    """
    _auth_retry_kind = ""
    if _synthetic_recovery_turn or (_synthetic_payload and message == SUBAGENT_SYNTHESIS_PROMPT):
        # A synthesis turn is runner-authored even though its ledger actor is
        # ``subagent``. Preserve inject provenance across login; classifying
        # from the broad actor would drain the internal prompt as user text.
        _auth_retry_kind = SYNTHETIC_RECOVERY_KIND
    elif _turn_actor == "subagent":
        _auth_retry_kind = SUBAGENT_COMPLETION_KIND
    if (
        _auth_retry_kind
        and not _consumed_reported
        and not any(
            entry.get("kind") == _auth_retry_kind and entry.get("content") == message
            for entry in slot._queue
        )
    ):
        _current_meta = (
            _current_message.get("meta")
            if _current_message is not None and isinstance(_current_message.get("meta"), dict)
            else None
        )
        _queue_recovery(
            0,
            message,
            kind=_auth_retry_kind,
            extra_meta=_current_meta,
        )


async def _requeue_after_prompt_busy(
    slot: _ChatSlot,
    message: str,
    *,
    _prompt_depth: int,
    _turn_emitted: bool,
    _is_synthetic: bool,
    _queue_recovery: Callable[..., str],
) -> None:
    """Re-queue a turn whose provider was reset after prompt-busy retries ran out.

    Within the busy budget a top-level turn is re-queued on the fresh provider;
    past it, or nested, the transcript says so instead.
    """
    if _should_suppress_requeue(slot):
        pass
    elif _prompt_depth == 0 and slot._prompt_busy_retries <= 3:
        # Single emit: slot.append persists + broadcasts one chat_message
        # via _on_message (see the AcpProcessDied note in ``_run_chat``); no explicit
        # broadcast_ws or the UI shows a duplicate card.
        _retry_msg = "⟳ Session busy — retrying…"
        slot.append("error", _retry_msg, "msg msg-err", meta={"kind": TRANSIENT_RETRY_KIND})
        _requeue_text, _requeue_payload = build_recovery_requeue(
            message,
            _turn_emitted,
            cause=ResetCause.SESSION_BUSY,
            message_is_synthetic=_is_synthetic,
        )
        _queue_recovery(
            0,
            _requeue_text,
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=_requeue_payload,
        )
    elif slot._prompt_busy_retries > 3:
        slot.append("error", "Session stuck — please start a new chat.", "msg msg-err")
    else:
        # depth>0 with budget remaining: no re-queue (mirrors AcpProcessDied),
        # but still surface feedback so the nested turn doesn't fail silently.
        slot.append("error", "⟳ Session busy — please retry.", "msg msg-err")
