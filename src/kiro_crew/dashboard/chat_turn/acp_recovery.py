"""Recovery from an AcpError a dashboard turn raised: the post-token CONTINUE re-prompt,
whether a thinking-binding mismatch may discard the conversation and replay once, and the
except arms that recover the turn: the dead-process arm (``_on_process_died``) and the
``_on_acp_*`` arms of the ``except AcpError`` ladder. A new arm that retries, resets or
replays the turn goes here (a model fallback hop in ``model_fallback``); one that ends it
goes in ``turn_failures``.

The turn's ``except AcpError`` chain keeps the classification (which arm applies); the
arm's phase here runs that arm's redaction of error text, its retry-budget resets and its
recovery.
"""

from __future__ import annotations

import asyncio
import functools
import time
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.dashboard.chat_utils import (
    TRANSIENT_GIVE_UP_TEXT,
    TRANSIENT_NOTICE_GIVE_UP,
    TRANSIENT_NOTICE_META_KEY,
    TRANSIENT_NOTICE_RESUMING,
    TRANSIENT_NOTICE_RETRYING,
    TRANSIENT_RESUMING_TEXT,
    TRANSIENT_RETRY_KIND,
    TRANSIENT_RETRYING_TEXT,
    RecoveryPayload,
    ResetCause,
    build_recovery_requeue,
    payload_for_replay,
)
from kiro_crew.dashboard.recovery_replays import replays_of
from kiro_crew.dashboard.state import _TURN_CONTINUING_INJECT_KINDS
from kiro_crew.llm_helpers import (
    CREDENTIAL_RELOAD_RETRY_NOTICE,
    acp_error_is_connection_failure,
    acp_error_is_throttle,
    acp_error_needs_fresh_credentials,
    connection_retry_delay,
    credential_give_up_text,
)
from kiro_crew.recovery.ladder import SESSION_RECOVERY_MAX_ATTEMPTS
from kiro_crew.security import redact

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _POSTTOKEN_RECOVER_MSG,
        SESSION_NOT_FOUND_GIVE_UP_TEXT,
        SESSION_NOT_FOUND_RETRY_TEXT,
        SYNTHETIC_RECOVERY_KIND,
        TRANSIENT_RETRIES,
        DashboardState,
        ReplayFamily,
        _ChatSlot,
        _has_user_queued_followup,
        _should_suppress_requeue,
        crew_log_emit,
        effective_session_key,
        fallback_rewound_transient_budget,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        runtime_death,
        subagents_attached_async,
        throttle_retry_delay,
        transient_retry_delay,
    )
    from kiro_crew.dashboard.chat_turn.recovery import (
        _count_turn_retry,
        _discard_unanswered_attempt,
        _recovery_delay,
        _shared_dependency_delay,
    )
    from kiro_crew.dashboard.chat_turn.turn_entry import _session_stop_generation, _stop_pressed
    from kiro_crew.dashboard.chat_turn.turn_ledgers import (
        _queue_recovery,
        _replay_owes_delivery,
        _requeue_owed_completion,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import _persist_partial_reply


async def _recover_posttoken_transient(
    slot: _ChatSlot,
    _msg: str,
    *,
    state: DashboardState,
    session_key: str,
    _first_turn_history_assembled: bool,
    _prompt_depth: int,
    _queue_recovery: Callable[..., str],
    _stop_pressed: Callable[[], bool],
) -> None:
    """Resume a turn whose stream died on a transient backend error after output.

    The partial is already persisted. One CONTINUE re-prompt of the same live
    session is queued after a short backoff, unless the user intervened during the
    wait; the one-shot allowance is spent only by a real enqueue.
    """
    # The live session already holds this turn's assembled prompt and history
    # (tokens streamed — ``_turn_emitted`` — and the ACP process stays alive across
    # the post-token 5xx). Settle the FRESH first-turn history debt NOW, before
    # branching on recover / Stop-suppress / nested-turn / follow-up takeover:
    # whichever way this resolves, the NEXT turn runs on the same live session, and
    # leaving the debt armed makes that turn (``_context_is_new`` while armed —
    # including a queued follow-up that suppresses recovery) rebuild and re-prepend
    # the full replay the provider already retained. Guard on
    # ``_first_turn_history_assembled``: a slash first turn (``/help``) streams
    # native output and can reach this arm, but it assembled no history, so it must
    # not settle a debt it never paid — the next ordinary prompt pays it.
    if _first_turn_history_assembled:
        try:
            state.sessions.consume_first_turn_history_owed(session_key)
        except Exception:
            logger.debug(
                "settling first-turn history debt on post-token 5xx failed",
                exc_info=True,
            )
    # Surface a brief recovery notice (one append). Only when the requeue
    # below will actually happen is the row a PENDING one (retry kind +
    # resuming token); otherwise nothing resumes — Stop is active or this
    # is a nested turn — so the row is terminal and says so, with the
    # give-up token so the dashboard keeps it on the ErrorCard (whose
    # Continue affordance is the way forward) instead of a soft notice
    # reading "resuming…" forever.
    _will_recover = not _should_suppress_requeue(slot) and _prompt_depth == 0
    if _will_recover:
        slot.append(
            "error",
            TRANSIENT_RESUMING_TEXT,
            "msg msg-err",
            meta={
                "kind": TRANSIENT_RETRY_KIND,
                TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_RESUMING,
            },
        )
    else:
        slot.append(
            "error",
            TRANSIENT_GIVE_UP_TEXT,
            "msg msg-err",
            meta={TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP},
        )
    if _will_recover:
        _delay = transient_retry_delay(1)  # single short backoff (one-shot)
        logger.info(
            "Transient backend 5xx AFTER emit in slot %s — one-shot "
            "CONTINUE re-prompt of live session in %.1fs: %s",
            slot.key,
            _delay,
            _msg[:80],
        )
        # Back off, then re-queue the CONTINUE instruction onto the SAME
        # live session (no reset). The partial + notice are already shown;
        # the model resumes from the preserved context and appends the
        # continued answer as a new message below. Consume the one-shot
        # allowance HERE — only a real enqueue burns it.
        await _recovery_delay(_delay)
        # Re-read every interrupt signal after the backoff: `_will_recover`
        # was decided before it, and an interrupt arriving during the wait
        # resolves while no prompt is active, so nothing downstream drops
        # this entry (the dispatch-point purge covers the promise-only and
        # post-compaction continuations only). The FULL set applies here,
        # unlike the verbatim same-model replay in ``_on_acp_transient_retry``, because
        # this is a CONTINUATION of a turn that already streamed: the partial is
        # persisted and on screen, so a follow-up typed during the wait is
        # the user answering it, and the continuation is inserted at the
        # HEAD — ahead of that message. An unconsumed steer is degraded to
        # a head card by `_requeue_unconsumed_steers` in this turn's
        # finally, which pushes the continuation to position 1 and would
        # have it resume the abandoned turn on a LATER drain, when the
        # intervention signal is gone. Dropping costs nothing: the partial
        # stands and the give-up row's Continue affordance is the way on.
        _posttoken_took_over = bool(
            _has_user_queued_followup(slot) or getattr(slot, "_pending_steers", None)
        )
        if _should_suppress_requeue(slot) or _stop_pressed() or _posttoken_took_over:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the rule matches the word "token" in "Post-token" (this arm runs on `_turn_emitted`, i.e. after the turn streamed its first token); the format string holds no secret and every interpolated value is non-sensitive: a session slot key, a float delay and a bool  # noqa: E501
            logger.info(
                "Post-token CONTINUE re-prompt dropped for slot %s — the user "
                "intervened during the %.1fs backoff (took_over=%s)",
                slot.key,
                _delay,
                _posttoken_took_over,
            )
            # The "resuming…" row above is persisted and would otherwise
            # stand as the last word on a resume that never happened, so
            # correct it with the give-up token the nested-turn and
            # Stop-already-active paths use — the ErrorCard keeps its
            # Continue affordance instead of reading "resuming…" forever.
            slot.append(
                "error",
                TRANSIENT_GIVE_UP_TEXT,
                "msg msg-err",
                meta={TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP},
            )
            # Nothing to hand back: this arm counts no attempt, and the
            # one-shot is consumed BELOW the wait precisely so a retry
            # that never runs cannot spend it — an allowance burned here
            # silently disarms the next real recovery. The persisted
            # partial stays (append-only).
        else:
            slot._posttoken_retry_used = True
            _queue_recovery(
                0,
                _POSTTOKEN_RECOVER_MSG,
                kind=SYNTHETIC_RECOVERY_KIND,
                payload=RecoveryPayload.CONTINUATION,
            )
    # else: Stop active (_should_suppress_requeue) or nested turn
    # (_prompt_depth != 0) — do NOT requeue; partial + notice already
    # shown, so the streamed answer survives in the transcript. The
    # allowance is left UNconsumed so a later turn can still recover once.


async def _binding_rebuild_allowed(turn) -> bool:
    """Whether a thinking-binding mismatch may discard and replay once.

    Only an outer turn with no model activity yet: a reply, thought or
    tool call that already landed could repeat a side effect. The
    one-shot is shared with the image-history and poisoned-conversation
    recoveries, so a failed fresh conversation cannot loop. Attached
    sub-agents still use the native conversation, so a discard waits for
    them. The probe yields, so every user-intent signal is read again
    after it; the ledger re-checks them at the drain and consume seams.
    A sub-agent completion still owed to the parent skips the intent
    vetoes, as in the image-history arm: it is requeued as the
    completion it is, so no Stop or newer message can drop it.
    """
    _escalation = turn._escalation
    _prompt_depth = turn._prompt_depth
    _turn_emitted = turn._turn_emitted
    _turn_thought = turn._turn_thought
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state

    def _intent_unchanged() -> bool:
        if _replay_owes_delivery(turn):
            return True
        return (
            not _should_suppress_requeue(slot)
            and not _stop_pressed(turn)
            and not _has_user_queued_followup(slot)
            and not getattr(slot, "_pending_steers", None)
        )

    if (
        _prompt_depth != 0
        or _turn_emitted
        or _turn_thought
        or turn._turn_tool_calls
        or _escalation.one_shot_spent
        or not _intent_unchanged()
    ):
        return False
    if await subagents_attached_async(state, slot, session_key, "thinking_binding_recovery"):
        return False
    return _intent_unchanged() and effective_session_key(slot) == session_key


async def _on_process_died(turn: _ChatTurn, exc) -> None:
    """The session's process died under the turn."""
    _prompt_depth = turn._prompt_depth
    slot = turn.slot
    _is_synthetic = turn._is_synthetic
    _turn_emitted = turn._turn_emitted
    client = turn.client
    message = turn.message
    logger.warning("ACP process died in slot %s: %s — resetting session", slot.key, exc)
    # The class this handler caught is a fact only this site holds, and the
    # closer in `_turn_close_crew_log` (chat_turn/crew_log_attribution.py), from `_run_chat`'s
    # `finally`, reads `_crew_log_error` for it, so naming it here is what keeps
    # `turn/failed` from reporting an unnamed failure.
    _crew_log_error = type(exc).__name__
    turn._crew_log_error = _crew_log_error
    needs_session_reset = True
    turn.needs_session_reset = needs_session_reset
    if getattr(exc, "ambiguous_delivery", False) is True:
        # Every steer written into this turn sits in the same stalled pipe as
        # its prompt, so the teardown requeues each as possibly delivered.
        slot._steer_possibly_delivered.update(slot._pending_steers)
    await _persist_partial_reply(turn, "error: process died")
    # WHOSE failure was this? A process this slot was sharing -- with a
    # sub-agent of its own, or with a co-tenant session -- died for reasons
    # this slot has no account of, and charging it to this slot's recovery
    # budget is what put "Session stuck — please start a new chat" in front
    # of users who had done nothing. The death was classified ONCE where it
    # was detected, so this reads that record rather than forming its own.
    #
    # Recovery still has to be bounded, so the attempt is counted either way
    # -- against this slot when the runtime was its own, against the
    # shared-death streak when it was not, under the SAME ladder limit. A
    # single-tenant runtime, which is every runtime at cap 1 with no
    # sub-agent on it, takes the first branch and behaves exactly as before.
    _own_fault = runtime_death.caused_by_this_session(client)
    if _own_fault:
        slot._acp_pipe_death_retries += 1
    else:
        runtime_death.note_shared_death(slot.key)
    # Both counts run side by side and the limit is tested against whichever
    # is further along, so a shared death never refunds spent budget.
    _death_attempts = max(slot._acp_pipe_death_retries, runtime_death.shared_deaths(slot.key))
    if not _own_fault:
        logger.warning(
            "slot %s lost a turn to a SHARED runtime's death (%d running) — "
            "re-queuing without charging this session",
            slot.key,
            _death_attempts,
        )
    # A Stop that already resolved to idle is invisible to the suppress
    # check, and `_queue_recovery` takes no Stop snapshot of its own, so
    # only this gate can see it (the same pairing as _on_acp_session_not_found).
    if _should_suppress_requeue(slot) or _stop_pressed(turn):
        pass
    elif _prompt_depth == 0 and _death_attempts <= SESSION_RECOVERY_MAX_ATTEMPTS:
        # Persisted card: reliably visible at turn-teardown (an ephemeral
        # chat_status is dropped by the frontend once the streaming turn ends).
        # slot.append already emits ONE chat_message (via _on_message /
        # _broadcast_chat_message in ws_mode) AND persists the card — do NOT
        # also broadcast_ws("chat_message") or the UI renders a duplicate card
        # until the post-turn history refresh reconciles it.
        _retry_msg = "⟳ Connection lost — retrying…"
        slot.append("error", _retry_msg, "msg msg-err", meta={"kind": TRANSIENT_RETRY_KIND})
        _requeue_text, _requeue_payload = build_recovery_requeue(
            message,
            _turn_emitted,
            cause=ResetCause.CONNECTION_LOST,
            message_is_synthetic=_is_synthetic,
            # A request-frame drain stall may have delivered the buffered
            # prompt to a kiro-cli that resumed reading; replaying it verbatim
            # would run its tools twice. Resume from restored state instead.
            ambiguous_delivery=getattr(exc, "ambiguous_delivery", False),
        )
        _queue_recovery(
            turn,
            0,
            _requeue_text,
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=_requeue_payload,
        )
    elif _death_attempts > SESSION_RECOVERY_MAX_ATTEMPTS:
        # Two verdicts, because the user's next move differs. A session whose
        # OWN runtime keeps dying is stuck and a fresh chat is the fix; a
        # session riding a process that keeps being taken out from under it is
        # not stuck at all, and telling that user to start over would discard
        # a healthy conversation over someone else's fault.
        if _own_fault:
            slot.append("error", "Session stuck — please start a new chat.", "msg msg-err")
        else:
            slot.append(
                "error",
                "The agent process this chat shares kept restarting — please retry.",
                "msg msg-err",
            )
    else:
        slot.append("error", "⟳ Connection lost — please retry.", "msg msg-err")


async def _on_acp_retryable(turn: _ChatTurn, exc) -> None:
    """An ACP error the turn retries on a fresh session."""
    _is_synthetic = turn._is_synthetic
    _msg = turn._msg
    _prompt_depth = turn._prompt_depth
    _turn_emitted = turn._turn_emitted
    client = turn.client
    message = turn.message
    slot = turn.slot
    logger.info(
        "ACP transient (%s) in slot %s — resetting session",
        _msg[:80],
        slot.key,
    )
    # The ACP subprocess is dead (pipe death) or busy — always reset the
    # session and count the failure, regardless of depth (mirrors the
    # AcpProcessDied / PromptBusyExhaustedError handlers). Only the
    # re-queue is depth-0-gated. Gating this whole block on
    # `_prompt_depth == 0` would let a depth>0 pipe-death fall through to
    # `_on_acp_error_card`: no reset (the next turn hits the dead process)
    # and the failure never counting toward the exhaustion threshold.
    needs_session_reset = True  # checked in _turn_release_session (chat_turn/session_reset.py)
    turn.needs_session_reset = needs_session_reset
    await _persist_partial_reply(turn, "error: process died")
    # Option Y: pipe-death ("process exited"/"not running") shares the
    # _acp_pipe_death_retries counter with the AcpProcessDied handler;
    # genuine "already in progress" busy uses _prompt_busy_retries.
    _is_pipe_death = "process exited" in _msg or "not running" in _msg
    # Bound BEFORE the branch, not inside it. Both arms below fall
    # through to the same `elif _exhausted:` reporting, which reads this
    # flag -- so binding it only where a death is involved leaves the
    # genuine-busy path reading an unbound local and raising
    # UnboundLocalError *inside* an except handler, which escapes past
    # _run_chat's finally and replaces the terminal card with nothing. True is
    # also the right answer for busy: nothing shared died, the slot's own
    # conversation is occupied, so the pre-existing "start a new chat"
    # verdict is the correct one and stays unchanged.
    _own_fault = True
    if _is_pipe_death:
        # Same attribution question as `_on_process_died`,
        # and the same answer: this arm catches the pipe-death that
        # arrives as an AcpError instead of a typed one, so a shared
        # process's death must not land on this slot's budget here
        # either. A single-tenant runtime takes the first branch and is
        # unchanged.
        _own_fault = runtime_death.caused_by_this_session(client)
        if _own_fault:
            slot._acp_pipe_death_retries += 1
        else:
            runtime_death.note_shared_death(slot.key)
        _exhausted = (
            max(slot._acp_pipe_death_retries, runtime_death.shared_deaths(slot.key))
            > SESSION_RECOVERY_MAX_ATTEMPTS
        )
        _status = "⟳ Connection lost — retrying…"
        turn._status = _status
    else:
        slot._prompt_busy_retries += 1
        _exhausted = slot._prompt_busy_retries > 3
        _status = "⟳ Session busy — retrying…"
        turn._status = _status
    # Resolved Stop: see the same gate in _on_process_died.
    if _should_suppress_requeue(slot) or _stop_pressed(turn):
        pass
    elif _exhausted:
        logger.info("Retry budget exhausted for slot %s (own_fault=%s)", slot.key, _own_fault)
        # Same two verdicts as the sibling arms: telling a user whose
        # conversation is healthy to discard it, because a process it
        # merely shared kept dying, is the harm this whole change removes
        # -- and this arm reaches the same users, just via a plain
        # AcpError rather than the typed death.
        if _own_fault:
            slot.append("error", "Session stuck — please start a new chat.", "msg msg-err")
        else:
            slot.append(
                "error",
                "The agent process this chat shares kept restarting — please retry.",
                "msg msg-err",
            )
    elif _prompt_depth == 0:
        # Single emit (see the _on_process_died note): slot.append persists +
        # broadcasts one chat_message via _on_message; no explicit broadcast_ws.
        logger.info(
            "Re-queuing slot %s after transient (pipe_death=%s, attempt %d)",
            slot.key,
            _is_pipe_death,
            (
                max(slot._acp_pipe_death_retries, runtime_death.shared_deaths(slot.key))
                if _is_pipe_death
                else slot._prompt_busy_retries
            ),
        )
        slot.append("error", turn._status, "msg msg-err", meta={"kind": TRANSIENT_RETRY_KIND})
        _requeue_text, _requeue_payload = build_recovery_requeue(
            message,
            _turn_emitted,
            # Shared branch: `_status` above already told the user
            # which of the two happened, so the continuation must
            # agree with it rather than pick one.
            cause=(ResetCause.CONNECTION_LOST if _is_pipe_death else ResetCause.SESSION_BUSY),
            message_is_synthetic=_is_synthetic,
        )
        _queue_recovery(
            turn,
            0,
            _requeue_text,
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=_requeue_payload,
        )
    else:
        # depth>0 with budget remaining: session already reset + failure
        # counted above; do NOT re-queue (mirrors AcpProcessDied /
        # PromptBusyExhaustedError) — surface feedback so the nested turn
        # doesn't fail silently against the now-dead process.
        _retry_msg = (
            "⟳ Connection lost — please retry."
            if _is_pipe_death
            else "⟳ Session busy — please retry."
        )
        slot.append("error", _retry_msg, "msg msg-err")


async def _on_acp_session_not_found(turn: _ChatTurn, exc) -> None:
    """The session the turn spoke to is gone."""
    _is_synthetic = turn._is_synthetic
    _prompt_depth = turn._prompt_depth
    _turn_emitted = turn._turn_emitted
    message = turn.message
    session_key = turn.session_key
    slot = turn.slot
    # The backend process is alive but holds no session under the id this
    # chat is bound to, so the dead-provider eviction never fires and a
    # plain retry draws the same answer. Reset drops the live binding and
    # keeps the session-map entry, so the next claim spawns a fresh
    # process that session/loads the SAME backend id (falling back to a
    # new session plus history replay only when that load fails). ONE
    # retry per landed turn: a backend that loses it again ends here.
    # A credential failure rides the same path, worded for its cause.
    _creds = acp_error_needs_fresh_credentials(exc)
    logger.warning(
        "Backend %s for slot %s — re-loading on a fresh runtime",
        "could not load its cloud credentials" if _creds else "lost the session",
        slot.key,
    )
    needs_session_reset = True  # checked in _turn_release_session (chat_turn/session_reset.py)
    turn.needs_session_reset = needs_session_reset
    await _persist_partial_reply(
        turn,
        "error: cloud credentials not loaded" if _creds else "error: session not found",
    )
    # A Stop that already resolved to idle is invisible to the suppress
    # check; the replay snapshots below are post-Stop, so only this gate
    # can see it.
    if _should_suppress_requeue(slot) or _stop_pressed(turn):
        pass
    elif _prompt_depth == 0 and not slot._session_not_found_retry_used:
        slot._session_not_found_retry_used = True
        slot.append(
            "error",
            CREDENTIAL_RELOAD_RETRY_NOTICE if _creds else SESSION_NOT_FOUND_RETRY_TEXT,
            "msg msg-err",
            meta={"kind": TRANSIENT_RETRY_KIND},
        )
        _requeue_text, _requeue_payload = build_recovery_requeue(
            message,
            _turn_emitted,
            cause=ResetCause.CONNECTION_LOST,
            message_is_synthetic=_is_synthetic,
        )
        # Snapshot BEFORE the enqueue: the reset in _turn_release_session
        # (chat_turn/session_reset.py) is awaited, and a Stop landing there must veto the replay.
        _snf_stop_gen = getattr(slot, "_stop_generation", 0)
        _snf_session_stop_gen = _session_stop_generation(turn)
        _snf_qid = _queue_recovery(
            turn,
            0,
            _requeue_text,
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=_requeue_payload,
        )
        replays_of(slot).arm(
            ReplayFamily.SESSION_NOT_FOUND,
            entry_id=_snf_qid or "",
            session_key=session_key,
            stop_gen=_snf_stop_gen,
            session_stop_gen=_snf_session_stop_gen,
        )
    elif _creds:
        # The fresh process failed too, so the helper itself is broken
        # and a new chat would fail the same way: the provider's own
        # instruction is the only actionable line.
        slot.append(
            "error",
            f"❌ {credential_give_up_text(exc)} Then send your message again.",
            "msg msg-err",
        )
    else:
        slot.append("error", SESSION_NOT_FOUND_GIVE_UP_TEXT, "msg msg-err")


async def _on_acp_binding_mismatch(turn: _ChatTurn, exc) -> None:
    """The conversation is bound to another session."""
    _attachment_meta = turn._attachment_meta
    _attachments = turn._attachments
    _escalation = turn._escalation
    _is_synthetic = turn._is_synthetic
    message = turn.message
    session_key = turn.session_key
    slot = turn.slot
    # The provider rejected preserved thinking that is bound to another
    # conversation prefix. Resending the same native conversation repeats
    # the rejection, so this is the image-history recovery's shape: discard
    # only the resume SID once, replay the turn from Kiro Crew's transcript
    # on a fresh conversation, and let the ledger veto the replay on a
    # later Stop, steer, queued follow-up or rebind. A refused rebuild
    # falls through to the terminal error in `_on_acp_error_card`.
    await _persist_partial_reply(turn, "error: thinking binding mismatch")
    # Not a transient outage cycle: no retry, fallback or streak
    # bookkeeping may leak into the rebuilt conversation.
    _escalation.arm_discard(clear_streak=True)
    needs_conversation_discard = True
    turn.needs_conversation_discard = needs_conversation_discard
    slot._transient_5xx_retries = 0
    slot._infra_retries = 0
    slot._fallback_candidate_idx = 0
    slot._fallback_walked = []
    slot.append(
        "error",
        "⟳ Preserved model reasoning no longer matches this conversation — "
        "rebuilding the model session from this chat and retrying once…",
        "msg msg-err",
        meta={"kind": TRANSIENT_RETRY_KIND},
    )
    if _replay_owes_delivery(turn):
        # An owed sub-agent completion is queued as the completion it
        # is: no recovery record, so nothing cancels it before it runs
        # and the parent still receives the result.
        _requeue_owed_completion(turn)
    else:
        _binding_replay_meta: dict[str, Any] | None = None
        if _attachment_meta:
            _binding_replay_meta = {k: list(v) for k, v in _attachment_meta.items()}
        elif _attachments:
            _binding_replay_meta = {"files": list(_attachments)}
        # Snapshot BEFORE the enqueue: the discard in _turn_release_session
        # (chat_turn/session_reset.py) is awaited, and a Stop landing there must veto the replay.
        _binding_stop_gen = getattr(slot, "_stop_generation", 0)
        _binding_session_stop_gen = _session_stop_generation(turn)
        _binding_qid = _queue_recovery(
            turn,
            0,
            message,
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=payload_for_replay(_is_synthetic),
            extra_meta=_binding_replay_meta,
        )
        replays_of(slot).arm(
            ReplayFamily.THINKING_BINDING,
            entry_id=_binding_qid or "",
            session_key=session_key,
            stop_gen=_binding_stop_gen,
            session_stop_gen=_binding_session_stop_gen,
        )


async def _on_acp_image_unsupported(turn: _ChatTurn, exc, _image_replay_owed) -> None:
    """The model refused the turn's image format."""
    _escalation = turn._escalation
    _is_synthetic = turn._is_synthetic
    _turn_emitted = turn._turn_emitted
    message = turn.message
    session_key = turn.session_key
    slot = turn.slot
    # Kiro accepted this turn with no new attachment, then rejected an
    # image carried by the native conversation. Retrying that same
    # session is deterministic; discard only its resume SID so the
    # dashboard/channel identity and text transcript survive. The fresh
    # session receives Kiro Crew's bounded text replay, never native
    # binary image blocks. One-shot accounting is shared with the
    # canary-based poison recovery, so a failed fresh attempt cannot
    # enter a discard loop.
    #
    # "No new image" is TWO facts, not one. `_attachments` covers the
    # dashboard upload lists, but a channel turn (and a dashboard turn
    # that types a path) carries its image as a bare path inside the
    # message TEXT, which `build_prompt_blocks` inlines as a real image
    # block for the CURRENT turn while `_attachments` stays empty. Such a
    # rejection is of the image the user just sent: discarding the
    # conversation would clear a healthy resume SID and the verbatim
    # replay would re-inline the same bytes, so it must fall through to
    # the terminal error in `_on_acp_error_card`, whose formatted text
    # already says to remove or re-encode the attachment.
    await _persist_partial_reply(turn, "error: terminal")
    _escalation.arm_discard(clear_streak=True)
    needs_conversation_discard = True
    turn.needs_conversation_discard = needs_conversation_discard
    if _turn_emitted:
        # A tool or assistant output already landed. Continue from the
        # persisted transcript rather than replaying the original user
        # request and potentially repeating a completed side effect.
        _image_recovery_text = _POSTTOKEN_RECOVER_MSG
        _image_recovery_payload = RecoveryPayload.CONTINUATION
    else:
        # No model activity landed, so the current text is safe to
        # replay verbatim after removing the poisoned native history.
        _image_recovery_text = message
        _image_recovery_payload = payload_for_replay(_is_synthetic)
    slot.append(
        "error",
        "⟳ The backend retained an image it can no longer process — "
        "restarting the model session without binary image history and "
        "continuing…",
        "msg msg-err",
        meta={"kind": TRANSIENT_RETRY_KIND},
    )
    if _image_replay_owed:
        # An owed completion is queued as the completion it is: no
        # recovery record, so nothing cancels it before it runs.
        _requeue_owed_completion(turn)
    else:
        # Snapshot the stop counters and the binding BEFORE the enqueue:
        # the conversation discard and the pending-reset consume are
        # awaited between here and the drain's dispatch, and a soft Stop
        # landing in that window preserves the queue while `_stopping`
        # snaps back to idle. A SYNTHETIC_RECOVERY_KIND item carries no
        # family record of its own, so without these the drain would
        # dispatch a cancelled turn and re-run a remaining destructive
        # step. Same guard, same words, as the sibling
        # poisoned-conversation canary recovery.
        _image_stop_gen = getattr(slot, "_stop_generation", 0)
        _image_session_stop_gen = _session_stop_generation(turn)
        _image_qid = _queue_recovery(
            turn,
            0,
            _image_recovery_text,
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=_image_recovery_payload,
        )
        replays_of(slot).arm(
            ReplayFamily.IMAGE_HISTORY,
            entry_id=_image_qid or "",
            session_key=session_key,
            stop_gen=_image_stop_gen,
            session_stop_gen=_image_session_stop_gen,
        )


async def _on_acp_transient_retry(turn: _ChatTurn, exc) -> None:
    """A transient error before the first token: retry the turn."""
    _is_synthetic = turn._is_synthetic
    _msg = turn._msg
    _prompt_depth = turn._prompt_depth
    client = turn.client
    message = turn.message
    slot = turn.slot
    # Transient backend 5xx (InternalServerError / DispatchFailure /
    # ConnectionReset, JSON-RPC -32603): the kiro-cli process is ALIVE —
    # only the model backend hiccupped — so do NOT reset the session
    # (needs_session_reset stays False). Re-prompt the SAME live session
    # with bounded backoff, reusing the llm_helpers transient classifier
    # + backoff curve landed for unattended callers.
    # _run_chat's `not _turn_emitted` guard means no assistant tokens
    # or tool calls have been delivered this turn, so re-prompting can't
    # double-stream output or re-run a side-effecting tool. Auth/validation
    # errors are excluded by the classifier and fall through to the bare
    # error in `_on_acp_error_card` (fail-fast). On budget exhaustion the
    # elif for this arm goes false and the next arm in chat_runner tries a
    # fallback-model swap first (`_fallback_swap_for_turn` ->
    # `_on_acp_transient_exhausted`). Only when no fallback applies does
    # `_on_acp_error_card` surface a clean ❌ on a still-resumable session —
    # unless CONSECUTIVE cycles exhaust this way, in which case it escalates
    # to a session destroy (poisoned persisted conversation; see its
    # escalation block).
    #
    # A connection failure (the network path dropped, no provider
    # answer) may run past TRANSIENT_RETRIES while the recovery window
    # opened at the ladder's first retry is still open, on a backoff
    # capped at CONNECTION_RETRY_MAX_DELAY, so a short outage resumes
    # the turn instead of ending it. Past the window it falls through to
    # the same branches as an exhausted provider-error ladder.
    #
    # A throttle waits on its own, longer curve (one KAS attempt is ~6s of
    # the engine's own retries) and rewinds the failed prompt before the
    # re-send, so the model reads one copy instead of one per attempt.
    if slot._transient_5xx_retries == 0:
        slot._transient_ladder_started = time.monotonic()
    slot._transient_5xx_retries += 1
    _in_recovery_window = slot._transient_5xx_retries > TRANSIENT_RETRIES
    _throttled = acp_error_is_throttle(exc)
    if acp_error_is_connection_failure(exc):
        _base_delay = connection_retry_delay(slot._transient_5xx_retries)
    elif _throttled:
        _base_delay = throttle_retry_delay(slot._transient_5xx_retries)
    else:
        _base_delay = transient_retry_delay(slot._transient_5xx_retries)
    # Local curve, floored by the dependency coordinator's shared
    # cooldown for this provider scope (one schedule per scope, RFC
    # §4.4); a typed throttle is reported to the adaptive controller.
    _delay = _shared_dependency_delay(exc, _base_delay, slot_key=slot.key)
    logger.info(
        "Transient backend error in slot %s (attempt %d/%d%s) — re-prompting "
        "live session in %.1fs: %s",
        slot.key,
        slot._transient_5xx_retries,
        TRANSIENT_RETRIES,
        ", connection recovery window" if _in_recovery_window else "",
        _delay,
        _msg[:80],
    )
    # No tokens streamed (guarded in _run_chat's elif), so no chunk message exists;
    # strip defensively before re-queue all the same.
    slot.purge_chunks()
    if _should_suppress_requeue(slot):
        # A Stop still resolving ends the episode, so its count goes too.
        slot._turn_retries = {}
    elif _stop_pressed(turn):
        # Resolved Stop: skip the "retrying" card the post-backoff re-read
        # would only withdraw (see _on_process_died). This ends the
        # turn with no requeue, so it owes the same per-turn budget refresh
        # as the post-backoff drop arm below: the attempt counted above
        # would otherwise shorten the next turn's ladder.
        slot._transient_5xx_retries = 0
        slot._turn_retries = {}
        slot._infra_retries = 0
        slot._fallback_candidate_idx = 0
        slot._fallback_walked = []
    elif _prompt_depth == 0:
        # Single emit (see the _on_process_died note): slot.append persists +
        # broadcasts one chat_message; no explicit broadcast_ws. Back off,
        # then re-queue — _run_chat's finally dequeues onto the SAME live
        # session (no reset), preserving conversation state.
        # The notice is PENDING, not terminal (the tag stops the UI
        # re-offering a choice that re-runs itself); the post-backoff
        # re-read below is the one path that can leave it standing for a
        # retry that never happened, and it corrects the row in place.
        # Inside the recovery window one standing notice covers the
        # outage: a retrying row is not repeated when only the replays
        # of this same turn have been appended since it.
        _tail: dict = {}
        for _row in reversed(slot.messages):
            if (
                _row.get("role") == "inject"
                and (_row.get("meta") or {}).get("injectKind") in _TURN_CONTINUING_INJECT_KINDS
            ):
                continue
            _tail = _row
            break
        if not (
            _in_recovery_window
            and _tail.get("role") == "error"
            and (_tail.get("meta") or {}).get(TRANSIENT_NOTICE_META_KEY)
            == TRANSIENT_NOTICE_RETRYING
        ):
            slot.append(
                "error",
                TRANSIENT_RETRYING_TEXT,
                "msg msg-err",
                meta={
                    "kind": TRANSIENT_RETRY_KIND,
                    TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_RETRYING,
                },
            )
        await _recovery_delay(_delay)
        # The rewind awaits a KAS answer, so it runs BEFORE the Stop
        # re-read below: a Stop landing during it still drops the re-send.
        if _throttled and not (_should_suppress_requeue(slot) or _stop_pressed(turn)):
            await _discard_unanswered_attempt(client, slot.key)
        # Re-read the STOP signals after the multi-second backoff: the
        # guard above ran before the wait, and a Stop pressed during it
        # resolves while no prompt is active, so nothing downstream
        # catches this entry (the dispatch-point purge covers the
        # promise-only and post-compaction continuations only). Both
        # signals are needed: the live suppress state for a stop still
        # resolving, and `_stop_pressed()` for one that pressed and
        # already snapped back to "idle" during the wait — which is also
        # the only one that sees a stop issued on a LINKED CHANNEL
        # surface, since that moves the session-scoped count alone.
        #
        # Deliberately NOT the steer / follow-up half of the L1 and
        # post-token re-reads. What is re-queued here is a VERBATIM
        # replay of the user's own message on the `not _turn_emitted`
        # path: no token and no tool call landed, so there is nothing for
        # a correction to contradict, and dropping it would erase the
        # request itself with no output anywhere in the transcript. It is
        # inserted at the HEAD, so a follow-up typed during the backoff
        # simply runs after it — the order the user sent them — and an
        # unconsumed steer is degraded to a head card by
        # `_requeue_unconsumed_steers` in this turn's finally, which
        # dequeues BEFORE the replay. Same two signals, same reason, as
        # `_on_acp_transient_exhausted` (the throttle-exhaustion arm), which
        # replays this identical message.
        if _should_suppress_requeue(slot) or _stop_pressed(turn):
            logger.info(
                "Transient re-prompt dropped for slot %s — stop during the " "%.1fs backoff",
                slot.key,
                _delay,
            )
            slot.append(
                "error",
                TRANSIENT_GIVE_UP_TEXT,
                "msg msg-err",
                meta={TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP},
            )
            # A NO-REQUEUE exit ENDS the turn, so this arm owes the same
            # per-turn budget refresh as `_on_acp_error_card` and the landed arm in
            # _turn_persist_and_settle — the refresh the requeue chain would otherwise have reached.
            # The whole ladder, not `-= 1`: a landed turn hands back every
            # attempt, and decrementing alone would leave a slot that had
            # already spent attempts 1..2 permanently short — while a
            # count left standing shortens the next real ladder, inflates
            # its backoff seed and brings the fallback swap closer.
            slot._transient_5xx_retries = 0
            slot._turn_retries = {}
            slot._infra_retries = 0
            slot._fallback_candidate_idx = 0
            slot._fallback_walked = []
        else:
            _count_turn_retry(slot, exc)
            _queue_recovery(
                turn,
                0,
                message,
                kind=SYNTHETIC_RECOVERY_KIND,
                # Verbatim replay: ORIGINAL only if the incoming text was
                # the user's. On a recovery turn it is the runner's
                # continuation.
                payload=payload_for_replay(_is_synthetic),
            )
    else:
        # depth>0 (nested turn): don't re-queue — surface a clean
        # transient status; the live session stays resumable.
        slot.append(
            "error",
            TRANSIENT_GIVE_UP_TEXT,
            "msg msg-err",
            meta={TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP},
        )


async def _on_acp_transient_exhausted(turn: _ChatTurn, exc, _fb_candidate) -> None:
    """A transient error before the first token, with the retries spent."""
    _crew_log_sid = turn._crew_log_sid
    _crew_log_turn_no = turn._crew_log_turn_no
    _is_synthetic = turn._is_synthetic
    client = turn.client
    message = turn.message
    slot = turn.slot
    # Append-only the session's log (flag-gated, fail-soft). Emitted HERE,
    # in the branch body, so it runs after _fallback_swap_for_turn has
    # released slot._model_pick_lock rather than while it is held. This
    # is the one model decision a transcript cannot answer afterwards:
    # the user picked the primary and the turn ran somewhere else.
    crew_log_emit.on_model_selected(
        _crew_log_sid, _fb_candidate, "fallback", turn=_crew_log_turn_no
    )
    # ── Throttle-exhaustion model fallback (agent.fallback_model) ──
    # The same-model budget of `_on_acp_transient_retry` is spent and the error is still
    # transient (throttle/capacity). _fallback_swap_for_turn already
    # moved the live session onto `_fb_candidate` via the substitute
    # set_model path (and returned None — falling through to the
    # terminal arm in `_on_acp_error_card` — when the chain is empty,
    # exhausted, or unusable). Announce the swap visibly (never
    # silent: the user picked the primary, so running elsewhere must
    # be said out loud), then re-queue the SAME message on the SAME
    # live session, exactly like the same-model retry in `_on_acp_transient_retry`.
    #
    # Attempt budget: rewinding the counter grants the candidate
    # exactly one more pass through the same-model arm (`_on_acp_transient_retry`), so
    # each candidate gets FALLBACK_CANDIDATE_ATTEMPTS attempts (this
    # re-queued one + the rewound passes) before the next exhaustion
    # lands back here and advances the chain. The rewind value is
    # derived in ONE place with the unattended surfaces' budget — see
    # llm_helpers.fallback_rewound_transient_budget /
    # FallbackState.should_retry_active. Nested turns
    # (_prompt_depth > 0) get no fallback
    # in v1 and Stop-suppressed cycles never swap (both guarded in
    # _run_chat's elif condition before the side-effecting swap runs).
    slot.purge_chunks()
    _fb_primary_safe, _ = redact_exfiltration_urls(
        slot._fallback_primary_model or "the selected model"
    )
    _fb_primary_safe, _ = redact_credentials(_fb_primary_safe)
    _fb_cand_safe = redact(_fb_candidate)
    # Persisted notice card (withheld-pin pattern): the explanation has
    # to survive a reload because the state it explains does (the
    # session stays on the fallback until the restore probe succeeds).
    slot.append(
        "notice",
        f"⚠️ {_fb_primary_safe} is throttled — running on {_fb_cand_safe} "
        f"until {_fb_primary_safe} recovers.",
        "msg msg-info",
    )
    logger.info(
        "Re-queuing slot %s on fallback model %s (candidate %d of chain)",
        slot.key,
        _fb_candidate,
        slot._fallback_candidate_idx,
    )
    slot._transient_5xx_retries = fallback_rewound_transient_budget()
    await asyncio.sleep(transient_retry_delay(1))
    # The rewind runs before the Stop guard below, as in _on_acp_transient_retry.
    if acp_error_is_throttle(exc) and not (_should_suppress_requeue(slot) or _stop_pressed(turn)):
        await _discard_unanswered_attempt(client, slot.key)
    # Stop guard AFTER the sleep: a Stop
    # pressed during this backoff resolves while no prompt is active,
    # so without this check the cancelled prompt would be requeued and
    # execute on the fallback anyway. Same two signals the sibling
    # requeue paths use: live suppress state, plus the live Stop signal
    # (monotonic stop generations, slot- and session-scoped, vs. their
    # turn-entry snapshots -- catches a Stop that already resolved back
    # to "idle" during the sleep, from any surface). Skips ONLY the
    # insert — the branch's fall-through bookkeeping is unchanged.
    if _should_suppress_requeue(slot) or _stop_pressed(turn):
        logger.info(
            "model fallback: dropping re-queue on slot %s — stop during backoff",
            slot.key,
        )
        # This arm ENDS the turn, so mirror the per-turn resets of
        # `_on_acp_error_card` and the landed arm in _turn_persist_and_settle that the
        # requeue chain would otherwise have reached: without them the
        # next genuine user turn inherits a near-exhausted transient
        # budget (premature fallback swap + spurious throttle notice)
        # and a restore probe suppressed by the stale walk index.
        slot._transient_5xx_retries = 0
        slot._turn_retries = {}
        slot._infra_retries = 0
        slot._fallback_candidate_idx = 0
        slot._fallback_walked = []
    else:
        # Through _queue_recovery like every other retry: a direct
        # queue_insert carries no admission stamp, so the drain's
        # fail-closed re-check would destroy this fallback-model retry in
        # every channel-linked or unattended session — exactly the
        # long-running jobs most likely to hit throttle fallback.
        _count_turn_retry(slot, exc)
        _queue_recovery(
            turn,
            0,
            message,
            kind=SYNTHETIC_RECOVERY_KIND,
            # Verbatim replay, same rule as in _on_acp_transient_retry.
            payload=payload_for_replay(_is_synthetic),
        )


async def _on_acp_posttoken_transient(turn: _ChatTurn, exc) -> None:
    """A transient error after the turn streamed output."""
    _first_turn_history_assembled = turn._first_turn_history_assembled
    _msg = turn._msg
    _prompt_depth = turn._prompt_depth
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # Post-token transient 5xx: assistant tokens and/or tool calls
    # already streamed this turn (_turn_emitted). Rather than fail-fast,
    # we RECOVER by re-prompting the SAME live session with a CONTINUE
    # instruction. The live ACP/kiro-cli process is still alive and holds
    # the interrupted turn's full context — original prompt, streamed
    # partial text, and any completed tool results — so the model resumes
    # from where it stopped instead of restarting. This is why the
    # tool-call case is not fail-fast: the continue prompt tells the
    # model NOT to re-run tools that already completed, so a post-tool
    # transient recovers safely (the model reads prior tool results and
    # continues) instead of double-running a side-effecting tool. We allow
    # EXACTLY ONE such retry per turn (_posttoken_retry_used one-shot).
    #
    # PARITY NOTE: the subagent path carries a hand-maintained copy of
    # this ladder (subagent.py `_stream_with_transient_retry`) with
    # intentionally identical semantics — same activity predicate,
    # same one-shot post-activity rule. A fix to either copy's
    # predicate or budget rules must be mirrored in the other.
    #
    # ACCEPTED TRADEOFF (owner decision — do NOT re-add a
    # tool-call fail-fast guard): a mid-stream 5xx is rare, and the
    # CONTINUE instruction explicitly tells the model to resume rather
    # than re-run completed tools. A residual double-execution risk
    # remains only for a tool that was still IN FLIGHT (dispatched but not
    # yet completed) when the 5xx hit a side-effecting/destructive tool;
    # the owner accepts that narrow risk rather than failing the whole
    # turn fast. This is deliberate — recovering the turn is preferred.
    #
    # ONE-SHOT ACCOUNTING: the allowance is consumed ONLY
    # when a recovery is actually enqueued (set immediately before the
    # enqueue in `_recover_posttoken_transient`, AFTER the eligibility check + backoff). Setting
    # it here — before the Stop-suppressed / nested / cancel-during-sleep
    # gates — would wrongly burn the allowance on paths that never
    # recover, denying a LATER turn its one legitimate recovery. Loop
    # prevention still holds: a re-failure DURING the recovery turn takes
    # the exception path (which never resets the flag) and the synthetic
    # recovery turn is excluded from the per-turn reset, so the flag stays
    # True across the recovery and a repeat post-token 5xx surfaces
    # instead of re-queueing forever.
    #
    # APPEND-ONLY design: never retract the streamed partial. We PRESERVE
    # it — finalize it as a normal assistant message exactly like
    # `_on_acp_error_card` — then surface a brief recovery notice, then
    # (if eligible) auto-retry ONCE by re-queueing the CONTINUE
    # instruction (NOT the original message). The user sees an append-only
    # sequence:
    #   [partial] [recover notice] [continued answer]
    # with nothing removed. No frontend reconcile / SSE gate is needed —
    # append-only is correct for both WebSocket and SSE clients.
    # Persisting the partial BEFORE the backoff sleep means a cancel
    # (Stop) during the sleep simply leaves partial+notice shown, no loss.
    # Persist the streamed partial as a real assistant message (copy of
    # `_on_acp_error_card`'s persist pattern): redact, strip the live chunk
    # messages, then append the finalized assistant bubble.
    #
    # BILLING: record this attempt's spend HERE, at the seam, BEFORE the
    # recovery helper runs its backoff + requeue. A post-token transient
    # means the attempt already streamed credits that the live session's
    # `last_prompt_stats` still holds right now, but will NOT survive
    # forward: the next prompt on the live session runs `carry_over()`,
    # which zeroes the credit/token counters (only context state survives
    # — AcpPromptStats.carry_over), so NO continuation's EVENT_COMPLETE
    # can ever recover this attempt's billing. Recording at the seam is
    # safe from double-counting: the recovery continuation is a SEPARATE
    # `_run_chat` turn with its own fresh (zeroed-then-refilled) stats
    # object and its own `_turn_usage_persisted` guard, so it bills only
    # its own spend. This is true for BOTH the ordinary successful requeue
    # and the drop-after-backoff arm inside the helper — neither can carry
    # this attempt's credits, so the seam is the only place that bills them.
    await _persist_partial_reply(turn, "error: transient attempt")
    await _recover_posttoken_transient(
        slot,
        _msg,
        state=state,
        session_key=session_key,
        _first_turn_history_assembled=_first_turn_history_assembled,
        _prompt_depth=_prompt_depth,
        _queue_recovery=functools.partial(_queue_recovery, turn),
        _stop_pressed=functools.partial(_stop_pressed, turn),
    )
