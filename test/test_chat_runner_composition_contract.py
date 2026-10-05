"""The dashboard chat runner stays one namespace while its owners live in ``chat_turn``.

``kiro_crew.dashboard.chat_runner`` keeps its import path, its patch surface, the
turn entry ``_run_chat`` and every construct a path-keyed repository guard reads in
that file; the modules of ``kiro_crew.dashboard.chat_turn`` hold these
responsibilities, one each, and ``chat_turn.compose`` runs every function they
define on the runner's globals. These tests pin what that composition promises:

* every name the runner bound before the split still resolves on it, and each
  moved name keeps the kind and signature it had in the one-module file and is ONE
  object whichever module a caller reads it from;
* each owner function runs on the runner's globals, every global it reads resolves
  there, and no owner captures a name a test rebinds on the runner, so a patch of
  ``kiro_crew.dashboard.chat_runner.<name>`` reaches it (the rebound names are
  read off the test corpus, by a scan proven able to see each spelling);
* nothing but the runner imports an owner, no owner imports another at runtime,
  and importing the runner loads every owner;
* the constructs guards count in ``dashboard/chat_runner.py`` stay there, and the
  owners with coroutines are inside the ``config_dir`` guard.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import dis
import importlib
import importlib.util
import inspect
import pkgutil
import re
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from typing import Any

import pytest
from source_corpus import repo_files_named, repo_root

import kiro_crew.dashboard.chat_runner as cr
from kiro_crew.dashboard import chat_turn
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = cr.__name__
_FACADE_PATH = Path(cr.__file__).resolve()
_SRC = _FACADE_PATH.parents[2]
_OWNER_PACKAGE = chat_turn.__name__
_OWNER_DIR = Path(chat_turn.__file__).resolve().parent
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"})

#: The owners the runner composes. Adding or removing one changes the composition,
#: so the set is spelled out rather than globbed.
_OWNER_MODULES = frozenset(
    {
        "acp_recovery",
        "directives",
        "file_changes",
        "mcp_session",
        "model_fallback",
        "prompt_assembly",
        "recipient",
        "recovery",
        "steer_queue",
        "tool_approval",
        "turn_context",
        "turn_marker",
        "turn_stats",
    }
)

#: Every module-level name ``chat_runner`` bound at the base the split was cut from:
#: what it defined and what it imported, private names included, because tests and
#: production read private names off it too.
_BASE_NAMES = frozenset("""
        ACP_BACKENDS_COMPACT AUTH_REQUIRED_KIND AcpAuthRequired AcpError AcpProcessDied
        AcpPromptBusy Any Awaitable CHAT_TYPE_DIRECT COMPACT_REPLAY_PENDING_NOTICE
        CRON_NOTIFICATION_KIND CRON_NOTIFY_PREFIX CRON_NOTIFY_RE Callable CapabilityError
        CapabilityStartupError CrewLogPrevious DENY_CAUSE_APPROVAL_NO_BUDGET
        DENY_CAUSE_APPROVAL_TIMEOUT DENY_CAUSE_APPROVAL_UNDELIVERABLE DENY_CAUSE_BATCH_CASCADE
        DENY_CAUSE_HOOK_ERROR DENY_CAUSE_INVALID_NAME DENY_CAUSE_POLICY
        DENY_CLASS_AWS_CREDENTIAL DENY_CLASS_SSO_CREDENTIAL DM_SLOT_MODE DashboardState
        EMPTY_RUNG_CONTINUE EMPTY_RUNG_GIVE_UP EMPTY_RUNG_REPLAY EMPTY_TURN_NOTICE
        EMPTY_TURN_NOTICE_AFTER_RECOVERY EMPTY_TURN_NOTICE_AFTER_WORK EVENT_AGENT_SWITCHED
        EVENT_CLEAR_STATUS EVENT_COMPACTION_STATUS EVENT_COMPLETE EVENT_MCP_OAUTH_REQUEST
        EVENT_MCP_SERVER_INITIALIZED EVENT_MCP_SERVER_INIT_FAILURE EVENT_PERMISSION_REQUEST
        EVENT_STEER_CONSUMED EVENT_SUBAGENT_ACTIVITY EVENT_SUBAGENT_LIST EVENT_TEXT_CHUNK
        EVENT_THINKING_CHUNK EVENT_TODO_UPDATE EVENT_TOOL_CALL EVENT_TOOL_CALL_UPDATE
        EVENT_TOOL_RESULT EmptyTurnActivity FALSE_TOOL_BLOCKER_REPLAY_KIND FallbackState
        FileTooLargeError HOOK_CONTINUATION_RECOVERY_PREFIX HOOK_EVENT_AGENT_SPAWN
        HOOK_EVENT_POST_TOOL_USE HOOK_EVENT_PRE_TOOL_USE HOOK_EVENT_STOP
        HOOK_EVENT_USER_PROMPT_SUBMIT HOOK_HALTED_RECOVERY_PREFIX HUMAN_TURN_META_KEY
        InfraError KiroCrewConfig L1_TOOL_CALL LLMEvent MAX_BLOCKED_LINKS_PER_MESSAGE
        MAX_PROMPT_BYTES MCP_APP_MESSAGE_KIND MODEL_UNENTITLED_KIND MONITOR_WAKE_PREFIX
        MonitorCompletionHook NATIVE_SUBAGENT_DONE_RESULT_CAP NATIVE_SUBAGENT_DONE_TRUNC_MARKER
        NATIVE_SUBAGENT_OUTPUT_HARD NATIVE_SUBAGENT_OUTPUT_TAIL NATIVE_SUBAGENT_TERMINAL_KEEP
        NATIVE_SUBAGENT_TERMINAL_TTL_SECS NamedTuple OUTCOME_REJECTED_TRANSPORT_FLOOR
        PENDING_CHANNEL_ATTR PHASE_PER_TURN PHASE_SESSION_START POISONED_SESSION_CYCLES Path
        PostedOptions PromptBusyExhaustedError QUESTION_CARD_SHOWN_PREFIX QUICK_PROMPTS
        REFUSAL_INBAND_RECOVERY_PREFIX REFUSAL_RECOVERY_PREFIX RESTORED_QUEUE_KEY
        RecoveryPayload Refusal RefusalInfo
        ResetCause ResolvedBindings
        SESSION_NOT_FOUND_CANCELLED_TEXT
        SESSION_NOT_FOUND_GIVE_UP_TEXT SESSION_NOT_FOUND_RETRY_TEXT
        SESSION_RECOVERY_MAX_ATTEMPTS SESSION_START_FAILED_KIND SLACK_NAMESPACE
        STALE_RECOVERY_PREFIX
        STEER_NOTICE_BOUND_SECS STEER_POSSIBLY_DELIVERED_META
        STEER_POSSIBLY_DELIVERED_NOTE STEER_STATE_CONSUMED
        STEER_STATE_REQUEUED STOP_CLASS_FAILED STOP_REASON_CANCELLED
        STOP_REASON_COMPACTION_FAILED STOP_REASON_END_TURN STOP_REASON_REFUSAL
        STOP_REASON_STALE_RECOVER STOP_REASON_TOOL_STALL STOP_RECOVERY_MAX_RETRIES
        SUBAGENT_COMPLETION_KIND SUBAGENT_COMPLETION_PREFIXES SUBAGENT_DELIVERY_KINDS
        SUBAGENT_SYNTHESIS_PREFIX
        SUBAGENT_SYNTHESIS_PROMPT SYNTHESIS_CLEAR SYNTHESIS_HELD SYNTHESIS_UNKNOWN
        SYNTHETIC_RECOVERY_KIND SecurityEvent SessionBusyError
        SessionClosingError SessionEndingError
        SessionMcpReport SpeculativeResumeRefused StartPriority
        StreamRedactor StructuredStatus TERMINAL_TOOL_STATUSES TOOL_ALLOW TOOL_AUTO_APPROVE
        TOOL_DENY TOOL_STALL_RECOVERY_PREFIX TRANSIENT_GIVE_UP_TEXT TRANSIENT_NOTICE_GIVE_UP
        TRANSIENT_NOTICE_META_KEY TRANSIENT_NOTICE_RESUMING TRANSIENT_NOTICE_RETRYING
        TRANSIENT_RESUMING_TEXT TRANSIENT_RETRIES TRANSIENT_RETRYING_TEXT TRANSIENT_RETRY_KIND
        TURN_ACTOR_META_KEY TURN_FALLBACK_ATTR TURN_OPENING_INJECT_KINDS TURN_TIMEOUT_CAUSE
        ToolHookResult UNCLAIMED_DIRECTIVE_NOTICE USAGE_LIMIT_KIND USER_LABEL
        UnknownMemoryStore ValidationError WAIT_REASON_INPUT _ACTIVITY_NO_REPLY_CONTINUE_MSG
        _AppAgentNotLoaded _BATCH_REJECT_CLEARED_BY _BLOCKED_SLASH_COMMANDS _BUSY_RECOVER_MSG
        _COMPACTION_CONTINUE_MSG _COMPACTION_FAILED_RETRIES _COMPACT_FAIL_REASON_MAX_CHARS
        _CONN_RECOVER_MSG _CONTEXT_FRAME_CONTRACT _CREDENTIAL_HINT_CLASSES _CREDENTIAL_TAG_RE
        _ChatSlot _DIRECTIVE_NOT_APPLIED_FALLBACK _DIRECTIVE_NOT_APPLIED_OUTCOMES
        _DIRECTIVE_SHAPED_RE _EAGER_SPAWN_BACKOFF_BASE_SECS _EAGER_SPAWN_BACKOFF_MAX_SECS
        _EAGER_SPAWN_DEBOUNCE_SECS _EAGER_SPAWN_ERROR_DETAIL_MAX_CHARS _EAGER_SPAWN_FAILURE_CAP
        _EAGER_SPAWN_MAX_CONCURRENT _EMPTY_AUTO_CONTINUE_MSG
        _FirstVisibleClock _JEV_ROUTE_AUTO_MODELS
        _JEV_ROUTE_BASELINE_ATTR _KIRO_ONLY_BLOCKED_SLASH_COMMANDS _LOCAL_TURN_OPENER_ROLES
        _LOCAL_TURN_PROMPT_META_KEYS _MANUAL_RESUME_MSG _MAX_MODEL_ID_LEN
        _MAX_NATIVE_CARD_ERROR _MAX_RECONSTRUCT_BYTES _MAX_SLOT_MESSAGES _MAX_SNAPSHOT
        _MAX_SNAPSHOT_PATH_CHARS _MAX_TCID_LEN _MAX_TCID_SOURCES _MAX_TOOL_PURPOSE
        _MAX_TURN_SNAPSHOT_CHARS _MAX_TURN_SNAPSHOT_ENTRIES _MONITOR_DIRECTIVE_TOOLS
        _MemoryUnavailable _NATIVE_SUBAGENT_STALE_SECS _PATH_TRUNCATION_MARKER
        _PENDING_RESET_RETRY_DELAY_SECS _PENDING_RESET_RETRY_WARN_EVERY _POISON_CANARY_PROMPT
        _POISON_CANARY_TIMEOUT_SECS _POSTTOKEN_RECOVER_MSG _PRE_SPAWN_CAPABILITY_CODES
        _PROMISE_ONLY_CONTINUE_MSG _QUEUE_KIND_ACTORS _RECIPIENT_LOGGED _RECIPIENT_LOGGED_CAP
        _REFUSAL_CARD_LEAD _REFUSAL_FALLBACK_RESUME_MSG _RESERVED _RESUME_PREFETCH_MAX_LIVE
        _RESUME_PREFETCH_TTL_SECS _RE_TRAILING_REQUEST_ID _SEGMENT_RAW_MAX_CHARS
        _SNAPSHOT_READ_BYTES _SNAPSHOT_TRUNCATION_MARKER _SPEC_HOOKS_UNREADABLE_BLOCK
        _STEER_NOTICE_BOUND_SECS _SYNTHESIS_RECHECK_MAX
        _SYNTHESIS_RECHECK_SECS _SYNTHETIC_RECOVERY_MSGS _Snapshot
        _TODO_BLOCK_READ_EVENT_KINDS _TURN_ACTOR_META_KEY _WRITE_COMMANDS
        _accumulate_segment_raw _active_fallback _actor_for_queue_items _admit_prefetch
        _agent_fallback_chain _answer_text_only _append_compaction_notice _append_native_output
        _apply_incognito_prefix _apply_turn_snapshot_budget _arm_generation
        _arm_pending_reset_retry _arm_queued_delivery_settlement
        _arm_synthesis_recheck _armed_prefetches
        _attach_turn_stats _attested_peer _audit_admission_refusal _audit_name_grant_refusal
        _authorize_recipient _auto_approve_reason _backfill_canonical_model
        _begin_local_turn_marker _broadcast_auto_tool _broadcast_compaction_result
        _broadcast_expired_oauth_banners _cap_armed_prefetches _cap_redacted
        _clear_eager_spawn_failures _clear_fallback_sticky_state
        _clear_local_turn_marker
        _clip_card_error _configured_refusal_fallback _connections_managed_mcp_names
        _consume_pending_reset _context_usage_payload _credential_tool_hint_for _crew_log_class
        _crew_log_lineage _crew_log_model _crew_log_workspace _current_turn_carries_image_ref
        _decisions_strip_meta _default_session_model _deliver_cross_surface_reply
        _deliver_cross_surface_user_message _deliver_linked_slack_message _dequeue_next_message
        _dequeue_next_system_message _detach_appended_context _directive_recovery_instruction
        _discard_stale_decision _drain_session_init_oauth_requests _drop_stale_admissions
        _eager_spawn _eager_spawn_backoff_secs _eager_spawn_held_off _eager_spawn_sem
        _emit_mcp_oauth_request _emit_recovery_outcome _emit_ttft_metric _emit_turn_metric
        _empty_auto_continue_enabled _empty_max_auto_continues _entry_arm_generation
        _evict_prefetches_beyond _expand_dollar_skills _expand_prompt_mention
        _expand_prompt_mention_off_loop _extract_base_command _extract_bash_command
        _extract_full_command _fallback_swap_for_turn _find_prompt _finish_queue_cycle
        _flush_file_changes _flush_segment _folder_steering_turn _gateway_shutdown_requested
        _get_skills _handle_goal_command _handle_workflow_command _has_user_queued_followup
        _is_bedrock_profile_id _is_pre_spawn_refusal _is_safe_oauth_url _is_turn_inject
        _jev_current_window _jev_downgrade_refused _jev_preview_on _jev_route_armed
        _jev_route_baseline _jev_route_premise_moved
        _launch_synthesis _line_change_input _link_principal
        _list_aim_prompts _local_turn_generation_for _local_turn_opening_row
        _log_glued_footer_text _log_once _mark_kiro_signed_out _mark_mcp_oauth_completed
        _mark_permission_resolved _mark_steer_row_state _marker_digest _mask_quoted_separators
        _matches_trusted_pattern _maybe_consolidate _maybe_inject_persona
        _mcp_server_name_is_ambiguous _model_unentitled_meta _name_grant_refusal_for
        _name_grant_refusal_off_loop _native_card_feed _native_crew_should_auto_approve
        _native_done_result _native_subagent_close_all _native_subagent_sync _normalize_model
        _note_cycle_start_failure _note_eager_spawn_failure _note_reply_row
        _pending_reset_retries _persist_tool_result_rows _persistable_session_policy
        _pinned_model_verdict _post_native_question_card _pre_tool_block_reason
        _pre_tool_hooks_should_block _prefetch_ttl _prepare_mirror_msg _prepare_spec_hooks
        _prewarm_allowance _probe_fallback_restore_for_slot
        _probe_fallback_restore_for_slot_locked _prompt_read_within_root
        _publish_session_mcp_report _queue_entry_is_orchestration
        _read_and_tighten_turn_execution _recipient_principal _reconstruct_str_replace_before
        _record_session_mcp_event _record_turn_snapshot _recover_app_agent_binding
        _recovery_delay _redact_acp_string _redact_display_text _redact_for_display
        _redact_meta_for_role _redact_segment _redact_segment_text _redact_tool_field
        _redacted_hook_block _refined_tool_row_content _reflow_label_and_audit
        _refusal_fallback_swap _register_native_card _reject_attributed _reject_hook_blocked
        _reject_hook_error _reject_invalid_tool _release_prefetch_reservation
        _remove_queued_by_id _requeue_unconsumed_steers _require_session_memory_assignment
        _resolve_channel_target _resolve_folder_steering_dirs _resolve_mirror_target
        _resolve_prompt_mention _resolve_refusal_fallback_target _restore_refusal_fallback
        _retain_terminal_native _retire_local_turn_marker _retire_sessions_on_identity_change
        _route_history_source _route_model_for_turn _run_chat
        _run_pending_synthesis _safe_native_crew_debug_title _safe_read_snapshot
        _schedule_prefetch_ttl _schedule_widget_registration _segment_row_meta
        _session_auto_approves _session_mcp_report
        _session_principal
        _session_stop_generation_for _settle_consumed_steers _shared_dependency_delay
        _should_suppress_requeue _slot_binding _slot_is_trusted _slot_predecessor_store
        _slot_prompt_project _snapshot_write_target _spawn_admitted_prefetch
        _spec_confirm_hooks_notice _spec_keys_notice _split_command_segments
        _start_next_queued_turn _steer_policy_notice _strip_yaml_frontmatter
        _supersede_open_mcp_oauth_banners _surface_agent_welcome _surface_prompt_chip
        _sync_served_model _tcid_identity_key _terminal_error_meta _tool_call_ws_payload
        _tool_identity_fields _tool_meta _tool_risk_meta
        _truncate_snapshot _turn_clock _turn_line_changes
        _turn_outcome _turn_rows _turn_start_priority
        _unregister_native_card _validate_tool_name acp_error_is_session_not_found
        acp_error_is_transient advance_fallback_candidate advertised_model_ids
        agent_welcome_message annotations app_inject_row append_and_surface
        apply_session_directive approval_command approval_display_command asyncio
        attachment_meta attributable_user_chars build_infra_retry_prompt build_recovery_requeue
        build_refusal_recovery_prompt build_refusal_steer_notice build_stale_recovery_prompt
        build_tool_stall_recovery_prompt canonical_memory_mode capabilities_of
        chat_done_payload chunk_for_transport chunk_generation classify_deny
        classify_empty_turn classify_stop_reason compact_unsupported_reply
        configured_fallback_chain consume_reinjection context_entry_expired credential_records
        crew_fired_spec_hooks crew_log_emit cross_surface_withheld data_home datetime
        default_ladder directive_queue disposition_for_stop_reason drain_pending_context
        drained_to_thread durable_row_count effective_session_key emit_counter
        emit_turn_duration emit_turn_usage expire_slack_options
        fallback_rewound_transient_budget find_written_steer_row fire_tool_hooks
        first_advertised_fallback format_approval_no_budget_card format_approval_timeout_card
        functools generate_session_summary get_instance get_recorder get_visible_providers
        has_leaked_tool_call has_unfinished_progress_claim hashlib hook_gate_kwargs
        identity_grant_covers_child inspect invalidate_stale_kas_session is_claude_backend_name
        is_claude_code is_coding_event is_dispatchable_member_name
        is_false_current_tool_blocker is_false_current_tool_blocker_near_miss
        is_harness_slash_command is_monitor_completion_evidence is_promise_only_terminal
        is_read_only_bash is_sensitive_path is_synthetic_payload_item
        is_synthetic_recovery_item is_system_injection_item json kirocrew_managed_names
        line_changes_from_file_changes local_turn_prompt_within_bounds log_decline logger
        logging math mcp_apps_render member_lifecycle mint_options_token mirror_is_paused
        model_is_unusable model_registry normalize_agent_model normalize_banner
        normalize_stop_reason note_coding_activity oauth_url_contains_credential os
        parse_hook_continuations parse_session_key
        parse_workflow_command payload_for_replay
        persist_token_record_async person_priority pick_epoch_host
        pin_human_approval post_linked_approval pre_tool_match_names prepare_store_vectors
        probe_fallback_restore provider_active_model provider_advertised_ids
        provider_fallback_active provider_raw_model publish_turn_identity
        queue_entry_is_user_origin queued_text_for_display re read_context_tokens
        read_effective_agent read_session_execution read_turn_model rearm_reinjection
        record_activity record_agent_selection record_interaction_event
        record_provider_agent_switch redact redact_and_truncate redact_credentials
        redact_credentials_with_records redact_exfiltration_urls
        redact_exfiltration_urls_with_records redact_for_display redact_log_via_context
        redact_via_context reflow_and_label_glued_option_marker refresh_materialized_agents
        refresh_tag_grants_cache refusal_card_text refusal_for_command_off_loop
        refuse_stale_switch register_guarded_history_write register_images_off_loop
        register_widgets_off_loop remember_slack_options reproject_claimed_session
        resolve_agent_bindings resolve_board_tags resolve_credential_tool_hint
        resolve_effective_model resolve_linked_approval resolve_pin_spelling
        resolve_session_agent_bindings resolve_substitute_set_model resource_status
        restore_agent_selection restore_replacement_if_handover_did_not_land
        resume_takes_tool_search_replay row_mid
        run_bg_oneliner run_in_embed_pool run_to_completion runtime_death safe_read_file
        safe_read_file_bytes_nolink safety_override sanitized_oauth_endpoint save_slot_off_loop
        schedule_eager_spawn sel sel_is_warm
        select_provider_backend session_agent session_agent_selection_kind
        session_directive session_skill_globs settle_consumed_steers shell_command_for_event
        should_continue_after_compaction should_log_decline
        should_notice_compaction_dropped_leak should_notice_leaked_tool_call
        should_notice_mixed_turn_leak should_queue_hook_continuation
        should_queue_refusal_recovery should_recover_promise_only shutdown_event
        slack_mirror_is_paused slot_history_key slot_steering_principal
        slot_switch_session_lock spawn_guarded_turn split_blocks stat_module
        stricter_memory_mode strip_control_comments subagents_attached_async
        subprocess_executor synthesis_fire_verdict
        telemetry_channel_of tighten_live_session_execution
        tighten_live_slot_memory_mode tighten_replacement_to_restricted_original time timezone
        title_then_refresh tool_approval_timeout_secs tool_calls_are_read_only_preparation
        transient_retry_delay turn_outcome
        turn_stats_meta unsafe_bash_reason unverified_directive_notice
        unverified_directive_outcome usage_has_billing user_text_span uuid
        validate_ask_user_question validate_file_path verify_mirror_admission
        warm_project_agent_names with_bounded_redaction_records
    """.split())

#: Each moved name, the owner that holds it, and its kind and signature in the
#: one-module runner (captured from it before the split).
_BASE_SURFACE: dict[str, tuple[tuple[str, str, str], ...]] = {
    "directives": (
        ("UNCLAIMED_DIRECTIVE_NOTICE", "value", "str"),
        ("_DIRECTIVE_NOT_APPLIED_FALLBACK", "value", "str"),
        ("_DIRECTIVE_NOT_APPLIED_OUTCOMES", "value", "dict"),
        ("_MONITOR_DIRECTIVE_TOOLS", "value", "frozenset"),
        ("_directive_recovery_instruction", "function", "(tool: 'str') -> 'str'"),
        ("unverified_directive_notice", "function", "(tool: 'str') -> 'str'"),
        ("unverified_directive_outcome", "function", "(tool: 'str') -> 'str'"),
    ),
    "file_changes": (
        ("_PATH_TRUNCATION_MARKER", "value", "str"),
        ("_Snapshot", "class", ""),
        (
            "_apply_turn_snapshot_budget",
            "function",
            "(entries: 'list[dict[str, Any]]') -> 'tuple[list[dict[str, Any]], int, int]'",
        ),
        (
            "_line_change_input",
            "function",
            "(fc: 'dict[str, Any]', after: '_Snapshot | None') -> 'dict[str, str] | None'",
        ),
        ("_note_reply_row", "function", "(slot: \"'_ChatSlot'\", row: 'dict[str, Any]') -> 'None'"),
        (
            "_reconstruct_str_replace_before",
            "function",
            "(path: 'str', raw_params: 'dict') -> 'str | None'",
        ),
        (
            "_record_turn_snapshot",
            "function",
            "(slot: \"'_ChatSlot'\", snapshot: 'dict[str, Any]') -> 'None'",
        ),
        ("_safe_read_snapshot", "function", "(path: 'str') -> '_Snapshot | None'"),
        (
            "_snapshot_write_target",
            "function",
            "(raw_params: 'dict | None', diff_old_text: 'str | None' = None, diff_path: 'str' = '') -> 'dict | None'",
        ),
        ("_truncate_snapshot", "function", "(content: 'str') -> '_Snapshot'"),
        ("_turn_line_changes", "function", "(changes: 'Any') -> 'int'"),
        (
            "_turn_rows",
            "function",
            "(slot: \"'_ChatSlot'\", turn_boundary: 'int', turn_start_mid: 'str | None') -> 'list[dict[str, Any]]'",
        ),
    ),
    "mcp_session": (
        ("_connections_managed_mcp_names", "function", "() -> 'frozenset[str]'"),
        (
            "_drain_session_init_oauth_requests",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", client: 'Any') -> 'None'",
        ),
        (
            "_mcp_server_name_is_ambiguous",
            "function",
            "(server_name: 'str', safe_name: 'str') -> 'bool'",
        ),
        (
            "_publish_session_mcp_report",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", provider: 'Any') -> 'None'",
        ),
        (
            "_record_session_mcp_event",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", provider: 'Any', kind: 'str', server_name: 'str', error: 'str' = '', *, fanout_no_owner: 'bool' = False) -> 'None'",
        ),
    ),
    "model_fallback": (
        ("_agent_fallback_chain", "function", "() -> 'tuple[str, ...]'"),
        ("_clear_fallback_sticky_state", "function", "(slot: 'Any', client: 'Any') -> 'None'"),
        ("_configured_refusal_fallback", "function", "() -> 'str'"),
        (
            "_default_session_model",
            "function",
            "(cfg: \"'KiroCrewConfig | None'\", slot: \"'_ChatSlot'\", agent_model: 'str') -> 'str'",
        ),
        (
            "_fallback_swap_for_turn",
            "async function",
            "(slot: 'Any', client: 'Any') -> 'str | None'",
        ),
        (
            "_probe_fallback_restore_for_slot",
            "async function",
            "(slot: 'Any', client: 'Any') -> 'None'",
        ),
        (
            "_probe_fallback_restore_for_slot_locked",
            "async function",
            "(slot: 'Any', client: 'Any') -> 'None'",
        ),
        (
            "_refusal_fallback_swap",
            "async function",
            "(slot: 'Any', client: 'Any', candidate: 'str', session_key: 'str' = '') -> 'str | None'",
        ),
        (
            "_resolve_refusal_fallback_target",
            "function",
            "(refusal: \"'RefusalInfo | None'\") -> 'str'",
        ),
        ("_restore_refusal_fallback", "async function", "(slot: 'Any', client: 'Any') -> 'None'"),
        ("_sync_served_model", "function", "(slot: 'Any', client: 'Any') -> 'None'"),
    ),
    "recipient": (
        ("_attested_peer", "function", "(transport: 'Any', conversation_id: 'str') -> 'str'"),
        (
            "_audit_admission_refusal",
            "function",
            "(session_key: 'str', link: 'Any', *, outcome: 'str' = 'unverified') -> 'None'",
        ),
        (
            "_authorize_recipient",
            "function",
            "(transport: 'Any', channel_type: 'str', conversation_id: 'str', thread_id: 'str | None', *, principal: 'str', session_key: 'str', audit_allowed: 'bool' = False) -> 'bool'",
        ),
        ("_link_principal", "function", "(link: 'Any') -> 'str'"),
        (
            "_log_once",
            "function",
            "(marker: 'tuple[str, str, str]', level: 'int', message: 'str', *args: 'Any') -> 'None'",
        ),
        ("_marker_digest", "function", "(marker: 'tuple[str, str, str]') -> 'str'"),
        (
            "_recipient_principal",
            "function",
            "(session_key: 'str', link: 'Any', transport: 'Any') -> 'str'",
        ),
        (
            "_resolve_channel_target",
            "function",
            "(state: 'Any', session_key: 'str', link: 'Any', *, principal: 'str | None' = None, check_recipient: 'bool' = True) -> 'Any'",
        ),
        ("_resolve_mirror_target", "function", "(state: 'Any', session_key: 'str') -> 'Any'"),
        ("_session_principal", "function", "(session_key: 'str') -> 'str'"),
        ("cross_surface_withheld", "function", "(state: 'Any', slot: 'Any') -> 'bool'"),
    ),
    "recovery": (
        (
            "_answer_text_only",
            "function",
            "(segment_text: 'str', notice_chunks: 'list[str]') -> 'str'",
        ),
        ("_current_turn_carries_image_ref", "function", "(message: 'str') -> 'bool'"),
        ("_empty_auto_continue_enabled", "function", "() -> 'bool'"),
        ("_empty_max_auto_continues", "function", "() -> 'int'"),
        (
            "_model_unentitled_meta",
            "function",
            "(exc: 'BaseException') -> 'dict[str, object] | None'",
        ),
        (
            "_note_cycle_start_failure",
            "function",
            "(slot_key: 'str', exc: 'BaseException', *, self_wake: 'bool') -> 'None'",
        ),
        ("_recovery_delay", "async function", "(secs: 'float') -> 'None'"),
        (
            "_session_stop_generation_for",
            "function",
            "(sessions: 'Any', session_key: 'str') -> 'int'",
        ),
        (
            "_shared_dependency_delay",
            "function",
            "(exc: 'BaseException', local_delay: 'float', *, slot_key: 'str') -> 'float'",
        ),
        ("_should_suppress_requeue", "function", "(slot) -> 'bool'"),
        (
            "_terminal_error_meta",
            "function",
            "(exc: 'BaseException') -> 'dict[str, object] | None'",
        ),
    ),
    "steer_queue": (
        ("_QUEUE_KIND_ACTORS", "value", "dict"),
        ("_actor_for_queue_items", "function", "(items: \"'list[dict]'\") -> 'str'"),
        (
            "_drop_stale_admissions",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot') -> 'None'",
        ),
        ("_has_user_queued_followup", "function", "(slot: \"'_ChatSlot'\") -> 'bool'"),
        (
            "_mark_steer_row_state",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", message: 'str', new_state: 'str', siblings: 'list[str] | None' = None) -> 'None'",
        ),
        ("_queue_entry_is_orchestration", "function", "(item: 'dict') -> 'bool'"),
        (
            "_requeue_unconsumed_steers",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\") -> 'None'",
        ),
        (
            "_settle_consumed_steers",
            "function",
            "(slot: \"'_ChatSlot'\", snapshot: 'str', state: \"'DashboardState | None'\" = None) -> 'None'",
        ),
    ),
    "tool_approval": (
        ("_CREDENTIAL_HINT_CLASSES", "value", "frozenset"),
        ("_SPEC_HOOKS_UNREADABLE_BLOCK", "value", "str"),
        (
            "_audit_name_grant_refusal",
            "function",
            "(*, session_key: 'str', slot: 'Any', event: 'Any', refusal: 'Refusal', tier: 'str') -> 'None'",
        ),
        ("_auto_approve_reason", "function", "(slot: 'Any', yolo_active: 'bool') -> 'str'"),
        (
            "_credential_tool_hint_for",
            "async function",
            "(reason: 'str', cause: 'str', subject: 'str' = '') -> 'str'",
        ),
        ("_name_grant_refusal_for", "async function", "(event: 'object') -> 'Refusal | None'"),
        ("_native_crew_should_auto_approve", "function", "(native_tracker, state, slot) -> 'bool'"),
        ("_persistable_session_policy", "function", "(slot: 'Any', yolo_active: 'bool') -> 'str'"),
        ("_pre_tool_block_reason", "function", "(pre_hook_results: 'Any') -> 'str'"),
        ("_pre_tool_hooks_should_block", "function", "(pre_hook_results: 'Any') -> 'bool'"),
        ("_slot_is_trusted", "function", "(slot: 'Any') -> 'bool'"),
        ("_spec_confirm_hooks_notice", "function", "(agent: 'str', count: 'int') -> 'str'"),
        ("_spec_keys_notice", "function", "(agent: 'str', keys: 'list[str]') -> 'str'"),
    ),
    "turn_context": (
        (
            "_detach_appended_context",
            "function",
            "(original: 'str', expanded: 'str') -> 'tuple[str, str]'",
        ),
        (
            "_folder_steering_turn",
            "function",
            "(slot: 'Any', execution_context: 'Any', *, context_is_new: 'bool', provider_has_history: 'bool', needs_reinjection: 'bool') -> 'bool'",
        ),
        (
            "_read_and_tighten_turn_execution",
            "function",
            "(conversation_log: 'Any', session_key: 'str', transcript_key: 'str | None' = None)",
        ),
        ("drain_pending_context", "function", "(slot: \"'_ChatSlot'\") -> 'str'"),
    ),
    "turn_marker": (
        ("_LOCAL_TURN_OPENER_ROLES", "value", "frozenset"),
        ("_LOCAL_TURN_PROMPT_META_KEYS", "value", "tuple"),
        (
            "_begin_local_turn_marker",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', generation: 'int') -> 'None'",
        ),
        (
            "_clear_local_turn_marker",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', generation: 'int') -> 'None'",
        ),
        ("_gateway_shutdown_requested", "function", "(state: 'DashboardState') -> 'bool'"),
        ("_local_turn_generation_for", "function", "(slot: '_ChatSlot') -> 'int'"),
        (
            "_local_turn_opening_row",
            "function",
            "(slot: '_ChatSlot') -> \"'dict[str, Any] | None'\"",
        ),
        (
            "_retire_local_turn_marker",
            "function",
            "(slot: '_ChatSlot', generation: 'int') -> 'bool'",
        ),
    ),
    "turn_stats": (
        ("_FirstVisibleClock", "class", ""),
        (
            "_attach_turn_stats",
            "function",
            "(slot: \"'_ChatSlot'\", elapsed_ms: 'int', credits: 'float', cost_usd: 'float', turn_boundary: 'int' = 0, model: 'str' = '', ttft_ms: 'int' = 0) -> 'bool'",
        ),
        (
            "_context_usage_payload",
            "function",
            "(slot_key: 'str', client: 'Any') -> 'dict[str, Any]'",
        ),
        (
            "_turn_clock",
            "function",
            "(slot: \"'_ChatSlot'\", t0: 'float | None', *, top_level: 'bool', recovery_turn: 'bool') -> '_FirstVisibleClock'",
        ),
    ),
}


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{_OWNER_PACKAGE}.{stem}")


def _owners() -> list[types.ModuleType]:
    return [_owner(info.name) for info in pkgutil.iter_modules([str(_OWNER_DIR)])]


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines at top level
    or as a member of a class the owner defines."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members = [(f"{name}.{k}", v) for k, v in vars(value).items()]
            for label, member in members:
                fn = getattr(member, "__func__", member)
                if isinstance(member, property):
                    fn = member.fget
                if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                    found.append((f"{owner.__name__.rsplit('.', 1)[-1]}.{label}", fn))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(_OWNER_DIR.glob("*.py"))
        if path.name != "__init__.py"
    }


# ── the surface ───────────────────────────────────────────────────────────────


def test_the_owner_set_is_the_package() -> None:
    assert {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])} == _OWNER_MODULES
    assert set(_BASE_SURFACE) <= _OWNER_MODULES


def test_every_name_the_runner_bound_at_the_base_still_resolves() -> None:
    """Callers, the ``dashboard.chat`` facade and tests read private names off the
    runner as well as public ones, so every module-level binding survives the
    split, not only the public surface."""
    assert len(_BASE_NAMES) > 600
    assert sorted(name for name in _BASE_NAMES if not hasattr(cr, name)) == []


def _member(name: str) -> tuple[object, str]:
    obj = getattr(cr, name)
    if inspect.isclass(obj):
        return obj, "class"
    if inspect.isfunction(obj):
        return obj, ("async " if inspect.iscoroutinefunction(obj) else "") + "function"
    return obj, "value"


@pytest.mark.parametrize(
    ("owner", "name", "kind", "signature"),
    [(owner, *row) for owner, rows in _BASE_SURFACE.items() for row in rows],
    ids=[f"{owner}:{row[0]}" for owner, rows in _BASE_SURFACE.items() for row in rows],
)
def test_a_moved_name_keeps_its_base_shape_and_owner(
    owner: str, name: str, kind: str, signature: str
) -> None:
    """Every name that moved keeps the kind and signature it had in the one-module
    runner, lives in the owner its responsibility names, and is ONE object: the
    runner attribute and the owner's are the same."""
    obj, found_kind = _member(name)
    assert found_kind == kind
    if kind == "value":
        assert type(obj).__name__ == signature
    elif kind != "class":
        assert str(inspect.signature(obj)) == signature  # type: ignore[arg-type]
    assert getattr(_owner(owner), name) is obj


