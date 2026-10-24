"""Core LLM runner — _run_chat, segment flushing, prompt expansion."""

from __future__ import annotations

import asyncio
import difflib  # noqa: F401
import functools  # noqa: F401
import hashlib
import inspect
import json
import logging
import math
import os  # noqa: F401
import re
import stat as stat_module  # noqa: F401
import time
import uuid
import weakref
from pathlib import Path
from typing import (  # noqa: F401
    Any,
    Awaitable,
    Callable,
    Concatenate,
    Coroutine,
    NamedTuple,
    ParamSpec,
)

from kiro_crew import (  # noqa: F401
    context_omissions,
    mcp_apps_render,
    model_registry,
    resource_status,
    runtime_death,
    session_directive,
    shutdown_event,
)
from kiro_crew.acp.client import (
    AcpAuthRequired,
    AcpConversationBindingMismatch,
    AcpError,
    AcpProcessDied,
    AcpPromptBusy,
    _is_safe_oauth_url,
    advertised_model_ids,
    model_is_unusable,
    resolve_pin_spelling,
)
from kiro_crew.acp.types import (
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_STEER_CONSUMED,
    STOP_CLASS_FAILED,
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    STOP_RECOVERY_MAX_RETRIES,
    TERMINAL_TOOL_STATUSES,
    WAIT_REASON_INPUT,
    RefusalInfo,
    StopClass,
    StructuredStatus,
    TurnUsage,
    classify_stop_reason,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_LOCAL_CLEAR,
    Routing,
    routing_for,
)
from kiro_crew.agent_discovery import (
    agent_welcome_message,
    session_skill_globs,
    warm_project_agent_names,
)
from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name  # noqa: F401
from kiro_crew.agent_sdk.capabilities import capabilities_of
from kiro_crew.agent_sdk.spec_hooks import (
    invalidate_stale_kas_session,
    refuse_stale_switch,
    reproject_claimed_session,
)
from kiro_crew.autonudge import get_instance
from kiro_crew.config.loader import (  # noqa: F401
    KiroCrewConfig,
    data_home,
    refresh_materialized_agents,
    resolve_agent_bindings,
    resolve_effective_model,
)
from kiro_crew.config.sections import ResolvedBindings
from kiro_crew.connections import get_visible_providers  # noqa: F401
from kiro_crew.constants import strip_control_comments  # noqa: F401
from kiro_crew.constants import STEER_NOTICE_BOUND_SECS
from kiro_crew.context import (
    discard_memory_prefetch,
    embed_startup_query,
    prefetch_session_memory,
    reserve_memory_prefetch,
)
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard import chat_turn as _chat_turn
from kiro_crew.dashboard import directive_queue
from kiro_crew.dashboard import git_repo_touches as _git_repo_touches
from kiro_crew.dashboard.chat_delivery import COMMANDS_OFF_META_KEY  # noqa: F401
from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY as _TURN_ACTOR_META_KEY
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop  # noqa: F401
from kiro_crew.dashboard.chat_summary import generate_session_summary
from kiro_crew.dashboard.chat_title import title_then_refresh
from kiro_crew.dashboard.chat_turn.account_identity import (
    _mark_kiro_signed_out,
    _retire_sessions_on_identity_change,
)
from kiro_crew.dashboard.chat_turn.acp_recovery import (  # noqa: F401
    _binding_rebuild_allowed,
    _on_acp_binding_mismatch,
    _on_acp_image_unsupported,
    _on_acp_posttoken_transient,
    _on_acp_retryable,
    _on_acp_session_not_found,
    _on_acp_transient_exhausted,
    _on_acp_transient_retry,
    _on_process_died,
)
from kiro_crew.dashboard.chat_turn.app_agent_binding import (
    _on_app_agent_not_loaded,
    _recover_app_agent_binding,
)
from kiro_crew.dashboard.chat_turn.crew_log_attribution import (
    PENDING_CHANNEL_ATTR,
    _crew_log_class,
    _crew_log_workspace,
    _slot_predecessor_store,
    _turn_close_crew_log,
)
from kiro_crew.dashboard.chat_turn.decisions_hooks import _JEV_ROUTE_AUTO_MODELS
from kiro_crew.dashboard.chat_turn.deny_chokepoints import _steer_policy_notice
from kiro_crew.dashboard.chat_turn.eager_spawn import (
    schedule_eager_spawn,
    stop_eager_spawns,
)
from kiro_crew.dashboard.chat_turn.file_changes import _MAX_RECONSTRUCT_BYTES  # noqa: F401
from kiro_crew.dashboard.chat_turn.mcp_session import (  # noqa: F401
    _on_mcp_oauth_request,
    _on_mcp_server_init_failure,
    _on_mcp_server_initialized,
)
from kiro_crew.dashboard.chat_turn.model_fallback import (  # noqa: F401
    _fallback_swap_for_turn,
    _on_acp_model_access,
    _sync_served_model,
)
from kiro_crew.dashboard.chat_turn.native_cards import (
    _on_subagent_activity,
    _on_subagent_list,
)
from kiro_crew.dashboard.chat_turn.permission_prompt import _on_permission_prompt
from kiro_crew.dashboard.chat_turn.prompt_assembly import (  # noqa: F401
    _expand_prompt_mention,
    _resolve_prompt_mention,
    _slot_prompt_project,
    _surface_prompt_chip,
    _turn_build_prompt,
    _turn_settle_debts,
)
from kiro_crew.dashboard.chat_turn.queue_cycle import (
    TURN_FAILED_META,
    _end_turn_tail,
    _note_failed_before_the_try,
    _turn_record_outcome,
    _TurnExit,
)
from kiro_crew.dashboard.chat_turn.queue_drain import (
    _arm_queued_delivery_settlement,
    _start_next_queued_turn,
    drain_idle_parent_queue_for_stall,
    subagents_hold_user_messages,
)
from kiro_crew.dashboard.chat_turn.recipient import (  # noqa: F401
    _authorize_recipient,
    _resolve_channel_target,
    _resolve_mirror_target,
)
from kiro_crew.dashboard.chat_turn.recovery import (  # noqa: F401
    _answer_text_only,
    _current_turn_carries_image_ref,
    _should_suppress_requeue,
)
from kiro_crew.dashboard.chat_turn.segment_flush import (
    _accumulate_segment_raw,
    _flush_segment,
    _redact_delta,
    _turn_settle_answer,
)
from kiro_crew.dashboard.chat_turn.session_events import (
    _on_agent_switched,
    _on_clear_status,
    _on_compaction_status,
    _on_todo_update,
)
from kiro_crew.dashboard.chat_turn.session_reset import _turn_release_session
from kiro_crew.dashboard.chat_turn.slash_commands import (
    _answer_slash_without_channel,
    _turn_compact_command,
    _turn_slash_gate,
)
from kiro_crew.dashboard.chat_turn.steer_queue import (  # noqa: F401
    _has_user_queued_followup,
    _on_steer_consumed,
    _requeue_unconsumed_steers,
)
from kiro_crew.dashboard.chat_turn.stop_recovery import (
    _turn_persist_and_settle,
    _turn_stop_recovery,
)
from kiro_crew.dashboard.chat_turn.tool_approval import (  # noqa: F401
    _auto_approve_reason,
    _on_permission_request,
    _slot_is_trusted,
)
from kiro_crew.dashboard.chat_turn.tool_results import _on_tool_result
from kiro_crew.dashboard.chat_turn.tool_rows import (
    _MAX_TCID_SOURCES,
    _on_tool_call,
    _on_tool_call_update,
    _tcid_identity_key,
)
from kiro_crew.dashboard.chat_turn.turn_admission import _turn_admit
from kiro_crew.dashboard.chat_turn.turn_context import _startup_query_prewarm_applies  # noqa: F401
from kiro_crew.dashboard.chat_turn.turn_dispatch_gates import (
    _turn_dispatch_gates,
    _turn_dispatch_prep,
)
from kiro_crew.dashboard.chat_turn.turn_ending import _turn_ending_chain
from kiro_crew.dashboard.chat_turn.turn_entry import (
    _stop_pressed,
    _turn_open,
    _turn_release_handles,
)
from kiro_crew.dashboard.chat_turn.turn_failures import (  # noqa: F401
    _on_acp_error_card,
    _on_auth_required,
    _on_cancelled,
    _on_error,
    _on_prompt_busy,
    _on_session_closing,
)
from kiro_crew.dashboard.chat_turn.turn_ledgers import (
    _replay_owes_delivery,
    _report_consumed,
)
from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow
from kiro_crew.dashboard.chat_turn.turn_marker import (  # noqa: F401
    _clear_local_turn_marker,
    _gateway_shutdown_requested,
)
from kiro_crew.dashboard.chat_turn.turn_reply_seams import (
    _cut_segment,
    _flush_thinking_stream,
    _settle_held,
)
from kiro_crew.dashboard.chat_turn.turn_spec_hooks import _turn_stop_hook
from kiro_crew.dashboard.chat_turn.turn_stats import (  # noqa: F401
    _MAX_MODEL_ID_LEN,
    _context_usage_payload,
    _on_complete,
    _turn_context_usage,
    turn_stats_meta,
)
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    _BLOCKED_SLASH_COMMANDS,
    _MAX_TOOL_PURPOSE,
    _dequeue_next_message,
    _dequeue_next_system_message,
    _maybe_consolidate,
    _maybe_inject_persona,
    _redact_for_display,
    _redact_tool_field,
    _validate_tool_name,
    chat_done_payload,
    chunk_generation,
    effective_session_key,
    expire_slack_options,
    mirror_is_paused,
    open_construct_at_end,
    run_to_completion,
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
from kiro_crew.dashboard.mate_thread_wakes import note_turn_end as _note_mate_wakes
from kiro_crew.dashboard.poisoned_escalation import POISONED_SESSION_CYCLES  # noqa: F401
from kiro_crew.dashboard.recovery_replays import SESSION_NOT_FOUND_CANCELLED_TEXT  # noqa: F401
from kiro_crew.dashboard.recovery_replays import ReplayFamily

