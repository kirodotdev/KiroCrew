"""Core LLM runner — _run_chat, segment flushing, prompt expansion."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import stat as stat_module
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from kiro_crew import mcp_apps_render, model_registry, plan_mode, session_directive
from kiro_crew.acp.client import AcpAuthRequired, AcpError, AcpProcessDied, _is_safe_oauth_url
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
from kiro_crew.autonudge import get_instance
from kiro_crew.config.loader import (
    KiroCrewConfig,
    config_dir,
    normalize_agent_model,
    resolve_agent_bindings,
)
from kiro_crew.context_management import (
    ensure_go_all_option,
    looks_like_plan,
    strip_plan_markers,
    validate_plan_format,
)
from kiro_crew.dashboard.chat_persistence import _build_history_prefix, save_slot_off_loop
from kiro_crew.dashboard.chat_title import (
    _extract_and_redact_plan_metadata,
    _maybe_auto_title,
    _rephrase_plan_lite,
    _reset_auto_run_for_new_plan,
)
from kiro_crew.dashboard.chat_utils import (
    _BLOCKED_SLASH_COMMANDS,
    _MAX_TOOL_PURPOSE,
    _SLASH_COMMANDS,
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
    effective_session_key,
    is_system_injection,
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
)
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.dashboard.state import (
    CRON_NOTIFY_PREFIX,
    CRON_NOTIFY_RE,
    NATIVE_SUBAGENT_DONE_RESULT_CAP,
    NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
    NATIVE_SUBAGENT_OUTPUT_HARD,
    NATIVE_SUBAGENT_OUTPUT_TAIL,
    NATIVE_SUBAGENT_TERMINAL_KEEP,
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    REFUSAL_RECOVERY_PREFIX,
    STALE_RECOVERY_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
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
    should_queue_refusal_recovery,
    unsafe_bash_reason,
)
from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    _normalize_tool_name,
    _tool_matches,
    fire_tool_hooks,
    safe_read_file,
    validate_file_path,
)
from kiro_crew.llm_helpers import (
    TRANSIENT_RETRIES,
    PromptBusyExhaustedError,
    acp_error_is_transient,
    record_interaction_event,
    transient_retry_delay,
)
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.messaging.link import SLACK_NAMESPACE
from kiro_crew.messaging.renderer import chunk_text
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
from kiro_crew.security import (
    _EXFIL_PATTERNS,
    StreamRedactor,
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.session import SessionClosingError
from kiro_crew.slack.handler import post_linked_approval, resolve_linked_approval
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
    is_synthetic_recovery_item,
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


def _turn_outcome(stop_reason: str | None) -> str:
    """Map an EVENT_COMPLETE stop_reason to a low-cardinality turn outcome.

    Single source of truth shared by the ``kirocrew.turn.duration`` emit in
    ``_run_chat`` and its unit test, so the mapping can't silently drift from
    what the test asserts (tests must exercise real production logic).
    """
    s = stop_reason or ""
    if s in ("", "end_turn", "stop", "completed"):
        return "ok"
    if "timeout" in s:
        return "timeout"
    return "error"


def _emit_turn_metric(
    duration_ms: int | float | None,
    stop_reason: str | None,
    slot_key: str,
    *,
    elapsed_ms: int | float | None = None,
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
    attrs: dict = {"outcome": _turn_outcome(stop_reason)}
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


def _context_usage_payload(slot_key: str, client: Any) -> dict[str, Any]:
    """Build the ``context_usage`` WS payload: pct plus real token counts.

    The token counts let the frontend ring tooltip show "used / window" in
    absolute tokens (sourced from the adapter's usage_update), so a 44%-of-200k
    reading is not misread as 44%-of-1M. Token fields are omitted (0) when the
    provider doesn't report them.
    """
    pct = client.context_usage_pct()
    payload: dict[str, Any] = {"slot": slot_key, "pct": round(pct, 1)}
    # Use the provider's public accessors — last_prompt_stats lives on the
    # inner AcpClient, not on the provider, so reaching for it on `client`
    # (the AcpProvider returned by get_or_create) would always miss.
    window = client.context_window_tokens() if hasattr(client, "context_window_tokens") else 0
    if window:
        used = client.context_used_tokens() if hasattr(client, "context_used_tokens") else 0
        # used == 0 means "not measured yet", not "empty context" — it is the
        # post-compaction state (AcpPromptStats.reset_after_compaction keeps
        # the window but zeroes the counts until the next turn's telemetry).
        # Shipping {used: 0, window: W} would overwrite the compaction reset
        # with a false "0 / W tokens" claim in the ring tooltip.
        if used:
            payload["used_tokens"] = used
            payload["window_tokens"] = window
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
) -> None:
    """Attach per-turn stats to the last assistant message's meta.

    Mirrors ``_flush_file_changes``: the meta lands on the in-memory message
    BEFORE ``_save_slot_to_history`` persists it, and reaches the live UI via
    the ``chat_done`` → ``refreshSlot`` re-fetch (no dedicated WS event).

    ``elapsed_ms`` is the turn wall clock (or the provider-reported duration
    when available); ``credits`` is kiro-cli's per-turn ``meteringUsage`` sum;
    ``cost_usd`` is claude_code's API-reported cost. Zero fields are omitted so
    the frontend renders only what the provider actually bills in.

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


# Query params that are a normal, expected part of an OAuth 2.0 / OIDC
# consent URL (incl. PKCE).  Their values are high-entropy by design
# (``state``, ``code_challenge``, ``nonce`` …) and must NOT be treated as
# leaked credentials — otherwise every real GitHub/Google sign-in URL is
# rejected.  Anything *outside* this set is still scanned for real secrets.
_OAUTH_QUERY_PARAMS = frozenset(
    {
        # RFC 6749 / OIDC core
        "client_id",
        "redirect_uri",
        "response_type",
        "response_mode",
        "scope",
        "state",
        "nonce",
        "code_challenge",
        "code_challenge_method",
        "prompt",
        "login_hint",
        "access_type",
        "audience",
        "resource",
        "display",
        "id_token_hint",
        "max_age",
        "ui_locales",
        "acr_values",
        "request_uri",
        # Provider-specific but standard & benign (GitHub: login/allow_signup;
        # Microsoft: domain_hint; Slack: user_scope/team).  Kept in sync with the
        # legit-URL corpus in test/test_mcp_oauth_banner.py.
        "login",
        "allow_signup",
        "domain_hint",
        "user_scope",
        "team",
    }
)


# Unambiguous credential signatures that NEVER legitimately appear inside an
# OAuth/OIDC opaque token (state, code_challenge, …). Unlike _EXFIL_PATTERNS,
# this set excludes the generic base64-blob / heavy-URL-encoding heuristics
# (which match a real high-entropy PKCE value), so it is safe to apply even to
# the exempted OAuth params: a real sign-in URL never carries an AWS key, Slack
# token, or PEM/SSH private-key header in a query value, but a malicious MCP
# server smuggling a credential out through state= would be caught.
_OAUTH_PARAM_CREDENTIAL_RE = re.compile(
    r"(?:"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)


def _oauth_url_contains_credential(url: str) -> bool:
    """True if the URL embeds an *actual* credential or secret.

    Legitimate OAuth consent URLs carry high-entropy params (``state``,
    ``code_challenge``, ``client_id``) and are frequently >200 chars — so we
    deliberately do NOT apply the generic long-query exfiltration heuristic
    here (that rule exists to catch data being smuggled out in query strings,
    and would reject every real OAuth URL).  Instead we reject only when a
    genuine credential pattern is present: any AWS key / Slack token / private
    key (``redact_credentials``), the full exfil heuristic on a NON-OAuth query
    param, or an unambiguous credential signature even inside a recognized OAuth
    param (a real OAuth opaque token never contains an AWS/Slack/PEM secret).
    """
    if not url:
        return False
    # Hard reject: an actual embedded credential anywhere in the URL.
    _, hit_cred = redact_credentials(url)
    if hit_cred:
        return True
    try:
        parsed = urlparse(url)
    except ValueError:
        return True  # unparseable → refuse to render
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in _OAUTH_QUERY_PARAMS:
            # High-entropy OAuth/PKCE values are allowed, but a hard credential
            # signature smuggled into an OAuth param is still exfil — reject it.
            if _OAUTH_PARAM_CREDENTIAL_RE.search(value):
                return True
            continue
        # Non-OAuth param (or path): apply the full exfil heuristic.
        if _EXFIL_PATTERNS.search(value):
            return True
    return False


def _emit_mcp_oauth_request(
    state: "DashboardState", slot: "_ChatSlot", server_name: str, oauth_url: str
) -> None:
    """Append an mcp_oauth banner so the user can authorize an MCP server.

    If the URL is unsafe (non-http(s) scheme) or carries a credential / exfil
    pattern, surface a *rejected* banner explaining why instead of silently
    dropping.  Otherwise the user has no idea their MCP server failed to
    authenticate, and they can't escalate to whoever owns that server.
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
    if _oauth_url_contains_credential(oauth_url):
        # Legitimate OAuth consent URLs carry state/code_challenge/client_id —
        # never AKIA*/Bearer/etc.  Surface this so the user can ask the server
        # owner to fix it instead of just seeing nothing happen.
        logger.warning(
            "ACP: rejecting MCP OAuth URL with credential/exfil pattern for %s",
            server_name or "(unknown)",
        )
        slot.append(
            "mcp_oauth",
            f"🚫 {label} sent an authentication URL containing a credential pattern (rejected).",
            "msg msg-warn",
            meta={
                "server_name": safe_name,
                "failed": True,
                "rejected_url": True,
                "error": "URL contained credential or exfiltration pattern",
            },
        )
        return
    content = f"🔐 {label} requires authentication."
    slot.append(
        "mcp_oauth",
        content,
        "msg msg-info",
        meta={"server_name": safe_name, "oauth_url": oauth_url},
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
    # survives.) The emit-path gate `_oauth_url_contains_credential` deliberately
    # exempts OAuth params from exactly those heuristics — its docstring notes they
    # "would reject every real OAuth URL".
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


# Known redirect forms where & is NOT a command separator:
# N>&M (e.g. 2>&1), &> file, &>> file, >&N
_REDIRECT_PLACEHOLDER = "\x00REDIR\x00"
_REDIRECT_RE = re.compile(r"[0-9]*>&[0-9]*|&>>?")
# After redirects are masked, split on remaining separators.
_CMD_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|&|\n|\|)\s*")
# Command substitution forms that split-then-fnmatch cannot safely reach:
# $(...), backticks, and process substitution <(...)/>(...). Deny-by-default
# when any are present — the pattern match would operate on the outer shell
# syntax, not the embedded sub-command, giving a false sense of authorization.
_CMD_SUBSTITUTION_RE = re.compile(r"\$\(|`|<\(|>\(")


def _mask_quoted_separators(text: str) -> tuple[str, dict[str, str]]:
    """Replace command separators that appear INSIDE quotes with placeholders.

    The split regex (``_CMD_SPLIT_RE``) is quote-unaware, so a separator inside
    a quoted string — e.g. the ``|`` in ``grep "a|b" file && wc -l`` — would be
    treated as a command boundary, mis-segmenting a command the user trusted and
    denying it (fail-closed but a real usability regression). We walk the string
    tracking single/double quote state and swap any ``| & ; \\n`` that is quoted
    for a unique placeholder, restoring it inside each segment before matching.
    Returns ``(masked_text, restore_map)``.
    """
    out: list[str] = []
    restore: dict[str, str] = {}
    quote: str | None = None
    n = 0
    for ch in text:
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
                err = err[:200]
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


def _native_crew_should_auto_approve(native_tracker, state, slot) -> bool:
    """Return True only when a native crew subagent is ACTIVE *and* an
    auto-approve condition holds — otherwise deny (CWE-1188 secure default).

    Active-crew is a NECESSARY precondition: with no live native subagent the
    parent turn is not blocked on a crew tool, so this path must never
    auto-approve — regardless of the ``auto_approve_subagent_tools`` hook,
    ``slot._trust``, or yolo. Only when a crew is active do those signals grant
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
        or getattr(slot, "_trust", False)
        or state.is_yolo_active()
    )


