"""The dashboard turn's except arms outside the ``except AcpError`` recovery ladder, and the
ladder's terminal fall-through: a cancel (``_on_cancelled``), a sign-in and a busy prompt
queue, each requeueing the message (``_on_auth_required``, ``_on_prompt_busy``), a closing
gateway (``_on_session_closing``), the ACP error card with the poisoned-conversation
escalation, whose canary probe, discard and one requeue run here with the canary's settings
(``_on_acp_error_card``), and any other error (``_on_error``). The arm for a slot whose app
agent is not loaded is ``app_agent_binding``'s.

A new arm that ends the turn goes here; one that retries, resets or replays it goes in
``acp_recovery`` (a model fallback hop in ``model_fallback``)."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from kiro_crew.agent_capabilities import CapabilityError
from kiro_crew.dashboard.chat_utils import TRANSIENT_RETRY_KIND
from kiro_crew.security import redact
from kiro_crew.session_capabilities import CapabilityStartupError

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _PROJECT_CHANGED_OTHER_TEXT,
        _SESSION_START_REFUSAL_TEXT,
        STOP_REASON_CANCELLED,
        SYNTHETIC_RECOVERY_KIND,
        KiroCrewConfig,
        _member_capable_backend_names,
        _MemoryUnavailable,
        acp_error_is_transient,
        logger,
        run_bg_oneliner,
    )
    from kiro_crew.dashboard.chat_turn.account_identity import _mark_kiro_signed_out
    from kiro_crew.dashboard.chat_turn.eager_spawn import _is_pre_spawn_refusal
    from kiro_crew.dashboard.chat_turn.recipient import _deliver_linked_slack_message
    from kiro_crew.dashboard.chat_turn.recovery import (
        _note_cycle_failure,
        _note_cycle_start_failure,
        _requeue_after_prompt_busy,
        _requeue_auth_retry,
        _should_suppress_requeue,
        _terminal_error_meta,
        _turn_retry_outcomes,
    )
    from kiro_crew.dashboard.chat_turn.turn_entry import _session_stop_generation, _stop_pressed
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _queue_recovery
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import _persist_partial_reply


async def _on_cancelled(turn: _ChatTurn) -> None:
    """The turn was cancelled."""
    _stop_reason: str
    slot = turn.slot
    _crew_log_error = "CancelledError"
    turn._crew_log_error = _crew_log_error
    # A bare CancelledError here is either the operator's Stop press — the
    # one cancel they cause deliberately, labelled the non-fault
    # ``cancelled`` — or an involuntary cancel (tab close, idle-slot sweep,
    # shutdown, or the turn ceiling, which `_bounded_turn` converts to a
    # TimeoutError one frame above _run_chat). The involuntary cases share a neutral
    # ``error: cancelled``: recording the credits is this change's goal,
    # and telling the ceiling apart from the other involuntary
    # cancels is a separate concern no row consumer here needs.
    if _stop_pressed(turn):
        _cancel_reason = STOP_REASON_CANCELLED
    else:
        _cancel_reason = "error: cancelled"
    # A bare CancelledError never reaches the EVENT_COMPLETE latch in _on_complete, so
    # _stop_reason is still "" here. The first-turn history-debt settle keys
    # ``_turn_emitted and _stop_reason != STOP_REASON_CANCELLED`` off it: once
    # output has streamed, an un-set "" would read as "not cancelled" and
    # settle the debt on a turn kiro-cli DISCARDS (the cancelled prompt
    # carried the assembled history), so the next warm turn would omit it.
    # Mark the cancel on _stop_reason too — every cancelled exit (the Stop
    # press AND the involuntary cases: tab close, idle sweep, shutdown, the
    # turn ceiling converted one frame above _run_chat) is a discarded turn whose history
    # must stay owed, so the debt correctly stays armed (GPT 6.1 F1).
    _stop_reason = STOP_REASON_CANCELLED
    turn._stop_reason = _stop_reason
    await _persist_partial_reply(turn, _cancel_reason)
    if _cancel_reason != STOP_REASON_CANCELLED and slot.is_closing:
        # Closing the chat cut this turn off: one tab, the bulk archive or
        # ``close_target``, each of which fences the slot with ``begin_close``
        # before it cancels. Land the row a restart lands, so the chat reopened
        # from history reads as interrupted and offers Resume.
        from kiro_crew.dashboard.slot_persistence.turn_marker import (
            record_close_interruption,
        )

        record_close_interruption(slot)


async def _on_auth_required(turn: _ChatTurn, exc) -> None:
    """The session needs a sign-in."""
    _current_message = turn._current_message
    _synthetic_payload = turn._synthetic_payload
    _synthetic_recovery_turn = turn._synthetic_recovery_turn
    _turn_actor = turn._turn_actor
    slot = turn.slot
    state = turn.state
    # The signed-out CLI is discovered HERE, not by a probe: this is the
    # authoritative logout signal now that readiness is latched at boot.
    # Non-retryable — respawning hits the same wall — so never re-queue, and
    # latch the service signed-out so the fail-closed gates stop trusting a
    # stale ready value.
    message = turn.message
    session_key = turn.session_key
    sessions = turn.sessions
    logger.warning("ACP auth required in slot %s: %s", slot.key, exc)
    # Every queued prompt would hit the same wall. Popping them one by one
    # would drain the whole queue into identical failures, leaving nothing to
    # resume after the user signs in — so hold the queue intact instead.
    # The current system input was already popped before this turn began;
    # restore it through the ordinary recovery helper so its delivery
    # callbacks, provenance, and containment stamp survive post-login retry.
    # One auth failure owns one identity. Clear any older retry before this
    # turn decides whether it produced an exact replacement row.
    await _requeue_auth_retry(
        slot,
        message,
        _synthetic_recovery_turn=_synthetic_recovery_turn,
        _synthetic_payload=_synthetic_payload,
        _turn_actor=_turn_actor,
        _consumed_reported=turn._consumed_reported,
        _current_message=_current_message,
        _queue_recovery=functools.partial(_queue_recovery, turn),
    )
    _auth_required = True
    turn._auth_required = _auth_required
    needs_session_reset = True
    turn.needs_session_reset = needs_session_reset
    await _persist_partial_reply(turn, "error: auth required")
    _auth_msg = str(exc)
    # Stamped with a kind (live broadcast `kind`, rebuilt transcript
    # `meta.kind`) so the frontend can offer the fix -- a deep link to the
    # Kiro sign-in card -- instead of a Continue button that would hit the
    # same wall. The exception decides (from the harness that raised it)
    # whether that card IS the fix; on a harness with its own credential
    # store the row stays a plain error. The prose is unchanged.
    slot.append("error", _auth_msg, "msg msg-err", meta=_terminal_error_meta(exc))
    _mark_kiro_signed_out(state)
    await _deliver_linked_slack_message(state, slot, sessions, session_key, _auth_msg)


async def _on_prompt_busy(turn: _ChatTurn) -> None:
    """The session's prompt queue stayed busy."""
    _prompt_depth = turn._prompt_depth
    slot = turn.slot
    # Provider was killed after prompt-busy retries exhausted — reset (and
    # re-queue only when retry-eligible; see per-branch handling below).
    _is_synthetic = turn._is_synthetic
    _turn_emitted = turn._turn_emitted
    message = turn.message
    logger.info("Prompt busy exhausted in slot %s — resetting session", slot.key)
    needs_session_reset = True  # checked in _turn_release_session (chat_turn/session_reset.py)
    turn.needs_session_reset = needs_session_reset
    await _persist_partial_reply(turn, "error: prompt busy exhausted")
    slot._prompt_busy_retries += 1
    await _requeue_after_prompt_busy(
        slot,
        message,
        _prompt_depth=_prompt_depth,
        _turn_emitted=_turn_emitted,
        _is_synthetic=_is_synthetic,
        _queue_recovery=functools.partial(_queue_recovery, turn),
        _stop_pressed=functools.partial(_stop_pressed, turn),
    )


