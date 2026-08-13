"""Core LLM runner — _run_chat, segment flushing, prompt expansion."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import shlex
import stat as stat_module
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import mcp_apps_render, model_registry, session_directive
from kiro_crew.acp.client import (
    AcpAuthRequired,
    AcpError,
    AcpProcessDied,
    AcpPromptBusy,
    _is_safe_oauth_url,
    advertised_model_ids,
    model_is_unusable,
)
from kiro_crew.acp.types import (
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_STEER_CONSUMED,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
)
from kiro_crew.agent_discovery import warm_project_agent_names
from kiro_crew.autonudge import get_instance
from kiro_crew.browser_cli import install as browser_cli_install
from kiro_crew.config.loader import (
    KiroCrewConfig,
    data_home,
    normalize_agent_model,
    resolve_agent_bindings,
)
from kiro_crew.connections import get_visible_providers
from kiro_crew.context_blocks import (
    PHASE_PER_TURN,
    PHASE_SESSION_START,
    USER_LABEL,
    attributable_user_chars,
    split_blocks,
)
from kiro_crew.context_management import (
    ensure_go_all_option,
    looks_like_plan,
    strip_plan_markers,
    validate_plan_format,
)
from kiro_crew.dashboard.chat_persistence import _build_history_prefix, save_slot_off_loop
from kiro_crew.dashboard.chat_summary import generate_session_summary
from kiro_crew.dashboard.chat_title import (
    _extract_and_redact_plan_metadata,
    _maybe_auto_title,
    _rephrase_plan_lite,
    _reset_auto_run_for_new_plan,
    maybe_refresh_title,
)
from kiro_crew.dashboard.chat_utils import (
    _BLOCKED_SLASH_COMMANDS,
    _MAX_TOOL_PURPOSE,
    _SLASH_COMMANDS,
    ResetCause,
    _append_compaction_notice,
    _apply_incognito_prefix,
    _broadcast_auto_tool,
    _broadcast_compaction_result,
    _dequeue_next_message,
    _dequeue_next_system_message,
    _extract_bash_command,
    _maybe_consolidate,
    _maybe_inject_persona,
    _normalize_model,
    _redact_for_display,
    _redact_meta_for_role,
    _redact_tool_field,
    _remove_queued_by_id,
    _validate_tool_name,
    build_recovery_requeue,
    effective_session_key,
    expire_slack_options,
    is_system_injection_item,
    mirror_is_paused,
    remember_slack_options,
    slack_mirror_is_paused,
    slot_history_key,
)
from kiro_crew.dashboard.handlers import (
    MAX_PROMPT_BYTES,
    _find_prompt,
    _get_skills,
    _list_aim_prompts,
)
from kiro_crew.dashboard.handlers.usage import (
    persist_token_record_async,
    read_context_tokens,
    read_effective_agent,
    read_turn_model,
)
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.dashboard.state import (
    CRON_NOTIFY_PREFIX,
    CRON_NOTIFY_RE,
    HOOK_CONTINUATION_RECOVERY_PREFIX,
    HOOK_HALTED_RECOVERY_PREFIX,
    NATIVE_SUBAGENT_DONE_RESULT_CAP,
    NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
    NATIVE_SUBAGENT_OUTPUT_HARD,
    NATIVE_SUBAGENT_OUTPUT_TAIL,
    NATIVE_SUBAGENT_TERMINAL_KEEP,
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    REFUSAL_RECOVERY_PREFIX,
    STALE_RECOVERY_PREFIX,
    SUBAGENT_COMPLETION_PREFIXES,
    SUBAGENT_SYNTHESIS_PREFIX,
    SUBAGENT_SYNTHESIS_PROMPT,
    TOOL_STALL_RECOVERY_PREFIX,
    DashboardState,
    _ChatSlot,
    _mark_permission_resolved,
    build_refusal_recovery_prompt,
    build_stale_recovery_prompt,
    build_tool_stall_recovery_prompt,
    is_read_only_bash,
    parse_hook_continuations,
    should_queue_hook_continuation,
    should_queue_refusal_recovery,
    unsafe_bash_reason,
)
from kiro_crew.dashboard.steer_settle import settle_consumed_steers
from kiro_crew.dashboard.turn_dispatch import (
    format_approval_no_budget_card,
    format_approval_timeout_card,
    spawn_guarded_turn,
    tool_approval_timeout_secs,
)
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    ToolHookResult,
    _normalize_tool_name,
    _tool_matches,
    fire_tool_hooks,
    safe_read_file,
    validate_file_path,
)
from kiro_crew.image_artifacts import register_images_off_loop
from kiro_crew.llm_helpers import (
    TRANSIENT_RETRIES,
    PromptBusyExhaustedError,
    acp_error_is_transient,
    record_interaction_event,
    run_bg_oneliner,
    transient_retry_delay,
)
from kiro_crew.mcp_discovery import kirocrew_managed_names
from kiro_crew.members import record_activity
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.messaging.link import SLACK_NAMESPACE, telemetry_channel_of
from kiro_crew.messaging.renderer import chunk_text
from kiro_crew.metrics.events import TURN_TIMEOUT_CAUSE, emit_counter
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.platform import redact_via_context
from kiro_crew.providers.acp import is_claude_backend
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    LLMEvent,
)
from kiro_crew.safety_override import safety_override
from kiro_crew.security import (
    StreamRedactor,
    is_sensitive_path,
    oauth_url_contains_credential,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.session import SessionClosingError, SpeculativeResumeRefused
from kiro_crew.slack.handler import post_linked_approval, resolve_linked_approval
from kiro_crew.slack.outbound import PostedOptions
from kiro_crew.validation import ValidationError, infer_use_case, validate_ask_user_question
from kiro_crew.widget_artifacts import register_widgets_off_loop

logger = logging.getLogger(__name__)

# The synthetic recovery message constants live in chat_utils (single source
# of truth shared with the queue/merge predicates — is_system_injection must
# classify them identically to the turn logic here). Re-exported under their
# historical names so existing imports keep working.
from kiro_crew.dashboard.chat_utils import (  # noqa: E402
    _EMPTY_AUTO_CONTINUE_MSG,
    _POSTTOKEN_RECOVER_MSG,
    _SYNTHETIC_RECOVERY_MSGS,
    SYNTHETIC_RECOVERY_KIND,
    RecoveryPayload,
    is_synthetic_payload_item,
    is_synthetic_recovery_item,
    mint_options_token,
    payload_for_replay,
)


def _empty_auto_continue_enabled() -> bool:
    """Config gate for the empty-response auto-continue rung (default ON —
    the recovery is bounded to one nudge per user message and always
    transcript-visible). Fail-open to the default: a config-load hiccup must
    not disable self-healing mid-incident."""
    try:
        return bool(KiroCrewConfig.load().session.empty_response_auto_continue)
    except Exception:  # pragma: no cover — config load must not break recovery
        return True


def drain_pending_context(slot: "_ChatSlot") -> str:
    """Drain ``slot._pending_context`` into a prepend-ready context prefix.

    Returns the concatenated ``[Background context from "<source>"] … [End of
    background context]`` blocks (empty string when there is nothing to inject)
    and clears the queue. Expired entries (``maxAge`` elapsed) are discarded.

    Extracted from ``_run_chat`` so the entry contract — the ``content`` /
    ``source`` keys and the delimiter frame — is pinned by a unit test and
    shared by every producer (app-kit context inject, Slack thread backfill),
    rather than duplicated inline where a key rename could silently break a
    consumer while its producer's own tests stay green.
    """
    if not slot._pending_context:
        return ""
    now = time.time()
    ctx_parts: list[str] = []
    for entry in slot._pending_context:
        max_age = entry.get("maxAge")
        if max_age is not None:
            injected_at = entry.get("injectedAt", 0)
            if injected_at + max_age < now:
                continue  # expired — silently discard
        source = entry.get("source", "app")
        ctx_parts.append(
            f'[Background context from "{source}"]\n'
            f'{entry["content"]}\n'
            f"[End of background context]\n"
        )
    slot._pending_context.clear()
    return "\n".join(ctx_parts) + "\n" if ctx_parts else ""


def _turn_outcome(stop_reason: str | None, *, exhausted: bool = False) -> str:
    """Map an EVENT_COMPLETE stop_reason to a low-cardinality turn outcome.

    Single source of truth shared by the ``kirocrew.turn.duration`` emit in
    ``_run_chat`` and its unit test, so the mapping can't silently drift from
    what the test asserts (tests must exercise real production logic).

    The two watchdog stop reasons are distinct outcomes, not ``error``: a
    stall-recovery turn is re-driven in place (its budget/outcome is tracked
    by ``kirocrew.watchdog.recovery.outcome``), so folding it into ``error``
    would make the fault rate count every recovered stall as a fault AND hide
    the stall population the watchdog work exists to measure. Checked BEFORE
    the ``timeout`` substring so a stall never misclassifies.

    ``exhausted`` marks a stall turn whose recovery budget is already spent
    (the caller reads the slot budgets the stop-reason branches maintain):
    the slot dies with "start a new chat", so the turn labels
    ``stall_exhausted`` — a terminal fault to the aggregator — keeping the
    recovered-stall exclusion from hiding dead sessions while ``fault_rate``
    stays a single-series computation.
    """
    s = stop_reason or ""
    if s in ("", "end_turn", "stop", "completed"):
        return "ok"
    if s == STOP_REASON_TOOL_STALL or s == STOP_REASON_STALE_RECOVER:
        if exhausted:
            return "stall_exhausted"
        return "tool_stall" if s == STOP_REASON_TOOL_STALL else "stale_recover"
    if "timeout" in s:
        return "timeout"
    return "error"


def _emit_turn_metric(
    duration_ms: int | float | None,
    stop_reason: str | None,
    slot_key: str,
    *,
    elapsed_ms: int | float | None = None,
    exhausted: bool = False,
) -> None:
    """Emit kirocrew.turn.duration (best-effort).

    Single source of truth shared by the ``_run_chat`` turn-completion path and
    its unit test, so the metric name, attrs, and outcome mapping live in
    production and any drift fails the test (tests must drive real
    production code). One histogram powers both turn latency and fault rate.

    ``duration_ms`` is the provider-reported duration and ``elapsed_ms`` the
    locally measured wall clock; the first non-zero wins. Both are needed
    because the acp provider ALWAYS reports ``TurnUsage.duration_ms == 0``
    (nothing in the codebase assigns it — only claude_code fills it in), so a
    provider-only value silently skipped the emit for effectively all traffic
    and left turn latency / fault rate / throughput reading a flat 0.

    A still-zero duration skips the emit deliberately: an absent sample reads
    as "no data" on the Telemetry page, whereas a recorded 0 would render as a
    plausible-looking 0ms p50 — the very symptom this guard's misuse caused.

    Caveat on what the wall clock measures: ``elapsed_ms`` runs from the start
    of the turn, so a turn parked on an interactive tool-approval prompt counts
    the operator's thinking time as turn duration. There is no finer-grained
    source on the acp path (the provider reports nothing at all), so this is
    the honest maximum available — but it means the histogram is "turn
    wall-clock", not pure model latency, and a high p90 can mean slow approvals
    rather than a slow model.
    """
    value = duration_ms or elapsed_ms
    if not value:
        return
    attrs: dict = {"outcome": _turn_outcome(stop_reason, exhausted=exhausted)}
    try:
        source = infer_use_case(slot_key)
        if source:
            attrs["session_source"] = source
    except Exception:
        pass
    try:
        get_recorder().histogram("kirocrew.turn.duration", value, unit="ms", attrs=attrs)
    except Exception:
        logger.debug("turn metric emit failed", exc_info=True)


def _emit_recovery_outcome(mechanism: str, outcome: str, attempts: int) -> None:
    """Emit kirocrew.watchdog.recovery.outcome (best-effort).

    One counter point per RESOLVED recovery cycle, derived from the per-slot
    retry budgets the stop-reason branches already maintain
    (``slot._stale_recovery_retries`` / ``slot._tool_stall_retries``):

    - ``outcome=recovered`` — a synthetic recovery turn completed ``ok`` while
      a budget was armed (emitted at the budget-reset block, which is the one
      place a completed cycle and its attempt count coexist).
    - ``outcome=exhausted`` — the budget hit its cap and the slot surfaced
      "start a new chat" (emitted in the stall branches themselves).

    ``attempt_bucket`` is the attempt count clamped to the budget cap (1-3) —
    a closed enum per the metrics/schema.py cardinality rule, mirroring the
    CLI's ``attempt_number_bucket`` precedent. Single source of truth shared
    with its unit test so the mapping cannot silently drift.
    """
    try:
        get_recorder().counter(
            "kirocrew.watchdog.recovery.outcome",
            attrs={
                "mechanism": mechanism,
                "outcome": outcome,
                "attempt_bucket": max(1, min(int(attempts), 3)),
            },
        )
    except Exception:
        logger.debug("recovery outcome metric emit failed", exc_info=True)


def _pre_tool_hooks_should_block(pre_hook_results: Any) -> bool:
    """Deny-by-default for unexpected hook output, plus explicit BLOCKED:.

    PreToolUse script hooks return a list of strings (each either a
    stdout-injection string or a 'BLOCKED:<name>:<reason>' marker emitted
    by ``_fire`` when a hook exits 2). This helper returns True when the
    auto-approve path must reject the tool: anything that's not a list of
    strings is treated as suspicious (deny-by-default), and any
    BLOCKED:-prefixed string blocks. An empty list is the documented
    pass-through contract (no hooks registered, or all registered hooks
    exited 0 with no stdout) and returns False.
    """
    if pre_hook_results is None or not isinstance(pre_hook_results, list):
        return True
    return any(not isinstance(r, str) or r.startswith("BLOCKED:") for r in pre_hook_results)


def _pre_tool_block_reason(pre_hook_results: Any) -> str:
    """Return the first hook-authored block reason, or a safe fallback."""
    if isinstance(pre_hook_results, list):
        for result in pre_hook_results:
            if isinstance(result, str) and result.startswith("BLOCKED:"):
                parts = result.split(":", 2)
                reason = parts[2].strip() if len(parts) == 3 else ""
                if reason:
                    return reason
    return "blocked by a PreToolUse policy hook"


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


async def _reject_hook_blocked(
    client: Any,
    slot: Any,
    event: Any,
    *,
    session_key: str,
    pre_hook_results: Any,
    refusal_reasons: list[tuple[str, str]],
    metadata: dict | None = None,
) -> None:
    """Deny a tool a PreToolUse hook blocked, and record WHY for the model.

    Four things have to happen together: reject the call, show the user a blocked
    row, audit the denial, and append the hook's reason to ``refusal_reasons`` so
    the recovery nudge tells the model what it did wrong. Doing the first three
    without the fourth is the defect this fixes — the turn dies silently and the
    model stalls with no reason to adapt to, while every other signal looks
    correct. They live here rather than at each permission path so a path added
    later cannot deny by omission.
    """
    # event.title prefers the model's own `description` field (_select_tool_title),
    # so it is LLM-controlled display text. Redact once up front and use that
    # everywhere: the row is broadcast to the dashboard AND persisted to the
    # ConversationLog, and the sibling reject path (_safe_reject_title) redacts for
    # both that row and the audit.
    title, reason = _redacted_hook_block(event, pre_hook_results)
    await client.reject_tool(event.request_id)
    slot.append("tool", f"🚫 {title} (hook blocked)", "msg msg-tool")
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
    refusal_reasons.append((title, reason))


async def _reject_invalid_tool(
    client: Any,
    slot: Any,
    event: Any,
    *,
    session_key: str,
    error: Exception,
    metadata: dict | None = None,
) -> None:
    """Deny a tool whose display name failed validation, on redacted surfaces.

    Reject, blocked row, and audit live together so a permission path added
    later cannot publish the raw model-authored title by omission: the title
    is redacted once here and used for both the transcript row (broadcast to
    the dashboard AND persisted to the ConversationLog) and the audit
    ``tool_name``. The validation *error* needs no redaction —
    ``_validate_tool_name`` raises only fixed messages that never echo the
    offending name.
    """
    title = _redact_display_text(event.title)
    await client.reject_tool(event.request_id)
    slot.append("tool", f"🚫 {title} (invalid: {error})", "msg msg-tool")
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


async def _reject_hook_error(
    client: Any,
    slot: Any,
    event: Any,
    *,
    session_key: str,
    error: str,
    metadata: dict | None = None,
) -> None:
    """Deny a tool whose PreToolUse hook fire raised, on redacted surfaces.

    Same chokepoint shape as :func:`_reject_invalid_tool`: the model-authored
    title is redacted once and reaches the blocked row and the audit only in
    that form. *error* is the hook exception text; hooks are fired with the
    tool name and parsed input, so an exception that wraps its inputs can
    carry model-authored text — redact it before the audit too.
    """
    title = _redact_display_text(event.title)
    await client.reject_tool(event.request_id)
    slot.append("tool", f"🚫 {title} (hook error)", "msg msg-tool")
    sel().log_tool_invocation(
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name=title,
        tool_kind=event.tool_kind,
        outcome="hook_error",
        request_id=event.request_id,
        error=_redact_display_text(error),
        metadata=metadata,
    )


def _is_bedrock_profile_id(model: str) -> bool:
    """True if *model* is a concrete Bedrock inference-profile id rather than a
    portable model alias.

    A region-routed inference profile (``global.anthropic.claude-opus-4-8[1m]``,
    ``us.anthropic.…``) pins one specific Bedrock model + region. kiro-cli
    resolves the picked alias to such an id internally and reports it on
    ``client._model``; the portable forms the picker sets (``claude-opus-4.7``,
    ``sonnet``, ``deepseek-3.2``) never carry the ``*.anthropic.*`` namespace or
    the ``[1m]`` capability suffix.
    """
    m = model.lower()
    return "anthropic." in m or "[1m]" in m


def _backfill_canonical_model(client: Any, provider: str) -> str:
    """Read the provider's resolved model (``client.client._model``) and map it
    to its canonical registry key for the dropdown, or ``""`` if unavailable.

    AcpProvider stores a provider id on ``_model``. ``canonicalize_for_provider``
    maps it back to the canonical key ONLY for ``claude_code`` (the canonical-
    keyed dropdown); for kiro/acp it is a no-op so a kiro dotted id that happens
    to be spelled like a claude_code alias (e.g. ``claude-sonnet-4.6``,
    ``claude-haiku-4.5``) is NOT rewritten to a claude_code canonical key.
    Skips the ``"auto"`` sentinel. Single home for the slot.model backfill so the
    early (pre-turn) and late (mid-turn init) sites agree.

    kiro-profile guard: on the kiro/acp path ``canonicalize_for_provider`` is a
    no-op, so a backfilled value is stored into ``slot.model`` verbatim and
    re-sent as a ``set_model`` override on every resume. kiro reports the
    RESOLVED Bedrock inference-profile id (e.g.
    ``global.anthropic.claude-opus-4-8[1m]``) — not the alias the user picked —
    so backfilling it pins the slot to one profile + region. A session that once
    resolved to the 1M Opus profile then stays nailed to it across resumes even
    when that profile is capacity-throttled, and the picker can no longer
    dislodge the poisoned value (observed: every "model unavailable" throttle hit
    the profile-form id, never the dotted alias, which kiro routes with capacity
    awareness). So for non-``claude_code`` providers we DROP a profile-form id
    (return ``""``) to keep ``slot.model`` empty and let the next get_or_create
    re-resolve; a portable alias (what the picker actually sets) is still kept.
    claude_code is unaffected: its profile id canonicalizes to a dropdown key
    that is the model the user explicitly chose.
    """
    prov_model = getattr(getattr(client, "client", None), "_model", "") or ""
    if not (isinstance(prov_model, str) and prov_model and prov_model != "auto"):
        return ""
    if provider != "claude_code" and _is_bedrock_profile_id(prov_model):
        return ""
    return model_registry.canonicalize_for_provider(prov_model, provider)


def _pinned_model_withheld(client: Any, model: str, provider: str) -> bool:
    """True when the live session cannot run the model this slot is pinned to.

    ``providers.acp`` withholds an inherited/persisted model the account is not
    entitled to and leaves the session on the backend default, so the turn
    succeeds — but nothing told the user, and the composer chip plus the picker
    went on reporting a model no turn would ever use (observed after a plan
    downgrade: the chip still read ``claude-opus-5`` while every turn ran on
    auto). This is the read side of that withhold, using the SAME predicate so
    the two cannot disagree about what "usable" means.

    The caller only REPORTS on a true result — it does not clear the pin. The
    withhold already keeps the model off the wire and the frontend already
    displays the effective model, so a stale pin is inert and recovers by itself
    if entitlement returns.

    Only the kiro/acp path is checked. ``slot.model`` is a bare dotted wire id
    there — the same namespace ``session/new`` advertises — while claude_code
    holds canonical keys against bare advertised ids, and comparing those two
    namespaces would call every legitimate model unusable (see
    :func:`model_is_unusable`'s namespace note). ``model_is_unusable`` itself
    fails open on an empty advertised set, so a session that advertised nothing
    (or a provider with no getter) leaves the pin alone: entitlement unknown is
    not entitlement denied.
    """
    if not model or model == "auto" or provider == "claude_code":
        return False
    if getattr(client, "is_claude_backend", False):
        return False
    getter = getattr(client, "available_models", None)
    if not callable(getter):
        return False
    try:
        advertised = advertised_model_ids(getter())
    except Exception:
        return False
    return model_is_unusable(model, advertised)


def _context_usage_payload(slot_key: str, client: Any) -> dict[str, Any]:
    """Build the ``context_usage`` WS payload: pct plus real token counts.

    The token counts let the frontend ring tooltip show "used / window" in
    absolute tokens (sourced from the adapter's usage_update), so a 44%-of-200k
    reading is not misread as 44%-of-1M.

    When real per-turn token counts are unavailable the payload carries
    ``reset: True`` instead of the ``used_tokens``/``window_tokens`` pair. This
    is load-bearing, not cosmetic: the frontend keeps the percentage and the
    token counts in two independent slices (``slotContextPct`` vs
    ``slotContextTokens``), so a bare ``{slot, pct}`` frame updates the
    percentage while leaving whatever token counts the ring last stored in
    place — a headline that disagrees with the count beside it. Emitting
    ``reset`` whenever ``used`` is unknown — a fresh session before the first
    ``usage_update``, or the post-compaction / post-model-switch state where the
    provider zeroes ``used`` but keeps the window — moves the two fields
    together: the ring drops its stored counts and the meter self-corrects on
    the next turn's telemetry. Harmless when nothing is stored.
    """
    pct = client.context_usage_pct()
    payload: dict[str, Any] = {"slot": slot_key, "pct": round(pct, 1)}
    # Use the provider's public accessors — last_prompt_stats lives on the
    # inner AcpClient, not on the provider, so reaching for it on `client`
    # (the AcpProvider returned by get_or_create) would always miss.
    window = client.context_window_tokens() if hasattr(client, "context_window_tokens") else 0
    # used == 0 means "not measured yet", not "empty context" — it is the
    # post-compaction / post-model-switch state (AcpPromptStats zeroes the
    # counts but keeps the window until the next turn's telemetry). Shipping
    # {used: 0, window: W} would assert a false "0 / W tokens", so we omit the
    # pair and signal a reset instead.
    used = 0
    if window and hasattr(client, "context_used_tokens"):
        used = client.context_used_tokens()
    if window and used:
        payload["used_tokens"] = used
        payload["window_tokens"] = window
    else:
        payload["reset"] = True
    return payload


# ── File-chip snapshots ────────────────────────────────────────────────────
# When the agent invokes a write tool, capture the file's content BEFORE the
# write executes. After the turn ends, capture the AFTER content and attach
# {path, before, after} entries to the assistant message meta. Frontend
# renders these as file-change chips with click-through to a Monaco diff.

_WRITE_COMMANDS = frozenset({"create", "strReplace", "insert"})
_MAX_SNAPSHOT = 200_000  # cap per-file snapshot to bound message meta size
# Reconstruction reads the whole file synchronously on the event loop; past
# this size the stored snapshot is truncated to _MAX_SNAPSHOT anyway, so
# reconstruction declines instead of stalling the loop on a huge file.
_MAX_RECONSTRUCT_BYTES = 2_000_000

# Poisoned-conversation escalation threshold: number of CONSECUTIVE turn
# cycles that must each exhaust the full pre-stream transient-5xx ladder
# (TRANSIENT_RETRIES + 1 attempts, zero output) before the terminal error
# branch stops advising "retry in a moment" and instead destroys the native
# session and re-queues once on a fresh conversation. Two cycles ⇒ at least
# 2×(TRANSIENT_RETRIES+1) consecutive pre-stream failures spanning a user
# action (Continue / new message), which a momentary capacity blip does not
# survive but a backend-rejected persisted conversation always does.
POISONED_SESSION_CYCLES = 2

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

# Cap the backend-echoed reason interpolated into the "Compaction failed"
# notice. The notice is a one-line receipt in the transcript, so an unbounded
# provider string (a stack trace, an echoed payload) would scroll the
# conversation away instead of explaining it.
_COMPACT_FAIL_REASON_MAX_CHARS = 300


def _truncate_snapshot(content: str) -> str:
    """Cap content at _MAX_SNAPSHOT chars, appending a marker if truncated.
    Shared by before-content (in _snapshot_write_target) and after-content
    (in _flush_file_changes) so both paths show consistent diffs."""
    if len(content) > _MAX_SNAPSHOT:
        return content[:_MAX_SNAPSHOT] + f"\n... (truncated at {_MAX_SNAPSHOT} chars)"
    return content


def _safe_read_snapshot(path: str) -> str | None:
    """Read a file's content for snapshot purposes, refusing sensitive paths.

    Routes path validation through ``hooks.validate_file_path`` (the same
    helper hooks.py uses for its own file ops) so the sensitive-path check
    has a single enforcement point — if the security policy gains additional
    checks in the future, both the LLM-tool intercept layer and the snapshot
    layer pick them up automatically.

    Returns the (possibly truncated) text content, or None if the path is
    sensitive / not a regular file / unreadable.
    """
    try:
        validated = validate_file_path(path)
        if validated is None:
            return None
        p = Path(validated)
        if not p.is_file():
            return None
        return _truncate_snapshot(p.read_text(errors="replace"))
    except Exception:
        return None


def _reconstruct_str_replace_before(path: str, raw_params: dict) -> str | None:
    """Reconstruct the FULL-FILE before-content for a strReplace edit.

    kiro-cli's ACP diff content block carries only the replaced FRAGMENT as
    ``oldText`` for strReplace — not the whole file. Using it verbatim as the
    before-snapshot made the chip diff a one-line fragment against the
    full-file after, counting the entire file as additions (regression from
    the #920 race fix, which correctly assumed full-file ``oldText`` for
    create but not for strReplace).

    Instead, read the file from disk and classify it by testing BOTH
    hypotheses explicitly, reconstructing only when exactly one is plausible
    (needle presence alone proves nothing — ``oldStr`` can re-form across
    the replacement seam, e.g. ``oldStr="ab", newStr="a", before="abb"`` →
    after ``"ab"``):

    * ``newStr`` absent → post-write excluded (post-write content always
      contains ``newStr``); pre-write proven iff ``oldStr`` occurs exactly
      once (the tool refuses ambiguous ``oldStr``).
    * ``newStr`` present but not unique (overlap-safe ``find()==rfind()``)
      → post-write can neither be excluded nor reversed → decline.
    * ``newStr`` unique → the single reversal candidate decides: candidate
      tool-consistent AND pre-write plausible → undecidable, decline (seam
      shapes); consistent only → reverse; inconsistent with pre-write
      plausible → pre-write proven (post-write excluded).

    Returns None when reconstruction isn't provable (missing/empty params —
    including an empty ``newStr`` deletion, whose position in the after-state
    is unrecoverable — ``replaceAll`` edits, where oldStr uniqueness is not
    enforced and reversal would over-revert pre-existing ``newStr``
    occurrences, non-regular or oversized files (``_MAX_RECONSTRUCT_BYTES``),
    unreadable files, or an undecidable/implausible state); the caller then
    falls through to the pre-existing source-priority chain.
    """
    old_str = raw_params.get("oldStr")
    new_str = raw_params.get("newStr")
    if not isinstance(old_str, str) or not isinstance(new_str, str) or not old_str or not new_str:
        return None
    if raw_params.get("replaceAll"):
        # replaceAll is the one mode where strReplace does NOT enforce oldStr
        # uniqueness, so the pre-write proof below doesn't hold and reversing
        # every newStr occurrence over-reverts any that pre-existed the edit
        # (server review finding: fabricated counts). Position/count is
        # unrecoverable — decline and fall through to the fragment chain.
        return None
    try:
        # Bound the read (server review findings): only regular files —
        # /dev/zero and FIFOs stat as 0 bytes but read unboundedly — and
        # only up to _MAX_RECONSTRUCT_BYTES (re-checked after the read,
        # since stat() races with an external writer growing the file).
        st = Path(path).expanduser().stat()
        if not stat_module.S_ISREG(st.st_mode) or st.st_size > _MAX_RECONSTRUCT_BYTES:
            return None
        # Read through hooks.safe_read_file — the symlink-safe chokepoint
        # (re-checks the RESOLVED target + O_NOFOLLOW open, closing the
        # validate→read TOCTOU window; AWS-33/AWS-62). Raw content, no
        # truncation: the cap must apply AFTER the reverse substitution or
        # the needle could be cut mid-file. PermissionError (sensitive
        # target / symlink race) and ordinary read errors both decline via
        # the except-fallback — file-chip capture must never block a turn.
        content = safe_read_file(path)
    except Exception:
        return None
    if len(content) > _MAX_RECONSTRUCT_BYTES:
        # Re-check after the read: the stat() gate above races with an
        # external writer growing the file, and the substring scans below
        # are O(n) — keep them bounded.
        return None
    pre_write_plausible = content.count(old_str) == 1
    # Post-write content ALWAYS contains newStr (the edit just inserted it),
    # so newStr absent excludes post-write entirely.
    if new_str not in content:
        return content if pre_write_plausible else None
    # newStr present but NOT unique (overlap-safe: find()==rfind()):
    # post-write can neither be excluded (any occurrence could be the edit
    # site) nor reversed (ambiguous). Server review finding: strReplace
    # "ab"→"a" on "aabb" → "aab" looks pre-write-plausible (one "ab") but
    # IS post-write — classifying it pre-write recorded the after as the
    # before and erased the edit from the chip. Decline.
    if content.count(new_str) != 1 or content.find(new_str) != content.rfind(new_str):
        return None
    # newStr unique: the single possible reversal candidate decides.
    candidate = content.replace(new_str, old_str, 1)
    post_write_consistent = candidate.count(old_str) == 1
    if post_write_consistent and pre_write_plausible:
        return None  # seam shapes: valid as both states — undecidable
    if post_write_consistent:
        return candidate
    if pre_write_plausible:
        # Post-write EXCLUDED (its only possible edit site is
        # tool-inconsistent), so pre-write is proven even with newStr
        # coincidentally present in the file.
        return content
    return None


def _snapshot_write_target(
    raw_params: dict | None,
    diff_old_text: str | None = None,
    diff_path: str = "",
) -> dict | None:
    """Return {"path", "content"} of a file before modification for write tools.

    For strReplace, FIRST reconstructs the full-file before via
    ``_reconstruct_str_replace_before`` (disk read + reverse substitution),
    because the ACP diff content block's ``oldText`` is only the replaced
    fragment for that command.

    Otherwise prefers the authoritative ``diff_old_text`` from the ACP diff
    content block (kiro-cli's in-band before-text) over a disk read, because
    by the time we process the event the write has already landed on disk
    (the auto-approved path is a one-way notification — kiro-cli does NOT
    wait for the dashboard to drain its asyncio.Queue before executing the
    write). For create the content block IS full-file: ``""`` for a new
    file, the entire previous content for an overwrite.

    Falls back to a disk read only when no content block is present
    (``diff_old_text is None``), which is the correct path for the
    blocking permission-request flow where the file hasn't been written yet.

    Returns None for non-write tools or when a path can't be resolved. Failures
    (file not found, permission, decode errors) yield empty content rather than
    raising — file-chip capture must never block a turn. Sensitive paths
    (~/.aws, ~/.ssh, etc.) yield None so credentials never enter message meta.
    """
    if not isinstance(raw_params, dict):
        return None
    cmd = raw_params.get("command", "")
    path = raw_params.get("path", "") or diff_path
    if not path or cmd not in _WRITE_COMMANDS:
        return None
    # Refuse sensitive paths even before the write executes (the file may not
    # exist yet for `create`, which makes _safe_read_snapshot return None for
    # a different reason). validate_file_path is the same hooks.py helper used
    # by the LLM-tool intercept layer, so the security boundary is identical.
    if validate_file_path(path) is None:
        return None

    # strReplace: the content block's oldText is only the replaced fragment,
    # never the full file — reconstruct the true full-file before from disk +
    # reverse substitution. Falls through to the generic chain when
    # reconstruction is impossible.
    if cmd == "strReplace":
        before_full = _reconstruct_str_replace_before(path, raw_params)
        if before_full is not None:
            return {"path": path, "content": _truncate_snapshot(before_full)}

    # Prefer authoritative content-block before-text when available.
    if diff_old_text is not None:
        # diff_old_text == "" means "file was created" (no previous content).
        # Apply truncation so content-block-sourced text obeys the same cap as
        # disk-sourced text (security + message-meta size invariant).
        before = _truncate_snapshot(diff_old_text) if diff_old_text else ""
        return {"path": path, "content": before}

    # Fallback: read from disk (correct on the blocking permission-request path
    # where the write has NOT yet executed).
    content = _safe_read_snapshot(path)
    if content is None:
        # File doesn't exist yet (`create` on a new file is the common case)
        # OR was unreadable. Either way, record an empty before so the chip
        # still surfaces.
        return {"path": path, "content": ""}
    return {"path": path, "content": content}


def _flush_file_changes(slot: "_ChatSlot") -> None:
    """Attach accumulated file changes to the last assistant message.

    Dedups by path (first before, last after), reads the AFTER content from
    disk, and writes the list to message meta as ``file_changes``. Called on
    every exit path (success / cancel / error) so users always see what was
    modified, even on aborted turns.
    """
    # Defensive: only proceed when a real, non-empty list is present. Tests
    # using MagicMock slots leave _file_changes as a MagicMock attribute
    # (always truthy), so an isinstance check is needed in addition to the
    # length check to avoid a synthetic message getting fabricated when no
    # writes actually happened.
    fc_changes = getattr(slot, "_file_changes", None)
    if not isinstance(fc_changes, list) or not fc_changes:
        return
    # Dedup: keep first before for each path (truest "before") since a file
    # may be modified multiple times in one turn.
    deduped: dict[str, dict[str, str]] = {}
    for fc in slot._file_changes:
        p = fc["path"]
        if p not in deduped:
            deduped[p] = {"path": p, "before": fc["content"], "after": ""}
    # Read after-content once per path. Uses _safe_read_snapshot so sensitive
    # paths and unreadable files yield empty after rather than crashing or
    # leaking credentials.
    for entry in deduped.values():
        after = _safe_read_snapshot(entry["path"])
        entry["after"] = after if after is not None else ""
    # Scrub credentials and exfil URLs from path/before/after BEFORE attaching
    # to message meta. _save_slot_to_history runs _redact_meta on persist, but
    # the in-memory slot.messages reaches the dashboard UI via SSE/WS BEFORE
    # persistence — so without this, a config file containing an AKIA* key
    # (path not on the sensitive-path list) would briefly appear in the chip
    # diff. Redact in place so both the live and persisted views are clean.
    for entry in deduped.values():
        entry["path"], _ = redact_credentials(entry["path"])
        entry["path"], _ = redact_exfiltration_urls(entry["path"])
        if entry["before"]:
            entry["before"], _ = redact_credentials(entry["before"])
            entry["before"], _ = redact_exfiltration_urls(entry["before"])
        if entry["after"]:
            entry["after"], _ = redact_credentials(entry["after"])
            entry["after"], _ = redact_exfiltration_urls(entry["after"])
    # No-op entries (before == after, e.g. an idempotent format-on-save)
    # are deliberately KEPT: the dashboard renders an explicit "no changes"
    # caption for them (FileChangeChips) instead of a contentless diff, so
    # the UI is the single place that answers the no-op state. Dropping them in
    # the backend would compare post-truncation/post-redaction content, which
    # would silently discard real changes past the snapshot limit or inside
    # redacted spans.
    fc_list = list(deduped.values())
    # Attach to the most recent assistant message; if none exists (turn
    # aborted before any text), create a synthetic message so the chips
    # still surface.
    for m in reversed(slot.messages):
        if m.get("role") == "assistant":
            m.setdefault("meta", {})["file_changes"] = fc_list
            break
    else:
        # broadcast=False: the synthetic message reaches the UI via the same
        # SSE/WS path the dashboard already drains for this slot. Default
        # broadcast=True would schedule a fan-out via asyncio.ensure_future(),
        # which (a) is redundant here and (b) raises RuntimeError when the
        # function is invoked from a sync context like a unit test.
        slot.append(
            "assistant",
            "*(stopped — files were modified)*",
            "msg msg-a",
            broadcast=False,
            meta={"file_changes": fc_list},
        )
    logger.info("Attached %d file_changes to slot %s", len(fc_list), slot.key)
    # Honour the in-place-mutation contract (see resolve_permission_message in
    # state.py): "the periodic flush skips non-dirty slots, so an unflagged
    # in-place mutation can be lost on restart". The assistant-message branch
    # above mutates meta in place without appending, so nothing else marks the
    # slot dirty. That matters on the error/cancel call path, which — unlike the
    # success path — is NOT followed by an explicit save_slot_off_loop: without
    # this flag a periodic flush that snapshotted the message just before this
    # write clears _dirty, and the file_changes never reach disk.
    # (slot.append in the else branch already sets it; setting it once here
    # covers both branches and cannot be missed by a later edit.)
    slot._dirty = True
    slot._file_changes = []


def _attach_turn_stats(
    slot: "_ChatSlot",
    elapsed_ms: int,
    credits: float,
    cost_usd: float,
    turn_boundary: int = 0,
    model: str = "",
) -> None:
    """Attach per-turn stats to the last assistant message's meta.

    Mirrors ``_flush_file_changes``: the meta lands on the in-memory message
    BEFORE ``_save_slot_to_history`` persists it, and reaches the live UI via
    the ``chat_done`` → ``refreshSlot`` re-fetch (no dedicated WS event).

    ``elapsed_ms`` is the turn wall clock (or the provider-reported duration
    when available); ``credits`` is kiro-cli's per-turn ``meteringUsage`` sum;
    ``cost_usd`` is claude_code's API-reported cost. ``model`` is what served
    this turn (``read_turn_model``): a concrete id on a pinned session, or the
    bare ``"auto"`` when the turn was handed to Auto and the backend disclosed
    no id for it — Auto's per-turn choice is not on the ACP wire, so ``"auto"``
    is the whole of what can be said truthfully. Zero/empty fields are omitted
    so the frontend renders only what the provider actually reported.

    ``turn_boundary`` is ``len(slot.messages)`` captured at turn start: only
    messages appended DURING this turn are candidates. Without it, an
    error/refusal-only turn (which appends no assistant message) would walk
    back into the PREVIOUS turn's assistant message and overwrite its stats
    with the failed turn's numbers. No-op when the turn produced no assistant
    message or when there is nothing to show.
    """
    if elapsed_ms <= 0:
        return
    stats: dict[str, Any] = {"elapsed_ms": int(elapsed_ms)}
    if credits > 0:
        stats["credits"] = round(credits, 4)
    if cost_usd > 0:
        stats["cost_usd"] = round(cost_usd, 6)
    if model:
        stats["model"] = model
    boundary = max(0, turn_boundary)
    for m in reversed(slot.messages[boundary:]):
        if m.get("role") == "assistant":
            m.setdefault("meta", {})["turn_stats"] = stats
            break


def _redact_acp_string(s: str) -> str:
    """Scrub credentials + exfil URLs from an ACP-controlled string.

    server_name and error fields come from kiro-cli (and ultimately from the
    MCP server's own metadata).  Treat them as untrusted: they end up in chat
    content and in the live WS broadcast, both of which are external surfaces.
    """
    if not s:
        return s
    s, _ = redact_credentials(s)
    s, _ = redact_exfiltration_urls(s)
    return s


# Native subagent cards carry a short error string only, so a long provider
# message is clipped. The request id is what identifies the failure
# server-side and the formatter appends it LAST, so a plain head-slice drops
# precisely the part worth keeping.
_MAX_NATIVE_CARD_ERROR = 200
_RE_TRAILING_REQUEST_ID = re.compile(r"\(request_id:\s*[0-9a-fA-F-]+\)\s*$")


def _clip_card_error(text: str, limit: int = _MAX_NATIVE_CARD_ERROR) -> str:
    """Clip *text* to *limit* characters, keeping any trailing request id."""
    if len(text) <= limit:
        return text
    match = _RE_TRAILING_REQUEST_ID.search(text)
    if not match:
        return text[:limit]
    suffix = match.group(0).strip()
    head = limit - len(suffix) - 4  # room for the elision marker and a space
    if head <= 0:
        return text[:limit]
    return f"{text[:head]}... {suffix}"


def _emit_mcp_oauth_request(
    state: "DashboardState",
    slot: "_ChatSlot",
    server_name: str,
    oauth_url: str,
    card_owned: bool = False,
) -> None:
    """Append an mcp_oauth banner so the user can authorize an MCP server.

    If the URL is unsafe (non-http(s) scheme) or carries a credential / exfil
    pattern, surface a *rejected* banner explaining why instead of silently
    dropping.  Otherwise the user has no idea their MCP server failed to
    authenticate, and they can't escalate to whoever owns that server.

    ``card_owned`` records that some other surface owns this request's consent
    flow end to end — see :func:`_connections_managed_mcp_names`. It is an
    annotation, not a filter: the message is appended either way, because it is
    also the data feed that surface reads its approval URL out of. Only the
    render layer may act on it. Left off, the meta key is absent and the message
    is byte-identical to an unannotated one.

    Deliberately annotates the authorize banner only. A rejected URL is a
    security notice rather than a consent prompt — no card can act on it — so it
    stays unconditionally visible wherever banners render.
    """
    safe_name = _redact_acp_string(server_name)
    label = safe_name or "MCP server"

    if not _is_safe_oauth_url(oauth_url):
        logger.warning("ACP: refusing unsafe MCP OAuth URL for %s", server_name or "(unknown)")
        slot.append(
            "mcp_oauth",
            f"🚫 {label} sent an unsafe authentication URL (scheme rejected).",
            "msg msg-warn",
            meta={
                "server_name": safe_name,
                "failed": True,
                "rejected_url": True,
                "error": "unsafe URL scheme",
            },
        )
        return
    if oauth_url_contains_credential(oauth_url):
        # Two distinct causes reach this branch and the user cannot tell them
        # apart from the banner alone:
        #   1. A genuinely bogus URL — legitimate OAuth consent URLs carry
        #      state/code_challenge/client_id, never AKIA*/Bearer/etc.
        #   2. A legitimate consent URL at an endpoint outside
        #      ``_OAUTH_AUTHORIZATION_ENDPOINTS``. The PKCE entropy carve-out
        #      applies only at an approved (host, path), so an unlisted
        #      self-hosted IdP has its ``code_challenge`` scanned as a bare
        #      secret and fails closed.
        # Case 2 has a remedy (the ``oauth_endpoints.json`` operator keystone,
        # see security._load_operator_oauth_endpoints) but it is agent-fenced
        # with no dashboard writer, so naming it here is the only way the user
        # learns it exists. Without this the failure reads as unfixable.
        logger.warning(
            "ACP: rejecting MCP OAuth URL with credential/exfil pattern for %s",
            server_name or "(unknown)",
        )
        slot.append(
            "mcp_oauth",
            f"🚫 {label} sent an authentication URL containing a credential "
            "pattern (rejected). If this is a self-hosted or otherwise "
            "unlisted identity provider, its authorization endpoint may need "
            "adding to oauth_endpoints.json in the Kiro Crew data home; "
            "otherwise ask the server owner to fix the URL.",
            "msg msg-warn",
            meta={
                "server_name": safe_name,
                "failed": True,
                "rejected_url": True,
                "error": "URL contained credential or exfiltration pattern",
                "remedy": "oauth_endpoints.json",
            },
        )
        return
    content = f"🔐 {label} requires authentication."
    meta: dict[str, Any] = {"server_name": safe_name, "oauth_url": oauth_url}
    if card_owned:
        meta["card_owned"] = True
    slot.append(
        "mcp_oauth",
        content,
        "msg msg-info",
        meta=meta,
    )


def _connections_managed_mcp_names() -> frozenset[str]:
    """Servers whose OAuth consent a rendered Connections card owns end to end.

    Membership is an ownership FACT, not a decision about what the user sees: it
    only tells the caller that a card surface drives this server's consent flow,
    so a request for it can be tagged ``card_owned`` and the render layer given
    something to act on. Nothing here suppresses anything.

    Two conditions, both required, each consumed from the facility that already
    decides it rather than re-derived here:

    * :func:`kirocrew_managed_names` -- our own MCP store wrote the entry. This is
      the single ownership discriminator, shared with the agent-spec emit path and
      the config-sync gate, so ownership means one thing everywhere.
    * :func:`get_visible_providers` -- the name is a Connections provider with a
      rendered card. Connect keys the store by provider slug and the card reads it
      back by slug, so the slug is the join between the two.

    Ownership ALONE is not enough. The dashboard's add-custom-server API writes to
    the same store, so a hand-added remote is every bit as "ours" while having no
    card anywhere. A provider whose launch gate is closed has no card either.
    Requiring a card keeps the annotation on servers that genuinely have a second
    surface, so the render layer never has to second-guess it.

    Registry slugs are slash-free, so ``mcp_server_alias`` is the identity on this
    set and kiro-cli's ``serverName`` is the slug verbatim -- no alias widening is
    needed. What remains open is an exact-slug collision: a server hand-added
    under a real slug is annotated, though it still renders on that provider's
    card, so a surface survives.

    Does blocking file I/O (store read + registry read) -- callers on the event
    loop must hand it to a worker thread.

    FAILS OPEN to the empty set on any error: nothing is annotated and every
    surface renders every banner, which is exactly today's behavior.
    """
    try:
        managed = kirocrew_managed_names()
        carded = {provider["slug"] for provider in get_visible_providers()}
    except Exception:
        logger.warning("Cannot resolve Connections-owned MCP names", exc_info=True)
        return frozenset()
    return frozenset(managed & carded)


async def _drain_session_init_oauth_requests(
    state: "DashboardState", slot: "_ChatSlot", client: Any
) -> None:
    """Surface the MCP OAuth requests kiro-cli buffered during session init.

    kiro-cli emits ``_kiro.dev/mcp/oauth_request`` while bringing MCP servers up;
    ``AcpClient`` collects them into ``pending_oauth_requests``. EVERY one is
    emitted as an ``mcp_oauth`` message, with no exceptions — that message is not
    just a banner, it is the state feed the Connections card reads its approval
    URL out of, so dropping one costs the user their only way to authorize.

    Requests for a server a Connections card owns are tagged ``card_owned`` (see
    :func:`_connections_managed_mcp_names`) purely so the render layer can decide
    whether chat needs to repeat a prompt the card already shows. That is a
    presentation question and it is answered where the flag that governs the card
    is known — not here.

    Async because resolving ownership reads files; the lookup runs in a worker
    thread and only when there is something to tag.
    """
    acp_client = getattr(client, "client", None)
    pop_pending = getattr(acp_client, "pop_pending_oauth_requests", None)
    if not callable(pop_pending):
        return
    pending = pop_pending() or []
    if not pending:
        # Resolve ownership only when there is something to tag — this runs on
        # every session init and the common case is zero requests.
        return
    managed = await asyncio.to_thread(_connections_managed_mcp_names)
    for req in pending:
        if not isinstance(req, dict):
            continue
        server_name = req.get("serverName") or ""
        # Raw (unredacted) name on purpose: store keys are raw and this is a
        # set-membership test, so an untrusted value can only miss. Redaction
        # happens inside _emit_mcp_oauth_request.
        _emit_mcp_oauth_request(
            state,
            slot,
            server_name,
            req.get("oauthUrl") or "",
            card_owned=bool(server_name) and server_name in managed,
        )


def _mark_mcp_oauth_completed(
    state: "DashboardState", slot: "_ChatSlot", server_name: str, success: bool, error: str = ""
) -> None:
    """Patch the most recent open mcp_oauth banner for ``server_name`` to a terminal state."""
    safe_name = _redact_acp_string(server_name)
    target: dict | None = None
    for m in reversed(slot.messages):
        if m.get("role") != "mcp_oauth":
            continue
        meta = m.get("meta") or {}
        # Compare against the redacted form already stored on the banner.
        if meta.get("server_name") != safe_name:
            continue
        if meta.get("completed") or meta.get("failed"):
            continue
        target = m
        break
    if target is None:
        return
    # Redact the RESTORED payload before it is re-emitted. This function copies the
    # whole stored dict into both slot.messages and the `chat_message_update`
    # broadcast below, and that broadcast bypasses _prepare_messages — a genuine
    # egress point.
    #
    # Scope of the exposure, stated precisely: the SAVE path already redacts meta
    # (`_build_message_entry`), and `ConversationLog.append` has no `meta` parameter
    # at all, so meta this version wrote to disk comes back already clean. What this
    # guards is history lines this version did not write — legacy lines, a tampered
    # session file, or the verbatim-preserved foreign byte ranges. That is the same
    # threat model the sibling gates are written against, so it is defence in depth
    # rather than a live hole.
    #
    # The matching loop above reads only control fields (`server_name`,
    # `completed`, `failed`), which is why this reader looked safe on a first pass.
    # What decides safety is not which fields a reader INSPECTS but whether it
    # re-emits the dict. This one does.
    #
    # CAREFUL — `_redact_meta_for_role` is STRICTER than the emit-path gate and does
    # NOT preserve realistic `oauth_url`s: it calls `redact_exfiltration_urls`,
    # whose query-length (>=200) and base64-blob heuristics blank a real Google OIDC
    # or GitHub PKCE consent URL. (Measured: those two are blanked; only a short URL
    # survives.) The emit-path gate `security.oauth_url_contains_credential`
    # deliberately exempts OAuth params from exactly those heuristics — it is
    # "the sole path allowed to exempt standard OAuth entropy from the generic
    # URL heuristics" (its docstring).
    #
    # That is harmless HERE only because `oauth_url` is dead data by this point:
    # every path through this function sets `completed` or `failed`, and
    # McpOAuthBanner.tsx returns on the `failed` (line 50) and `completed` (line 61)
    # branches BEFORE the link-rendering branch (line 73). Do NOT reuse this gate on
    # a path where the authorize link is still rendered — there it would break the
    # user's ability to authorize an MCP server.
    new_meta = _redact_meta_for_role("mcp_oauth", dict(target.get("meta") or {}))
    if success:
        new_meta["completed"] = True
        new_meta.pop("failed", None)
        new_meta.pop("error", None)
    else:
        new_meta["failed"] = True
        safe_err = _redact_acp_string(error)
        if safe_err:
            new_meta["error"] = safe_err
    label = safe_name or "MCP server"
    new_content = f"🔓 {label} authenticated." if success else f"🚫 {label} authentication failed."
    updated = slot.update_message(target.get("ts", ""), content=new_content, meta=new_meta)
    if updated is None:
        return
    state.broadcast_ws(
        "chat_message_update",
        {"slot": slot.key, "ts": target.get("ts", ""), "meta": new_meta, "content": new_content},
    )


def _tool_meta(event: "LLMEvent") -> dict[str, str] | None:
    """Build the meta dict persisted on a tool message — `tool_call_id`,
    `purpose`, and the full redacted `input`. Output is appended later by the
    EVENT_TOOL_RESULT handler. The inline detail panel is the only source of
    truth for what an agent ran, so the meta carries the full content (capped
    at 1 MB and 8 KB respectively as defensive safety nets).

    `tool_call_id` is redacted to match `_broadcast_auto_tool` in
    `chat_utils.py`, which has always redacted before the WS broadcast.
    Keeping it consistent across persisted meta and live broadcast means the
    frontend join (`toolLog[i].tool_call_id` ↔ `message.meta.tool_call_id`)
    works whether the entry came from the live tool-call event or from a
    historical replay. Comparison sites (e.g. EVENT_TOOL_RESULT) must
    redact `event.tool_call_id` before matching against the stored value."""
    if not event.tool_call_id:
        return None
    return {
        "tool_call_id": _redact_tool_field(event.tool_call_id),
        "purpose": _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE),
        "input": _redact_tool_field(event.tool_input),
    }


def _tool_call_ws_payload(event: "LLMEvent") -> dict[str, str | bool]:
    """Build the live dashboard payload for a tool invocation.

    ``is_shell`` is intentionally an explicit capability signal rather than a
    frontend guess based on the tool title. Shell commands usually have no
    trustworthy total, so the dashboard can render an indeterminate status
    today while future tools can add a real progress mode without changing the
    tool-card data flow.
    """
    title, _ = redact_exfiltration_urls(event.title)
    title, _ = redact_credentials(title)
    kind, _ = redact_exfiltration_urls(event.tool_kind)
    kind, _ = redact_credentials(kind)
    return {
        "slot": "",  # Filled by the caller because it belongs to the session.
        "tool": title,
        "kind": kind,
        "is_shell": event.is_shell,
        "tool_call_id": _redact_tool_field(event.tool_call_id),
        "purpose": _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE),
        "input_preview": _redact_tool_field(event.tool_input),
    }


# Known redirect forms where & is NOT a command separator:
# N>&M (e.g. 2>&1), &> file, &>> file, >&N
_REDIRECT_PLACEHOLDER = "\x00REDIR\x00"
_REDIRECT_RE = re.compile(r"[0-9]*>&[0-9]*|&>>?")
# After redirects are masked, split on remaining separators.
_CMD_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|&|\n|\|)\s*")
# Grant-safe variant: excludes bare & (background/arithmetic) and \n (display)
# because this function serves the Trust dropdown (grant direction) where each
# extra segment becomes one more binary offered for auto-approval.
_CMD_GRANT_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|\|)\s*")
# Command substitution forms that split-then-fnmatch cannot safely reach:
# $(...), backticks, and process substitution <(...)/>(...). Deny-by-default
# when any are present — the pattern match would operate on the outer shell
# syntax, not the embedded sub-command, giving a false sense of authorization.
_CMD_SUBSTITUTION_RE = re.compile(r"\$\(|`|<\(|>\(")


def _mask_quoted_separators(text: str, *, mask_escaped: bool = False) -> tuple[str, dict[str, str]]:
    """Replace command separators that appear INSIDE quotes with placeholders.

    The split regex (``_CMD_SPLIT_RE``) is quote-unaware, so a separator inside
    a quoted string — e.g. the ``|`` in ``grep "a|b" file && wc -l`` — would be
    treated as a command boundary, mis-segmenting a command the user trusted and
    denying it (fail-closed but a real usability regression). We walk the string
    tracking single/double quote state and swap any ``| & ; \\n`` that is quoted
    for a unique placeholder, restoring it inside each segment before matching.
    Returns ``(masked_text, restore_map)``.

    Quote tracking MUST honor backslash escapes, because getting this wrong is a
    segmentation bypass rather than a cosmetic error. ``type 'foo'\\'; cmd``
    closes its quote at the second ``'``, so the ``\\'`` that follows is a literal
    apostrophe OUTSIDE quotes and the ``;`` is a real separator the shell acts
    on. Reading that ``\\'`` as an opening quote instead makes the rest of the
    line look quoted, the ``;`` gets masked, the whole line reads as one segment,
    and an appended command rides in behind whatever the first segment was
    allowed to do.

    A backslash escapes the next character everywhere EXCEPT inside single
    quotes, where the shell treats it literally — the same rule
    :func:`_unquoted_shell_hazard` applies, and for the same reason.
    """
    out: list[str] = []
    restore: dict[str, str] = {}
    quote: str | None = None
    escaped = False
    n = 0
    for ch in text:
        if escaped:
            escaped = False
            # When mask_escaped is True (grant path), an escaped separator
            # (e.g. \|) is treated as a literal — mask it so the split regex
            # skips it.  When False (deny path), escaped separators still
            # segment because treating \; as a literal would let an attacker
            # hide a second command behind an escape.
            if mask_escaped and ch in "|&;\n":
                ph = f"\x00SEP{n}\x00"
                n += 1
                restore[ph] = ch
                out.append(ph)
            else:
                out.append(ch)
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            out.append(ch)
            continue
        if quote:
            if ch == quote:
                quote = None
            elif ch in "|&;\n":
                ph = f"\x00SEP{n}\x00"
                n += 1
                restore[ph] = ch
                out.append(ph)
                continue
        elif ch in ("'", '"'):
            quote = ch
        out.append(ch)
    return "".join(out), restore


# Native kiro-cli subagents (``use_subagent``) are surfaced in the Activity tab
# via the ``_kiro.dev/subagent/list_update`` notification (one card per
# sub-agent), handled by ``_native_subagent_sync`` below. The list_update gives
# authoritative per-sub-agent identity/status, so the ``subagent`` tool call's
# ``stages`` payload is not parsed.

_NATIVE_SUBAGENT_STALE_SECS = 120.0  # auto-close cards with no progress after 2 min


def _native_done_result(chunks: "list[str] | None") -> str:
    """Return the newest bounded native-card output with a truncation marker."""
    joined = "".join(chunks or [])
    if len(joined) <= NATIVE_SUBAGENT_DONE_RESULT_CAP:
        return joined
    return NATIVE_SUBAGENT_DONE_TRUNC_MARKER + joined[-NATIVE_SUBAGENT_DONE_RESULT_CAP:]


def _append_native_output(
    buf: list[str],
    text: str,
    total: int,
    cap: int = NATIVE_SUBAGENT_OUTPUT_TAIL,
    hard: int = NATIVE_SUBAGENT_OUTPUT_HARD,
) -> int:
    """Append output and collapse it to the newest tail past the hard ceiling."""
    buf.append(text)
    total += len(text)
    if total > hard:
        tail = "".join(buf)[-cap:]
        buf[:] = [tail]
        total = len(tail)
    return total


def _native_card_feed(card_output, card_id: str) -> str:
    """Build, bound, and redact native output at the broadcast boundary."""
    feed = _native_done_result((card_output or {}).get(card_id))
    if feed:
        feed, _ = redact_exfiltration_urls(feed)
        feed, _ = redact_credentials(feed)
    return feed


def _native_subagent_sync(state, slot, subagents, tracker, card_output=None) -> None:
    """Reconcile per-subagent Activity cards from a kiro-cli
    ``_kiro.dev/subagent/list_update`` notification.

    Native (``use_subagent``) crews run *inside* the parent kiro-cli session, so
    their internal tool calls are not attributable per sub-agent over standard
    ACP. But kiro-cli also emits this list (the same data its TUI shows) with one
    entry per sub-agent carrying ``sessionId``, ``sessionName``,
    ``role``/``agentName``, ``initialQuery`` and ``status``. We map each entry to
    its own Activity card (``native:<sessionId>``): spawned when first seen,
    completed when its status terminates. This yields one card per sub-agent
    (matching the spawn_run/spawn_sub_agents card model) with no agent-prompt
    changes.

    ``tracker`` is per-turn state: ``{session_id: {started, done, agent, task}}``.
    """
    if not isinstance(subagents, list):
        return
    _running = ("working", "running", "pending", "queued", "in_progress", "")
    now = time.time()
    # Track which sids are still reported by kiro-cli this update
    _seen_sids: set[str] = set()
    for sub in subagents:
        if not isinstance(sub, dict):
            continue
        sid = str(sub.get("sessionId") or "")
        if not sid:
            continue
        _seen_sids.add(sid)
        card_id = f"native:{_redact_tool_field(sid)}"
        _status_raw = sub.get("status")
        status = _status_raw if isinstance(_status_raw, dict) else {}
        stype = str(status.get("type") or "").lower()
        smsg = str(status.get("message") or "")
        if sid not in tracker:
            agent, _ = redact_exfiltration_urls(str(sub.get("role") or sub.get("agentName") or ""))
            agent, _ = redact_credentials(agent)
            task, _ = redact_exfiltration_urls(
                str(sub.get("initialQuery") or sub.get("sessionName") or "")[:2000]
            )
            task, _ = redact_credentials(task)
            # Skip cards with empty task entirely — kiro-cli sometimes emits
            # list_update notifications where initialQuery/sessionName are both
            # empty. Showing an Activity card with no meaningful input is
            # confusing UX ("Starting..." with nothing to explain what it does).
            # Mark as done immediately so we don't re-process on next update.
            if not task.strip():
                tracker[sid] = {
                    "started": now,
                    "done": True,
                    "agent": agent,
                    "task": "",
                    "last_activity": now,
                }
                logger.debug(
                    "native subagent skipped (empty task): sid=%s slot=%s",
                    sid,
                    slot.key,
                )
                continue
            tracker[sid] = {
                "id": card_id,
                "started": now,
                "done": False,
                "agent": agent,
                "task": task,
                "last_activity": now,
                "last_tool": "",
            }
            # Register in state-level dict so DELETE /api/spawn can cancel native cards.
            _register_native_card(state, card_id, slot.key, sid)
            logger.debug(
                "native subagent spawn broadcast: id=%s agent=%s slot=%s",
                card_id,
                agent,
                slot.key,
            )
            state.broadcast_ws(
                "subagent_spawn",
                {"id": card_id, "slot": slot.key, "task": task, "agent": agent},
            )
        else:
            # Card is still being reported this update — keep it alive. Update
            # unconditionally (not just on non-empty smsg): a card reported
            # with empty status messages for >120s would otherwise be
            # auto-closed the instant it disappears from the list, since its
            # last_activity would still be the creation timestamp.
            tracker[sid]["last_activity"] = now
        info = tracker[sid]
        if info["done"]:
            continue
        if stype and stype not in _running:
            err = None
            if stype in ("failed", "error") and smsg:
                err, _ = redact_exfiltration_urls(smsg)
                err, _ = redact_credentials(err)
                err = _clip_card_error(err)
            info["done"] = True
            _feed = _native_card_feed(card_output, card_id)
            _elapsed = time.time() - info["started"]
            _result = _feed or "(output in chat)"
            info["elapsed"] = _elapsed
            info["error"] = err
            info["result"] = _result
            info["done_at"] = time.time()
            _unregister_native_card(state, card_id)
            state.broadcast_ws(
                "subagent_done",
                {
                    "id": card_id,
                    "slot": slot.key,
                    "elapsed": _elapsed,
                    "error": err,
                    "task": info["task"],
                    "agent": info["agent"],
                    "result": _result,
                },
            )
        elif smsg and smsg.lower() != "running":
            # Surface a non-generic status message as the card's current tool.
            tool, _ = redact_exfiltration_urls(smsg)
            tool, _ = redact_credentials(tool)
            info["last_tool"] = tool[:80]
            state.broadcast_ws(
                "subagent_tool",
                {"id": card_id, "slot": slot.key, "tool": tool[:80]},
            )

    # Staleness timeout: auto-close cards that kiro-cli no longer reports and
    # that have had no activity for too long. This prevents native sub-agent
    # cards from staying stuck in "Starting..." indefinitely when kiro-cli
    # fails to emit a terminal status. Cards still present in the current
    # list_update are alive by definition and never timed out here.
    for sid, info in list(tracker.items()):
        if info.get("done"):
            continue
        if sid in _seen_sids:
            continue  # still reported by kiro-cli this update — not stale
        last_act = info.get("last_activity", info["started"])
        if now - last_act > _NATIVE_SUBAGENT_STALE_SECS:
            info["done"] = True
            _cid = f"native:{_redact_tool_field(sid)}"
            _feed = _native_card_feed(card_output, _cid)
            _elapsed = now - info["started"]
            _error = "timed out (no activity)"
            _result = _feed or "(no output received)"
            info["elapsed"] = _elapsed
            info["error"] = _error
            info["result"] = _result
            info["done_at"] = now
            _unregister_native_card(state, _cid)
            state.broadcast_ws(
                "subagent_done",
                {
                    "id": _cid,
                    "slot": slot.key,
                    "elapsed": _elapsed,
                    "error": _error,
                    "task": info.get("task", ""),
                    "agent": info.get("agent", ""),
                    "result": _result,
                },
            )
            logger.info(
                "native subagent %s auto-closed: stale for %.0fs",
                _cid,
                now - last_act,
            )


def _register_native_card(state, card_id: str, slot_key: str, session_id: str) -> None:
    """Register a native subagent card in the state-level dict for cancel support."""
    if not hasattr(state, "_native_cards"):
        state._native_cards = {}  # card_id -> {slot, session_id, started}
    state._native_cards[card_id] = {
        "slot": slot_key,
        "session_id": session_id,
        "started": time.time(),
    }


def _unregister_native_card(state, card_id: str) -> None:
    """Remove a native subagent card from the state-level dict."""
    if hasattr(state, "_native_cards"):
        state._native_cards.pop(card_id, None)


def _native_subagent_close_all(state, slot, tracker, card_output=None) -> None:
    """Complete any still-open native subagent cards (turn-end safety net)."""
    for sid, info in tracker.items():
        if info.get("done"):
            continue
        info["done"] = True
        _cid = f"native:{_redact_tool_field(sid)}"
        _feed = _native_card_feed(card_output, _cid)
        _elapsed = time.time() - info.get("started", time.time())
        _result = _feed or "(output in chat)"
        info["elapsed"] = _elapsed
        info["error"] = None
        info["result"] = _result
        info["done_at"] = time.time()
        _unregister_native_card(state, _cid)
        state.broadcast_ws(
            "subagent_done",
            {
                "id": _cid,
                "slot": slot.key,
                "elapsed": _elapsed,
                "error": None,
                "task": info.get("task", ""),
                "agent": info.get("agent", ""),
                "result": _result,
            },
        )


def _retain_terminal_native(
    tracker: "dict[str, dict]",
    keep: int = NATIVE_SUBAGENT_TERMINAL_KEEP,
    ttl_secs: float = NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    now: "float | None" = None,
) -> "dict[str, dict]":
    """Retain recent terminal records for bounded post-turn reconnect replay."""
    current = time.time() if now is None else now
    terminal = [
        (sid, info)
        for sid, info in tracker.items()
        if info.get("done")
        and info.get("id")
        and (current - float(info.get("done_at") or 0.0)) <= ttl_secs
    ]
    if keep >= 0 and len(terminal) > keep:
        terminal.sort(key=lambda item: float(item[1].get("done_at") or 0.0), reverse=True)
        terminal = terminal[:keep]
    return {sid: info for sid, info in terminal}


def _slot_is_trusted(slot: Any) -> bool:
    """True when this slot's tool calls are auto-approved. TWO representations.

    * ``slot._trust`` — the interactive "trust this session" grant. A human clicked
      it, so it does not expire and the click is its own audit record.
    * ``slot._trust_scope`` — a ``SafetyOverride`` SCOPED grant, for an unattended
      app worker where there is no human to click anything. It is SEL-audited
      fail-closed at activation, TTL-bounded, and re-checked HERE on every approval
      via ``is_scope_active`` — so the grant lapsing is what revokes trust, with no
      cooperation required from whatever armed it.

    Strictly additive: the scope is consulted only when the slot actually carries a
    key, so a slot without the attribute — which is every ordinary chat session —
    takes exactly the decision it took before this existed.

    Deliberately does NOT renew the grant. The task runner slides its grant forward
    on tool activity because the run's own progress is the liveness signal; a crew's
    signal is its watchdog, and renewing here would let a crew whose watchdog died
    keep its grant alive off its own tool calls — which is the bound this is for.
    """
    if getattr(slot, "_trust", False):
        return True
    scope = str(getattr(slot, "_trust_scope", "") or "")
    if not scope:
        return False
    return bool(safety_override().is_scope_active(scope))


def _auto_approve_reason(slot: Any, yolo_active: bool) -> str:
    """SEL provenance for an auto-approval: yolo, session trust, or a scoped grant.

    Yolo first because it is process-wide and outranks anything per-slot, then the
    human's session flag, then the scoped grant — the same precedence
    :func:`_slot_is_trusted` decides by. Purely descriptive; it authorises nothing.
    """
    if yolo_active:
        return "yolo"
    if getattr(slot, "_trust", False):
        return "trust"
    if str(getattr(slot, "_trust_scope", "") or ""):
        return "trust_scope"
    return "trust"


def _persistable_session_policy(slot: Any, yolo_active: bool) -> str:
    """The session-level approval policy to STORE for this slot: ``"auto"`` or ``""``.

    Deliberately NOT :func:`_slot_is_trusted`, and that difference is the whole
    point of this function. Everything else on the trust path decides ONE approval
    and re-decides the next one; this value is written into the session store and
    read LATER — by the subagent spawn gate and by each subagent's own approval
    policy — at a point where nothing re-checks whether the grant still holds.

    So only a grant that cannot lapse may be cached here:

    * ``slot._trust`` — a human clicked "trust this session". It does not expire,
      and the click is its own audit record, so caching it changes nothing.
    * yolo — process-wide, and revoking it deactivates the override for everyone.

    A ``SafetyOverride`` SCOPED grant (``slot._trust_scope``) must NOT reach here.
    Its entire value is being re-checked on every approval, so a cached ``"auto"``
    would outlive it: pause or retire the crew, or disable the app, and a turn
    already in flight would keep auto-approving subagent tool calls off a policy
    written before the revocation — exactly the property the scoped grant exists to
    provide, defeated by caching it.

    A scope-trusted worker is not left stalling: its own tool approvals never
    consult this value. They go through :func:`_slot_is_trusted` per event, which
    re-checks the scope each time.
    """
    if yolo_active or getattr(slot, "_trust", False):
        return "auto"
    return ""


def _native_crew_should_auto_approve(native_tracker, state, slot) -> bool:
    """Return True only when a native crew subagent is ACTIVE *and* an
    auto-approve condition holds — otherwise deny (CWE-1188 secure default).

    Active-crew is a NECESSARY precondition: with no live native subagent the
    parent turn is not blocked on a crew tool, so this path must never
    auto-approve — regardless of the ``auto_approve_subagent_tools`` hook,
    the slot's trust, or yolo. Only when a crew is active do those signals grant
    approval; with all three false the tool still falls through to the normal
    interactive/trust gate rather than being silently approved here.
    """
    has_active_crew = bool(native_tracker) and any(
        not info.get("done") for info in native_tracker.values()
    )
    if not has_active_crew:
        return False
    return bool(
        (state.context_builder and state.context_builder.hooks.auto_approve_subagent_tools)
        or _slot_is_trusted(slot)
        or state.is_yolo_active()
    )


def _safe_native_crew_debug_title(title: str) -> str:
    """Redact credentials/exfiltration URLs from an LLM-controlled native-crew
    tool title before it is logged. Control chars are escaped at the log call
    via %r."""
    safe, _ = redact_exfiltration_urls(title or "")
    safe, _ = redact_credentials(safe)
    return safe


def _split_command_segments(
    tool_title: str,
    split_re: "re.Pattern[str] | None" = None,
    mask_escaped: bool = False,
) -> tuple[str, list[str]] | None:
    """Split a shell tool title into its unquoted command segments.

    Returns ``(normalized_title, segments)``. Returns ``None`` — which every
    caller MUST treat as "deny" — when the command contains substitution
    (``$(...)``, backticks, process substitution), because no amount of
    per-segment matching can reach inside a sub-command, or when it contains a
    NUL byte, which would forge one of this function's own placeholders.

    Extracted so that every command-keyed approval path shares ONE splitter:
    a second, independently written shell splitter is exactly how a bypass
    gets introduced (quoted separators, masked redirects, backgrounding).

    Pass ``split_re=_CMD_GRANT_SPLIT_RE`` for the grant path (Trust dropdown)
    where bare ``&`` and ``\\n`` must NOT widen the offered set.
    """
    normalized = _normalize_tool_name(tool_title)
    if _CMD_SUBSTITUTION_RE.search(normalized):
        return None
    # Both masking passes below key on NUL-delimited placeholders
    # (``\x00REDIR\x00``, ``\x00SEP{n}\x00``), so the scheme is only
    # unambiguous while the input carries no NUL of its own. A title that
    # already contains one forges a placeholder: the redirect-restore loop
    # then draws more placeholders than it masked and raises StopIteration
    # (aborting the turn), and a forged ``\x00SEP{n}\x00`` restores to a
    # separator the command never had. NUL is never legitimate here -- execve
    # cannot carry it in an argument -- so deny by default rather than strip,
    # which would match patterns against text that is not what would run.
    if "\x00" in normalized:
        return None
    # First mask separators that live INSIDE quotes (a quoted "a|b" must not be
    # split on its `|`), so _CMD_SPLIT_RE only ever cuts on real, unquoted
    # command boundaries. The placeholders are restored in each segment below.
    quote_masked, sep_restore = _mask_quoted_separators(normalized, mask_escaped=mask_escaped)
    # Two-pass split: mask known redirect forms (2>&1, &>, &>>) so their &
    # isn't mistaken for a background operator, then split on remaining &.
    # Track masked positions to reconstruct original text in each segment.
    redirects: list[str] = []

    def _mask(m: "re.Match") -> str:
        redirects.append(m.group())
        return _REDIRECT_PLACEHOLDER

    masked = _REDIRECT_RE.sub(_mask, quote_masked)
    split_parts = (split_re or _CMD_SPLIT_RE).split(masked)
    # Restore original redirect syntax in each segment for pattern matching.
    redir_iter = iter(redirects)
    segments = []
    for part in split_parts:
        if not part.strip():
            continue
        restored = part
        while _REDIRECT_PLACEHOLDER in restored:
            restored = restored.replace(_REDIRECT_PLACEHOLDER, next(redir_iter), 1)
        # Restore any quoted separators masked before the split.
        for ph, ch in sep_restore.items():
            if ph in restored:
                restored = restored.replace(ph, ch)
        segments.append(restored)
    return normalized, segments


def _matches_trusted_pattern(tool_title: str, patterns: set[str]) -> str | None:
    """Return the matched pattern if tool_title matches any trusted pattern.

    For piped/chained commands, splits into segments and checks each
    independently — ALL segments must match for the command to be trusted.
    Returns comma-joined matched patterns for audit provenance.

    Deny-by-default for commands containing command substitution ($(...),
    backticks, process substitution) — fnmatch cannot reach sub-commands.
    """
    split = _split_command_segments(tool_title)
    if split is None:
        return None
    normalized, segments = split
    if len(segments) > 1:
        matched_patterns = []
        for seg in segments:
            seg_matched = None
            for pattern in patterns:
                if _tool_matches(pattern, seg) or _tool_matches(pattern, f"Running: {seg}"):
                    seg_matched = pattern
                    break
            if seg_matched is None:
                return None
            matched_patterns.append(seg_matched)
        return ",".join(matched_patterns)
    for pattern in patterns:
        if _tool_matches(pattern, tool_title) or _tool_matches(pattern, normalized):
            return pattern
    return None


_BROWSER_CLI_BIN = "playwright-cli"

# Verbs whose entire effect stays INSIDE the browser page/session. These are
# auto-approved when the Playwright CLI is installed (presence-as-consent), so
# that ordinary browsing does not prompt on every step.
#
# This is an ALLOWLIST, not a denylist, so a verb added by a future CLI release
# is denied until it is reviewed and listed — fail-closed, not fail-open.
_BROWSER_CLI_PAGE_VERBS = frozenset(
    {
        # Core / lifecycle. `close` is deliberately absent -- see the
        # exclusion note below. `detach` stays: it releases the session
        # without taking the operator's window with it.
        "open",
        "attach",
        "detach",
        "goto",
        "resize",
        # Interaction
        "type",
        "click",
        "dblclick",
        "fill",
        "drag",
        "drop",
        "hover",
        "select",
        "check",
        "uncheck",
        # Reading the page
        "snapshot",
        "find",
        "generate-locator",
        "highlight",
        # Dialogs
        "dialog-accept",
        "dialog-dismiss",
        # Navigation
        "go-back",
        "go-forward",
        "reload",
        # Keyboard / mouse
        "press",
        "keydown",
        "keyup",
        "mousemove",
        "mousedown",
        "mouseup",
        "mousewheel",
        # Capture (writes only into the service's own output dir)
        "screenshot",
        "pdf",
        # Tabs. `tab-close` is absent for the same reason as `close`.
        "tab-list",
        "tab-new",
        "tab-select",
        # Read-only request metadata: route-list prints the mock table
        # (pattern strings, no URLs) and config-print prints the session's
        # launch configuration.
        "route-list",
        # DevTools / diagnostics
        "console",
        "tracing-start",
        "tracing-stop",
        "video-stop",
        "video-chapter",
        "video-show-actions",
        "video-hide-actions",
        "show",
        "pause-at",
        "resume",
        "step-over",
        # Session management. The listing only; `close-all` / `kill-all`
        # are absent -- they are the widest-blast-radius verbs the CLI has.
        "list",
    }
)

# Auto-approvable ONLY in their bare form, because a positional argument turns
# them into an arbitrary-local-path WRITE. Bare, both write inside the output
# dir that ``browser_cli.snapshots`` points the CLI at.
_BROWSER_CLI_BARE_ONLY_VERBS = frozenset({"video-start"})

# Deliberately absent from every set above, so they keep interactive approval:
#   eval / run-code      — run attacker-authored code in an authenticated page;
#                          with fetch() that is a complete exfiltration path.
#   upload               — sends an arbitrary LOCAL file to the current page.
#   state-load           — reads an arbitrary local path and injects the cookies
#                          it finds into the live session.
#   install / install-browser — mutate the machine; installation is the
#                          dashboard's job (Settings > Browser), not the agent's.
#   cookie-list / cookie-get, localstorage-list / -get,
#   sessionstorage-list / -get  — RETURN the session credential itself. These
#                          were auto-approved in a first version on the reasoning
#                          that their effect stays "inside the page"; that
#                          conflates blast radius with sensitivity. The effect of
#                          a read is the VALUE it prints into the agent's
#                          context, and for these verbs that value is the login.
#   requests / network   — print the URL of every request. A URL can BE the
#                          credential: a presigned S3 URL or a magic-link carries
#                          the secret in the path or query string, so listing
#                          URLs prints a credential into context the same way
#                          cookie-list does.
#   request / request-headers / request-body, response-headers / response-body
#                        — print a request's headers verbatim, i.e. its
#                          Authorization and Cookie values.
#   close / tab-close / close-all / kill-all
#                        — `attach` points the CLI at the operator's OWN browser,
#                          holding their live logins and their open tabs, so these
#                          take that window (or every session at once) down and
#                          unsaved work with it. Nothing recovers it. The agent
#                          prompt already says never to close an attached browser,
#                          but prose the model is asked to honor is advice, not a
#                          control, and the gate cannot see whether a session is
#                          attached or CLI-owned — so it fails closed. `detach`
#                          stays approved: it releases the session and leaves the
#                          window alone, which is what cleanup actually needs.
#   config-print         — prints the session's launch configuration, and the
#                          documented way to constrain this browser is a proxy
#                          set through `launchOptions.proxy.server`, whose value
#                          carries the proxy credential. So the verb that reads
#                          "harmless settings dump" prints a secret on exactly
#                          the setup this design recommends.
#   state-save           — serialises the WHOLE storage state, cookies included,
#                          to a file the agent can then read with its own file
#                          tools. Bare-form no longer helps: the file is the
#                          credential.
#   delete-data          — with `attach`, the CLI operates the operator's REAL
#                          logged-in browser. `delete-data` destroys session
#                          state (cookies, storage, cache) nothing recovers —
#                          equivalent to the user clicking "Clear all site data"
#                          on every origin the browser knows.
#   cookie-set / cookie-delete / cookie-clear,
#   localstorage-set / localstorage-delete / localstorage-clear,
#   sessionstorage-set / sessionstorage-delete / sessionstorage-clear
#                        — `attach` means the operator's REAL browser. The -set
#                          verbs are session fixation (inject a controlled
#                          credential the attacker can reuse); the -delete and
#                          -clear verbs destroy operator login state nothing
#                          recovers. Both directions reach outside "inside the
#                          page" once the page IS the operator's live session.
#   route / unroute / network-state-set
#                        — a route intercepts requests and returns forged
#                          responses. The agent reads the page (via `snapshot`),
#                          so a route lets an injected agent control what the
#                          NEXT read returns — fabricating confirmation of an
#                          action that never happened or hiding an error the
#                          operator should see. `unroute` removes a route the
#                          operator set intentionally. `network-state-set`
#                          toggles offline mode, severing the page from its
#                          server — a denial-of-service on the operator's
#                          browsing.
# A prompt-injected agent must not be able to convert "browsing is allowed"
# into arbitrary code execution, arbitrary local reads, or arbitrary writes.
# Auto-approval must never become a local-machine primitive: the effect of a
# read is the VALUE it prints into the agent's context, and that value
# determines whether the verb is safe — not whether the verb's blast radius
# stays "inside the page".


# Flags that carry no local-filesystem path and no code. An ALLOWLIST for the
# same fail-closed reason as the verb list. It is load-bearing rather than
# cosmetic: the CLI takes an output path as `--filename=<name>`, so a path can
# arrive as a FLAG and not only as a positional argument. Skipping unrecognized
# flags on the way to the verb would therefore auto-approve an arbitrary local
# WRITE. Anything not listed here falls through to interactive approval --
# notably:
#   --filename  MEASURED: the value is resolved against the CLI invocation's
#               CWD, *not* against PLAYWRIGHT_MCP_OUTPUT_DIR (that variable only
#               governs auto-generated names). So even a bare
#               `--filename=README.md` overwrites a file in the user's repo, and
#               there is no "safe" spelling of it to allow. The un-named form
#               (`playwright-cli screenshot`) IS auto-approved and writes into
#               the service's own directory, printing the path -- so the capture
#               loop keeps working without this flag.
#   --profile / --config  name a local path to READ.
_BROWSER_CLI_SAFE_FLAGS = frozenset(
    {
        # MEASURED against the installed CLI: `-s=`, `--s=` and `--session=`
        # are all accepted and all name the same session. Only `-s` was listed,
        # so the named-session form this repo's own prompt.md tells the agent to
        # use (`--s=chrome`) fell through to interactive approval on EVERY
        # command after `attach` -- the documented primary workflow.
        "-s",
        "--s",
        "--session",
        "--json",
        "--raw",
        "--help",
        "--version",
        "--headed",
        "--browser",
        "--persistent",
        "--extension",
        "--cdp",
        "--endpoint",
        "--domain",
        "--hide",
        # Shape-only capture options: they change the image, not its location.
        "--type",
        "--full-page",
        "--hires",
    }
)


_BROWSER_CLI_SESSION_FLAGS = frozenset({"-s", "--s", "--session"})
# A leading URI scheme, e.g. `https:`, `file:`, `view-source:`.
_URI_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")


# Names that reach the local machine without naming an address. Refusing
# loopback ADDRESSES is not sufficient on its own: `localhost` and anything under
# the reserved `.localhost` suffix (RFC 6761) resolve to loopback by name.
_LOOPBACK_HOST_NAMES = frozenset({"localhost"})

# The longest a DNS name may be, per RFC 1035.
_MAX_DNS_NAME_LEN = 253

# A host this gate is willing to treat as a DNS name. The decisive rule is that
# the FINAL label starts with an ASCII letter, which is what separates a real
# name from an address a browser will decode: every alternate spelling of an IPv4
# address ends in a numeric or ``0x`` label -- decimal (`2852039166`), hex
# (`0xa9fea9fe`), octal-dotted (`0251.0376.0251.0376`), the dotted mixtures, and
# short forms like `127.1`. One rule refuses them all without enumerating the
# encodings. A host that matches nothing here (a percent-encoded authority, a
# non-ASCII IDN label) is not classified and so falls through to interactive
# approval, which is the correct direction to fail.
_DNS_HOST_RE = re.compile(r"(?:[^\s./\\:@\[\]]+\.)*[^\W\d_][^\s./\\:@\[\]]*", re.ASCII)


def _is_remote_navigable_host(host: str) -> bool:
    """Whether http(s) navigation to *host* may be auto-approved.

    Refused, so that navigation there costs one interactive approval:

    * **Every local control plane.** Kiro Crew's own dashboard is served over
      loopback, and the approval mode, trust settings and YOLO switch all live on
      it -- so an auto-approved ``goto`` plus auto-approved clicks would let the
      agent widen its own ceiling with no human in the loop. The rule is the
      whole loopback range and the loopback names rather than one port number:
      the dashboard's port is only known at runtime (a pod gets its own), and
      "never auto-drive a local admin UI" covers the dashboard, pods, and
      anything else the operator happens to run on the host under one rule.
      ``0.0.0.0`` / ``::`` are refused with it, because a listener bound to the
      unspecified address is reachable that way too.
    * **Link-local**, where cloud instance metadata and its credentials live: a
      ``goto`` followed by an auto-approved ``snapshot`` would print an instance
      role's credentials into the agent's context.
    * **Private** (RFC 1918: 10/8, 172.16/12, 192.168/16), **CGNAT/shared**
      (100.64/10), and **all other non-globally-routable addresses** including
      multicast, reserved/future-use, documentation, and benchmarking ranges.
      A ``goto http://10.0.0.5/admin`` followed by an auto-approved ``snapshot``
      prints internal infrastructure responses into the agent's context -- the
      same SSRF vector as link-local, aimed at internal services rather than
      the metadata endpoint.

    Ranges are tested by ``ipaddress``' own ``is_global`` property (True only
    for globally-routable addresses), applied to both the address itself and
    any embedded IPv4 (``ipv4_mapped``, ``sixtofour``). ``is_global`` subsumes
    loopback, link-local, unspecified, private, CGNAT/shared, multicast,
    reserved, documentation, and benchmarking ranges in one predicate, without
    hand-rolled CIDRs.

    DNS names are NOT resolved. Resolving inside the approval predicate is a
    blocking network call on the hot path AND a DNS-rebinding TOCTOU: a name can
    answer a public address at approval time then resolve to a private one when
    the browser re-resolves milliseconds later. The residual risk (a public name
    pointing at a private address) is accepted; browser-side network policy is
    the correct mitigation layer for that class.

    Ordinary public http(s) browsing is unaffected and stays auto-approved.
    """
    lowered = host.lower().rstrip(".")
    if not lowered:
        return False
    if lowered in _LOOPBACK_HOST_NAMES or lowered.endswith(".localhost"):
        return False
    try:
        addr: Any = ipaddress.ip_address(lowered)
    except ValueError:
        # Not an address literal, so it can only be a name -- and only when it
        # actually looks like one. See `_DNS_HOST_RE`.
        if len(lowered) > _MAX_DNS_NAME_LEN:
            return False
        return _DNS_HOST_RE.fullmatch(lowered) is not None
    candidates = [addr]
    for embedding in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(addr, embedding, None)
        if embedded is not None:
            candidates.append(embedded)
    return all(c.is_global for c in candidates)


def _is_safe_browser_cli_argument(arg: str) -> bool:
    """Whether a page verb's positional argument is safe to auto-approve.

    A positional that is not URI-shaped is ordinary page input -- an element ref,
    a key name, literal text -- and passes. A URI-shaped one passes only as plain
    http(s) to a host :func:`_is_remote_navigable_host` accepts; every other
    shape falls through to interactive approval, because a non-http scheme is not
    "a page action" at all:

    * ``file:`` reads local disk into the page, and the next ``snapshot`` prints
      it into the agent's context -- an arbitrary local file read.
    * ``data:`` and ``javascript:`` inject script into the page.
    * ``view-source:`` does both.

    So the rule matches the one used for flags: recognized shape or refuse.
    """
    m = _URI_SCHEME_RE.match(arg)
    if m is None:
        return True  # not URI-shaped: an element ref, a key name, literal text
    if m.group(1).lower() not in ("http", "https"):
        return False
    # Refuse before parsing anything a browser and `urlsplit` read DIFFERENTLY.
    # `urlsplit` follows RFC 3986; a browser follows the WHATWG URL spec, and
    # where they disagree the browser's answer is the one that gets navigated:
    #
    #   * a backslash is a path separator in a special scheme, so
    #     `http://<target>\@innocuous/` ends its authority at the backslash and
    #     navigates to <target> -- while `urlsplit` reads everything before the
    #     last `@` as userinfo and reports `innocuous` as the host, which is the
    #     value this guard would have checked.
    #   * tab, CR and LF are STRIPPED from a URL before parsing, so they can be
    #     inserted mid-host to break up a literal the guard would recognize.
    #
    # There is no safe spelling of either inside an http(s) URL a page actually
    # needs, so an argument carrying one costs an approval prompt rather than
    # being reconciled between two parsers.
    if "\\" in arg or any(ch in arg for ch in ("\t", "\n", "\r")):
        return False
    try:
        host = urllib.parse.urlsplit(arg).hostname
    except ValueError:
        return False  # unparseable authority -- cannot reason about it
    if not host:
        return False
    return _is_remote_navigable_host(host)


# A plain session label: no separators, no traversal, no leading dash.
_SESSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _unquoted_shell_hazard(text: str) -> str | None:
    """Name the first shell construct in *text* the CLI never sees, or ``None``.

    Load-bearing for approval, not cosmetic: every construct here is performed by
    the SHELL before the command runs, so the verb and flag allowlists cannot see
    it. They inspect the tokens they are handed; the shell decides what those
    tokens become.

    * **Redirection.** An otherwise-approved ``playwright-cli snapshot`` with
      ``> somefile`` appended CREATES OR TRUNCATES that file. The segment splitter
      does not cut on ``>``, so ``>`` and the path arrive as ordinary positionals
      and the whole thing reads as "snapshot with two extra arguments".
    * **Expansion.** ``open "${PATH:+file:///etc/passwd}"`` is not URI-shaped when
      the guard sees it, so it passes as ordinary page input — and the shell then
      expands it into a ``file://`` URL, making the next ``snapshot`` an arbitrary
      local file read. ``$VAR`` and backticks are the same mechanism, and so is
      brace expansion: ``{file:///etc/passwd,}`` expands to that URL with no
      variable and no substitution involved. A leading ``~`` expands to a home
      directory the same way.

    Globbing (``*``, ``?``, ``[]``) is deliberately NOT treated as a hazard, and
    the asymmetry is the point: brace and tilde expansion ALWAYS rewrite the
    token, while an unmatched glob is left literal by the shell — and refusing
    ``?`` would deny every URL carrying a query string, which is most of them.
    A glob that does match names a local file, and no auto-approved verb takes a
    local path as a positional.

    One quote-aware walker serves all of it, because a second shell parser is how
    a bypass gets introduced. Quote rules are the shell's own: single quotes make
    everything literal, so ``type 'price is $5'`` and ``click "div > span"`` are
    legitimate arguments and stay approved; a backslash escapes the next
    character everywhere except inside single quotes.
    """
    quote: str | None = None
    escaped = False
    at_word_start = True
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            continue
        # Double quotes suppress word splitting and globbing but NOT parameter or
        # command substitution, so `$` and a backtick stay dangerous inside them.
        if ch in ("$", "`"):
            return "expansion"
        if ch in ("{", "}"):
            return "brace-expansion"
        if quote == '"':
            if ch == '"':
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch in "><":
            return "redirection"
        if ch == "~" and at_word_start:
            return "tilde-expansion"
        at_word_start = ch.isspace()
    return None


def _is_browser_cli_command(tool_title: str) -> bool:
    """True when EVERY segment of a shell command is an auto-approvable
    ``playwright-cli`` page-scoped verb.

    Matched against the REAL command recovered from ``tool_input`` — never the
    model-authored title, which an injected agent controls and could forge.
    Reuses :func:`_split_command_segments`, so command substitution and quoted
    separators are handled by the one hardened splitter.
    """
    split = _split_command_segments(tool_title)
    if split is None:
        return False
    _, segments = split
    if not segments:
        return False
    for seg in segments:
        # BEFORE tokenizing: redirection and expansion are the shell's work, not
        # the CLI's, so no amount of verb checking can make them safe.
        if _unquoted_shell_hazard(seg) is not None:
            return False
        try:
            tokens = shlex.split(seg)
        except ValueError:
            return False  # unbalanced quotes — cannot reason about it
        if len(tokens) < 2 or tokens[0] != _BROWSER_CLI_BIN:
            return False
        # Separate flags from positionals. Every flag must be recognized as
        # path-free and code-free; an UNKNOWN flag denies the whole command
        # rather than being skipped over on the way to the verb.
        positionals: list[str] = []
        for tok in tokens[1:]:
            if tok.startswith("-"):
                name, _, value = tok.partition("=")
                if name not in _BROWSER_CLI_SAFE_FLAGS:
                    return False
                # A session name becomes a directory under the CLI's own data
                # dir, so a traversal-shaped value writes outside it. Restrict it
                # to a plain label. This also closes the same hole on the
                # pre-existing `-s`, which never validated its value.
                if name in _BROWSER_CLI_SESSION_FLAGS and not _SESSION_NAME_RE.fullmatch(value):
                    return False
            else:
                positionals.append(tok)
        if not positionals:
            return False
        verb = positionals[0]
        if verb in _BROWSER_CLI_PAGE_VERBS:
            # Positionals carry the navigation target, so they are validated
            # like flags are -- see `_is_safe_browser_cli_argument`.
            if not all(_is_safe_browser_cli_argument(a) for a in positionals[1:]):
                return False
            continue
        # Bare only: MEASURED, an output name is resolved against the CLI's CWD,
        # so any argument here is an arbitrary local write. Bare, both write into
        # the service's own directory.
        if verb in _BROWSER_CLI_BARE_ONLY_VERBS and len(positionals) == 1:
            continue
        return False
    return True


def _extract_base_command(tool_title: str) -> str:
    """Extract base binary name(s) for glob pattern generation.

    Handles piped/chained commands split by |, &&, ;
    "Running: ls /tmp" -> "ls"
    "Running: cat /etc/hosts | wc -l" -> "cat,wc"
    "Running: grep -r foo . && echo done" -> "grep,echo"
    "Running: grep -E 'foo|bar' file.txt" -> "grep"
    "SomeMcpTool" -> "SomeMcpTool"

    Delegates to :func:`_split_command_segments` with
    ``_CMD_GRANT_SPLIT_RE`` — the same shared splitter (quote masking,
    redirect masking, substitution denial) but a narrower operator set that
    excludes bare ``&`` and ``\\n``.  Those operators are correct for the
    deny path (enforcement) where over-splitting fails closed, but wrong
    for the grant path (Trust dropdown) where each extra segment becomes
    one more binary offered for auto-approval.

    When the command contains substitution, returns only the first token —
    the enforcement path independently denies substitution commands.
    """
    split = _split_command_segments(tool_title, split_re=_CMD_GRANT_SPLIT_RE, mask_escaped=True)
    if split is None:
        # Command substitution — can't safely extract bases.  Return only
        # the first token so the Trust dropdown doesn't offer junk patterns.
        normalized = _normalize_tool_name(tool_title)
        parts = normalized.strip().split(None, 1)
        return parts[0] if parts else normalized
    normalized, segments = split
    bases = []
    for seg in segments:
        parts = seg.strip().split(None, 1)
        if parts:
            bases.append(parts[0])
    return ",".join(dict.fromkeys(bases)) if bases else normalized


def _extract_full_command(tool_title: str) -> str:
    """Extract the full normalized command (strip display prefix)."""
    return _normalize_tool_name(tool_title)


def _resolve_channel_target(state: Any, session_key: str, link: Any) -> Any:
    """Resolve ``(link, transport)`` through the cross-surface send ladder.

    This is the shared capability/governance seam for both actual mirror
    delivery and the dashboard's read-only ``links[].live`` projection.  It
    intentionally skips Slack, whose dedicated client and streaming path are
    not registered in ``channel_transports``.
    """
    if link is None or link.channel_type == SLACK_NAMESPACE or not link.channel_id:
        return None
    try:
        from kiro_crew.platform.context import PlatformCompositionError
        from kiro_crew.platform.governance_profiles import vet_and_audit

        # vet_and_audit == governance_permits + a SEL governance-decision record
        # for BOTH grant and denial. Every call here is a real send/link
        # decision (the read-only links[].live projection uses the in-memory
        # state._channel_link_is_live instead), so a governance decision at this
        # egress chokepoint MUST land in the SEL trail — the security contract
        # requires every permission decision to be audited.
        decision = vet_and_audit(
            "channels",
            link.channel_type,
            session_key=session_key,
            tool_name="chat.channel_mirror",
            # fail_closed=True: this is an EGRESS chokepoint on a network
            # surface, so a degraded governance evaluation must DENY rather than
            # degrade-to-permit. vet_and_audit forwards this to
            # governance_permits, which swallows its own internal errors and
            # returns a non-permissive Decision under fail_closed. Matches the
            # other "channels"-scope gates: messaging/identity.py,
            # slack/gateway.py, dashboard/handlers_system.py.
            fail_closed=True,
        )
        # Default False, not True: a Decision without ``permitted`` is an
        # unusable answer from a gate, and must not read as permission.
        if not getattr(decision, "permitted", False):
            logger.info(
                "cross-surface: outbound to %s denied by governance policy; " "skipping mirror",
                link.channel_type,
            )
            return None
    except PlatformCompositionError:
        # A composition error means the governance ceiling itself is invalid.
        # governance_permits deliberately re-raises it rather than degrading;
        # swallowing it here would defeat that contract and let a broken
        # ceiling read as an ordinary skip.
        raise
    except Exception:
        logger.debug(
            "cross-surface: governance check failed for %s; skipping mirror " "(fail-closed)",
            link.channel_type,
            exc_info=True,
        )
        return None
    transport = state.get_channel_transport(link.channel_type)
    if transport is None or not transport.capabilities.supports_proactive_send:
        logger.debug(
            "cross-surface: skip mirror to %s (transport=%s, proactive=%s)",
            link.channel_type,
            transport is not None,
            getattr(
                getattr(transport, "capabilities", None),
                "supports_proactive_send",
                None,
            ),
        )
        return None
    return link, transport


def _resolve_mirror_target(state: Any, session_key: str) -> Any:
    """Resolve a session's outbound mirror through the shared send ladder."""
    return _resolve_channel_target(
        state,
        session_key,
        state.sessions.get_mirror_link(session_key),
    )


def _mark_kiro_signed_out(state: Any) -> None:
    """Latch the prerequisite service to signed-out after an ACP auth failure.

    Readiness is probed at boot and on explicit user action only, so the ACP
    attempt is what discovers a mid-session logout. Feeding that back into the
    service is what keeps the still-fail-closed gates — the poll-driven kiro-cli
    spawn sites and the destructive reruns — from acting on a stale ready latch,
    with no timer re-probe. Best-effort: never disrupt the turn's teardown.
    """

    service = getattr(state, "kiro_prerequisite_service", None)
    if service is None:
        return
    try:
        service.mark_signed_out()
    except Exception:
        logger.debug("Could not latch Kiro signed-out state", exc_info=True)


async def _deliver_auth_error_to_slack(
    state: Any,
    slot: Any,
    sessions: Any,
    session_key: str,
    message: str,
) -> None:
    """Mirror an auth-required error to a linked Slack thread.

    A user driving the linked session from Slack must not be left without a
    response when the CLI is signed out, so the auth-required error is delivered
    to the linked thread.
    """

    slack_client = getattr(state, "slack_client", None)
    if slack_client is None:
        return
    # A disconnected thread is muted for turn output, and an auth failure IS turn
    # output. The dashboard renders the same error, which is where a user who just
    # disconnected the thread is working.
    if slack_mirror_is_paused(state, session_key):
        return
    thread_ts = getattr(slot, "_slack_thread_ts", "")
    channel_id = getattr(slot, "_slack_channel", "")
    if (not thread_ts or not channel_id) and sessions is not None:
        thread_ts, channel_id = sessions.get_slack_link(session_key)
    if not (thread_ts and channel_id):
        return
    try:
        await slack_client.post_message(channel_id, message, thread_ts)
    except Exception:
        logger.debug(
            "Failed to deliver Kiro auth error to linked Slack thread",
            exc_info=True,
        )


async def _deliver_cross_surface_reply(state: Any, session_key: str, assistant_text: str) -> None:
    """Deliver a completed dashboard reply to a linked NON-Slack channel.

    The channel-neutral leg of cross-surface sync: reads the session's outbound
    mirror link, resolves the registered ``MessagingTransport`` for that channel
    and pushes the reply via ``send_message`` — capability-gated on
    ``supports_proactive_send``. Slack keeps its dedicated rich streaming mirror
    inline in the turn loop, so it is skipped here. Silent no-op when the session
    has no non-Slack mirror, the transport is not registered, or the channel
    cannot send proactively (e.g. WeCom, whose replies are bound to an inbound
    token). Best-effort: a delivery failure never disrupts the dashboard turn.
    """
    if not assistant_text:
        return
    # Disconnected: the binding is retained so a reply there still resolves here,
    # but turn output stops. Asked before resolving so a muted channel costs no
    # transport lookup.
    if mirror_is_paused(state, session_key):
        return
    target = _resolve_mirror_target(state, session_key)
    if target is None:
        return
    link, transport = target
    # Redact through the canonical egress shim so a loaded companion's extra
    # credential/token regexes apply (not just the OSS baseline).
    text = redact_via_context(assistant_text)
    # Split on the channel's max message length so a long reply mirrors in full
    # rather than being hard-truncated by the transport (Telegram caps at 4096),
    # matching the Slack leg's split_message chunking.
    parts = chunk_text(text, transport.capabilities.max_message_chars)
    try:
        for part in parts:
            await transport.send_message(link.channel_id, part, thread_id=link.thread_id)
        logger.info(
            "cross-surface: mirrored reply to %s:%s (%d chars, %d part(s))",
            link.channel_type,
            link.channel_id,
            len(text),
            len(parts),
        )
    except Exception:
        logger.debug("Failed to mirror reply to %s", link.channel_type, exc_info=True)


async def _deliver_cross_surface_user_message(
    state: Any, session_key: str, user_message: str
) -> None:
    """Mirror the user's dashboard message to a linked NON-Slack channel.

    The user-message half of the channel-neutral cross-surface leg: before the
    turn's reply is delivered, push what the user typed in the dashboard to the
    linked channel so the remote conversation reads coherently (question then
    reply), matching Slack's ``💬 _msg_`` echo. Capability-gated and best-effort,
    mirroring ``_deliver_cross_surface_reply``. Slack is handled by its dedicated
    streaming mirror; the caller guards out slash commands and recovery turns.
    """
    if not user_message:
        return
    # Same gate as the reply leg: these are the two sites that carry turn output,
    # and a disconnect silences both or the remote conversation reads as a
    # question with no answer.
    if mirror_is_paused(state, session_key):
        return
    target = _resolve_mirror_target(state, session_key)
    if target is None:
        return
    link, transport = target
    try:
        await transport.send_message(
            link.channel_id,
            f"💬 {_prepare_mirror_msg(user_message)}",
            thread_id=link.thread_id,
        )
        logger.info(
            "cross-surface: mirrored user message to %s:%s",
            link.channel_type,
            link.channel_id,
        )
    except Exception:
        logger.debug("Failed to mirror user message to %s", link.channel_type, exc_info=True)


def _prepare_mirror_msg(raw_user_message: str) -> str:
    """Prepare a user message for the cross-surface / Slack mirror echo.

    Truncates first, then redacts through the canonical ``redact_via_context``
    egress shim so a loaded companion's extra credential/token regexes apply.
    The shim's standalone fallback is the OSS baseline ``security.redact``, so a
    standalone host keeps the previous redaction behaviour.
    """
    return redact_via_context((raw_user_message or "")[:500])


def _flush_segment(
    state: DashboardState,
    slot: _ChatSlot,
    assistant_text: str,
    *,
    broadcast: bool = True,
    quiet_persist: bool = False,
) -> None:
    """Finalize current text block as a segment and persist it.

    ``quiet_persist`` additionally suppresses the per-message ``chat_message``
    broadcast that ``slot.append`` emits for the finalized assistant message.
    Used ONLY by the mid-turn steer cut: at that boundary every client has
    already finalized its streaming message (optimistic freeze on the
    initiating tab, steer_push freeze on the others), so a broadcast here
    would render a DUPLICATE copy of the pre-steer text below the steer
    bubble. Normal end-of-segment flushes keep the broadcast — there the
    clients still hold a live streaming message for it to reconcile into.
    """

    # Remove trailing chunk messages (they belong to this segment).
    # Also pull aside any stop_event interleaved with this segment's chunks
    # so it lands AFTER the finalized assistant message. Historical
    # stop_events from prior turns stay in place.
    def _is_stop_event(m: dict) -> bool:
        cls_val = m.get("cls", "")
        if not cls_val or not isinstance(cls_val, str):
            return False
        try:
            parsed = json.loads(cls_val)
            return isinstance(parsed, dict) and parsed.get("kind") == "stop_event"
        except (json.JSONDecodeError, ValueError):
            return False

    # Walk backwards to find the start of the trailing chunk/stop_event run.
    boundary = len(slot.messages)
    for i in range(len(slot.messages) - 1, -1, -1):
        role = slot.messages[i].get("role", "")
        if role == "chunk" or _is_stop_event(slot.messages[i]):
            boundary = i
        else:
            break
    head = slot.messages[:boundary]
    tail = slot.messages[boundary:]
    trailing_stop_events = [m for m in tail if _is_stop_event(m)]
    slot.messages = (
        head  # drops chunks AND trailing stop_events; tail.non-chunk-non-stop stays in head
    )
    # The window rewrite above is only half the release: append put each chunk
    # row in `_pending` as well, as the SAME dict, so the queue still owns every
    # token of this segment. This is the SUCCESS path — the one a long streamed
    # turn normally takes — so skipping it leaks the whole stream on any slot
    # that is not asked for another turn.
    slot.release_pending_chunks()
    # Redact the accumulated text
    redacted, exfil_warnings = redact_exfiltration_urls(assistant_text)
    for w in exfil_warnings:
        logger.warning("Exfiltration URL redacted in chat segment: %s", w)
    redacted, cred_warnings = redact_credentials(redacted)
    for w in cred_warnings:
        logger.warning("Credential redacted in chat segment: %s", w)
    # Persist as assistant message. Broadcast is kept enabled so that
    # other tabs viewing the same slot receive the finalized text.
    # The active tab already has this content from streaming chunks;
    # the chat_segment event tells it to finalize streaming → assistant.
    slot.append("assistant", redacted, "msg msg-a", broadcast=not quiet_persist)
    last_msg: dict = slot.messages[-1]
    # If a regenerate is pending, attach the stashed variants to this fresh assistant message.
    if slot._pending_variants:
        pending_list = [
            {
                **v,
                "content": redact_credentials(redact_exfiltration_urls(v.get("content", ""))[0])[0],
            }
            for v in slot._pending_variants
            if isinstance(v, dict)
        ]
        pending_list.append({"content": redacted, "ts": last_msg.get("ts", "")})
        last_msg["variants"] = pending_list
        last_msg["variant_idx"] = len(pending_list) - 1
        slot._pending_variants = []
    # Re-append any stop_event that belongs to this segment's trailing run,
    # placed AFTER the finalized assistant message so the UI shows
    # prose → stop card.
    for ev in trailing_stop_events:
        slot.messages.append(ev)
    # Tell the frontend to finalize streaming → assistant.
    if broadcast:
        state.broadcast_ws("chat_segment", {"slot": slot.key})
        # Notify other tabs about variant metadata so they don't need a full refresh.
        # Use last_msg (the assistant message) not slot.messages[-1] which may be a
        # trailing stop_event appended after the assistant message.
        if last_msg.get("variants"):
            state.broadcast_ws(
                "chat_variant_switch",
                {"slot": slot.key, "index": last_msg.get("variant_idx", 0), "content": redacted},
            )
    # Auto-register any <mcwidget> in this segment as an (unpinned) artifact so
    # it appears in the session's Artifacts tab and the star becomes a pure
    # metadata flip. Registered from the REDACTED text — the artifact is a
    # dashboard-surfaced copy of the widget, so it must not persist a credential
    # the segment redaction just stripped out of chat.
    _schedule_widget_registration(state, slot, redacted, str(last_msg.get("ts", "")))


def _schedule_widget_registration(
    state: DashboardState,
    slot: _ChatSlot,
    text: str,
    message_ts: str,
) -> None:
    """Fire-and-forget widget auto-registration for a finalized segment.

    Detached deliberately: registration touches the artifact store (blocking
    filesystem work, offloaded to an executor inside
    ``register_widgets_off_loop``), and a widget artifact appearing a beat after
    the message renders is invisible to the user, whereas awaiting it would add
    store latency to every segment flush of every turn. Failures are logged by
    the callee and never surface into the turn.

    Spawned via ``asyncio.create_task`` (not ``loop.create_task``) to match every
    other detached task in this module — tests that neutralize background work
    patch ``chat_runner.asyncio.create_task``, and a task spawned off the loop
    handle directly would slip past that and run real store I/O mid-test.

    The no-running-loop case is guarded: with no loop, registration is skipped
    rather than raising into a segment flush (some callers in this module's
    history are sync, and a CLI/test path has no artifact-store expectations).

    **Restricted sessions never register.** Incognito / temporary slots
    (``slot.is_restricted``) are denied every artifact write at the HTTP gate
    (``_is_restricted_session``), so registering here would be a back door around
    that ceiling: widget HTML from a session the user expected to leave no trace
    would persist to ``artifacts/<slug>/`` and show up in the library. The gate
    keys off the SAME ``slot.is_restricted`` signal, so the two agree by
    construction.
    """
    if not text:
        return
    if getattr(slot, "is_restricted", False):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    # Store the BARE slot key, not "dashboard:<key>". The in-session Artifacts
    # tab queries ?session=<activeSlot>, which is the bare key, and
    # ``ArtifactStore.list`` compares ``session_key`` exactly (no prefix folding,
    # unlike ``_collect_session_docs``). WidgetFrame's fallback create also sends
    # the bare key, so this keeps auto-registered and star-created artifacts in
    # the same bucket — the one the tab can actually see.
    #
    # Two independent registration passes ride the same restricted-session gate
    # and off-loop dispatch: <mcwidget> bodies (inline HTML) and local markdown
    # images (bytes copied off disk). The cheap substring pre-checks keep a
    # plain prose segment from scheduling either task.
    if "<mcwidget" in text:
        task = asyncio.create_task(register_widgets_off_loop(text, message_ts, slot.key))
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    if "![" in text:
        image_task = asyncio.create_task(register_images_off_loop(text, message_ts, slot.key))
        state._background_tasks.add(image_task)
        image_task.add_done_callback(state._background_tasks.discard)


def _expand_prompt_mention(
    message: str,
    state: DashboardState,
    slot: _ChatSlot,
) -> tuple[str, str]:
    """Expand ``@prompt-name rest`` into SOP content + user instructions.

    Returns ``(expanded_message, "ok")`` if a prompt was resolved,
    ``(original_message, "blocked")`` if blocked by sensitive-path check,
    ``(original_message, "too_large")`` if file exceeds size limit, or
    ``(original_message, "not_found")`` if no match.
    """
    if not message.startswith("@"):
        return message, "not_found"

    # Parse @name from start of message — name ends at first whitespace or EOL
    body = message[1:]  # strip leading @
    parts = body.split(None, 1)
    mention = parts[0] if parts else body
    user_text = parts[1].strip() if len(parts) > 1 else ""

    try:
        match = _find_prompt(mention)
    except Exception:
        return message, "not_found"
    if not match:
        return message, "not_found"

    if is_sensitive_path(match["path"]):
        return message, "blocked"

    try:
        raw = Path(match["path"]).read_bytes()
    except OSError:
        return message, "not_found"
    if len(raw) > MAX_PROMPT_BYTES:
        logger.warning(
            "Prompt %s exceeds max size (%d > %d bytes)",
            mention,
            len(raw),
            MAX_PROMPT_BYTES,
        )
        return message, "too_large"
    content = raw.decode("utf-8", errors="replace")

    content, _ = redact_credentials(content)
    content, _ = redact_exfiltration_urls(content)

    # Inject SOP as instructions the agent must follow
    expanded = f"Execute the following instructions:\n\n{content}"
    if user_text:
        expanded += f"\n\n---\nAdditional context from user: {user_text}"

    # Show the user what happened
    slot.append(
        "system",
        f"📜 Loaded prompt **@{match['fullName']}** ({len(content):,} chars)",
        "msg msg-info",
    )
    state.push_slots_update()

    return expanded, "ok"


def _expand_dollar_skills(
    message: str,
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
) -> tuple[str, int]:
    """Expand ``$skillname`` tokens anywhere in *message* into appended skill bodies.

    Leaves the literal ``$token`` in place (decision (a)) and appends a
    ``[Skill: name]`` block per resolved skill after the user's message, so the
    agent sees both the user's intent marker and the loaded procedure. Unknown
    tokens are left untouched.

    Resolution + security live in ``SkillsLoader.resolve_dollar_skills`` (allowlist
    match, no path construction — per input-validation guidance). This function adds
    the runner-side concerns: redaction of the loaded content, a user-visible chip,
    and SEL audit.

    Returns ``(expanded_message, count)`` where *count* is the number of skills
    appended (0 if none resolved).
    """
    if "$" not in message:
        return message, 0
    skills = _get_skills(state)
    try:
        resolved = skills.resolve_dollar_skills(message)
    except Exception:
        logger.exception("dollar-skill resolution failed")
        # Audit the failed resolution attempt — the security-controls guideline
        # requires every tool invocation/permission decision to emit a SEL event,
        # including failures (mirrors the prompt-expansion not_found/error path).
        sel().log_tool_invocation(
            session_key=session_key,
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name="skill_dollar_expansion",
            tool_kind="prompt",
            outcome="error",
            metadata={"reason": "exception", "slot": slot.key},
        )
        return message, 0
    if not resolved:
        if skills.has_dollar_candidate(message):
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name="skill_dollar_expansion",
                tool_kind="prompt",
                outcome="not_found",
                metadata={"slot": slot.key},
            )
        return message, 0

    blocks: list[str] = []
    names: list[str] = []
    for _token, name, body in resolved:
        body, _ = redact_credentials(body)
        body, _ = redact_exfiltration_urls(body)
        blocks.append(f"[Skill: {name}]\n\n{body}")
        names.append(name)

    expanded = message + "\n\n" + "\n\n---\n\n".join(blocks)

    slot.append(
        "system",
        f"📎 Loaded skill(s) via `$`: **{', '.join(names)}**",
        "msg msg-info",
    )
    state.push_slots_update()
    return expanded, len(names)


def _should_suppress_requeue(slot) -> bool:
    """Return True if a stop is active and re-queue should be suppressed."""
    if slot._stop_state != "idle":
        logger.info("Suppressing re-queue — stop in progress (state=%s)", slot._stop_state)
        return True
    return False


async def _consume_pending_reset(state: DashboardState, slot: _ChatSlot) -> None:
    """Reset the session for a deferred project change, if one is queued.

    Called both before get_or_create (idle picker change) and at turn end
    (mid-turn set_project). Clears the flag only after a successful reset, and
    compare-and-clears so a key queued by a concurrent api_chat_slot_project
    during the await isn't clobbered.
    """
    if not slot._pending_reset_history_key:
        return
    pending_key = slot._pending_reset_history_key
    try:
        await state.sessions.reset(pending_key)
        if slot._pending_reset_history_key == pending_key:
            slot._pending_reset_history_key = None
    except Exception:
        logger.warning(
            "Failed to consume pending project-change reset for slot %s",
            slot.key,
            exc_info=True,
        )


# Debounce before a speculative spawn. Absorbs rapid consecutive signals
# (slot create immediately followed by a project set, or a user re-picking
# the project) so only the settled state spawns a session.
_EAGER_SPAWN_DEBOUNCE_SECS = 1.5

# Global cap on concurrent speculative spawns. Bounds the process/RSS burst
# when many slots fire signals at once (bulk restore, slot surfing); a spawn
# that cannot get a permit simply skips — the first message cold-starts as
# it does today, so the cap only ever degrades back to current behavior.
_EAGER_SPAWN_MAX_CONCURRENT = 2
_eager_spawn_sem = asyncio.Semaphore(_EAGER_SPAWN_MAX_CONCURRENT)

# How long a speculatively RESUMED session may sit unclaimed before it is
# torn down. A resumed session holds kiro-cli's native per-session lock, so
# a prefetch the user walked away from must release it cleanly rather than
# wait out the 30-minute idle sweep. Fresh (non-resumed) eager sessions keep
# the idle-sweep-only behavior — they hold no prior transcript's lock.
_RESUME_PREFETCH_TTL_SECS = 600.0

# Population cap on live-but-unclaimed prefetched sessions. The spawn
# semaphore bounds concurrent SPAWNS, not accumulated LIVE processes: after a
# gateway restart restores many resumable tabs, flipping through them could
# stack one full kiro-cli process (RSS + native session lock) per dwelled tab
# for the whole TTL. Arming a new prefetch beyond the cap evicts the OLDEST
# unclaimed one via the conditional remove_if_unclaimed — a claimed session is
# never touched, it just falls out of the accounting.
_RESUME_PREFETCH_MAX_LIVE = 3
# Insertion-ordered arm registry (loop-owned, like all chat_runner state):
# session_key -> None. Entries leave on TTL fire, on eviction, or lazily when
# an eviction attempt finds the session already claimed/gone.
_armed_prefetches: "dict[str, None]" = {}


async def _cap_armed_prefetches(sessions: Any, new_key: str) -> None:
    """Register *new_key* as armed and evict oldest unclaimed beyond the cap."""
    _armed_prefetches.pop(new_key, None)  # re-arm moves the key to newest
    _armed_prefetches[new_key] = None
    while len(_armed_prefetches) > _RESUME_PREFETCH_MAX_LIVE:
        oldest = next(iter(_armed_prefetches))
        _armed_prefetches.pop(oldest, None)
        try:
            # Shielded for the same reason as the TTL removal: an interrupted
            # removal leaks the process holding the native lock.
            if await asyncio.shield(sessions.remove_if_unclaimed(oldest)):
                logger.info(
                    "Resume prefetch: evicted oldest unclaimed %s (cap %d)",
                    oldest,
                    _RESUME_PREFETCH_MAX_LIVE,
                )
            # False = claimed or already gone — either way it no longer
            # counts against the cap; dropping the registry entry suffices.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Resume prefetch: eviction failed for %s", oldest, exc_info=True)


def schedule_eager_spawn(
    state: "DashboardState", slot: "_ChatSlot", *, allow_resume: bool = False
) -> "asyncio.Task | None":
    """Speculatively create *slot*'s session ahead of its first message.

    Fire-and-forget: called from the slot-create and project-set handlers so
    the multi-second ACP handshake (spawn + session/new, or session/load for
    a resumable slot) overlaps with the user's think-time instead of being
    paid on the first send. No-op unless ``session.eager_spawn`` is enabled.

    At most one pending task per slot: a newer signal cancels the older task,
    so the spawn always reflects the slot's settled agent/model/project.

    ``allow_resume`` opts this spawn into resume prefetch (the slot-focused
    intent signal): a resumable key performs the speculative ``session/load``
    instead of skipping, with the ``resumed=True`` observation armed for the
    first real turn and a TTL teardown if no turn ever claims it. The other
    intent signals keep the refusal — slot create has no mapping, and the
    agent/project switch handlers reset the session themselves.
    """
    try:
        cfg = KiroCrewConfig.load()
        if not cfg.session.eager_spawn:
            return None
    except Exception:
        return None
    prev = getattr(slot, "_eager_spawn_task", None)
    if prev is not None and not prev.done():
        prev.cancel()
    task = asyncio.create_task(_eager_spawn(state, slot, allow_resume=allow_resume))
    slot._eager_spawn_task = task
    return task


async def _eager_spawn(
    state: "DashboardState", slot: "_ChatSlot", *, allow_resume: bool = False
) -> None:
    """Debounce, re-validate, then create the slot's session and release it.

    Ordering is load-bearing:

    1. The turn-in-flight bail (``slot.running``) MUST precede the pending-
       reset consume. The project-set endpoint is reachable from inside the
       kiro-cli process group via the ``set_project`` MCP tool, and consuming
       the reset kills that session's process group — mid-turn that would
       kill the caller, which is exactly what the deferred-reset design
       exists to prevent. When a turn is running, its own end-of-turn path
       consumes the reset instead.
    2. ``get_or_create`` acquires the per-session semaphore; it is released
       immediately below because no turn follows. A first message arriving
       mid-handshake blocks on that same semaphore and then reuses the
       created session — the per-key serialization in ``get_or_create`` is
       what makes the eager call and the real call converge on one session.
    """
    try:
        await asyncio.sleep(_EAGER_SPAWN_DEBOUNCE_SECS)
        sessions = getattr(state, "sessions", None)
        if sessions is None:
            return
        if state.get_slot(slot.key) is not slot:
            return  # slot deleted or replaced while debouncing
        if slot.running:
            return  # a real turn owns session creation (and the pending reset)
        if _eager_spawn_sem.locked():
            logger.info("Eager spawn: concurrency cap reached, skipping slot %s", slot.key)
            return
        async with _eager_spawn_sem:
            await _consume_pending_reset(state, slot)
            session_key = effective_session_key(slot)
            if allow_resume:
                # The focus signal only ever adds the RESUME case; fresh eager
                # spawn stays owned by the create/project/agent signals. This
                # probe is the in-memory hint (no disk, no pruning): SessionMap
                # is loop-owned and unlocked, so the pruning ``resumable_sid``
                # lookup must not run in a worker thread — a prune there would
                # race concurrent loop-side map writes. The authoritative
                # pruning lookup happens inside get_or_create's resume path,
                # on the loop, where it always ran; a false-positive hint just
                # means the speculative load falls back and is torn down below.
                if not sessions.resumable_hint(session_key):
                    return
            # Snapshot the bindings the handshake is about to bake in. A
            # switch handler (workspace, model, reasoning effort) that fires
            # mid-handshake resets the session key — but the reset no-ops
            # because nothing is registered yet, so without this check the
            # eager task would register a session carrying the OLD bindings
            # and the first real turn would silently reuse it (e.g. run tools
            # in the wrong workspace). Agent/project changes re-arm through
            # schedule_eager_spawn and cancel this task, but the other
            # switches don't — the snapshot covers them all uniformly.
            _bound = (slot.agent, slot.model, slot.project, slot.reasoning_effort)
            kiro_agent: str | None = None
            # Canonical crew identity for watchdog overrides. Seeded from the
            # slot, replaced by the resolver's alias below: an EMPTY slot runs
            # the DEFAULT crew (resolve_agent_bindings step 2), whose overrides
            # would be discarded by passing "" here.
            crew_alias = slot.agent or ""
            agent_model = ""
            try:
                cfg = KiroCrewConfig.load()
                bindings = resolve_agent_bindings(cfg, slot.agent or None)
                kiro_agent = bindings.kiro_agent
                crew_alias = bindings.resolved_alias
                agent_model = normalize_agent_model(bindings.model)
            except Exception:
                logger.warning(
                    "Eager spawn: failed to resolve agent bindings for slot %s",
                    slot.key,
                    exc_info=True,
                )
            _t0 = time.monotonic()
            try:
                # speculative=True keeps the one-shot first-turn flag armed for
                # the real first message (atomically, at registration) and
                # refuses resumable keys — unless allow_resume opted in, in
                # which case the speculative session/load runs here and the
                # resumed=True observation is armed for the real turn. See
                # get_or_create's docstring.
                _, is_new, resumed = await sessions.get_or_create(
                    session_key,
                    agent=kiro_agent or slot.agent or None,
                    # Canonical crew identity — the resolver's alias, which
                    # covers the default crew on an empty slot; plumbed to the
                    # session so per-agent watchdog windows never depend on a
                    # cross-namespace name match. "" is authoritative: no
                    # alias applied, so no override applies.
                    crew_agent=crew_alias,
                    model=slot.model or agent_model or None,
                    cwd=slot.project or None,
                    speculative=True,
                    speculative_resume=allow_resume,
                    reasoning_effort_override=slot.reasoning_effort or None,
                )
            except SpeculativeResumeRefused:
                # Two sources: the entry gate (resumable key, resume not
                # opted in — fresh eager spawn leaves it to the first turn)
                # or a failed speculative LOAD (allow_resume path: F2 fell
                # back / mapping vanished / provider switch), rejected before
                # registration so no claimable fallback session exists. Both
                # end the same way: the first real message handles it.
                logger.info("Eager spawn: %s left to first turn (refused)", session_key)
                return
            sessions.release(session_key)
            # The cleanup below may only tear down a session THIS task created.
            # is_new=False means another creator won the same-key race (or the
            # claim attached to an already-registered session): a real turn
            # owns that runtime, may have finished its turn already, and may
            # have background work (subagents) still attached — removing it
            # here would terminate the winner's session out from under it. The
            # winner registered with its own current bindings, so the stale-
            # bindings hazard these guards exist for does not apply to it.
            if not is_new:
                logger.info(
                    "Eager spawn: another creator won %s, leaving session alone", session_key
                )
                return
            # The slot can be deleted while the handshake ran; the delete
            # handler's sessions.remove() may have executed before this task
            # registered the session, which would leave an orphan that a
            # recreated slot with the same key would silently reuse with THIS
            # slot's (now stale) agent/cwd bindings. Tear it down.
            if state.get_slot(slot.key) is not slot:
                logger.info(
                    "Eager spawn: slot %s vanished mid-handshake, removing session", slot.key
                )
                await sessions.remove(session_key)
                return
            # Same shape for a binding change: a switch handler's reset ran
            # before registration and found nothing, so the session we just
            # registered carries stale bindings. Remove it — the first real
            # message cold-starts with the current bindings, exactly as if
            # eager spawn never ran.
            if (slot.agent, slot.model, slot.project, slot.reasoning_effort) != _bound:
                logger.info(
                    "Eager spawn: slot %s bindings changed mid-handshake, removing session",
                    slot.key,
                )
                await sessions.remove(session_key)
                return
            logger.info(
                "Eager spawn: session ready for %s in %.0fms (new=%s resumed=%s)",
                session_key,
                (time.monotonic() - _t0) * 1000.0,
                is_new,
                resumed,
            )
            if allow_resume and resumed:
                _schedule_prefetch_ttl(state, slot, session_key)
                await _cap_armed_prefetches(sessions, session_key)
            # allow_resume and not resumed cannot happen: a speculative
            # resume whose load fell back is rejected BEFORE registration
            # (SpeculativeResumeRefused, caught above) precisely so no
            # claimable fallback session ever exists — a real turn queued
            # during the load would otherwise claim it and strand its
            # exchanges behind the preserved old sid.
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Eager spawn failed for slot %s", slot.key, exc_info=True)


def _schedule_prefetch_ttl(state: "DashboardState", slot: "_ChatSlot", session_key: str) -> None:
    """Arm the unclaimed-prefetch teardown for a speculatively RESUMED session.

    One pending TTL per slot: a newer prefetch cancels the older timer, so the
    countdown always covers the most recent load. The teardown itself is
    conditional — ``remove_if_unclaimed`` no-ops once a real turn consumed the
    one-shot markers or a claimant holds the semaphore — so a TTL that fires
    after the user came back does nothing.
    """
    prev = getattr(slot, "_prefetch_ttl_task", None)
    if prev is not None and not prev.done():
        prev.cancel()
    slot._prefetch_ttl_task = asyncio.create_task(_prefetch_ttl(state, slot, session_key))


async def _prefetch_ttl(state: "DashboardState", slot: "_ChatSlot", session_key: str) -> None:
    """Tear down a resume-prefetched session no real turn claimed in time.

    A resumed session pins kiro-cli's native per-session lock; leaving an
    abandoned prefetch to the 30-minute idle sweep holds that lock (and the
    process's RSS) far longer than the speculation was worth. The removal
    preserves the session map, so the next focus or first message resumes
    again normally.
    """
    try:
        await asyncio.sleep(_RESUME_PREFETCH_TTL_SECS)
        _armed_prefetches.pop(session_key, None)  # arm window over either way
        sessions = getattr(state, "sessions", None)
        if sessions is None:
            return
        _current = state.get_slot(slot.key)
        if _current is not None and _current is not slot:
            return  # slot replaced — the new occupant owns this key now
        if _current is None:
            # Slot DELETED — do not assume the delete handler cleaned up this
            # session: it removes the slot-key-derived history session, while
            # a channel-born slot's prefetch registered under its LINKED
            # session key (effective_session_key). Returning here would leak
            # that process holding kiro-cli's native lock. Fall through to the
            # conditional removal — it no-ops on an already-removed key and
            # never touches a claimed session (e.g. the channel side using
            # the linked session).
            pass
        elif slot.running:
            return  # a real turn claimed (or is claiming) the session
        # Shielded: a cancel landing after remove_if_unclaimed has popped the
        # registry entry but before provider.shutdown() finishes would leak
        # the process holding kiro-cli's native lock — nothing else can find
        # it anymore. The shield lets the removal run to completion while the
        # cancel still propagates to this task.
        if await asyncio.shield(sessions.remove_if_unclaimed(session_key)):
            logger.info(
                "Resume prefetch: unclaimed session %s expired after %.0fs",
                session_key,
                _RESUME_PREFETCH_TTL_SECS,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Prefetch TTL failed for slot %s", slot.key, exc_info=True)


async def _handle_goal_command(state: "DashboardState", slot: "_ChatSlot", message: str) -> None:
    """Handle the ``/goal`` slash command (v0 self-verdict loop).

    Extracted from ``_run_chat`` so it is unit-testable in isolation. Pure glue
    over the async ``AutoNudgeService`` (``add`` / ``get_by_slot`` / ``remove``);
    no autonudge-internals change. Subcommands: ``status`` (default/empty),
    ``clear``, else arm with an optional ``--max N`` budget (default 50, clamped
    1..50).
    """
    _goal_svc = get_instance()
    _parts = message.split(None, 1)
    _rest = _parts[1].strip() if len(_parts) > 1 else ""
    if _goal_svc is None:
        body = (
            "🎯 Goal loops are unavailable (AutoNudge is disabled). "
            "Set `KIROCREW_AUTONUDGE=1` and restart the gateway."
        )
    elif _rest in ("", "status"):
        _loop = _goal_svc.get_by_slot(slot.key)
        if _loop is not None:
            _cap = _loop.max_cycles or "∞"
            body = f"🎯 Active goal (budget {_cap} turns). " "Use `/goal clear` to stop it."
        else:
            body = (
                "No active goal. Set one with `/goal <objective>` "
                "(optionally `/goal --max N <objective>`)."
            )
    elif _rest == "clear":
        _loop = _goal_svc.get_by_slot(slot.key)
        if _loop is not None:
            await _goal_svc.remove(_loop.id)
            body = "🎯 Goal cleared."
        else:
            body = "No active goal to clear."
    else:
        _max_cycles = 50
        _objective = _rest
        _m = re.match(r"--max\s+(\d+)\s+(.*)", _rest, re.DOTALL)
        if _m:
            _max_cycles = max(1, min(50, int(_m.group(1))))
            _objective = _m.group(2).strip()
        elif _rest.startswith("--max"):
            _objective = ""
        if not _objective:
            body = "Usage: `/goal <objective>` or `/goal --max N <objective>`."
        else:
            _slug = re.sub(r"[^A-Za-z0-9._-]", "_", slot.key)
            _sentinel = str(data_home() / "goal-stop" / f"{_slug}.stop")
            Path(_sentinel).unlink(missing_ok=True)
            _nudge = (
                f"Goal: {_objective}\n"
                "Each idle cycle, in order: "
                f'(1) if the file {_sentinel} exists -> autonudge_stop(reason="sentinel") and stop; '
                "(2) if the goal is fully met by concrete evidence (a passing test, a built file, "
                'command output — not a guess) -> autonudge_stop(reason="goal met"), post a one-line '
                "summary citing the evidence, and stop; "
                "(3) else do ONE atomic step (<=5 tool calls) and make the deliverable durable "
                "(write the file / run the check) before claiming progress.\n"
                "Guardrails: never git push; never read credential files. Hard blocker -> state it once and "
                f'autonudge_stop(reason="blocked"). Budget {_max_cycles} cycles (service stops at '
                "the cap). One short progress line per cycle."
            )
            await _goal_svc.add(
                slot.key,
                message=_nudge,
                idle_secs=15,
                max_cycles=_max_cycles,
                stop_sentinel_path=_sentinel,
            )
            body = (
                f"⊙ Goal set ({_max_cycles}-turn budget): {_objective}\n\n"
                "I'll work toward it across turns and stop when it's met "
                "(verified by evidence) — or run `/goal clear` to stop."
            )
    body = _redact_for_display(body)
    sel().log_tool_invocation(
        session_key=slot.key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name="/goal",
        tool_kind="slash_command",
        outcome="ok",
        metadata={"slot": slot.key},
    )
    slot.append("assistant", body, "msg msg-a")
    state.push_slots_update()
    slot.append("done", "", "done")


def _settle_consumed_steers(slot: "_ChatSlot", snapshot: str) -> None:
    """Settle pending steers covered by a ``steering_consumed`` echo.

    The parse-and-match rules live in ``steer_settle.settle_consumed_steers``,
    shared with the ``/side`` sidecar, which hands kiro-cli the same
    fire-and-forget steers and needs the same answer.
    """
    if not slot._pending_steers:
        return
    # settle_all_on_empty preserves this path's long-standing behaviour on an
    # empty echo. The /side sidecar deliberately chose the opposite (an empty
    # echo is no evidence, so keep entries pending and let the requeue show a
    # cancellable card); whether the main chat should follow is a separate
    # change, because its requeue is not exercised here.
    remaining = settle_consumed_steers(slot._pending_steers, snapshot, settle_all_on_empty=True)
    logger.debug(
        "Steer consumed for slot %s (%d settled, %d still pending)",
        slot.key,
        len(slot._pending_steers) - len(remaining),
        len(remaining),
    )
    slot._pending_steers[:] = remaining


def _requeue_unconsumed_steers(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Degrade unconsumed mid-turn steers into ordinary queue cards.

    Called from ``_run_chat``'s finally on every turn-exit path. A steer that
    kiro-cli never confirmed via ``steering_consumed`` died with the turn
    (stall-cancel, soft STOP, error, or a steer racing the turn's natural
    end); without this it would vanish silently.

    Requeues at the HEAD of the slot queue — steers were meant to be injected
    before any queued item ran — preserving their relative order, and
    broadcasts a ``queue_push`` per message so open clients render the card.
    The card is visible and individually cancellable: a user whose STOP meant
    "discard" dismisses it with one click; nothing is ever silently lost.
    A hard kill never reaches here with pending steers (the force-stop
    handler clears ``_pending_steers`` alongside ``_queue``).
    """
    if not slot._pending_steers:
        return
    requeued = slot._pending_steers[:]
    slot._pending_steers.clear()
    for steer_msg in reversed(requeued):
        # Raw-at-rest by design: slot._queue is a DELIVERY payload (the drained
        # entry becomes the next turn's LLM input), matching every other queue
        # producer (queue_append in chat_handlers / messaging). All dashboard
        # egresses redact: the three "queue" response sites in chat_handlers
        # apply _redact_for_display, and every queue_* broadcast (including the
        # queue_push below) sanitizes. Sanitizing at insert would corrupt the
        # delivered message relative to the normal queue path.
        qid = slot.queue_insert(0, steer_msg)
        try:
            content, _ = redact_exfiltration_urls(steer_msg)
            content, _ = redact_credentials(content)
            state.broadcast_ws(
                "queue_push",
                {
                    "slot": slot.key,
                    "content": _redact_for_display(content),
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "queue_id": qid,
                },
            )
        except Exception:
            # Broadcast is best-effort — the message is already safely in the
            # queue; clients reconcile from slot detail on next fetch.
            logger.warning(
                "queue_push broadcast failed for requeued steer (slot %s)",
                slot.key,
                exc_info=True,
            )
    logger.info(
        "Requeued %d unconsumed steer(s) for slot %s (turn ended before consumption)",
        len(requeued),
        slot.key,
    )


async def _start_next_queued_turn(state: DashboardState, slot: _ChatSlot) -> bool:
    """Dequeue and start one ready Kiro turn, preserving queue semantics."""

    if not slot._queue:
        return False

    try:
        merge = KiroCrewConfig.load().dashboard.merge_queued_messages
    except Exception:
        logger.warning(
            "Failed to load config; falling back to sequential dequeue",
            exc_info=True,
        )
        merge = False

    in_stage = bool(slot._in_stage_execution)
    hold_users = bool(
        (
            state.subagents is not None
            and state.subagents.running_agents_for(f"dashboard:{slot.key}")
        )
        or in_stage
    )
    if hold_users:
        # During a multi-stage plan hold cron notifications too: each stage is
        # its own _run_chat whose tail-drain runs while _in_stage_execution is
        # still set, so draining a cron here starts an unrelated turn between
        # stages and scatters the plan. It drains at end-of-plan once the gate
        # clears. Sub-agent completions / recovery still flow.
        next_msg, consumed = _dequeue_next_system_message(slot, exclude_cron=in_stage)
    else:
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=merge)
    if next_msg is None:
        return False

    is_recovery = any(is_synthetic_recovery_item(item) for item in consumed)
    # Orthogonal to `is_recovery`, which decides how the row renders: this decides
    # whether the runner may mirror the text to a linked thread as user speech.
    # They diverge on a recovery that replays the user's own message.
    synthetic_payload = any(is_synthetic_payload_item(item) for item in consumed)
    is_system_injection = any(is_system_injection_item(item) for item in consumed)
    if slot._stopping and not is_system_injection:
        slot.append(
            "error",
            "⟳ Session reset — processing next message with conversation history",
            "msg msg-err",
        )
        slot._stopping = False

    for item in consumed:
        content, _ = redact_exfiltration_urls(item["content"])
        content, _ = redact_credentials(content)
        state.broadcast_ws(
            "queue_pop",
            {
                "slot": slot.key,
                "content": _redact_for_display(content),
                "queue_id": item["id"],
            },
        )
        _remove_queued_by_id(slot.messages, item["id"])

    next_msg, _ = redact_exfiltration_urls(next_msg)
    next_msg, _ = redact_credentials(next_msg)
    is_cron = next_msg.startswith(CRON_NOTIFY_PREFIX)
    is_subagent = next_msg.startswith(SUBAGENT_COMPLETION_PREFIXES)
    if not (is_cron or is_subagent or is_recovery):
        slot._pending_synthesis = False
    match = CRON_NOTIFY_RE.match(next_msg) if is_cron else None
    cron_label = match.group(1) if match else "cron"
    cron_label, _ = redact_exfiltration_urls(cron_label)
    cron_label, _ = redact_credentials(cron_label)
    # Structured completion facts stamped at enqueue time (gateway _subagent_done)
    # ride through to the row so the card reads them instead of re-parsing the
    # header prose. Only a single, un-merged system injection carries
    # them: a merge concatenates several entries under one synthetic header, for
    # which per-entry facts are meaningless — subagent completions never merge
    # (they drain one at a time and break any user-message merge), so this only
    # fires on the shape it was computed for.
    _row_meta = consumed[0].get("meta") if (is_subagent and len(consumed) == 1) else None
    # When synthesis is pending, mark the completion so the frontend can collapse
    # the per-completion assistant response that follows (it will be restated by
    # the synthesis turn).
    if is_subagent and slot._pending_synthesis and isinstance(_row_meta, dict):
        _row_meta = {**_row_meta, "synthesisPending": True}
    if is_subagent:
        row_role = "subagent"
    elif is_cron or is_recovery:
        row_role = "inject"
    else:
        row_role = "user"
    if is_cron:
        # A cron row's `cls` slot carries a JSON payload, not a CSS class name:
        # `cronLabel` is structured data the frontend reads off the row.
        row_cls = json.dumps({"cronLabel": cron_label})
    elif is_recovery:
        row_cls = "msg msg-inject"
    else:
        row_cls = "msg msg-u"
    # Durable provenance for every `inject` row. `cls` is NOT persisted for this
    # role (chat_persistence only keeps it for `role == "system"`), and the
    # frontend's `meta.cronLabel` exists on the wire only because parse_cls_meta
    # synthesizes it at emit time — so anything keyed on it silently disappears
    # after a flush + rehydrate. `meta` IS persisted and restored, so the render
    # side can ask what a row IS instead of guessing from what its text is not.
    #
    # The recovery split matters: build_recovery_requeue replays the USER'S OWN
    # message verbatim when the turn emitted nothing, and that row must keep
    # rendering as speech. `synthetic_payload` is the existing answer to exactly
    # that question, so reuse it rather than inventing a second signal.
    if row_role == "inject":
        _inject_kind = "cron" if is_cron else "recovery" if synthetic_payload else "user_replay"
        _inject_meta: dict = {"injectKind": _inject_kind}
        if is_cron:
            _inject_meta["cronLabel"] = cron_label
        _row_meta = {**_row_meta, **_inject_meta} if isinstance(_row_meta, dict) else _inject_meta
    slot.append(
        row_role,
        next_msg,
        row_cls,
        meta=_row_meta if isinstance(_row_meta, dict) else None,
    )

    task = spawn_guarded_turn(
        state,
        slot,
        _run_chat(state, slot, next_msg, _synthetic_payload=synthetic_payload),
    )
    slot.task = task
    return True


async def _run_pending_synthesis(state: DashboardState, slot: _ChatSlot) -> None:
    """Consume and run one armed synthesis turn.

    Kiro readiness is not waited on here. Readiness is latched at boot and only
    refreshed by an explicit user action, so waiting on a stale not-ready value
    would park this waiter indefinitely instead of letting the ACP attempt
    report the real auth state. The turn runs; a signed-out CLI surfaces as an
    ``AcpAuthRequired`` error card from ``_run_chat``.
    """

    try:
        if not slot._pending_synthesis:
            _finish_queue_cycle(state, slot)
            return
        if slot._queue:
            state.push_slots_update()
            if await _start_next_queued_turn(state, slot):
                return
        if (
            state.subagents is None
            or state.subagents.running_agents_for(f"dashboard:{slot.key}")
            or slot._subagent_deliveries_inflight != 0
        ):
            _finish_queue_cycle(state, slot)
            return

        # All delivery guards hold. Consume immediately before the turn begins.
        slot._pending_synthesis = False
        # Append the row BEFORE dispatching, matching `_start_next_queued_turn`.
        # This site bypasses that function (it runs no queue entry), and it was
        # the only turn-dispatching path that appended nothing — so the prompt
        # reached the conversation log with no dashboard row, and on replay it
        # resurfaced attributed to the USER. `inject` is the role every other
        # runner-authored continuation already uses, which is exactly what
        # SUBAGENT_SYNTHESIS_PREFIX's own docstring promises.
        slot.append(
            "inject",
            SUBAGENT_SYNTHESIS_PROMPT,
            "msg msg-inject",
            meta={"injectKind": "synthesis"},
        )
        state.push_slots_update()
        synthesis_task = spawn_guarded_turn(
            state,
            slot,
            # Declare the provenance structurally too. Without it this turn starts
            # a time-to-first-token clock whose own contract excludes synthetic
            # prompts, and `_is_synthetic` has to recover the same fact by
            # re-matching the marker string downstream.
            _run_chat(state, slot, SUBAGENT_SYNTHESIS_PROMPT, _synthetic_payload=True),
        )
        try:
            await synthesis_task
        except (asyncio.TimeoutError, TimeoutError):
            # The ceiling fired; spawn_guarded_turn's callback already rendered
            # the card naming the limit. Swallow here so the timeout does not
            # also propagate into this function's caller, which dispatches
            # fire-and-forget and would drop it unretrieved.
            pass
    finally:
        slot._synthesis_inflight = False


def _finish_queue_cycle(state: DashboardState, slot: _ChatSlot) -> None:
    """Start synthesis when eligible, otherwise mark a queue cycle idle."""

    if not slot._queue:
        slot._stopping = False
    if (
        slot._pending_synthesis
        and not slot._synthesis_inflight
        and state.subagents is not None
        and not state.subagents.running_agents_for(f"dashboard:{slot.key}")
        and slot._subagent_deliveries_inflight == 0
    ):
        slot._synthesis_inflight = True
        task = asyncio.create_task(_run_pending_synthesis(state, slot))
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
        state.push_slots_update()
        return

    slot.append("done", "", "done")
    slot.task = None
    state.push_slots_update()
    state.broadcast_ws("chat_done", {"slot": slot.key})
    # The turn that just finished is the most likely moment for this session's
    # PRs to have moved (opened, pushed, merged, reviewed), so re-read their
    # status now instead of leaving the sidebar chips on TTL rotation and the
    # detail panel on no refresh at all.
    state.refresh_slot_source_status(slot.key)
    state.push_refresh("history")
    if not slot._titled:
        title_task = asyncio.create_task(_maybe_auto_title(state, slot))
        state._background_tasks.add(title_task)
        title_task.add_done_callback(state._background_tasks.discard)
    else:
        # Already titled: re-examine an AUTO title at bounded milestones so
        # long sessions aren't stuck with a name generated from their very
        # first message. Self-guarding (origin/milestone/in-flight checks in
        # maybe_refresh_title) — the common case returns without any LLM call.
        refresh_task = asyncio.create_task(maybe_refresh_title(state, slot))
        state._background_tasks.add(refresh_task)
        refresh_task.add_done_callback(state._background_tasks.discard)

    # Intent summary for the chat summary panel. Self-guarding: the common case
    # (feature disabled) returns before any work, and an unchanged transcript is
    # served from the sidecar cache without a model call.
    summary_task = asyncio.create_task(generate_session_summary(state, slot))
    state._background_tasks.add(summary_task)
    summary_task.add_done_callback(state._background_tasks.discard)


def _emit_ttft_metric(t0: float, session_key: str, *, is_new: bool, resumed: bool) -> None:
    """Emit the user-message → first-visible-token latency histogram.

    Best-effort, one point per top-level user prompt. ``first_turn`` splits the
    cold-path population eager spawn targets (the slot's first message) from
    steady-state turns, and ``resumed`` separates ``session/load`` costs — the
    same attribution axes as the startup metric, so the two histograms can be
    read side by side.
    """
    try:
        # Re-read at call time even though the module also imports it at the
        # top: the rebind is what lets a test patching
        # ``kiro_crew.metrics.provider.get_recorder`` reach this emit.
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().histogram(
            "kirocrew.chat.first_token.duration",
            (time.monotonic() - t0) * 1000.0,
            unit="ms",
            attrs={
                "channel": telemetry_channel_of(session_key),
                "first_turn": bool(is_new),
                "resumed": bool(resumed),
            },
        )
    except Exception:
        logger.debug("TTFT metric emission failed", exc_info=True)


async def _run_chat(
    state: DashboardState,
    slot: _ChatSlot,
    message: str,
    *,
    _prompt_depth: int = 0,
    _synthetic_payload: bool = False,
    regenerate_hint: str = "",
) -> None:
    """Stream LLM response into *slot*.  Survives browser disconnect."""

    # Capture before any await: a Stop can complete while pre-turn setup is
    # suspended and reset _stop_state to idle before continuation processing.
    # The monotonic generation preserves that user intent across the whole call.
    _stop_gen_at_entry = slot._stop_generation

    session_key = effective_session_key(slot)
    sessions = getattr(state, "sessions", None)

    # Time-to-first-token clock: starts when the user's message reaches the
    # runner, stops at the first visible model output (text OR thinking chunk).
    # This is the end-to-end latency eager spawn / warm pooling exist to cut —
    # startup.duration only covers the handshake slice, so without this the
    # user-perceived win is not measurable. Top-level user prompts only:
    # synthetic payloads and nested prompts are runner-authored, and mixing
    # them in would skew the distribution the feature is judged by.
    _ttft_t0 = time.monotonic() if (_prompt_depth == 0 and not _synthetic_payload) else None

    # Inherit Slack link: if this dashboard session mirrors a Slack thread,
    # copy the link so every exit path, including an auth failure, can reply on
    # the originating surface.
    if sessions is not None and session_key.startswith("dashboard:"):
        link = sessions.get_slack_link(session_key)
        if not (link and link[0]):
            raw_key = session_key[len("dashboard:") :]
            link = sessions.get_slack_link(raw_key)
            if link and link[0] and link[1]:
                sessions.set_slack_link(session_key, link[0], link[1])

    # No pre-turn readiness gate: latched readiness is only refreshed at boot and
    # on explicit user action, so denying here would block a send the CLI would
    # have served. The ACP attempt below is the authority and raises
    # AcpAuthRequired when the CLI is signed out.

    async def _fire(
        event: str,
        context: str = "",
        tool_name: str = "",
        tool_input: dict | None = None,
        tool_response: dict | None = None,
        hook_continuation_count: int = 0,
    ) -> list[str]:
        """Fire script hooks. Returns stdout texts from exit-0 hooks (for context injection)."""
        injected: list[str] = []
        if state._hook_store is None:
            if event == HOOK_EVENT_PRE_TOOL_USE:
                injected.append("BLOCKED:system:hook store not initialized")
                logger.error("Hook store not initialized for PRE_TOOL_USE - blocking tool")
            return injected
        try:
            results = await state._hook_store.fire(
                event,
                context,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_response=tool_response,
                parent_session_key=session_key,
                hook_continuation_count=hook_continuation_count,
            )
            for r in results:
                if r.exit_code == 0 and r.stdout:
                    injected.append(r.stdout)
                    logger.info("Hook %s stdout: %s", r.hook_name, r.stdout[:200])
                    state.broadcast_ws(
                        "activity_event",
                        {
                            "slot": slot.key,
                            "kind": "hook",
                            "text": f"Hook {r.hook_name}: injected {len(r.stdout)} chars",
                        },
                    )
                elif r.exit_code == 2:
                    injected.append(
                        f"BLOCKED:{r.hook_name}:{r.stderr[:200] if r.stderr else 'hook denied'}"
                    )
                    logger.warning(
                        "Hook %s blocked tool: %s",
                        r.hook_name,
                        r.stderr[:200] if r.stderr else "exit 2",
                    )
                    state.broadcast_ws(
                        "activity_event",
                        {
                            "slot": slot.key,
                            "kind": "hook",
                            "text": f"Hook {r.hook_name} BLOCKED: {r.stderr[:100] if r.stderr else 'denied'}",
                        },
                    )
                elif r.exit_code not in (0, 2) and r.stderr:
                    # Non-zero, non-block: show warning
                    logger.warning("Hook %s warning: %s", r.hook_name, r.stderr[:200])
        except Exception as exc:
            if event == HOOK_EVENT_PRE_TOOL_USE:
                logger.warning("Hook fire error during blocking event %s: %s", event, exc)
                raise
            logger.warning("Hook fire error: %s", exc)
        return injected

    assistant_text = ""
    # Initialized HERE (not with the other turn-state flags below) because
    # _steer_segment_cut declares it nonlocal — mypy requires the binding to
    # exist textually before the nested def. Semantics unchanged: nothing
    # touches it between here and the flag block.
    _produced_visible_output = False
    last_heartbeat = time.time()
    chunk_seq = 0
    in_tool_group = False
    # Whole-turn assistant-text buffer for orchestrator plan detection. Unlike
    # `assistant_text` (reset on every tool-call boundary), this is NEVER reset
    # mid-turn, so a plan emitted BEFORE further tool calls is still visible at
    # end-of-turn. Only accumulated on a planning turn (see `_orch_planning`).
    _orch_plan_buf = ""
    # Set True when the final-segment detector below arms a plan, so the
    # whole-turn-buffer fallback doesn't arm a second time.
    _armed_final = False
    # A turn is a "planning turn" iff it's orchestrator mode AND not a stage
    # execution turn driven by _stage_loop. Only planning turns detect/arm a
    # plan; stage-execution turns must never re-arm (that corrupted the stage
    # total). `_in_stage_execution` is set by _stage_loop around its _run_chat.
    _orch_planning = getattr(slot, "mode", "") == "orchestrator" and not getattr(
        slot, "_in_stage_execution", False
    )
    # Rolling-buffer redactor for the live chat_chunk wire stream. Per-chunk
    # redaction misses a credential split across streaming boundaries;
    # this withholds the trailing credential-class run until it is confirmed safe
    # so raw fragments never reach WS/SSE consumers. assistant_text (the source
    # for the final _flush_segment redaction) is accumulated independently and is
    # unaffected. Reset per segment via _flush_text_stream / _wsred.reset().
    _wsred = StreamRedactor()

    def _flush_text_stream() -> None:
        """Emit the redactor's withheld tail as a final chat_chunk before a
        segment is finalized, so WS/SSE viewers see the complete (redacted) text
        and never a truncated stream. No-op when the buffer is empty."""
        nonlocal chunk_seq
        wire = _wsred.flush()
        if not wire:
            return
        chunk_seq += 1
        slot.append("chunk", wire, "chunk")
        state.broadcast_ws("chat_chunk", {"slot": slot.key, "content": wire, "seq": chunk_seq})

    # Same rolling-buffer protection for the separate chat_thinking wire stream
    # (thinking is broadcast-only / ephemeral, but still real-time on the WS).
    _thinkred = StreamRedactor()

    def _flush_thinking_stream() -> None:
        """Emit the thinking redactor's withheld tail when the thinking phase
        ends (any non-thinking event) or the turn completes. No-op when empty."""
        wire = _thinkred.flush()
        if wire:
            state.broadcast_ws("chat_thinking", {"slot": slot.key, "content": wire})

    def _steer_segment_cut() -> None:
        """Finalize the accumulated text as a segment at a mid-turn steer.

        Published on the slot (next to ``_acp_client``) so the dashboard steer
        handler can cut the segment right BEFORE it persists the steer user
        message. Without the cut, kiro-cli keeps the segment open across the
        steer, so ``_flush_segment`` at end-of-segment appends the WHOLE text
        (pre-steer + post-steer) BELOW the steer bubble — the reply the user
        watched stream above their steer jumps to the bottom when the chat_done
        refresh rebuilds from server history — and the pre-steer ``chunk``
        entries are stranded above the bubble forever (the trailing-run walk in
        ``_flush_segment`` stops at the first non-chunk message).

        ``broadcast=False``: the initiating tab already froze its streaming
        message when it pushed the optimistic bubble, and other tabs freeze on
        the ``steer_push`` echo (appendSlotMessage finalize-on-steer). A
        chat_segment broadcast here could instead finalize a NEWER post-steer
        streaming message that raced ahead on the initiating tab.

        Sync on purpose — the handler and this turn share the event loop, so
        the flush cannot interleave with chunk processing.
        """
        nonlocal assistant_text, _produced_visible_output
        # Drop the wire redactor's withheld tail instead of emitting it: a
        # chat_chunk broadcast here would arrive AFTER the clients froze their
        # streaming message at the steer boundary, opening a phantom streaming
        # bubble below the steer card. No text is lost — assistant_text
        # accumulates the full segment independently of the wire buffer, and
        # _flush_segment persists (and re-redacts) that full text.
        _wsred.reset()
        if assistant_text.strip():
            # quiet_persist: the clients already hold this text in their
            # frozen (pre-steer) message; the append's chat_message broadcast
            # would render a duplicate copy below the steer bubble.
            _flush_segment(state, slot, assistant_text, broadcast=False, quiet_persist=True)
            # The flushed segment IS visible output. Every other mid-turn site
            # that resets assistant_text (compaction, clear, agent switch,
            # /compact) sets this flag too; without it, a turn that streamed
            # text, got steered, and then ended with no further text would hit
            # the empty-response branch and requeue the ORIGINAL prompt —
            # re-running its side effects.
            _produced_visible_output = True
        assistant_text = ""

    # Partial-output guard for transient-5xx retry: flipped True once ANY
    # assistant token streams or a tool call fires this turn. A transient
    # backend 5xx is only retried while this is False, so a re-prompt can't
    # double-stream text or re-run a side-effecting tool.
    _turn_emitted = False
    # Model-activity marker for the poisoned-conversation streak ONLY:
    # flipped True on thinking chunks. Deliberately separate from
    # _turn_emitted — thinking is ephemeral/broadcast-only, so retrying
    # after a thinking-only failure stays safe (see EVENT_THINKING_CHUNK) —
    # but a backend that streams reasoning IS serving this conversation, so
    # such a failure is mid-generation, not the poisoned pre-stream
    # signature, and must not count toward (or survive as) a discard streak.
    _turn_thought = False
    # Refresh the one-shot post-token recovery allowance at the START of a
    # GENUINE new user turn, so each real user message gets exactly one recovery.
    # A repeated post-token 5xx that happens DURING recovery must NOT recover
    # again (infinite loop), so the synthetic recovery turn — detected by the
    # incoming message being the recover instruction — deliberately does NOT
    # refresh the allowance and inherits the True flag set when recovery was
    # enqueued. Suppressed/nested recoveries never set the flag, so
    # this reset is a no-op for them and a later real turn can still recover.
    if message not in _SYNTHETIC_RECOVERY_MSGS:
        slot._posttoken_retry_used = False
    # tool_call_id -> DISPLAY TITLE (LLM-authored prose for shell tools; used
    # only for PostToolUse hook name-matching — NOT trustworthy for security).
    _pending_tools: dict[str, str] = {}
    # tool_call_id -> canonical directive-tool name (forgery gate). Written
    # ONLY at EVENT_TOOL_CALL, ONLY from the out-of-band _meta.kiro identity
    # (event.tool_name + event.mcp_server_name), never from the title. This is
    # the ONLY map the session-directive gate below trusts.
    _pending_dir_tool: dict[str, str] = {}
    # tool_call_id -> the output we already produced for a CONSUMED directive
    # (applied confirmation, or the native-sub-agent not-applied note). A tool
    # call can surface more than one result frame; once the mapping above is
    # consumed a later frame would otherwise fall through with the RAW marker
    # text and overwrite the applied outcome in the transcript. Replaying the
    # stored output keeps every frame consistent and marker-free.
    _dir_consumed_out: dict[str, str] = {}
    # session_id -> {started, done, agent, task} for native kiro-cli subagents,
    # reconciled from `_kiro.dev/subagent/list_update` (one card per sub-agent).
    # The slot holds the same live dict so reconnect snapshots can restore cards.
    _native_tracker: dict[str, dict] = {}
    slot._native_subagent_tracker = _native_tracker
    # inner tool_call_id -> native card id, from `_kiro.dev/session/update`, so a
    # sub-agent's tool calls stream onto its own card.
    _native_tc_card: dict[str, str] = {}
    # tool_call_ids whose output was already streamed to a native card — kiro
    # emits two tool_call_update frames per tool (content + rawOutput), so we
    # dedupe to avoid printing the same output twice.
    _native_result_seen: set[str] = set()
    # native card id -> accumulated activity feed (tool calls + outputs). The
    # published frontend replaces a card's live `streaming` text with `result`
    # on done, so we persist the feed here and send it as the done `result`.
    _native_card_output: dict[str, list[str]] = {}
    slot._native_subagent_output = _native_card_output
    _native_card_output_len: dict[str, int] = {}
    needs_session_reset = False
    # Poisoned-conversation escalation: unlike needs_session_reset (which
    # preserves the resume sid so the next turn session/loads the same native
    # conversation), discard_conversation CLEARS the sid so the next turn
    # cold-starts a fresh conversation — while keeping the session-map entry,
    # whose Slack thread/channel linkage must survive the recovery. Set only
    # by the consecutive pre-stream-exhaustion branch in the AcpError handler
    # below.
    needs_conversation_discard = False
    _auth_required = False
    saw_compaction = False
    _turn_tool_calls = 0  # tool dispatches this turn (refusal diagnostic)
    _retrying_empty = False
    # Whether THIS turn consumed the one-shot post-compaction re-injection flag.
    # Bound at turn scope, not at the consume site: the consume lives inside the
    # context-builder leg, and the probe/base legs skip it entirely — reading an
    # unbound local at the restore would raise UnboundLocalError.
    _needs_reinjection = False
    # Set ONLY where the turn is recorded as successful. The `finally` restores
    # the re-injection flag when this is still False, which covers every
    # non-landing exit — the early `return`s (stale-recover, tool-stall, error
    # re-queue), every `except` arm, and a hard CancelledError — not just the
    # graceful-cancel and empty-re-queue paths that reach the success check.
    _turn_landed = False
    # Recoverable tool refusals (host-gate policy deny / read-only bash gate)
    # recorded during this turn as (redacted_title, reason). If non-empty when
    # the turn ends — and the user did not stop it — a recovery continuation is
    # enqueued so the model learns why and can adapt instead of stalling.
    _refusal_reasons: list[tuple[str, str]] = []
    # Track how deep an unbroken hook-continuation run is, so the Stop hook can
    # see it: each consecutive hook continuation is one deeper; any other turn
    # (a real user message, a refusal recovery) breaks the run and resets it.
    # Gate on synthetic provenance, not the marker text alone: a real dequeued
    # continuation carries _synthetic_payload, but a user who types the marker
    # verbatim is ordinary speech and must not inflate the depth a gate hook
    # sees (would let a spoofed message drive the hook's self-limit).
    if _synthetic_payload and message.startswith(HOOK_CONTINUATION_RECOVERY_PREFIX):
        slot._hook_continuation_depth += 1
    else:
        slot._hook_continuation_depth = 0
    # Runner-authored continuations are orchestration, not user input, and the
    # post-fan-out synthesis prompt is one too: never mirror either to linked
    # surfaces (Slack/Telegram) as if the user typed it — only the assistant reply
    # is delivered. A recovery that replays the user's own message is NOT covered,
    # because that text is the user's; the queue entry distinguishes the two so a
    # user who types a marker verbatim still counts as ordinary user speech.
    _is_synthetic = _synthetic_payload or message.startswith(SUBAGENT_SYNTHESIS_PREFIX)

    # ── Slash commands: detect early, before session acquisition ──
    first_word = message.split()[0] if message.strip() else ""
    _is_cc_provider = KiroCrewConfig.load().agent.provider == "claude_code"
    is_slash = (first_word.startswith("/") and _is_cc_provider) or first_word in _SLASH_COMMANDS

    # Block dangerous/local-only commands before acquiring a session
    if first_word in _BLOCKED_SLASH_COMMANDS:
        sel().log_tool_invocation(
            session_key="",
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name=first_word,
            tool_kind="slash_command",
            outcome="blocked",
            metadata={"slot": slot.key},
        )
        slot.append(
            "assistant",
            f"⚠️ `{first_word}` is not available in the dashboard.",
            "msg msg-a",
        )
        state.push_slots_update()
        slot.append("done", "", "done")
        return

    # ── /goal: arm / clear a goal-driven self-verdict loop (v0) ──
    # v0 rides AutoNudgeService unchanged: the nudge instructs the agent to
    # self-check its Definition of Done each cycle and call autonudge_stop when
    # met.
    if first_word == "/goal":
        await _handle_goal_command(state, slot, message)
        return

    # ── /prompts: handle locally instead of forwarding to kiro-cli ──
    if first_word == "/prompts":

        args = message.split(None, 2)  # /prompts [get] [name]
        sub = args[1] if len(args) > 1 else ""

        if sub == "get" and len(args) > 2:
            # /prompts get <name> — invoke the prompt in this chat
            name = args[2]
            expanded, status = _expand_prompt_mention(f"@{name}", state, slot)
            if status == "ok":
                sel().log_tool_invocation(
                    session_key="",
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="ok",
                    metadata={"mention": f"@{name}", "slot": slot.key, "via": "/prompts get"},
                )
                # Re-enter _run_chat with the expanded message (depth=1, no further expansion)
                await _run_chat(state, slot, expanded, _prompt_depth=1)
            elif status == "blocked":
                sel().log_tool_invocation(
                    session_key="",
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="blocked",
                    metadata={"mention": f"@{name}", "slot": slot.key, "via": "/prompts get"},
                )
                slot.append(
                    "assistant", f"🔒 Prompt `{name}` blocked — sensitive path.", "msg msg-a"
                )
                state.push_slots_update()
                slot.append("done", "", "done")
            elif status == "too_large":
                sel().log_tool_invocation(
                    session_key="",
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="too_large",
                    metadata={"mention": f"@{name}", "slot": slot.key, "via": "/prompts get"},
                )
                slot.append(
                    "assistant",
                    f"⚠️ Prompt `{name}` exceeds size limit ({MAX_PROMPT_BYTES // 1000}KB).",
                    "msg msg-a",
                )
                state.push_slots_update()
                slot.append("done", "", "done")
            else:
                sel().log_tool_invocation(
                    session_key="",
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="not_found",
                    metadata={"mention": f"@{name}", "slot": slot.key, "via": "/prompts get"},
                )
                slot.append("assistant", f"❌ Prompt `{name}` not found.", "msg msg-a")
                state.push_slots_update()
                slot.append("done", "", "done")
            return

        # /prompts or /prompts list — show available prompts
        try:
            # _list_aim_prompts walks the (possibly large or edition-supplied)
            # prompt_source_roots() with rglob + file reads; keep it off the
            # event loop so a slow/network-backed root can't stall the gateway.
            prompts = await asyncio.to_thread(_list_aim_prompts)
        except Exception:
            prompts = []
        if not prompts:
            slot.append(
                "assistant",
                "No prompts found. Create prompts in `~/.kiro/prompts/` (or `~/.kiro/crew/prompts/`).",
                "msg msg-a",
            )
            sel().log_tool_invocation(
                session_key="",
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name="prompt_list",
                tool_kind="prompt",
                outcome="empty",
                metadata={"count": 0, "slot": slot.key, "via": "/prompts"},
            )
            state.push_slots_update()
            slot.append("done", "", "done")
            return
        lines = ["**Available Prompts** — type `@name` to invoke\n"]
        by_source: dict[str, list] = {}
        for p in prompts:
            (by_source.setdefault(p["source"], [])).append(p)
        for src, items in sorted(by_source.items()):
            label = "User Prompts" if src in ("aim", "package") else f"User Prompts ({src})"
            lines.append(f"\n**{label}:**")
            for p in items:
                desc = f" — {p['description']}" if p["description"] else ""
                lines.append(f"- `@{p['fullName']}`{desc}")
        text = "\n".join(lines)
        text, _ = redact_credentials(text)
        text, _ = redact_exfiltration_urls(text)
        slot.append("assistant", text, "msg msg-a")
        sel().log_tool_invocation(
            session_key="",
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name="prompt_list",
            tool_kind="prompt",
            outcome="ok",
            metadata={"count": len(prompts), "slot": slot.key, "via": "/prompts"},
        )
        state.push_slots_update()
        slot.append("done", "", "done")
        return

    # A new turn supersedes whatever question the previous one ended on, so any
    # OPTIONS control still live in this session's Slack thread stops being
    # answerable. Guarded on _prompt_depth so the in-turn re-entry that expands
    # a /prompts reference does not count as a new turn.
    #
    # Placed HERE, below every local-command return and above the turn machinery,
    # for the same reason the Slack entry points expire here: `/goal`, a
    # `/prompts` listing or error, and a blocked slash command all return without
    # starting an agent turn, and spending the control on one of those would
    # strike a still-valid question through with nothing to answer it. Moved once,
    # rather than guarded per command, so a new local command inherits the right
    # behaviour by default.
    if _prompt_depth == 0:
        await expire_slack_options(state, session_key)

    # Publish the identity of the turn this call is about to run. From here down
    # the local `session_key` is what the turn acquires, audits and releases,
    # while `slot.linked_session_key` stays mutable underneath it: a cron
    # injection binds an existing slot with no `running` gate, so a turn that
    # started on `dashboard:<slot>` can find the slot routed at `cron:<id>`
    # before it ends. A cancel that re-derived the key would then address a
    # session this turn never ran on, so the cancel routes read this instead.
    #
    # Placed at the same boundary as the OPTIONS expiry above, and for the same
    # reason: `/goal`, a `/prompts` listing or error, and a blocked slash command
    # all return without starting an agent turn, so none of them owns a turn
    # identity. `/prompts get` re-enters at `_prompt_depth=1`, and it is that
    # depth-1 call which reaches the machinery below while its depth-0 wrapper
    # returns above — so keying on the BOUNDARY is what puts the identity on the
    # invocation that actually runs the turn. Keying on `_prompt_depth == 0`
    # would put it on the wrapper instead.
    #
    # BELOW the expiry, not above it: that await is the only thing between the
    # boundary and the try, so installing after it means every exit that can
    # happen while the identity is live reaches the teardown that retires it.
    # Only plain assignments separate this line from the try.
    slot._active_turn_session_key = session_key

    _acquired = False
    _mirror_stream_ts: str = ""
    _mirror_chan: str | None = ""
    _mirror_active_task = ""
    _mirror_active_task_title = ""
    _mirror_thread: str | None = ""
    _mirror_task_counter = 0
    try:
        # Resolve agent bindings early so we pass the correct kiro-cli
        # agent name (e.g. "kirocrew") instead of the KiroCrew slot name
        # (e.g. "default") which has no matching ~/.kiro/agents/ config.
        kiro_agent: str | None = None
        memory_store: str | None = None
        # The KiroCrew agent's own default model ("" = inherit). Ranks below the
        # slot's explicit pick and above the bound kiro agent's pin / the global
        # agent.model fallback, both of which get_or_create resolves when this
        # and slot.model are empty.
        agent_model = ""
        # Read the provider into a local alongside the other bindings. Both model
        # branches below need it, and `cfg` is only bound inside the try — a
        # malformed config raises, the except swallows it, and touching
        # `cfg.agent.provider` afterwards would raise UnboundLocalError and kill
        # the turn. "" is the honest value for "config unreadable": it is not
        # "claude_code", so the model helpers fall through to their live-client
        # guards (`is_claude_backend`, the advertised list) rather than trusting a
        # provider name that could not be read.
        provider_name = ""
        # Canonical crew identity for watchdog overrides — same seeding rule
        # as the eager-spawn path (the two must agree): slot value until the
        # resolver supplies its alias, which covers the default crew on an
        # empty slot.
        crew_alias = slot.agent or ""
        try:
            cfg = KiroCrewConfig.load()
            provider_name = cfg.agent.provider
            # Warm the project agent index OFF the loop, then resolve inline. Only
            # the warm is offloaded: resolve_agent_bindings can raise StopIteration
            # on a malformed config, and StopIteration cannot be delivered through a
            # Future, so awaiting it would hang instead of surfacing the error.
            await warm_project_agent_names(slot.project)
            bindings = resolve_agent_bindings(cfg, slot.agent or None, slot.project or None)
            kiro_agent = bindings.kiro_agent
            crew_alias = bindings.resolved_alias
            memory_store = bindings.memory_store_name
            agent_model = normalize_agent_model(bindings.model)
        except Exception:
            logger.warning("Failed to resolve agent bindings in _run_chat", exc_info=True)

        state.broadcast_ws(
            "activity_event", {"slot": slot.key, "kind": "status", "text": "Creating session…"}
        )
        slot.model = _normalize_model(slot.model or "") or ""
        # Consume a deferred project-change reset queued while idle, before
        # get_or_create or we'd reuse the stale session for one turn. Safe here:
        # no session lock is held yet, so reset() can't self-kill.
        await _consume_pending_reset(state, slot)
        client, is_new, resumed = await state.sessions.get_or_create(
            session_key,
            agent=kiro_agent or slot.agent or None,
            # Same canonical crew identity as the eager-spawn path — the two
            # must agree or an eager session and its real first turn would
            # carry different watchdog windows.
            crew_agent=crew_alias,
            model=slot.model or agent_model or None,
            cwd=slot.project or None,
            reasoning_effort_override=slot.reasoning_effort or None,
        )
        _acquired = True
        # Member activity pointer — once per SESSION, not per turn: the log
        # answers "which sessions did this member take part in", so a per-turn
        # append would inflate every count taken from it. `slot.agent` is the
        # member the human picked; `kiro_agent` is the template it resolved to,
        # and only the member identity is recorded.
        #
        # Offloaded: this opens and appends to a file, and `_run_chat` shares the
        # single gateway event loop with every other session — matching the
        # to_thread offloads used for the other file IO in this function.
        # record_activity is total, so no guard is needed here.
        if is_new and slot.agent:
            await asyncio.to_thread(
                record_activity,
                slot.agent,
                session_key,
                slot.memory_mode or "",
                project=slot.project or "",
                via="chat",
                dedupe_session=True,
            )
        # Publish the live inner AcpClient onto the slot so a concurrent request
        # (the dashboard steer handler) can reach the running session's client
        # to inject a mid-turn steer. Cleared in the finally below.
        slot._acp_client = getattr(client, "client", None)
        # This consumer implements the low-fidelity child downgrade (the
        # interactive card) — opt in so the handle-level fail-close gate
        # yields those events here instead of rejecting them itself.
        # setattr: the LLMProvider interface doesn't declare the attribute;
        # AcpSessionProvider forwards it to the handle, other providers ignore.
        try:
            setattr(client, "child_fidelity_aware", True)
        except Exception:  # pragma: no cover - providers without the attr
            pass
        # Companion steer handle: lets the steer handler cut the current text
        # segment at the steer boundary (see _steer_segment_cut). Same
        # lifecycle as _acp_client.
        slot._steer_segment_cut = _steer_segment_cut
        # Backfill slot.model from provider if user didn't explicitly set one.
        # AcpProvider stores the resolved model on client._model. For claude_code
        # that is a provider id; map it back to the canonical registry key so it
        # matches the canonical-keyed dropdown rows (else the active row won't
        # highlight and the header shows the raw provider id). Gated on the real
        # provider so a kiro/acp dotted id (which collides with a claude_code
        # alias spelling) is left as-is.
        withheld_pin = False
        if not slot.model:
            slot.model = _backfill_canonical_model(client, provider_name) or slot.model
        elif (is_new or resumed) and _pinned_model_withheld(client, slot.model, provider_name):
            withheld_pin = True
            # The session just advertised what this account can run, and the pin
            # is not on the list — the spawn withheld it, so this session runs on
            # the backend default.
            #
            # The pin is deliberately KEPT. Withholding (providers.acp) already
            # guarantees it is never sent and `displayModel` already guarantees
            # it is never shown as the running model, so a stale pin is inert —
            # while clearing it would be a one-way delete of an explicit user
            # setting, decided from ONE session's advertised list. Keeping it
            # means a plan re-upgrade (or a transiently short advertised list)
            # self-heals with no action from the user; clearing would force them
            # to notice and re-pick. Inert-and-recoverable beats tidy.
            #
            # Gated on a fresh/resumed session so this reports once per spawn —
            # the moment the withhold actually happens — rather than repeating on
            # every turn of a warm session.
            logger.warning(
                "Slot %s is pinned to %s, which this account cannot run; "
                "the session is on the backend default (pin kept for re-upgrade)",
                slot.key,
                slot.model,
            )
            # Say it in the transcript too, not only in the server log. Otherwise
            # the chip silently reads Auto, the picker no longer lists the model,
            # and there is no way to learn the account lost access to it.
            #
            # A persisted "notice" card rather than a transient activity line: the
            # explanation has to survive a reload, because the state it explains
            # does (the pin stays, and the chip keeps reading Auto). Soft info
            # styling for the same reason the empty-response notices use it — a
            # plan change is not a crash. slot.append persists AND broadcasts one
            # chat_message, so it needs no companion broadcast_ws.
            slot.append(
                "notice",
                f"{slot.model} isn't offered right now — "
                f"this session is running on auto instead. Pick another model "
                f"from the composer, or leave it: your model choice is kept and "
                f"will be used automatically once it's offered again.",
                "msg msg-info",
            )
        agent_label = kiro_agent or slot.agent or "default"
        # The label states what the session RUNS on, so a withheld pin reports the
        # effective model rather than `slot.model` — the pin is kept, so reading it
        # here would print the withheld model on the activity line directly beside
        # the notice card explaining that it is not what is running.
        model_label = "auto" if withheld_pin else (slot.model or "auto")
        # `spawned` marks the frames where a session was actually (re)started, so
        # consumers can act on a real session boundary. The frame itself is also
        # emitted on warm turns, where nothing was spawned and the advertised
        # model list cannot have changed.
        spawned = bool(is_new or resumed)
        if resumed:
            state.broadcast_ws(
                "activity_event",
                {
                    "slot": slot.key,
                    "kind": "session",
                    "spawned": spawned,
                    "text": f"Session resumed · {agent_label} · {model_label}",
                },
            )
        else:
            state.broadcast_ws(
                "activity_event",
                {
                    "slot": slot.key,
                    "kind": "session",
                    "spawned": spawned,
                    "text": f"Session created · {agent_label} · {model_label}",
                },
            )

        # Propagate trust/YOLO to session so subagents inherit auto-approve.
        # A scoped grant is excluded on purpose — see _persistable_session_policy.
        # Assigned unconditionally (not only when granting) so a turn that starts
        # after a grant went away clears any policy an earlier turn stored.
        state.sessions.set_approval_policy(
            session_key, _persistable_session_policy(slot, state.is_yolo_active())
        )

        # Drain MCP OAuth requests captured during session init. kiro-cli
        # buffers `_kiro.dev/mcp/oauth_request` notifications during MCP
        # server bring-up; the AcpClient collected them into
        # `pending_oauth_requests`. Every one is emitted; the ones a
        # Connections card owns are tagged so the render layer can avoid
        # repeating a prompt that card already shows.
        try:
            await _drain_session_init_oauth_requests(state, slot, client)
        except Exception:  # pragma: no cover — never let UI surfacing kill chat init
            logger.warning("Failed to surface pending MCP OAuth requests", exc_info=True)

        # Publish this turn's session identity so managed MCP tools resolve
        # X-Session-Key; one shared writer lives in messaging.identity.
        await publish_turn_identity(state.sessions, session_key)

        # ── @prompt expansion: resolve @name to SOP/prompt content ──
        # Captured BEFORE any expansion: `@prompt` replaces `message` and
        # `$skill` appends to it, so len(message) at classification time no
        # longer reflects what the user actually typed. See
        # attributable_user_chars. Transform-side corrections (marker
        # neutralization, the multibyte fold, a rewriting hook) are NOT applied
        # here — build_message maps these bounds to their final position itself.
        user_typed_len = len(message)
        prompt_expanded = False
        if message.startswith("@") and not is_slash and _prompt_depth < 1:
            original = message
            message, _status = _expand_prompt_mention(message, state, slot)
            if _status == "ok":
                prompt_expanded = True
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="ok",
                    metadata={"mention": original.split()[0], "slot": slot.key},
                )
            elif _status in ("blocked", "too_large"):
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome=_status,
                    metadata={"mention": original.split()[0], "slot": slot.key},
                )
                label = (
                    "sensitive path"
                    if _status == "blocked"
                    else f"size limit ({MAX_PROMPT_BYTES // 1000}KB)"
                )
                slot.append("system", f"🔒 Prompt blocked — {label}.", "msg msg-info")
                state.push_slots_update()
                return
            elif _status == "not_found":
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="not_found",
                    metadata={"mention": original.split()[0], "slot": slot.key},
                )

        # ── $skill expansion: resolve $name tokens anywhere → append skill body ──
        # Operates ONLY on the user's typed message, never on @prompt-substituted
        # content: `prompt_expanded` is True when an @prompt body replaced `message`
        # above (at the same _prompt_depth=0), so we skip $skill here to prevent a
        # prompt author's embedded $tokens from silently loading extra skills into
        # the context (expand-what-the-user-typed, principle of least surprise).
        # Skipped for slash commands; _prompt_depth<1 blocks the recursive _run_chat
        # path. Token is left literal; resolved bodies are appended.
        if "$" in message and not is_slash and not prompt_expanded and _prompt_depth < 1:
            message, _n_skills = _expand_dollar_skills(message, state, slot, session_key)
            if _n_skills:
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="skill_dollar_expansion",
                    tool_kind="prompt",
                    outcome="ok",
                    metadata={"count": str(_n_skills), "slot": slot.key},
                )

        # Ensure the mirror-source message is always bound before both the Slack
        # and channel-neutral user-message mirror legs run. The assignment that
        # refines it below only executes for non-slash turns that have a
        # context_builder; without this default, a non-slash turn with no
        # context_builder would hit UnboundLocalError at the mirror legs.
        _user_msg_for_mirror = message

        # Per-turn injection breakdown, recorded on the usage row at turn end.
        # Empty when this turn injected nothing (no context builder / raw path).
        slot_ctx_blocks: dict[str, int] = {}
        slot_ctx_phase = ""
        # Chars this turn prepends between the request header and the user's
        # text; set in the context_builder branch, 0 elsewhere.
        _user_prepend_offset = 0
        # Exact bounds of the user's text in the final prompt, reported by
        # build_message. Empty on the non-context-builder paths. The probe/base
        # pair re-derives the bounds after the later prepends (see below).
        _user_span: list[int] = []
        _span_probe = ""
        _span_base_len = -1
        if is_slash:
            full_message = message
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name="slash_command",
                tool_kind="slash",
                outcome="bypass",
                metadata={"command": first_word, "slot": slot.key},
            )
        elif state.context_builder:

            # Length of the message as the user's text stands NOW (after any
            # @prompt/$skill expansion, before the context this branch prepends).
            # The difference against len(message) at build_message time is how
            # far the user's text was pushed down — its offset for split_blocks.
            _core_msg_len = len(message)

            compressed: str | None = None
            # Provider-agnostic session replay: KiroCrew's conversation_log
            # is the canonical history source. Skip only when the provider
            # successfully resumed its own native session (same provider,
            # full-fidelity history already loaded via ACP session/load).
            _provider_has_history = resumed
            if not _provider_has_history:
                from kiro_crew.providers.acp import (
                    AcpProvider,  # circular: providers -> session -> chat_runner
                )

                if isinstance(client, AcpProvider) and client.client.resumed:
                    _provider_has_history = True
            if is_new and not _provider_has_history and state.context_builder.conversation_log:
                from kiro_crew.context import (  # circular: context -> chat
                    build_session_replay,
                    window_for_provider_client,
                )

                # drop the just-flushed current-turn user message
                # from replay. chat_handlers.py:146 (or queue dequeue at L1898)
                # always appended exactly one message before _run_chat fires,
                # and the periodic flush_loop may have already written it to
                # disk during the kiro-cli cold spawn (~5s flush vs ≥15s spawn).
                # Scale the replay budget to the model window (client is live here).
                # Offloaded: resolving this chat's tab id globs and opens every
                # session file sharing it to rebuild an index, then reads each
                # chained file in full — unbounded file IO on the hottest path in
                # the gateway, where it would block every other request.
                compressed = await asyncio.to_thread(
                    build_session_replay,
                    state.context_builder.conversation_log,
                    session_key,
                    exclude_last_n=1,
                    model_window=window_for_provider_client(client),
                )
                logger.info(
                    "Session replay: key=%s result=%s",
                    session_key,
                    f"{len(compressed)} chars" if compressed else "None (no history)",
                )
            # After a soft-cancel, kiro-cli drops the cancelled turn from its
            # conversation log — but everything BEFORE the cancel is preserved.
            # Re-inject just the cancelled turn (user prompt + partial assistant)
            # as a preamble so the LLM remembers what was interrupted, without
            # duplicating older history. Flag lives on the session (set by
            # SessionManager.stop_turn), consumed one-shot here. Use getattr
            # for prev_turn_cancelled so test doubles don't raise on access.
            _session = getattr(state.sessions, "_sessions", {}).get(session_key)
            if _session is not None and getattr(_session, "prev_turn_cancelled", False):
                _session.prev_turn_cancelled = False
                if state.context_builder and state.context_builder.conversation_log:
                    from kiro_crew.context import (
                        build_cancelled_turn_preamble,  # circular: context -> dashboard.chat -> chat_runner (can't top-level: context imports chat at module load); circular: context -> chat -> chat_runner; circular: context -> chat
                    )

                    preamble = build_cancelled_turn_preamble(
                        state.context_builder.conversation_log, session_key
                    )
                    if preamble:
                        message = preamble + "\n\n" + message
            logger.info("🔍 Chat slot=%s is_new=%s mode=%r", slot.key, is_new, slot.mode)
            # Drain any pending subagent delivery failures so the LLM knows
            # about timed-out results and can read them from disk.
            if slot._pending_subagent_failures:
                failures = slot._pending_subagent_failures[:]
                slot._pending_subagent_failures.clear()
                message = "\n\n".join(failures) + "\n\n" + message
            # Save raw user message before context/persona prepend for Slack
            # mirror — avoids leaking injected context to the linked thread.
            _user_msg_for_mirror = message
            # Drain pending context injections (silent background context
            # from apps/subagents).  Expired entries are discarded.
            _ctx_prefix = drain_pending_context(slot)
            if _ctx_prefix:
                message = _ctx_prefix + message
            # Use resolved kiro agent name (e.g. "kirocrew"), not the slot
            # name (e.g. "default"), so build_message's is_custom check
            # correctly identifies kirocrew sessions and enables skills.
            # Snapshot the message length AFTER every prefix this branch
            # prepended (cancelled-turn preamble, subagent failures, drained
            # pending context) but BEFORE the theme persona, which APPENDS a
            # suffix after the user's text. The prepend offset for split_blocks
            # must count only the prefixes; folding the appended persona in
            # would shove the user span past the real typed text and mis-carve.
            _msg_len_pre_persona = len(message)
            # Theme persona injection — APPENDS a persona suffix after the user
            # text (see _maybe_inject_persona); build_message still accounts for
            # it in the context budget. It is deliberately excluded from
            # _user_prepend_offset below (an append, not a prepend).
            # Folder breadcrumb: inject once per session, and again after a
            # folder move (no session reset — it's just a label refresh).
            folder_path = None
            if is_new or slot._folder_changed:
                folder_path = state.folder_breadcrumb(slot.folder_id) or None
                slot._folder_changed = False
            _color_theme = getattr(slot, "color_theme", "")
            # Governance gate: installed-pack persona injection
            # is a governable capability. A policy can force-disable it wholesale
            # (default-allow standalone). Only consult when a persona could
            # actually be injected (new turn + installed "custom-" theme) to
            # avoid a governance call on every ordinary turn. fail_closed=True:
            # unlike the neighboring capability sites (spawn/messaging/...),
            # which have always-on chokepoint checks behind governance, this
            # gate is the ONLY enforcement of the enterprise persona
            # off-switch — a degraded permissive Decision would silently
            # bypass a policy that disables capabilities.theme_persona.
            # A governance-evaluation error therefore denies (persona skipped
            # for that turn; the chat itself is unaffected).
            _persona_permitted = True
            if is_new and isinstance(_color_theme, str) and _color_theme.startswith("custom-"):
                from kiro_crew.platform.governance_profiles import governance_permits

                _decision = governance_permits(
                    "capabilities.theme_persona",
                    "",
                    session_key=session_key,
                    log_warning=False,
                    fail_closed=True,
                )
                _persona_permitted = getattr(_decision, "permitted", False)
                if not _persona_permitted:
                    logger.info(
                        "theme persona injection skipped: capabilities."
                        "theme_persona denied by governance policy"
                    )
            if _persona_permitted:
                message = _maybe_inject_persona(
                    message,
                    _color_theme,
                    is_new,
                    theme_consent_sha=getattr(slot, "theme_consent_sha", None),
                )
            # Scale the injected-context budget to the active model's context
            # window so a 200K model gets one-fifth the memory/lessons/history
            # chars a 1M model gets (same share of the window). Resolve from the
            # live session client (prefers its usage-reported window, else its
            # resolved model id) — the same helper Slack uses, so both surfaces
            # share one strategy. Unset/Auto ⇒ None ⇒ the 1M reference (unchanged
            # default behavior).
            from kiro_crew.context import window_for_provider_client  # circular: context -> chat

            model_window = window_for_provider_client(client)
            # Everything this branch PREPENDED (cancelled-turn preamble,
            # subagent failures, drained pending context) sits between the
            # request header and the user's text; record its length so the
            # breakdown carves the user span at the right offset, not flush
            # against the header. Both $skill AND the theme persona append AFTER
            # the user text, so neither shifts this — the persona suffix is
            # excluded by measuring the length before it was appended.
            _user_prepend_offset = max(0, _msg_len_pre_persona - _core_msg_len)
            # build_message resolves where the user's own text ENDS UP (it owns
            # the hook rewrite, the neutralization and the multibyte fold) and
            # writes the exact bounds into _user_span.
            # build_message performs blocking work (episodic query embed via
            # urllib to Ollama, file reads) — run off-loop (mc-embed bulkhead)
            # so a slow embedding endpoint can't stall the gateway event loop.
            # A compaction on the PREVIOUS turn dropped the session-start
            # context, taking the skills index with it. Read-and-clear the flag
            # here so this turn re-injects the index exactly once.
            _needs_reinjection = state.sessions.consume_needs_reinjection(session_key)
            full_message, _ = await run_in_embed_pool(
                state.context_builder.build_message,
                message,
                is_new,
                session_key,
                agent=kiro_agent or slot.agent or None,
                resumed=resumed,
                workspace=slot.workspace or None,
                project=slot.project or None,
                memory_store=memory_store,
                compressed_history=compressed,
                mode=slot.mode,
                blocks_reads=slot.blocks_reads,
                provider_type=cfg.agent.provider,
                runtime_source="dashboard",
                exclude_last_n=1,
                folder_path=folder_path,
                model_window=model_window,
                user_text_range=(
                    _user_prepend_offset,
                    _user_prepend_offset
                    + attributable_user_chars(user_typed_len, prompt_expanded=prompt_expanded),
                ),
                user_span_out=_user_span,
                needs_reinjection=_needs_reinjection,
            )
            # The reported span is valid for the message as build_message
            # returned it. Several later steps PREPEND to the finished prompt
            # (incognito/temporary notice, re-injected history, hook context, a
            # regenerate system line), each of which slides the span. Rather than
            # patch every site, snapshot the length and the spanned text here and
            # re-derive the offset once, just before classification — and verify
            # the shifted span still holds the same text, so a future transform
            # that breaks the assumption degrades to the legacy reconstruction
            # instead of silently persisting a wrong attribution.
            if len(_user_span) == 2:
                _span_probe = full_message[_user_span[0] : _user_span[1]]
                _span_base_len = len(full_message)
            full_message = _apply_incognito_prefix(slot, full_message)
        else:
            full_message = message

        # Re-inject history if session was reset but messages haven't been
        # saved to JSONL yet (e.g. stop button killed the process mid-chat).
        # build_session_context already injects recent() from JSONL, so this
        # only adds value when in-memory messages are newer than disk.
        # Skip for soft stops — session is preserved, no re-injection needed.
        if is_new and slot.messages:
            # Check if last stop was soft (session preserved, no re-injection).
            # cls is a JSON-encoded dict (see api_chat_slot_stop); parse it.
            _last_stop_soft = False
            for m in reversed(slot.messages):
                cls_val = m.get("cls", "")
                if not isinstance(cls_val, str) or not cls_val.startswith("{"):
                    continue
                try:
                    _cls = json.loads(cls_val)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(_cls, dict) or _cls.get("kind") != "stop_event":
                    continue
                if _cls.get("outcome") == "soft":
                    _last_stop_soft = True
                break
            if not _last_stop_soft:
                history_key = slot_history_key(slot)
                disk_count = 0
                if state.conversation_log:
                    disk_count = len(state.conversation_log.read_messages(history_key))
                mem_count = sum(1 for m in slot.messages if m.get("role") in ("user", "assistant"))
                if mem_count > disk_count:
                    history = _build_history_prefix(slot)
                    if history:
                        full_message = history + full_message

        if is_new:
            spawn_injected = await _fire(HOOK_EVENT_AGENT_SPAWN, session_key)
        else:
            spawn_injected = []

        injected = await _fire(HOOK_EVENT_USER_PROMPT_SUBMIT, message)
        all_injected = spawn_injected + injected
        if all_injected:
            hook_ctx = "\n\n".join(all_injected)
            full_message = f"[Hook context]\n{hook_ctx}\n[End hook context]\n\n{full_message}"

        if regenerate_hint:
            full_message = f"[System: {regenerate_hint}]\n\n{full_message}"

        # Slash commands use _kiro.dev/commands/execute for full native output;
        # regular messages use session/prompt.
        # Attribute the FINAL prompt back to the blocks that produced it, after
        # every prefix above has been applied. Classifying the OUTPUT rather than
        # counting at each of the ~30 append sites means the breakdown cannot
        # drift from what was actually sent, and an unrecognised block surfaces
        # as `unclassified` instead of being folded into a neighbour.
        ctx_len = len(full_message) - len(message)
        if ctx_len > 0:
            # Re-derive the user span against the FINAL prompt: everything added
            # after build_message is a pure prepend, so the whole shift is the
            # length delta. Accept it only when the shifted slice still holds the
            # same text — otherwise fall back to the reconstruction below.
            _span_arg: tuple[int, int] | None = None
            if len(_user_span) == 2 and _span_base_len >= 0:
                _shift = len(full_message) - _span_base_len
                _s, _e = _user_span[0] + _shift, _user_span[1] + _shift
                if 0 <= _s <= _e <= len(full_message) and full_message[_s:_e] == _span_probe:
                    _span_arg = (_s, _e)
                else:
                    logger.warning(
                        "context breakdown: user span did not survive post-assembly "
                        "prefixes (shift=%d); falling back to reconstruction",
                        _shift,
                    )
            slot_ctx_blocks = split_blocks(
                full_message,
                user_chars=attributable_user_chars(user_typed_len, prompt_expanded=prompt_expanded),
                user_offset=_user_prepend_offset,
                user_span=_span_arg,
            )
            slot_ctx_phase = PHASE_SESSION_START if is_new else PHASE_PER_TURN
            # Named rather than counted: naming only four blocks by hand
            # under-describes most of the bytes being reported.
            _named = ", ".join(
                label.replace("_", " ")
                for label, _ in sorted(slot_ctx_blocks.items(), key=lambda kv: -kv[1])
                if label != USER_LABEL
            )
            state.broadcast_ws(
                "activity_event",
                {
                    "slot": slot.key,
                    "kind": "context",
                    "text": (
                        f"Injected {ctx_len:,} chars of context ({_named})"
                        if _named
                        else f"Injected {ctx_len:,} chars of context"
                    ),
                },
            )

        event_stream = client.stream_command(message) if is_slash else client.stream(full_message)
        state.broadcast_ws("chat_status", {"slot": slot.key, "status": "Thinking…"})
        state.broadcast_ws(
            "activity_event", {"slot": slot.key, "kind": "status", "text": "Thinking…"}
        )

        # ── Bidirectional sync: mirror user message to linked Slack thread ──
        # Resolving the link is deliberately NOT gated on syntheticness — only the
        # user ECHO below is runner-authored. A recovery continuation still owes its
        # ANSWER to the thread that asked: gating the whole setup leaves
        # `_mirror_thread` empty, the reply leg downstream silently no-ops, and the
        # question already sitting on Slack is never answered at all.
        # A DISCONNECTED thread stops here and nowhere else: `_mirror_thread` and
        # `_mirror_chan` stay empty, which is what silences the echo, the tool
        # stream, the assistant reply and the stream teardown together. Disconnect
        # is the user saying "not into this conversation", which applies to the
        # answer as much as to the echo — so it is one gate, not four.
        if state.slack_client and not is_slash and not slack_mirror_is_paused(state, session_key):
            _mirror_thread, _mirror_chan = state.sessions.get_slack_link(session_key)
            if _mirror_thread and _mirror_chan:
                try:
                    if not _is_synthetic:
                        _mirror_msg = _prepare_mirror_msg(_user_msg_for_mirror)
                        await state.slack_client.post_message(
                            _mirror_chan, f"💬 _{_mirror_msg}_", _mirror_thread
                        )
                    # Start a stream for real-time tool animations
                    _mirror_stream_ts = (
                        await state.slack_client.start_stream(
                            _mirror_chan, _mirror_thread, initial_text="Thinking…"
                        )
                        or ""
                    )
                except Exception:
                    logger.debug("Failed to mirror user message to Slack", exc_info=True)

        # Channel-neutral leg: mirror the user message to a linked non-Slack
        # proactive channel (e.g. Telegram) so the remote conversation reads
        # coherently (question then reply), matching the Slack echo above.
        if not is_slash and not _is_synthetic:
            await _deliver_cross_surface_user_message(state, session_key, _user_msg_for_mirror)

        _stop_reason = ""
        # Cleared at turn START so post-turn consumers never read the PREVIOUS
        # turn's value: a turn that dies before EVENT_COMPLETE (ACP crash, auth
        # expiry, transport drop) never reaches the assignment below, and a
        # stale "end_turn" from the last successful turn would make the failed
        # turn look cleanly finished (e.g. to the session-summary gate).
        slot._last_stop_reason = ""
        # Tool-stall metadata forwarded by the ACP watchdog on its terminal
        # event (title / redacted command / evidence) — feeds the dedicated
        # tool-stall recovery nudge below.
        _stall_tool_title = ""
        _stall_command = ""
        _stall_evidence = ""
        # ── Per-turn stats (elapsed / credits) ──
        # Wall-clock start of the turn. kiro (acp) leaves TurnUsage.duration_ms
        # at 0, so elapsed is measured here; claude_code's API-reported
        # duration_ms is preferred when present. Captured at EVENT_COMPLETE and
        # attached to the final assistant message via _attach_turn_stats so the
        # dashboard shows the same end-of-turn stats kiro-cli prints natively.
        # _turn_msg_boundary scopes the attach to THIS turn's messages so an
        # error-only turn can't overwrite the previous turn's stats.
        _turn_t0 = time.monotonic()
        _turn_elapsed_ms = 0
        _turn_credits = 0.0
        _turn_cost_usd = 0.0
        _turn_model = ""
        _turn_msg_boundary = len(slot.messages)

        # Lease-dispatch race gate: this session's semaphore lease
        # was taken by get_or_create above, but the provider turn only opens on
        # the first stream iteration below. If a gateway restart / Make-Live
        # cutover moved the SessionManager into the closing state during the
        # async prep between, dispatching now would open a turn ABSENT from the
        # shutdown drain snapshot → killed mid-turn with its native lock held
        # (empty-response bug). Re-check SYNCHRONOUSLY here — no await between
        # this check and the async-for — so the _closing read and the stream's
        # turn registration (AcpClient.stream_events clears _turn_done before its
        # first await) are one atomic span, strictly ordered w.r.t. close_all's
        # _closing set. Abort (lease released by the outer finally) if closing.
        try:
            state.sessions.begin_turn(session_key)
        except SessionClosingError:
            logger.info("Aborting dispatch for %s — gateway is shutting down", session_key)
            return
        async for event in event_stream:
            # Heartbeat every 5s during long operations
            if time.time() - last_heartbeat > 5:
                state.broadcast_ws("heartbeat", {"slot": slot.key, "ts": time.time()})
                last_heartbeat = time.time()

            # First visible model output for this user prompt — emit TTFT once.
            if _ttft_t0 is not None and event.kind in (EVENT_TEXT_CHUNK, EVENT_THINKING_CHUNK):
                _emit_ttft_metric(_ttft_t0, session_key, is_new=is_new, resumed=resumed)
                _ttft_t0 = None

            # Security: tool_call_id originates from LLM — redact before any use
            if hasattr(event, "tool_call_id") and event.tool_call_id:
                _tcid, _ = redact_exfiltration_urls(event.tool_call_id)
                _tcid, _ = redact_credentials(_tcid)
                event.tool_call_id = _tcid

            # Leaving the thinking phase → flush any withheld thinking tail so a
            # credential split across thinking chunks can't cross the wire raw.
            if event.kind != EVENT_THINKING_CHUNK:
                _flush_thinking_stream()

            if event.kind == EVENT_TEXT_CHUNK:
                # If we just exited a tool group, finalize the streaming
                # message so post-tool text starts a fresh message.
                if in_tool_group:
                    _flush_text_stream()
                    if assistant_text:
                        _flush_segment(state, slot, assistant_text)
                        assistant_text = ""
                    else:
                        # No accumulated text, but still tell frontend to
                        # finalize any streaming message before tools.
                        state.broadcast_ws("chat_segment", {"slot": slot.key})
                    # Fallback: text after tools means all preceding tools
                    # are complete — mark any that weren't already marked
                    # (e.g. tools with no output).
                    for m in reversed(slot.messages):
                        if m.get("role") == "tool" and not m.get("meta", {}).get("done"):
                            m.setdefault("meta", {})["done"] = True
                            tcid = m.get("meta", {}).get("tool_call_id", "")
                            if tcid:
                                state.broadcast_ws(
                                    "tool_result",
                                    {"slot": slot.key, "tool_call_id": tcid, "output": ""},
                                )
                        elif m.get("role") not in ("tool", "permission", "chunk"):
                            break
                in_tool_group = False
                safe_chunk, _ = redact_exfiltration_urls(event.text)
                safe_chunk, _ = redact_credentials(safe_chunk)
                assistant_text += safe_chunk
                # Mirror into the never-reset whole-turn buffer so a plan
                # emitted before later tool calls survives the tool-boundary
                # reset of assistant_text above (planning turn only).
                if _orch_planning:
                    _orch_plan_buf += safe_chunk
                _turn_emitted = True  # tokens delivered — transient retry now unsafe
                # Stream to the wire through the rolling buffer so a credential
                # split across token boundaries can't cross a broadcast boundary
                # unredacted. Only the confirmed-safe prefix is emitted;
                # the trailing (possibly-partial-credential) run is withheld until
                # the next chunk or the segment flush. assistant_text above still
                # accumulates the full text for the authoritative final redaction.
                wire = _wsred.feed(event.text)
                if wire:
                    chunk_seq += 1
                    slot.append("chunk", wire, "chunk")
                    # Push chunk to WS clients (HTTP SSE reader drains from slot._pending)
                    state.broadcast_ws(
                        "chat_chunk",
                        {"slot": slot.key, "content": wire, "seq": chunk_seq},
                    )
            elif event.kind == EVENT_THINKING_CHUNK:
                # Thinking content is not included in the main response text.
                # Broadcast as a separate WS event for frontend rendering.
                # Streamed through StreamRedactor so a credential split across
                # thinking chunks can't cross the wire unredacted;
                # the withheld tail is flushed by _flush_thinking_stream when the
                # thinking phase ends or the turn completes.
                wire = _thinkred.feed(event.text)
                if wire:
                    state.broadcast_ws(
                        "chat_thinking",
                        {"slot": slot.key, "content": wire},
                    )
                # Deliberately NOT a turn-emit: do not flip _turn_emitted here.
                # Thinking is ephemeral, broadcast-only (never persisted to
                # slot.messages and never an irreversible side effect), so a
                # thinking-only turn that then hits a transient backend 5xx is
                # still safe — and worth — retrying. The only cost of a retry is
                # a cosmetic re-stream of reasoning the user already saw; no
                # answer text is doubled and no tool re-runs. If thinking ever
                # starts being persisted/accumulated, this must become a
                # turn-emit (set _turn_emitted = True) to avoid a double-emit —
                # pinned by TestRunChatTransientRetry.test_transient_after_thinking_only_retries.
                # It IS model activity though: the backend demonstrably serves
                # this conversation, so it must break the poisoned-discard
                # streak (a mid-generation death is not the pre-stream
                # rejection signature).
                _turn_thought = True
            elif event.kind == EVENT_TOOL_CALL:
                _turn_tool_calls += 1
                # Flush pre-tool text silently (no broadcast) so it persists,
                # but keep the streaming message in place for correct tool ordering.
                _flush_text_stream()
                if not in_tool_group and assistant_text:
                    _flush_segment(state, slot, assistant_text, broadcast=False)
                    assistant_text = ""
                in_tool_group = True
                _turn_emitted = True  # tool side effect — transient retry now unsafe
                # Broadcast for real-time visibility and persist
                _tool_payload = _tool_call_ws_payload(event)
                _tool_payload["slot"] = slot.key
                # Snapshot file BEFORE write tools execute. Accumulates per-turn,
                # flushed to assistant message meta in _flush_file_changes on turn end.
                # Prefer the in-band diff_old_text from the ACP content block
                # (authoritative) over a disk read which races with the write.
                # Offloaded: strReplace reconstruction reads the file from
                # disk, and a slow/hung filesystem must not stall the loop.
                _file_snapshot = await asyncio.to_thread(
                    _snapshot_write_target,
                    event.raw_tool_params,
                    diff_old_text=event.diff_old_text,
                    diff_path=event.diff_path,
                )
                if _file_snapshot:
                    slot._file_changes.append(_file_snapshot)
                state.broadcast_ws(
                    "tool_call",
                    _tool_payload,
                )
                slot.append(
                    "tool", f"🔧 {_tool_payload['tool']}", "msg msg-tool", meta=_tool_meta(event)
                )
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name=_redact_display_text(event.title),
                    tool_kind=event.tool_kind,
                    outcome="invoked",
                )
                # AskUserQuestion: validate via schema, redact, and broadcast
                if event.title == "AskUserQuestion" and event.tool_input:
                    try:
                        _q_input = json.loads(event.tool_input)
                        _questions = validate_ask_user_question(_q_input)
                        for q in _questions:
                            q["question"], _ = redact_exfiltration_urls(q["question"])
                            q["question"], _ = redact_credentials(q["question"])
                            q["header"], _ = redact_exfiltration_urls(q["header"])
                            q["header"], _ = redact_credentials(q["header"])
                            for o in q["options"]:
                                o["label"], _ = redact_exfiltration_urls(o["label"])
                                o["label"], _ = redact_credentials(o["label"])
                                o["description"], _ = redact_exfiltration_urls(o["description"])
                                o["description"], _ = redact_credentials(o["description"])
                        state.broadcast_ws(
                            "question_card",
                            {"slot": slot.key, "questions": _questions},
                        )
                    except (
                        json.JSONDecodeError,
                        TypeError,
                        KeyError,
                        AttributeError,
                        ValidationError,
                    ) as exc:
                        logger.warning("AskUserQuestion validation failed: %s", exc)
                # Fire PreToolUse hooks for auto-approved tools.
                # NOTE: For EVENT_TOOL_CALL, hooks are informational only - the tool
                # is already running (auto-approved by kiro-cli). Hook results cannot
                # block execution. Hook scripts can log, audit, or trigger side effects.
                _raw = event.title or ""
                if _raw.startswith("Running: "):
                    _raw = _raw[9:]
                if event.tool_call_id:
                    _pending_tools[event.tool_call_id] = _raw
                    # Forgery gate: record the directive-tool name ONLY
                    # from the trusted _meta.kiro identity and ONLY for a genuine
                    # call served by KiroCrew's OWN core MCP server — never the
                    # title, and never another (possibly third-party) MCP server
                    # that merely exposes a tool named e.g. "monitor_start". A
                    # shell tool has no mcp_server_name and canonical tool_name
                    # "execute_bash", so it can never register here. Recorded at
                    # EVENT_TOOL_CALL only (the UPDATE refinement rewrites titles).
                    if event.mcp_server_name == session_directive.CORE_MCP_SERVER:
                        _cannon = session_directive.match_tool(event.tool_name)
                        if _cannon:
                            _pending_dir_tool[event.tool_call_id] = _cannon
                # If this tool call belongs to a native sub-agent (mapped via
                # _kiro.dev/session/update), stream it onto that sub-agent's card.
                _nat_card = _native_tc_card.get(event.tool_call_id) if event.tool_call_id else None
                if _nat_card:
                    _ntool, _ = redact_exfiltration_urls(_raw or event.title or "")
                    _ntool, _ = redact_credentials(_ntool)
                    _ntool = _ntool[:80]
                    _native_card_output_len[_nat_card] = _append_native_output(
                        _native_card_output.setdefault(_nat_card, []),
                        f"\u2192 {_ntool}\n",
                        _native_card_output_len.get(_nat_card, 0),
                    )
                    state.broadcast_ws(
                        "subagent_chunk",
                        {"id": _nat_card, "slot": slot.key, "text": f"\u2192 {_ntool}\n"},
                    )
                await fire_tool_hooks(state._hook_store, event.title, event.tool_input)
                # Mirror tool call to linked Slack stream
                if _mirror_stream_ts:
                    try:
                        if _mirror_active_task:
                            await state.slack_client.append_task(
                                _mirror_chan,
                                _mirror_stream_ts,
                                _mirror_active_task,
                                _mirror_active_task_title,
                                "complete",
                            )
                        _mirror_task_counter += 1
                        _mirror_active_task = f"tool_{_mirror_task_counter}"
                        _task_title = event.tool_purpose or event.title
                        _task_title, _ = redact_exfiltration_urls(_task_title)
                        _task_title, _ = redact_credentials(_task_title)
                        _task_title = _task_title[:75]
                        _mirror_active_task_title = _task_title
                        await state.slack_client.append_task(
                            _mirror_chan,
                            _mirror_stream_ts,
                            _mirror_active_task,
                            _task_title,
                            "in_progress",
                        )
                    except Exception:
                        logger.debug("Mirror tool task failed", exc_info=True)
            elif event.kind == EVENT_TOOL_CALL_UPDATE:
                # claude-agent-acp emits an initial `tool_call` with empty
                # input (title falls back to generic name like "Terminal" or
                # "grep") followed by a `tool_call_update` carrying the
                # populated rawInput and a refined title from the upstream
                # `toolInfoFromToolUse`.  Patch the existing pill (toolLog)
                # and the persisted message in place by tool_call_id so the
                # user sees the actual command rather than the stub.
                if not event.tool_call_id:
                    continue
                try:
                    _tcid_upd = _redact_tool_field(event.tool_call_id)
                    _title_upd = ""
                    if event.title:
                        _title_upd, _ = redact_exfiltration_urls(event.title)
                        _title_upd, _ = redact_credentials(_title_upd)
                    _kind_upd = ""
                    if event.tool_kind:
                        _kind_upd, _ = redact_exfiltration_urls(event.tool_kind)
                        _kind_upd, _ = redact_credentials(_kind_upd)
                    _input_upd = _redact_tool_field(event.tool_input) if event.tool_input else ""
                    _purpose_upd = (
                        _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE)
                        if event.tool_purpose
                        else ""
                    )
                    # Snapshot file BEFORE the write tool actually executes.
                    # Initial tool_call had empty rawInput so no snapshot was
                    # taken there; this is the first event with the file path.
                    # Prefer the in-band diff_old_text from the ACP content
                    # block (authoritative) over a disk read which races with
                    # the write. Offloaded: reconstruction reads from disk and
                    # a slow/hung filesystem must not stall the loop.
                    _file_snapshot_upd = await asyncio.to_thread(
                        _snapshot_write_target,
                        event.raw_tool_params,
                        diff_old_text=event.diff_old_text,
                        diff_path=event.diff_path,
                    )
                    if _file_snapshot_upd:
                        slot._file_changes.append(_file_snapshot_upd)
                    # Refresh the toolLog entry (sseToolActivity merges by id).
                    state.broadcast_ws(
                        "tool_call",
                        {
                            "slot": slot.key,
                            "tool": _title_upd,
                            "kind": _kind_upd,
                            "tool_call_id": _tcid_upd,
                            "input_preview": _input_upd,
                            # The update is the event that supplies the real
                            # shell title/input, so it must carry the same
                            # capability signal as the initial tool_call.
                            "is_shell": event.is_shell,
                            "is_update": True,
                            # Omitted rather than sent empty: consumers merge a
                            # refinement field-by-field and read an absent
                            # `purpose` as "keep what the initial tool_call
                            # supplied", so an empty value would blank a good
                            # purpose (the session list's running-status line).
                            **({"purpose": _purpose_upd} if _purpose_upd else {}),
                        },
                    )
                    # Update the audit log so the SEL trail captures the
                    # refined title/kind, not just the "Terminal"/"grep" stub
                    # logged at the initial EVENT_TOOL_CALL.
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=slot.agent or "kirocrew",
                        source="dashboard",
                        tool_name=_title_upd,
                        tool_kind=_kind_upd,
                        outcome="refined",
                    )
                    # Patch the persisted tool message in place so its content
                    # shows the refined title and meta carries the populated
                    # input.  Walk in reverse and break on the first match —
                    # auto-approved tools may have a later "✅ {title}" entry
                    # with the same tool_call_id, and we don't want to
                    # overwrite that post-approval marker. Preserve whatever
                    # leading icon (🔧/✅/🚫) the existing message has.
                    _meta_patch: dict[str, str] = {}
                    if _input_upd:
                        _meta_patch["input"] = _input_upd
                    # A refinement is the only event carrying the purpose when the
                    # initial tool_call streamed an empty rawInput, so the patch has
                    # to reach the PERSISTED meta too: _tool_meta() wrote "" there,
                    # and the reloaded transcript reads meta.purpose (ToolCallLine),
                    # so a live-only fix would lose the purpose on the next reload.
                    if _purpose_upd:
                        _meta_patch["purpose"] = _purpose_upd
                    _patched = False
                    _patched_content: str | None = None
                    for m in reversed(slot.messages):
                        if m.get("role") != "tool":
                            continue
                        if m.get("meta", {}).get("tool_call_id") != _tcid_upd:
                            continue
                        if _title_upd:
                            existing = m.get("content", "") or ""
                            prefix = existing[:1] if existing[:1] in ("🔧", "✅", "🚫") else "🔧"
                            _patched_content = f"{prefix} {_title_upd}"
                            m["content"] = _patched_content
                            slot.invalidate_source_links()
                        if _meta_patch:
                            m_meta = m.setdefault("meta", {})
                            m_meta.update(_meta_patch)
                        _patched = True
                        break
                    if _patched:
                        slot._dirty = True
                        state.broadcast_ws(
                            "chat_message_update",
                            {
                                "slot": slot.key,
                                "tool_call_id": _tcid_upd,
                                **({"content": _patched_content} if _patched_content else {}),
                                **({"meta": _meta_patch} if _meta_patch else {}),
                            },
                        )
                    # Update _pending_tools so PostToolUse hooks see the
                    # refined name (e.g. "ls /tmp") instead of the stub
                    # ("Terminal").  Strip the "Running: " prefix to match
                    # the EVENT_TOOL_CALL handler's normalization — hooks
                    # match by tool name and would miss otherwise.
                    if event.title and event.tool_call_id in _pending_tools:
                        _refined_name = event.title
                        if _refined_name.startswith("Running: "):
                            _refined_name = _refined_name[9:]
                        _pending_tools[event.tool_call_id] = _refined_name
                except Exception:
                    logger.warning(
                        "EVENT_TOOL_CALL_UPDATE handler failed for tool_call_id=%s",
                        event.tool_call_id,
                        exc_info=True,
                    )
            elif event.kind == EVENT_TOOL_RESULT:
                _out = _redact_tool_field(event.tool_output)
                # Redact the join key once for the WS broadcast and the
                # message-meta comparison below. `_tool_meta` stores the
                # redacted form, so the comparison must use the redacted form
                # too — see the `_tool_meta` docstring for the convention.
                _tcid = _redact_tool_field(event.tool_call_id) if event.tool_call_id else ""
                # MCP Apps (flag-independent on this side): if gatewayd spooled a
                # UI payload it injected an opaque marker into the result text.
                # Load it, push an mcp_app_render event to this slot, and strip
                # the marker from the transcript text (cosmetic, like redaction).
                # Awaited: the spool read inside is thread-offloaded (multi-MB
                # records must not stall this event loop).
                _out = await mcp_apps_render.handle_tool_result(
                    state,
                    slot_key=slot.key,
                    tool_call_id=_tcid,
                    text=_out,
                    # WS routes on the bare slot.key, but the gateway recorded
                    # the CANONICAL producing session on the spool. Pass it for
                    # the binding check or every real render is refused as a
                    # bare-vs-prefixed mismatch (silent no-render).
                    producing_session_key=effective_session_key(slot),
                )
                # Session directive: a stateless session-bound tool
                # (monitor_start / monitor_update / autonudge_stop / set_project
                # / suggest_followup / ask_question) returns a directive marker
                # instead of resolving its own session identity. Apply it HERE,
                # where slot.key + session_key are the AUTHORITATIVE session for
                # this turn, then record the applier's real outcome on KiroCrew's
                # OWN surfaces (transcript / WS / hooks) and drop the marker.
                # NOTE: gateway-off (the default), the MODEL already received the
                # tool's own return over the MCP pipe — this does NOT rewrite the
                # model's tool result, which is why the tool's own message is
                # written to not over-claim the (consumer-applied) effect.
                # Gated on _pending_dir_tool — the CANONICAL _meta.kiro tool name
                # for a genuine MCP call, NOT model-authored result/title text —
                # so a forged marker under a shell/non-directive tool is ignored.
                # A native sub-agent's tool calls DO surface here (flat events
                # tagged in _native_tc_card) but have no independently bindable
                # slot, so they are refused rather than applied to the parent —
                # combined with spawn_run sub-agents running their own loop, no
                # sub-agent can ever arm/mutate its parent (isolation).
                _dir_tool = _pending_dir_tool.get(event.tool_call_id, "")
                if not _dir_tool and event.tool_call_id in _dir_consumed_out:
                    # A LATER frame for a directive we already consumed: replay
                    # the output we produced instead of letting the raw marker
                    # text overwrite the applied outcome in the transcript.
                    _out = _dir_consumed_out[event.tool_call_id]
                elif _dir_tool:
                    if event.tool_call_id in _native_tc_card:
                        # SINGLE-CONSUME: one tool call can surface MORE THAN ONE
                        # result frame (a mid-stream content frame and the final
                        # status=completed rawOutput frame — the same reason the
                        # native-card path below keeps _native_result_seen).
                        # Without this pop, a directive would be applied twice:
                        # two armed loops, two cards, or a repeated mutation.
                        _pending_dir_tool.pop(event.tool_call_id, None)
                        # Isolation denial — audit it (the one place the gate
                        # actively refuses) so it is not a silent drop.
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="mcp-directive",
                            tool_name=_dir_tool,
                            outcome="denied",
                        )
                        # Re-redact: the applier's string interpolates
                        # LLM-derived text (autonudge_stop reason, a bad path in
                        # set_project's error, exception args), and it OVERWRITES
                        # the entry-point _redact_tool_field(_out) above — so it
                        # must pass exfil-URL + credential scrubbing before it
                        # reaches broadcast_ws / the persisted transcript.
                        _out = _redact_tool_field(
                            session_directive.strip_marker(_out)
                            + (
                                "\n\n[Not applied: a session-bound tool called "
                                "from a sub-agent has no session to act on.]"
                            )
                        )
                        _dir_consumed_out[event.tool_call_id] = _out
                    else:
                        _dir_args = session_directive.decode(_out, _dir_tool)
                        if _dir_args is None and event.tool_final:
                            if session_directive.is_refusal(_out):
                                # encode() refused to emit a marker because the
                                # VALIDATED payload exceeded the delivery limit.
                                # Nothing was applied and the result text already
                                # told the model so, so this is the by-design
                                # loud failure — it must not fire the warning
                                # below, which exists to surface a lost marker.
                                logger.info(
                                    "session-directive REFUSED for %r "
                                    "(tool_call_id=%s): payload over the %d-char "
                                    "delivery limit; nothing applied",
                                    _dir_tool,
                                    event.tool_call_id,
                                    session_directive.MAX_DIRECTIVE_CHARS,
                                )
                                # SINGLE-CONSUME + strip: a refusal is terminal,
                                # so release the mapping and cache the
                                # marker-free text for any later frame carrying
                                # this same tool_call_id (which no longer
                                # resolves _dir_tool).
                                _pending_dir_tool.pop(event.tool_call_id, None)
                                _out = _redact_tool_field(session_directive.strip_marker(_out))
                                _dir_consumed_out[event.tool_call_id] = _out
                            else:
                                # The gate already AUTHENTICATED this as a
                                # directive tool via the canonical _meta
                                # identity, and this is the FINAL frame — so a
                                # marker that does not decode means the effect is
                                # being dropped outright. Never let that be
                                # silent: this exact silence can hide a
                                # rawOutput-envelope escaping bug.
                                # Mid-stream frames legitimately decode to None
                                # and are excluded by the tool_final guard.
                                logger.warning(
                                    "session-directive decode FAILED for %r "
                                    "(tool_call_id=%s, out_len=%d) — effect dropped",
                                    _dir_tool,
                                    event.tool_call_id,
                                    len(_out or ""),
                                )
                        if _dir_args is not None:
                            # SINGLE-CONSUME (see the native branch above): drop
                            # the mapping BEFORE applying, so a second result
                            # frame for this same tool call cannot re-apply the
                            # effect. Left in place when no marker decoded yet —
                            # a mid-stream partial frame must not burn the
                            # mapping the final frame still needs.
                            _pending_dir_tool.pop(event.tool_call_id, None)
                            _out = _redact_tool_field(
                                await apply_session_directive(
                                    state, slot, session_key, _dir_tool, _dir_args
                                )
                            )
                            _dir_consumed_out[event.tool_call_id] = _out
                        else:
                            # Recorded directive tool but no valid marker in the
                            # result — strip any stray sentinel from the transcript.
                            _out = session_directive.strip_marker(_out)
                state.broadcast_ws(
                    "tool_result",
                    {
                        "slot": slot.key,
                        "tool_call_id": _tcid,
                        "output": _out,
                    },
                )
                # If this tool result belongs to a native sub-agent, stream its
                # real output onto that sub-agent's card so the OUTPUT section
                # shows actual results (git log, file contents, summaries) —
                # not just tool names. Mirrors spawn_sub_agents' subagent_chunk.
                _nat_card_r = (
                    _native_tc_card.get(event.tool_call_id) if event.tool_call_id else None
                )
                if (
                    _nat_card_r
                    and event.tool_output
                    and event.tool_call_id not in _native_result_seen
                ):
                    _native_result_seen.add(event.tool_call_id)
                    _nout, _ = redact_exfiltration_urls(_out)
                    _nout, _ = redact_credentials(_nout)
                    _native_card_output_len[_nat_card_r] = _append_native_output(
                        _native_card_output.setdefault(_nat_card_r, []),
                        f"{_nout[:4000]}\n",
                        _native_card_output_len.get(_nat_card_r, 0),
                    )
                    state.broadcast_ws(
                        "subagent_chunk",
                        {"id": _nat_card_r, "slot": slot.key, "text": f"{_nout[:4000]}\n"},
                    )
                # Mark the matching tool message as done so completion state
                # survives page reload (persisted in message meta, replayed via SSE).
                # Also persist the redacted output here so the inline detail panel
                # has data after a chat reload (toolLog Redux state is in-memory).
                # Iterate ALL matching messages — auto-approved tools create two
                # tool entries (🔧 pre-approval + ✅ post-approval) with the same
                # tool_call_id, and both pills should reflect the same output.
                if _tcid:
                    for m in slot.messages:
                        if (
                            m.get("role") == "tool"
                            and m.get("meta", {}).get("tool_call_id") == _tcid
                        ):
                            _meta = m.setdefault("meta", {})
                            _meta["done"] = True
                            _meta["output"] = _out
                # Fire PostToolUse hooks
                _tool_name = _pending_tools.pop(event.tool_call_id, "")
                try:
                    _redacted_out, _ = redact_credentials(_out[:2000])
                    _redacted_out, _ = redact_exfiltration_urls(_redacted_out)
                    await _fire(
                        HOOK_EVENT_POST_TOOL_USE,
                        tool_name=_tool_name,
                        tool_response={"output": _redacted_out},
                    )
                except Exception:
                    logger.debug("PostToolUse hook error", exc_info=True)
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Permission is part of the tool group, not a break in it —
                # leaving in_tool_group True ensures the post-tool text fallback
                # (above, in EVENT_TEXT_CHUNK) still fires once the tool resolves
                # and the LLM continues with text. Resetting it here was the
                # cause of pills staying in "running" state until the next tool
                # call or message end.
                # Flush accumulated text as a finalized segment before the
                # permission flow so the frontend renders them in order.
                _flush_text_stream()
                if assistant_text:
                    _flush_segment(state, slot, assistant_text)
                    assistant_text = ""
                _pre_tool_hooks_fired = False
                # Backend-subagent request whose SECURITY context is absent
                # (structured params missing, or shell with no recoverable
                # command — see AcpEvent.child_low_fidelity): every
                # auto-approve gate below is skipped for it, falling through
                # to the interactive card. A child WITH full context takes the
                # same branches as the main agent (mode parity).
                _child_low_fidelity = event.child_low_fidelity
                # DISPLAY-ONLY warning for the interactive card: the human
                # must know the title is ALL there is (the params the gates
                # would verify are absent, so the displayed text is
                # agent-authored and unverifiable). Computed HERE — outside
                # the context-builder block — so the card is labeled whenever
                # the fidelity guards are active, including hosts with no
                # context builder. Never written into event.title: the
                # TrustDropdown derives its learned patterns from the title,
                # and a mutated title would store junk pattern entries for
                # exactly the requests that need the clearest presentation.
                _child_lf_warning = (
                    "⚠️ UNVERIFIED child request (security context "
                    "missing — title is agent-authored): "
                    if _child_low_fidelity
                    else ""
                )
                if state.context_builder:
                    # Pass the raw shell command (not just the display title)
                    # so the security gate evaluates what actually executes.
                    # event.title may be an LLM-authored description that hides
                    # a dangerous command (see HookManager.on_tool_call).
                    tool_result = state.context_builder.hooks.on_tool_call(
                        event.title,
                        session_key=session_key,
                        agent=slot.agent or "",
                        app=slot._app or "",
                        tool_kind=event.tool_kind,
                        raw_params=event.raw_tool_params,
                        command=event.shell_command,
                        is_shell=event.is_shell,
                        mcp_server_name=event.mcp_server_name,
                        mcp_tool_name=event.tool_name,
                        # The RESOLVED agent (what actually served the turn), not
                        # slot.agent — that is an alias resolve_agent_bindings
                        # maps to a concrete kiro agent, so it must never decide
                        # which builtin app an agent belongs to.
                        resolved_agent=read_effective_agent(client),
                    )
                    if tool_result.action == TOOL_DENY:
                        await client.reject_tool(event.request_id)
                        # Surface WHY: carry the deny reason into the pill so
                        # the user sees "Blocked by security policy: ..." rather
                        # than an opaque "(blocked)" (or, on the claude provider,
                        # a cryptic "Tool use aborted" with no explanation).
                        _deny_reason = (tool_result.reason or "blocked").strip()
                        _deny_title = _redact_display_text(event.title)
                        _deny_msg, _ = redact_exfiltration_urls(_deny_reason)
                        _deny_msg, _ = redact_credentials(_deny_msg)
                        slot.append(
                            "tool",
                            f"🚫 {_deny_title} — {_deny_msg}",
                            "msg msg-tool",
                            meta=_tool_meta(event),
                        )
                        # Broadcast a visible activity event (mirrors the
                        # auto-approve branch) so the block isn't silent.
                        state.broadcast_ws(
                            "activity_event",
                            {
                                "slot": slot.key,
                                "kind": "permission",
                                "text": f"Blocked: {_deny_title} — {_deny_msg}",
                            },
                        )
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=_deny_title,
                            tool_kind=event.tool_kind,
                            outcome="denied",
                            request_id=event.request_id,
                            error="hook_deny",
                        )
                        # Recoverable host-gate refusal: record for auto-recovery.
                        # _deny_title/_deny_msg are ALREADY redacted just above
                        # (redact_exfiltration_urls + redact_credentials) for the
                        # display pill; reuse those sanitized values so the
                        # model-bound recovery prompt never sees raw command
                        # fragments, paths, or credentials.
                        _refusal_reasons.append((_deny_title, _deny_msg))
                        continue
                    if _child_low_fidelity:
                        # Backend-subagent origin whose tool_call frames never
                        # reached us (cache miss): command bytes are absent, so
                        # every gate below would judge the LLM-authored title
                        # alone. Fail closed past all auto-approve paths — the
                        # request falls through to the interactive card (which
                        # carries the _child_lf_warning display prefix). When
                        # the child's session/update frames WERE routed (the
                        # normal case), tool_input carries the real command
                        # bytes and the child takes the exact same mode
                        # branches as the main agent below.
                        if tool_result.action == TOOL_AUTO_APPROVE:
                            logger.info(
                                "downgrading auto-approve to interactive card for "
                                "low-fidelity subagent permission request (child=%s)",
                                event.sub_session_id,
                            )
                            tool_result = ToolHookResult(action=TOOL_ALLOW)
                    if tool_result.action == TOOL_AUTO_APPROVE:
                        try:
                            validated_tool = _validate_tool_name(
                                event.title, is_shell=event.is_shell
                            )
                        except ValueError as e:
                            await _reject_invalid_tool(
                                client, slot, event, session_key=session_key, error=e
                            )
                        else:
                            # Declarative auto-approve must NOT bypass scripted
                            # PreToolUse hooks — those are the audit/policy gate
                            # and exit-2 BLOCKED takes precedence over auto-approve.
                            try:
                                _parsed_input = (
                                    json.loads(event.tool_input) if event.tool_input else None
                                )
                            except Exception:
                                _parsed_input = None
                            try:
                                pre_hook_results = await _fire(
                                    HOOK_EVENT_PRE_TOOL_USE,
                                    tool_name=validated_tool,
                                    tool_input=_parsed_input,
                                )
                            except Exception as hook_exc:
                                await _reject_hook_error(
                                    client,
                                    slot,
                                    event,
                                    session_key=session_key,
                                    error=str(hook_exc),
                                )
                                continue
                            if _pre_tool_hooks_should_block(pre_hook_results):
                                await _reject_hook_blocked(
                                    client,
                                    slot,
                                    event,
                                    session_key=session_key,
                                    pre_hook_results=pre_hook_results,
                                    refusal_reasons=_refusal_reasons,
                                )
                                continue
                            await client.approve_tool(event.request_id)
                            _tool_title = _broadcast_auto_tool(state, slot, event)
                            # Defense-in-depth: _broadcast_auto_tool already
                            # returns a redacted title, but re-redact before this
                            # second external surface (activity feed + sel log) so
                            # the guarantee is local and idempotent — event.title
                            # is LLM-controlled display text (see below).
                            _tool_title, _ = redact_exfiltration_urls(_tool_title)
                            _tool_title, _ = redact_credentials(_tool_title)
                            state.broadcast_ws(
                                "activity_event",
                                {
                                    "slot": slot.key,
                                    "kind": "permission",
                                    "text": f"Auto-approved: {_tool_title}",
                                },
                            )
                            sel().log_tool_invocation(
                                session_key=session_key,
                                agent=slot.agent or "kirocrew",
                                source="dashboard",
                                tool_name=_tool_title,
                                tool_kind=event.tool_kind,
                                outcome="auto_approved",
                                request_id=event.request_id,
                            )
                        continue
                    try:
                        validated_tool = _validate_tool_name(event.title, is_shell=event.is_shell)
                    except ValueError as e:
                        await _reject_invalid_tool(
                            client, slot, event, session_key=session_key, error=e
                        )
                        continue
                    try:
                        _parsed_input = json.loads(event.tool_input) if event.tool_input else None
                    except Exception:
                        _parsed_input = None
                    try:
                        pre_hook_results = await _fire(
                            HOOK_EVENT_PRE_TOOL_USE,
                            tool_name=validated_tool,
                            tool_input=_parsed_input,
                        )
                    except Exception as hook_exc:
                        await _reject_hook_error(
                            client, slot, event, session_key=session_key, error=str(hook_exc)
                        )
                        continue
                    if _pre_tool_hooks_should_block(pre_hook_results):
                        await _reject_hook_blocked(
                            client,
                            slot,
                            event,
                            session_key=session_key,
                            pre_hook_results=pre_hook_results,
                            refusal_reasons=_refusal_reasons,
                        )
                        continue
                    _pre_tool_hooks_fired = True
                    # Hooks passed — fall through to patterns/trust-reads/trust/yolo/interactive
                # Native crew auto-approve: when a native subagent (crew pipeline)
                # is active and the session is configured to auto-approve subagent
                # tools, approve immediately to avoid deadlocking the blocked
                # parent turn. Deny-by-default (CWE-1188): with no active crew this
                # predicate is False no matter the trust flags, so the tool falls
                # through to the normal interactive/trust gate below.
                if (
                    _native_crew_should_auto_approve(_native_tracker, state, slot)
                    and not _child_low_fidelity
                ):
                    logger.debug(
                        "Native crew auto-approve: %r (request_id=%s)",
                        _safe_native_crew_debug_title(event.title),
                        event.request_id,
                    )
                    await client.approve_tool(event.request_id)
                    _tool_title = _broadcast_auto_tool(state, slot, event)
                    # Defense-in-depth: re-redact before this second external
                    # surface (activity feed + sel log). event.title is
                    # LLM-controlled display text; _broadcast_auto_tool already
                    # redacts, so both passes are idempotent.
                    _tool_title, _ = redact_exfiltration_urls(_tool_title)
                    _tool_title, _ = redact_credentials(_tool_title)
                    state.broadcast_ws(
                        "activity_event",
                        {
                            "slot": slot.key,
                            "kind": "permission",
                            "text": f"Auto-approved (crew): {_tool_title}",
                        },
                    )
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=slot.agent or "kirocrew",
                        source="dashboard",
                        tool_name=_tool_title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        request_id=event.request_id,
                        metadata={"reason": "native_crew"},
                    )
                    continue
                # Session-trusted patterns: auto-approve commands matching user globs.
                # Security: match against the ACTUAL command from tool_input (not
                # event.title which is LLM-controlled display text). For shell tools,
                # extract the real command; for non-shell MCP tools (no tool_input),
                # use event.title as it IS the provider-controlled tool name.
                # When tool_input exists but isn't recognized as bash, skip pattern
                # matching entirely (deny-by-default).
                if slot._trusted_patterns and not _child_low_fidelity:
                    _tp_cmd = _extract_bash_command(event.tool_input) if event.tool_input else ""
                    if _tp_cmd:
                        _tp_check_title = f"Running: {_tp_cmd}"
                    elif not event.tool_input:
                        _tp_check_title = event.title
                    else:
                        _tp_check_title = None
                    matched = (
                        _matches_trusted_pattern(_tp_check_title, slot._trusted_patterns)
                        if _tp_check_title is not None
                        else None
                    )
                    if matched:
                        try:
                            validated_tool = _validate_tool_name(
                                event.title, is_shell=event.is_shell
                            )
                        except ValueError as e:
                            await client.reject_tool(event.request_id)
                            _safe = _redact_display_text(event.title)
                            slot.append(
                                "tool",
                                f"🚫 {_safe} (invalid: {e})",
                                "msg msg-tool",
                            )
                            sel().log_tool_invocation(
                                session_key=session_key,
                                agent=slot.agent or "kirocrew",
                                source="dashboard",
                                tool_name=_safe,
                                tool_kind=event.tool_kind,
                                outcome="rejected",
                                request_id=event.request_id,
                                metadata={"reason": "invalid_tool_name", "pattern": matched},
                            )
                            continue
                        await client.approve_tool(event.request_id)
                        _tool_title = _broadcast_auto_tool(state, slot, event)
                        _tool_title, _ = redact_exfiltration_urls(_tool_title)
                        _tool_title, _ = redact_credentials(_tool_title)
                        slot.append(
                            "tool",
                            f"🔧 {_tool_title}",
                            "msg msg-tool",
                            meta=(
                                {
                                    "tool_call_id": event.tool_call_id,
                                    "purpose": redact_credentials(
                                        redact_exfiltration_urls((event.tool_purpose or "")[:200])[
                                            0
                                        ]
                                    )[0],
                                }
                                if event.tool_call_id
                                else None
                            ),
                        )
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=_tool_title,
                            tool_kind=event.tool_kind,
                            outcome="auto_approved",
                            request_id=event.request_id,
                            metadata={"reason": "trusted_pattern", "pattern": matched},
                        )
                        continue
                # Browser CLI: auto-approve page-scoped `playwright-cli` verbs so
                # that browsing does not prompt on every step. Placed AFTER the
                # user's own trusted patterns (a user grant still wins and is
                # logged as such) and, like that branch, keyed on the REAL command
                # from tool_input — never event.title, which the model authors.
                # Consent is the install itself: the binary is only on PATH
                # because the user (or this dashboard, at their click) put it
                # there. Verbs that escape the page — arbitrary code, arbitrary
                # local reads/writes, installers — are excluded by allowlist and
                # still prompt.
                # `is_shell` is REQUIRED, not belt-and-braces:
                # `_extract_bash_command` reads the `command` field out of ANY
                # tool_input JSON and falls back to the raw input, so without this
                # gate a non-shell tool that happens to carry a `command` field
                # (cron_add, which can schedule a shell command) would be
                # auto-approved here — turning "browsing is allowed" into
                # "creating a durable scheduled job is allowed".
                _bc_cmd = (
                    _extract_bash_command(event.tool_input)
                    if (event.is_shell and event.tool_input and not _child_low_fidelity)
                    else ""
                )
                _bc_ok = False
                if _bc_cmd and _is_browser_cli_command(f"Running: {_bc_cmd}"):
                    # Presence IS the consent signal, so verify it rather than
                    # assuming it: with no binary on PATH the user never opted in,
                    # and a `playwright-cli` call is anomalous enough to prompt.
                    # In a thread: `available()` -> `cli_path()` ->
                    # `find_node_tool` -> `node_bin_dirs()`, which globs and stats
                    # every version-manager root (mise alone contributes ~18 dirs
                    # on a developer box). The repo already treats that call as
                    # must-not-run-on-the-loop -- see the BLOCKING note on
                    # `dev_fleet/server.py`'s own use of it. On the loop it stalls
                    # every other dashboard request and the heartbeat.
                    _bc_ok = await asyncio.to_thread(browser_cli_install.available)
                if _bc_ok:
                    try:
                        validated_tool = _validate_tool_name(event.title, is_shell=event.is_shell)
                    except ValueError as e:
                        await client.reject_tool(event.request_id)
                        _safe = _redact_display_text(event.title)
                        slot.append("tool", f"🚫 {_safe} (invalid: {e})", "msg msg-tool")
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=_safe,
                            tool_kind=event.tool_kind,
                            outcome="rejected",
                            request_id=event.request_id,
                            metadata={"reason": "invalid_tool_name", "pattern": "browser_cli"},
                        )
                        continue
                    await client.approve_tool(event.request_id)
                    _tool_title = _broadcast_auto_tool(state, slot, event)
                    _tool_title, _ = redact_exfiltration_urls(_tool_title)
                    _tool_title, _ = redact_credentials(_tool_title)
                    slot.append("tool", f"🔧 {_tool_title}", "msg msg-tool")
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=slot.agent or "kirocrew",
                        source="dashboard",
                        tool_name=_tool_title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        request_id=event.request_id,
                        metadata={"reason": "browser_cli"},
                    )
                    continue
                # Trust-reads: auto-approve read-only bash commands
                # Detect bash tools by tool_input content (title is human-readable)
                cmd = _extract_bash_command(event.tool_input) if event.tool_input else ""
                yolo_active = state.is_yolo_active()
                # Evaluated ONCE for both branches below. Two separate calls could
                # straddle a scoped grant's expiry and disagree with each other, and
                # a scope check is not a pure read — it retires a lapsed grant and
                # logs that, which must happen once per event, not twice.
                slot_trusted = _slot_is_trusted(slot)
                if (
                    slot._trust_reads
                    and not slot_trusted
                    and not yolo_active
                    and cmd
                    and not _child_low_fidelity
                ):
                    if is_read_only_bash(cmd):
                        try:
                            validated_tool = _validate_tool_name(
                                event.title, is_shell=event.is_shell
                            )
                        except ValueError as e:
                            await _reject_invalid_tool(
                                client,
                                slot,
                                event,
                                session_key=session_key,
                                error=e,
                                metadata={"reason": "trust_reads"},
                            )
                            continue
                        await client.approve_tool(event.request_id)
                        _tool_title = _broadcast_auto_tool(state, slot, event)
                        slot.append(
                            "tool",
                            f"🔧 {_tool_title}",
                            "msg msg-tool",
                            meta=_tool_meta(event),
                        )
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=_redact_display_text(event.title),
                            tool_kind=event.tool_kind,
                            outcome="auto_approved",
                            request_id=event.request_id,
                            metadata={"reason": "trust_reads"},
                        )
                        continue
                # Trust mode (per-slot) or YOLO mode (global) — auto-approve.
                # Low-fidelity child events (backend subagents whose command
                # bytes never reached the caches) are excluded from every
                # auto-approve path and fall through to the interactive card;
                # children WITH cached bytes take these branches exactly like
                # the main agent (mode parity).
                if (slot_trusted or yolo_active) and not _child_low_fidelity:
                    try:
                        validated_tool = _validate_tool_name(event.title, is_shell=event.is_shell)
                    except ValueError as e:
                        await _reject_invalid_tool(
                            client, slot, event, session_key=session_key, error=e
                        )
                        continue
                    if not _pre_tool_hooks_fired:
                        try:
                            _parsed_input = (
                                json.loads(event.tool_input) if event.tool_input else None
                            )
                        except Exception:
                            _parsed_input = None
                        try:
                            pre_hook_results = await _fire(
                                HOOK_EVENT_PRE_TOOL_USE,
                                tool_name=validated_tool,
                                tool_input=_parsed_input,
                            )
                        except Exception as hook_exc:
                            await _reject_hook_error(
                                client, slot, event, session_key=session_key, error=str(hook_exc)
                            )
                            continue
                        if _pre_tool_hooks_should_block(pre_hook_results):
                            await _reject_hook_blocked(
                                client,
                                slot,
                                event,
                                session_key=session_key,
                                pre_hook_results=pre_hook_results,
                                refusal_reasons=_refusal_reasons,
                            )
                            continue
                    # always=False — KiroCrew owns trust scope; per-call request_permission
                    # is required for PreToolUse hooks to run on every tool invocation.
                    await client.approve_tool(event.request_id)
                    _tool_title = _broadcast_auto_tool(state, slot, event)
                    # Defense-in-depth: re-redact before the sel log (idempotent).
                    _tool_title, _ = redact_exfiltration_urls(_tool_title)
                    _tool_title, _ = redact_credentials(_tool_title)
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=slot.agent or "kirocrew",
                        source="dashboard",
                        tool_name=_tool_title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        request_id=event.request_id,
                        # Provenance, so an auditor can separate a human's session
                        # trust from an unattended worker's expiring scoped grant.
                        metadata={"reason": _auto_approve_reason(slot, yolo_active)},
                    )
                    continue
                # Auto-reject remaining tools after one rejection in a batch
                if getattr(slot, "_batch_rejected", False):
                    await client.reject_tool(event.request_id)
                    _title = _redact_display_text(event.title)
                    slot.append(
                        "tool", f"🚫 {_title} (rejected)", "msg msg-tool", meta=_tool_meta(event)
                    )
                    # Mark the permission as resolved so UI shows rejection
                    perm_meta: dict[str, str] = {
                        "request_id": str(event.request_id),
                        "tool_call_id": event.tool_call_id or "",
                        "resolved": "rejected",
                    }
                    slot.append("permission", _title, json.dumps(perm_meta))
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=slot.agent or "kirocrew",
                        source="dashboard",
                        tool_name=_title,
                        tool_kind=event.tool_kind,
                        outcome="rejected",
                        request_id=event.request_id,
                        metadata={"reason": "batch_rejection"},
                    )
                    logger.warning("AUTO-REJECTED tool=%r (batch rejection)", event.title)
                    continue
                # Interactive approval — send to frontend, wait for decision
                perm_meta = {
                    "request_id": str(event.request_id),
                    "tool_call_id": event.tool_call_id or "",
                }
                if event.tool_input:
                    # Security: scan for exfiltration URLs and credentials
                    sanitized, _ = redact_exfiltration_urls(event.tool_input)
                    sanitized, _ = redact_credentials(sanitized)
                    perm_meta["tool_input"] = sanitized
                # Flag read-only bash commands for context-aware buttons
                cmd = _extract_bash_command(event.tool_input) if event.tool_input else ""
                if cmd:
                    perm_meta["is_read_only"] = "1" if is_read_only_bash(cmd) else ""
                # Pre-compute pattern fields for the TrustDropdown.
                # NOTE: derived from the UN-annotated event.title — the
                # _child_lf_warning prefix is applied to the DISPLAY text
                # only, so learned trust patterns keep meaning tool identity.
                _safe_title, _ = redact_exfiltration_urls(event.title)
                _safe_title, _ = redact_credentials(_safe_title)
                perm_meta["tool_title"] = _safe_title
                _full = _extract_full_command(event.title)
                _full, _ = redact_exfiltration_urls(_full)
                _full, _ = redact_credentials(_full)
                perm_meta["full_command"] = _full
                _base = _extract_base_command(event.title)
                _base, _ = redact_exfiltration_urls(_base)
                _base, _ = redact_credentials(_base)
                perm_meta["base_command"] = _base
                slot.append(
                    "permission",
                    f"{_child_lf_warning}{_safe_title}" if _child_lf_warning else _safe_title,
                    json.dumps(perm_meta),
                )
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[str] = loop.create_future()
                slot._approval_futures[str(event.request_id)] = fut
                # Push via global SSE AFTER registering the future, so the
                # slot dict reflects pending_approval=true and Board cards
                # move into the Blocked lane without a browser refresh.
                state.push_slots_update()
                # Mirror the prompt to the linked Slack thread so a user driving
                # this session from Slack can actually answer it. Without this,
                # the prompt only renders in the dashboard and a Slack-only user
                # never sees it — the turn then parks here for the whole approval
                # window, holding the slot lock and silently dropping inbound
                # messages.
                # The Slack click resolves THIS future (via state.resolve_approval);
                # we remain the sole caller of approve_tool/reject_tool below.
                _slack_approval_ts: str | None = None
                if (
                    slot._slack_linked
                    and slot._slack_channel
                    and slot._slack_thread_ts
                    and state.slack_client
                    # An approval prompt is turn output too, and it asks for a
                    # decision. Posting one into a thread the user disconnected
                    # would solicit an answer where they are no longer looking;
                    # the dashboard carries the same prompt.
                    and not slack_mirror_is_paused(state, session_key)
                ):
                    try:
                        _slack_approval_ts = await post_linked_approval(
                            state.slack_client,
                            slot._slack_channel,
                            slot._slack_thread_ts,
                            event.request_id,
                            session_key,
                            (
                                f"{_child_lf_warning}{event.title}"
                                if _child_lf_warning
                                else event.title
                            ),
                            event.tool_input or "",
                        )
                        if _slack_approval_ts is None:
                            # Delivery failed — do not park for the whole
                            # approval window.
                            # Resolve the future now (reject) and tell the user
                            # the prompt could not be delivered, so they retry
                            # rather than wait. The dashboard still rendered the
                            # prompt, so a dashboard user could also answer; but
                            # resolving keeps a Slack-only user from being stuck.
                            logger.warning(
                                "Linked approval delivery to Slack failed; auto-rejecting tool %r",
                                event.title,
                            )
                            slot.append(
                                "assistant",
                                "\u26a0\ufe0f A tool approval was required but I couldn't post it to "
                                "this Slack thread, so I auto-declined it. Please retry, or "
                                "approve from the dashboard.",
                                "msg msg-a",
                            )
                            state.push_slots_update()
                            if not fut.done():
                                fut.set_result("rejected")
                    except Exception:
                        # Any failure before the future is resolved (ImportError,
                        # post_linked_approval raising, slot.append/push raising in
                        # the delivery-failure branch) would otherwise fall through
                        # to the approval wait_for with an unresolved future — the exact
                        # wedge this fix prevents. Auto-reject so the turn unblocks,
                        # mirroring the _slack_approval_ts is None branch.
                        logger.warning("Error mirroring approval prompt to Slack", exc_info=True)
                        if not fut.done():
                            fut.set_result("rejected")
                # Pre-seeded so the `finally` backstop below is total over EVERY
                # exit from the await — including CancelledError, which slot
                # deletion / cleanup endpoints raise by cancelling slot.task.
                # Assigning only inside try/except would leave `outcome` unbound
                # on that path: the finally would raise UnboundLocalError,
                # replacing the CancelledError with a spurious exception and
                # skipping both the message marking and the Slack cleanup —
                # reintroducing the orphan-card bug on the cancel path.
                # "rejected" is the correct reading: a cancelled turn never
                # obtained consent.
                outcome = "rejected"
                # Per-SLOT, not the global config: `approval_timeout_for` is what
                # gives an app-owned worker with no human responder the background
                # deny-fast instead of parking for the attended window and being
                # denied anyway. See DashboardState.approval_timeout_for.
                #
                # But it is only ONE of the two bounds. `approval_timeout_for`
                # returns a flat constant, so on its own it can outlive the turn
                # it belongs to: the attended 7200s dwarfs the configurable
                # `tool_approval_timeout_secs()` (600s by default), and neither is
                # clamped to what is LEFT of a long agentic turn. The outer
                # `_bounded_turn` then cancels first and the timeout branch below
                # never runs — no card, no decline line, just a turn that dies
                # mid-prompt. Taking the MINIMUM keeps both properties: the
                # unattended deny-fast, and the double bound (ceiling and
                # remaining budget) that `tool_approval_timeout_secs` applies —
                # including its 0.0, which is what the no-budget branch reads.
                _approval_window = min(
                    state.approval_timeout_for(slot), tool_approval_timeout_secs()
                )
                _unattended_wait = slot.unattended
                _approval_card: str | None = None
                try:
                    if _approval_window <= 0:
                        # Too little of the turn left to both wait and report.
                        # Waiting anyway guarantees the ceiling fires first and
                        # relabels the unanswered approval as a turn timeout.
                        logger.warning(
                            "Declining approval for %r without waiting: no turn budget left",
                            event.title,
                        )
                        _approval_card = format_approval_no_budget_card()
                    else:
                        outcome = await asyncio.wait_for(fut, timeout=_approval_window)
                except asyncio.TimeoutError:
                    outcome = "rejected"
                    # Name the real cause. An unanswered prompt used to be
                    # indistinguishable from a generic turn timeout, because the
                    # window outlived the turn ceiling and the turn always died
                    # first — so an unattended run burned the full ceiling and
                    # the user was never told an approval was waiting.
                    logger.warning(
                        "Tool approval for %r went unanswered for %.0fs; declining",
                        event.title,
                        _approval_window,
                    )
                    _approval_card = format_approval_timeout_card(_approval_window)
                    if _unattended_wait:
                        # The card is for the human; this line is for the AGENT.
                        # A denial it cannot read makes it retry the same tool
                        # forever, because nothing in its transcript explains the
                        # refusal.
                        slot.append(
                            "assistant",
                            "\u26a0\ufe0f A tool needed approval and no one answered within "
                            f"{int(_approval_window)}s, so it was declined. This session is "
                            "running unattended — ask for the permission you need instead of "
                            "retrying the same call.",
                            "msg msg-a",
                        )
                    # Tell any monitoring loop bound to this slot that a cycle
                    # could not obtain approval. This branch IS the evidence a
                    # reactive stop needs: the prompt ran its full window with no
                    # decision, which an auto-approved tool never reaches. The
                    # loop stops on its next wake instead of spending the rest of
                    # its cap on cycles that cannot act. Best-effort and
                    # non-blocking — a monitoring convenience must never change
                    # how this turn's denial is reported.
                    try:
                        from kiro_crew.autonudge import (
                            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_runner
                        )

                        _autonudge = _autonudge_get()
                        if _autonudge is not None:
                            _autonudge.notify_approval_stalled(slot.key)
                    except Exception:
                        logger.debug("autonudge.notify_approval_stalled failed", exc_info=True)
                finally:
                    if _approval_card is not None:
                        try:
                            slot.append("error", _approval_card, "msg msg-err")
                        except Exception:
                            logger.debug("Failed to render approval card", exc_info=True)
                    slot._approval_futures.pop(str(event.request_id), None)
                    # Backstop: the future is now gone, so the permission
                    # message MUST NOT be left reading pending — the UI would
                    # keep rendering an approval bar whose every button answers
                    # 404, and a history reload would resurrect it. The primary
                    # resolvers (HTTP slot-approve, Slack click) already mark it
                    # and record richer decisions like "trust"/"yolo", so only
                    # write when still pending. This is the sole marker for the
                    # paths that resolve the future in-process: the approval
                    # timeout above (2h attended / 180s unattended) and the
                    # Slack-delivery auto-reject branches.
                    _approved = outcome in ("approved", "approved_trust_reads")
                    if _mark_permission_resolved(
                        slot.messages,
                        str(event.request_id),
                        "approved" if _approved else "rejected",
                        only_if_pending=True,
                    ):
                        slot._dirty = True
                        state.broadcast_ws(
                            "approval_resolved",
                            {
                                "id": str(event.request_id),
                                "approved": _approved,
                                # Keys the frame for the slot-scoped WS gate.
                                "slot": slot.key,
                            },
                        )
                        state.push_slots_update()
                    # Clean up the Slack prompt: remove the registry entry and
                    # delete the buttons message now the decision is in.
                    if _slack_approval_ts is not None:
                        try:
                            resolve_linked_approval(slot._slack_channel, _slack_approval_ts)
                            if state.slack_client:
                                await state.slack_client.delete_message(
                                    slot._slack_channel, _slack_approval_ts
                                )
                        except Exception:
                            logger.debug(
                                "Failed to clean up linked Slack approval message",
                                exc_info=True,
                            )
                if outcome == "approved_trust_reads":
                    slot._trust_reads = True
                    outcome = "approved"
                if outcome == "approved":
                    try:
                        validated_tool = _validate_tool_name(event.title, is_shell=event.is_shell)
                    except ValueError as e:
                        await _reject_invalid_tool(
                            client,
                            slot,
                            event,
                            session_key=session_key,
                            error=e,
                            metadata={"reason": "interactive"},
                        )
                        break
                    try:
                        _parsed_input = json.loads(event.tool_input) if event.tool_input else None
                    except Exception:
                        _parsed_input = None
                    try:
                        pre_hook_results = await _fire(
                            HOOK_EVENT_PRE_TOOL_USE,
                            tool_name=validated_tool,
                            tool_input=_parsed_input,
                        )
                    except Exception as hook_exc:
                        await _reject_hook_error(
                            client,
                            slot,
                            event,
                            session_key=session_key,
                            error=str(hook_exc),
                            metadata={"reason": "interactive"},
                        )
                        break
                    if _pre_tool_hooks_should_block(pre_hook_results):
                        await _reject_hook_blocked(
                            client,
                            slot,
                            event,
                            session_key=session_key,
                            pre_hook_results=pre_hook_results,
                            refusal_reasons=_refusal_reasons,
                            metadata={"reason": "interactive"},
                        )
                    else:
                        await client.approve_tool(event.request_id)
                        _approved_title = _redact_display_text(event.title)
                        slot.append(
                            "tool", f"✅ {_approved_title}", "msg msg-tool", meta=_tool_meta(event)
                        )
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=_approved_title,
                            tool_kind=event.tool_kind,
                            outcome="approved",
                            request_id=event.request_id,
                            metadata={"reason": "interactive"},
                        )
                else:
                    await client.reject_tool(event.request_id)
                    # Explain WHY when the command tripped the read-only safety
                    # gate, so the pill reads "Cancelled due to unsafe shell
                    # pattern …" instead of the bare adapter default.
                    # unsafe_bash_reason() embeds fragments of the LLM-supplied
                    # command (base name / pipe target), so redact it — and the
                    # title — before it reaches the dashboard or SEL metadata.
                    _safety_reason = unsafe_bash_reason(cmd) if cmd else ""
                    if _safety_reason:
                        _safety_reason, _ = redact_exfiltration_urls(_safety_reason)
                        _safety_reason, _ = redact_credentials(_safety_reason)
                    _safe_reject_title, _ = redact_exfiltration_urls(event.title)
                    _safe_reject_title, _ = redact_credentials(_safe_reject_title)
                    _reject_label = (
                        f"🚫 {_safe_reject_title} (cancelled — {_safety_reason})"
                        if _safety_reason
                        else f"🚫 {_safe_reject_title} (rejected)"
                    )
                    slot.append("tool", _reject_label, "msg msg-tool")
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=slot.agent or "kirocrew",
                        source="dashboard",
                        tool_name=_safe_reject_title,
                        tool_kind=event.tool_kind,
                        outcome="rejected",
                        request_id=event.request_id,
                        metadata={"reason": _safety_reason or "interactive"},
                    )
                    # NOTE: Do NOT append to _refusal_reasons here.
                    # This is an interactive user denial — the user chose to reject
                    # the tool. Refusal-recovery is only for system-side blocks —
                    # the hook-deny (TOOL_DENY) path, which is the other site that
                    # appends to _refusal_reasons.

                if outcome != "approved":
                    # mark batch_rejected as true and continue loop instead of breaking
                    # This will allow for marking other batched approval requests as rejected too
                    slot._batch_rejected = True
                    logger.warning(
                        "PERM REJECTED tool=%r outcome=%r — auto-rejecting remaining batch",
                        event.title,
                        outcome,
                    )
                    continue
            elif event.kind == EVENT_STEER_CONSUMED:
                _settle_consumed_steers(slot, event.text or "")
            elif event.kind == EVENT_COMPACTION_STATUS:
                logger.debug("Main loop: compaction event text=%r", event.text)
                if _broadcast_compaction_result(state, slot, event):
                    saw_compaction = True
                    _produced_visible_output = True
                    assistant_text = ""
                    _wsred.reset()
            elif event.kind == EVENT_CLEAR_STATUS:
                slot.messages.clear()
                # The boundary was captured against the pre-clear message
                # count; the list is now empty, so reset it to 0 or the
                # clear-confirmation appended below would fall outside the
                # turn-stats scan slice and the completed turn would drop its
                # elapsed/credits stats.
                _turn_msg_boundary = 0
                assistant_text = ""
                _wsred.reset()
                _produced_visible_output = True
                slot.append("assistant", "🗑️ Conversation cleared.", "msg msg-a")
                state.broadcast_ws("slot_clear", {"slot": slot.key})
                state.broadcast_ws(
                    "chat_message",
                    {"slot": slot.key, "role": "assistant", "content": "🗑️ Conversation cleared."},
                )
            elif event.kind == EVENT_AGENT_SWITCHED:
                new_agent, _ = redact_credentials(event.text)
                new_agent, _ = redact_exfiltration_urls(new_agent)
                if new_agent:
                    slot.agent = new_agent
                    assistant_text = ""
                    _wsred.reset()
                    _produced_visible_output = True
                    slot.append(
                        "assistant",
                        f"🔄 Switched to agent: {new_agent}",
                        "msg msg-a",
                    )
                    state.broadcast_ws(
                        "slot_agent_switch",
                        {"slot": slot.key, "agent": new_agent},
                    )
                    needs_session_reset = True
            elif event.kind == EVENT_MCP_OAUTH_REQUEST:
                # kiro-cli emits this notification when an MCP server's token
                # has expired or never existed. Surface as an inline banner —
                # kiro-cli's local callback handles the rest of the OAuth flow.
                _emit_mcp_oauth_request(state, slot, event.server_name, event.oauth_url)
            elif event.kind == EVENT_MCP_SERVER_INITIALIZED:
                # kiro-cli emits this once an MCP server has finished init
                # (typically right after a successful OAuth callback completes).
                # Patch the matching mcp_oauth banner so the user sees a
                # confirmation instead of a stale "Authorize" prompt.
                _mark_mcp_oauth_completed(state, slot, event.server_name, success=True)
            elif event.kind == EVENT_MCP_SERVER_INIT_FAILURE:
                _mark_mcp_oauth_completed(
                    state, slot, event.server_name, success=False, error=event.text or ""
                )
            elif event.kind == EVENT_TODO_UPDATE:
                # Agent's own TODO list. Store on the slot (so /api/chat/slots
                # and the WS `slots` snapshot rehydrate it after a reconnect),
                # then push a lightweight delta so the pill updates mid-turn
                # instead of waiting for the next full slots snapshot.
                if slot.set_todo(event.todo):
                    state.broadcast_ws(
                        "todo_update",
                        {"slot": slot.key, "todo": slot.todo_payload()},
                    )
            elif event.kind == EVENT_SUBAGENT_LIST:
                # kiro-cli per-subagent state (native use_subagent crews).
                # Reconcile one Activity card per sub-agent (spawn/done).
                logger.debug(
                    "EVENT_SUBAGENT_LIST: %s subagents, slot=%s",
                    len(event.subagents or []),
                    slot.key,
                )
                _native_subagent_sync(
                    state, slot, event.subagents, _native_tracker, _native_card_output
                )
            elif event.kind == EVENT_SUBAGENT_ACTIVITY:
                # kiro-cli's _kiro.dev/session/update tags a sub-agent's inner
                # tool call with its sessionId. This ALWAYS arrives before the
                # corresponding flat tool_call/tool_call_update, so building the
                # toolCallId->card map here lets those flat events (which carry
                # the full tool title AND the real output) attribute to the
                # right sub-agent card.
                _sid = event.sub_session_id
                if _sid in _native_tracker and event.tool_call_id:
                    _native_tc_card[event.tool_call_id] = f"native:{_redact_tool_field(_sid)}"
                # Permission-rejection notices (the handle's own "⛔ …" lines,
                # e.g. drain-time rejects yielded at turn start) arrive BEFORE
                # any subagent_list populates the per-turn tracker — dropping
                # them here would leave the user watching a child tool fail
                # with no explanation. When the card cannot exist yet, persist
                # the explanation as a slot notice instead.
                if _sid not in _native_tracker and event.text and event.text.startswith("⛔"):
                    _txt, _ = redact_exfiltration_urls(event.text)
                    _txt, _ = redact_credentials(_txt)
                    slot.append("notice", _txt, "msg msg-info")
                # Some kiro-cli builds also stream the sub-agent's own text via
                # agent_message_chunk on this channel — surface it on the card.
                if _sid in _native_tracker and event.text:
                    _card_id = f"native:{_redact_tool_field(_sid)}"
                    _txt, _ = redact_exfiltration_urls(event.text)
                    _txt, _ = redact_credentials(_txt)
                    _native_card_output_len[_card_id] = _append_native_output(
                        _native_card_output.setdefault(_card_id, []),
                        _txt,
                        _native_card_output_len.get(_card_id, 0),
                    )
                    state.broadcast_ws(
                        "subagent_chunk",
                        {"id": _card_id, "slot": slot.key, "text": _txt},
                    )
            elif event.kind == EVENT_COMPLETE:
                # Hang-attribution snapshot BEFORE the close-all safety net
                # below force-marks every card done: only children still
                # unfinished at the cut may count toward timeout attribution
                # (terminal entries linger in the tracker for reconnect
                # replay and would corrupt the series).
                _children_unfinished = any(not _i.get("done") for _i in _native_tracker.values())
                # Safety net: complete any native subagent cards still marked
                # running at turn end (in case a terminal status was missed),
                # so cards don't stay stuck "running".
                _native_subagent_close_all(state, slot, _native_tracker, _native_card_output)
                _u = event.usage
                # Capture per-turn stats for the assistant-message footer.
                # Prefer the provider-reported duration (claude_code) over the
                # local wall clock (kiro/acp reports duration_ms=0).
                try:
                    _turn_elapsed_ms = int(_u.duration_ms or (time.monotonic() - _turn_t0) * 1000)
                    _turn_credits = float(_u.credits or 0.0)
                    _turn_cost_usd = float(_u.cost_usd or 0.0)
                except (TypeError, ValueError):
                    _turn_elapsed_ms = int((time.monotonic() - _turn_t0) * 1000)
                # Model attribution for the footer. read_turn_model reports the
                # id the backend actually served, or the bare "auto" when the
                # turn was handed to Auto and no concrete id came back — the
                # two are different facts and a blank footer conflates them
                # with a missing measurement. Still never guesses: an
                # unattributable turn stays "" and the footer omits the field.
                _turn_model = read_turn_model(client)
                if _u.input_tokens or _u.output_tokens or _u.credits:
                    try:
                        _provider_name = cfg.agent.provider  # type: ignore[possibly-undefined]
                    except (NameError, AttributeError):
                        _provider_name = ""
                    # Late backfill: CC reports model only via the `init`
                    # system event which arrives after the run starts, so
                    # slot.model may still be empty here even though the
                    # provider learned the model mid-turn. Read it back
                    # before persisting so tokens.jsonl is never tagged
                    # with a blank model for CC sessions.
                    _record_model = slot.model
                    if not _record_model:
                        _canonical = _backfill_canonical_model(client, _provider_name)
                        if _canonical:
                            slot.model = _canonical
                            _record_model = _canonical
                    # Read context-window occupancy off the same `client`
                    # used above (mirrors _context_usage_payload's accessor
                    # pattern); read_context_tokens never raises.
                    _ctx_used, _ctx_window = read_context_tokens(client)
                    await persist_token_record_async(
                        slot.key,
                        _record_model,
                        event,
                        provider=_provider_name,
                        # The row stays keyed by its dashboard slot for title and
                        # navigation joins; source follows the session the turn
                        # actually ran on, including linked channel sessions.
                        surface=telemetry_channel_of(session_key),
                        # Resolved agent, not the slot alias: resolve_agent_bindings
                        # maps e.g. "default" to "kirocrew" before dispatch, so the
                        # alias would credit an agent that never ran.
                        agent=read_effective_agent(client) or slot.agent or "",
                        context_used=_ctx_used,
                        context_window=_ctx_window,
                        ctx_blocks=slot_ctx_blocks,
                        phase=slot_ctx_phase,
                        # Same wall clock the turn-duration histogram below is
                        # given, so the row store and the histogram can never
                        # disagree about one turn. acp reports 0 here.
                        elapsed_ms=_turn_elapsed_ms,
                        model_source=client,
                    )
                # ── Turn-completion histogram (OTel M2) ──
                # kirocrew.turn.duration → turn latency p50/p90 + fault rate.
                # elapsed_ms carries the wall clock computed above because acp
                # leaves usage.duration_ms at 0 — without it this histogram is
                # never emitted for the default backend.
                # ``exhausted`` mirrors the stop-reason branches below: the
                # recovery-outcome exclusion from fault_rate is earned only by
                # a turn that is actually re-driven in place, so a stall takes
                # the terminal stall_exhausted label when its 3-attempt budget
                # is already spent ("Session stuck") OR when it is a NESTED
                # turn (depth > 0), which the branches below never re-queue —
                # it dies with "please retry", a user-visible fault that must
                # reach fault_rate.
                if event.stop_reason == STOP_REASON_STALE_RECOVER:
                    _turn_exhausted = _prompt_depth > 0 or slot._stale_recovery_retries >= 3
                elif event.stop_reason == STOP_REASON_TOOL_STALL:
                    _turn_exhausted = _prompt_depth > 0 or slot._tool_stall_retries >= 3
                else:
                    _turn_exhausted = False
                _emit_turn_metric(
                    event.usage.duration_ms,
                    event.stop_reason,
                    slot.key,
                    elapsed_ms=_turn_elapsed_ms,
                    exhausted=_turn_exhausted,
                )
                if "timeout" in (event.stop_reason or ""):
                    # Hang-resilience series: attribute the CAUSE of a turn
                    # timeout (the 2h-ceiling hang class). Both attrs are
                    # booleans read defensively from live state — the inner
                    # client may be an AcpClient (flag on itself) or an
                    # AcpSessionProvider (flag on its handle).
                    _ac = slot._acp_client
                    _awaiting = bool(
                        getattr(_ac, "_awaiting_permission", False)
                        or getattr(getattr(_ac, "_handle", None), "_awaiting_permission", False)
                    )
                    emit_counter(
                        TURN_TIMEOUT_CAUSE,
                        {
                            "path": "provider_timeout",
                            "awaiting_permission": _awaiting,
                            "children_announced": _children_unfinished,
                        },
                    )
                _stop_reason = event.stop_reason
                # Recorded on the slot so post-turn consumers reached later
                # (which do not receive the event) can tell a turn that really
                # finished from one cut short by a timeout, cancel or stall.
                slot._last_stop_reason = _stop_reason or ""
                if _stop_reason == STOP_REASON_TOOL_STALL:
                    _stall_tool_title = event.title
                    _stall_command = event.tool_input
                    _stall_evidence = event.text
                if (
                    _stop_reason
                    and _stop_reason != STOP_REASON_END_TURN
                    and _stop_reason != STOP_REASON_CANCELLED
                    and _stop_reason != STOP_REASON_STALE_RECOVER
                    and _stop_reason != STOP_REASON_TOOL_STALL
                ):
                    logger.warning(
                        "Unexpected stop_reason %r for slot %s",
                        _stop_reason,
                        slot.key,
                    )
                break

        # Turn stream ended: flush any withheld thinking tail (a thinking-final
        # turn never hit the loop-top flush for a following non-thinking event).
        _flush_thinking_stream()

        # Auto-recover a genuinely-wedged turn. The ACP layer probed a stale turn
        # via session/cancel and got no ack within the grace window — a confirmed
        # wedge (a done-but-missing-frame turn would have acked and completed
        # normally). Reset the session (kill the wedged runtime + session/load
        # resume in the finally) and re-queue a continue-nudge so the turn
        # finishes IN PLACE, on this same slot, with NO user message required —
        # the finally's dequeue re-dispatches it against the resumed session,
        # which restores the prior committed work so the model continues rather
        # than restarts. Bounded so a permanently-broken session surfaces a clean
        # "start a new chat" instead of looping. Complementary to a companion guard,
        # which surfaces the stuck sessions this cannot recover.
        if _stop_reason == STOP_REASON_STALE_RECOVER:
            needs_session_reset = True  # checked in finally block (reset + resume)

            def _emit_stale(msg: str) -> None:
                slot.append("error", msg, "msg msg-err")
                state.broadcast_ws(
                    "chat_message",
                    {"slot": slot.key, "role": "error", "content": msg},
                )

            if _prompt_depth == 0 and slot._stale_recovery_retries < 3:
                slot._stale_recovery_retries += 1
                slot.queue_insert(
                    0,
                    f"{STALE_RECOVERY_PREFIX}\n{build_stale_recovery_prompt()}",
                    kind=SYNTHETIC_RECOVERY_KIND,
                    payload=RecoveryPayload.CONTINUATION,
                )
                _emit_stale("⟳ Recovering a stalled turn…")
            elif slot._stale_recovery_retries >= 3:
                # Budget exhausted — terminal for this slot until a turn
                # actually completes. The budget is deliberately NOT reset
                # here: zeroing it would re-arm a fresh 3-attempt recovery
                # cycle on the next stall of a permanently wedged slot
                # (recover→exhaust looping forever). Telemetry dedup is the
                # emitted flag's job instead: emit exhausted once per cycle,
                # and the flag also blocks a later "recovered" mis-emit.
                if not slot._stale_recovery_exhausted_emitted:
                    _emit_recovery_outcome(
                        "stale_recover", "exhausted", slot._stale_recovery_retries
                    )
                    slot._stale_recovery_exhausted_emitted = True
                _emit_stale("Session stuck — please start a new chat.")
            else:
                # depth>0 (nested turn) with budget remaining: reset the session
                # but don't re-queue (mirrors the pipe-death depth>0 handling);
                # surface feedback so the nested turn doesn't fail silently.
                _emit_stale("⟳ Turn stalled — please retry.")
            return

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

            def _emit_stall(msg: str) -> None:
                slot.append("error", msg, "msg msg-err")
                state.broadcast_ws(
                    "chat_message",
                    {"slot": slot.key, "role": "error", "content": msg},
                )

            _idle_m = re.search(r"idle_secs=(\d+)", _stall_evidence or "")
            _idle_secs = int(_idle_m.group(1)) if _idle_m else 0
            _stuck = "stuck_input" in (_stall_evidence or "")
            if _prompt_depth == 0 and slot._tool_stall_retries < 3:
                slot._tool_stall_retries += 1
                _body = build_tool_stall_recovery_prompt(
                    _stall_tool_title,
                    _idle_secs,
                    command=_stall_command,
                    stuck_input=_stuck,
                )
                slot.queue_insert(
                    0,
                    f"{TOOL_STALL_RECOVERY_PREFIX}\n{_body}",
                    kind=SYNTHETIC_RECOVERY_KIND,
                    payload=RecoveryPayload.CONTINUATION,
                )
                _emit_stall("⟳ Tool appeared stalled — recovering…")
            elif slot._tool_stall_retries >= 3:
                # Budget exhausted — mirrors the stale_recover branch above:
                # budget left alone (a wedged slot must not re-enter a fresh
                # recovery cycle); the emitted flag dedups the metric and
                # blocks a later "recovered" mis-emit.
                if not slot._tool_stall_exhausted_emitted:
                    _emit_recovery_outcome("tool_stall", "exhausted", slot._tool_stall_retries)
                    slot._tool_stall_exhausted_emitted = True
                _emit_stall("Session stuck — please start a new chat.")
            else:
                _emit_stall("⟳ Tool appeared stalled — please retry.")
            return

        # CC process died mid-turn: re-queue message for automatic retry
        # (mirrors AcpProcessDied handling). Eager reconnect in the provider
        # restores MCPs in background; re-queue ensures the user's message
        # is not silently dropped.
        if _stop_reason and _stop_reason.startswith("error:"):
            _rc = getattr(client, "exit_code", None)
            _rc_suffix = f" (exit {_rc})" if _rc is not None else ""

            def _emit_error(msg: str) -> None:
                slot.append("error", msg, "msg msg-err")
                state.broadcast_ws(
                    "chat_message",
                    {"slot": slot.key, "role": "error", "content": msg},
                )

            if _prompt_depth == 0 and slot._acp_pipe_death_retries < 3:
                slot._acp_pipe_death_retries += 1
                _requeue_text, _requeue_payload = build_recovery_requeue(
                    message,
                    _turn_emitted,
                    cause=ResetCause.CONNECTION_LOST,
                    message_is_synthetic=_is_synthetic,
                )
                slot.queue_insert(
                    0,
                    _requeue_text,
                    kind=SYNTHETIC_RECOVERY_KIND,
                    payload=_requeue_payload,
                )
                _emit_error(f"⟳ Connection lost{_rc_suffix} — retrying...")
            elif slot._acp_pipe_death_retries >= 3:
                _emit_error(f"Session stuck{_rc_suffix} — please start a new chat.")
            else:
                _emit_error(f"⟳ Connection lost{_rc_suffix} — please retry.")
            return

        # /compact acknowledged but compaction deferred — send a lightweight
        # follow-up to trigger the actual compaction so the user doesn't have to.
        logger.debug(
            "Compaction check: first_word=%r saw_compaction=%s", first_word, saw_compaction
        )
        if first_word == "/compact" and not saw_compaction:
            # Clear streamed "Compacting conversation..." text from kiro-cli
            # (claude-agent-acp doesn't stream that, but the cleanup is harmless).
            slot.purge_chunks()
            assistant_text = ""
            _wsred.reset()
            _produced_visible_output = True
            state.broadcast_ws("chat_done", {"slot": slot.key})

            # claude-agent-acp performs /compact synchronously inside session/prompt;
            # there is no out-of-band _kiro.dev/compaction/status notification, so
            # EVENT_COMPLETE is the done signal. Skip the kiro-only async wait.
            #
            # Note: the success message is hardcoded so no redaction pass is
            # needed today. If claude-agent-acp ever returns a compaction
            # summary (e.g. via EVENT_COMPLETE payload growing a `summary`
            # field), pipe it through redact_credentials + redact_exfiltration_urls
            # before interpolation — matching the kiro-cli path below.
            if is_claude_backend(client):
                msg = "✅ Conversation compacted."
                _append_compaction_notice(state, slot, msg)
                state.broadcast_context_usage(slot.key, _context_usage_payload(slot.key, client))
            else:
                # Tell frontend to show compacting state and disable input
                logger.info("Deferred compaction: waiting for compaction result")
                state.broadcast_ws(
                    "chat_message",
                    {"slot": slot.key, "role": "compacting", "content": ""},
                )
                # kiro-cli fires compaction asynchronously after EVENT_COMPLETE —
                # just wait for the result without sending another prompt.
                compaction_result = await client.wait_for_compaction()
                logger.info("Deferred compaction result: %s", compaction_result)
                if compaction_result["type"] == "completed":
                    summary, _ = redact_credentials(compaction_result.get("summary", ""))
                    summary, _ = redact_exfiltration_urls(summary)
                    msg = (
                        f"✅ Conversation compacted: {summary}"
                        if summary
                        else "✅ Conversation compacted."
                    )
                elif compaction_result["type"] == "failed":
                    # The provider ships the reason on `failed` too, and every
                    # other surface already tells the user what it was — Slack,
                    # Telegram, Discord, and this dashboard's own AUTO-compact
                    # notice (see chat_utils._compaction_notice_text). Dropping
                    # it here left the one path a user takes deliberately as the
                    # only one that says nothing, so a `/compact` that fails
                    # because the conversation is too large is indistinguishable
                    # from one that failed because the backend was unreachable —
                    # and the user's next move differs in those two cases.
                    #
                    # Redacted with the same pair as the completed branch above:
                    # the text is backend-echoed, so it is not trusted to be
                    # free of credentials or exfiltration URLs even though the
                    # provider already redacts once at its own boundary.
                    error, _ = redact_credentials(compaction_result.get("summary", ""))
                    error, _ = redact_exfiltration_urls(error)
                    error = error.strip()
                    if len(error) > _COMPACT_FAIL_REASON_MAX_CHARS:
                        # A notice is a one-line receipt, not a log: a provider
                        # that echoes a wall of text (a stack trace, a dumped
                        # payload) would otherwise push the whole transcript out
                        # of the reader's view.
                        error = error[:_COMPACT_FAIL_REASON_MAX_CHARS].rstrip() + "…"
                    msg = f"❌ Compaction failed: {error}" if error else "❌ Compaction failed."
                else:
                    msg = "⚠️ Compaction timed out."
                _append_compaction_notice(state, slot, msg)
                # Update the context meter from the provider's post-compaction
                # state. On success the provider has dropped its stale counts by
                # the time the completed status arrives (reset_after_compaction),
                # and wait_for_compaction grace-drains for kiro's fresh
                # post-compaction metadata (~1s after the status), which
                # re-derives REAL numbers against the kept served window.
                # _context_usage_payload ships those accurate counts when the
                # metadata has landed, and otherwise a `reset` frame (used == 0)
                # carrying whatever pct the provider currently reports, so the
                # frontend drops its stale counts and the meter self-corrects on
                # the next turn. On failure/timeout `used` is unchanged and still
                # valid, so the same call re-sends the real counts as-is.
                state.broadcast_context_usage(slot.key, _context_usage_payload(slot.key, client))

        if assistant_text:
            # ── Plan format validation (planning turn only) ─────
            # `_orch_planning` excludes stage-execution turns, so a stage turn
            # whose output contains plan-like text can never re-arm/re-count.
            if _orch_planning:

                has_plan, valid, issues = validate_plan_format(assistant_text)
                if not has_plan and looks_like_plan(assistant_text):
                    # Cheap regex thinks it's a plan — let LLM confirm/reformat
                    logger.info(
                        "Detected plan-like response without header, asking LLM to reformat"
                    )
                    issues = [
                        "No '📋 Plan for:' header",
                        "No 'Stage N:' lines found",
                        "Missing [OPTION: Go | Go All | Cancel] footer",
                    ]
                    rephrased = await _rephrase_plan_lite(
                        state,
                        assistant_text,
                        issues,
                        might_not_be_plan=True,
                    )
                    if rephrased:
                        has_plan = True
                        _, valid, issues = validate_plan_format(rephrased)
                        if valid:
                            logger.info("LLM reformatted plan-like response into valid plan")
                            assistant_text = rephrased
                if has_plan and not valid:
                    logger.info("Plan format invalid (%s), attempting rephrase", issues)
                    rephrased = await _rephrase_plan_lite(state, assistant_text, issues)
                    if rephrased:
                        _, valid2, issues2 = validate_plan_format(rephrased)
                        if valid2:
                            logger.info("Plan rephrased successfully")
                            assistant_text = rephrased
                        else:
                            logger.warning("Rephrase still invalid (%s), stripping plan", issues2)
                            assistant_text = strip_plan_markers(assistant_text)
                            has_plan = False
                    else:
                        logger.warning("Rephrase failed, stripping plan markers")
                        assistant_text = strip_plan_markers(assistant_text)
                        has_plan = False
                if has_plan:
                    _armed_final = True
                    _reset_auto_run_for_new_plan(slot)
                    assistant_text = ensure_go_all_option(assistant_text)
                    # Store stage count for _stage_loop
                    slot._stage_titles, slot._plan_goal, slot._stage_descriptions = (
                        _extract_and_redact_plan_metadata(assistant_text)
                    )
            _flush_text_stream()
            _flush_segment(state, slot, assistant_text, broadcast=False)
        elif _stop_reason == STOP_REASON_REFUSAL:
            # Model-side content refusal (kiro-cli passes Anthropic's `refusal`
            # stop reason through verbatim) with no accompanying text. This is
            # DETERMINISTIC — a blind retry just re-hits the same refusal and
            # burns credits — so surface a distinct, non-retried card. A refusal
            # on turn 1 with zero tool calls and no visible output usually points
            # at what we PREPENDED (persona / injected context / replay), not the
            # user's text; log the redacted prompt head + turn shape at WARNING.
            _refusal_head, _ = redact_exfiltration_urls(full_message[:600])
            _refusal_head, _ = redact_credentials(_refusal_head)
            logger.warning(
                "Model refusal for slot %s — not retrying "
                "[is_new=%s resumed=%s tool_calls=%d visible_output=%s "
                "prompt_bytes=%d prompt_head=%r]",
                slot.key,
                is_new,
                resumed,
                _turn_tool_calls,
                _produced_visible_output,
                len(full_message),
                _refusal_head,
            )
            slot.append(
                "error",
                "Response declined by the model. Try rephrasing your request.",
                "msg msg-err",
            )
        elif (
            _stop_reason != STOP_REASON_CANCELLED
            and not _produced_visible_output
            and not _refusal_reasons
        ):
            # Model returned an empty response — retry once, then notify user.
            # Precedence: a turn that ended on a recoverable tool refusal also has
            # empty assistant_text when the model went straight to the blocked
            # tool with no preamble. That is NOT a blind-retry case — re-running
            # the same message just re-hits the same gate. The `not _refusal_reasons`
            # guard lets it fall through to the refusal-recovery path below, which
            # hands the model the reason so it can adapt instead of looping.
            logger.warning(
                "Empty model response for slot %s (attempt %d)",
                slot.key,
                slot._empty_response_retries + 1,
            )
            if _prompt_depth == 0 and slot._empty_response_retries < 1:
                # Seamless self-heal: silently re-queue on the first empty
                # response. An ephemeral status indicator is not used here — it
                # is emitted at turn-teardown and the frontend drops it once the
                # streaming turn ends (so it never surfaces). Only the second
                # consecutive empty surfaces a persisted notice card below.
                slot._empty_response_retries += 1
                slot.queue_insert(
                    0,
                    message,
                    kind=SYNTHETIC_RECOVERY_KIND,
                    # Verbatim replay: ORIGINAL only if the incoming text was the
                    # user's. On a recovery turn it is the runner's continuation.
                    payload=payload_for_replay(_is_synthetic),
                )
                _retrying_empty = True
            elif (
                _prompt_depth == 0
                and slot._empty_response_retries < 2
                and not _should_suppress_requeue(slot)
                and _empty_auto_continue_enabled()
            ):
                # Second consecutive empty: the silent SAME-message re-queue
                # also produced nothing. Re-sending the identical prompt tends
                # to reproduce the identical empty generation, but a DIFFERENT
                # message reliably recovers (observed repeatedly in the field —
                # the user typing "continue" broke the pattern every time). So
                # auto-send ONE synthetic continue nudge on the same live
                # session, with a transcript-visible notice so the recovery is
                # never invisible. Third empty falls through to the give-up
                # notice below — bounded, no loop.
                slot._empty_response_retries += 1
                slot.append(
                    "notice",
                    "ℹ️ The model returned nothing twice — auto-continuing once.",
                    "msg msg-info",
                )
                slot.queue_insert(
                    0,
                    _EMPTY_AUTO_CONTINUE_MSG,
                    kind=SYNTHETIC_RECOVERY_KIND,
                    payload=RecoveryPayload.CONTINUATION,
                )
                _retrying_empty = True
            else:
                # Recoverable, usually-transient: the runner already silently
                # self-retried once (first empty = silent re-queue). Surface a
                # soft "notice" card (not a red "error" card) so a self-healing
                # event doesn't read like a crash — and phrase it to reflect
                # that the retry already happened. Single emit (see AcpProcessDied
                # note): slot.append persists + broadcasts one chat_message via
                # _on_message; no explicit broadcast_ws.
                _empty_msg = (
                    "ℹ️ The model returned nothing this turn (it was retried "
                    "and auto-continued automatically). Just send your message "
                    "again to continue."
                )
                slot.append("notice", _empty_msg, "msg msg-info")
        # Fallback arm: a plan emitted BEFORE further tool calls was flushed out
        # of `assistant_text` (reset on each tool boundary), so the final-segment
        # detector above missed it and no [OPTION] gate would register — the
        # model appears to "skip the plan and keep working". Recover the plan
        # from the never-reset whole-turn buffer and arm the gate from it
        # (planning turn only; skipped if the final-segment path already armed).
        if _orch_planning and not _armed_final and _orch_plan_buf:
            _hp_buf, _valid_buf, _ = validate_plan_format(_orch_plan_buf)
            if _hp_buf and _valid_buf:
                logger.info(
                    "Arming plan gate from whole-turn buffer for slot %s "
                    "(plan was followed by tool calls)",
                    slot.key,
                )
                _reset_auto_run_for_new_plan(slot)
                slot._stage_titles, slot._plan_goal, slot._stage_descriptions = (
                    _extract_and_redact_plan_metadata(_orch_plan_buf)
                )
        # On an empty-response re-queue the turn produced nothing and will
        # immediately re-run; skip persistence / consolidation / success-recording
        # so we don't save a spurious empty turn or skew reliability metrics.
        if not _retrying_empty:
            # Attach per-turn stats (elapsed / credits) to the last assistant
            # message so the footer can show them (parity with kiro-cli).
            # Scoped to this turn's messages via _turn_msg_boundary.
            _attach_turn_stats(
                slot,
                _turn_elapsed_ms,
                _turn_credits,
                _turn_cost_usd,
                turn_boundary=_turn_msg_boundary,
                model=_turn_model,
            )
            # Attach accumulated file changes to last assistant message before persist
            _flush_file_changes(slot)
            # Save to history and trigger memory consolidation
            await save_slot_off_loop(state, slot)
            # Reset ALL retry budgets once the cycle completes (success OR the
            # terminal second-empty error) so each new user turn gets fresh budgets.
            # Guarded by _retrying_empty so the empty re-queue iteration preserves
            # every counter: an empty re-queue is NOT a successful turn, so it must
            # not reset the pipe-death/busy budgets (otherwise an empty interleaved
            # between transient failures would extend the intended 3-retry budget).
            #
            # A non-zero stall budget reaching this reset on an OK turn is a
            # COMPLETED recovery cycle: the stall branches return early, so the
            # only way here with an armed budget is the synthetic recovery turn
            # finishing cleanly. Emit outcome=recovered with the attempt count
            # read BEFORE the reset (the exhausted counterpart lives in the
            # stall branches). Gated on the ok outcome so a user cancelling the
            # recovery turn is never counted as a successful recovery.
            if _turn_outcome(_stop_reason) == "ok":
                # An armed budget whose cycle already emitted "exhausted" is
                # not a recovery — the flag blocks the mis-emit (the budget is
                # no longer zeroed at exhaustion, so it can reach here armed).
                if slot._stale_recovery_retries > 0 and not slot._stale_recovery_exhausted_emitted:
                    _emit_recovery_outcome(
                        "stale_recover", "recovered", slot._stale_recovery_retries
                    )
                if slot._tool_stall_retries > 0 and not slot._tool_stall_exhausted_emitted:
                    _emit_recovery_outcome("tool_stall", "recovered", slot._tool_stall_retries)
            slot._empty_response_retries = 0
            slot._prompt_busy_retries = 0
            slot._acp_pipe_death_retries = 0
            slot._stale_recovery_retries = 0
            slot._tool_stall_retries = 0
            slot._stale_recovery_exhausted_emitted = False
            slot._tool_stall_exhausted_emitted = False
            slot._transient_5xx_retries = 0
            # NOTE: the poisoned-conversation streak/one-shot
            # (_prestream_exhausted_cycles / _poisoned_reset_used) are NOT
            # unconditionally reset here: this block also runs for CANCELLED
            # turns, and a user's Stop press by itself proves nothing about
            # the conversation's health. The activity-based streak break
            # (assistant tokens, a tool call — _turn_emitted — or streamed
            # thinking — _turn_thought — is positive evidence the backend
            # accepts this conversation, even if the user then cancelled it)
            # lives in the FINALLY block so it also covers the recovery paths
            # that `return` before this point. The one-shot
            # itself still re-arms only on a LANDED turn (the record_success
            # block below): breaking the streak is cheap to be generous
            # with, re-arming a spent discard is not.
            # NOTE: slot._posttoken_retry_used is intentionally NOT reset here.
            # The one-shot post-token recovery allowance is refreshed at the
            # START of a GENUINE new user turn (see the gated reset near
            # `_turn_emitted = False`), never on the synthetic recovery turn.
            # Resetting it on the recovery turn's completion would let a repeated
            # post-token 5xx during recovery re-queue forever.

        if _stop_reason == STOP_REASON_CANCELLED:
            logger.info("Turn cancelled by user for slot %s", slot.key)
        elif not _retrying_empty:
            _maybe_consolidate(state, slot)
        state.sessions.check_context_usage(session_key, client)
        pct = client.context_usage_pct()
        state.broadcast_context_usage(slot.key, _context_usage_payload(slot.key, client))
        if _stop_reason != STOP_REASON_CANCELLED and not _retrying_empty:
            state.sessions.record_success(session_key)
            # A LANDED turn breaks the pre-stream-exhaustion streak and
            # re-arms the poisoned-conversation one-shot: only a prompt that
            # actually reached the model and completed proves the (possibly
            # fresh) conversation works. Deliberately NOT in the cancel-
            # inclusive budget block above — a Stop press during the recovery
            # turn must not re-arm a second discard without that evidence.
            slot._prestream_exhausted_cycles = 0
            slot._poisoned_reset_used = False
            # This turn landed: the prompt (including any re-injected skills
            # index) reached the model, so the `finally` must NOT restore the
            # one-shot flag.
            _turn_landed = True
            # Per-interaction telemetry (PlatformContext seam) — shared helper so
            # the payload shape and model reflection cannot drift across surfaces.
            record_interaction_event(client, session_key, "dashboard")
        # Broadcast prompt stats for activity viewer
        _prompt_stats = getattr(  # type: ignore[assignment]
            getattr(client, "_client", client), "last_prompt_stats", None
        )
        if _prompt_stats:
            state.broadcast_ws(
                "activity_event",
                {
                    "slot": slot.key,
                    "kind": "stats",
                    "text": f"Turn complete: {_prompt_stats.event_count} events, {len(_prompt_stats.tool_calls)} tool calls, context {round(pct)}%",  # type: ignore[attr-defined]
                },
            )
        # Pass the full redacted final assistant segment (text after the last
        # tool call, end-of-turn plan/OPTIONS processing applied) to Stop hooks.
        # fire() matches Stop hooks against this and puts it on stdin as
        # ``assistant_text``; run_script_hook caps ONLY the KIROCREW_HOOK_CONTEXT
        # env var (ARG_MAX safety). The full segment is passed (not sliced to
        # [:500]) so the tail — e.g. the harness [OPTIONS:] line — reaches both
        # the matcher and the hook body.
        _final = redact_credentials(redact_exfiltration_urls(assistant_text)[0])[0]
        # Report how deep this hook-continuation run is so a gate hook can
        # diagnose or apply a stricter limit than the configurable backstop.
        _stop_hook_out = await _fire(
            HOOK_EVENT_STOP,
            _final,
            hook_continuation_count=slot._hook_continuation_depth,
        )

        # ── Stop-hook continuation ─────────────────────────────────────────
        # A Stop hook that exits 0 and prints {"decision": "block", "reason":
        # ...} asks the harness to continue with `reason` as the next message
        # (https://kiro.dev/docs/hooks/types#agent-stop), so a hook can judge the
        # finished turn and keep the session going — a test-gate hook, or one that
        # auto-continues a trivial read — without a round-trip to the user.
        # Suppressed on a user stop or a pending reset so a hook can never
        # override the Stop button. A configurable consecutive-turn backstop
        # bounds faulty always-block hooks; 0 explicitly disables that backstop.
        # The finally block's dequeue loop dispatches accepted continuations.
        if should_queue_hook_continuation(slot._stopping, needs_session_reset, _stop_reason) and (
            # Suppress if any user stop was initiated during this turn (streaming,
            # completion persistence, or the hook _fire above): stop_turn()
            # reporting "idle" resets _stop_state before this guard reads
            # _stopping, but _stop_generation counts stop INITIATIONS and never
            # rewinds, so an entry-vs-now delta is the durable signal.
            slot._stop_generation
            == _stop_gen_at_entry
        ):
            _hook_reasons = parse_hook_continuations(_stop_hook_out)
            # No block decision -> nothing to queue; skip the cap load and
            # arithmetic on the common empty path (also what the old
            # `_hook_reasons and _nudge_cap` short-circuit did).
            if _hook_reasons:
                _nudge_cap = (
                    await asyncio.to_thread(KiroCrewConfig.load)
                ).agent.max_stop_hook_nudges
                # Config loading yields to the event loop. Recheck the Stop
                # boundary before mutating the queue so a Stop that lands during
                # that await cannot be bypassed by the stale outer guard.
                if (
                    not should_queue_hook_continuation(
                        slot._stopping, needs_session_reset, _stop_reason
                    )
                    or slot._stop_generation != _stop_gen_at_entry
                ):
                    _hook_reasons = []
            else:
                _nudge_cap = 0
            # The cap bounds TOTAL consecutive continuation turns, and one Stop
            # event can carry several block reasons, so clamp to the remaining
            # budget rather than checking depth once and queueing all of them.
            # _hook_continuation_depth only counts turns that have RUN, so also
            # subtract continuations already sitting in the queue from an earlier
            # multi-reason event: they will run and add depth, and ignoring them
            # lets each event recompute room from depth alone and overshoot.
            _pending = sum(
                1
                for _it in slot._queue
                if is_synthetic_recovery_item(_it)
                and _it["content"].startswith(HOOK_CONTINUATION_RECOVERY_PREFIX)
            )
            _room = (
                len(_hook_reasons)
                if not _nudge_cap
                else max(0, _nudge_cap - slot._hook_continuation_depth - _pending)
            )
            # queue_insert(0, …) prepends, so insert in reverse to keep several
            # hooks' instructions in firing order.
            for _reason in reversed(_hook_reasons[:_room]):
                slot.queue_insert(
                    0,
                    f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\n{_reason}",
                    kind=SYNTHETIC_RECOVERY_KIND,
                )
            if _hook_reasons and _room < len(_hook_reasons):
                # The run reached the cap: some (or all) reasons were refused.
                # Surface an inject row (renders as a halt card carrying the
                # reached depth) but dispatch nothing for the excess. This is the
                # backstop against a buggy always-block hook looping an
                # unattended session. `0` disables the cap entirely.
                _dropped = len(_hook_reasons) - _room
                slot.append(
                    "inject",
                    f"{HOOK_HALTED_RECOVERY_PREFIX} #{slot._hook_continuation_depth}\n"
                    f"A Stop hook asked to continue, but this run reached "
                    f"agent.max_stop_hook_nudges = {_nudge_cap} "
                    f"(depth {slot._hook_continuation_depth}); {_dropped} nudge(s) "
                    f"were dropped and the run was halted. Raise or disable the "
                    f"cap in config to allow more.",
                    "msg msg-inject",
                )
                state.push_slots_update()

        # ── Tool-refusal recovery ──────────────────────────────────────────
        # A recoverable refusal (host-gate policy deny or the read-only bash
        # gate) ended this turn early via kiro-cli's tool-interrupted marker.
        # Hand the reason back to the model as a continuation so it can adapt —
        # an allowed alternative, a different tool, or a reasoned stop — instead
        # of stalling for the user. Skipped on a user stop or when a session
        # reset is already re-queuing. No turn cap by design: the model decides
        # when to stop, and the user's Stop button stays the hard breaker. The
        # finally block's dequeue loop picks this up and dispatches it.
        if should_queue_refusal_recovery(
            _refusal_reasons, slot._stopping, needs_session_reset, _stop_reason
        ):
            _recovery_body = build_refusal_recovery_prompt(_refusal_reasons)
            if _recovery_body:
                slot.queue_insert(
                    0,
                    f"{REFUSAL_RECOVERY_PREFIX}\n{_recovery_body}",
                    kind=SYNTHETIC_RECOVERY_KIND,
                    payload=RecoveryPayload.CONTINUATION,
                )

        # ── Bidirectional sync: mirror response to linked Slack thread ──
        if assistant_text and state.slack_client and _mirror_thread and _mirror_chan:
            try:
                from kiro_crew.slack.format import (  # circular: slack.format -> dashboard.state -> chat
                    build_options_blocks,
                    extract_options,
                    render_for_slack,
                )

                # Extract the OPTIONS tag from the RAW text, before rendering.
                # It is a plain-text marker, so pulling it off after conversion
                # means whatever conversion did to the tail decides whether the
                # controls render at all -- and a >39,000-char turn used to lose
                # the tag entirely to to_slack_mrkdwn's self-truncation.
                _mirror_body, _mirror_options = extract_options(assistant_text)

                for _part in render_for_slack(_mirror_body):
                    await state.slack_client.post_message(_mirror_chan, _part, _mirror_thread)
                if _mirror_options:
                    # Keep the ts this posts: the control has to be spendable
                    # later, and discarding the ts is what leaves a superseded
                    # question clickable forever. build_options_blocks already
                    # redacts each choice through redact_for_display, so nothing
                    # extra is needed here.
                    # The asker is THIS session, named explicitly. Resolving it
                    # from the thread would name whoever owns the thread at mint
                    # time, so a relink landing mid-turn would stamp the control
                    # with a conversation that never asked the question.
                    _mirror_token = await asyncio.to_thread(mint_options_token, state, session_key)
                    _mirror_blocks = build_options_blocks(
                        _mirror_options, staleness_token=_mirror_token
                    )
                    # The thread's owner BEFORE the post. A relink landing while
                    # post_blocks is in flight moves the conversation to another
                    # session, and a control recorded under the key this turn
                    # started with would be filed where that session's expiry
                    # never looks -- and clickable into a conversation it does not
                    # belong to. Same treatment the other two posting paths get.
                    _pre_owner = (
                        state.sessions.get_session_for_thread(_mirror_thread) or session_key
                        if getattr(state, "sessions", None)
                        else session_key
                    )
                    _mirror_ts = await state.slack_client.post_blocks(
                        _mirror_chan,
                        _mirror_blocks,
                        "Options",
                        _mirror_thread,
                    )
                    if _mirror_ts:
                        _owner = (
                            state.sessions.get_session_for_thread(_mirror_thread) or session_key
                            if getattr(state, "sessions", None)
                            else session_key
                        )
                        remember_slack_options(
                            state,
                            _owner,
                            PostedOptions(
                                channel=_mirror_chan,
                                ts=_mirror_ts,
                                choices=tuple(_mirror_options),
                                blocks=tuple(_mirror_blocks),
                            ),
                        )
                        if _owner != _pre_owner:
                            # An owner change IS supersession: the question we just
                            # posted would be answered into a conversation that has
                            # moved on. Narrowed to OUR ts so a control the new
                            # owner recorded meanwhile survives.
                            await expire_slack_options(state, _owner, ts=_mirror_ts)
            except Exception:
                logger.debug("Failed to mirror response to Slack", exc_info=True)

        # Channel-neutral leg: deliver the completed reply to a linked non-Slack
        # proactive channel (e.g. Telegram) via Transport.send_message. Slack is
        # handled above by its dedicated streaming mirror. Only a slash command is
        # withheld: it has no mirrored question, whereas every requeue site runs
        # downstream of the user-message leg above, so a recovery reply always has
        # a preceding question on the linked surface — withholding it would strand
        # that question unanswered.
        if not is_slash:
            await _deliver_cross_surface_reply(state, session_key, assistant_text)
    except asyncio.CancelledError:
        if assistant_text:
            slot.purge_chunks()
            slot.append(
                "assistant",
                redact_credentials(redact_exfiltration_urls(assistant_text)[0])[0],
                "msg msg-a",
            )
    except AcpAuthRequired as exc:
        # The signed-out CLI is discovered HERE, not by a probe: this is the
        # authoritative logout signal now that readiness is latched at boot.
        # Non-retryable — respawning hits the same wall — so never re-queue, and
        # latch the service signed-out so the fail-closed gates stop trusting a
        # stale ready value.
        logger.warning("ACP auth required in slot %s: %s", slot.key, exc)
        # Every queued prompt would hit the same wall. Popping them one by one
        # would drain the whole queue into identical failures, leaving nothing to
        # resume after the user signs in — so hold the queue intact instead.
        _auth_required = True
        needs_session_reset = True
        if assistant_text:
            slot.purge_chunks()
            slot.append(
                "assistant",
                redact_credentials(redact_exfiltration_urls(assistant_text)[0])[0],
                "msg msg-a",
            )
        _auth_msg = str(exc)
        slot.append("error", _auth_msg, "msg msg-err")
        _mark_kiro_signed_out(state)
        await _deliver_auth_error_to_slack(state, slot, sessions, session_key, _auth_msg)
    except AcpProcessDied as exc:
        logger.warning("ACP process died in slot %s: %s — resetting session", slot.key, exc)
        needs_session_reset = True
        if assistant_text:
            slot.purge_chunks()
            slot.append(
                "assistant",
                redact_credentials(redact_exfiltration_urls(assistant_text)[0])[0],
                "msg msg-a",
            )
        slot._acp_pipe_death_retries += 1
        if _should_suppress_requeue(slot):
            pass
        elif _prompt_depth == 0 and slot._acp_pipe_death_retries <= 3:
            # Persisted card: reliably visible at turn-teardown (an ephemeral
            # chat_status is dropped by the frontend once the streaming turn ends).
            # slot.append already emits ONE chat_message (via _on_message /
            # _broadcast_chat_message in ws_mode) AND persists the card — do NOT
            # also broadcast_ws("chat_message") or the UI renders a duplicate card
            # until the post-turn history refresh reconciles it.
            _retry_msg = "⟳ Connection lost — retrying…"
            slot.append("error", _retry_msg, "msg msg-err")
            _requeue_text, _requeue_payload = build_recovery_requeue(
                message,
                _turn_emitted,
                cause=ResetCause.CONNECTION_LOST,
                message_is_synthetic=_is_synthetic,
            )
            slot.queue_insert(
                0,
                _requeue_text,
                kind=SYNTHETIC_RECOVERY_KIND,
                payload=_requeue_payload,
            )
        elif slot._acp_pipe_death_retries > 3:
            slot.append("error", "Session stuck — please start a new chat.", "msg msg-err")
        else:
            slot.append("error", "⟳ Connection lost — please retry.", "msg msg-err")
    except PromptBusyExhaustedError:
        # Provider was killed after prompt-busy retries exhausted — reset (and
        # re-queue only when retry-eligible; see per-branch handling below).
        logger.info("Prompt busy exhausted in slot %s — resetting session", slot.key)
        needs_session_reset = True  # checked in finally block
        if assistant_text:
            slot.purge_chunks()
            slot.append(
                "assistant",
                redact_credentials(redact_exfiltration_urls(assistant_text)[0])[0],
                "msg msg-a",
            )
        slot._prompt_busy_retries += 1
        if _should_suppress_requeue(slot):
            pass
        elif _prompt_depth == 0 and slot._prompt_busy_retries <= 3:
            # Single emit: slot.append persists + broadcasts one chat_message
            # via _on_message (see the AcpProcessDied note above); no explicit
            # broadcast_ws or the UI shows a duplicate card.
            _retry_msg = "⟳ Session busy — retrying…"
            slot.append("error", _retry_msg, "msg msg-err")
            _requeue_text, _requeue_payload = build_recovery_requeue(
                message,
                _turn_emitted,
                cause=ResetCause.SESSION_BUSY,
                message_is_synthetic=_is_synthetic,
            )
            slot.queue_insert(
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
    except AcpError as exc:
        # The exception CLASS is logged alongside the message because the
        # session-health scanner keys its prompt_stuck signal off this line, and
        # the message text is no longer a reliable carrier: _format_acp_error
        # rewrites the backend's "prompt already in progress" into user-facing
        # prose. The class name is the structural classification rendered into
        # text, so a scanner never has to pattern-match wording that a copy
        # edit (or translation) can move. See session_health._PATTERNS.
        logger.warning("ACP error in slot %s: [%s] %s", slot.key, type(exc).__name__, exc)
        _msg = str(exc)
        # Retry-eligible transients:
        #   - "already in progress": prompt busy (kiro-cli side)
        #   - "process exited" / "not running": ACP subprocess died, need cold-start
        # For both: reset the session and re-queue the message so auto-nudges
        # (and dashboard messages) get executed on a fresh provider instead of
        # surfacing a bare ❌ error card with no work done.
        # Prompt-busy is matched STRUCTURALLY (the AcpPromptBusy subclass) with
        # the string as a fallback. _format_acp_error rewrites the backend's
        # "prompt already in progress" into friendly prose that no longer
        # contains the marker, so a string-only check silently loses the
        # reset-and-requeue path for every producer that formats before raising.
        # The fallback still covers history-restored / unformatted messages.
        _retry_eligible = (
            isinstance(exc, AcpPromptBusy)
            or "already in progress" in _msg
            or "process exited" in _msg
            or "not running" in _msg
        )
        if _retry_eligible:
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
            # the generic else: no reset (the next turn hits the dead process)
            # and the failure never counting toward the exhaustion threshold.
            needs_session_reset = True  # checked in finally block
            if assistant_text:
                _safe, _ = redact_exfiltration_urls(assistant_text)
                _safe, _ = redact_credentials(_safe)
                slot.purge_chunks()
                slot.append("assistant", _safe, "msg msg-a")
            # Option Y: pipe-death ("process exited"/"not running") shares the
            # _acp_pipe_death_retries counter with the AcpProcessDied handler;
            # genuine "already in progress" busy uses _prompt_busy_retries.
            _is_pipe_death = "process exited" in _msg or "not running" in _msg
            if _is_pipe_death:
                slot._acp_pipe_death_retries += 1
                _exhausted = slot._acp_pipe_death_retries > 3
                _status = "⟳ Connection lost — retrying…"
            else:
                slot._prompt_busy_retries += 1
                _exhausted = slot._prompt_busy_retries > 3
                _status = "⟳ Session busy — retrying…"
            if _should_suppress_requeue(slot):
                pass
            elif _exhausted:
                logger.info(
                    "Retry budget exhausted for slot %s — surfacing 'Session stuck'", slot.key
                )
                slot.append("error", "Session stuck — please start a new chat.", "msg msg-err")
            elif _prompt_depth == 0:
                # Single emit (see AcpProcessDied note): slot.append persists +
                # broadcasts one chat_message via _on_message; no explicit broadcast_ws.
                logger.info(
                    "Re-queuing slot %s after transient (pipe_death=%s, attempt %d)",
                    slot.key,
                    _is_pipe_death,
                    slot._acp_pipe_death_retries if _is_pipe_death else slot._prompt_busy_retries,
                )
                slot.append("error", _status, "msg msg-err")
                _requeue_text, _requeue_payload = build_recovery_requeue(
                    message,
                    _turn_emitted,
                    # Shared branch: `_status` above already told the user
                    # which of the two happened, so the continuation must
                    # agree with it rather than pick one.
                    cause=(
                        ResetCause.CONNECTION_LOST if _is_pipe_death else ResetCause.SESSION_BUSY
                    ),
                    message_is_synthetic=_is_synthetic,
                )
                slot.queue_insert(
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
        elif (
            not _turn_emitted
            and acp_error_is_transient(exc)
            and slot._transient_5xx_retries < TRANSIENT_RETRIES
        ):
            # Transient backend 5xx (InternalServerError / DispatchFailure /
            # ConnectionReset, JSON-RPC -32603): the kiro-cli process is ALIVE —
            # only the model backend hiccupped — so do NOT reset the session
            # (needs_session_reset stays False). Re-prompt the SAME live session
            # with bounded backoff, reusing the llm_helpers transient classifier
            # + backoff curve landed for unattended callers.
            # The `not _turn_emitted` guard means no assistant tokens
            # or tool calls have been delivered this turn, so re-prompting can't
            # double-stream output or re-run a side-effecting tool. Auth/validation
            # errors are excluded by the classifier and fall through to the bare
            # error below (fail-fast). On budget exhaustion this elif goes false
            # and the bare-error else surfaces a clean ❌ on a still-resumable
            # session — unless CONSECUTIVE cycles exhaust this way, in which
            # case the else escalates to a session destroy (poisoned
            # persisted conversation; see the escalation block there).
            slot._transient_5xx_retries += 1
            _delay = transient_retry_delay(slot._transient_5xx_retries)
            logger.info(
                "Transient backend 5xx in slot %s (attempt %d/%d) — re-prompting "
                "live session in %.1fs: %s",
                slot.key,
                slot._transient_5xx_retries,
                TRANSIENT_RETRIES,
                _delay,
                _msg[:80],
            )
            # No tokens streamed (guarded above), so no chunk message exists;
            # strip defensively before re-queue all the same.
            slot.purge_chunks()
            if _should_suppress_requeue(slot):
                pass
            elif _prompt_depth == 0:
                # Single emit (see AcpProcessDied note): slot.append persists +
                # broadcasts one chat_message; no explicit broadcast_ws. Back off,
                # then re-queue — the finally block dequeues onto the SAME live
                # session (no reset), preserving conversation state.
                slot.append("error", "⟳ Backend hiccup — retrying…", "msg msg-err")
                await asyncio.sleep(_delay)
                slot.queue_insert(
                    0,
                    message,
                    kind=SYNTHETIC_RECOVERY_KIND,
                    # Verbatim replay: ORIGINAL only if the incoming text was the
                    # user's. On a recovery turn it is the runner's continuation.
                    payload=payload_for_replay(_is_synthetic),
                )
            else:
                # depth>0 (nested turn): don't re-queue — surface a clean
                # transient status; the live session stays resumable.
                slot.append("error", "⟳ Backend hiccup — please retry.", "msg msg-err")
        elif _turn_emitted and acp_error_is_transient(exc) and not slot._posttoken_retry_used:
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
            # queue_insert below, AFTER the eligibility check + backoff). Setting
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
            # it — finalize it as a normal assistant message exactly like the
            # terminal else: branch — then surface a brief recovery notice, then
            # (if eligible) auto-retry ONCE by re-queueing the CONTINUE
            # instruction (NOT the original message). The user sees an append-only
            # sequence:
            #   [partial] [recover notice] [continued answer]
            # with nothing removed. No frontend reconcile / SSE gate is needed —
            # append-only is correct for both WebSocket and SSE clients.
            # Persisting the partial BEFORE the backoff sleep means a cancel
            # (Stop) during the sleep simply leaves partial+notice shown, no loss.
            # Persist the streamed partial as a real assistant message (copy of
            # the terminal else: persist pattern): redact, strip the live chunk
            # messages, then append the finalized assistant bubble.
            if assistant_text:
                _safe, _ = redact_exfiltration_urls(assistant_text)
                _safe, _ = redact_credentials(_safe)
                slot.purge_chunks()
                slot.append("assistant", _safe, "msg msg-a")
            # Surface a brief recovery notice (one append).
            slot.append("error", "⟳ Backend hiccup — recovering…", "msg msg-err")
            if not _should_suppress_requeue(slot) and _prompt_depth == 0:
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
                await asyncio.sleep(_delay)
                slot._posttoken_retry_used = True
                slot.queue_insert(
                    0,
                    _POSTTOKEN_RECOVER_MSG,
                    kind=SYNTHETIC_RECOVERY_KIND,
                    payload=RecoveryPayload.CONTINUATION,
                )
            # else: Stop active (_should_suppress_requeue) or nested turn
            # (_prompt_depth != 0) — do NOT requeue; partial + notice already
            # shown, so the streamed answer survives in the transcript. The
            # allowance is left UNconsumed so a later turn can still recover once.
        else:
            if assistant_text:
                _safe, _ = redact_exfiltration_urls(assistant_text)
                _safe, _ = redact_credentials(_safe)
                slot.purge_chunks()
                slot.append("assistant", _safe, "msg msg-a")
            # ── Poisoned-conversation escalation ────────────────────────────
            # A transient-classified error that reaches this terminal branch
            # with ZERO output means a full retry ladder was exhausted
            # pre-stream (the transient elif above only goes false on budget
            # exhaustion). ONE such cycle is plausibly a momentary outage; the
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
            # ONE-SHOT per landed turn (_poisoned_reset_used): if even the
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
            _prestream_exhausted = (
                not _turn_emitted and not _turn_thought and acp_error_is_transient(exc)
            )
            if _prestream_exhausted:
                slot._prestream_exhausted_cycles += 1
            else:
                # A different terminal error (or one with streamed output)
                # breaks the streak — consecutive means consecutive.
                slot._prestream_exhausted_cycles = 0
            if (
                _prestream_exhausted
                and slot._prestream_exhausted_cycles >= POISONED_SESSION_CYCLES
                and not slot._poisoned_reset_used
                and _prompt_depth == 0
                and not _should_suppress_requeue(slot)
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
            if _canary_ok and slot._stop_generation != _stop_gen_before_canary:
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
                    slot._prestream_exhausted_cycles,
                    slot.key,
                    session_key,
                )
                # Consume the one-shot HERE, where the recovery is actually
                # enqueued (mirrors _posttoken_retry_used accounting).
                slot._poisoned_reset_used = True
                needs_conversation_discard = True  # checked in finally block
                slot.append(
                    "error",
                    "⟳ The backend keeps rejecting this conversation — "
                    "restarting the model session and retrying (this chat's "
                    "messages are kept; the model rebuilds its working "
                    "context from them)…",
                    "msg msg-err",
                )
                slot.queue_insert(0, message, kind=SYNTHETIC_RECOVERY_KIND)
                # Fresh conversation ⇒ fresh ladder for the recovery cycle.
                slot._transient_5xx_retries = 0
            else:
                _err_text, _ = redact_exfiltration_urls(str(exc))
                _err_text, _ = redact_credentials(_err_text)
                slot.append(
                    "error",
                    f"⏱️ {_err_text}" if "timed out" in _msg else f"❌ {_err_text}",
                    "msg msg-err",
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
    except Exception as exc:
        logger.exception("Dashboard chat error in slot %s", slot.key)
        _err_text, _ = redact_exfiltration_urls(str(exc))
        _err_text, _ = redact_credentials(_err_text)
        slot.append("error", _err_text, "msg msg-err")
        await state.sessions.record_failure(session_key)
    finally:
        # Poisoned-conversation streak break — in the FINALLY on purpose (fork
        # GPT review): several recovery paths (stale-turn, tool-stall,
        # pipe-death) `return` before the main completion block, and a turn
        # with model activity that exits through them must STILL break the
        # exhaustion streak — the backend demonstrably served this
        # conversation. Safe against the terminal AcpError handler's streak
        # increment: incrementing requires ZERO activity, so the two are
        # mutually exclusive by construction and this can never clobber a
        # legitimate increment. One-shot re-arm stays landed-turn-only.
        if _turn_emitted or _turn_thought:
            slot._prestream_exhausted_cycles = 0
        # Completion can be bypassed by cancellation, provider errors, or timeouts.
        # Close cards idempotently and retain only bounded terminal records for
        # reconnect replay until the next turn installs a fresh tracker.
        # Snapshot which children were still unfinished FIRST — close_all
        # force-marks every card done, and terminal entries linger in the
        # tracker for replay, so reading the tracker afterwards would blame
        # long-completed children for a later ceiling timeout.
        _children_unfinished_final = any(not _i.get("done") for _i in _native_tracker.values())
        try:
            _native_subagent_close_all(state, slot, _native_tracker, _native_card_output)
        finally:
            slot._native_subagent_tracker = _retain_terminal_native(_native_tracker)
            slot._native_subagent_output = {}
        slot._batch_rejected = False
        # Stash the hang-attribution snapshot BEFORE dropping the client ref:
        # if this turn was cut by the dashboard ceiling (_bounded_turn), the
        # done-callback (finish_turn_task) runs AFTER this finally, when
        # _acp_client is already None — it reads these two fields to emit
        # kirocrew.turn.timeout.cause for the ceiling path.
        _lc = slot._acp_client
        slot._last_turn_awaiting_permission = bool(
            getattr(_lc, "_awaiting_permission", False)
            or getattr(getattr(_lc, "_handle", None), "_awaiting_permission", False)
        )
        slot._last_turn_children_announced = _children_unfinished_final
        # Steer handle: turn is over, drop the live client ref so a late steer
        # can't target a dead session (the route also re-checks running state).
        slot._acp_client = None
        # Same lifecycle for the segment-cut handle: a late steer must not
        # flush into a finished turn's (already-flushed) locals.
        slot._steer_segment_cut = None
        # Same lifecycle for the child-fidelity opt-in latched at turn start:
        # it is THIS consumer's promise to render the low-fidelity downgrade
        # card. Leaving it set would let a later, fidelity-UNAWARE consumer of
        # the same provider/handle inherit the opt-in and silently disable the
        # handle-level fail-close choke point.
        try:
            setattr(client, "child_fidelity_aware", False)
        except Exception:
            pass
        # Ensure file changes always surface, even on cancel/error. Wrapped so
        # a raise here cannot skip the re-arm below and re-introduce the orphan
        # bug this fix prevents.
        try:
            _flush_file_changes(slot)
        except Exception:
            logger.debug("_flush_file_changes failed", exc_info=True)
        # This turn consumed the one-shot post-compaction re-injection flag but
        # never landed, so the prompt carrying the skills index was discarded —
        # an early return (stale-recover / tool-stall / error re-queue), an
        # except arm, a hard CancelledError, a graceful cancel, or an empty
        # re-queue. Put the flag back here rather than at the success check,
        # because most of those paths never reach it; without this the index is
        # lost for the remaining life of the session. Wrapped for the same
        # reason as the flush above.
        if _needs_reinjection and not _turn_landed:
            try:
                state.sessions.mark_needs_reinjection(session_key)
            except Exception:
                logger.debug("re-arming skills re-injection failed", exc_info=True)
        # ── AutoNudge: (re)arm the idle timer on EVERY turn-exit path. ──
        # Must be in finally, not the happy path: a turn that ends via timeout
        # / AcpProcessDied / AcpError / cancel would otherwise never re-arm,
        # silently orphaning the loop.
        try:
            from kiro_crew.autonudge import (
                get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_runner
            )

            _autonudge = _autonudge_get()
            if _autonudge is not None:
                _autonudge.notify_turn_complete(slot.key)
        except Exception:
            logger.debug("autonudge.notify_turn_complete failed", exc_info=True)
        # Clean up mirror stream on any exit path.
        #
        # The release below MUST happen however this block exits. Each await in
        # here is already guarded against ``Exception``, but ``CancelledError``
        # derives from ``BaseException`` (since 3.8), so a cancellation
        # delivered while one of them is suspended slips past every
        # ``except Exception`` and skips the release entirely.
        #
        # The permit is keyed by SESSION, so leaking it does not merely lose
        # this turn: the session reads as permanently busy, every later turn for
        # it blocks forever, no queued turn drains, and only a gateway restart
        # clears it. The nested try/finally makes the release unconditional
        # while preserving the reset-then-release ordering.
        try:
            if _mirror_stream_ts and state.slack_client and _mirror_chan:
                try:
                    if _mirror_active_task:
                        await state.slack_client.append_task(
                            _mirror_chan,
                            _mirror_stream_ts,
                            _mirror_active_task,
                            _mirror_active_task_title,
                            "complete",
                        )
                except Exception:
                    logger.debug("Task append cleanup failed", exc_info=True)
                try:
                    await state.slack_client.stop_stream(_mirror_chan, _mirror_stream_ts)
                except Exception:
                    logger.debug("Stream cleanup failed", exc_info=True)
            if _acquired and (needs_session_reset or needs_conversation_discard):
                try:
                    if needs_conversation_discard:
                        # Poisoned-conversation escalation: clear ONLY the
                        # resume sid (keeping the session-map entry with its
                        # Slack thread/channel linkage), so the re-queued
                        # recovery turn cold-starts a fresh native
                        # conversation instead of session/load-ing the same
                        # rejected one (which reset() would do).
                        await state.sessions.discard_conversation(session_key)
                    else:
                        await state.sessions.reset(session_key)
                except Exception:
                    logger.warning("Failed to reset session %s after agent switch", session_key)
        finally:
            if _acquired:
                # A successful reset() above already popped the key under its
                # own lock, so this is a no-op on that path (the popped
                # session's semaphore is discarded with it). Kept unconditional
                # so a reset that failed or was cancelled still hands back the
                # permit rather than stranding the session.
                state.sessions.release(session_key)
            # This turn's identity dies WITH its session, and inside the same
            # finally for the same reason the release is: the reset above can be
            # cancelled, and CancelledError derives from BaseException, so a
            # clear placed after this block is simply skipped and the slot keeps
            # advertising a turn that is gone.
            #
            # It must also land before `_start_next_queued_turn` further down
            # can install the SUCCESSOR's key — a clear after that would wipe a
            # live turn's identity and drop the cancel routes back to mutable
            # routing. Compare-and-clear keeps that true if the ordering is ever
            # rearranged: only the turn that installed a key may retire it.
            #
            # Outside the `_acquired` guard: the identity is published before
            # the permit is taken, so a cold start that never acquired one still
            # has something to retire.
            if slot._active_turn_session_key == session_key:
                slot._active_turn_session_key = ""
        # End-of-turn fallback: catches set_project calls that fired mid-turn,
        # after the start-of-turn consume already ran. Guarded because a raise
        # here would skip the steer requeue and queue drain below, silently
        # stranding queued work at the end of an otherwise successful turn.
        try:
            _had_pending_reset = bool(slot._pending_reset_history_key)
            await _consume_pending_reset(state, slot)
            # The consume tore down the session for a mid-turn project change;
            # without a respawn the NEXT message pays the full cold start the
            # eager path exists to hide. Only when a reset was actually
            # consumed — an ordinary turn end must not spawn anything.
            if _had_pending_reset:
                schedule_eager_spawn(state, slot)
        except Exception:
            logger.debug("_consume_pending_reset failed", exc_info=True)
        # ── Requeue unconsumed steers ──
        # A steer handed to kiro-cli that never echoed steering_consumed dies
        # with the turn (stall-cancel, soft STOP, error, or a steer that raced
        # the turn's natural end). Degrade each one to an ordinary queue card
        # at the HEAD of the queue (steers outrank queued messages — they were
        # meant to be injected before any queued item ran). This mirrors the
        # existing STOP semantics: soft stop preserves the queue; a hard kill
        # discards it (the force-stop handler clears _pending_steers alongside
        # _queue, so nothing is requeued there). The card is visible and
        # individually cancellable — a user who meant "discard" clicks ✕;
        # nothing is ever silently lost.
        _requeue_unconsumed_steers(state, slot)
        # ── Retire any wait countdown ──
        # A healthy `wait` clears its own state with a final keepalive ping, but
        # that ping is best-effort and cannot run at all if the MCP subprocess
        # died mid-sleep (hard stop, crash, gateway abort). Clearing at turn end
        # is the backstop that keeps a dead wait from leaving a countdown ticking
        # toward a deadline nothing is waiting on, and drops any end request the
        # tool never collected so it cannot reach the next sleep.
        # Also releases the contested latch: it is deliberately turn-scoped, so
        # this is the ONLY thing that clears it. A slot whose parent and subagent
        # both slept in one turn gets its countdown back on the next turn.
        if (
            slot._wait_state is not None
            or slot._end_wait_request is not None
            or slot._wait_contested
        ):
            slot._wait_state = None
            slot._end_wait_request = None
            slot._wait_contested = False
        # Record this turn's auth outcome so the orchestrator _stage_loop, which
        # runs stages as separate _run_chat calls, can mirror this same
        # "hold the queue for post-login resume" guard on its end-of-plan handoff.
        slot._last_turn_auth_required = _auth_required
        next_turn_started = False
        if slot._queue and not _auth_required:
            # No readiness gate before the next queued turn. Readiness is latched
            # at boot, so parking the queue on a stale not-ready value would
            # strand it indefinitely; the successor's own ACP attempt reports a
            # signed-out CLI as an AcpAuthRequired error card instead.
            #
            # `_auth_required` is the ONE exception: this turn just proved the CLI
            # is signed out, so every queued prompt would fail identically. The
            # queue is left intact (cards stay visible and individually
            # cancellable) and resumes on the user's next send after they log in
            # — the no-loss rule, without a readiness waiter to strand it.
            state.push_slots_update()
            next_turn_started = await _start_next_queued_turn(state, slot)

        if not next_turn_started:
            _finish_queue_cycle(state, slot)