def test_the_handler_seams_are_the_runner_objects() -> None:
    """``chat_handlers`` imports six seams from the runner by name; each must be the
    object the runner holds, so a patch on the runner and the handler agree."""
    from kiro_crew.dashboard import chat_handlers, state

    seams = (
        "_context_usage_payload",
        "_run_chat",
        "_start_next_queued_turn",
        "_sync_served_model",
        "context_entry_expired",
        "schedule_eager_spawn",
    )
    assert all(getattr(chat_handlers, name) is getattr(cr, name) for name in seams)
    assert cr.context_entry_expired is state.context_entry_expired


def test_the_chat_facade_reexports_the_runner_objects() -> None:
    from kiro_crew.dashboard import chat

    tree = ast.parse(Path(chat.__file__).read_text(encoding="utf-8"))
    names = [
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == _FACADE
        for alias in node.names
    ]
    assert len(names) >= 3
    assert [name for name in names if getattr(chat, name) is not getattr(cr, name)] == []


#: Functions that stay defined in ``dashboard/chat_runner.py``. The first five are
#: the coroutines tasks are started with: ``stall_attribution`` names a stalled task
#: by the outermost frame it can place, and a frame in that file is what reads as a
#: dashboard chat turn. ``_arm_pending_reset_retry`` and ``_arm_synthesis_recheck``
#: define the coroutines they start, ``_launch_synthesis`` starts the pending
#: synthesis, and ``_finish_queue_cycle`` starts it and the title and summary passes,
#: so each stays beside the tasks it creates.
_RUNNER_FILE_DEFS = (
    "_run_chat",
    "_start_next_queued_turn",
    "_run_pending_synthesis",
    "_eager_spawn",
    "_prefetch_ttl",
    "_arm_pending_reset_retry",
    "_arm_synthesis_recheck",
    "_launch_synthesis",
    "_finish_queue_cycle",
)