def _safe_native_crew_debug_title(title: str) -> str:
    """Redact credentials/exfiltration URLs from an LLM-controlled native-crew
    tool title before it is logged. Control chars are escaped at the log call
    via %r."""
    safe, _ = redact_exfiltration_urls(title or "")
    safe, _ = redact_credentials(safe)
    return safe


def _matches_trusted_pattern(tool_title: str, patterns: set[str]) -> str | None:
    """Return the matched pattern if tool_title matches any trusted pattern.

    For piped/chained commands, splits into segments and checks each
    independently — ALL segments must match for the command to be trusted.
    Returns comma-joined matched patterns for audit provenance.

    Deny-by-default for commands containing command substitution ($(...),
    backticks, process substitution) — fnmatch cannot reach sub-commands.
    """
    normalized = _normalize_tool_name(tool_title)
    if _CMD_SUBSTITUTION_RE.search(normalized):
        return None
    # First mask separators that live INSIDE quotes (a quoted "a|b" must not be
    # split on its `|`), so _CMD_SPLIT_RE only ever cuts on real, unquoted
    # command boundaries. The placeholders are restored in each segment below.
    quote_masked, sep_restore = _mask_quoted_separators(normalized)
    # Two-pass split: mask known redirect forms (2>&1, &>, &>>) so their &
    # isn't mistaken for a background operator, then split on remaining &.
    # Track masked positions to reconstruct original text in each segment.
    redirects: list[str] = []

    def _mask(m: "re.Match") -> str:
        redirects.append(m.group())
        return _REDIRECT_PLACEHOLDER

    masked = _REDIRECT_RE.sub(_mask, quote_masked)
    split_parts = _CMD_SPLIT_RE.split(masked)
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