# ``apply_session_directive`` is part of the
# runner's base-names surface (``_BASE_NAMES`` in
# test_chat_runner_composition_contract.py), which callers and tests read off
# ``chat_runner`` directly; the ratchet keeps it bound here even though the
# turn loop itself consumes the structured outcome.
from kiro_crew.dashboard.session_directive_apply import (  # noqa: F401
    apply_session_directive,
    apply_session_directive_outcome,
)
from kiro_crew.dashboard.state import (  # noqa: F401
    _MAX_SLOT_MESSAGES,
    CRON_NOTIFY_PREFIX,
    NATIVE_SUBAGENT_DONE_RESULT_CAP,
    NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
    NATIVE_SUBAGENT_OUTPUT_HARD,
    NATIVE_SUBAGENT_OUTPUT_TAIL,
    NATIVE_SUBAGENT_TERMINAL_KEEP,
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    DashboardState,
    _ChatSlot,
    append_and_surface,
    build_infra_retry_prompt,
    build_stale_recovery_prompt,
    context_entry_expired,
    row_mid,
)
from kiro_crew.dashboard.steer_settle import settle_consumed_steers
from kiro_crew.dashboard.turn_dispatch import (
    spawn_guarded_turn,
    tool_approval_timeout_secs,
)
from kiro_crew.deny_guidance import (  # noqa: F401
    DENY_CLASS_AWS_CREDENTIAL,
    DENY_CLASS_SSO_CREDENTIAL,
)
from kiro_crew.execution_context import (  # noqa: F401
    read_session_execution,
    tighten_live_session_execution,
)
from kiro_crew.executors import run_in_embed_pool, subprocess_executor
from kiro_crew.hooks import (  # noqa: F401
    HOOK_EVENT_POST_TOOL_USE,
    fire_tool_hooks,
    safe_read_file,
    validate_file_path,
)
from kiro_crew.image_artifacts import register_images_off_loop
from kiro_crew.llm_helpers import (  # noqa: F401
    _NO_PRIOR_STATS,
    TRANSIENT_RETRIES,
    PromptBusyExhaustedError,
    acp_error_is_session_not_found,
    acp_error_is_transient,
    advance_fallback_candidate,
    connection_recovery_open,
    fallback_rewound_transient_budget,
    first_advertised_fallback,
    probe_fallback_restore,
    provider_last_turn_usage,
    run_bg_oneliner,
    slot_switch_session_lock,
    throttle_retry_delay,
    transient_retry_delay,
    usage_has_billing,
)
from kiro_crew.mcp_discovery import kirocrew_managed_names  # noqa: F401
from kiro_crew.members import (
    record_activity,
    select_provider_backend,
)
from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_NOTICE_AFTER_RECOVERY
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.messaging.link import (  # noqa: F401
    SLACK_NAMESPACE,
    telemetry_channel_of,
)
from kiro_crew.messaging.renderer import chunk_for_transport  # noqa: F401
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.name_grant import (  # noqa: F401
    Refusal,
    pin_human_approval,
    refusal_for_command_off_loop,
)
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
    SessionMcpReport,
)
from kiro_crew.repeat_loop import RepeatLoopTracker
from kiro_crew.safety_override import safety_override  # noqa: F401
from kiro_crew.security import (
    REDACTED_CREDENTIAL_TAG,
    is_sensitive_path,
    oauth_url_contains_credential,
    redact_credentials,
    redact_exfiltration_urls,
    redact_exfiltration_urls_with_records,
)
from kiro_crew.sel import sel, sel_is_warm
from kiro_crew.session import (
    SessionBusyError,
    SessionClosingError,
    SpeculativeResumeRefused,
)
from kiro_crew.session_agent_selection import (  # noqa: F401
    record_agent_selection,
    record_provider_agent_switch,
    resolve_session_agent_bindings,
    restore_agent_selection,
    session_agent_selection_kind,
)
from kiro_crew.slack.handler import post_linked_approval
from kiro_crew.start_priority import StartPriority, person_priority
from kiro_crew.trust_patterns import (  # noqa: F401 -- compatibility re-export
    _mask_quoted_separators,
)
from kiro_crew.trust_patterns import extract_base_command as _extract_base_command
from kiro_crew.trust_patterns import extract_full_command as _extract_full_command
from kiro_crew.trust_patterns import matches_trusted_pattern as _matches_trusted_pattern
from kiro_crew.trust_patterns import (  # noqa: F401 -- compatibility re-export
    split_command_segments as _split_command_segments,
)
from kiro_crew.wakatime.heartbeats import (  # noqa: F401
    note_coding_activity,
    note_coding_cadence,
)
from kiro_crew.widget_artifacts import register_widgets_off_loop

#: Session-start refusals from ``agent_capabilities.prepare_member_capabilities`` that a retry
#: repeats unchanged, each with the chat row's English text naming its fix.
#: The dashboard shows its own localized copy and fix button per code.
_SESSION_START_REFUSAL_TEXT = {
    "materialization_changed": (
        "This crew member's agent file changed outside the Capabilities page. "
        "Open Capabilities, review the change and save it, then start a new chat."
    ),
    "parent_identity_changed": (
        "This crew member's parent template was moved or replaced. Open "
        "Capabilities and stop following the parent, or put the parent back, "
        "then start a new chat."
    ),
    "foreign_private_copy": (
        "This crew member uses another member's private agent. Open the crew "
        "member and pick a different agent under Built from, then start a new chat."
    ),
    "project_identity_changed": (
        "This crew member's saved capabilities belong to the project {project}. "
        "Start the chat in {project}."
    ),
    "private_template_shadowed": (
        "Rename or remove {file} in the .kiro/agents folder of {project}, then "
        "start a new chat. That project file hides this crew member's own agent file."
    ),
}
#: ``project_identity_changed`` when the member saved with no project to name.
_PROJECT_CHANGED_OTHER_TEXT = (
    "This crew member's saved capabilities belong to another project. "
    "Start the chat in the crew member's own project."
)

logger = logging.getLogger(__name__)

#: Shown once when a live backend lost this chat's session and a fresh
#: runtime re-loads it; the turn is retried once behind it.
SESSION_NOT_FOUND_RETRY_TEXT = "⟳ The agent lost this chat's session — reconnecting…"
#: Shown when the one reconnect also failed to get a usable session.
SESSION_NOT_FOUND_GIVE_UP_TEXT = (
    "❌ Could not reconnect this chat's session. Send your message again, "
    "or start a new chat if this keeps happening."
)

# The synthetic recovery message constants live in chat_utils_injected_rows and chat_utils_turn_recovery (single source
# of truth shared with the queue/merge predicates — is_system_injection must
# classify them identically to the turn logic here). Re-exported here so
# callers importing them from chat_runner resolve them.
from kiro_crew.dashboard.chat_utils import (  # noqa: E402, F401
    _POSTTOKEN_RECOVER_MSG,
    CRON_NOTIFICATION_KIND,
    SEED_DECLINED_NOTICE_KIND,
    SUBAGENT_COMPLETION_KIND,
    SYNTHESIS_CLEAR,
    SYNTHETIC_RECOVERY_KIND,
    background_wake_absent,
    should_recover_promise_only,
    subagents_attached_async,
    synthesis_fire_verdict,
)