# Canary probe for the poisoned-conversation escalation: before any discard,
# ONE tool-free prompt is run through an ephemeral fresh background session
# (run_bg_oneliner). Only a canary that SUCCEEDS while this conversation keeps
# failing constitutes conversation-specific rejection evidence — the exact
# incident signature ("a fresh session works instantly while this session
# fails every prompt"). A canary that also fails means the backend itself is
# down/throttled, so no discard fires and nothing is consumed; the next
# user-initiated exhausted cycle re-probes. This replaces any error-text
# classification: the ACP classifier contract forbids branching on formatted
# message wording, and the canary needs no classification at all.
_POISON_CANARY_PROMPT = "Reply with the single word OK."


_POISON_CANARY_TIMEOUT_SECS = 30.0


async def _on_acp_error_card(turn: _ChatTurn, exc) -> None:
    """Any other ACP error: the error card and the turn's failure record."""
    _directive_loop_gen = turn._directive_loop_gen
    _directive_loop_id = turn._directive_loop_id
    _directive_self_wake = turn._directive_self_wake
    _escalation = turn._escalation
    _msg = turn._msg
    _prompt_depth = turn._prompt_depth
    _turn_emitted = turn._turn_emitted
    _turn_priority = turn._turn_priority
    _turn_thought = turn._turn_thought
    client = turn.client
    message = turn.message
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    await _persist_partial_reply(turn, "error: terminal")
    # ── Poisoned-conversation escalation ────────────────────────────
    # A transient-classified error that reaches this terminal branch
    # with ZERO output means a full retry ladder was exhausted
    # pre-stream (the transient elif in _run_chat's except AcpError chain only goes false
    # on budget exhaustion). ONE such cycle is plausibly a momentary outage; the
    # SECOND CONSECUTIVE one — ladder exhausted, the user pressed
    # Continue (or sent a new message), fresh ladder exhausted again —
    # is the signature of a POISONED persisted conversation: the
    # backend deterministically rejects this session's native history
    # while brand-new sessions on the same gateway+model work fine
    # (observed live: a session/load'ed conversation failing pre-stream
    # identically 11 hours apart, across separate kiro-cli processes,
    # while a new session answered instantly). Retrying into that
    # conversation can never succeed and the ❌ message's own advice
    # ("retry in a moment") sends the user in circles — the only thing
    # that recovers is what a manual "start a new chat" does: a fresh
    # native conversation. So escalate exactly that, in place:
    # DISCARD the native conversation — sessions.discard_conversation()
    # clears the resume sid (sessions.reset() would session/load the
    # same poisoned conversation right back) while KEEPING the
    # session-map entry, whose Slack thread/channel linkage must
    # survive — and re-queue the message once. The successor turn
    # cold-starts a fresh conversation with the slot transcript
    # re-injected as context, preserving the dashboard session.
    # ONE-SHOT per landed turn (``_escalation``): if even the
    # fresh conversation fails (genuine prolonged outage), the streak
    # keeps counting but no further discard fires until some turn
    # actually lands — a discard loop is impossible.
    # A transient-classified error with ZERO model activity (no
    # tokens, no tool call, no thinking) that reaches this terminal
    # branch means a full retry ladder was exhausted pre-stream. A
    # turn that streamed even reasoning died MID-generation — the
    # backend was serving this conversation — so it is not the
    # poisoned signature and resets the streak below. The signature
    # is deliberately classifier-only (no error-text
    # matching — the ACP classifier contract forbids branching on
    # formatted message wording); disambiguation between "poisoned
    # conversation" and "backend-wide outage/throttle" is done by the
    # CANARY PROBE below, not by classifying the error.
    _escalation.record_terminal_failure(
        emitted=_turn_emitted, thought=_turn_thought, transient=acp_error_is_transient(exc)
    )
    if (
        _escalation.should_probe()
        and _prompt_depth == 0
        and not _should_suppress_requeue(slot)
        # Resolved Stop: the canary snapshot below is taken after the
        # Stop moved the counter (see _on_process_died in acp_recovery.py).
        and not _stop_pressed(turn)
    ):
        # ── Canary probe: conversation-specific evidence, or bust ──
        # Two exhausted ladders alone cannot distinguish a poisoned
        # persisted conversation from a sustained backend-wide outage
        # or throttle, and discarding a healthy conversation is a
        # one-way door for its native state. So reproduce the actual
        # incident signature before acting: run ONE tool-free prompt
        # through an ephemeral FRESH background session. Only "the
        # fresh conversation answers while this one has failed
        # 2×(TRANSIENT_RETRIES+1) consecutive prompts" justifies the
        # discard. A canary that fails or times out means the backend
        # itself is unhealthy: no discard, the one-shot stays
        # UNconsumed, and the streak stays accrued so the next
        # user-initiated exhausted cycle re-probes — when the outage
        # ends but this conversation still fails, the discard fires
        # then, with evidence.
        _canary_ok = False
        # Snapshot the stop generation BEFORE the (up to 30s) probe: a
        # Stop pressed while the canary runs must veto the discard and
        # the re-queue — the user just cancelled this work, and
        # re-executing it as a synthetic recovery turn would run
        # cancelled tools. Re-reading _stop_state afterwards is NOT
        # enough (teardown can drive it back to "idle" concurrently —
        # the race documented in chat_handlers._make_stop_resolver);
        # the generation only ever counts up on initiations.
        _stop_gen_before_canary = slot._stop_generation
        _session_stop_gen_before_canary = _session_stop_generation(turn)
        # The canary must run on the SAME served model as the failing
        # session — a success on any other model (the cheap background
        # default, or a rejected-model fallback) says nothing about
        # whether THIS conversation is rejected, and would discard a
        # healthy conversation during a model-specific outage. Read it
        # via the provider's PUBLIC served_model accessor (never the
        # private _client internals — those are free to move);
        # strict_model makes it a hard requirement (set_model failure
        # or rejection raises instead of degrading). No readable
        # model ⇒ the probe cannot be trusted ⇒ inconclusive ⇒ no
        # discard.
        _session_model = str(getattr(client, "served_model", "") or "").strip()
        if _session_model:
            try:
                _canary_text = await run_bg_oneliner(
                    state.sessions,
                    _POISON_CANARY_PROMPT,
                    model=_session_model,
                    strict_model=True,
                    sel_source="poisoned_canary",
                    sel_session_key="_poison_canary",
                    timeout=_POISON_CANARY_TIMEOUT_SECS,
                    # Inside the failing turn: whoever waits on it waits here.
                    start_priority=_turn_priority,
                )
                # Require actual output: an empty completion is not
                # positive evidence that fresh conversations work.
                _canary_ok = bool(_canary_text.strip())
            except Exception as _canary_exc:
                logger.info(
                    "Poisoned-conversation canary failed for slot %s on "
                    "model %s (%s) — backend/model-wide failure, not "
                    "conversation-specific; no discard this cycle",
                    slot.key,
                    _session_model,
                    _canary_exc,
                )
        else:
            logger.info(
                "Poisoned-conversation canary skipped for slot %s — "
                "session model unreadable, probe would be meaningless; "
                "no discard this cycle",
                slot.key,
            )
    else:
        _canary_ok = False
    if _canary_ok and (
        slot._stop_generation != _stop_gen_before_canary
        or _session_stop_generation(turn) != _session_stop_gen_before_canary
    ):
        # A Stop was initiated while the canary ran (even if it already
        # resolved and _stop_state is back to "idle"): the user
        # cancelled this work mid-probe, so a positive canary must not
        # discard the conversation or re-queue the cancelled message.
        # Nothing is consumed — the one-shot stays armed and the streak
        # stays accrued for a later user-INITIATED cycle.
        _canary_ok = False
        logger.info(
            "Poisoned-conversation canary succeeded for slot %s but a "
            "stop was initiated during the probe — vetoing discard/requeue",
            slot.key,
        )
    if _canary_ok:
        logger.warning(
            "Pre-stream transient exhaustion on %d consecutive cycles in "
            "slot %s while a fresh canary conversation succeeded — the "
            "backend is rejecting THIS conversation specifically: "
            "discarding conversation for %s and re-queueing once on a "
            "fresh one",
            _escalation.exhausted_cycles,
            slot.key,
            session_key,
        )
        # Consume the one-shot HERE, where the recovery is actually
        # enqueued (mirrors _posttoken_retry_used accounting). The
        # streak stays: this discard came from it.
        _escalation.arm_discard(clear_streak=False)
        needs_conversation_discard = True  # checked in _turn_release_session (session_reset)
        turn.needs_conversation_discard = needs_conversation_discard
        slot.append(
            "error",
            "⟳ The backend keeps rejecting this conversation — "
            "restarting the model session and retrying (this chat's "
            "messages are kept; the model rebuilds its working "
            "context from them)…",
            "msg msg-err",
            meta={"kind": TRANSIENT_RETRY_KIND},
        )
        # The verbatim requeue carries the retry identity forward via
        # _queue_recovery itself (one mechanism for every recovery
        # family), so this site needs no site-local re-stamp.
        _queue_recovery(turn, 0, message, kind=SYNTHETIC_RECOVERY_KIND)
        # Fresh conversation ⇒ fresh ladder for the recovery cycle.
        slot._transient_5xx_retries = 0
        slot._infra_retries = 0
    else:
        _err_text = redact(str(exc))
        # Fallback-chain story (agent.fallback_model): when this cycle
        # walked fallback candidates and STILL landed here, the error
        # card must tell the whole story — the primary throttled AND
        # every fallback tried was also unavailable — not just the last
        # candidate's error. Model ids come from config (LLM-reachable
        # via the MCP config-write path), so they pass the same
        # redaction as the error text.
        if slot._fallback_walked:
            _fb_story = (
                f"{slot._fallback_primary_model or 'The selected model'} throttled; "
                f"fallbacks {', '.join(slot._fallback_walked)} also unavailable. "
            )
            _fb_story = redact(_fb_story)
            _err_text = _fb_story + _err_text
        # Structural tag for a model-entitlement rejection, so the
        # frontend can render the fix (model picker / Settings -> Chat)
        # instead of a Continue that re-runs the rejection. Decided from
        # the same evidence the formatter used — the rejected id is
        # absent from the session's advertised list — never from the
        # prose, which is what a copy edit or translation would move.
        # The same seam carries the sign-in tag: a session the upstream
        # rejected gets a "Sign in to Kiro" affordance instead.
        _unentitled_meta = _terminal_error_meta(exc)
        slot.append(
            "error",
            f"⏱️ {_err_text}" if "timed out" in _msg else f"❌ {_err_text}",
            "msg msg-err",
            meta=_unentitled_meta,
        )
        # Record a STRUCTURAL terminal outcome for this slot's last turn,
        # but ONLY for a self-driven nudge fire (``_directive_self_wake``).
        # A malformed-request rejection is deterministic in the payload's
        # SHAPE: re-sending the identical context reproduces it, so the
        # auto-nudge loop must stop re-firing rather than spend cycle
        # after cycle on the same rejection. The flag is read by the fire
        # path to STOP the loop, so it must reflect the LOOP's OWN cycle:
        # a HUMAN turn that happens to be malformed on a slot that also
        # carries an active loop must NOT set it, or the next fire would
        # stop an unrelated loop the human never drove. The nudge turn is
        # the only producer marked ``_directive_self_wake`` (gateway sets
        # it on the fire path), so gating here is exactly that scope.
        # Cleared at the start of every genuine new turn so a human
        # /clear-then-message re-arms the loop. getattr-guarded: only
        # AcpError carries the attribute, and this branch also catches
        # plain errors.
        if _directive_self_wake and getattr(exc, "structural_terminal", False):
            slot._last_turn_structural_terminal = True
            # Scope the verdict to the loop id AND the config generation
            # it fired under. The fire guard applies the stop through an
            # ATOMIC (id, generation) fence in AutoNudgeService, so a
            # stale completion of an old instruction cannot stop a loop
            # whose config advanced (A->B->A). loop id is still recorded
            # because a generation is per-loop, not globally unique.
            slot._last_turn_structural_terminal_loop_id = _directive_loop_id
            slot._last_turn_structural_terminal_loop_gen = _directive_loop_gen
        # A cycle that never obtained a model session. Same scope rule as
        # the structural verdict above -- only a self-driven nudge fire
        # counts, or a human turn that happened to be starved would spend
        # an unrelated loop's stand-down budget. The streak itself lives on
        # the loop (``notify_cycle_start_failed``), which backs the loop off
        # and eventually stands it down instead of firing cycle after cycle
        # that can only fail the same way. Read with getattr: the two ACP
        # exception families tag this fact independently and share no base.
        _note_cycle_start_failure(slot.key, exc, self_wake=_directive_self_wake)
        # A cycle that DID reach a model session and dispatched but then
        # died terminally here -- a backend error after retries were spent,
        # a persistent tool error, a prompt timeout. The gate (self-wake
        # only, structural / session-start / pre-dispatch excluded, scoped
        # to the fired loop's config generation) lives in the helper, which
        # _on_error (the generic ``except`` arm) also calls -- a report in only
        # one arm would miss whichever death the other serves. No row meta
        # is resolved in this arm (a structural rejection is excluded by its
        # own ``exc`` tag, and the pre-dispatch classes land in the generic
        # arm, not here), so ``err_meta`` is None.
        await _note_cycle_failure(
            slot.key,
            exc,
            self_wake=_directive_self_wake,
            loop_id=_directive_loop_id,
            expected_generation=_directive_loop_gen,
        )
        # This branch ENDS the retry cycle: the error is terminal and
        # nothing is re-queued. Refresh the transient-5xx budget now so the
        # NEXT cycle — the Continue press this very error message invites
        # ("retry in a moment"), or a new user message — gets the designed
        # TRANSIENT_RETRIES fresh attempts. Without this, the budget
        # consumed by a failed cycle leaks into every later cycle (the
        # happy-path reset only runs when a cycle COMPLETES), so after one
        # exhaustion ❌ a single further 5xx fails instantly with zero
        # retries until some turn happens to finish cleanly. Loop safety is
        # unchanged: the reset happens only on a NO-REQUEUE exit, so a new
        # budget always requires a new user- or system-initiated cycle —
        # automatic retry chains within a cycle stay bounded at
        # TRANSIENT_RETRIES. (_posttoken_retry_used needs no counterpart
        # here: it is already refreshed at genuine-turn start.)
        slot._transient_5xx_retries = 0
        if _prompt_depth == 0:
            _turn_retry_outcomes(slot, "exhausted")
        # Same NO-REQUEUE-exit rule for the L1 gateway-capacity count:
        # without it a cycle whose L1 continuation died here leaves the
        # slot reading "recovering" on the health panel until some later
        # turn happens to land (the happy-path reset only runs when a
        # cycle COMPLETES).
        slot._infra_retries = 0
        # Same terminal-cycle refresh for the fallback-chain walk state
        # (the sticky _active_fallback_model deliberately survives — the
        # session really is on the fallback until the restore probe
        # moves it back).
        slot._fallback_candidate_idx = 0
        slot._fallback_walked = []


