"""Recovery from an AcpError a dashboard turn raised: the post-token CONTINUE re-prompt
and the poisoned-conversation verdict.

The turn's except arm keeps the classification (which arm applies), every redaction
of error text and every retry-budget reset a repository guard counts in
``chat_runner.py``; an owner here runs the arm's recovery once the arm is chosen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _POSTTOKEN_RECOVER_MSG,
        SYNTHETIC_RECOVERY_KIND,
        TRANSIENT_GIVE_UP_TEXT,
        TRANSIENT_NOTICE_GIVE_UP,
        TRANSIENT_NOTICE_META_KEY,
        TRANSIENT_NOTICE_RESUMING,
        TRANSIENT_RESUMING_TEXT,
        TRANSIENT_RETRY_KIND,
        DashboardState,
        RecoveryPayload,
        RecoveryProvenance,
        _ChatSlot,
        _has_user_queued_followup,
        _recovery_delay,
        _should_suppress_requeue,
        logger,
        transient_retry_delay,
    )


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
        # unlike the verbatim same-model replay arm in ``_run_chat``, because this is a
        # CONTINUATION of a turn that already streamed: the partial is
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
                # Same text as the provider-budget banner recovery, different
                # owner: the tag keeps banner stripping and the provider-specific
                # queue policy away from a retry that may legitimately repeat its
                # answer.
                provenance=RecoveryProvenance.TRANSIENT_RETRY,
            )
    # else: Stop active (_should_suppress_requeue) or nested turn
    # (_prompt_depth != 0) — do NOT requeue; partial + notice already
    # shown, so the streamed answer survives in the transcript. The
    # allowance is left UNconsumed so a later turn can still recover once.