# Consumption contract carried inside every pending-context frame, between the
# opening delimiter and the injected content. One sentence, imperative, because
# it is re-sent on every turn that drains context: it must be cheap and it must
# be unambiguous. "Respond only to the user's visible message" is what makes
# the feature-request seed start the guided flow instead of being recited;
# "never quote, echo, or reveal" is what keeps internal operator instructions
# out of the visible transcript for every other producer too.
# Best-effort model compliance, NOT a confidentiality boundary: a model can
# ignore it, so pending-context payloads must never carry secrets or content
# that would be harmful if echoed.
_CONTEXT_FRAME_CONTRACT = (
    "This block is silent operator context, not authored by the user: follow "
    "it when shaping your reply, but never quote, echo, restate, or reveal it "
    "— respond only to the user's visible message after this block."
)


# Chars of whole-file before one turn's accumulator may hold beside the capped
# snapshots. A before proven whole keeps its whole text (each at most
# ``_MAX_RECONSTRUCT_BYTES``) until the flush, so an edit past the cut can still
# be told from no edit and shown (``_excerpt_pair``). In memory only, never on
# the message; the oldest entries lose it first (``_record_turn_snapshot``).
_MAX_TURN_FULL_CONTENT_CHARS = 2 * _MAX_RECONSTRUCT_BYTES


# Events that end a denied tool group, clearing ``slot._batch_rejected``.
#
# A plain "Deny" suppresses the REST OF THE GROUP the user just refused: the
# other tool calls the model dispatched from the same assistant message are part
# of the same action, so re-prompting for each is nagging. The flag is cleared
# per ACTION, not per TURN: a turn-lifetime flag auto-denies a call the model
# issues LATER in that turn, after taking the denial as feedback and revising,
# without ever showing it — breaking deny -> discuss -> revise -> retry.
#
# Model-authored output is the boundary: a group's text/reasoning is streamed
# BEFORE its tool_use blocks, so neither of these events ever falls between two
# members of one group, while a revised retry necessarily follows fresh model
# output. Deliberately NOT included is ``EVENT_TOOL_RESULT`` — the denied call's
# own result can arrive as one, which would end the group the user just refused.
#
# Clearing only ever restores the interactive prompt; it never auto-approves. A
# denial still denies, and only its blast radius changes.
_BATCH_REJECT_CLEARED_BY = frozenset({EVENT_TEXT_CHUNK, EVENT_THINKING_CHUNK})


#: Refusal grounds already reported for a conversation in this process, so the
#: per-send leg says each once at its own level and at DEBUG afterwards. Bounded
#: in BOTH dimensions: cleared when full, so a churn of conversation ids cannot
#: grow it without limit, and each entry is a fixed-length digest of its marker,
#: never the marker itself -- a conversation id comes off a mirror row in a file
#: in-sandbox code can write at any length, so retaining it verbatim would let
#: 512 refusals with successively larger ids hold unbounded memory under the cap.
_RECIPIENT_LOGGED: set[str] = set()
_RECIPIENT_LOGGED_CAP = 512


#: The ONE off-loop entry point to the name-grant check, promoted to
#: :mod:`kiro_crew.name_grant` so every surface that honours a name-based grant
#: (this module's rungs, the task runner, subagents, the channel turn driver)
#: shares it. Kept as a module attribute because this name is the seam the
#: dashboard rungs are stubbed through — the rungs below look it up on this
#: module at call time.
_name_grant_refusal_off_loop = refusal_for_command_off_loop


def _member_capable_backend_names() -> str:
    """The ``agent.member_acp_backend`` values that can load a member's saved spec.

    Read from :data:`ACP_BACKENDS_MEMBER_CAPABILITIES` so the remedy a refused member
    chat shows names the set the refusal was judged against. kiro-cli is left out:
    a member backend set to ``kiro`` runs on KAS, which the list already names.
    """
    names = sorted(ACP_BACKENDS_MEMBER_CAPABILITIES - {ACP_BACKEND_KIRO})
    return ", ".join(names[:-1]) + " or " + names[-1] if len(names) > 1 else names[0]


async def _expand_prompt_mention_off_loop(
    message: str,
    state: DashboardState,
    slot: _ChatSlot,
    *,
    project: str | None = None,
) -> tuple[str, str]:
    """:func:`_expand_prompt_mention` with the filesystem half on a worker thread.

    The resolve-and-read half walks the prompt roots and reads the matched file;
    the project's ``.kiro/prompts`` is not the gateway's own directory, may be
    network-backed, and its local half is uncacheable, so on the loop one
    ``@mention`` on slow storage stalls every other request and the heartbeat
    with it. Only that half is offloaded — the chip append is a loop-owned
    mutation (see :func:`_surface_prompt_chip`) and runs here, after the
    ``await``, which is also what keeps it ordered ahead of whatever the caller
    appends next.
    """
    expanded, status, chip = await asyncio.to_thread(
        _resolve_prompt_mention, message, _slot_prompt_project(slot, project)
    )
    _surface_prompt_chip(state, slot, chip)
    return expanded, status


# One retry task per slot key, deduped — a decline observed by the retry task
# itself re-enters _arm_pending_reset_retry and must not stack a second task.
_pending_reset_retries: dict[str, tuple["_ChatSlot", asyncio.Task]] = {}


# Global cap on concurrent speculative spawns. Bounds the process/RSS burst
# when many slots fire signals at once (bulk restore, slot surfing); a spawn
# that cannot get a permit simply skips — the first message cold-starts as
# it does today, so the cap only ever degrades back to current behavior.
_EAGER_SPAWN_MAX_CONCURRENT = 2
_eager_spawn_sem = asyncio.Semaphore(_EAGER_SPAWN_MAX_CONCURRENT)


# Insertion-ordered arm registry (loop-owned, like all chat_runner state):
# session_key -> _RESERVED (admitted, not yet registered) or the arm
# generation the entry registered under (see _arm_generation). Entries leave
# on TTL fire, on eviction, or lazily when an eviction attempt finds the
# session already claimed/gone.
_armed_prefetches: "dict[str, object]" = {}

# Bumped at every registration (_cap_armed_prefetches). An admission carries
# the generation current when its SLOT SIGNAL arrived and may only evict
# entries registered at or before it: an entry registered AFTER the signal
# was armed by a CONCURRENT signal that won the same allowance, not by an
# older one the newer signal outranks. Without this, two simultaneous slot
# signals on an allowance of one spawn twice -- the slower signal's admission
# lands after the faster one registered, sees a "live" entry, evicts the
# session spawned moments ago, and spawns its own.
_arm_generation = 0


def _turn_start_priority(
    *, user_origin: bool, crew_log_actor: str, provenance_restored: bool
) -> StartPriority:
    """The start priority of a turn's cold start (rule: ``kiro_crew.start_priority``).

    FOREGROUND only for a turn a person sent: authenticated-human provenance this
    process observed, owned by the user -- the same predicate the crew-log gates
    use (``_crew_log_actor == "user" and not _turn_provenance_restored``). That
    covers a typed turn, a person's Resume/Continue press and the runner's requeue
    of a person's failed turn; a self-wake, a dispatch-named actor (cron, sub-agent,
    gateway, app, crew) or a restored queue entry starts BACKGROUND.
    """
    return person_priority(user_origin and crew_log_actor == "user" and not provenance_restored)


#: How long the first message's early embedding may wait for the shared embedder.
#: It outlasts a cold session start plus the longest wait a build may join it for
#: (``STARTUP_RECALL_TIMEOUT_MAX_SECS``). Past it a queued embed is dropped and the
#: build embeds for itself; an embed already running finishes and is cached.
_STARTUP_QUERY_PREWARM_SECS = 30.0


async def _prewarm_startup_query(
    state: "DashboardState",
    slot: "_ChatSlot",
    text: str,
    *,
    memory_store: str | None,
    execution_context: Any,
    prefetch_memory: bool = False,
) -> None:
    """Start embedding a turn's first message ahead of its session-start build.

    Runs beside whatever the turn does next (``session/new`` on a cold start). The
    build then reads the cached vector, or joins the running embed and waits for it
    up to ``memory.startup_recall_timeout_secs``, ranking by keyword past it.

    With *prefetch_memory* (a cold start on the send path), the build's memory
    reads start too, after the embed so they join it. The build takes their result
    only when every input still matches (``store_admission``'s early memory reads).
    """
    builder = state.context_builder
    if not _startup_query_prewarm_applies(builder, slot, text, execution_context):
        return
    workspace = slot.workspace or None
    task = asyncio.create_task(
        _embed_startup_query_off_loop(builder, text, workspace=workspace, memory_store=memory_store)
    )
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    # One loop step: the task submits its embed to the recall pool before the caller
    # submits the build, so the build usually finds that embed in flight and joins it
    # within its own deadline. A pool too busy to start it first leaves the build to
    # embed for itself, as it does without this.
    await asyncio.sleep(0)
    if not prefetch_memory:
        return
    # Reserved on the loop, before the thread exists: a build that comes first
    # finds it not started and reads for itself rather than waiting on it.
    reserve_memory_prefetch(workspace, memory_store, text)
    task = asyncio.create_task(
        _prefetch_session_memory_off_loop(
            builder, text, workspace=workspace, memory_store=memory_store
        )
    )
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    await asyncio.sleep(0)