@pytest.mark.parametrize("name", _RUNNER_FILE_DEFS)
def test_a_task_entry_stays_defined_in_the_runner_file(name: str) -> None:
    assert Path(getattr(cr, name).__code__.co_filename).resolve() == _FACADE_PATH


#: Owner phases cut from blocks of ``_run_chat`` and ``_start_next_queued_turn``
#: that never awaited. Each is a coroutine so the async-path guards keep reading it,
#: and its body still runs without a suspension point: the check-then-mutate a queue
#: drain or a consume gate makes stays atomic on the event loop, as the inline block
#: was. An ``await`` added to one would let another task change the slot between the
#: check and the mutation.
_AWAIT_FREE_PHASES = (
    "_refresh_genuine_turn_allowances",
    "_rearm_turn_episode",
    "_checklist_resync",
    "_purge_superseded_continuations",
    "_drop_revoked_replays",
    "_replay_vetoed_at_consume",
    "_requeue_auth_retry",
    "_requeue_after_prompt_busy",
    "_report_unclaimed_directives",
)
_SUSPENDING_OPS = frozenset(
    {"GET_AWAITABLE", "SEND", "YIELD_VALUE", "GET_AITER", "GET_ANEXT", "BEFORE_ASYNC_WITH"}
)


def _suspends(fn: Any) -> bool:
    return bool({op.opname for op in dis.get_instructions(fn)} & _SUSPENDING_OPS)


