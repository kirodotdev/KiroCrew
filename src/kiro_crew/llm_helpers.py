"""Shared LLM interaction helpers — stream collection, JSON parsing, history saving.

Eliminates duplicate code across gateway, handler, dashboard, taskrunner,
subagent, and history modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.client import AcpError, AcpPromptBusy
from kiro_crew.acp.types import TurnUsage
from kiro_crew.hooks import fire_tool_hooks, get_global_hook_store
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
    LLMProvider,
)
from kiro_crew.security import is_denied, is_sensitive_bash_command, is_sensitive_path
from kiro_crew.sel import sel as _sel

_PROMPT_BUSY_RETRIES = 2
_PROMPT_BUSY_DELAY = 1.5  # seconds between retries

# Transient backend (Bedrock 5xx / throttle / stream-reset) retry budget. These
# are server-side hiccups where the credential is VALID — retry helps, re-auth
# does not. Kept separate from the prompt-busy budget above.
_TRANSIENT_RETRIES = 3
_TRANSIENT_DELAY = 2.0  # base seconds; exponential backoff + jitter

# Per-process RNG for retry jitter, auto-seeded from os.urandom at import. The
# entropy seed spreads jitter uniformly *across* processes/machines — so a
# fleet-wide transient (e.g. several gateways hitting the same backend 5xx at the
# same minute) doesn't retry in lockstep and re-thunder the recovering backend.
# Auto-seeding (rather than os.getpid()) is container-safe: under Docker/ECS/K8s
# the gateway is commonly PID 1, so a PID seed would be identical fleet-wide and
# collapse the spread. Tests that need determinism patch asyncio.sleep (so the
# jitter value is never observed) or reseed _JITTER_RNG in a fixture.
_JITTER_RNG = random.Random()

# Substrings (lowercased) that mark a RETRYABLE transient backend failure.
# Matched against the formatted AcpError message (see acp.client._format_acp_error).
# Auth/validation markers are deliberately ABSENT so those fail fast — a retry
# cannot fix an expired token or a bad request, and silently retrying them would
# only delay the correct "re-auth"/"fix the request" signal to the operator.
_TRANSIENT_MARKERS = (
    "internal server error",
    "internal error: api error",
    "serviceunavailable",
    "service unavailable",
    "throttl",  # ThrottlingException + "Bedrock is throttling"
    "toomanyrequests",
    "servicequotaexceeded",
    "modelstreamerror",
    "connection reset",
    "connectionreset",
    "dispatch failure",  # AWS SDK connector-level I/O failure (conn/DNS/TLS drop)
    "dispatchfailure",  # Rust DispatchFailure variant (unspaced)
    # Model-unavailable capacity/rollout, matched against _format_acp_error's
    # wording. Two phrasings are listed: the current "on the backend" text
    # (#1550) and the pre-2026-08 "on Bedrock" one, so a transcript or log line
    # written by an older gateway still classifies. Any future rewording of
    # that branch must add its marker here.
    #
    # Deliberately does NOT cover the sibling unentitled-model branch: that one
    # is terminal by design (_model_is_unentitled), so a marker matching it
    # would resurrect the pointless retry loop #1550 removed.
    "is unavailable on the backend",
    "is unavailable on bedrock",
    "transient error (http 5xx)",  # _format_acp_error's generic-5xx message
)


def _is_transient_acp_error(msg: str) -> bool:
    """True iff an AcpError message looks like a retryable transient backend
    failure. Auth failures are explicitly excluded (they need re-auth, not retry)."""
    low = msg.lower()
    if (
        "authentication failed" in low
        or "accessdenied" in low
        or "expiredtoken" in low
        or "unrecognizedclient" in low
        or "invalidsignature" in low
    ):
        return False
    return any(m in low for m in _TRANSIENT_MARKERS)


# ── Public reuse surface for callers with their own stream loop ──
#
# stream_and_collect owns the transient retry for unattended callers, but the
# interactive dashboard/Slack path (dashboard.chat_runner) consumes the ACP
# stream directly and cannot funnel through stream_and_collect. These thin
# wrappers let it reuse the SAME classifier, retry budget, and backoff curve —
# one source of truth, no duplicated heuristics.

# Public name for the transient retry budget (number of retries after the
# initial attempt). Re-exported so external callers don't import the private.
TRANSIENT_RETRIES = _TRANSIENT_RETRIES


def is_transient_backend_error(msg: str) -> bool:
    """True iff *msg* (a formatted AcpError string) is a retryable transient
    backend failure (5xx / throttle / stream-reset) rather than an
    auth/validation error. Public alias of :func:`_is_transient_acp_error`."""
    return _is_transient_acp_error(msg)


def acp_error_is_transient(exc: BaseException) -> bool:
    """Authoritative retry-eligibility decider for an ACP error.

    Prefers the structured verdict carried on ``AcpError.transient`` — classified
    from the RAW JSON-RPC error at raise time (see
    ``acp.client._is_transient_raw_error``) — so the retry decision is
    independent of how the user-facing message is worded. Falls back to
    string-matching the formatted message for exceptions raised without the flag
    (legacy raise paths, non-``AcpError`` exceptions, tests).

    The formatted message may rewrite a generic 5xx into a friendly string that
    the marker-based string classifier alone does not recognise; relying on the
    structured flag keeps that case retryable."""
    flag = getattr(exc, "transient", None)
    if isinstance(flag, bool):
        return flag
    return is_transient_backend_error(str(exc))


def transient_retry_delay(attempt: int) -> float:
    """Backoff delay (seconds) for the *attempt*-th (1-based) transient retry.

    Exponential (base ``_TRANSIENT_DELAY``, doubling per attempt) plus
    per-process jitter, so every caller that retries transient backend errors
    backs off on the identical curve and co-located peers don't retry in
    lockstep (see ``_JITTER_RNG``)."""
    base = _TRANSIENT_DELAY * (2 ** (attempt - 1))
    return base + _JITTER_RNG.random() * 0.25 * base


class PromptBusyExhaustedError(Exception):
    """Provider was shut down after prompt-busy retries were exhausted."""


if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog
    from kiro_crew.hooks import HookManager

logger = logging.getLogger(__name__)


def record_interaction_event(client: LLMProvider, session_key: str, surface: str) -> None:
    """Record one per-interaction telemetry event via the PlatformContext seam.

    The Default ``TelemetryProvider.record_event`` is a no-op, so standalone is
    unchanged; a companion records one event per successful turn. Payload is
    strictly metadata (session key, surface, model) — never prompt/response text
    or file contents. Best-effort: a telemetry failure never affects the turn.

    Shared by every surface (dashboard, Slack) so the payload shape and the
    model-extraction reflection cannot drift between call sites.
    """
    from kiro_crew.platform import current_context

    try:
        # Resolve the active model across backend shapes. After Kiro startup the
        # provider's ``_client`` is an ``AcpSessionProvider`` that exposes the
        # model via a ``model`` property (backed by ``_handle.model``); before
        # startup / for the raw client it is the ``_model`` attribute. Try the
        # property first, then the raw attr, on the inner client then the outer.
        inner = getattr(client, "_client", client)
        model = ""
        for obj in (inner, client):
            model = getattr(obj, "model", "") or getattr(obj, "_model", "") or ""
            if model:
                break
        current_context().telemetry.record_event(
            "interaction",
            {"session_key": session_key, "surface": surface, "model": model},
        )
    except Exception:
        logger.debug("telemetry.record_event(interaction) failed", exc_info=True)


def _extract_tool_input_strings(tool_input: str) -> list[str]:
    """Extract all string values from a JSON tool_input for security scanning.

    Recursively walks nested dicts and lists to find all string values,
    ensuring sensitive paths in nested structures like
    ``{"args": {"path": "~/.aws/credentials"}}`` are not missed.

    Handles dict, list, plain-string, and malformed JSON gracefully. On
    parse failure, returns the raw string itself as the single candidate.
    """
    if not tool_input:
        return []
    try:
        parsed = json.loads(tool_input)
    except (json.JSONDecodeError, ValueError):
        # Not JSON — treat the raw string as a path/command candidate
        return [tool_input]
    if isinstance(parsed, str):
        return [parsed]

    results: list[str] = []

    def _collect(obj: object) -> None:
        if isinstance(obj, str) and obj:
            results.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _collect(v)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    _collect(parsed)
    return results


# ── Tool Approval Policies ──


class ToolApprovalPolicy(Enum):
    """How to handle tool permission requests during streaming."""

    AUTO_APPROVE = "auto_approve"
    REJECT_ALL = "reject_all"
    HOOK_BASED = "hook_based"


# Callback type for custom tool approval logic
OnPermissionCallback = Callable[[LLMEvent], Awaitable[bool]]


# ── Stream and Collect ──


async def run_bg_oneliner(
    sessions: Any,
    prompt: str,
    *,
    model: str | None = None,
    sel_source: str = "bg_oneliner",
    sel_session_key: str = "_bg",
    timeout: float | None = None,
) -> str:
    """Stream a single prompt through an ephemeral background session and return
    the accumulated text.

    Consolidates the identical "acquire a ``_bg`` session -> best-effort pin the
    cheap model -> drive the event loop -> ``destroy()`` in ``finally``" skeleton
    that was copied across title, link-label, folder-icon, and session-summary
    generation. The task is tool-free by contract: permission requests are
    rejected and **always** SEL-logged as ``denied`` — every permission decision
    must be audited (``backend-security-controls``). Callers may override
    ``sel_source`` to attribute the denial to their feature; callers that omit it
    are audited under the generic ``"bg_oneliner"`` source rather than silently
    dropping the SEL event.

    Errors propagate to the caller (the ``_bg`` session is still ``destroy()``-ed
    in ``finally``): callers that want best-effort "" fallback wrap the call
    themselves, while callers that surface the failure (title/nav) get it
    unchanged. ``sessions`` is duck-typed (a ``SessionManager``-like object
    exposing ``get_bg_session()``) rather than statically imported, so this
    low-level helper stays free of a dashboard/session import cycle.
    """
    session = await sessions.get_bg_session()

    def _first_advertised_fallback(advertised: Any, rejected: str | None) -> str | None:
        """First advertised model that is neither the rejected id nor the
        ``"auto"`` sentinel — the reactive replacement when the preferred model
        is refused mid-prompt."""
        rej = (rejected or "").strip().lower()
        for m in advertised or []:
            if not isinstance(m, str) or not m.strip():
                continue
            low = m.strip().lower()
            if low == rej or low == "auto":
                continue
            return m
        return None

    async def _drive(model_to_use: str | None) -> str:
        text = ""
        set_model = getattr(session, "set_model", None)
        # Pass the caller's preference (often the governed "auto") to set_model,
        # which resolves it against the session's advertised model list at the
        # wire chokepoint (AcpSessionHandle.set_model -> resolve_usable_model):
        # a hardcoded/unentitled id, or "auto" on a partition that does not serve
        # it, is swapped for the first advertised model instead of
        # reaching the wire and failing mid-prompt with Invalid model ID.
        # Best-effort: a failed override falls back to the default.
        if model_to_use and set_model is not None:
            try:
                await set_model(model_to_use)
            except Exception:
                logger.debug(
                    "bg oneliner: model override to %s failed; using default", model_to_use
                )
        async for event in session.prompt(prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                text += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Audit the denial BEFORE rejecting: every permission decision
                # must be SEL-logged (backend-security-controls), and a
                # reject_tool transport failure must NOT skip the audit.
                # ``sel_source`` carries a non-empty default so callers that
                # don't attribute a feature still produce an audit record.
                _sel().log_tool_invocation(
                    session_key=sel_session_key,
                    tool_name=getattr(event, "title", "unknown") or "unknown",
                    outcome="denied",
                    source=sel_source or "bg_oneliner",
                    request_id=str(event.request_id),
                )
                await session.reject_tool(event.request_id)
            elif event.kind == EVENT_TOOL_CALL:
                # Tool-free by contract, but an AUTO-APPROVED tool arrives with no
                # permission request to reject — audit it so no invocation escapes
                # the SEL log (backend-security-controls; mirrors the cron/
                # contradiction bg path this helper subsumes).
                _sel().log_tool_invocation(
                    session_key=sel_session_key,
                    tool_name=getattr(event, "title", "unknown") or "unknown",
                    outcome="allowed",
                    source=sel_source or "bg_oneliner",
                )
            elif event.kind == EVENT_COMPLETE:
                break
        return text

    async def _run(model_to_use: str | None) -> str:
        if timeout is not None:
            return await asyncio.wait_for(_drive(model_to_use), timeout)
        return await _drive(model_to_use)

    try:
        try:
            return await _run(model)
        except AcpError as exc:
            # Reactive fallback: the model was rejected mid-prompt — e.g. "auto"
            # on a partition that does not serve it, or any id the
            # account cannot run. The advertised list can't be used
            # to gate "auto" statically (it is a sentinel, never advertised), so
            # this is the layer that turns your spec's "else the first available
            # model" into action: retry ONCE with the first advertised model that
            # is neither the rejected id nor "auto". Only fires when the raise-time
            # classifier tagged a rejected model AND named an advertised set.
            rejected = getattr(exc, "rejected_model", None)
            advertised = getattr(exc, "advertised", None) or []
            fallback = _first_advertised_fallback(advertised, rejected) if rejected else None
            if not fallback:
                raise
            logger.warning(
                "bg oneliner: model %r rejected; retrying once with %r", rejected, fallback
            )
            return await _run(fallback)
    finally:
        await session.destroy()


def provider_last_turn_usage(provider: Any) -> TurnUsage:
    """Best-effort read of the just-completed turn's billing usage.

    ``stream_and_collect`` breaks on ``EVENT_COMPLETE`` and returns only text,
    discarding the event's ``usage``. Background surfaces that dispatch through
    it (cron, heartbeat, autonudge, workflow, task-runner self-review) therefore
    have no event to hand :func:`persist_token_record_async`. This recovers the
    turn's billing from the provider's ``last_prompt_stats`` — the same post-turn
    read ``chat_runner`` performs — and wraps it in a ``TurnUsage`` so it can be
    passed straight through as the ``event`` argument.

    On the ACP backend the only non-zero per-turn billing signal is ``credits``;
    the token fields stay 0, matching the real usage record. Providers that expose
    no stats (non-ACP backends, test doubles) yield an empty ``TurnUsage``
    (credits=0). Never raises.
    """
    try:
        inner = getattr(provider, "_client", None) or getattr(provider, "_handle", None)
        stats = getattr(inner, "last_prompt_stats", None) if inner is not None else None
        if stats is not None:
            return TurnUsage(credits=float(getattr(stats, "credits", 0.0) or 0.0))
    except Exception:
        logger.debug("provider_last_turn_usage read failed", exc_info=True)
    return TurnUsage()


async def stream_and_collect(
    provider: LLMProvider,
    message: str,
    *,
    approval_policy: ToolApprovalPolicy = ToolApprovalPolicy.AUTO_APPROVE,
    hooks: HookManager | None = None,
    on_chunk: Callable[[str], None] | None = None,
    on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None,
    retry_transient: bool = True,
    max_turns: int | None = None,
    session_key: str = "",
    agent: str = "",
    app: str = "",
) -> str:
    """Stream a message through an LLM provider and collect the full response.

    This is the core pattern used by cron, heartbeat, subagent, consolidator,
    taskrunner, and title generation.

    Args:
        provider: The LLM provider to stream through.
        message: The prompt to send.
        approval_policy: How to handle tool permission requests.
        hooks: HookManager for HOOK_BASED approval policy.
        on_chunk: Optional callback invoked with each text chunk (for progress).
        on_tool_approval: Optional async callback for interactive approval.
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

            All three matter for ``HOOK_BASED`` callers specifically. The gate
            resolves ``ceiling ∩ profile``, and it can only look up a profile it
            has been told the name of; with all three empty it applied the
            ceiling alone, so an app profile narrowing (say) ``filesystem.write``
            was silently not enforced for tools this helper approved. Callers
            using ``REJECT_ALL`` or ``AUTO_APPROVE`` are unaffected — the first
            runs no tools, the second never consults the gate.

    Returns:
        The complete response text.
    """
    transient_attempts = 0
    attempt = 0
    while True:
        result_text = ""
        tool_call_count = 0
        try:
            async for event in provider.stream(message):
                if event.kind == EVENT_TEXT_CHUNK:
                    result_text += event.text
                    if on_chunk:
                        on_chunk(event.text)
                elif event.kind == EVENT_PERMISSION_REQUEST:
                    approved = await _resolve_permission(
                        provider,
                        event,
                        approval_policy,
                        hooks,
                        on_tool_approval,
                        session_key=session_key,
                        agent=agent,
                        app=app,
                    )
                    if not approved:
                        continue
                elif event.kind == EVENT_TOOL_CALL:
                    tool_call_count += 1
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
                elif event.kind == EVENT_COMPLETE:
                    break
            return result_text
        except AcpError as exc:
            msg = str(exc)
            # Prompt-busy is matched STRUCTURALLY first, with the substring kept
            # as a fallback. _format_acp_error rewrites the backend's "prompt
            # already in progress" into friendly prose that no longer carries
            # the marker, so a string-only check silently loses BOTH arms below
            # (cancel+retry and PromptBusyExhaustedError) for any producer that
            # formats before raising — which the shared-runtime AcpSessionHandle
            # now does. Unattended callers (workflows/agent_pool, handlers/side,
            # the subagent-completion injector) depend on those arms to reset a
            # wedged parent session, so losing them surfaces a generic failure
            # and leaves the session stuck. The fallback still covers
            # unformatted / history-restored messages.
            busy = isinstance(exc, AcpPromptBusy) or "already in progress" in msg

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
            if (
                retry_transient
                and not result_text
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
                continue

            # ── Case 3: fatal (auth, validation, exhausted retries) — propagate. ──
            raise


async def stream_and_collect_json(
    provider: LLMProvider,
    message: str,
    *,
    approval_policy: ToolApprovalPolicy = ToolApprovalPolicy.AUTO_APPROVE,
    hooks: HookManager | None = None,
) -> dict | None:
    """Stream a message and parse the response as JSON.

    Combines ``stream_and_collect`` with ``parse_llm_json``.
    Returns parsed dict or None on failure.
    """
    text = await stream_and_collect(provider, message, approval_policy=approval_policy, hooks=hooks)
    return parse_llm_json(text)


async def _resolve_permission(
    provider: LLMProvider,
    event: LLMEvent,
    policy: ToolApprovalPolicy,
    hooks: HookManager | None,
    on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None,
    session_key: str = "",
    agent: str = "",
    app: str = "",
) -> bool:
    """Resolve a tool permission request. Returns True if approved."""
    from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
    from kiro_crew.sel import sel

    def _log(outcome: str, **extra):
        sel().log_tool_invocation(
            session_key=session_key,
            agent=agent,
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome=outcome,
            request_id=event.request_id,
            **extra,
        )

    if policy == ToolApprovalPolicy.REJECT_ALL:
        await provider.reject_tool(event.request_id)
        _log("rejected", metadata={"reason": "reject_all_policy"})
        return False

    # ── Always-enforced deny checks (regardless of approval policy) ──
    # These run even for AUTO_APPROVE callers (workflows, crons, etc.)
    # to ensure BUILTIN_DENY_PATTERNS and sensitive-path protection cannot
    # be bypassed by callers that skip HookManager wiring.
    normalized = event.title or ""
    if not normalized:
        await provider.reject_tool(event.request_id)
        _log("denied", error="Blocked: missing tool title", metadata={"mechanism": "always_deny"})
        return False
    if is_sensitive_path(normalized):
        await provider.reject_tool(event.request_id)
        _log(
            "denied",
            error=f"Blocked: sensitive path: {normalized}",
            metadata={"mechanism": "always_deny"},
        )
        return False
    _bash_reason = is_sensitive_bash_command(normalized)
    if _bash_reason:
        await provider.reject_tool(event.request_id)
        _log("denied", error=_bash_reason, metadata={"mechanism": "always_deny"})
        return False
    # Honor the user's Settings>Security opt-out + governance pins on this
    # surface too (cron / Slack / workflow / heartbeat). Without threading the
    # effective set, is_denied() fails closed to ALL built-ins here, which would
    # re-introduce "disabled but still blocked" on every non-dashboard surface.
    # No HookManager (rare) → None → fail-closed default (all built-ins).
    _denied_regexes = hooks.effective_denied_regexes() if hooks is not None else None
    _deny_reason = is_denied(normalized, denied_regexes=_denied_regexes)
    if _deny_reason:
        await provider.reject_tool(event.request_id)
        _log("denied", error=_deny_reason, metadata={"mechanism": "always_deny"})
        return False

    # Defense-in-depth: also inspect event.tool_input for sensitive paths/commands.
    # The title usually carries the full path/command (kiro-cli convention), but
    # tool_input may contain additional arguments or the actual path when the
    # title is a generic tool name (e.g. "Read", "Bash").
    _tool_input = event.tool_input or ""
    if _tool_input:
        # Extract string values from JSON tool_input for path/command checking.
        _input_strings = _extract_tool_input_strings(_tool_input)
        for s in _input_strings:
            if is_sensitive_path(s):
                await provider.reject_tool(event.request_id)
                _log(
                    "denied",
                    error=f"Blocked: sensitive path in tool_input: {s}",
                    metadata={"mechanism": "always_deny_input"},
                )
                return False
            _input_bash = is_sensitive_bash_command(s)
            if _input_bash:
                await provider.reject_tool(event.request_id)
                _log("denied", error=_input_bash, metadata={"mechanism": "always_deny_input"})
                return False
            _input_deny = is_denied(s, denied_regexes=_denied_regexes)
            if _input_deny:
                await provider.reject_tool(event.request_id)
                _log("denied", error=_input_deny, metadata={"mechanism": "always_deny_input"})
                return False

    if policy == ToolApprovalPolicy.HOOK_BASED and hooks:
        tool_result = hooks.on_tool_call(
            event.title,
            session_key=session_key,
            agent=agent,
            app=app,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
            command=event.shell_command,
            is_shell=event.is_shell,
        )
        if tool_result.action == TOOL_DENY:
            await provider.reject_tool(event.request_id)
            _log("denied", error=tool_result.reason)
            return False
        if tool_result.action == TOOL_AUTO_APPROVE:
            await provider.approve_tool(event.request_id)
            _log("auto_approved", metadata={"reason": "hook_auto_approve"})
            return True

    # Interactive approval if callback provided
    if on_tool_approval:
        approved = await on_tool_approval(event)
        if not approved:
            await provider.reject_tool(event.request_id)
            _log("rejected", metadata={"reason": "interactive_rejected"})
            return False

    # Default: auto-approve
    await provider.approve_tool(event.request_id)
    _log("auto_approved")
    return True


# ── JSON Parsing ──


_JSON_DECODER = json.JSONDecoder()


def _extract_json_of_type(text: str, expected_type: type) -> dict | list | None:
    """Extract the first top-level JSON value of *expected_type* embedded in prose.

    Scans successive ``{`` (dict) or ``[`` (list) offsets and uses the stdlib
    ``raw_decode`` to parse a complete JSON value at each — this validates the
    full JSON grammar and correctly handles nesting and string escapes. Returns
    the first value that matches *expected_type*, or None.

    Scanning successive offsets (rather than committing to the first delimiter)
    is what makes this robust to a stray structural brace in the prose preamble
    (e.g. ``"use {placeholder}: {\\"a\\": 1}"``). Only TOP-LEVEL matches count: a
    ``{`` nested inside an earlier-starting ``[ ... ]`` is consumed by that
    array's decode, so a dict request never digs a nested object out of a
    surrounding array.
    """
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Only attempt a decode at a JSON container start. Scanning BOTH
        # delimiters in positional order (not just the expected one) is what
        # prevents digging a nested object out of a surrounding array: a
        # leading "[ ... ]" is decoded as a list, found to be the wrong type,
        # and skipped past in full — so a dict request on "[1, {\\"a\\":2}]"
        # returns None rather than the inner {"a":2}.
        if ch not in "{[":
            i += 1
            continue
        try:
            data, end = _JSON_DECODER.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(data, expected_type):
            return data  # type: ignore[return-value]
        # Valid JSON of the wrong type — skip past its full extent.
        i = end
    return None


def _parse_llm(text: str, expected_type: type) -> dict | list | None:
    """Parse JSON from LLM output, tolerating fences and surrounding prose.

    Background turns (e.g. memory consolidation) run on a shared lite session.
    On the Claude Code backend that session is not tool/persona-scoped the way
    kiro's no-tools lite agent is, so the model may wrap the JSON in prose. To
    keep consolidation from silently no-opping, fall back to extracting the
    first top-level JSON value of the expected type when a strict parse fails.
    """
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        if isinstance(data, expected_type):
            return data  # type: ignore[return-value]
        return None
    except json.JSONDecodeError:
        # Fallback: extract the first top-level JSON value of the expected type
        # embedded in prose (scans successive delimiters, validates via stdlib).
        result = _extract_json_of_type(text, expected_type)
        if result is None:
            logger.debug("Failed to parse LLM JSON: %.200s", text)
        return result


def parse_llm_json(text: str) -> dict | None:
    """Parse JSON dict from LLM output, stripping markdown fences if present."""
    return _parse_llm(text, dict)  # type: ignore[return-value]


def parse_llm_json_list(text: str) -> list | None:
    """Parse a JSON array from LLM output, stripping markdown fences."""
    return _parse_llm(text, list)  # type: ignore[return-value]


# ── Conversation History Helpers ──


def save_conversation_turn(
    log: ConversationLog,
    key: str,
    user_text: str,
    assistant_text: str,
    source_thread: str | None = None,
    source_user: str | None = None,
    agent: str | None = None,
) -> None:
    """Save a user+assistant conversation turn to the history log.

    Consolidates the repeated pattern of appending user and assistant
    messages with provenance tracking.  When *agent* is supplied it is
    recorded in the session metadata on file creation so that
    ``/kirocrew sessions`` displays the correct agent name.
    """
    log.append(
        key,
        "user",
        user_text,
        source_thread=source_thread,
        source_user=source_user,
        agent=agent,
    )
    if assistant_text:
        log.append(
            key,
            "assistant",
            assistant_text,
            source_thread=source_thread,
            source_user=source_user,
        )


async def save_conversation_turn_off_loop(
    log: ConversationLog,
    key: str,
    user_text: str,
    assistant_text: str,
    source_thread: str | None = None,
    source_user: str | None = None,
    agent: str | None = None,
) -> None:
    """Save a turn without blocking (or fail-fast-dropping on) the event loop.

    :func:`save_conversation_turn` makes TWO ``ConversationLog.append`` calls, and
    append acquires a cross-process flock and writes to disk -- ~12 ms each on a
    large transcript. Called directly from an ``async def`` that is worse than
    slow: on a running loop ``_locked`` makes a single NON-blocking acquire and
    raises :class:`~kiro_crew.history.HistoryLockTimeout` on any concurrent
    holder, and most callers swallow that, so the durable copy was dropped
    exactly when another writer was active. Off the loop the same primitive takes
    the patient poll-to-deadline path instead.

    This is the single choke point for every async caller, so the offload cannot
    be forgotten at a new call site and the ten Slack sites do not each restate
    it.

    Unlike :func:`~kiro_crew.history.append_off_loop`, this **awaits** the write
    rather than firing it at the executor and returning. That difference is
    deliberate: callers here go on to refresh a dashboard tab or hand the session
    to consolidation, both of which read the transcript back, so the turn has to
    be on disk before the caller continues. ``append_off_loop`` has no such
    reader and can afford to be fire-and-forget.

    The whole turn is written under one :meth:`~kiro_crew.history.ConversationLog.atomic_appends`
    hold. ``append`` locks per ROW, so without it two concurrent turns for the
    same session could interleave into ``user_A, user_B, assistant_A,
    assistant_B`` -- turns that no longer pair up, which no timestamp ordering can
    repair because each row's ``ts`` is individually correct. On the loop that was
    impossible (a synchronous caller never yields between its two appends), so the
    hazard is introduced BY offloading and has to be closed here rather than
    inherited.
    """

    def _write() -> None:
        with log.atomic_appends(key):
            save_conversation_turn(
                log,
                key,
                user_text,
                assistant_text,
                source_thread=source_thread,
                source_user=source_user,
                agent=agent,
            )

    await asyncio.to_thread(_write)