async def _prefetch_session_memory_off_loop(
    builder: Any, text: str, *, workspace: str | None, memory_store: str | None
) -> None:
    """Run ``prefetch_session_memory`` on the recall pool. A failure only costs the head start."""
    from kiro_crew.embeddings import PRIORITY_INTERACTIVE, EmbeddingWork, embedding_work

    # Same deadline as the early embed it joins, so the reads wait for that vector
    # rather than rank by keyword while the session is still starting.
    token = embedding_work.set(
        EmbeddingWork(time.monotonic() + _STARTUP_QUERY_PREWARM_SECS, priority=PRIORITY_INTERACTIVE)
    )
    try:
        await run_in_embed_pool(
            prefetch_session_memory, builder, text, workspace=workspace, memory_store=memory_store
        )
    except Exception:  # noqa: BLE001 - the build reads for itself when this does not
        discard_memory_prefetch(workspace, memory_store, text)
        logger.debug("Session memory was not read early", exc_info=True)
    finally:
        embedding_work.reset(token)


async def _embed_startup_query_off_loop(
    builder: Any, text: str, *, workspace: str | None, memory_store: str | None
) -> None:
    """Run ``embed_startup_query`` on the recall pool. A failure only costs the head start."""
    from kiro_crew.embeddings import PRIORITY_INTERACTIVE, EmbeddingWork, embedding_work

    # A deadline of its own also routes the job to the recall pool, so the embed
    # never holds a worker a prompt build needs.
    token = embedding_work.set(
        EmbeddingWork(time.monotonic() + _STARTUP_QUERY_PREWARM_SECS, priority=PRIORITY_INTERACTIVE)
    )
    try:
        await run_in_embed_pool(
            embed_startup_query, builder, text, workspace=workspace, memory_store=memory_store
        )
    except Exception:  # noqa: BLE001 - the build embeds for itself when this does not
        logger.debug("First-message embedding was not started early", exc_info=True)
    finally:
        embedding_work.reset(token)


# One entry per gateway memory-preparation task that has parked at least one
# slot's queue. The value maps each parked slot key to whether that slot has
# already been shown the notice; the presence of the task key is what says the
# drain callback is armed. Weak so a finished task (and the state its callback
# closes over) is not kept alive by this table.
_PARKED_FOR_MEMORY: "weakref.WeakKeyDictionary[Any, dict[str, bool]]" = weakref.WeakKeyDictionary()


_P = ParamSpec("_P")


def _hands_off_queue_on_exit(
    fn: "Callable[Concatenate[DashboardState, _ChatSlot, _TurnExit, _P], Coroutine[Any, Any, None]]",
) -> "Callable[Concatenate[DashboardState, _ChatSlot, _P], Coroutine[Any, Any, None]]":
    """Give every ``_run_chat`` exit its tail, including the early ones.

    The main ``try``/``finally`` covers a turn only from session admission on,
    and a second cancel can cut that ``finally`` short. The guard hands the turn
    a fresh :class:`_TurnExit` as its third argument and, unless the turn ran
    :func:`_end_turn_tail` itself or opted out (the remote-slot refusal, whose
    queue drains on its peer, and the ``/prompts get`` branch, whose nested turn
    has its own guard), runs it once the turn ends. The record is an argument,
    so each call, the nested ``/prompts get`` turn included, has its own with
    nothing to reset. The guard runs no tail for a coroutine being closed
    (``GeneratorExit``), which cannot await; a turn closed inside its ``try``
    still runs the tail from its own ``finally``."""

    @functools.wraps(fn)
    async def guarded(
        state: DashboardState, slot: _ChatSlot, /, *args: _P.args, **kwargs: _P.kwargs
    ) -> None:
        turn_exit = _TurnExit()
        closing = False
        setup_failed = False
        try:
            await fn(state, slot, turn_exit, *args, **kwargs)
        except GeneratorExit:
            closing = True
            raise
        except Exception:
            if turn_exit.outcome is None and not turn_exit.handed_off:
                _note_failed_before_the_try(slot, turn_exit)
                # A local command's handler failing is that entry's own failure;
                # any other raise above the main try is setup the next queued
                # entry would run through and fail the same way.
                setup_failed = not turn_exit.local_command
            raise
        finally:
            if not closing and not turn_exit.handed_off:
                try:
                    # A cancel landing inside the tail is deferred until the
                    # hand-off ran, then re-raised: the tail marks the turn
                    # handed off on entry, so nothing would run it again.
                    await run_to_completion(
                        _end_turn_tail(state, slot, turn_exit, setup_failed=setup_failed)
                    )
                except Exception:
                    # The exception that ended the turn is the one to propagate.
                    logger.warning(
                        "turn tail failed for slot %s", getattr(slot, "key", "?"), exc_info=True
                    )

    return guarded


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
        from kiro_crew.metrics.provider import get_recorder  # noqa: F811

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


class _MemoryUnavailable(RuntimeError):
    """A memory refusal whose display code must not be inferred from prose."""


class _AppAgentNotLoaded(Exception):
    """An app-owned slot's kiro-cli agent is not materialized yet.

    Raised inside ``_run_chat`` after the self-heal warm has run and the app
    agent STILL did not resolve. It is deliberately fatal to the turn: the
    alternative — dispatching the default agent — is the exact silent
    substitution the app-dispatch fix exists to prevent (generic agent, none of
    the app's MCP tools, no error). Carries the user-facing card text as its
    message so the dedicated handler can surface it through the same
    ``slot.append("error", ...)`` path as every other terminal turn error.
    """


# Event kinds that prove the model READ this turn's prompt: it produced text or
# reasoning, or asked for a tool or a permission. The checklist blocks (a sync
# block or a cold-start recovery block) are settled on the first such event
# and on nothing else. A terminal frame alone -- the transport's own
# `complete(timeout)`, a compaction failure, an un-acked cancel -- is not
# evidence the prompt was read, so a turn that yields only that keeps the
# debt and the block is said again next turn.
_TODO_BLOCK_READ_EVENT_KINDS = frozenset(
    {EVENT_TEXT_CHUNK, EVENT_THINKING_CHUNK, EVENT_TOOL_CALL, EVENT_PERMISSION_REQUEST}
)


class _AbnormalTurnEvent:
    """Minimal ``event`` carrier for the abnormal-end usage row.

    ``_build_token_record`` reads a turn's billing off ``event.usage`` and its
    terminal reason off ``event.stop_reason`` -- the same two attributes an
    ``AcpEvent`` exposes at ``EVENT_COMPLETE``. A turn that ends through
    ``_persist_partial_reply`` never produced that event, so this wraps the
    recovered :class:`TurnUsage` and the turn's stop reason in the shape the
    record builder already reads, and nothing more.
    """

    __slots__ = ("usage", "stop_reason")

    def __init__(self, usage: TurnUsage, stop_reason: str) -> None:
        self.usage = usage
        self.stop_reason = stop_reason