def test_the_suspension_scan_sees_an_await() -> None:
    async def awaits() -> None:
        await asyncio.sleep(0)

    async def does_not() -> None:
        return None

    assert _suspends(awaits)
    assert not _suspends(does_not)


@pytest.mark.parametrize("name", _AWAIT_FREE_PHASES)
def test_an_await_free_phase_never_suspends(name: str) -> None:
    fn = getattr(cr, name)
    assert inspect.iscoroutinefunction(fn)
    assert Path(fn.__code__.co_filename).resolve().parent == _OWNER_DIR
    assert not _suspends(fn), f"{name} gained a suspension point"


def test_run_chat_keeps_its_entry_signature() -> None:
    sig = inspect.signature(cr._run_chat)
    assert list(sig.parameters) == [
        "state",
        "slot",
        # Supplied by the exit guard (``_hands_off_queue_on_exit``), never by a
        # caller; the signature follows ``__wrapped__`` to the turn itself.
        "turn_exit",
        "message",
        "_prompt_depth",
        "_attachments",
        "_attachment_meta",
        "_synthetic_payload",
        "_replay",
        "_synthetic_recovery_turn",
        "_replays_completion",
        "_steer_possibly_delivered",
        "_directive_user_origin",
        "_turn_provenance_restored",
        "_directive_self_wake",
        "_directive_loop_id",
        "_directive_loop_gen",
        "_directive_channel_origin",
        "_turn_actor",
        "regenerate_hint",
        "_on_consumed",
        "_on_irreversibly_consumed",
        "monitor_completion",
        "_current_message",
    ]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in list(sig.parameters.values())[4:])
    assert inspect.iscoroutinefunction(cr._run_chat)


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """``from kiro_crew.dashboard.chat_runner import *`` exports what the one-module
    file did; the runner declares no ``__all__``, so every public binding goes."""
    assert not hasattr(cr, "__all__")
    probe = tmp_path / "chat_runner_star_probe.py"
    probe.write_text(
        "from kiro_crew.dashboard.chat_runner import *  # noqa: F401,F403\n", encoding="utf-8"
    )
    spec = importlib.util.spec_from_file_location("chat_runner_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (
        "drain_pending_context",
        "cross_surface_withheld",
        "unverified_directive_notice",
        "UNCLAIMED_DIRECTIVE_NOTICE",
    ):
        assert getattr(module, name) is getattr(cr, name)


