"""Running one prompt to its answer: ``stream_and_collect`` and its JSON variant.

Streaming a turn and collecting its text, answering its permission requests, retrying a
busy or transient backend, walking the fallback chain, summing the turn's billing, and
failing a turn the backend never finished.

Served through :mod:`kiro_crew.llm_helpers`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from kiro_crew.agent_sdk import CONTEXT_EVENT_COMPACTION
from kiro_crew.agent_sdk.drivers.acp_turn_vocab import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpError,
    LLMEvent,
    LLMProvider,
    TurnUsage,
)
from kiro_crew.agent_sdk.drivers.acp_vocab import (
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TIMEOUT,
    AcpTimeoutError,
)
from kiro_crew.hooks import fire_tool_hooks, get_global_hook_store
from kiro_crew.llm_helpers_parts.backend_errors import (
    _PROMPT_BUSY_DELAY,
    _PROMPT_BUSY_RETRIES,
    _TRANSIENT_RETRIES,
    TOOL_ACTIVITY_ATTR,
    PromptBusyExhaustedError,
    acp_error_is_transient,
    is_context_overflow_error,
    is_prompt_busy,
    transient_retry_delay,
)
from kiro_crew.llm_helpers_parts.model_fallback import (
    FALLBACK_CANDIDATE_ATTEMPTS,
    FALLBACK_STORY_ATTR,
    TURN_FALLBACK_ATTR,
    FallbackState,
    advance_fallback_candidate,
    first_advertised_fallback,
    probe_fallback_restore,
)
from kiro_crew.llm_helpers_parts.tool_approval import ToolApprovalPolicy, _resolve_permission
from kiro_crew.llm_helpers_parts.turn_results import parse_llm_json
from kiro_crew.llm_helpers_parts.turn_usage import (
    _TURN_BILLED_ATTR,
    _attempt_usage,
    _billing_stats,
    _sum_usage,
)
from kiro_crew.sel import sel as _sel

if TYPE_CHECKING:
    from kiro_crew.hooks import HookManager

logger = logging.getLogger("kiro_crew.llm_helpers")


#: Terminal stop reasons that mean the backend never finished the turn: the
#: session handle ends a turn its backend did not answer within the turn ceiling
#: as ``STOP_REASON_TIMEOUT``, and an unrecovered stale turn as
#: ``STOP_REASON_STALE_RECOVER``. A user-requested stop (``"cancelled"``) is not
#: one: its partial text is what the caller asked to keep.
_UNFINISHED_TURN_STOP_REASONS = frozenset({STOP_REASON_TIMEOUT, STOP_REASON_STALE_RECOVER})


def unfinished_turn_error(event: LLMEvent, partial_output: str) -> AcpTimeoutError | None:
    """The prompt-timeout error for a turn the backend never finished, else ``None``.

    Raised, not returned as an answer, so the caller's failure path runs: the text
    collected so far is not a result. It is the same error the per-process
    client raises when a prompt times out, carrying the partial text, and it is
    never transient: a retry would re-send the prompt to a backend that stopped
    answering and wait out the ceiling again. A collector that ends a turn on
    ``EVENT_COMPLETE`` asks this function rather than listing the unfinished
    stop reasons itself.
    """
    stop_reason = str(getattr(event, "stop_reason", "") or "")
    if stop_reason not in _UNFINISHED_TURN_STOP_REASONS:
        return None
    error = AcpTimeoutError(
        partial_output,
        message=f"ACP prompt timed out: the backend did not finish the turn ({stop_reason})",
    )
    error.transient = False
    return error


async def stream_and_collect(
    provider: LLMProvider,
    message: str,
    *,
    approval_policy: ToolApprovalPolicy = ToolApprovalPolicy.AUTO_APPROVE,
    hooks: HookManager | None = None,
    on_chunk: Callable[[str], None] | None = None,
    on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None,
    on_steer_consumed: Callable[[str], None] | None = None,
    on_complete: Callable[[LLMEvent], None] | None = None,
    on_compaction: Callable[[LLMEvent], None] | None = None,
    on_tool_gate: Callable[[str, bool, bool, str], None] | None = None,
    retry_transient: bool = True,
    max_turns: int | None = None,
    session_key: str = "",
    agent: str = "",
    app: str = "",
    model_fallback: bool = False,
    fallback_models: Sequence[str] = (),
    allow_image: bool = True,
    undelivered_watch_key: str = "",
) -> str:
    """Stream a message through an LLM provider and collect the full response.

    This is the core pattern used by cron, heartbeat, subagent, consolidator,
    taskrunner, and title generation.

    Args:
        provider: The LLM provider to stream through.
        message: The prompt to send.
        approval_policy: How to handle tool permission requests.
        hooks: HookManager for the HOOK_BASED and READ_ONLY approval policies.
            AUTO_APPROVE consults the shared identity-aware permission floor
            directly.
        on_chunk: Optional callback invoked with each text chunk (for progress).
        on_tool_approval: Optional async callback for interactive approval.
        on_steer_consumed: Optional callback invoked with the backend's
            ``steering_consumed`` echo text. A mid-turn steer is a
            fire-and-forget write, so this echo is the ONLY authoritative signal
            that the backend injected it; a caller that steers must observe this
            to know which of its steers to requeue when the turn ends.
        on_complete: Optional callback invoked with the provider's raw
            ``EVENT_COMPLETE``. It is not invoked when the stream exhausts or
            the caller cancels before that event. Raising from the callback is
            swallowed so observation cannot fail the completed turn.
        on_compaction: Optional callback invoked with each
            ``CONTEXT_EVENT_COMPACTION`` the backend reports during the turn; its
            ``text`` is the status (``started``, ``completed``, ``failed``). A
            backend that compacts the session on its own drops context earlier
            prompts carried, and this is how a caller learns of it. Raising from
            the callback is swallowed.
        on_tool_gate: Optional callback invoked once per tool permission
            decision with ``(tool_title, approved, security_blocked, reason)``. Lets a
            caller tell "the model did work" apart from "every tool the model
            attempted was blocked" — a distinction the returned text cannot
            carry, because a model whose tools were all refused still returns
            plausible prose. ``security_blocked`` is True only for the
            unconditional deny checks (sensitive path, sensitive bash, a deny
            pattern); a governance ``TOOL_DENY`` and an unattended-approval
            timeout are refusals that say nothing about the job, so they arrive
            with ``approved=False`` and ``security_blocked=False``. ``reason`` is
            the refusal text the audit row recorded (for a deny pattern it names
            the rule that matched), empty for an approval: without it an
            unattended caller can only say that a call was refused, not why.
            Delivered when the attempt settles, not mid-stream, and decisions
            from an abandoned retry attempt are discarded: they describe work
            the final turn never did. ``tool_title`` and ``reason`` can echo
            LLM-authored text: redact them before display or persistence. Raising
            from the callback is swallowed; observing a gate decision must never
            fail the turn.
        retry_transient: When True (default), transient backend errors are
            retried in-place with bounded backoff. Set False from callers that
            already own an outer transient-retry loop, so the inner arm doesn't
            compound their attempts (retry-layer amplification).
        max_turns: Optional cap on tool-call iterations per prompt. When reached,
            the event loop breaks and returns whatever text has been collected.
            None (default) means no limit.
        session_key: Calling surface's session key, forwarded to the PreToolUse
            gate. Empty (default) preserves every existing caller's behavior.
        agent: Calling agent name, forwarded to the gate alongside *session_key*.
        app: Owning app name, forwarded to the gate so the app's governance
            PROFILE is resolved — not just the enterprise ceiling.

            All three matter for ``AUTO_APPROVE``, ``HOOK_BASED``, and
            ``READ_ONLY`` callers. The gate resolves ``ceiling ∩ profile``, and
            it can only look up a profile it has been told the name of; with all
            three empty it applies the ceiling alone. ``REJECT_ALL`` runs no
            tools. Every other policy consults the gate before an approval.
        fallback_models: Ordered chain of model ids tried when the same-model
            transient budget exhausts on a throttle/capacity error (Case 2.75).
            Empty (the default) disables the chain — behavior is byte-for-byte
            today's fail-loudly. Requires ``retry_transient=True`` (a caller
            that owns the outer transient loop also owns any fallback policy).
            Every swap is logged at warning and published on the provider via
            :data:`TURN_FALLBACK_ATTR`; the swap is sticky for the session and
            a later call on the same provider probes one primary restore.
        allow_image: ``False`` sends every attempt text-only (see
            ``LLMProvider.stream``), for a prompt that is text ABOUT a session.
        undelivered_watch_key: The session key the caller authenticated for this
            turn, used only to record a dispatched worker's work-tool call the
            harness never delivered
            (:class:`kiro_crew.undelivered_call_watch.UndeliveredCallWatch`).
            Empty watches nothing.

    Returns:
        The complete response text.
    """
    _undelivered_watch = None
    if undelivered_watch_key:
        from kiro_crew.undelivered_call_watch import UndeliveredCallWatch

        _undelivered_watch = UndeliveredCallWatch(undelivered_watch_key, provider)
    transient_attempts = 0
    _model_fallback_attempted = False
    attempt = 0
    _fb_chain = tuple(
        m.strip() for m in (fallback_models or ()) if isinstance(m, str) and m.strip()
    )
    _fb_state = FallbackState(_fb_chain) if _fb_chain else None
    # Cross-attempt tool activity for every retry that replays the ORIGINAL
    # prompt. A tool can complete an external mutation before any text streams,
    # so both the same-model retry (Case 2) and fallback chain (Case 2.75) stop
    # once any attempt fires a tool call. Prompt-busy keeps its separate retry
    # contract, but activity from that attempt remains sticky for a later
    # transient error.
    _turn_tool_activity = False
    # Sticky-restore probe (§restore policy): if an earlier turn on this
    # provider fell back, try ONCE to move back to the primary before this
    # turn streams. Quiet on success (log only); a still-throttled primary
    # keeps the fallback for this turn.
    if getattr(provider, TURN_FALLBACK_ATTR, None) is not None:
        await probe_fallback_restore(provider, surface="stream_and_collect")
    # Accumulates across attempts, so it lives OUTSIDE the retry loop: a turn that
    # was billed and then retried must report the sum, not the last attempt.
    turn_billed = TurnUsage()
    while True:
        result_text = ""
        tool_call_count = 0
        # Consumption is committed on every exit EXCEPT a retry. A retry re-sends the
        # original message without the steer, so committing there would mark a steer
        # delivered that the model never saw. Every other exit — success or failure — is
        # terminal for this steer: the backend already consumed it, and `consumed` is what
        # suppresses the requeue, so dropping the acknowledgement makes the cleanup hand an
        # already-answered question back and ask it twice. Re-initialised per attempt.
        consumed_this_attempt: list[str] = []
        # Same per-attempt discipline as the steer acknowledgements above, and for the
        # same reason: a retry re-sends the original message, so decisions from an
        # abandoned attempt describe work the final turn never did. Committing them
        # would let a refusal from a discarded attempt outvote a clean retry and fail
        # a healthy job.
        gate_this_attempt: list[tuple[str, bool, bool, str]] = []
        # Tool calls that reached the gate, and tool calls that actually ran.
        # A tool auto-approved upstream never raises a permission request, so it
        # executes without a gate decision: correlating the two by
        # ``tool_call_id`` is what lets a caller see that work happened.
        gate_decided_ids: set[str] = set()
        executed_calls: list[tuple[str, str]] = []
        retrying = False
        # Billing accrued on THIS attempt is measured against the stats object as
        # it stands now: a retry installs a fresh one, so without a per-attempt
        # baseline an attempt that was billed and then failed is invisible.
        attempt_stats_before = _billing_stats(provider)
        try:
            events = (
                provider.stream(message)
                if allow_image
                else provider.stream(message, allow_image=False)
            )
            async for event in events:
                if _undelivered_watch is not None:
                    # A dispatched worker's report the harness never delivered.
                    if event.kind == EVENT_TOOL_CALL:
                        _undelivered_watch.on_call(event)
                    elif event.kind == EVENT_TOOL_RESULT:
                        await _undelivered_watch.on_result(event)
                if event.kind == EVENT_TEXT_CHUNK:
                    result_text += event.text
                    if on_chunk:
                        on_chunk(event.text)
                elif event.kind == EVENT_PERMISSION_REQUEST:
                    # Captures this decision's outcome and mechanism, so a hard
                    # security block is distinguishable from a governance denial
                    # or an unattended-approval timeout. Only the former says
                    # anything about the job itself.
                    _decision: list[tuple[str, str, str]] = []
                    approved = await _resolve_permission(
                        provider,
                        event,
                        approval_policy,
                        hooks,
                        on_tool_approval,
                        session_key=session_key,
                        agent=agent,
                        app=app,
                        on_decision=lambda outcome, mech, why: _decision.append(
                            (outcome, mech, why)
                        ),
                    )
                    if on_tool_gate:
                        _mech, _why = _decision[-1][1:] if _decision else ("", "")
                        gate_this_attempt.append(
                            (event.title or "", approved, _mech.startswith("always_deny"), _why)
                        )
                        if event.tool_call_id:
                            gate_decided_ids.add(event.tool_call_id)
                    if not approved:
                        continue
                elif event.kind == EVENT_TOOL_CALL:
                    tool_call_count += 1
                    # Sticky across attempts (never reset in the retry loop):
                    # once ANY attempt fired a tool, a transient path must not
                    # replay the original prompt — see _turn_tool_activity.
                    _turn_tool_activity = True
                    if on_tool_gate:
                        executed_calls.append((event.tool_call_id or "", event.title or ""))
                    if max_turns is not None and tool_call_count > max_turns:
                        logger.warning(
                            "max_turns=%d exceeded (%d tool calls), breaking",
                            max_turns,
                            tool_call_count,
                        )
                        _sel().log_tool_invocation(
                            session_key="",
                            source="llm_helpers",
                            tool_name=event.title or "",
                            tool_kind=event.tool_kind,
                            outcome="denied_max_turns",
                            metadata={"max_turns": max_turns, "count": tool_call_count},
                        )
                        break
                    # Fire PreToolUse hooks for auto-approved tools (informational only)
                    _sel().log_tool_invocation(
                        session_key="",
                        source="llm_helpers",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                    )
                    await fire_tool_hooks(
                        get_global_hook_store(),
                        event.title,
                        event.tool_input,
                    )
                elif event.kind == EVENT_STEER_CONSUMED:
                    consumed_this_attempt.append(event.text or "")
                elif event.kind == CONTEXT_EVENT_COMPACTION:
                    if on_compaction:
                        try:
                            on_compaction(event)
                        except Exception:
                            logger.debug("on_compaction callback failed", exc_info=True)
                elif event.kind == EVENT_COMPLETE:
                    if on_complete:
                        try:
                            on_complete(event)
                        except Exception:
                            logger.debug("on_complete callback failed", exc_info=True)
                    unfinished = unfinished_turn_error(event, result_text)
                    if unfinished is not None:
                        raise unfinished
                    break
            return result_text
        except AcpError as exc:
            msg = str(exc)
            if is_context_overflow_error(exc):
                # Left on the provider for ``recycle_background``: a caller that
                # swallows this error (history consolidation does) still has its
                # shared ``_bg`` conversation replaced rather than kept at a size
                # every later turn is refused on.
                from kiro_crew.session_background import mark_context_overflowed

                mark_context_overflowed(provider)
            # See is_prompt_busy for why this is structural rather than a
            # substring test. Both arms below (cancel+retry and
            # PromptBusyExhaustedError) hang off it, and the unattended callers
            # (workflows/agent_pool, handlers/side, the subagent-completion
            # injector) depend on them to reset a wedged parent session, so a
            # missed wedge surfaces a generic failure and leaves the session
            # stuck.
            busy = is_prompt_busy(exc)

            # ── Case 1: prompt-busy (provider mid-turn) — cancel + retry. ──
            if busy:
                if attempt >= _PROMPT_BUSY_RETRIES:
                    # Provider is permanently stuck — kill it so the next
                    # get_or_create cold-starts a fresh process.
                    logger.warning(
                        "Prompt busy after %d retries, shutting down provider", _PROMPT_BUSY_RETRIES
                    )
                    try:
                        await provider.shutdown()
                    except Exception:
                        logger.debug("Provider shutdown after busy retries failed", exc_info=True)
                    raise PromptBusyExhaustedError(msg) from exc
                logger.warning(
                    "Prompt busy (attempt %d/%d), cancelling and retrying: %s",
                    attempt + 1,
                    _PROMPT_BUSY_RETRIES,
                    exc,
                )
                try:
                    await provider.cancel()
                except Exception:
                    logger.debug("Cancel before retry failed", exc_info=True)
                await asyncio.sleep(_PROMPT_BUSY_DELAY * (2**attempt))
                attempt += 1
                retrying = True
                continue

            # ── Case 2: transient backend (Bedrock 5xx / throttle / stream) ──
            # Credential is valid; the server hiccupped. Retry with exponential
            # backoff + jitter. Distinct budget from prompt-busy.
            #
            # Guards:
            #   - retry_transient: callers that own an outer transient loop pass
            #     False so the inner arm doesn't compound their attempts.
            #   - `not result_text`: only retry if NO tokens have streamed yet.
            #     A partial response must not be retried — the re-run would
            #     duplicate the already-emitted output.
            #   - `not _turn_tool_activity`: only retry if no attempt fired a
            #     tool call. A textless tool can already have mutated state.
            if (
                retry_transient
                and not result_text
                and not _turn_tool_activity
                and acp_error_is_transient(exc)
                and transient_attempts < _TRANSIENT_RETRIES
            ):
                transient_attempts += 1
                # Exponential backoff with per-process jitter (see _JITTER_RNG):
                # deterministic within a process for tests, uniform across the
                # fleet so co-located peers don't retry in lockstep.
                delay = transient_retry_delay(transient_attempts)
                logger.warning(
                    "Transient backend error (attempt %d/%d), retrying in %.1fs: %s",
                    transient_attempts,
                    _TRANSIENT_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                retrying = True
                continue

            # ── Case 2.75: throttle-exhaustion fallback chain ──
            # The same-model budget (Case 2) is spent and the error is still
            # transient (throttle/capacity — a throttle carries no rejection
            # metadata, so Case 2.5 can never fire for it). Walk the configured
            # chain: substitute set_model, then re-prompt. Two attempts per
            # candidate (initial + one ~2s retry — see FALLBACK_CANDIDATE_
            # ATTEMPTS), advance on transient failure, propagate non-transient
            # immediately (the classifier gate above already ensures that).
            # Empty chain ⇒ this block is inert and Case 3 surfaces the error
            # directly.
            #
            # ``not _turn_tool_activity`` is load-bearing over and above
            # ``not result_text``: a tool call can complete an EXTERNAL
            # MUTATION before any text streams. Any fired tool across ANY
            # attempt disables every original-prompt replay for this call.
            if (
                retry_transient
                and not result_text
                and not _turn_tool_activity
                and _fb_state is not None
                and acp_error_is_transient(exc)
                and transient_attempts >= _TRANSIENT_RETRIES
            ):
                if _fb_state.should_retry_active():
                    # Final attempt on the current candidate — the shared
                    # budget body already recorded it.
                    delay = transient_retry_delay(1)
                    logger.warning(
                        "model fallback: candidate %s still failing (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        _fb_state.active,
                        _fb_state.attempts,
                        FALLBACK_CANDIDATE_ATTEMPTS,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    retrying = True
                    continue
                # Advance to the next usable candidate via the shared walk
                # step (marker-seeded primary, skip-active, substitute
                # set_model, sticky-marker publish, greppable warning).
                _cand = await advance_fallback_candidate(
                    provider, _fb_state, surface="stream_and_collect"
                )
                if _cand is not None:
                    await asyncio.sleep(transient_retry_delay(1))
                    retrying = True
                    continue
                _story = _fb_state.exhaustion_story()
                if _story:
                    # Chain exhausted: surface the ORIGINAL error class with the
                    # chain's story attached for the delivering surface, and
                    # keep the incident greppable.
                    logger.warning(
                        "model fallback: chain exhausted (%s); surfacing original error: %s",
                        _story,
                        exc,
                    )
                    try:
                        setattr(exc, FALLBACK_STORY_ATTR, _story)
                    except Exception:
                        pass
                # Fall through to Case 2.5 / Case 3.

            # ── Case 2.5: model rejected (e.g. "auto" on GovCloud) — retry once
            # with the first advertised model. ──
            # Same reactive fallback as run_bg_oneliner: some partitions do not
            # serve the "auto" sentinel, and the advertised list cannot gate it
            # statically. When the backend rejects a model AND names available
            # alternatives, retry ONCE with the first usable advertised model.
            # Only fires when no tokens have streamed (safe to replay) and the
            # error carries rejection metadata.
            #
            # OPT-IN (model_fallback=True): a silent model swap is only correct
            # for a caller that did NOT choose the model — a background/system
            # turn on the governed "auto" (history consolidation). An interactive
            # turn where the user picked a model must surface the rejection, not
            # swap underneath them (AGENTS.md), so the default is off.
            rejected = getattr(exc, "rejected_model", None)
            advertised = getattr(exc, "advertised", None) or []
            if (
                model_fallback
                and not result_text
                and rejected
                and advertised
                and not _model_fallback_attempted
            ):
                fallback = first_advertised_fallback(advertised, rejected)
                if fallback:
                    _model_fallback_attempted = True
                    set_model_fn = getattr(provider, "set_model", None)
                    if set_model_fn:
                        try:
                            await set_model_fn(fallback)
                        except Exception:
                            logger.debug(
                                "set_model(%r) failed during model fallback",
                                fallback,
                                exc_info=True,
                            )
                    logger.warning(
                        "stream_and_collect: model %r rejected; " "retrying once with %r",
                        rejected,
                        fallback,
                    )
                    retrying = True
                    continue

            # ── Case 3: fatal (auth, validation, exhausted retries) — propagate. ──
            # Say whether a tool fired this turn, so a caller that re-runs the
            # prompt on a fresh session can refuse to repeat a side effect.
            setattr(exc, TOOL_ACTIVITY_ATTR, _turn_tool_activity)
            raise
        finally:
            # Runs before the value reaches the caller on success, and before the exception
            # propagates on failure, so the acknowledgement always precedes the cleanup that
            # would otherwise requeue the question.
            #
            # Billing is folded in on EVERY attempt, including the ones abandoned by a
            # retry: an attempt whose metering frame landed before the error was billed,
            # and the retry replaces the stats object that carried it. The total is
            # published on the attempt that is terminal -- the same `not retrying`
            # condition the callbacks below use -- so one logical turn publishes once,
            # whether it returns or raises.
            turn_billed = _sum_usage(
                turn_billed, _attempt_usage(provider, since=attempt_stats_before)
            )
            if not retrying:
                try:
                    setattr(
                        provider,
                        _TURN_BILLED_ATTR,
                        (_billing_stats(provider), turn_billed),
                    )
                except Exception:
                    # A provider that refuses the attribute (slots, frozen doubles)
                    # simply leaves the caller on the live-stats fallback.
                    logger.debug("publishing accumulated turn usage failed", exc_info=True)
            if not retrying and on_steer_consumed:
                for consumed_text in consumed_this_attempt:
                    on_steer_consumed(consumed_text)
            if not retrying and on_tool_gate:
                for gate_title, gate_approved, gate_blocked, gate_reason in gate_this_attempt:
                    try:
                        on_tool_gate(gate_title, gate_approved, gate_blocked, gate_reason)
                    except Exception:
                        # A caller's bookkeeping must never abort the turn.
                        logger.debug("on_tool_gate callback failed", exc_info=True)
                # A tool that executed without a matching gate decision was
                # permitted upstream, so it counts as an approval: work happened.
                # An id-less execution cannot be correlated, so it counts too —
                # over-reporting work risks a missed detection, under-reporting
                # it fails a job that did something.
                for _exec_id, _exec_title in executed_calls:
                    if _exec_id and _exec_id in gate_decided_ids:
                        continue
                    try:
                        on_tool_gate(_exec_title, True, False, "")
                    except Exception:
                        logger.debug("on_tool_gate callback failed", exc_info=True)


async def stream_and_collect_json(
    provider: LLMProvider,
    message: str,
    *,
    approval_policy: ToolApprovalPolicy = ToolApprovalPolicy.AUTO_APPROVE,
    hooks: HookManager | None = None,
    on_chunk: Callable[[str], None] | None = None,
    model_fallback: bool = False,
    allow_image: bool = True,
) -> dict | None:
    """Stream a message and parse the response as JSON.

    Combines ``stream_and_collect`` with ``parse_llm_json``.
    Returns parsed dict or None on failure.
    """
    text = await stream_and_collect(
        provider,
        message,
        approval_policy=approval_policy,
        hooks=hooks,
        on_chunk=on_chunk,
        model_fallback=model_fallback,
        allow_image=allow_image,
    )
    return parse_llm_json(text)