async def _persist_abnormal_turn_usage(
    slot: "_ChatSlot",
    client: object,
    session_key: str,
    *,
    elapsed_ms: int,
    stop_reason: str,
    since: object = _NO_PRIOR_STATS,
    usage: TurnUsage | None = None,
) -> bool:
    """Write the usage row for a turn ending without ``EVENT_COMPLETE``.

    The dashboard's one ``persist_token_record_async`` call sits in the
    ``EVENT_COMPLETE`` branch, so every turn that ends through
    ``_persist_partial_reply`` instead -- the turn ceiling, the Stop button, a
    signed-out CLI mid-turn, and the other recovery paths that share that seam
    -- records no credits however much it billed. This is the accounting half of
    that gap: it recovers the turn's billing from the still live provider
    (``provider_last_turn_usage`` reads the credits kiro-cli accumulated on
    ``last_prompt_stats``) and writes the same row the complete path writes,
    keyed and attributed the same way. This seam persists the row ONLY -- it
    emits no histogram sample. The row's persist opts out of the metric
    (``emit_metric=False``) because the persist would key the sample on
    ``slot.key``, filing a linked-channel abnormal turn under ``dashboard``
    instead of its channel surface; abnormal-turn sampling is out of this
    change's scope, so the sample is simply not emitted rather than re-keyed.

    ``client`` is the OUTER provider wrapper (``providers/acp`` /
    ``acp/session_provider``): attribution reads -- ``provider_seam``,
    ``read_context_tokens``, the effective agent -- live there, so the inner
    ``AcpClient`` would label every row ``provider="acp"`` with a ``(0, 0)``
    context window. The caller passes the inner handle only to decide liveness,
    and runs this before ``_turn_release_handles`` drops ``slot._acp_client`` -- the one
    point the credits are still reachable.

    ``since`` is the per-turn stats object snapshotted BEFORE the turn's
    cancellable work: a dispatch that dies before the runtime installs fresh
    stats (a busy session, a dead runtime, an expired credential) leaves the
    PREVIOUS turn's object in place, still carrying credits already recorded, and
    ``provider_last_turn_usage``'s identity check against ``since`` reports
    nothing rather than billing that earlier turn a second time.

    Gated by ``usage_has_billing`` for the same reason the ``EVENT_COMPLETE``
    call is: a turn that billed nothing writes no row. Returns ``True`` once the
    row has been written, so the caller marks the turn recorded and this stays
    once-per-turn across the seam's callers. Never raises -- these are
    best-effort analytics, as the complete path's own persist is.

    ``usage`` is the turn's billing when the caller already read it (the Stop
    footer shares the one read, because ``provider_last_turn_usage`` consumes
    the accumulated total); ``None`` reads it here.
    """
    try:
        if usage is None:
            usage = provider_last_turn_usage(client, since=since)
        if not usage_has_billing(usage):
            return False
        _provider_name = capabilities_of(client).provider_seam
        _record_model = read_turn_model(client) or slot.model
        _ctx_used, _ctx_window = read_context_tokens(client)
        await persist_token_record_async(
            slot.key,
            _record_model,
            _AbnormalTurnEvent(usage, stop_reason),
            provider=_provider_name,
            surface=telemetry_channel_of(session_key),
            agent=read_effective_agent(client) or slot.agent or "",
            context_used=_ctx_used,
            context_window=_ctx_window,
            app=getattr(slot, "_app", "") or "",
            elapsed_ms=elapsed_ms,
            model_source=client,
            # Abnormal-turn histogram sampling is out of this change's scope
            # (the usage ROW is the deliverable); opt the row's persist out of
            # the metric so it does not file a sample under slot.key, which for a
            # linked-channel turn would mis-attribute to session_source=dashboard.
            emit_metric=False,
        )
        return True
    except asyncio.CancelledError:
        # This runs inside the turn's CancelledError teardown, so its own
        # analytics I/O can be cancelled in turn. Swallow it: the row and sample
        # are best-effort, and re-raising here would escape _run_chat's recovery
        # handler and strand the durable completion the finally block still owes
        # (test_raw_complete_survives_cancellation_during_token_persistence).
        logger.debug("Abnormal-turn usage write cancelled for slot %s", slot.key)
        return False
    except Exception:
        logger.debug("Failed to persist abnormal-turn usage for slot %s", slot.key, exc_info=True)
        return False


def _turn_full_reply_text(turn: "_ChatTurn") -> tuple[str, str]:
    """The turn's COMPLETE answer and its FINAL segment, as the transcript shows.

    Returns ``(full_body, final_segment)``. ``full_body`` is everything to
    mirror; ``final_segment`` is the newest reply row's text, the one an OPTIONS
    control must be built from (and only from).

    ``turn.assistant_text`` holds only the segment since the last tool call: a
    tool boundary flushes the text so far to a finalized assistant row and
    resets the accumulator (``_on_tool_call``), so a turn that answers and then
    calls one more tool leaves only the trailing fragment -- or nothing -- in
    it. Mirroring that fragment would publish a truncated reply, or none, on a
    turn whose answer precedes its last tool call, while the dashboard keeps the
    whole thing from the transcript. Reading the final segment from the newest
    row rather than from ``turn.assistant_text`` also keeps a final OPTIONS ask
    whose row was flushed before a text-less last tool call, which empties
    ``turn.assistant_text`` -- the dashboard shows those choices from the same
    row, so the mirror must too.

    The body and the final segment both come from THIS turn's own reply rows
    (filled by ``_note_reply_row`` at every segment flush including the
    end-of-turn one), in window order. Identity, not position: a workflow or
    sub-agent result is appended into the same window by another writer while
    the turn runs and must not be folded into this reply. The rows are already
    redacted and OPTIONS-reflow-repaired by ``_flush_segment``, which is what the
    transcript renders too.

    The ids come from ``turn._reply_mids_snapshot`` when it is set:
    ``_flush_file_changes`` clears the live ``slot._turn_reply_mids`` on exit and
    runs BEFORE this leg, so a turn that wrote a file would otherwise find the
    live list already empty. The snapshot is taken just before that flush. When
    no snapshot was taken (the flush did not run this turn) the live list is
    read. Falls back to ``turn.assistant_text`` for both values when neither
    names a row -- a slot double in a test with no id list, or a turn whose
    single segment has not been flushed into the window.
    """
    trailing = turn.assistant_text
    non_empty = _turn_reply_segments(turn)
    if not non_empty:
        return trailing, trailing
    return "\n\n".join(non_empty), non_empty[-1]


def _turn_reply_segments(turn: "_ChatTurn") -> list[str]:
    """THIS turn's non-empty reply rows, in window order; ``[]`` when none is named.

    The row ids and why they come from the snapshot: :func:`_turn_full_reply_text`.
    """
    slot = turn.slot
    snapshot = turn._reply_mids_snapshot
    mids = snapshot if isinstance(snapshot, list) else getattr(slot, "_turn_reply_mids", None)
    if not isinstance(mids, list) or not mids:
        return []
    wanted = set(mids)
    parts = [
        row.get("content", "")
        for row in slot.messages
        if row_mid(row) in wanted and row.get("role") == "assistant"
    ]
    return [p for p in parts if p]