#: Module-level state the runner owns. Owner functions reach each by name through
#: the runner's namespace, so a test that rebinds one on the runner is the binding
#: every function sees -- which holds only while no owner keeps a copy.
_RUNNER_STATE = (
    "_RECIPIENT_LOGGED",
    "_RECIPIENT_LOGGED_CAP",
    "_eager_spawn_sem",
    "_armed_prefetches",
    "_arm_generation",
    "_pending_reset_retries",
    "logger",
)


@pytest.mark.parametrize("name", _RUNNER_STATE)
def test_runner_state_stays_on_the_runner(name: str) -> None:
    assert name in vars(cr)
    assert [o.__name__ for o in _owners() if name in vars(o)] == []


def test_the_owners_log_as_the_runner() -> None:
    """Log capture keyed to ``kiro_crew.dashboard.chat_runner`` keeps seeing the
    moved sites: an owner function logs through the runner's ``logger``."""
    assert cr.logger.name == _FACADE
    readers = [
        label for label, fn in _owner_functions() if "logger" in set(_global_names(fn.__code__))
    ]
    assert len(readers) >= 20


# ── one namespace ─────────────────────────────────────────────────────────────


def test_the_owners_define_functions_for_the_sweeps_to_check() -> None:
    """Every sweep below proves nothing unless it has functions to sweep."""
    labels = {label for label, _ in _owner_functions()}
    callables = sum(
        kind.endswith("function") for rows in _BASE_SURFACE.values() for _, kind, _ in rows
    )
    assert len(labels) >= callables
    assert {"recipient._resolve_channel_target", "steer_queue._drop_stale_admissions"} <= labels