async def _on_session_closing(turn: _ChatTurn) -> None:
    """The gateway is shutting down under the turn."""
    slot = turn.slot
    # Shutdown race, not a turn failure: the SessionManager began closing
    # while this turn was still in prep — get_or_create's registration gate
    # re-checks _closing under its lock and refuses to register a session
    # behind the shutdown snapshot (the provider it had already started is
    # hard-killed by the allocation path's own cleanup, logged as "killed
    # PID … for leaked provider"). Mirror the in-turn begin_turn gate in
    # _turn_dispatch_gates: log quietly, append no error card, and record no session
    # failure — nothing about this session is unhealthy, the gateway is
    # exiting, and the user's next send lands on the restarted process.
    logger.info("Aborting turn for %s — gateway is shutting down", slot.key)
    # This arm carries no `as exc` binding, so the class is named literally
    # -- the same shape as _on_cancelled. It is read only when the turn had
    # already opened; the documented raise happens in turn prep, which leaves
    # `_crew_log_turn_open` False and _turn_close_crew_log
    # (chat_turn/crew_log_attribution.py) emits nothing at all. Naming it here
    # is what keeps a turn closed by a shutdown from
    # being recorded as an unnamed failure.
    _crew_log_error = "SessionClosingError"
    turn._crew_log_error = _crew_log_error