def _extract_base_command(tool_title: str) -> str:
    """Extract base binary name(s) for glob pattern generation.

    Handles piped/chained commands split by |, &&, ;
    "Running: ls /tmp" -> "ls"
    "Running: cat /etc/hosts | wc -l" -> "cat,wc"
    "Running: grep -r foo . && echo done" -> "grep,echo"
    "SomeMcpTool" -> "SomeMcpTool"
    """
    normalized = _normalize_tool_name(tool_title)
    segments = re.split(r"\s*(?:\|\||&&|;|\|)\s*", normalized)
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
    if not text or "<mcwidget" not in text:
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
    task = asyncio.create_task(register_widgets_off_loop(text, message_ts, slot.key))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


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
            _sentinel = str(config_dir() / "goal-stop" / f"{_slug}.stop")
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

    kiro-cli injects the CONCATENATION of every steer queued since the last
    consumption; the echo text carries each one ``<user_message>``-wrapped.
    Parse the snapshot into its individual blocks and settle by EQUALITY —
    substring containment would false-positive short steers against longer
    ones or against the wrapper text itself, and a falsely-settled steer is
    silently lost when the turn dies. A steer registered after kiro-cli
    snapshotted (not among the blocks) stays pending. Settling is COUNT-AWARE: each snapshot block settles at
    most one pending entry, so a duplicate identical steer registered after
    the snapshot stays pending instead of being swept by set membership.
    When the echo carries no usable text (older backend, redacted echo),
    fall back to settling all — worst case is a visible, cancellable
    duplicate, never a silent loss.
    """
    if not slot._pending_steers:
        return
    if snapshot.strip():
        consumed_blocks = re.findall(r"<user_message>\n(.*?)\n</user_message>", snapshot, re.DOTALL)
        # The steer RPC wraps message.strip(); pending stores the raw message.
        # Strip for parity so whitespace never causes a false NON-match (the
        # safe direction is a duplicate, but exact settling is better).
        consumed_counts: dict[str, int] = {}
        for block in consumed_blocks:
            consumed_counts[block] = consumed_counts.get(block, 0) + 1
        remaining = []
        for m in slot._pending_steers:
            key = m.strip()
            if consumed_counts.get(key, 0) > 0:
                consumed_counts[key] -= 1
            else:
                remaining.append(m)
    else:
        remaining = []
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

    hold_users = bool(
        state.subagents is not None and state.subagents.running_agents_for(f"dashboard:{slot.key}")
    )
    if hold_users:
        next_msg, consumed = _dequeue_next_system_message(slot)
    else:
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=merge)
    if next_msg is None:
        return False

    if slot._stopping and not is_system_injection(next_msg):
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
    is_subagent = next_msg.startswith(SUBAGENT_COMPLETION_PREFIX)
    is_recovery = (
        next_msg.startswith(REFUSAL_RECOVERY_PREFIX)
        or next_msg.startswith(STALE_RECOVERY_PREFIX)
        or next_msg.startswith(TOOL_STALL_RECOVERY_PREFIX)
        or any(is_synthetic_recovery_item(item) for item in consumed)
    )
    if not (is_cron or is_subagent or is_recovery):
        slot._pending_synthesis = False
    match = CRON_NOTIFY_RE.match(next_msg) if is_cron else None
    cron_label = match.group(1) if match else "cron"
    cron_label, _ = redact_exfiltration_urls(cron_label)
    cron_label, _ = redact_credentials(cron_label)
    slot.append(
        "subagent" if is_subagent else "inject" if (is_cron or is_recovery) else "user",
        next_msg,
        (
            json.dumps({"cronLabel": cron_label})
            if is_cron
            else "msg msg-inject" if is_recovery else "msg msg-u"
        ),
    )

    task = spawn_guarded_turn(state, slot, _run_chat(state, slot, next_msg))
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
        synthesis_task = spawn_guarded_turn(
            state, slot, _run_chat(state, slot, SUBAGENT_SYNTHESIS_PROMPT)
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


async def _run_chat(
    state: DashboardState,
    slot: _ChatSlot,
    message: str,
    *,
    _prompt_depth: int = 0,
    regenerate_hint: str = "",
) -> None:
    """Stream LLM response into *slot*.  Survives browser disconnect."""

    session_key = effective_session_key(slot)
    sessions = getattr(state, "sessions", None)

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
    _orch_planning = (
        getattr(slot, "mode", "") == "orchestrator"
        and not getattr(slot, "_in_stage_execution", False)
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
    _auth_required = False
    saw_compaction = False
    _turn_tool_calls = 0  # tool dispatches this turn (refusal diagnostic)
    _retrying_empty = False
    # Recoverable tool refusals (host-gate policy deny / read-only bash gate)
    # recorded during this turn as (redacted_title, reason). If non-empty when
    # the turn ends — and the user did not stop it — a recovery continuation is
    # enqueued so the model learns why and can adapt instead of stalling.
    _refusal_reasons: list[tuple[str, str]] = []
    # True when this turn IS an automatic refusal-recovery continuation. Used to
    # keep the synthetic prompt out of the linked-Slack user-message mirror.
    _is_recovery = message.startswith(REFUSAL_RECOVERY_PREFIX)
    # The post-fan-out synthesis prompt is a synthetic continuation too: never
    # mirror it to linked surfaces (Slack/Telegram) as if the user typed it —
    # only its assistant reply is delivered.
    _is_synthetic = _is_recovery or message.startswith(SUBAGENT_SYNTHESIS_PREFIX)

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
        try:
            cfg = KiroCrewConfig.load()
            bindings = resolve_agent_bindings(cfg, slot.agent or None)
            kiro_agent = bindings.kiro_agent
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
            model=slot.model or agent_model or None,
            cwd=slot.project or None,
            reasoning_effort_override=slot.reasoning_effort or None,
        )
        _acquired = True
        # Publish the live inner AcpClient onto the slot so a concurrent request
        # (the dashboard steer handler) can reach the running session's client
        # to inject a mid-turn steer. Cleared in the finally below.
        slot._acp_client = getattr(client, "client", None)
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
        if not slot.model:
            slot.model = _backfill_canonical_model(client, cfg.agent.provider) or slot.model
        agent_label = kiro_agent or slot.agent or "default"
        model_label = slot.model or "auto"
        if resumed:
            state.broadcast_ws(
                "activity_event",
                {
                    "slot": slot.key,
                    "kind": "session",
                    "text": f"Session resumed · {agent_label} · {model_label}",
                },
            )
        else:
            state.broadcast_ws(
                "activity_event",
                {
                    "slot": slot.key,
                    "kind": "session",
                    "text": f"Session created · {agent_label} · {model_label}",
                },
            )

        # Propagate trust/YOLO to session so subagents inherit auto-approve.
        if slot._trust or state.is_yolo_active():
            state.sessions.set_approval_policy(session_key, "auto")
        else:
            state.sessions.set_approval_policy(session_key, "")

        # The slot is the source of truth for plan mode; the enforcement
        # registry is process-global and starts empty after a restart. Re-sync
        # every turn so a restored planning session is still gated, and so a
        # session that left plan mode does not keep a stale entry.
        plan_mode.set_active(session_key, bool(getattr(slot, "plan_mode", False)))

        # Drain MCP OAuth requests captured during session init. kiro-cli
        # buffers `_kiro.dev/mcp/oauth_request` notifications during MCP
        # server bring-up; the AcpClient collected them into
        # `pending_oauth_requests`. Surface each as an inline banner so the
        # user can authorize the affected MCP server.
        try:
            acp_client = getattr(client, "client", None)
            pop_pending = getattr(acp_client, "pop_pending_oauth_requests", None)
            if callable(pop_pending):
                for req in pop_pending():
                    _emit_mcp_oauth_request(
                        state, slot, req.get("serverName", ""), req.get("oauthUrl", "")
                    )
        except Exception:  # pragma: no cover — never let UI surfacing kill chat init
            logger.warning("Failed to surface pending MCP OAuth requests", exc_info=True)

        # Publish this turn's session identity so managed MCP tools resolve
        # X-Session-Key; one shared writer lives in messaging.identity.
        await publish_turn_identity(state.sessions, session_key)

        # ── @prompt expansion: resolve @name to SOP/prompt content ──
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
                compressed = build_session_replay(
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
            # Theme persona injection — prepend to message so build_message
            # accounts for it in context budget calculations.
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
            # build_message performs blocking work (episodic query embed via
            # urllib to Ollama, file reads) — run off-loop (mc-embed bulkhead)
            # so a slow embedding endpoint can't stall the gateway event loop.
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
                plan_mode=bool(getattr(slot, "plan_mode", False)),
                blocks_reads=slot.blocks_reads,
                provider_type=cfg.agent.provider,
                runtime_source="dashboard",
                exclude_last_n=1,
                folder_path=folder_path,
                model_window=model_window,
            )
            full_message = _apply_incognito_prefix(slot, full_message)
            if is_new:
                ctx_len = len(full_message) - len(message)
                state.broadcast_ws(
                    "activity_event",
                    {
                        "slot": slot.key,
                        "kind": "context",
                        "text": f"Injected {ctx_len:,} chars of context (memory, lessons, history, episodic)",
                    },
                )
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
                history_key = effective_session_key(slot)
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
        event_stream = client.stream_command(message) if is_slash else client.stream(full_message)
        state.broadcast_ws("chat_status", {"slot": slot.key, "status": "Thinking…"})
        state.broadcast_ws(
            "activity_event", {"slot": slot.key, "kind": "status", "text": "Thinking…"}
        )

        # ── Bidirectional sync: mirror user message to linked Slack thread ──
        if state.slack_client and not is_slash and not _is_synthetic:
            _mirror_thread, _mirror_chan = state.sessions.get_slack_link(session_key)
            if _mirror_thread and _mirror_chan:
                try:
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
                _title, _ = redact_exfiltration_urls(event.title)
                _title, _ = redact_credentials(_title)
                _kind, _ = redact_exfiltration_urls(event.tool_kind)
                _kind, _ = redact_credentials(_kind)
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
                    {
                        "slot": slot.key,
                        "tool": _title,
                        "kind": _kind,
                        "tool_call_id": _redact_tool_field(event.tool_call_id),
                        "purpose": _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE),
                        "input_preview": _redact_tool_field(event.tool_input),
                    },
                )
                slot.append("tool", f"🔧 {_title}", "msg msg-tool", meta=_tool_meta(event))
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name=event.title,
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
                        _task_title = event.tool_purpose or _title
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
                            "is_update": True,
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
                            # The gate already AUTHENTICATED this as a directive
                            # tool via the canonical _meta identity, and this is
                            # the FINAL frame — so a marker that does not decode
                            # means the effect is being dropped outright. Never
                            # let that be silent: this exact silence can hide a
                            # rawOutput-envelope escaping bug.
                            # Mid-stream frames legitimately decode to None and
                            # are excluded by the tool_final guard.
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
                        _deny_title, _ = redact_exfiltration_urls(event.title)
                        _deny_title, _ = redact_credentials(_deny_title)
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
                            tool_name=event.title,
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
                    if tool_result.action == TOOL_AUTO_APPROVE:
                        try:
                            validated_tool = _validate_tool_name(
                                event.title, is_shell=event.is_shell
                            )
                        except ValueError as e:
                            await client.reject_tool(event.request_id)
                            slot.append("tool", f"🚫 {event.title} (invalid: {e})", "msg msg-tool")
                            sel().log_tool_invocation(
                                session_key=session_key,
                                agent=slot.agent or "kirocrew",
                                source="dashboard",
                                tool_name=event.title,
                                tool_kind=event.tool_kind,
                                outcome="denied",
                                request_id=event.request_id,
                                error=f"validation_failed: {e}",
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
                                await client.reject_tool(event.request_id)
                                slot.append(
                                    "tool",
                                    f"🚫 {event.title} (hook error)",
                                    "msg msg-tool",
                                )
                                sel().log_tool_invocation(
                                    session_key=session_key,
                                    agent=slot.agent or "kirocrew",
                                    source="dashboard",
                                    tool_name=event.title,
                                    tool_kind=event.tool_kind,
                                    outcome="hook_error",
                                    request_id=event.request_id,
                                    error=str(hook_exc),
                                )
                                continue
                            if _pre_tool_hooks_should_block(pre_hook_results):
                                await client.reject_tool(event.request_id)
                                slot.append(
                                    "tool",
                                    f"🚫 {event.title} (hook blocked)",
                                    "msg msg-tool",
                                )
                                sel().log_tool_invocation(
                                    session_key=session_key,
                                    agent=slot.agent or "kirocrew",
                                    source="dashboard",
                                    tool_name=event.title,
                                    tool_kind=event.tool_kind,
                                    outcome="hook_blocked",
                                    request_id=event.request_id,
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
                        await client.reject_tool(event.request_id)
                        slot.append("tool", f"🚫 {event.title} (invalid: {e})", "msg msg-tool")
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="denied",
                            request_id=event.request_id,
                            error=f"validation_failed: {e}",
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
                        await client.reject_tool(event.request_id)
                        slot.append("tool", f"🚫 {event.title} (hook error)", "msg msg-tool")
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="hook_error",
                            request_id=event.request_id,
                            error=str(hook_exc),
                        )
                        continue
                    if _pre_tool_hooks_should_block(pre_hook_results):
                        await client.reject_tool(event.request_id)
                        slot.append("tool", f"🚫 {event.title} (hook blocked)", "msg msg-tool")
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="hook_blocked",
                            request_id=event.request_id,
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
                if _native_crew_should_auto_approve(_native_tracker, state, slot):
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
                if slot._trusted_patterns:
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
                            _safe, _ = redact_exfiltration_urls(event.title)
                            _safe, _ = redact_credentials(_safe)
                            slot.append(
                                "tool",
                                f"🚫 {_safe} (invalid: {e})",
                                "msg msg-tool",
                            )
                            sel().log_tool_invocation(
                                session_key=session_key,
                                agent=slot.agent or "kirocrew",
                                source="dashboard",
                                tool_name=event.title,
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
                # Trust-reads: auto-approve read-only bash commands
                # Detect bash tools by tool_input content (title is human-readable)
                cmd = _extract_bash_command(event.tool_input) if event.tool_input else ""
                yolo_active = state.is_yolo_active()
                if slot._trust_reads and not slot._trust and not yolo_active and cmd:
                    if is_read_only_bash(cmd):
                        try:
                            validated_tool = _validate_tool_name(
                                event.title, is_shell=event.is_shell
                            )
                        except ValueError as e:
                            await client.reject_tool(event.request_id)
                            slot.append(
                                "tool",
                                f"🚫 {event.title} (invalid: {e})",
                                "msg msg-tool",
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
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="auto_approved",
                            request_id=event.request_id,
                            metadata={"reason": "trust_reads"},
                        )
                        continue
                # Trust mode (per-slot) or YOLO mode (global) — auto-approve
                if slot._trust or yolo_active:
                    try:
                        validated_tool = _validate_tool_name(event.title, is_shell=event.is_shell)
                    except ValueError as e:
                        await client.reject_tool(event.request_id)
                        slot.append("tool", f"🚫 {event.title} (invalid: {e})", "msg msg-tool")
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="denied",
                            request_id=event.request_id,
                            error=f"validation_failed: {e}",
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
                            await client.reject_tool(event.request_id)
                            slot.append("tool", f"🚫 {event.title} (hook error)", "msg msg-tool")
                            sel().log_tool_invocation(
                                session_key=session_key,
                                agent=slot.agent or "kirocrew",
                                source="dashboard",
                                tool_name=event.title,
                                tool_kind=event.tool_kind,
                                outcome="hook_error",
                                request_id=event.request_id,
                                error=str(hook_exc),
                            )
                            continue
                        if _pre_tool_hooks_should_block(pre_hook_results):
                            await client.reject_tool(event.request_id)
                            slot.append("tool", f"🚫 {event.title} (hook blocked)", "msg msg-tool")
                            sel().log_tool_invocation(
                                session_key=session_key,
                                agent=slot.agent or "kirocrew",
                                source="dashboard",
                                tool_name=event.title,
                                tool_kind=event.tool_kind,
                                outcome="hook_blocked",
                                request_id=event.request_id,
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
                        metadata={"reason": "yolo" if yolo_active else "trust"},
                    )
                    continue
                # Auto-reject remaining tools after one rejection in a batch
                if getattr(slot, "_batch_rejected", False):
                    await client.reject_tool(event.request_id)
                    _title, _ = redact_exfiltration_urls(event.title)
                    _title, _ = redact_credentials(_title)
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
                        tool_name=event.title,
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
                # Pre-compute pattern fields for the TrustDropdown
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
                    _safe_title,
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
                # never sees it — the turn then parks here until the 2h timeout,
                # holding the slot lock and silently dropping inbound messages.
                # The Slack click resolves THIS future (via state.resolve_approval);
                # we remain the sole caller of approve_tool/reject_tool below.
                _slack_approval_ts: str | None = None
                if (
                    slot._slack_linked
                    and slot._slack_channel
                    and slot._slack_thread_ts
                    and state.slack_client
                ):
                    try:
                        _slack_approval_ts = await post_linked_approval(
                            state.slack_client,
                            slot._slack_channel,
                            slot._slack_thread_ts,
                            event.request_id,
                            session_key,
                            event.title,
                            event.tool_input or "",
                        )
                        if _slack_approval_ts is None:
                            # Delivery failed — do not silently park for 2h.
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
                        # to the 2h wait_for with an unresolved future — the exact
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
                try:
                    outcome = await asyncio.wait_for(fut, timeout=7200.0)
                except asyncio.TimeoutError:
                    outcome = "rejected"
                finally:
                    slot._approval_futures.pop(str(event.request_id), None)
                    # Backstop: the future is now gone, so the permission
                    # message MUST NOT be left reading pending — the UI would
                    # keep rendering an approval bar whose every button answers
                    # 404, and a history reload would resurrect it. The primary
                    # resolvers (HTTP slot-approve, Slack click) already mark it
                    # and record richer decisions like "trust"/"yolo", so only
                    # write when still pending. This is the sole marker for the
                    # paths that resolve the future in-process: the 2h timeout
                    # above and the Slack-delivery auto-reject branches.
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
                            {"id": str(event.request_id), "approved": _approved},
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
                        await client.reject_tool(event.request_id)
                        slot.append("tool", f"🚫 {event.title} (invalid: {e})", "msg msg-tool")
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="denied",
                            request_id=event.request_id,
                            error=f"validation_failed: {e}",
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
                        await client.reject_tool(event.request_id)
                        slot.append("tool", f"🚫 {event.title} (hook error)", "msg msg-tool")
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="hook_error",
                            request_id=event.request_id,
                            error=str(hook_exc),
                            metadata={"reason": "interactive"},
                        )
                        break
                    if _pre_tool_hooks_should_block(pre_hook_results):
                        await client.reject_tool(event.request_id)
                        slot.append("tool", f"🚫 {event.title} (hook blocked)", "msg msg-tool")
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="hook_blocked",
                            request_id=event.request_id,
                            metadata={"reason": "interactive"},
                        )
                    else:
                        await client.approve_tool(event.request_id)
                        _approved_title, _ = redact_exfiltration_urls(event.title)
                        _approved_title, _ = redact_credentials(_approved_title)
                        slot.append(
                            "tool", f"✅ {_approved_title}", "msg msg-tool", meta=_tool_meta(event)
                        )
                        sel().log_tool_invocation(
                            session_key=session_key,
                            agent=slot.agent or "kirocrew",
                            source="dashboard",
                            tool_name=event.title,
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
                        surface="dashboard",
                        # Resolved agent, not the slot alias: resolve_agent_bindings
                        # maps e.g. "default" to "kirocrew" before dispatch, so the
                        # alias would credit an agent that never ran.
                        agent=read_effective_agent(client) or slot.agent or "",
                        context_used=_ctx_used,
                        context_window=_ctx_window,
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
                _emit_turn_metric(
                    event.usage.duration_ms,
                    event.stop_reason,
                    slot.key,
                    elapsed_ms=_turn_elapsed_ms,
                )
                _stop_reason = event.stop_reason
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
                slot.queue_insert(0, f"{STALE_RECOVERY_PREFIX}\n{build_stale_recovery_prompt()}")
                _emit_stale("⟳ Recovering a stalled turn…")
            elif slot._stale_recovery_retries >= 3:
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
                slot.queue_insert(0, f"{TOOL_STALL_RECOVERY_PREFIX}\n{_body}")
                _emit_stall("⟳ Tool appeared stalled — recovering…")
            elif slot._tool_stall_retries >= 3:
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
                slot.queue_insert(0, message)
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
            slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
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
                compaction_result = await client.wait_for_compaction(timeout=120.0)
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
                    msg = "❌ Compaction failed."
                else:
                    msg = "⚠️ Compaction timed out."
                _append_compaction_notice(state, slot, msg)
                # Update context usage after compaction. On success the
                # provider dropped its stale counts when the completed status
                # arrived (AcpPromptStats.reset_after_compaction), then
                # wait_for_compaction grace-drained for kiro's fresh
                # post-compaction metadata (~1s after the status), which
                # re-derives REAL numbers against the kept served window. If
                # that metadata arrived, broadcast the accurate payload; if
                # not, fall back to the `reset` form so the frontend drops its
                # stale counts (same contract as the threshold auto-compact
                # path) and the meter self-corrects on the next turn. On
                # failure/timeout the counts are unchanged and still valid, so
                # re-send them as-is.
                if compaction_result["type"] == "completed":
                    _payload = _context_usage_payload(slot.key, client)
                    if _payload.get("used_tokens"):
                        state.broadcast_context_usage(slot.key, _payload)
                    else:
                        state.broadcast_context_usage(
                            slot.key, {"slot": slot.key, "pct": 0.0, "reset": True}
                        )
                else:
                    state.broadcast_context_usage(
                        slot.key, _context_usage_payload(slot.key, client)
                    )

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
                slot.queue_insert(0, message)
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
                slot.queue_insert(0, _EMPTY_AUTO_CONTINUE_MSG, kind=SYNTHETIC_RECOVERY_KIND)
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
            slot._empty_response_retries = 0
            slot._prompt_busy_retries = 0
            slot._acp_pipe_death_retries = 0
            slot._stale_recovery_retries = 0
            slot._tool_stall_retries = 0
            slot._transient_5xx_retries = 0
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
        await _fire(HOOK_EVENT_STOP, _final)

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
                )

        # ── Bidirectional sync: mirror response to linked Slack thread ──
        if assistant_text and state.slack_client and _mirror_thread and _mirror_chan:
            try:
                from kiro_crew.slack.format import (  # circular: slack.format -> dashboard.state -> chat
                    build_options_blocks,
                    extract_options,
                    split_message,
                    to_slack_mrkdwn,
                )

                _mirror_text = to_slack_mrkdwn(assistant_text)
                _mirror_text = redact_exfiltration_urls(_mirror_text)[0]
                _mirror_text = redact_credentials(_mirror_text)[0]
                _mirror_text, _mirror_options = extract_options(_mirror_text)

                for _part in split_message(_mirror_text):
                    await state.slack_client.post_message(_mirror_chan, _part, _mirror_thread)
                if _mirror_options:
                    await state.slack_client.post_blocks(
                        _mirror_chan,
                        build_options_blocks(_mirror_options),
                        "Options",
                        _mirror_thread,
                    )
            except Exception:
                logger.debug("Failed to mirror response to Slack", exc_info=True)

        # Channel-neutral leg: deliver the completed reply to a linked non-Slack
        # proactive channel (e.g. Telegram) via Transport.send_message. Slack is
        # handled above by its dedicated streaming mirror. Gated identically to
        # the user-message leg so a slash/recovery turn never mirrors an orphan
        # reply with no preceding question.
        if not is_slash and not _is_recovery:
            await _deliver_cross_surface_reply(state, session_key, assistant_text)
    except asyncio.CancelledError:
        if assistant_text:
            slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
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
            slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
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
            slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
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
            slot.queue_insert(0, message)
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
            slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
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
            slot.queue_insert(0, message)
        elif slot._prompt_busy_retries > 3:
            slot.append("error", "Session stuck — please start a new chat.", "msg msg-err")
        else:
            # depth>0 with budget remaining: no re-queue (mirrors AcpProcessDied),
            # but still surface feedback so the nested turn doesn't fail silently.
            slot.append("error", "⟳ Session busy — please retry.", "msg msg-err")
    except AcpError as exc:
        logger.warning("ACP error in slot %s: %s", slot.key, exc)
        _msg = str(exc)
        # Retry-eligible transients:
        #   - "already in progress": prompt busy (kiro-cli side)
        #   - "process exited" / "not running": ACP subprocess died, need cold-start
        # For both: reset the session and re-queue the message so auto-nudges
        # (and dashboard messages) get executed on a fresh provider instead of
        # surfacing a bare ❌ error card with no work done.
        _retry_eligible = (
            "already in progress" in _msg or "process exited" in _msg or "not running" in _msg
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
                slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
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
                slot.queue_insert(0, message)
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
            # session.
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
            slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
            if _should_suppress_requeue(slot):
                pass
            elif _prompt_depth == 0:
                # Single emit (see AcpProcessDied note): slot.append persists +
                # broadcasts one chat_message; no explicit broadcast_ws. Back off,
                # then re-queue — the finally block dequeues onto the SAME live
                # session (no reset), preserving conversation state.
                slot.append("error", "⟳ Backend hiccup — retrying…", "msg msg-err")
                await asyncio.sleep(_delay)
                slot.queue_insert(0, message)
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
                slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
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
                slot.queue_insert(0, _POSTTOKEN_RECOVER_MSG, kind=SYNTHETIC_RECOVERY_KIND)
            # else: Stop active (_should_suppress_requeue) or nested turn
            # (_prompt_depth != 0) — do NOT requeue; partial + notice already
            # shown, so the streamed answer survives in the transcript. The
            # allowance is left UNconsumed so a later turn can still recover once.
        else:
            if assistant_text:
                _safe, _ = redact_exfiltration_urls(assistant_text)
                _safe, _ = redact_credentials(_safe)
                slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
                slot.append("assistant", _safe, "msg msg-a")
            _err_text, _ = redact_exfiltration_urls(str(exc))
            _err_text, _ = redact_credentials(_err_text)
            slot.append(
                "error",
                f"⏱️ {_err_text}" if "timed out" in _msg else f"❌ {_err_text}",
                "msg msg-err",
            )
    except Exception as exc:
        logger.exception("Dashboard chat error in slot %s", slot.key)
        _err_text, _ = redact_exfiltration_urls(str(exc))
        _err_text, _ = redact_credentials(_err_text)
        slot.append("error", _err_text, "msg msg-err")
        await state.sessions.record_failure(session_key)
    finally:
        # Completion can be bypassed by cancellation, provider errors, or timeouts.
        # Close cards idempotently and retain only bounded terminal records for
        # reconnect replay until the next turn installs a fresh tracker.
        try:
            _native_subagent_close_all(state, slot, _native_tracker, _native_card_output)
        finally:
            slot._native_subagent_tracker = _retain_terminal_native(_native_tracker)
            slot._native_subagent_output = {}
        slot._batch_rejected = False
        # Steer handle: turn is over, drop the live client ref so a late steer
        # can't target a dead session (the route also re-checks running state).
        slot._acp_client = None
        # Same lifecycle for the segment-cut handle: a late steer must not
        # flush into a finished turn's (already-flushed) locals.
        slot._steer_segment_cut = None
        # Ensure file changes always surface, even on cancel/error. Wrapped so
        # a raise here cannot skip the re-arm below and re-introduce the orphan
        # bug this fix prevents.
        try:
            _flush_file_changes(slot)
        except Exception:
            logger.debug("_flush_file_changes failed", exc_info=True)
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
            if _acquired and needs_session_reset:
                try:
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
        # End-of-turn fallback: catches set_project calls that fired mid-turn,
        # after the start-of-turn consume already ran. Guarded because a raise
        # here would skip the steer requeue and queue drain below, silently
        # stranding queued work at the end of an otherwise successful turn.
        try:
            await _consume_pending_reset(state, slot)
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