def test_every_owner_function_runs_on_the_runner_globals() -> None:
    """A patch of ``kiro_crew.dashboard.chat_runner.<name>`` reaches an owner function
    only because the function reads the runner's globals, not its own module's."""
    strays = [
        label
        for label, fn in _owner_functions()
        if fn.__globals__ is not vars(cr) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_runner_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_runner_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_runner_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_runner() -> None:
    """An owner's own imports are inert for its functions, so a name missing from the
    runner surfaces only when its line runs -- often inside an ``except`` that turns
    the NameError into a refusal. The sweep makes it a test failure instead."""
    namespace = vars(cr)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_runner_reaches_an_owner_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract the rebinding exists for, exercised end to end on two owners."""
    calls: list[tuple[object, ...]] = []

    def _target(state: object, session_key: str, link: object) -> tuple[str, str]:
        calls.append((state, session_key, link))
        return ("chan", "thread")

    sessions = types.SimpleNamespace(get_mirror_link=lambda key: f"link:{key}")
    state = types.SimpleNamespace(sessions=sessions)
    monkeypatch.setattr(cr, "_resolve_channel_target", _target)
    assert cr._resolve_mirror_target(state, "dashboard:s1") == ("chan", "thread")
    assert calls == [(state, "dashboard:s1", "link:dashboard:s1")]

    session = types.SimpleNamespace(empty_response_max_continues=4)
    loaded = types.SimpleNamespace(load=lambda: types.SimpleNamespace(session=session))
    monkeypatch.setattr(cr, "KiroCrewConfig", loaded)
    assert cr._empty_max_auto_continues() == 4


def test_module_and_qualname_still_name_the_runner() -> None:
    """Reprs and pickling by reference read as they did before the split: every
    owner function resolves back through its own ``__module__`` and ``__qualname__``,
    and an owner's classes keep their own module (``inspect`` finds a class's source
    through it)."""
    wrong = []
    for label, fn in _owner_functions():
        target: object = sys.modules[fn.__module__]
        for part in fn.__qualname__.split("."):
            target = (
                vars(target).get(part) if isinstance(target, type) else getattr(target, part, None)
            )
            target = getattr(target, "fget", target)
            target = getattr(target, "__func__", target)
        if target is not fn:
            wrong.append(label)
    assert wrong == []
    assert cr._Snapshot.__module__ == f"{_OWNER_PACKAGE}.file_changes"


def test_a_moved_function_reads_its_source_from_its_owner() -> None:
    source = inspect.getsource(cr._requeue_unconsumed_steers)
    assert source.startswith("def _requeue_unconsumed_steers(")
    assert inspect.getsourcefile(cr._requeue_unconsumed_steers) == _owner("steer_queue").__file__


# ── one edge ──────────────────────────────────────────────────────────────────


def _package_of(path: Path) -> str:
    """The package a relative import in a file under ``src/`` resolves against."""
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def _import_targets(tree: ast.Module, package: str) -> list[tuple[ast.AST, str]]:
    """``(node, dotted module)`` for every module a tree imports, spelled any way.

    Covers ``import a.b``, ``from a import b`` (which may name module ``a.b``),
    relative ``from . import x`` / ``from ..x import y`` resolved against *package*,
    and a string-literal ``importlib.import_module(...)`` / ``__import__(...)`` call.
    """
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            found.append((node, base))
            found.extend((node, f"{base}.{alias.name}") for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            name = node.args[0].value
            if name.startswith("."):
                anchor = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(anchor, ast.Constant) and isinstance(anchor.value, str)):
                    continue
                name = importlib.util.resolve_name(name, anchor.value)
            found.append((node, name))
    return found


def _is_owner(target: str) -> bool:
    return target == _OWNER_PACKAGE or target.startswith(f"{_OWNER_PACKAGE}.")


def _is_facade_or_owner(target: str) -> bool:
    return target == _FACADE or target.startswith(f"{_FACADE}.") or _is_owner(target)


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    """Nodes under a module-level ``if TYPE_CHECKING:`` body; its ``else`` runs."""
    return {
        id(sub)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }


def _owner_importers(source: str, package: str) -> list[int]:
    """Lines where a module imports an owner, in any spelling and under
    ``TYPE_CHECKING`` too (a type-only import still makes an owner part of another
    module's surface)."""
    tree = ast.parse(source)
    return sorted(
        {node.lineno for node, target in _import_targets(tree, package) if _is_owner(target)}
    )


def _owner_runtime_edges(source: str, package: str) -> list[int]:
    """Lines where an owner imports the runner or an owner outside ``TYPE_CHECKING``."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    return sorted(
        {
            node.lineno
            for node, target in _import_targets(tree, package)
            if _is_facade_or_owner(target) and id(node) not in guarded
        }
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import recovery\n", True),
        ("from .recovery import _recovery_delay\n", True),
        ("from .. import chat_runner\n", True),
        ("from ..chat_runner import sel\n", True),
        ("from kiro_crew.dashboard import chat_runner\n", True),
        ("from kiro_crew.dashboard import chat_turn\n", True),
        ("import kiro_crew.dashboard.chat_runner\n", True),
        ("import kiro_crew.dashboard.chat_runner as runner\n", True),
        ("from kiro_crew.dashboard.chat_runner import sel\n", True),
        ("def f():\n    from kiro_crew.dashboard.chat_runner import sel\n", True),
        ("import importlib\nimportlib.import_module('kiro_crew.dashboard.chat_runner')\n", True),
        ("__import__('kiro_crew.dashboard.chat_turn.recovery')\n", True),
        (
            "import importlib\n"
            "importlib.import_module('.recovery', 'kiro_crew.dashboard.chat_turn')\n",
            True,
        ),
        ("if TYPE_CHECKING:\n    from kiro_crew.dashboard.chat_runner import sel\n", False),
        ("if TYPE_CHECKING:\n    from .. import chat_runner\n", False),
        ("from kiro_crew.dashboard import chat_utils\n", False),
        ("import kiro_crew.dashboard.chat_runner_elsewhere\n", False),
        ("from kiro_crew.dashboard.chat_utils import run_to_completion\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    """The runtime-edge check below is only as good as the spellings it resolves."""
    assert bool(_owner_runtime_edges(source, _OWNER_PACKAGE)) is flagged


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from .chat_turn import recovery\n", True),
        ("from . import chat_turn\n", True),
        ("from .chat_turn.recovery import _recovery_delay\n", True),
        ("import kiro_crew.dashboard.chat_turn.recovery\n", True),
        ("from kiro_crew.dashboard.chat_turn import recovery as r\n", True),
        ("if TYPE_CHECKING:\n    from .chat_turn import recovery\n", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.dashboard.chat_turn.recovery')\n",
            True,
        ),
        ("from . import chat_runner\n", False),
        ("from .chat_runner import _run_chat\n", False),
        ("from kiro_crew.dashboard import chat_runner\n", False),
        ("import importlib\nimportlib.import_module(name)\n", False),
    ],
)
def test_the_importer_check_sees_every_spelling(source: str, flagged: bool) -> None:
    """The one-import-path check below is only as good as the spellings it resolves."""
    assert bool(_owner_importers(source, "kiro_crew.dashboard")) is flagged


def test_nothing_but_the_runner_imports_an_owner() -> None:
    """An owner function imported straight from its module would be the same rebound
    object, but the runner is the one import path and the one patch surface, so no
    production module reaches past it."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if _OWNER_DIR in resolved.parents or resolved == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "chat_turn" not in text:
            continue
        importers.extend(
            f"{path.relative_to(_SRC)}:{line}" for line in _owner_importers(text, _package_of(path))
        )
    assert importers == []


def test_an_owner_imports_the_runner_and_its_siblings_only_for_type_checking() -> None:
    """No owner imports the runner or another owner at runtime: the runner imports
    the owners and nothing points back, so there is no import cycle to order."""
    offenders = [
        f"{stem}:{line}"
        for stem, source in _owner_sources().items()
        for line in _owner_runtime_edges(source, _OWNER_PACKAGE)
    ]
    assert offenders == []


def test_every_owner_with_a_coroutine_is_in_the_config_dir_guard() -> None:
    """``test_no_config_dir_in_async`` scans the files it lists, so an owner that
    gains an ``async def`` must be listed there, or its coroutines escape the guard."""
    guard = repo_root() / "test" / "test_no_config_dir_in_async.py"
    listed: set[str] = set()
    for node in ast.parse(guard.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_ASYNC_CHECKED_FILES" for t in node.targets
        ):
            listed = {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)}
    assert listed, "_ASYNC_CHECKED_FILES not found; this check went stale"
    assert "dashboard/chat_runner.py" in listed
    with_coroutines = {
        f"dashboard/chat_turn/{stem}.py"
        for stem, source in _owner_sources().items()
        if any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(ast.parse(source)))
    }
    assert with_coroutines, "no owner defines a coroutine; this check went stale"
    assert sorted(with_coroutines - listed) == []