@_hands_off_queue_on_exit
async def _run_chat(
    state: DashboardState,
    slot: _ChatSlot,
    # Supplied by the exit guard, never by a caller: callers see
    # ``_run_chat(state, slot, message, ...)``.
    turn_exit: _TurnExit,
    message: str,
    *,
    _prompt_depth: int = 0,
    # Attachment identifiers of the message this turn runs -- `meta.files` and
    # `meta.dirs`, the same lists the renderer resolves `[attached_file N]` markers
    # against. Passed by the two sites that OBSERVED them (the accepting handler and
    # the queue drain) rather than read back off the slot's last user row, which
    # would attribute a previous turn's files to a synthetic or recovery turn.
    _attachments: "tuple[str, ...] | list[str]" = (),
    # The same attachments keyed by their meta list (``files`` / ``dirs``),
    # preserving each path's TYPE where the flat list above cannot. The refusal
    # replay rebuilds the queue entry's meta from this, so a folder attachment
    # retries as a folder -- rebucketing the flat list under ``files`` would
    # retype it and resolve its ``[attached_dir N]`` marker against the wrong
    # list. Optional so the many existing callers and test doubles stay valid.
    _attachment_meta: "dict[str, list[str]] | None" = None,
    _synthetic_payload: bool = False,
    # The recovery replays this dispatch IS: the families whose recorded queue id
    # the drained entry carries (``RecoveryReplays.claim``). Identity travels as a
    # parameter because the message TEXT cannot carry it -- the drain redacts
    # credentials/exfil URLs after a swap records the raw text, so text equality
    # breaks for exactly the content most likely to draw a refusal, and a mutable
    # slot flag could be re-read after a correction landed. The consume seam
    # re-checks each claimed family before the provider sees the turn. Only the
    # queue drain sets this.
    _replay: frozenset[ReplayFamily] = frozenset(),
    # The drained entry carried the synthetic-recovery ``kind`` tag (a runner
    # requeue after a pre-output failure, including a re-queue of the USER'S OWN
    # words on a poisoned-conversation discard). Structural, from the entry --
    # ``_SYNTHETIC_RECOVERY_MSGS`` text membership cannot recognize a requeued
    # user message, and re-arming the refusal-retry allowance on one would let a
    # crashing fallback replay re-arm and repeat indefinitely.
    _synthetic_recovery_turn: bool = False,
    # The drained synthetic-recovery entry is a verbatim requeue of a sub-agent
    # completion (marked by ``_queue_recovery``), not runner-written text such as
    # a continuation or a retry prompt. Only the queue drain sets it.
    _replays_completion: bool = False,
    # The drained entry is a steer that may already have been delivered
    # (``STEER_POSSIBLY_DELIVERED_META``): the model input, and only it, is led
    # by ``STEER_POSSIBLY_DELIVERED_NOTE``.
    _steer_possibly_delivered: bool = False,
    _responsive_human: bool = False,
    _responsive_protected: bool = False,
    _directive_user_origin: bool = False,
    # This turn was drained from a queue entry a PREVIOUS process accepted
    # (`slot_queue_repository.RESTORED_QUEUE_KEY`). Its provenance therefore
    # rests on an ordinary writable file rather than on anything this process
    # observed, and a restored entry carries no actor at all -- which reads as
    # `user`. Only the queue drain sets it, and it is never persisted.
    _turn_provenance_restored: bool = False,
    # This turn is the delivered wake of a nudge/monitor loop bound to THIS slot
    # (set only by ``GatewayOrchestrator._fire_dashboard_nudge``). It is the
    # second producer the session-directive consumer admits as "the session's
    # own" for the crew/member self-arm rule: a member's loop firing on the
    # member's slot is the member keeping itself awake, and the arm/re-arm it
    # issues from inside that wake is its own act. Cron, app and sub-agent
    # injections never set it.
    _directive_self_wake: bool = False,
    # The id of the nudge/monitor loop whose delivered wake this turn is (set only
    # by ``_fire_dashboard_nudge`` alongside ``_directive_self_wake``). SCOPES the
    # structural-terminal slot verdict to the exact loop that produced
    # it: a slot outlives any single loop (stop one, arm another on the same
    # slot), so a slot-wide flag would let a stopped malformed loop's verdict
    # deactivate a DIFFERENT loop armed later on that slot. Also handed to the
    # directive consumer, whose arming gate reads the row back to tell a live
    # wake from one that outlived its loop's Stop. Empty for every
    # non-self-wake turn.
    _directive_loop_id: str = "",
    # The loop's CONFIG GENERATION at fire time (``loop.config_generation``),
    # recorded with the verdict so the structural stop is applied under an ATOMIC
    # (id, generation) fence: a stale completion of an OLD instruction cannot
    # deactivate a loop whose config advanced since (the A->B->A race that a
    # message-value key could not tell apart). Captured by the fire path.
    _directive_loop_gen: int = 0,
    _directive_channel_origin: bool = False,
    # The message is turn CONTENT and its first word is never read as a dashboard
    # command: set by the drain for an entry the channel hand-off stamped
    # (``COMMANDS_OFF_META_KEY``). A channel's own queue already replays every
    # entry this way; without the switch, a channel's queued "/clear" would be the
    # one queued "/clear" in the system that ran instead of being said.
    _commands_off: bool = False,
    # Who caused this turn, from the dispatch that knows -- a consumed queue
    # entry's enqueue-time ``kind`` tag, or an injector calling this runner
    # directly. Recorded in the session's log, so it must not be derivable from
    # the message TEXT: a user can type the cron or sub-agent banner, and the
    # crew log's whole premise is that a reader takes it as fact. Empty means no
    # dispatch claimed it, which the crew log records as ``user``.
    _turn_actor: str = "",
    regenerate_hint: str = "",
    _on_consumed: "Callable[[bool], None] | None" = None,
    _on_irreversibly_consumed: "Callable[[], Awaitable[None] | None] | None" = None,
    monitor_completion: MonitorCompletionHook | None = None,
    _current_message: dict | None = None,
) -> None:
    """Stream LLM response into *slot*.  Survives browser disconnect."""
    turn = _ChatTurn(
        state=state,
        slot=slot,
        turn_exit=turn_exit,
        message=message,
        _prompt_depth=_prompt_depth,
        _attachments=_attachments,
        _attachment_meta=_attachment_meta,
        _synthetic_payload=_synthetic_payload,
        _replay=_replay,
        _synthetic_recovery_turn=_synthetic_recovery_turn,
        _replays_completion=_replays_completion,
        _steer_possibly_delivered=_steer_possibly_delivered,
        _responsive_human=_responsive_human,
        _responsive_protected=_responsive_protected,
        _directive_user_origin=_directive_user_origin,
        _turn_provenance_restored=_turn_provenance_restored,
        _directive_self_wake=_directive_self_wake,
        _directive_loop_id=_directive_loop_id,
        _directive_loop_gen=_directive_loop_gen,
        _directive_channel_origin=_directive_channel_origin,
        _commands_off=_commands_off,
        _turn_actor=_turn_actor,
        regenerate_hint=regenerate_hint,
        _on_consumed=_on_consumed,
        _on_irreversibly_consumed=_on_irreversibly_consumed,
        monitor_completion=monitor_completion,
        _current_message=_current_message,
    )
    _flow = await _turn_open(turn)
    if _flow is _Flow.END:
        return

    _activity_after_terminal = turn._activity_after_terminal
    _compaction_notice_chunks = turn._compaction_notice_chunks
    _person_steered_turn = turn._person_steered_turn
    _tcid_collapsed = turn._tcid_collapsed
    _tcid_first_source = turn._tcid_first_source
    _thinkred = turn._thinkred
    _ttft_t0 = turn._ttft_t0
    _ttft_visible = turn._ttft_visible
    _wsred = turn._wsred
    in_tool_group = turn.in_tool_group
    last_heartbeat = turn.last_heartbeat
    session_key = turn.session_key
    _flow = await _turn_slash_gate(turn)
    if _flow is _Flow.END:
        return
    is_slash = turn.is_slash
    # Where the most recent compaction started inside `assistant_text`. A
    # synthesized terminal arrives only at turn end, so the accumulator still
    # holds any unfinished pre-compaction fragment, and the continuation gate
    # judges only the text after this offset. A segment cut empties the
    # accumulator and the next segment is wholly post-compaction, so the first
    # chunk appended to an empty accumulator zeroes the offset: no cut site has
    # to clear it, and a cut with no text after it leaves nothing to judge.
    _compaction_text_offset = 0
    turn._compaction_text_offset = _compaction_text_offset
    try:
        _flow = await _turn_admit(turn)
        if _flow is _Flow.END:
            return
        _crew_log_sid = turn._crew_log_sid
        _crew_log_turn_no = turn._crew_log_turn_no
        _replay_pending = turn._replay_pending
        client = turn.client
        is_new = turn.is_new
        resumed = turn.resumed
        await _turn_dispatch_prep(turn)
        await _turn_build_prompt(turn)
        message = turn.message

        # No await from here to the stream loop: begin_turn (in _turn_dispatch_gates) and the
        # stream's turn registration stay one yield-free span.
        _flow = await _turn_dispatch_gates(turn)
        if _flow is _Flow.END:
            return
        _crew_log_step = turn._crew_log_step
        _crew_log_step_t0 = turn._crew_log_step_t0
        _todo_recovery_carried = turn._todo_recovery_carried
        _todo_sync_rendered = turn._todo_sync_rendered
        full_message = turn.full_message
        event_stream = client.stream_command(message) if is_slash else client.stream(full_message)
        async for event in event_stream:
            if (_todo_sync_rendered or _todo_recovery_carried) and (
                event.kind in _TODO_BLOCK_READ_EVENT_KINDS
            ):
                # The model has started answering THIS prompt, so the checklist
                # block in it was read. A terminal-only stream (the transport's
                # synthetic timeout, a compaction failure) never reaches here and
                # leaves both debts owed for the next turn.
                if _todo_sync_rendered:
                    # Exactly the edits the block carried are now said; one
                    # ticked since assembly is said next turn.
                    slot.mark_todo_edits_stated(_todo_sync_rendered)
                    _todo_sync_rendered = ()
                    turn._todo_sync_rendered = _todo_sync_rendered
                if _todo_recovery_carried:
                    # The cold-start recovery block THIS turn carried is
                    # delivered: clear the debt so later turns stop re-sending
                    # it. A turn that did not carry it (a slash command, a warm
                    # turn) leaves the debt for the next non-slash turn to pay.
                    slot.clear_todo_recovery_pending()
                    _todo_recovery_carried = False
                    turn._todo_recovery_carried = _todo_recovery_carried
            # Async-generator creation is not prompt acceptance. The first
            # provider event is the earliest evidence that the replay-bearing
            # prompt entered the turn; pre-output errors and empty streams never
            # reach this branch. Acceptance is runner-local: the shared lease
            # stays armed until final settlement so a concurrent shutdown cannot
            # publish the fresh SID before this turn proves durable.
            if _replay_pending and not is_slash:
                _replay_pending = False
                turn._replay_pending = _replay_pending
                _replay_accepted_this_turn = True
                turn._replay_accepted_this_turn = _replay_accepted_this_turn
            # Heartbeat every 5s during long operations
            if time.time() - last_heartbeat > 5:
                state.broadcast_ws("heartbeat", {"slot": slot.key, "ts": time.time()})
                last_heartbeat = time.time()
                turn.last_heartbeat = last_heartbeat

            # First visible model output for this user prompt — emit TTFT once.
            if _ttft_t0 is not None and event.kind in (EVENT_TEXT_CHUNK, EVENT_THINKING_CHUNK):
                _emit_ttft_metric(_ttft_t0, session_key, is_new=is_new, resumed=resumed)
                _ttft_t0 = None
                turn._ttft_t0 = _ttft_t0

            # Security: tool_call_id originates from LLM — redact before any use
            if hasattr(event, "tool_call_id") and event.tool_call_id:
                _raw_tcid = event.tool_call_id
                _tcid, _ = redact_exfiltration_urls(_raw_tcid)
                _tcid, _ = redact_credentials(_tcid)
                event.tool_call_id = _tcid
                # The raw id is gone after that assignment, and every later reader
                # sees only the redacted value, so whether that value still names
                # ONE call has to be recorded HERE. EVERY id is recorded, not only
                # one redaction changed: a raw id that already IS the redaction tag
                # is left alone, yet shares its value with every credential-shaped
                # id that redacts to that tag, so skipping it would leave the pair
                # tracked as one source and reading unambiguous.
                _tcid_digest = hashlib.sha256(_raw_tcid.encode("utf-8", "replace")).hexdigest()[:16]
                _tcid_key = _tcid_identity_key(_tcid)
                if not _tcid_key or _tcid_key in _tcid_collapsed:
                    pass
                elif _tcid_key not in _tcid_first_source:
                    # Bounded: past the cap the key is simply not recorded, and an
                    # unrecorded key reads as naming nothing.
                    if len(_tcid_first_source) < _MAX_TCID_SOURCES:
                        _tcid_first_source[_tcid_key] = _tcid_digest
                elif _tcid_first_source[_tcid_key] != _tcid_digest:
                    _tcid_collapsed.add(_tcid_key)

            # Leaving the thinking phase → flush any withheld thinking tail so a
            # credential split across thinking chunks can't cross the wire raw.
            if event.kind != EVENT_THINKING_CHUNK:
                _flush_thinking_stream(turn)

            # The model produced new output, so the tool group the user denied is
            # over: end the batch-rejection suppression instead of letting it run
            # to the end of the turn and swallow a revised call the user never
            # saw. See _BATCH_REJECT_CLEARED_BY.
            if event.kind in _BATCH_REJECT_CLEARED_BY and getattr(slot, "_batch_rejected", False):
                slot._batch_rejected = False
                slot._batch_rejected_cause = ""
                logger.info(
                    "batch rejection cleared on %s — later tool calls in this "
                    "turn are approved independently",
                    event.kind,
                )

            if event.kind == EVENT_TEXT_CHUNK:
                # If we just exited a tool group, finalize the streaming
                # message so post-tool text starts a fresh message. The wire
                # redactor is NOT flushed: what it still holds from before the
                # tools is joined to this text (see _cut_segment).
                if in_tool_group:
                    _open_construct = open_construct_at_end(turn.assistant_text)
                    if turn.assistant_text and _open_construct:
                        _deferred_cut_text = turn.assistant_text
                        turn._deferred_cut_text = _deferred_cut_text
                        logger.debug(
                            "segment cut deferred on %s — %s still open",
                            slot.key,
                            _open_construct,
                        )
                    elif turn.assistant_text:
                        _cut_segment(turn)
                        _turn_flushed_visible_text = True
                        turn._turn_flushed_visible_text = _turn_flushed_visible_text
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
                                    {
                                        "slot": slot.key,
                                        "tool_call_id": tcid,
                                        "output": "",
                                        "final": True,
                                    },
                                )
                        # A `card` row is a change card / guide offer the
                        # tool group's own call put into the conversation, so
                        # the scan reads past it like the tool rows around it.
                        elif m.get("role") not in ("tool", "permission", "chunk", "card"):
                            break
                    # The same inference for the log. A tool that produced no
                    # output sent no result frame, so its call is still open here;
                    # without this it stays open for the life of the file.
                    #
                    # `completed` is passed EXPLICITLY, and only here: the model
                    # went on to produce this text, so the tools it was waiting on
                    # did finish. The turn-end closers cannot say that and take the
                    # emitter's `unknown` default instead.
                    crew_log_emit.close_open_tool_calls(
                        _crew_log_sid, _crew_log_turn_no, status="completed"
                    )
                    # Text after a tool group means the model was called AGAIN:
                    # close the call that issued those tools and open the next.
                    # This transition is the only per-model-call boundary the
                    # stream exposes -- its terminal event is the turn's, not a
                    # call's -- so the step is derived here rather than reported.
                    crew_log_emit.on_step_completed(
                        _crew_log_sid,
                        _crew_log_turn_no,
                        _crew_log_step,
                        ms=int((time.monotonic() - _crew_log_step_t0) * 1000),
                    )
                    _crew_log_step = crew_log_emit.on_step_started(_crew_log_sid, _crew_log_turn_no)
                    turn._crew_log_step = _crew_log_step
                    _crew_log_step_t0 = time.monotonic()
                    turn._crew_log_step_t0 = _crew_log_step_t0
                in_tool_group = False
                turn.in_tool_group = in_tool_group
                if (
                    turn._terminal_directive_applied
                    and not _person_steered_turn
                    and getattr(slot, "_pending_steers", None)
                ):
                    # A person steered into the quiet-ended turn and the
                    # backend has not echoed consumption yet (codex reports
                    # it only at the terminal): this text may already be the
                    # answer to them, so keep every chunk from here on.
                    _person_steered_turn = True
                    turn._person_steered_turn = _person_steered_turn
                if turn._terminal_directive_applied and not _person_steered_turn:
                    # Turn-end contract: the applied terminal directive is the
                    # turn's LAST act, so text after it is never the turn's
                    # output. It is not accumulated, streamed or persisted: a
                    # quiet end renders no assistant bubble, whatever the model
                    # goes on to say. The only record is the count reported at
                    # turn end. The common source is not the model's own words
                    # at all: a quiet end leaves the backend's history on a
                    # tool result, the backend's alternation repair fabricates a
                    # self-describing assistant placeholder to fill that gap on
                    # the next request, and the model reads it back as its own
                    # prior reply and repeats it after each later quiet end.
                    # Dropping on the structured signal, not the placeholder's
                    # wording, covers that and every other late text alike.
                    _activity_after_terminal += 1
                    turn._activity_after_terminal = _activity_after_terminal
                    continue
                # The streamed delta is NOT written to the ledger. Redacting each
                # delta on its own cannot see a credential split across two of
                # them, and token-by-token streaming makes that split the common
                # case -- so the pieces would land in an append-only file that a
                # reader can concatenate. The redacted whole body on
                # ``message/sent`` carries the same content, redacted once over
                # text where the credential is intact and therefore matchable.
                #
                # Pass 4 (`?token=` values) is DEFERRED to that whole-segment run
                # while a companion prefix set is installed: a delta cut inside an
                # approval link shows the pass a `token=` with no prefix before it,
                # so the value is redacted here and the exemption `_redact_segment`
                # would have granted can never apply. Every other pass still runs
                # per delta; the live wire goes through `_wsred`, which never
                # exempts; with the empty default prefix set nothing changes.
                safe_chunk, _removed = _redact_delta(event.text)
                if event.message_id:
                    slot.segment_message_id = event.message_id
                _accumulate_segment_raw(slot, event.text, safe_chunk, _removed)
                if not turn.assistant_text:
                    _compaction_text_offset = 0
                    turn._compaction_text_offset = _compaction_text_offset
                turn.assistant_text += safe_chunk
                if event.control_notice:
                    # A backend control notice that arrived as assistant text
                    # (the claude adapter's "Compacting..."). It accumulates,
                    # streams and persists exactly like any other chunk — the
                    # text path is deliberately untouched, because every attempt
                    # to special-case it there was swallowed by a later layer.
                    # Record it instead, so the post-compaction gate can tell
                    # the turn's own ANSWER from a control frame: a notice
                    # counted as an answer would shadow the continuation branch
                    # and leave the request unanswered — the exact hang this PR
                    # exists to fix.
                    _compaction_notice_chunks.append(safe_chunk)
                # Set BEFORE the `_turn_emitted` flip: the consumption report
                # below must stay adjacent to that flip (pinned by
                # test_subagent_delivery_ttl_anchor), so a diagnostic flag goes
                # above it rather than between the two.
                _saw_text_chunk = True
                turn._saw_text_chunk = _saw_text_chunk
                _turn_emitted = True  # tokens delivered — transient retry now unsafe
                turn._turn_emitted = _turn_emitted
                await _report_consumed(turn, irreversible=True)
                # Stream to the wire through the rolling buffer so a credential
                # split across token boundaries can't cross a broadcast boundary
                # unredacted. Only the confirmed-safe prefix is emitted;
                # the trailing (possibly-partial-credential) run is withheld until
                # the next chunk or the segment flush. assistant_text above still
                # accumulates the full text for the authoritative final redaction.
                # The first output after a tool call or approval prompt also
                # settles the run held there (see _cut_segment).
                wire = _wsred.feed(event.text)
                if wire:
                    wire = _settle_held(turn, wire, continued=True)
                if wire:
                    _ttft_visible.mark(wire)
                    turn.chunk_seq += 1
                    slot._chunk_seq = turn.chunk_seq
                    # Same seq and generation on the window row as on the wire
                    # frame (see _flush_text_stream) so a mid-stream snapshot
                    # carries them.
                    row = slot.append("chunk", wire, "chunk")
                    row["seq"] = turn.chunk_seq
                    row["gen"] = chunk_generation()
                    # Push chunk to WS clients (HTTP SSE reader drains from slot._pending)
                    state.broadcast_ws(
                        "chat_chunk",
                        {
                            "slot": slot.key,
                            "content": wire,
                            "seq": turn.chunk_seq,
                            "gen": chunk_generation(),
                        },
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
                    _ttft_visible.mark(wire)
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
                turn._turn_thought = _turn_thought
            elif event.kind == EVENT_TOOL_CALL:
                await _on_tool_call(turn, event)
                _activity_after_terminal = turn._activity_after_terminal
                in_tool_group = turn.in_tool_group
            elif event.kind == EVENT_TOOL_CALL_UPDATE:
                await _on_tool_call_update(turn, event)
            elif event.kind == EVENT_TOOL_RESULT:
                await _on_tool_result(turn, event)
            elif event.kind == EVENT_PERMISSION_REQUEST:
                _flow = await _on_permission_request(turn, event)
                if _flow is _Flow.CONTINUE:
                    continue
                _flow = await _on_permission_prompt(turn, event)
                if _flow is _Flow.BREAK:
                    break
                if _flow is _Flow.CONTINUE:
                    continue
            elif event.kind == EVENT_STEER_CONSUMED:
                await _on_steer_consumed(turn, event)
                _person_steered_turn = turn._person_steered_turn
            elif event.kind == EVENT_COMPACTION_STATUS:
                await _on_compaction_status(turn, event)
            elif event.kind == EVENT_CLEAR_STATUS:
                await _on_clear_status(turn, event)
                _replay_pending = turn._replay_pending
            elif event.kind == EVENT_AGENT_SWITCHED:
                _flow = await _on_agent_switched(turn, event)
                if _flow is _Flow.BREAK:
                    break
            elif event.kind == EVENT_MCP_OAUTH_REQUEST:
                await _on_mcp_oauth_request(turn, event)
            elif event.kind == EVENT_MCP_SERVER_INITIALIZED:
                await _on_mcp_server_initialized(turn, event)
            elif event.kind == EVENT_MCP_SERVER_INIT_FAILURE:
                await _on_mcp_server_init_failure(turn, event)
            elif event.kind == EVENT_TODO_UPDATE:
                await _on_todo_update(turn, event)
            elif event.kind == EVENT_SUBAGENT_LIST:
                await _on_subagent_list(turn, event)
            elif event.kind == EVENT_SUBAGENT_ACTIVITY:
                await _on_subagent_activity(turn, event)
            elif event.kind == EVENT_COMPLETE:
                _flow = await _on_complete(turn, event)
                if _flow is _Flow.BREAK:
                    break

        _flow = await _turn_stop_recovery(turn)
        if _flow is _Flow.END:
            return

        await _turn_compact_command(turn)

        await _turn_settle_answer(turn)

        await _turn_ending_chain(turn)
        await _turn_persist_and_settle(turn)
        await _turn_context_usage(turn)
        await _turn_stop_hook(turn)
    except asyncio.CancelledError:
        await _on_cancelled(turn)
    except AcpAuthRequired as exc:
        await _on_auth_required(turn, exc)
    except AcpProcessDied as exc:
        await _on_process_died(turn, exc)
    except PromptBusyExhaustedError:
        await _on_prompt_busy(turn)
    except AcpError as exc:
        # The exception CLASS is logged alongside the message because the
        # session-health scanner keys its prompt_stuck signal off this line, and
        # the message text is not a reliable carrier: _format_acp_error
        # rewrites the backend's "prompt already in progress" into user-facing
        # prose. The class name is the structural classification rendered into
        # text, so a scanner never has to pattern-match wording that a copy
        # edit (or translation) can move. See session_health._PATTERNS.
        _escalation = turn._escalation
        message = turn.message
        logger.warning("ACP error in slot %s: [%s] %s", slot.key, type(exc).__name__, exc)
        _msg = str(exc)
        turn._msg = _msg
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
            await _on_acp_retryable(turn, exc)
        elif acp_error_is_session_not_found(exc):
            await _on_acp_session_not_found(turn, exc)
        elif isinstance(exc, AcpConversationBindingMismatch) and await _binding_rebuild_allowed(
            turn
        ):
            await _on_acp_binding_mismatch(turn, exc)
        elif (
            getattr(exc, "image_format_unsupported", False)
            and not _attachments
            and not _current_turn_carries_image_ref(message)
            and not _escalation.one_shot_spent
            and _prompt_depth == 0
            and (
                (_image_replay_owed := not turn._turn_emitted and _replay_owes_delivery(turn))
                or (
                    not _should_suppress_requeue(slot)
                    # Resolved Stop: the `_image_stop_gen` snapshot in
                    # _on_acp_image_unsupported is taken after the Stop moved the
                    # counter, so only this read sees it (see _on_process_died).
                    and not _stop_pressed(turn)
                    and not _has_user_queued_followup(slot)
                    and not getattr(slot, "_pending_steers", None)
                )
            )
        ):
            await _on_acp_image_unsupported(turn, exc, _image_replay_owed)
        elif (
            not turn._turn_emitted
            and acp_error_is_transient(exc)
            and (
                slot._transient_5xx_retries < TRANSIENT_RETRIES
                or connection_recovery_open(exc, slot._transient_ladder_started)
            )
        ):
            await _on_acp_transient_retry(turn, exc)
        elif (
            not turn._turn_emitted
            and acp_error_is_transient(exc)
            and slot._transient_5xx_retries >= TRANSIENT_RETRIES
            and _prompt_depth == 0
            and not _should_suppress_requeue(slot)
            and (_fb_candidate := await _fallback_swap_for_turn(slot, turn.client)) is not None
        ):
            await _on_acp_transient_exhausted(turn, exc, _fb_candidate)
        elif turn._turn_emitted and acp_error_is_transient(exc) and not slot._posttoken_retry_used:
            await _on_acp_posttoken_transient(turn, exc)
        elif (
            not turn._turn_emitted
            and not slot._model_access_fallback_used
            and _prompt_depth == 0
            # An owed completion is replayed whatever a Stop says; see _on_acp_model_access.
            and (_replay_owes_delivery(turn) or not _should_suppress_requeue(slot))
            and isinstance((_rejected_id := getattr(exc, "rejected_model", None)), str)
            and _rejected_id.strip()
            and len(_rejected_id) <= _MAX_MODEL_ID_LEN
            and model_is_unusable(_rejected_id, getattr(exc, "advertised", None))
            and (
                _access_fb_candidate := first_advertised_fallback(
                    getattr(exc, "advertised", None), _rejected_id
                )
            )
            is not None
            and len(_access_fb_candidate) <= _MAX_MODEL_ID_LEN
        ):
            await _on_acp_model_access(turn, exc, _rejected_id, _access_fb_candidate)
        else:
            await _on_acp_error_card(turn, exc)
    except _AppAgentNotLoaded as exc:
        await _on_app_agent_not_loaded(turn, exc)
    except SessionClosingError:
        await _on_session_closing(turn)
    except Exception as exc:
        await _on_error(turn, exc)
    finally:
        # An interrupted or unexecuted proposal must not survive into a turn
        # whose backend may reuse its call id. Executed walker work is separate.
        _local_turn_marker_generation = turn._local_turn_marker_generation
        slot._git_repo_touch_pending.clear()
        slot._git_repo_touch_terminal.clear()
        slot._git_repo_touch_overflow = False
        await _turn_record_outcome(turn)
        await _turn_close_crew_log(turn)
        await _turn_release_handles(turn)
        await _turn_settle_debts(turn)
        await _turn_release_session(turn)
        # Retire the in-flight marker once every transcript mutation is complete
        # and the session permit is released in _turn_release_session (an await here
        # cannot skip that release), and only while the process is going to live on: during
        # a shutdown the cancellation that ended this turn came from the process
        # dying, which is exactly the interruption the marker exists to record --
        # the graceful save writes it and the next start converts it. A turn
        # that landed inside the shutdown grace keeps its clear. Before the queue
        # drain below, so the successor's own marker is never overtaken by this
        # omission. Guarded so a failing save cannot skip the tail.
        if not (_gateway_shutdown_requested(state) and not turn._turn_landed):
            try:
                await _clear_local_turn_marker(state, slot, _local_turn_marker_generation)
            except Exception:
                logger.debug("_clear_local_turn_marker failed", exc_info=True)
        # The per-exit duties and the queue hand-off, in their one order. Run to
        # completion: a cancel landing inside the tail (a turn deadline during
        # the deferred-reset consume) is deferred until the hand-off ran, then
        # re-raised, so the queue is never left behind a finished task.
        await run_to_completion(_end_turn_tail(state, slot, turn_exit))


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.dashboard.chat_runner.<name>`` reaches it wherever it now lives; see
# ``kiro_crew.dashboard.chat_turn``. Run once, after this body has bound every name.
_chat_turn.compose(globals(), _chat_turn.owner_modules())
