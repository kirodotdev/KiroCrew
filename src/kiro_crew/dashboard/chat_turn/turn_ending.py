"""The if/elif chain that decides how a dashboard turn ends (``_turn_ending_chain``): the leaked
tool-call notice, the L1 infrastructure retry, the promise-only and false-blocker recoveries,
the spent-budget and progress-claim notices, and the compaction-dropped leak card."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import (
    _PROMISE_ONLY_CONTINUE_MSG,
    FALSE_TOOL_BLOCKER_REPLAY_KIND,
    RecoveryPayload,
    has_unfinished_progress_claim,
    is_false_current_tool_blocker,
    is_promise_only_terminal,
    should_notice_compaction_dropped_leak,
    should_notice_leaked_tool_call,
    should_notice_mixed_turn_leak,
    tool_calls_are_read_only_preparation,
)
from kiro_crew.dashboard.recovery_replays import replays_of
from kiro_crew.recovery.ladder import L1_TOOL_CALL, InfraError, default_ladder

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        STOP_REASON_CANCELLED,
        STOP_REASON_END_TURN,
        SYNTHETIC_RECOVERY_KIND,
        ReplayFamily,
        build_infra_retry_prompt,
        effective_session_key,
        logger,
        should_recover_promise_only,
    )
    from kiro_crew.dashboard.chat_turn.recovery import _recovery_delay, _should_suppress_requeue
    from kiro_crew.dashboard.chat_turn.steer_queue import _has_user_queued_followup
    from kiro_crew.dashboard.chat_turn.tool_approval import _slot_is_trusted
    from kiro_crew.dashboard.chat_turn.turn_entry import _session_stop_generation, _stop_pressed
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _queue_recovery
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn


async def _turn_ending_chain(turn: _ChatTurn) -> None:
    """The chain deciding how the turn ends: leak notices, the L1 retry, promise-only and false-blocker recoveries."""
    _builtin_identity_trusted = turn._builtin_identity_trusted
    _compaction_dropped_leak = turn._compaction_dropped_leak
    _directive_user_origin = turn._directive_user_origin
    _incoming_message = turn._incoming_message
    _l1_escalated = turn._l1_escalated
    _l1_interrupted = turn._l1_interrupted
    _leak_card_posted = turn._leak_card_posted
    _noticed_leak = turn._noticed_leak
    _prompt_depth = turn._prompt_depth
    _recovering_infra = turn._recovering_infra
    _recovering_promise = turn._recovering_promise
    _refusal_reasons = turn._refusal_reasons
    _stop_gen_turn_start = turn._stop_gen_turn_start
    _stop_reason = turn._stop_reason
    _synthetic_payload = turn._synthetic_payload
    _ttft_visible = turn._ttft_visible
    _turn_credits = turn._turn_credits
    _turn_successful_tool_call_ids = turn._turn_successful_tool_call_ids
    _turn_tool_calls = turn._turn_tool_calls
    _turn_tool_identities = turn._turn_tool_identities
    client = turn.client
    slot = turn.slot
    state = turn.state
    # Leaked tool-call notice: the turn ended NORMALLY with an
    # invoke block emitted as TEXT and zero tool calls — the model wrote
    # the invocation into the prose channel instead of executing it, so
    # nothing ran and, in a monitor/autonudge loop, the session silently
    # stalls. Surface a visible notice and mark the turn un-landed.
    # NOTICE-ONLY by design — no continuation is queued, because an
    # injected "re-issue that call" carries runtime authority into
    # sessions where the call auto-approves (slot trust, yolo, or a static
    # agent tool allowlist — the last invisible at this layer, so no
    # fail-closed downgrade condition exists), and the leaked block may be
    # untrusted external content the model merely reproduced. Rationale in
    # full: should_notice_leaked_tool_call's docstring. Checked BEFORE the
    # promise-only guard: a leaked block is machine syntax, not a promise
    # sentence, and the more specific detector must own the turn.
    if should_notice_leaked_tool_call(
        stop_reason=_stop_reason,
        end_turn_reason=STOP_REASON_END_TURN,
        final_segment_text=turn.assistant_text,
        prompt_depth=_prompt_depth,
        is_cancelled=(_stop_reason == STOP_REASON_CANCELLED),
        refusal_reasons=_refusal_reasons,
        turn_tool_calls=_turn_tool_calls,
    ):
        logger.warning(
            "Leaked tool call for slot %s — the final message contained an "
            "invoke block as text with no tool call executed (credits=%.4f)",
            slot.key,
            _turn_credits,
        )
        slot.append(
            "notice",
            "ℹ️ A tool call leaked into the reply text instead of executing — "
            "nothing was run. Re-send your request to retry (an active monitor "
            "loop retries on its next cycle).",
            "msg msg-info",
        )
        _noticed_leak = True
        turn._noticed_leak = _noticed_leak
        _leak_card_posted = True
        turn._leak_card_posted = _leak_card_posted
    elif should_notice_mixed_turn_leak(
        stop_reason=_stop_reason,
        end_turn_reason=STOP_REASON_END_TURN,
        final_segment_text=turn.assistant_text,
        prompt_depth=_prompt_depth,
        turn_tool_calls=_turn_tool_calls,
    ):
        # MIXED TURN: the turn dispatched tool calls and THEN leaked a
        # final one as text. The two halves of the leak response split
        # here, because only one is unsafe on this shape: UN-LANDING stays
        # excluded, since earlier calls may already have taken effect, so
        # `_noticed_leak` is deliberately NOT set and the turn lands, bills
        # and consolidates normally — while the NOTICE is not excluded,
        # because a logger warning is invisible to the person in the chat
        # and the leak reads as a completed action. Wording differs from
        # the sibling on purpose: "nothing was
        # run" is false here. Rationale in full, including why the count is
        # described as attempted: should_notice_mixed_turn_leak's docstring.
        logger.warning(
            "Leaked tool call alongside %d executed tool call(s) for slot %s "
            "— the final segment contains an invoke block as text; the turn "
            "lands normally (diagnostic only)",
            _turn_tool_calls,
            slot.key,
        )
        slot.append(
            "notice",
            "ℹ️ The last tool call leaked into the reply text instead of "
            f"executing — the {_turn_tool_calls} call(s) before it were "
            "attempted and may already have taken effect, so part of this turn "
            "may have landed and part did not. Check what landed before "
            "re-sending (an active monitor loop retries on its next cycle).",
            "msg msg-info",
        )
        _leak_card_posted = True
        turn._leak_card_posted = _leak_card_posted
    # L1 of the recovery ladder (RFC overload-resilience §7): the turn ended
    # normally but its LAST tool result was an INFRASTRUCTURE refusal -- the
    # MCP stub's ``-32001 capacity`` error (the gateway daemon had no spawn
    # capacity inside the stub's wait budget) or a gateway
    # ``recoverable_infra`` marker. The model cannot tell that from a real
    # tool failure and typically gives up; the work is not done and the cause
    # is transient. Retry ONCE per attempt on the shared schedule: wait the
    # server's retry hint (jittered, capped), then inject a continuation that
    # names the failed call and asks for exactly that call again -- never a
    # verbatim replay of the user's message, since earlier calls this turn
    # may have taken effect. Bounded by the ladder (3 attempts per slot, then
    # escalate = stop retrying here and say so). The classification is the
    # ACP layer's (``client.last_infra_error``), not a regex over the text
    # here. Same stop / steer / follow-up guards as every sibling recovery.
    elif (
        _prompt_depth == 0
        and _stop_reason == STOP_REASON_END_TURN
        # isinstance, not a None check: like ``last_compaction_transient``
        # this attribute is read through getattr on whichever client class
        # serves the slot, and a provider (or test stand-in) that exposes an
        # auto-created attribute must not be read as a verdict.
        and isinstance(getattr(client, "last_infra_error", None), InfraError)
        and not _should_suppress_requeue(slot)
        and getattr(slot, "_stop_generation", _stop_gen_turn_start) == _stop_gen_turn_start
        and not _has_user_queued_followup(slot)
        and not getattr(slot, "_pending_steers", None)
    ):
        _infra = client.last_infra_error
        _l1 = default_ladder().observe_failure(
            L1_TOOL_CALL,
            slot.key,
            retry_after_secs=_infra.retry_after_secs,
            reason=_infra.error_class,
        )
        if _l1.retry:
            logger.info(
                "L1 recovery for slot %s: last tool result was %s (retry_after=%s); "
                "retrying in %.1fs (attempt %d)",
                slot.key,
                _infra.error_class,
                _infra.retry_after_secs,
                _l1.delay_secs,
                _l1.attempt,
            )
            slot.append(
                "notice",
                "⟳ The last tool call was refused for lack of gateway capacity "
                f"— retrying it in {int(round(_l1.delay_secs))}s "
                f"(attempt {_l1.attempt}).",
                "msg msg-info",
            )
            # Honour the server's retry hint before re-queuing: the delay is
            # already capped by the policy, and the slot shows as recovering
            # (its OWN retry counter is non-zero) for the duration. Never the
            # transient-5xx counter: that is a live budget read by the
            # re-prompt gate, the backoff seed and the throttle-exhaustion
            # model-fallback threshold, so counting a gateway-capacity wait
            # there shortens the next real 5xx ladder, inflates its backoff
            # seed, and brings the swap onto agent.fallback_model that many
            # errors closer — for a wait the model had no part in.
            slot._infra_retries += 1
            await _recovery_delay(_l1.delay_secs)
            # Re-read the interrupt signals AFTER the wait, exactly as the throttle-exhaustion
            # re-queue in _on_acp_transient_exhausted does: the guards in the
            # condition above were read seconds ago, and a Stop, steer or
            # follow-up arriving during the backoff resolves while no prompt
            # is active, so nothing downstream catches it — the dispatch-point
            # purge covers the promise-only and post-compaction continuations
            # only, deliberately leaving sibling recoveries alone. Live
            # signals, not the turn-entry snapshots, and `_stop_pressed()`
            # rather than the slot-scoped generation the condition above
            # reads: a stop issued on a linked channel surface moves only the
            # session-scoped count.
            _l1_took_over = bool(
                _has_user_queued_followup(slot) or getattr(slot, "_pending_steers", None)
            )
            if _should_suppress_requeue(slot) or _stop_pressed(turn) or _l1_took_over:
                logger.info(
                    "L1 recovery for slot %s: dropping the re-queue — the user "
                    "intervened during the %.1fs backoff (took_over=%s)",
                    slot.key,
                    _l1.delay_secs,
                    _l1_took_over,
                )
                # The notice above promised a retry at a time now past, and it
                # is a persisted card, so correct it in one line — same rule
                # (and same trigger split) as the dispatch-point purge: only a
                # user's own message takes over; a Stop ran nothing.
                slot.append(
                    "notice",
                    "ℹ️ The capacity retry was cancelled — "
                    + (
                        "your message takes over."
                        if _l1_took_over
                        else "the turn was stopped, the call was not retried."
                    ),
                    "msg msg-info",
                )
                # Hand the attempt back: it was counted for a retry that never
                # ran, and a counter left standing shortens the next real L1
                # ladder and reads as "recovering" on the health panel for a
                # slot that is idle. `_recovering_infra` stays False on this
                # arm on purpose — nothing is queued, so the turn LANDS and
                # must settle, save and reset its budgets like any other
                # landing; marking it un-landed would freeze the slot's
                # budgets behind a recovery that is never coming.
                slot._infra_retries -= 1
                _l1_interrupted = True
                turn._l1_interrupted = _l1_interrupted
            else:
                _queue_recovery(
                    turn,
                    0,
                    build_infra_retry_prompt(_infra.error_class, _infra.retry_after_secs),
                    kind=SYNTHETIC_RECOVERY_KIND,
                    payload=RecoveryPayload.CONTINUATION,
                )
                _recovering_infra = True
                turn._recovering_infra = _recovering_infra
        else:
            # This turn lands (nothing is re-queued) but the dependency never
            # came back, so the settlement in _turn_persist_and_settle must not close the
            # L1 run as a recovery.
            _l1_escalated = True
            turn._l1_escalated = _l1_escalated
            slot.append(
                "notice",
                "ℹ️ The last tool call was refused for lack of gateway capacity "
                f"and the automatic retries are spent ({_l1.attempt}). Re-send "
                "the request once the gateway has capacity.",
                "msg msg-info",
            )
    # Promise-only guard: the turn ended NORMALLY with visible text
    # whose FINAL segment only ANNOUNCES an immediate action ("I'll do that
    # now") without making the tool call, so the work never happened yet the
    # turn would otherwise land + bill. Inject exactly one continuation that
    # tells the model to carry out the announced action now. `assistant_text`
    # here is the post-last-tool segment (reset at each tool boundary), so a
    # turn that DID call a tool then summarised has a summary — not a promise —
    # and never matches. Bounded to one
    # attempt via slot._promise_only_retries; a second promise-only ending
    # falls through and lands normally rather than looping.
    # Chained as `elif` off the leaked-tool-call notice above: at most one
    # of the two unacted-turn paths may claim a turn.
    elif should_recover_promise_only(
        stop_reason=_stop_reason,
        end_turn_reason=STOP_REASON_END_TURN,
        # `_produced_visible_output` is set True ONLY on the paths that reset
        # assistant_text mid-turn (steer cut, compaction, clear, agent switch);
        # a normal streamed-text turn leaves it False and is handled by
        # _turn_settle_answer's `if _answer_text:` branch instead. But a promise-only turn IS
        # exactly a normal streamed-text turn, so keying the guard on the flag
        # alone would never fire for it. A non-empty final segment is itself
        # visible output, so derive it from
        # the text — the same `assistant_text` the terminal-promise detector
        # reads below.
        produced_visible_output=bool(turn.assistant_text.strip()) or turn._produced_visible_output,
        final_segment_text=turn.assistant_text,
        prompt_depth=_prompt_depth,
        promise_only_retries=slot._promise_only_retries,
        is_cancelled=(_stop_reason == STOP_REASON_CANCELLED),
        refusal_reasons=_refusal_reasons,
        # A completed side-effecting tool this turn (e.g. send_message) followed
        # by trailing promise-shaped text would otherwise let the continuation
        # REISSUE the action. The only non-zero exception requires a unique,
        # provider-canonical read/search/fetch builtin identity per dispatch and
        # a final completed result for the exact same id set, followed by the
        # model's false current-turn blocker claim.
        turn_tool_calls=_turn_tool_calls,
        turn_tool_identities=tuple(_turn_tool_identities),
        successful_tool_call_ids=frozenset(_turn_successful_tool_call_ids),
        builtin_identity_trusted=_builtin_identity_trusted,
        # A tool-bearing false-blocker retry may replay ONLY the exact prompt
        # admitted from an authenticated human. App, cron, subagent,
        # and synthetic recovery text cannot mint user authority.
        directive_user_origin=_directive_user_origin and not _synthetic_payload,
        # A soft Stop pressed while the promise streamed can arrive here as a
        # normal end_turn (cancel race); re-queueing then would dispatch the
        # stopped action. Gate on the same stop-state every sibling path uses,
        # PLUS the turn-window monotonic-counter check (catches a Stop that
        # already resolved back to idle) and a user-follow-up check (respects any
        # user-queued message rather than jumping ahead of it). A queued cron /
        # sub-agent event is orchestration, NOT a user intervention, so it does
        # not count — see `_has_user_queued_followup`.
        stop_in_progress=_should_suppress_requeue(slot),
        # The live Stop signal every end-of-turn gate shares: the slot's own
        # counter AND the session-scoped one, so a stop issued on a linked
        # channel surface is seen here too.
        stop_generation_unchanged=not _stop_pressed(turn),
        queue_empty=not _has_user_queued_followup(slot),
        # Mid-turn steers live in _pending_steers (a separate channel from
        # _queue) and are only degraded into queue cards in _run_chat's finally,
        # after this guard. Check them here so a "don't delete" steer aborts
        # recovery instead of being overridden by the announced action.
        no_pending_steers=(not getattr(slot, "_pending_steers", None)),
    ):
        if state.is_yolo_active() or _slot_is_trusted(slot):
            # auto-approve downgrade: with no human
            # approval between an injected continuation and the tool it triggers,
            # a terminal-promise detector false-accept could auto-dispatch an
            # action the user was still deciding on. Downgrade recovery to a
            # NOTICE here: state what happened and let the user re-send, rather
            # than auto-continuing unattended. This structurally bounds ANY
            # detector miss (a missed negation/conditional phrasing) to a safe
            # non-event, independent of what the regex fails to catch — the
            # approval path is the load-bearing safety claim, and auto-approve
            # removes it.
            #
            # Gate on BOTH grant sources, not yolo alone: approval is granted
            # by `slot_trusted or yolo_active`
            # (_on_permission_request ORs them), and `_slot_is_trusted` is True
            # for a per-session trust click or a scoped SafetyOverride grant —
            # neither of which sets global yolo. Checking yolo alone would leave
            # every trusted-but-not-yolo session on the auto-continue path with
            # its approval gate already removed, i.e. exactly the state this
            # downgrade exists to refuse.
            slot.append(
                "notice",
                "ℹ️ The model ended after saying it would act but didn't. "
                "Auto-continue is skipped under auto-approve mode — re-send your "
                "request to carry it out.",
                "msg msg-info",
            )
            # A promise-only turn announced work it never did, so it must NOT
            # be recorded as a landed success — even in the yolo notice-only
            # arm, where no continuation is injected. Mark it recovering so the
            # reset / consolidate guards of _turn_persist_and_settle and the record_success
            # guard of _turn_context_usage exclude it, matching the non-yolo arm;
            # otherwise auto-approve mode silently counts the un-acted turn as a clean land.
            _recovering_promise = True
            turn._recovering_promise = _recovering_promise
        elif _turn_tool_calls:
            # This is the false-current-tool-blocker exception: the pure
            # predicate admitted it only after count-aligned read/search/fetch
            # calls AND only for a prompt carrying authenticated-human
            # provenance. Replay that exact user message, as the first-empty
            # retry already does, rather than turning model-authored blocker or
            # action prose into synthetic authority. Static allowedTools,
            # MCP autoApprove, and hook grants may skip a later approval, but
            # the request they act on is still the user's own text.
            slot._promise_only_retries += 1
            logger.info(
                "False tool blocker after read-only preparation for slot %s — "
                "replaying the authenticated user request once (credits=%.4f)",
                slot.key,
                _turn_credits,
            )
            slot.append(
                "notice",
                "ℹ️ The model incorrectly stopped after read-only preparation — "
                "retrying your request once.",
                "msg msg-info",
            )
            _queue_recovery(
                turn,
                0,
                _incoming_message,
                kind=FALSE_TOOL_BLOCKER_REPLAY_KIND,
                payload=RecoveryPayload.ORIGINAL,
            )
            # Snapshot BOTH stop counters, same as the sibling recovery arms:
            # the dispatch-point purge compares each against its value at
            # enqueue, and a record without the session count would blind the
            # purge to a session-scoped Stop that pressed and resolved while
            # this replay waited in the queue.
            replays_of(slot).arm(
                ReplayFamily.CONTINUATION,
                entry_id="",
                session_key=effective_session_key(slot),
                stop_gen=getattr(slot, "_stop_generation", 0),
                session_stop_gen=_session_stop_generation(turn),
            )
            _recovering_promise = True
            turn._recovering_promise = _recovering_promise
        else:
            slot._promise_only_retries += 1
            logger.info(
                "Promise-only turn for slot %s — the final message announced an "
                "action with no tool call; injecting one continuation "
                "(credits=%.4f)",
                slot.key,
                _turn_credits,
            )
            slot.append(
                "notice",
                "ℹ️ The model ended after saying it would act but didn't — "
                "auto-continuing once.",
                "msg msg-info",
            )
            _queue_recovery(
                turn,
                0,
                _PROMISE_ONLY_CONTINUE_MSG,
                kind=SYNTHETIC_RECOVERY_KIND,
                payload=RecoveryPayload.CONTINUATION,
            )
            # Snapshot the monotonic stop counter so the dispatch-point purge can
            # detect a Stop that pressed AND resolved to idle while the continuation
            # waited in the queue (invisible to _should_suppress_requeue) — see the
            # purge block in `_start_next_queued_turn`.
            replays_of(slot).arm(
                ReplayFamily.CONTINUATION,
                entry_id="",
                session_key=effective_session_key(slot),
                stop_gen=getattr(slot, "_stop_generation", 0),
                session_stop_gen=_session_stop_generation(turn),
            )
            _recovering_promise = True
            turn._recovering_promise = _recovering_promise
    elif (
        _prompt_depth == 0
        and slot._promise_only_retries >= 1
        # Same derivation as the recovery arm above: the raw
        # `_produced_visible_output` flag is set True only on the reset-to-empty
        # paths, so a normal streamed-text SECOND promise-only turn leaves it
        # False and the give-up notice never surfaces — an announced action
        # landing silently, the exact thing this arm exists to prevent. A
        # non-empty final segment IS visible output.
        and (bool(turn.assistant_text.strip()) or turn._produced_visible_output)
        and _stop_reason == STOP_REASON_END_TURN
        and not _refusal_reasons
        and not _should_suppress_requeue(slot)
        and getattr(slot, "_stop_generation", _stop_gen_turn_start) == _stop_gen_turn_start
        and not _has_user_queued_followup(slot)
        and not getattr(slot, "_pending_steers", None)
        and (
            (
                _turn_tool_calls == 0
                and (
                    is_promise_only_terminal(turn.assistant_text)
                    or is_false_current_tool_blocker(turn.assistant_text)
                )
            )
            or (
                tool_calls_are_read_only_preparation(
                    _turn_tool_calls,
                    tuple(_turn_tool_identities),
                    frozenset(_turn_successful_tool_call_ids),
                    builtin_identity_trusted=_builtin_identity_trusted,
                )
                and is_false_current_tool_blocker(turn.assistant_text)
            )
        )
    ):
        # Spent-budget arm: a SECOND consecutive promise-only turn. The one-shot
        # recovery above already fired and did not stick, so we do NOT re-queue
        # (that would loop). But landing it silently hides the announced action
        # that never ran, so surface a give-up notice — mirroring the
        # empty-response third-strike arm — telling the user the one auto-retry
        # is spent. The turn still lands normally (no _recovering_promise).
        slot.append(
            "notice",
            "ℹ️ The model again ended after saying it would act but didn't. The "
            "one automatic retry is already spent — send the request again to "
            "perform the action.",
            "msg msg-info",
        )
    # A long mixed turn may execute many tools and then end with a NEW
    # foreground-progress claim ("I'm continuing with the gate run"). The
    # zero-tool promise recovery above intentionally refuses mixed turns:
    # replaying one could duplicate a push, deployment, message, or other
    # side effect. Silence is not acceptable either, because the terminal
    # assistant bubble then claims work is running after the turn is idle.
    # Diagnose only: keep the valid earlier work landed and state the true
    # lifecycle. The same notice covers present-progressive claims with no
    # tool calls, which are outside the narrow "I'll do it now" detector.
    elif (
        _prompt_depth == 0
        and (bool(turn.assistant_text.strip()) or turn._produced_visible_output)
        and _stop_reason == STOP_REASON_END_TURN
        and not _refusal_reasons
        and not _should_suppress_requeue(slot)
        and getattr(slot, "_stop_generation", _stop_gen_turn_start) == _stop_gen_turn_start
        and not _has_user_queued_followup(slot)
        and not getattr(slot, "_pending_steers", None)
        and (
            has_unfinished_progress_claim(turn.assistant_text)
            or (_turn_tool_calls > 0 and is_promise_only_terminal(turn.assistant_text))
        )
    ):
        slot.append(
            "notice",
            "ℹ️ This turn ended after the model said work was still in progress. "
            "No further main-agent steps run from this completed turn unless a "
            "Stop hook explicitly requests a bounded continuation. Separately "
            "shown subagents or monitor loops, if any, continue on their own; "
            "otherwise send a message to resume.",
            "msg msg-info",
        )
    # OUTSIDE the chain above, deliberately. Every arm in it owns the turn's
    # outcome -- it un-lands, re-drives or re-queues -- and two of them are
    # RECOVERIES (the L1 infrastructure retry and the promise-only guard).
    # This card owns no outcome, so taking an exclusive slot ahead of them
    # would starve a turn that both dropped a leak at its boundary and needs
    # a recovery: its gates (normal end_turn, no cancel, no refusal, depth 0)
    # exclude neither shape, so the recovery would simply never run. An
    # independent `if` lets both happen, which is what the turn actually
    # warrants -- one explanation, and the recovery it was already owed.
    #
    # `_leak_card_posted` (not the ordering) is what keeps one turn to one
    # leak card: either arm above that already carded sets it.
    if should_notice_compaction_dropped_leak(
        dropped_leak=_compaction_dropped_leak,
        leak_already_noticed=_leak_card_posted,
        stop_reason=_stop_reason,
        end_turn_reason=STOP_REASON_END_TURN,
        prompt_depth=_prompt_depth,
        is_cancelled=(_stop_reason == STOP_REASON_CANCELLED),
        refusal_reasons=_refusal_reasons,
    ):
        # `_noticed_leak` stays False: the turn keeps whatever outcome its
        # own arm gave it, including the post-compaction continuation that
        # fires BECAUSE the segment is blank. The card explains the raw
        # syntax the user saw; it never asks for the call to be re-issued.
        logger.warning(
            "Leaked tool call dropped at a compaction boundary for slot %s — "
            "an invoke block streamed as text and the mid-turn summarization "
            "cleared the segment before the turn-end scan (credits=%.4f)",
            slot.key,
            _turn_credits,
        )
        slot.append(
            "notice",
            "ℹ️ A tool call leaked into the reply text instead of executing, "
            "just before the context was compacted — that call did not run. "
            "Any raw `<invoke>` text above is that leak, not a reply.",
            "msg msg-info",
        )
    # On an empty-response re-queue the turn produced nothing and will
    # immediately re-run; _turn_persist_and_settle skips persistence entirely so we don't
    # save a spurious empty turn or skew reliability metrics.
    #
    # A PROMISE-ONLY recovery turn is DIFFERENT from an empty re-queue: it
    # produced visible output and consumed billed credits, so it MUST still
    # persist those stats + the transcript: skipping the attach makes the
    # consumed credits vanish from the turn record. What it must NOT do is
    # record a success or reset the retry budgets, which stays gated in
    # _turn_persist_and_settle and _turn_context_usage.
    if turn._ttft_quiet_text:
        # Turn end: the chat_done refresh is what shows a quietly persisted
        # steer segment. A no-op when a broadcast already stopped the clock.
        _ttft_visible.mark("quiet")