def _run_child(tmp_path: Path, script: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_a_fresh_runner_import_loads_every_owner(tmp_path: Path) -> None:
    """Importing the runner imports every owner with it: none loads lazily on a
    later call, so the import order stays the one the one-module file had."""
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.chat_runner
        missing = [n for n in sys.argv[1:] if f"kiro_crew.dashboard.chat_turn.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """,
        *sorted(_OWNER_MODULES),
    )


def test_a_second_runner_import_recomposes_the_owners_onto_it(tmp_path: Path) -> None:
    """A process that purges the runner and imports it again gets owners that run on
    the NEW namespace."""
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.chat_runner as first
        del sys.modules["kiro_crew.dashboard.chat_runner"]
        import kiro_crew.dashboard.chat_runner as second
        from kiro_crew.dashboard.chat_turn import steer_queue
        fn = second._requeue_unconsumed_steers
        assert second is not first
        assert fn.__globals__ is vars(second)
        assert steer_queue._requeue_unconsumed_steers is fn
        print("ok")
        """,
    )


# ── the patch reach ───────────────────────────────────────────────────────────

#: Pre-filter: a test source that names the runner at all.
_MENTIONS_THE_RUNNER = re.compile(r"chat_runner")

#: The patch helpers whose ``(target, "name", ...)`` call rebinds a facade name.
_PATCH_CALLS = ("setattr", "patch.object", "delattr")
_PATCH_MULTIPLE = ("patch.multiple",)
#: ``patch.multiple`` keywords that configure the patch rather than name a target.
_MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_RUNNER_STRING = re.compile(r"""^kiro_crew\.dashboard\.chat_runner\.(\w+)$""")


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression spelling a test module binds to the runner module, to a
    fixed point: imports, ``importlib.import_module``, ``sys.modules[...]`` and
    plain re-assignment."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.dashboard":
            aliases |= {a.asname or a.name for a in node.names if a.name == "chat_runner"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Name) or target.id in aliases:
                continue
            bound = ast.unparse(value) in aliases
            imported = (
                isinstance(value, ast.Call)
                and ast.unparse(value.func).endswith("import_module")
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == _FACADE
            )
            if bound or imported:
                aliases.add(target.id)
                changed = True
    return aliases


def _parametrized_strings(function: ast.AST) -> dict[str, set[str]]:
    """``{parameter: values}`` for a test function's ``pytest.mark.parametrize``
    decorators whose values are string literals."""
    found: dict[str, set[str]] = {}
    for decorator in getattr(function, "decorator_list", []):
        if not (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).endswith("parametrize")
            and len(decorator.args) >= 2
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[1], (ast.List, ast.Tuple))
        ):
            continue
        names = [n.strip() for n in str(decorator.args[0].value).split(",")]
        if len(names) == 1:
            values = {e.value for e in decorator.args[1].elts if isinstance(e, ast.Constant)}
            if values and all(isinstance(v, str) for v in values):
                found[names[0]] = values
    return found


def _resolve_name(node: ast.AST, params: dict[str, set[str]]) -> set[str] | None:
    """The attribute names *node* can spell, or None when the scan cannot tell."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in params:
        return set(params[node.id])
    return None


def _resolve_target(node: ast.AST, params: dict[str, set[str]]) -> set[str] | None:
    """The runner names a ``patch("kiro_crew.dashboard.chat_runner.<name>")`` target
    can spell, an empty set when it names something else, or None when unresolved."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        match = _RUNNER_STRING.match(node.value)
        return {match.group(1)} if match else set()
    if isinstance(node, ast.JoinedStr) and ast.unparse(node).startswith(f"f'{_FACADE}."):
        tail = node.values[-1]
        if len(node.values) == 2 and isinstance(tail, ast.FormattedValue):
            return _resolve_name(tail.value, params)
        return None
    return set()


def _patched_names_in(text: str) -> set[str]:
    """Names one test source rebinds on the runner itself.

    A dotted target (``chat_runner.KiroCrewConfig.load``) patches an attribute of a
    shared object rather than a runner binding, so every holder of that object sees
    it; only first-level runner names count. A name spelled by a parametrized
    argument resolves to its parameter values; anything else the scan cannot
    resolve is reported as ``<dynamic>``, which fails the patch-reach test closed.
    """
    if not _MENTIONS_THE_RUNNER.search(text):
        return set()
    tree = ast.parse(text)
    aliases = _facade_aliases(tree)
    found: set[str] = set()
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    scopes = [(tree, {})] + [(fn, _parametrized_strings(fn)) for fn in functions]
    seen: set[int] = set()
    for scope, params in reversed(scopes):
        for node in ast.walk(scope):
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                target = node.args[0] if node.args else kw.get("target")
                if target is not None and ast.unparse(target) in aliases:
                    if func.endswith(_PATCH_CALLS):
                        name = (
                            node.args[1]
                            if len(node.args) >= 2
                            else kw.get("attribute", kw.get("name"))
                        )
                        resolved = _resolve_name(name, params) if name is not None else None
                        found |= resolved if resolved is not None else {"<dynamic>"}
                    elif func.endswith(_PATCH_MULTIPLE):
                        if any(k.arg is None for k in node.keywords):
                            found.add("<dynamic>")
                        found |= {k for k in kw if k not in _MULTIPLE_OPTIONS}
                elif target is not None and func.split(".")[-1] in ("patch", "setattr", "delattr"):
                    resolved = _resolve_target(target, params)
                    found |= resolved if resolved is not None else {"<dynamic>"}
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and ast.unparse(target.value) in aliases:
                        found.add(target.attr)
    found |= set(re.findall(r"""["']kiro_crew\.dashboard\.chat_runner\.(\w+)["']""", text))
    return found


def _facade_patched_names() -> set[str]:
    """Names any test rebinds on the runner."""
    root = repo_root()
    here = Path(__file__).resolve()
    found: set[str] = set()
    for path in repo_files_named(".py"):
        parts = path.relative_to(root).parts
        in_tests = parts[0] == "test" or (parts[0] == "src" and "tests" in parts)
        if not in_tests or path.resolve() == here:
            continue
        found |= _patched_names_in(path.read_text(encoding="utf-8", errors="replace"))
    return found


def _captured_names(source: str) -> set[str]:
    """Names an owner module binds or evaluates when it LOADS, outside ``TYPE_CHECKING``.

    That is everything a later patch of the runner cannot reach: a runtime import
    (at module level or nested in a module-level ``if``/``try``/``with``), a default
    argument, a decorator, a class base, a class body, or a module-level expression.
    A name read inside a function body is read at call time from the runner's
    globals, so it is not captured. The module docstring evaluates nothing.
    """
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    found: set[str] = set()

    def loads(node: ast.AST) -> set[str]:
        return {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

    def visit(statements: list[ast.stmt]) -> None:
        for node in statements:
            if id(node) in guarded:
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.update((a.asname or a.name).split(".")[0] for a in node.names)
                found.update(a.name.split(".")[-1] for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for part in node.decorator_list + node.args.defaults:
                    found.update(loads(part))
                for part in node.args.kw_defaults:
                    if part is not None:
                        found.update(loads(part))
            elif isinstance(node, ast.ClassDef):
                for part in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                    found.update(loads(part))
                visit(node.body)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("test", "iter", "items"):
                    value = getattr(node, field, None)
                    if isinstance(value, ast.AST):
                        found.update(loads(value))
                    elif isinstance(value, list):
                        for item in value:
                            found.update(loads(item))
                for block in ("body", "orelse", "finalbody"):
                    visit(getattr(node, block, []))
                for handler in getattr(node, "handlers", []):
                    if handler.type is not None:
                        found.update(loads(handler.type))
                    visit(handler.body)
            elif not (
                node is tree.body[0]
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.update(loads(node))

    visit(tree.body)
    return found


def test_the_patch_scan_reads_every_spelling() -> None:
    planted = (
        "import importlib, sys\n"
        "import kiro_crew.dashboard.chat_runner as runner\n"
        "from kiro_crew.dashboard import chat_runner as crm\n"
        "facade = importlib.import_module('kiro_crew.dashboard.chat_runner')\n"
        "alias = facade\n"
        "typed: object = alias\n"
        "held = sys.modules['kiro_crew.dashboard.chat_runner']\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(runner, 'first', 1)\n"
        "    monkeypatch.setattr(crm, 'second', 2)\n"
        "    patch.object(alias, 'third')\n"
        "    crm.fourth = 4\n"
        "    monkeypatch.setattr('kiro_crew.dashboard.chat_runner.fifth', 5)\n"
        "    monkeypatch.setattr('kiro_crew.dashboard.chat_runner.Shared.attr', 6)\n"
        "    monkeypatch.setattr(crm.Shared, 'attr', 7)\n"
        "    monkeypatch.setattr(other, 'not_runner', 8)\n"
        "    monkeypatch.delattr(crm, 'sixth')\n"
        "    patch.object(target=typed, attribute='seventh')\n"
        "    patch.multiple(held, eighth=1, ninth=2, create=True)\n"
        "@pytest.mark.parametrize('which', ['tenth', 'eleventh'])\n"
        "def test_param(which):\n"
        "    patch(f'kiro_crew.dashboard.chat_runner.{which}')\n"
        "    patch.object(crm, which)\n"
    )
    assert _patched_names_in(planted) == {
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
    }
    unresolved = (
        "from kiro_crew.dashboard import chat_runner as crm\n"
        "def test(monkeypatch, name):\n"
        "    patch(f'kiro_crew.dashboard.chat_runner.{name}')\n"
    )
    assert _patched_names_in(unresolved) == {"<dynamic>"}
    unresolved_name = (
        "from kiro_crew.dashboard import chat_runner as crm\n"
        "def test(monkeypatch, name):\n"
        "    monkeypatch.setattr(crm, name, 1)\n"
    )
    assert _patched_names_in(unresolved_name) == {"<dynamic>"}
    splat = (
        "from kiro_crew.dashboard import chat_runner as crm\n"
        "def test():\n"
        "    patch.multiple(crm, **targets)\n"
    )
    assert _patched_names_in(splat) == {"<dynamic>"}


def test_the_capture_scan_flags_what_an_owner_evaluates_when_it_loads() -> None:
    planted = (
        '"""An owner."""\n'
        "from typing import TYPE_CHECKING\n"
        "from kiro_crew.dashboard.chat_utils import run_to_completion\n"
        "import asyncio as aio\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.dashboard.chat_runner import sel\n"
        "else:\n"
        "    from kiro_crew.sel import log_runtime\n"
        "try:\n"
        "    from kiro_crew.platform import nested_import\n"
        "except ImportError:\n"
        "    nested_import = None\n"
        "LIMIT = _MAX_SLOT_MESSAGES * 2\n"
        "_note(LIMIT)\n"
        "def f(x=_DEFAULT, *, y=_KW):\n"
        "    return sel(), save_slot_off_loop\n"
        "class C(_Base):\n"
        "    attr = _CLASS_BODY\n"
    )
    captured = _captured_names(planted)
    assert {"run_to_completion", "aio", "asyncio", "_MAX_SLOT_MESSAGES", "_note"} <= captured
    assert {"_DEFAULT", "_KW", "_Base", "_CLASS_BODY", "log_runtime", "nested_import"} <= captured
    assert {"sel", "save_slot_off_loop"} & captured == set()


def test_no_owner_captures_a_name_tests_rebind_on_the_runner() -> None:
    """The contract the composition stands for, derived from the tests: an owner
    that imported, defaulted or evaluated a rebound name when it loaded would keep
    that object, and a patch of the runner would silently stop applying there. An
    owner may DEFINE one -- the runner's binding of it is the rebound copy, and the
    runner's binding is the seam -- because every caller reads it through the
    runner's globals."""
    patched = _facade_patched_names()
    # Non-vacuous: the scan sees seams the runner's tests rebind, the moved ones too.
    assert {
        "sel",
        "save_slot_off_loop",
        "_flush_file_changes",
        "_resolve_channel_target",
        "_recovery_delay",
        "_should_suppress_requeue",
        "_RECIPIENT_LOGGED",
        "_EAGER_SPAWN_DEBOUNCE_SECS",
        "spawn_guarded_turn",
    } <= patched
    assert len(patched) >= 100
    assert "<dynamic>" not in patched, "a test patches a runner name the scan cannot resolve"
    for stem, source in _owner_sources().items():
        assert _captured_names(source) & patched == set(), stem
        defined = {name for name in vars(_owner(stem)) if name in patched}
        assert all(getattr(cr, name) is vars(_owner(stem))[name] for name in defined), stem


# ── the path-keyed guards keep their reach ────────────────────────────────────

#: Constructs repository guards enumerate in ``dashboard/chat_runner.py`` by path:
#: redaction sinks and the redactor census, the deny chokepoints and the approve
#: sites, the turn-ceiling, allocation, usage-row and turn-metric sites, the
#: runtime-death attribution, the transcript-derivation plumbing, the hook gate, the
#: agent-spec read registry, the slot.running reader map, the task entries, the
#: stop classifier, the crew-log closers, the persona gate, the L1 budget resets, the
#: memory-store seam's calls, the frame contract, the compaction-continuation and
#: native question-card wiring, and the start-priority claimer calls. An owner that
#: grew one would move it out of such a guard's sight without failing it, so each
#: stays in the runner.
_STAYS_IN_THE_RUNNER = (
    r"\b\w*redact\w*\(",
    r"StreamRedactor",
    r"\.reject_tool\(",
    r"_reject_attributed\(",
    r"\.approve_tool\(",
    r"begin_turn\(",
    r"get_or_create\(",
    r"persist_token_record",
    r"_emit_turn_metric\(",
    r"caused_by_this_session",
    r"note_shared_death",
    r"clear_shared_deaths",
    r"_acp_pipe_death_retries",
    r"except AcpProcessDied",
    r"\.recent\(",
    r"warm_project_agent_names\(",
    r"\.on_tool_call\(",
    r"spawn_guarded_turn\(",
    r"create_task\(",
    r"ensure_future\(",
    r"\bslot\.task = ",
    r"\.(?:turn_)?running\b",
    r"is_claude_code\(",
    r"capabilities_of\(",
    r"classify_stop_reason\(",
    r"notify_turn_complete\(",
    r"on_tool_completed\(",
    r"latch_crew_log_previous\(",
    r"capabilities\.theme_persona",
    r"(?m)^_CONTEXT_FRAME_CONTRACT\b",
    r"slot\._infra_retries = 0",
    r"slot\._transient_5xx_retries = 0",
    r"\bmemory_store=",
    r"compaction_settled=_compaction_completed,",
    r"await _post_native_question_card\(",
    r"\brun_bg_oneliner\(",
)


@pytest.mark.parametrize("pattern", _STAYS_IN_THE_RUNNER)
def test_a_construct_the_runner_guards_count_stays_in_the_runner(pattern: str) -> None:
    assert re.search(pattern, _FACADE_PATH.read_text(encoding="utf-8"))
    holders = [stem for stem, source in _owner_sources().items() if re.search(pattern, source)]
    assert holders == []


def test_a_stalled_owner_frame_still_reads_as_a_dashboard_chat_turn() -> None:
    """A moved helper called from outside the runner (the mirror, a handler) leaves
    an owner file as the only frame ``stall_attribution`` recognises, and that
    frame names the dashboard chat surface as the runner's own frame does."""
    from kiro_crew.stall_attribution import Frame, classify_surface

    outside = Frame("/site/kiro_crew/dashboard/chat_mirror.py", 10, "mirror_reply")
    for path in (_FACADE_PATH, *(_OWNER_DIR / f"{stem}.py" for stem in sorted(_OWNER_MODULES))):
        inner = Frame(path.as_posix(), 1, "_resolve_channel_target")
        assert classify_surface([inner, outside]) == "dashboard chat", path.name
    assert classify_surface([outside]) == "unknown"


# ── compose, on its own ───────────────────────────────────────────────────────


def _write_module(tmp_path: Path, name: str, source: str) -> types.ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_rebinds_functions_and_class_members(tmp_path: Path) -> None:
    """On synthetic modules: a module function, a nested function, a method, a
    static method and a property of an owner class all read the host namespace
    afterwards; a function the owner merely imported is left alone; the owner class
    keeps its own module; and a second compose onto a fresh namespace moves them."""
    owner = _write_module(
        tmp_path,
        "chat_turn_compose_owner_probe",
        """
        from os.path import join

        def helper():
            return VALUE

        def outer():
            def inner():
                return VALUE
            return inner

        class Tally:
            def method(self):
                return VALUE

            @staticmethod
            def static():
                return VALUE

            @property
            def value(self):
                return VALUE
        """,
    )
    namespace = {"__name__": "chat_turn_compose_host_probe", "VALUE": "host"}
    namespace["helper"] = owner.helper
    chat_turn.compose(namespace, (owner,))

    assert namespace["helper"]() == "host" and owner.helper is namespace["helper"]
    assert owner.outer()() == "host"
    assert owner.Tally().method() == "host"
    assert owner.Tally.static() == "host"
    assert owner.Tally().value == "host"
    assert owner.join.__module__ != "chat_turn_compose_host_probe"
    assert owner.helper.__module__ == "chat_turn_compose_host_probe"
    assert owner.Tally.__module__ == "chat_turn_compose_owner_probe"
    namespace["VALUE"] = "patched"
    assert namespace["helper"]() == "patched"

    fresh = {"__name__": "chat_turn_compose_host_probe", "VALUE": "fresh"}
    chat_turn.compose(fresh, (owner,))
    assert owner.helper() == "fresh"
