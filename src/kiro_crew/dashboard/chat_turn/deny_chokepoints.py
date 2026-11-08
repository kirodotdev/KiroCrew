"""Answering a dashboard turn's permission request with a rejection: redacted deny rows, the
security-event audit before the wire, and the bounded in-band steer of the reason."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import STEER_NOTICE_BOUND_SECS
from kiro_crew.dashboard.state import (
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_INVALID_NAME,
    DENY_CAUSE_POLICY,
    REFUSAL_INBAND_RECOVERY_PREFIX,
    build_refusal_steer_notice,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
    )
    from kiro_crew.dashboard.chat_turn.tool_approval import (
        _credential_tool_hint_for,
        _pre_tool_block_reason,
    )


def _redact_display_text(text: str) -> str:
    """Redact model-authored display text for an external surface.

    ``event.title`` prefers the model's own ``description`` field
    (``_select_tool_title``), so any surface it reaches — a transcript row that
    is broadcast to the dashboard AND persisted to the ConversationLog, or a
    SEL audit ``tool_name`` — must see it only through this helper. Both
    redactors return their input unchanged when nothing matches, so clean
    titles pass through byte-identical.
    """
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _redacted_hook_block(event: Any, pre_hook_results: Any) -> tuple[str, str]:
    """Build a redacted ``(tool title, hook reason)`` recovery entry."""
    return (
        _redact_display_text(event.title),
        _redact_display_text(_pre_tool_block_reason(pre_hook_results)),
    )


#: Upper bound on any in-band steer-notice write. The notice is best-effort,
#: but the AWAIT must not be: every deny path runs reject_tool + a SEL audit
#: write after :func:`_steer_policy_notice`, and unbounded, a backpressured ACP
#: stdin could hold the await until the turn deadline cancelled the coroutine —
#: skipping both. Sized to a pipe write with margin, far below the 60s approval
#: reporting margin, and applied inside the helper so every caller inherits it.
_STEER_NOTICE_BOUND_SECS = STEER_NOTICE_BOUND_SECS


async def _steer_repeat_loop_notice(client: Any, notice: str) -> bool:
    """Steer a repeat-loop notice into the running turn, best-effort and bounded.

    Same capability gate and bound as :func:`_steer_policy_notice`, but the
    notice is advice, not a deny correction: it never joins the turn's refusal
    ledger, so an unconfirmed one cannot schedule a recovery continuation.
    """
    if not notice or not getattr(client, "supports_refusal_steer", False):
        return False
    try:
        sent = await asyncio.wait_for(client.steer(notice), timeout=_STEER_NOTICE_BOUND_SECS)
    except Exception:
        logger.debug("repeat-loop steer failed", exc_info=True)
        return False
    return sent is not False


async def _steer_policy_notice(
    client: Any,
    title: str,
    reason: str,
    notices: list[str],
    slot: Any = None,
    state: Any = None,
    *,
    cause: str = DENY_CAUSE_POLICY,
) -> bool:
    """Hand a deny reason to the model IN-BAND, before the rejection is answered.

    Must be called while the ``session/request_permission`` is still unanswered.
    That ordering is the whole mechanism: the turn is provably in flight (the
    backend is blocked on our response), so ``_session/steer`` is queued instead
    of dropped, and the backend folds it in at the next model-inference boundary
    — the one right after the rejected tool resolves. The model reads the real
    reason inside the SAME turn, so the fallback recovery continuation (a second
    billed turn) is not needed.

    Opt-in by positive capability, never by harness identity: a backend outside
    ``ACP_BACKENDS_STEER`` has no ``_session/steer``, reports
    ``supports_refusal_steer`` False, and keeps the recovery-continuation
    behaviour unchanged. That includes codex, whose user steer travels on
    ``_session/steering`` but whose approval answer discards it with the turn.
    ``getattr`` guards the attribute because the reject paths also run
    against minimal test doubles.

    Appends the notice to *notices* (the turn's pending list, settled later by the
    ``steering_consumed`` echo) and returns whether it was written. Best-effort:
    steering is an optimisation on top of a fallback that still works, so a
    failure here must never turn a clean policy block into a turn error.

    *cause* picks the notice's cause-specific wording; see
    :func:`build_refusal_steer_notice`. Every deny path that reaches a model runs
    through here, so a new one states its cause rather than inheriting "policy".
    """
    if not getattr(client, "supports_refusal_steer", False):
        return False
    notice = build_refusal_steer_notice(
        title,
        reason,
        cause=cause,
        credential_tool_hint=await _credential_tool_hint_for(reason, cause, title),
    )
    if not notice:
        return False
    try:
        # BOUNDED: every deny path runs reject_tool + a SEL audit write after
        # this helper, and an unbounded await on a backpressured ACP stdin
        # could ride until the turn deadline cancelled the coroutine — skipping
        # both, so the UI would read rejected while the wire and the audit
        # trail never heard about it. Bounding HERE makes every caller inherit
        # the guard instead of each deny site re-discovering it.
        # asyncio.TimeoutError is an Exception, so the handler below already
        # treats expiry as the ordinary best-effort failure it is.
        sent = await asyncio.wait_for(client.steer(notice), timeout=_STEER_NOTICE_BOUND_SECS)
    except Exception:
        logger.debug("policy-notice steer failed; falling back to recovery turn", exc_info=True)
        return False
    if not sent:
        return False
    notices.append(notice)
    # Display-only row, appended ONLY once the steer is actually on the wire: it
    # tells the person a policy blocked the call and why, rendered by the shared
    # blocked-tool card. Nothing is
    # queued and no turn is dispatched (the agent already has the reason), which
    # is why the marker's own copy does not claim a recovery. Appending it when
    # the steer had NOT been written would assert an explanation the model never
    # received. Best-effort for the same reason as the steer itself.
    if slot is not None:
        try:
            # The cause rides the MARKER line, following the
            # HOOK_HALTED_RECOVERY_PREFIX precedent (`… #<depth>`): the card's
            # always-visible summary has to name the right cause. Rendering one
            # "safety policy blocked the call" line for every cause would tell the
            # person to go audit a security rule that does not exist — the same
            # cause-blind wording the model is spared, left in place for the human.
            slot.append(
                "inject",
                f"{REFUSAL_INBAND_RECOVERY_PREFIX} {cause}\n{notice}",
                "msg msg-inject",
            )
            if state is not None:
                state.push_slots_update()
        except Exception:
            logger.debug("policy-notice display row failed", exc_info=True)
    return True


async def _reject_attributed(client: Any, request_id: Any, *, attribution: str) -> None:
    """Answer a permission request with a rejection whose attribution is stated.

    kiro-cli tells the model "User denied tool execution" for EVERY rejection,
    which is false for a host deny. Outside the ``_reject_*`` helpers above,
    every answer site in this module goes through here and must say how the
    model learns the real cause: ``"steered"`` when a ``_steer_policy_notice``
    precedes this answer on every path, or ``"exempt: <why>"`` when the generic
    message is TRUE on the path left unsteered (a provenance-gated steer names
    the branch it leaves alone). Keyword-only and required, so a new site that
    omits it is a mypy error; test_refusal_inband_notice.py checks the value.
    """
    await client.reject_tool(request_id)


async def _reject_hook_blocked(
    client: Any,
    slot: Any,
    event: Any,
    *,
    session_key: str,
    pre_hook_results: Any,
    refusal_reasons: list[tuple[str, str]],
    refusal_notices: list[str] | None,
    metadata: dict | None = None,
) -> None:
    """Deny a tool a PreToolUse hook blocked, and record WHY for the model.

    Five things have to happen together: reject the call, show the user a blocked
    row, audit the denial, tell the model the reason in-band, and append that
    reason to ``refusal_reasons`` so the fallback recovery nudge can carry it when
    the in-band notice could not be delivered. Doing the first three without the
    last two is the defect this fixes — the turn dies silently and the model
    stalls with no reason to adapt to, while every other signal looks correct.
    They live here rather than at each permission path so a path added later
    cannot deny by omission.

    ``refusal_notices`` is the turn's pending in-band notice list; omitted (None)
    means the caller wants the fallback-only behaviour, which is what the direct
    unit tests of this helper exercise.
    """
    # event.title prefers the model's own `description` field (_select_tool_title),
    # so it is LLM-controlled display text. Redact once up front and use that
    # everywhere: the row is broadcast to the dashboard AND persisted to the
    # ConversationLog, and the sibling reject path (_safe_reject_title) redacts for
    # both that row and the audit.
    title, reason = _redacted_hook_block(event, pre_hook_results)
    # Audit FIRST, before any wire I/O for this decision: the steer and the
    # rejection both await the ACP pipe, and a backend that stops reading stdin
    # blocks those awaits until the turn deadline cancels this coroutine — an
    # SEL write sequenced after them never runs. Record the decision, then
    # attempt delivery.
    sel().log_tool_invocation(
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name=title,
        tool_kind=event.tool_kind,
        outcome="hook_blocked",
        request_id=event.request_id,
        metadata=metadata,
    )
    # BEFORE reject_tool: the unanswered permission request is what proves the
    # turn is still in flight, so the steer is queued rather than dropped.
    if refusal_notices is not None:
        await _steer_policy_notice(client, title, reason, refusal_notices, slot)
    await client.reject_tool(event.request_id)
    slot.append("tool", f"🚫 {title} (hook blocked)", "msg msg-tool")
    refusal_reasons.append((title, reason))


async def _reject_invalid_tool(
    client: Any,
    slot: Any,
    event: Any,
    *,
    session_key: str,
    error: Exception,
    refusal_reasons: list[tuple[str, str]],
    refusal_notices: list[str] | None,
    state: Any = None,
    metadata: dict | None = None,
) -> None:
    """Deny a tool whose display name failed validation, on redacted surfaces.

    Reject, blocked row, audit, the in-band notice, and the fallback entry live
    together so a permission path added later cannot deny by omission: the title
    is redacted once here and used for the transcript row (broadcast to the
    dashboard AND persisted to the ConversationLog), the audit ``tool_name``, and
    the notice. The validation *error* needs no redaction —
    ``_validate_tool_name`` raises only fixed messages that never echo the
    offending name.

    This is the deny the model can actually FIX: the rejected name is its own
    output, so being told which validation failed lets it reissue the call inside
    the same turn. Without that it reads kiro-cli's "User denied tool execution",
    concludes the person refused the action, and abandons a call nobody objected
    to.

    ``refusal_notices`` is REQUIRED and may be ``None`` for fallback-only callers.
    Required rather than defaulted because an omitted notice list is invisible at
    the call site and silently restores the pre-notice behaviour — which is
    exactly the by-omission gap this change set out to close, and which a default
    would let a future call site re-open while compiling and passing tests.
    """
    title = _redact_display_text(event.title)
    _reason = str(error)
    # Audit FIRST, before any wire I/O for this decision (see
    # _reject_hook_blocked): a stalled ACP reader cancels this coroutine at the
    # turn deadline, and an SEL write sequenced after the awaits never runs.
    sel().log_tool_invocation(
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name=title,
        tool_kind=event.tool_kind,
        outcome="denied",
        request_id=event.request_id,
        error=f"validation_failed: {error}",
        metadata=metadata,
    )
    # BEFORE reject_tool: the unanswered permission request is what proves the
    # turn is still in flight, so the steer is queued rather than dropped.
    if refusal_notices is not None:
        await _steer_policy_notice(
            client,
            title,
            _reason,
            refusal_notices,
            slot,
            state,
            cause=DENY_CAUSE_INVALID_NAME,
        )
    await client.reject_tool(event.request_id)
    slot.append("tool", f"🚫 {title} (invalid: {error})", "msg msg-tool")
    # The fallback's input. Without this entry a harness with no steer — or a
    # steer that was never folded in — leaves this deny with NO channel to the
    # model at all, while the policy path still gets its continuation. The
    # in-band notice is the primary path, never the only one.
    refusal_reasons.append((title, _reason))


async def _reject_hook_error(
    client: Any,
    slot: Any,
    event: Any,
    *,
    session_key: str,
    error: str,
    refusal_reasons: list[tuple[str, str]],
    refusal_notices: list[str] | None,
    state: Any = None,
    metadata: dict | None = None,
) -> None:
    """Deny a tool whose PreToolUse hook fire raised, on redacted surfaces.

    Same chokepoint shape as :func:`_reject_invalid_tool`, including the same
    REQUIRED ``refusal_notices`` for the same reason: the model-authored title is
    redacted once and reaches the blocked row, the audit, the in-band notice and
    the fallback entry only in that form. *error* is the hook exception text;
    hooks are fired with the tool name and parsed input, so an exception that
    wraps its inputs can carry model-authored text — redact it before the audit
    AND before it reaches the model.

    The notice matters most here because nothing judged the call: a hook faulted
    while deciding it. Left with kiro-cli's "User denied tool execution" the model
    infers a refusal that never happened and routes around an action that was
    never actually denied.
    """
    title = _redact_display_text(event.title)
    _safe_error = _redact_display_text(error)
    # Audit FIRST, before any wire I/O for this decision (see
    # _reject_hook_blocked): a stalled ACP reader cancels this coroutine at the
    # turn deadline, and an SEL write sequenced after the awaits never runs.
    sel().log_tool_invocation(
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name=title,
        tool_kind=event.tool_kind,
        outcome="hook_error",
        request_id=event.request_id,
        error=_safe_error,
        metadata=metadata,
    )
    # BEFORE reject_tool, for the same in-flight-turn reason as the sibling paths.
    if refusal_notices is not None:
        await _steer_policy_notice(
            client,
            title,
            _safe_error,
            refusal_notices,
            slot,
            state,
            cause=DENY_CAUSE_HOOK_ERROR,
        )
    await client.reject_tool(event.request_id)
    slot.append("tool", f"🚫 {title} (hook error)", "msg msg-tool")
    # See _reject_invalid_tool: the fallback needs an entry or this deny reaches
    # the model through no channel at all when the steer could not be delivered.
    refusal_reasons.append((title, _safe_error))
