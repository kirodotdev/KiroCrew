"""Recovering a dashboard turn whose stream ended badly: the stop reasons it retries on its own
(``_turn_stop_recovery``); and the end of a turn that reached the post-loop chain
(``_turn_persist_and_settle``): its stats footer, file changes, marker retire and save, the
landed turn's reset of every retry budget, and consolidation."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Literal

from kiro_crew.dashboard.chat_utils import (
    TRANSIENT_RETRY_KIND,
    RecoveryPayload,
    ResetCause,
    build_recovery_requeue,
    payload_for_replay,
)
from kiro_crew.dashboard.member_message_notes import post_turn_reply
from kiro_crew.dashboard.state import (
    STALE_RECOVERY_PREFIX,
    TOOL_STALL_RECOVERY_PREFIX,
    build_tool_stall_recovery_prompt,
)
from kiro_crew.recovery.ladder import L1_TOOL_CALL, SESSION_RECOVERY_MAX_ATTEMPTS, default_ladder

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        STOP_CLASS_FAILED,
        STOP_REASON_CANCELLED,
        STOP_REASON_COMPACTION_FAILED,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
        STOP_RECOVERY_MAX_RETRIES,
        SYNTHETIC_RECOVERY_KIND,
        WAIT_REASON_INPUT,
        _maybe_consolidate,
        build_stale_recovery_prompt,
        logger,
        runtime_death,
        save_slot_off_loop,
    )
    from kiro_crew.dashboard.chat_turn.file_changes import _flush_file_changes, _turn_after_reads
    from kiro_crew.dashboard.chat_turn.recovery import (
        _COMPACTION_FAILED_RETRIES,
        _should_suppress_requeue,
        _turn_retry_outcomes,
    )
    from kiro_crew.dashboard.chat_turn.steer_queue import _has_user_queued_followup
    from kiro_crew.dashboard.chat_turn.turn_entry import _stop_pressed
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _queue_recovery
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow
    from kiro_crew.dashboard.chat_turn.turn_marker import _retire_local_turn_marker
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import (
        _emit_error,
        _emit_stale,
        _emit_stall,
        _flush_thinking_stream,
        _persist_partial_reply,
    )
    from kiro_crew.dashboard.chat_turn.turn_stats import (
        _attach_owned_turn_stats,
        _emit_recovery_outcome,
        _turn_outcome,
    )


async def _turn_stop_recovery(turn: _ChatTurn) -> Literal[_Flow.END] | None:
    """Recover from the stop reasons the turn can retry on its own."""
    _is_synthetic = turn._is_synthetic
    _prompt_depth = turn._prompt_depth
    _stall_command = turn._stall_command
    _stall_evidence = turn._stall_evidence
    _stall_status = turn._stall_status
    _stall_tool_title = turn._stall_tool_title
    _stop_class = turn._stop_class
    _stop_reason = turn._stop_reason
    _turn_emitted = turn._turn_emitted
    client = turn.client
    message = turn.message
    needs_session_reset = turn.needs_session_reset
    slot = turn.slot
    # Turn stream ended: flush any withheld thinking tail (a thinking-final turn never
    # hit the loop-top flush in _run_chat's stream loop for a following non-thinking event).
    _flush_thinking_stream(turn)

    # Auto-recover a genuinely-wedged turn. The ACP layer probed a stale turn
    # via session/cancel and got no ack within the grace window — a confirmed
    # wedge (a done-but-missing-frame turn would have acked and completed
    # normally). Reset the session (kill the wedged runtime + session/load
    # resume in _turn_release_session, chat_turn/session_reset.py) and re-queue a
    # continue-nudge so the turn finishes IN PLACE, on this same slot, with NO
    # user message required —
    # the dequeue in _run_chat's finally re-dispatches it against the resumed session,
    # which restores the prior committed work so the model continues rather
    # than restarts. Bounded so a permanently-broken session surfaces a clean
    # "start a new chat" instead of looping. Complementary to a companion guard,
    # which surfaces the stuck sessions this cannot recover.
    if _stop_reason == STOP_REASON_STALE_RECOVER:
        needs_session_reset = True  # checked in _turn_release_session (reset + resume)
        turn.needs_session_reset = needs_session_reset
        # This arm and the three below return before _turn_settle_answer's end-of-turn
        # flush, where the streamed text exists only as chunk rows, which a save
        # never writes. Keep it as the interrupted reply, with its footer,
        # above the recovery card.
        await _persist_partial_reply(turn, _stop_reason)

        if _prompt_depth == 0 and slot._stale_recovery_retries < STOP_RECOVERY_MAX_RETRIES:
            slot._stale_recovery_retries += 1
            _queue_recovery(
                turn,
                0,
                f"{STALE_RECOVERY_PREFIX}\n{build_stale_recovery_prompt()}",
                kind=SYNTHETIC_RECOVERY_KIND,
                payload=RecoveryPayload.CONTINUATION,
            )
            _emit_stale(turn, "⟳ Recovering a stalled turn…", will_retry=True)
        elif slot._stale_recovery_retries >= STOP_RECOVERY_MAX_RETRIES:
            # Budget exhausted — terminal for this slot until a turn
            # actually completes. The budget is deliberately NOT reset
            # here: zeroing it would re-arm a fresh 3-attempt recovery
            # cycle on the next stall of a permanently wedged slot
            # (recover→exhaust looping forever). Telemetry dedup is the
            # emitted flag's job instead: emit exhausted once per cycle,
            # and the flag also blocks a later "recovered" mis-emit.
            if not slot._stale_recovery_exhausted_emitted:
                _emit_recovery_outcome("stale_recover", "exhausted", slot._stale_recovery_retries)
                slot._stale_recovery_exhausted_emitted = True
            _emit_stale(turn, "Session stuck — please start a new chat.")
        else:
            # depth>0 (nested turn) with budget remaining: reset the session
            # but don't re-queue (mirrors the pipe-death depth>0 handling);
            # surface feedback so the nested turn doesn't fail silently.
            _emit_stale(turn, "⟳ Turn stalled — please retry.")
        return _Flow.END

    # Dedicated tool-stall recovery — MUST precede the generic "error:"
    # handler (the stop reason starts with "error:" by design so callers
    # without this branch still get generic handling). The legacy routing
    # re-queued the ORIGINAL user message verbatim: the agent received the
    # full original ask again, restarted the task, re-ran the very command
    # that stalled, stalled again — three cycles of rework ending in
    # "Session stuck". Instead: a continue-nudge that names the stalled
    # tool, points at any redirected log file, and (for stuck-input
    # verdicts) says to re-run non-interactively. Separate retry budget
    # from pipe-death so a stall can never burn the reconnect budget.
    if _stop_reason == STOP_REASON_TOOL_STALL:
        await _persist_partial_reply(turn, _stop_reason)

        _idle_m = re.search(r"idle_secs=(\d+)", _stall_evidence or "")
        _idle_secs = int(_idle_m.group(1)) if _idle_m else 0
        # Typed first: an execution-layer / oracle ``waiting_input`` status
        # is the verdict; the evidence-text marker stays as the fallback for
        # a provider that forwards no status object.
        _stuck = (
            _stall_status is not None and _stall_status.wait_reason == WAIT_REASON_INPUT
        ) or "stuck_input" in (_stall_evidence or "")
        if _prompt_depth == 0 and slot._tool_stall_retries < STOP_RECOVERY_MAX_RETRIES:
            slot._tool_stall_retries += 1
            _body = build_tool_stall_recovery_prompt(
                _stall_tool_title,
                _idle_secs,
                command=_stall_command,
                stuck_input=_stuck,
            )
            _queue_recovery(
                turn,
                0,
                f"{TOOL_STALL_RECOVERY_PREFIX}\n{_body}",
                kind=SYNTHETIC_RECOVERY_KIND,
                payload=RecoveryPayload.CONTINUATION,
            )
            _emit_stall(turn, "⟳ Tool appeared stalled — recovering…", will_retry=True)
        elif slot._tool_stall_retries >= STOP_RECOVERY_MAX_RETRIES:
            # Budget exhausted — mirrors the stale_recover branch above:
            # budget left alone (a wedged slot must not re-enter a fresh
            # recovery cycle); the emitted flag dedups the metric and
            # blocks a later "recovered" mis-emit.
            if not slot._tool_stall_exhausted_emitted:
                _emit_recovery_outcome("tool_stall", "exhausted", slot._tool_stall_retries)
                slot._tool_stall_exhausted_emitted = True
            _emit_stall(turn, "Session stuck — please start a new chat.")
        else:
            _emit_stall(turn, "⟳ Tool appeared stalled — please retry.")
        return _Flow.END

    # Automatic compaction failed and the backend then abandoned the turn.
    # Not reaching the branch below is load-bearing: this reason is in the
    # "error:" family, and that branch is pipe-death recovery — it would
    # label the requeue "Connection lost", which is not what happened. The
    # session reset IS needed either way: this completion is synthetic (the
    # client stopped reading; the backend never sent end_turn), so the
    # backend still counts the turn as in progress and the next prompt
    # would collide with "prompt already in progress". The reset in _turn_release_session
    # (chat_turn/session_reset.py) tears that runtime down and session/load-resumes.
    #
    # Whether the abandoned message is re-queued depends on WHY compaction
    # failed, which is the whole point of the verdict the ACP layer
    # records. A compaction that overflowed the window fails again
    # identically, so replaying it just burns the budget — that is the case
    # the unconditional return was written for. A compaction whose
    # summarization call was throttled or 5xx'd has nothing wrong with it,
    # and dropping the user's message for it silently ends the turn on a
    # backend hiccup the very next attempt would clear.
    if _stop_reason == STOP_REASON_COMPACTION_FAILED:
        needs_session_reset = True  # checked in _turn_release_session (chat_turn/session_reset.py)
        turn.needs_session_reset = needs_session_reset
        await _persist_partial_reply(turn, _stop_reason)
        if (
            # Attribute, not a stop-reason variant: the reason is the ACP
            # layer's to classify, and both client classes record it.
            # Compared against True rather than read for truthiness: the
            # retry must require a real verdict, so a provider that has
            # never set the attribute (or exposes an auto-created stand-in
            # for it) cannot be read as "transient" by accident.
            getattr(client, "last_compaction_transient", False) is True
            # Verbatim replay is only safe before anything streamed —
            # exactly the guard the transient-5xx sibling in _run_chat's AcpError
            # arm uses. Once output or a tool call has landed, re-sending the message could
            # repeat a side effect, so an emitted turn keeps the old
            # give-up behaviour rather than inventing a continuation.
            and not _turn_emitted
            and _prompt_depth == 0
            and slot._compaction_failed_retries < _COMPACTION_FAILED_RETRIES
            # The USER'S INTENT WINS over this recovery. A requeue lands at
            # queue index 0, so without these four checks a message the user
            # has since stopped or replaced would run BEFORE the correction
            # they typed. Same hazard and same guard as the promise-only
            # continuation in _turn_ending_chain: a live stop (`_should_suppress_requeue` /
            # `_stopping`), a stop that COMPLETED during this turn (both
            # flags snap back to idle, so only the monotonic generation
            # counter sees it), a pending steer, or a user-authored queue
            # entry each mean the turn must stay abandoned.
            and not _should_suppress_requeue(slot)
            and not _stop_pressed(turn)
            and not bool(getattr(slot, "_pending_steers", None))
            and not _has_user_queued_followup(slot)
        ):
            slot._compaction_failed_retries += 1
            # No reason interpolated here: each arming site already logs the
            # whole frame at WARNING when the failure arrives, so repeating a
            # truncated copy is the only thing the forwarded reason string
            # would have bought.
            logger.info(
                "Transient compaction failure in slot %s (attempt %d/%d) — "
                "re-queuing the abandoned message",
                slot.key,
                slot._compaction_failed_retries,
                _COMPACTION_FAILED_RETRIES,
            )
            # No backoff call here, matching the pipe-death sibling in this
            # function: the session teardown + session/load replay in
            # _turn_release_session (chat_turn/session_reset.py) already sits between this queue
            # and the retry.
            _queue_recovery(
                turn,
                0,
                message,
                kind=SYNTHETIC_RECOVERY_KIND,
                # Verbatim replay, so ORIGINAL only when the incoming text
                # was the user's own — on a recovery turn it is the
                # runner's continuation.
                payload=payload_for_replay(_is_synthetic),
            )
            # A recovery IS queued, so this notice is not terminal: the tag
            # stops the UI offering a retry that re-runs itself. The
            # compaction-status path already appended the row naming the
            # reason, so this one only reports the retry.
            slot.append(
                "error",
                "⟳ Compaction failed — retrying…",
                "msg msg-err",
                meta={"kind": TRANSIENT_RETRY_KIND},
            )
        return _Flow.END

    # CC process died mid-turn: re-queue message for automatic retry
    # (mirrors AcpProcessDied handling). Eager reconnect in the provider
    # restores MCPs in background; re-queue ensures the user's message
    # is not silently dropped.
    # The RETRYABLE `failed` class is exactly the generic "error:" family: a
    # tool stall, a compaction failure and a refusal have already been
    # handled above (or are non-retryable), so only transport / process
    # death reaches this re-queue.
    if _stop_class.name == STOP_CLASS_FAILED and _stop_class.retryable:
        await _persist_partial_reply(turn, _stop_reason)
        _rc = getattr(client, "exit_code", None)
        _rc_suffix = f" (exit {_rc})" if _rc is not None else ""

        # Attribution, as in the AcpProcessDied handler: a process this slot
        # shared dying is not this slot's failure, so it advances the
        # shared-death streak instead of this slot's recovery budget. Both
        # are bounded by the same ladder limit, so the re-queue stays bounded
        # either way, and a single-tenant runtime is unchanged.
        _own_fault = runtime_death.caused_by_this_session(client)
        # Whichever count is further along bounds the re-queue. Reading only
        # the shared streak here would REFUND budget the slot already spent:
        # after three own-fault deaths a shared one would read as 1 and hand
        # the slot fresh attempts it had used up.
        _death_attempts = max(slot._acp_pipe_death_retries, runtime_death.shared_deaths(slot.key))
        if _prompt_depth == 0 and _death_attempts < SESSION_RECOVERY_MAX_ATTEMPTS:
            if _own_fault:
                slot._acp_pipe_death_retries += 1
            else:
                runtime_death.note_shared_death(slot.key)
            _requeue_text, _requeue_payload = build_recovery_requeue(
                message,
                _turn_emitted,
                cause=ResetCause.CONNECTION_LOST,
                message_is_synthetic=_is_synthetic,
            )
            _queue_recovery(
                turn,
                0,
                _requeue_text,
                kind=SYNTHETIC_RECOVERY_KIND,
                payload=_requeue_payload,
            )
            _emit_error(turn, f"⟳ Connection lost{_rc_suffix} — retrying...", will_retry=True)
        elif _death_attempts >= SESSION_RECOVERY_MAX_ATTEMPTS:
            if _own_fault:
                _emit_error(turn, f"Session stuck{_rc_suffix} — please start a new chat.")
            else:
                _emit_error(
                    turn,
                    f"The agent process this chat shares kept restarting{_rc_suffix}"
                    " — please retry.",
                )
        else:
            _emit_error(turn, f"⟳ Connection lost{_rc_suffix} — please retry.")
        return _Flow.END
    return None


async def _turn_persist_and_settle(turn: _ChatTurn) -> None:
    """A turn's end after the post-loop chain: footer, files, marker, save, landed budgets, consolidation."""
    _is_monitor_wake = turn._is_monitor_wake
    _l1_escalated = turn._l1_escalated
    _l1_interrupted = turn._l1_interrupted
    _local_turn_marker_generation = turn._local_turn_marker_generation
    _noticed_leak = turn._noticed_leak
    _prompt_depth = turn._prompt_depth
    _recovering_compaction = turn._recovering_compaction
    _recovering_infra = turn._recovering_infra
    _recovering_promise = turn._recovering_promise
    _refusal_reasons = turn._refusal_reasons
    _retrying_empty = turn._retrying_empty
    _shell_tool_calls = turn._shell_tool_calls
    _stop_reason = turn._stop_reason
    _ttft_visible = turn._ttft_visible
    _turn_cost_usd = turn._turn_cost_usd
    _turn_credits = turn._turn_credits
    _turn_elapsed_ms = turn._turn_elapsed_ms
    _turn_model = turn._turn_model
    _turn_msg_boundary = turn._turn_msg_boundary
    _turn_start_mid = turn._turn_start_mid
    slot = turn.slot
    state = turn.state
    if not _retrying_empty:
        # Attach per-turn stats (elapsed / credits) to the last assistant
        # message so the footer can show them (parity with kiro-cli).
        # Scoped to this turn's messages via _turn_msg_boundary, and to the
        # rows this runner appended when it appended any: a workflow result
        # another writer appended mid-turn must not take this turn's figures.
        # A turn whose only row is not a recorded reply (a command's own
        # confirmation) keeps the plain boundary scan.
        _reply_mids = getattr(slot, "_turn_reply_mids", None)
        _stats_attached = _attach_owned_turn_stats(
            slot,
            _turn_elapsed_ms,
            _turn_credits,
            _turn_cost_usd,
            turn_boundary=_turn_msg_boundary,
            model=_turn_model,
            ttft_ms=_ttft_visible.ms,
            reply_mids=_reply_mids if isinstance(_reply_mids, list) and _reply_mids else None,
            retries=sum(slot._turn_retries.values()) if _prompt_depth == 0 else 0,
        )
        if _prompt_depth == 0 and _stats_attached:
            slot._carried_ttft_clock = None
        # Attach accumulated file changes to this turn's assistant row before persist.
        # The turn-end disk reads and diffs (a whole file of up to two megabytes
        # for a large edited file) run off the event loop; the flush then applies
        # them to the slot here, on the loop. The teardown flush in
        # _turn_release_handles (chat_turn/turn_entry.py) reads inline without them: it
        # runs where an await could skip the session-permit release, so it keeps the
        # ordinary capped read only.
        _changes_now = getattr(slot, "_file_changes", None)
        _after_reads = (
            await asyncio.to_thread(_turn_after_reads, list(_changes_now), bool(_shell_tool_calls))
            if isinstance(_changes_now, list) and _changes_now
            else None
        )
        # Copied before the flush, which clears the reply ids on exit.
        _note_reply_mids = list(_reply_mids) if isinstance(_reply_mids, list) else []
        # The Slack mirror leg (_turn_stop_hook) runs AFTER this flush, so it reads
        # the snapshot rather than the cleared live list to publish the whole
        # answer (see _turn_full_reply_text).
        turn._reply_mids_snapshot = _note_reply_mids
        _flush_file_changes(
            slot,
            turn_boundary=_turn_msg_boundary,
            turn_start_mid=_turn_start_mid,
            turn_had_shell=bool(_shell_tool_calls),
            after_reads=_after_reads,
        )
        # The reply is in the window, so this save is the durable clear of
        # the in-flight marker: retire it first and the omission rides the
        # same write instead of costing a second one in _run_chat's finally. Not
        # for a provider-side cancel: a shutdown cancels the provider the
        # same way, persists the partial reply as an ordinary row, and the
        # shutdown check in _run_chat's finally must still see the marker to preserve it.
        if _stop_reason != STOP_REASON_CANCELLED:
            _retire_local_turn_marker(slot, _local_turn_marker_generation)
        # Save to history and trigger memory consolidation
        await save_slot_off_loop(state, slot)
        # A crewmate's answer in its DM thread is one notification per
        # landed turn. Not for a turn the user stopped (they are here), nor
        # for one about to re-run (its re-run's reply is the answer).
        if _stop_reason != STOP_REASON_CANCELLED and not (
            _recovering_promise or _recovering_compaction or _noticed_leak or _recovering_infra
        ):
            post_turn_reply(state, slot, _note_reply_mids)
    # Reset ALL retry budgets once the cycle completes (success OR the
    # terminal second-empty error) so each new user turn gets fresh budgets.
    # Guarded by _retrying_empty, _recovering_promise and _noticed_leak:
    # neither a re-queue nor an unacted turn is a landed turn, so all must
    # preserve the counters (an unacted turn that reset budgets would also
    # mask the transient-failure retry accounting).
    if (
        not _retrying_empty
        and not _recovering_promise
        and not _recovering_compaction
        and not _noticed_leak
        and not _recovering_infra
    ):
        # A landed turn closes this slot's L1 run of infrastructure failures
        # (a no-op when none was open); the ladder measures the outage. An
        # ESCALATED run is closed with forget instead: the run ended spent,
        # so the next user turn gets a fresh budget without a recovery
        # duration being measured for an outage that never closed. So is a
        # run the user INTERRUPTED during the backoff — the retry never ran,
        # so nothing here observed the dependency come back.
        if _l1_escalated or _l1_interrupted:
            default_ladder().forget(L1_TOOL_CALL, slot.key)
        else:
            default_ladder().observe_success(L1_TOOL_CALL, slot.key)
        # A non-zero stall budget reaching this reset on an OK turn is a
        # COMPLETED recovery cycle: the stall branches of _turn_stop_recovery return
        # early, so the only way here with an armed budget is the synthetic recovery
        # turn finishing cleanly. Emit outcome=recovered with the attempt count
        # read BEFORE the reset (the exhausted counterpart lives in those
        # stall branches). Gated on the ok outcome so a user cancelling the
        # recovery turn is never counted as a successful recovery.
        if _turn_outcome(_stop_reason) == "ok":
            # An armed budget whose cycle already emitted "exhausted" is
            # not a recovery — the flag blocks the mis-emit (the budget is
            # not zeroed at exhaustion, so it can reach here armed).
            if slot._stale_recovery_retries > 0 and not slot._stale_recovery_exhausted_emitted:
                _emit_recovery_outcome("stale_recover", "recovered", slot._stale_recovery_retries)
            if slot._tool_stall_retries > 0 and not slot._tool_stall_exhausted_emitted:
                _emit_recovery_outcome("tool_stall", "recovered", slot._tool_stall_retries)
            if _prompt_depth == 0:
                _turn_retry_outcomes(slot, "recovered")
        slot._empty_response_retries = 0
        slot._empty_episode_productive = False
        slot._prompt_busy_retries = 0
        slot._acp_pipe_death_retries = 0
        slot._stale_recovery_retries = 0
        slot._tool_stall_retries = 0
        slot._compaction_failed_retries = 0
        slot._stale_recovery_exhausted_emitted = False
        slot._tool_stall_exhausted_emitted = False
        slot._transient_5xx_retries = 0
        slot._infra_retries = 0
        if _prompt_depth == 0:
            slot._turn_retries = {}
        # Same evidence, same reason, for the streak of deaths that belonged
        # to a process this slot was sharing rather than to the slot itself:
        # a landed turn proves recovery worked, so the next shared death
        # starts its own count instead of inheriting one.
        runtime_death.clear_shared_deaths(slot.key)
        # A turn that LANDED proves this session can start, so clear any
        # start-failure streak a monitoring loop bound to this slot recorded.
        # Any landed turn counts, a human's as much as a cycle's: the streak
        # only ever slows or stops a loop, so clearing it on the broader
        # evidence can only keep a working loop running.
        try:
            from kiro_crew.autonudge import (
                get_instance as _autonudge_landed_get,  # circular: autonudge -> dashboard.chat -> chat_runner
            )

            _landed_svc = _autonudge_landed_get()
            if _landed_svc is not None:
                _landed_svc.notify_cycle_landed(slot.key)
        except Exception:
            logger.debug("autonudge.notify_cycle_landed failed", exc_info=True)
        # Per-cycle fallback-chain walk state resets with the budgets; the
        # sticky _active_fallback_model / _fallback_primary_model pair
        # deliberately survives a landed turn — the session stays on the
        # fallback until the start-of-turn restore probe succeeds.
        slot._fallback_candidate_idx = 0
        slot._fallback_walked = []
        # Reset the promise-only one-shot on a LANDED turn so the guard re-arms per
        # user turn (matching state_parts/slot_ladder.py's contract and the sibling budgets).
        # Without this a single false positive would disarm it for the slot's
        # whole life, and the "two consecutive such turns" case stays only
        # half-covered. A promise-only recovery turn is NOT landed, so it is
        # excluded here and the increment it made persists until a real turn lands.
        slot._promise_only_retries = 0
        # Same contract for the post-compaction one-shot: re-arm per landed
        # turn, so a long session that compacts more than once is recovered
        # each time. A compaction-recovery turn is not landed, so its own
        # increment survives until a real turn lands — which is what keeps a
        # continuation that overflows again from looping.
        slot._compaction_continue_retries = 0
        # NOTE: the poisoned-conversation streak/one-shot (``_escalation``)
        # are NOT
        # unconditionally reset here: this block also runs for CANCELLED
        # turns, and a user's Stop press by itself proves nothing about
        # the conversation's health. The activity-based streak break
        # (assistant tokens, a tool call — _turn_emitted — or streamed
        # thinking — _turn_thought — is positive evidence the backend
        # accepts this conversation, even if the user then cancelled it)
        # lives in _run_chat's FINALLY so it also covers the recovery paths
        # in _turn_stop_recovery that end the turn before this phase. The one-shot
        # itself still re-arms only on a LANDED turn (the record_success
        # block in _turn_context_usage): breaking the streak is cheap to be generous
        # with, re-arming a spent discard is not.
        # NOTE: slot._posttoken_retry_used is intentionally NOT reset here.
        # The one-shot post-token recovery allowance is refreshed at the
        # START of a GENUINE new user turn (see the gated reset in _turn_open, near
        # `_turn_emitted = False`), never on the synthetic recovery turn.
        # Resetting it on the recovery turn's completion would let a repeated
        # post-token 5xx during recovery re-queue forever.

    if _stop_reason == STOP_REASON_CANCELLED and _refusal_reasons and not _stop_pressed(turn):
        # Not a user stop: the backend aborted the turn on the rejected tool
        # (codex answers its only reject option, `cancel`, this way). Logged
        # apart from the user case so an operator reading "cancelled by user"
        # is not sent looking for a Stop press that never happened.
        logger.info(
            "Turn for slot %s aborted by the backend after a policy-blocked tool "
            "call (stopReason=cancelled, no Stop pressed) -- refusal recovery follows",
            slot.key,
        )
    elif _stop_reason == STOP_REASON_CANCELLED:
        logger.info("Turn cancelled by user for slot %s", slot.key)
    elif (
        not _retrying_empty
        and not _recovering_promise
        and not _recovering_compaction
        and not _noticed_leak
        and not _recovering_infra
        and not _is_monitor_wake
    ):
        _maybe_consolidate(state, slot)