async def _on_error(turn: _ChatTurn, exc) -> None:
    """Any other error the turn raised."""
    _directive_loop_gen = turn._directive_loop_gen
    _directive_loop_id = turn._directive_loop_id
    _directive_self_wake = turn._directive_self_wake
    slot = turn.slot
    state = turn.state
    _memory_preparation_admitted = turn._memory_preparation_admitted
    session_key = turn.session_key
    if _is_pre_spawn_refusal(exc):
        # An expected refusal before any agent process ran: the reason is
        # the whole story, and a traceback would read as a crash.
        logger.warning("Dashboard chat refused in slot %s: %s", slot.key, exc)
    else:
        logger.exception("Dashboard chat error in slot %s", slot.key)
    _crew_log_error = type(exc).__name__
    turn._crew_log_error = _crew_log_error
    # A prompt timeout from the shared runtime (AcpRequestTimeout) and any
    # other non-AcpError end land here: keep what streamed as the
    # interrupted reply, with its footer and usage row, like every other
    # abnormal end. Guarded so a failure here can never cost the error card.
    try:
        await _persist_partial_reply(turn, "error: terminal")
    except Exception:
        logger.debug("Keeping the partial reply failed for slot %s", slot.key, exc_info=True)
    _err_text = redact(str(exc))
    from kiro_crew.memory_startup import MemoryStartupUnavailable
    from kiro_crew.memory_stores import UnknownMemoryStore  # noqa: F811

    _err_meta: dict | None = None
    if isinstance(exc, (_MemoryUnavailable, UnknownMemoryStore)):
        _err_meta = {"code": "memory_unavailable"}
    elif isinstance(exc, CapabilityError) and exc.code in _SESSION_START_REFUSAL_TEXT:
        # A session-start capability refusal a retry repeats unchanged. The
        # row names the code's own fix, and the card offers that fix (or
        # none) instead of Resume.
        _refusal_text = _SESSION_START_REFUSAL_TEXT[exc.code]
        if exc.code == "project_identity_changed" and not exc.project:
            _refusal_text = _PROJECT_CHANGED_OTHER_TEXT
        _err_text = f"{exc.code}: {_refusal_text.format(file=exc.file, project=exc.project)}"
        _err_meta = {"code": exc.code, "member": exc.member}
        if exc.file:
            _err_meta["file"] = exc.file
        if exc.project:
            _err_meta["project"] = exc.project
    elif isinstance(exc, CapabilityStartupError) and str(exc) == "capability_harness_unsupported":
        # The bare code names neither the setting nor the saved spec, and
        # changing agent.acp_backend does nothing for a member chat.
        _err_text = (
            "capability_harness_unsupported: This crew member has saved "
            "capabilities, and the backend its chats run on "
            "(agent.member_acp_backend, not agent.acp_backend) cannot load "
            "them. Set agent.member_acp_backend to "
            f"{_member_capable_backend_names()}, or reset the member's "
            "Capabilities page, then start a new chat."
        )
        from kiro_crew.members import is_crewmate_worker_session

        if is_crewmate_worker_session(session_key):
            # A crewmate's worker is refused for its kit, which only some
            # member backends carry, so the member card's remedy does not fit.
            from kiro_crew.mate_worker import crewmate_worker_backend, worker_kit_backend_refusal

            _worker_reason = worker_kit_backend_refusal(
                crewmate_worker_backend(KiroCrewConfig.load().agent)
            )
            if _worker_reason:
                _err_text = "capability_harness_unsupported: " + _worker_reason
        _err_meta = _terminal_error_meta(exc)
    elif (
        isinstance(exc, CapabilityStartupError) and str(exc) == "capability_tool_limits_unsupported"
    ):
        _err_text = (
            "capability_tool_limits_unsupported: This crew member's saved "
            "capabilities turn off single tools on an MCP server. The backend "
            "its chats run on (agent.member_acp_backend) cannot turn off single "
            "tools, so they would be on. On the member's Capabilities page, turn "
            "the whole server off or turn those tools back on, then start a new "
            "chat."
        )
        _err_meta = _terminal_error_meta(exc)
    else:
        # A session start on the SHARED runtime lands here (see the sibling
        # note below), and its ``session_start_failed`` tag is what lets the
        # Continue endpoint count consecutive failed starts: the start
        # registered no session, so ``record_failure`` a few lines down
        # returns False and counts nothing. Every other exception reaching
        # this arm carries none of the tags and keeps its plain row.
        _err_meta = _terminal_error_meta(exc)
    slot.append("error", _err_text, "msg msg-err", meta=_err_meta)
    # The SIBLING of the tagged-start report in the AcpError branch (_on_acp_error_card).
    # A session start on the SHARED runtime raises AcpRequestTimeout /
    # AcpSessionStartTimeout, which descend from AcpRuntimeError and not from
    # AcpError, so they land here -- and a member slot is exactly the
    # population that runs there. Reporting only in the AcpError branch would
    # leave the loops this bound exists for never backing off.
    _note_cycle_start_failure(slot.key, exc, self_wake=_directive_self_wake)
    # The SIBLING of the failed-cycle report in the AcpError branch (_on_acp_error_card),
    # for the same reason the start-failure report is duplicated here: a
    # cycle that reached a session and dispatched but then died does not
    # always raise an AcpError. A prompt timeout (AcpRequestTimeout) and a
    # wedged session (AcpSessionStartTimeout) descend from AcpRuntimeError,
    # not AcpError, so they land in THIS arm, and an exhausted retry or a
    # plain Exception does too. Charging the streak only in the AcpError
    # branch would leave a loop failing this way firing cycle after cycle
    # that only fails the same way -- the exact waste this bound ends. The
    # gate is the helper's: same self-wake scope and the structural /
    # session-start exclusions as the AcpError arm, PLUS the pre-dispatch
    # exclusion that only this arm needs. A memory store that would not open
    # (``memory_unavailable``) and a member agent file changed out of band
    # (``materialization_changed``) never reached a session at all, so the
    # "reached a session and then died" stand-down -- and the operator notice
    # that names a backend/tool/timeout cause -- must not fire for them. They
    # are identified by the ``_err_meta`` resolved just above, passed through
    # so the helper needs no new classification. A plain internal bug keeps
    # charging: a self-wake cycle that dispatches and raises the same bug
    # every interval is exactly the no-progress waste this bound ends.
    await _note_cycle_failure(
        slot.key,
        exc,
        self_wake=_directive_self_wake,
        loop_id=_directive_loop_id,
        expected_generation=_directive_loop_gen,
        err_meta=_err_meta,
    )
    if not (isinstance(exc, MemoryStartupUnavailable) and not _memory_preparation_admitted):
        await state.sessions.record_failure(session_key)
