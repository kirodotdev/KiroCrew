"""Each name that moved out of the dashboard chat runner keeps its base shape and its owner.

``_BASE_SURFACE`` records, for every module-level name a ``chat_turn`` owner holds, the
owner and the kind and signature the name had in the one-module runner. The table and
the tests that read it live here, beside the composition contract
(``test_chat_runner_composition_contract.py``), which owns the owner set and the
helpers these tests share.
"""

from __future__ import annotations

import inspect
import pkgutil
import re

import pytest
from test_chat_runner_composition_contract import (
    _OWNER_DIR,
    _OWNER_MODULES,
    _owner,
    _owner_functions,
)

import kiro_crew.dashboard.chat_runner as cr

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
            "_classify_str_replace_before",
            "function",
            "(path: 'str', raw_params: 'dict') -> 'tuple[str | None, str | None]'",
        ),
        (
            "_line_change_input",
            "function",
            "(fc: 'dict[str, Any]', after: '_Snapshot | None') -> 'dict[str, str] | None'",
        ),
        ("_note_reply_row", "function", "(slot: \"'_ChatSlot'\", row: 'dict[str, Any]') -> 'None'"),
        (
            "_pending_str_replace_payload",
            "function",
            "(content: 'str', old_str: 'str', new_str: 'str') -> 'dict[str, _Snapshot]'",
        ),
        (
            "_record_turn_snapshot",
            "function",
            "(slot: \"'_ChatSlot'\", snapshot: 'dict[str, Any]', writer_key: 'str' = '') -> 'None'",
        ),
        (
            "_resolve_pending_str_replace",
            "function",
            "(pending: 'dict[str, Any]', after: 'str') -> '_Snapshot | None'",
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
        ("_WRITE_COMMANDS", "value", "frozenset"),
        ("_MAX_SNAPSHOT", "value", "int"),
        ("_SNAPSHOT_TRUNCATION_MARKER", "value", "str"),
        ("_MAX_RECONSTRUCT_BYTES", "value", "int"),
        ("_EXCERPT_CONTEXT_LINES", "value", "int"),
        ("_MAX_TURN_SNAPSHOT_CHARS", "value", "int"),
        ("_MAX_SNAPSHOT_PATH_CHARS", "value", "int"),
        ("_MAX_TURN_SNAPSHOT_ENTRIES", "value", "int"),
        ("_SNAPSHOT_READ_BYTES", "value", "int"),
        (
            "_cap_redacted",
            "function",
            "(text: 'str', limit: 'int', marker: 'str') -> 'tuple[str, bool]'",
        ),
        (
            "_turn_after_reads",
            "function",
            "(changes: 'list[dict[str, Any]]', turn_had_shell: 'bool') -> 'dict[str, tuple[_Snapshot | None, tuple[str, str] | None]]'",
        ),
        (
            "_flush_file_changes",
            "function",
            "(slot: \"'_ChatSlot'\", turn_boundary: 'int' = 0, turn_start_mid: 'str | None' = None, turn_had_shell: 'bool' = False, after_reads: 'dict[str, tuple[_Snapshot | None, tuple[str, str] | None]] | None' = None) -> 'None'",
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
        (
            "_emit_mcp_oauth_request",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", server_name: 'str', oauth_url: 'str', card_owned: 'bool' = False, minted_by: 'str' = '') -> 'None'",
        ),
        (
            "_supersede_open_mcp_oauth_banners",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", safe_name: 'str', ambiguous: 'bool') -> 'None'",
        ),
        ("_session_mcp_report", "function", "(provider: 'Any') -> \"'SessionMcpReport | None'\""),
        (
            "_mark_mcp_oauth_completed",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", server_name: 'str', success: 'bool', error: 'str' = '') -> 'None'",
        ),
        ("_redact_acp_string", "function", "(s: 'str') -> 'str'"),
    ),
    "model_fallback": (
        ("_agent_fallback_chain", "function", "() -> 'tuple[str, ...]'"),
        ("_clear_fallback_sticky_state", "function", "(slot: 'Any', client: 'Any') -> 'None'"),
        ("_configured_refusal_fallback", "function", "() -> 'str'"),
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
        ("_is_bedrock_profile_id", "function", "(model: 'str') -> 'bool'"),
        ("_backfill_canonical_model", "function", "(client: 'Any', provider: 'str') -> 'str'"),
        (
            "_pinned_model_verdict",
            "function",
            "(client: 'Any', model: 'str', provider: 'str') -> 'bool | None'",
        ),
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
        (
            "_deliver_linked_slack_message",
            "async function",
            "(state: 'Any', slot: 'Any', sessions: 'Any', session_key: 'str', message: 'str') -> 'None'",
        ),
        (
            "_deliver_cross_surface_reply",
            "async function",
            "(state: 'Any', session_key: 'str', assistant_text: 'str', *, slot: 'Any' = None) -> 'None'",
        ),
        (
            "_deliver_cross_surface_user_message",
            "async function",
            "(state: 'Any', session_key: 'str', user_message: 'str') -> 'None'",
        ),
        ("_prepare_mirror_msg", "function", "(raw_user_message: 'str') -> 'str'"),
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
        ("_REFUSAL_CARD_LEAD", "value", "str"),
        (
            "refusal_card_text",
            "function",
            "(refusal: \"'RefusalInfo | None'\", *, streamed_text: 'str' = '', fallback_off: 'bool' = False) -> 'str'",
        ),
        ("COMPACT_AFTER_TURN_NOTICE", "value", "str"),
        ("COMPACT_AFTER_TURN_NOTICE_KIND", "value", "str"),
        ("_tool_rows_settled", "function", "(slot: 'Any') -> 'bool'"),
        (
            "_notice_compaction_after_turn",
            "async function",
            "(state: 'DashboardState', slot: 'Any', client: 'Any', session_key: 'str') -> 'bool'",
        ),
        ("_COMPACTION_FAILED_RETRIES", "value", "int"),
        ("_COMPACT_FAIL_REASON_MAX_CHARS", "value", "int"),
        ("COMPACT_REPLAY_PENDING_NOTICE", "value", "str"),
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
        ("unavailable_agent_message", "function", "(agent_name: 'str') -> 'str'"),
        ("_slot_member_slug", "function", "(slot_key: 'str') -> 'str'"),
        (
            "_dm_member_binding_changed",
            "function",
            "(bound_member_id: 'str', slot_key: 'str') -> 'bool'",
        ),
        (
            "_require_session_memory_assignment",
            "function",
            "(session_key: 'str', memory_store: 'str | None') -> 'None'",
        ),
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
            "(slot: \"'_ChatSlot'\", elapsed_ms: 'int', credits: 'float', cost_usd: 'float', turn_boundary: 'int' = 0, model: 'str' = '', ttft_ms: 'int' = 0, turn_start_mid: 'str | None' = None, retries: 'int' = 0) -> 'bool'",
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
        (
            "_turn_outcome",
            "function",
            "(stop_reason: 'str | None', *, exhausted: 'bool' = False) -> 'str'",
        ),
        (
            "_emit_turn_metric",
            "function",
            "(duration_ms: 'int | float | None', stop_reason: 'str | None', slot_key: 'str', *, elapsed_ms: 'int | float | None' = None, exhausted: 'bool' = False, usage: 'object' = None, model: 'str' = '', provider: 'str' = '', harness: 'str' = 'unknown', trigger: 'str' = 'other') -> 'None'",
        ),
        (
            "_emit_recovery_outcome",
            "function",
            "(mechanism: 'str', outcome: 'str', attempts: 'int') -> 'None'",
        ),
        (
            "turn_stats_meta",
            "function",
            "(elapsed_ms: 'int', credits: 'float', cost_usd: 'float', model: 'str' = '') -> 'dict[str, Any] | None'",
        ),
        ("_MAX_MODEL_ID_LEN", "value", "int"),
        (
            "_emit_ttft_metric",
            "function",
            "(t0: 'float', session_key: 'str', *, is_new: 'bool', resumed: 'bool') -> 'None'",
        ),
        ("_AbnormalTurnEvent", "class", ""),
        (
            "_persist_abnormal_turn_usage",
            "async function",
            "(slot: \"'_ChatSlot'\", client: 'object', session_key: 'str', *, elapsed_ms: 'int', stop_reason: 'str', since: 'object' = <object object>, usage: 'TurnUsage | None' = None) -> 'bool'",
        ),
    ),
    "turn_failures": (
        ("_POISON_CANARY_PROMPT", "value", "str"),
        ("_POISON_CANARY_TIMEOUT_SECS", "value", "float"),
    ),
    "crew_log_attribution": (
        (
            "_slot_predecessor_store",
            "async function",
            "(sessions: 'Any', slot: 'Any', session_key: 'str') -> 'CrewLogPrevious'",
        ),
        ("_crew_log_model", "function", "(slot: 'Any', fallback: 'str' = '') -> 'str'"),
        ("_crew_log_lineage", "function", "(slot: 'Any') -> 'tuple[str, str]'"),
        ("_crew_log_seed_tree", "async function", "(slot: 'Any') -> 'None'"),
        ("_crew_log_inherited_parent", "function", "(slot: 'Any', head_sid: 'str') -> 'str'"),
        ("PENDING_CHANNEL_ATTR", "value", "str"),
        ("_crew_log_workspace", "function", "(slot: 'Any') -> 'str'"),
        ("_crew_log_class", "function", "(state: 'Any', slot: 'Any') -> 'tuple[str, str, bool]'"),
    ),
    "deny_chokepoints": (
        ("_redact_display_text", "function", "(text: 'str') -> 'str'"),
        (
            "_redacted_hook_block",
            "function",
            "(event: 'Any', pre_hook_results: 'Any') -> 'tuple[str, str]'",
        ),
        ("_STEER_NOTICE_BOUND_SECS", "value", "float"),
        ("_steer_repeat_loop_notice", "async function", "(client: 'Any', notice: 'str') -> 'bool'"),
        (
            "_steer_policy_notice",
            "async function",
            "(client: 'Any', title: 'str', reason: 'str', notices: 'list[str]', slot: 'Any' = None, state: 'Any' = None, *, cause: 'str' = 'policy') -> 'bool'",
        ),
        (
            "_reject_attributed",
            "async function",
            "(client: 'Any', request_id: 'Any', *, attribution: 'str') -> 'None'",
        ),
        (
            "_reject_hook_blocked",
            "async function",
            "(client: 'Any', slot: 'Any', event: 'Any', *, session_key: 'str', pre_hook_results: 'Any', refusal_reasons: 'list[tuple[str, str]]', refusal_notices: 'list[str] | None', metadata: 'dict | None' = None) -> 'None'",
        ),
        (
            "_reject_invalid_tool",
            "async function",
            "(client: 'Any', slot: 'Any', event: 'Any', *, session_key: 'str', error: 'Exception', refusal_reasons: 'list[tuple[str, str]]', refusal_notices: 'list[str] | None', state: 'Any' = None, metadata: 'dict | None' = None) -> 'None'",
        ),
        (
            "_reject_hook_error",
            "async function",
            "(client: 'Any', slot: 'Any', event: 'Any', *, session_key: 'str', error: 'str', refusal_reasons: 'list[tuple[str, str]]', refusal_notices: 'list[str] | None', state: 'Any' = None, metadata: 'dict | None' = None) -> 'None'",
        ),
    ),
    "native_cards": (
        ("_MAX_NATIVE_CARD_ERROR", "value", "int"),
        ("_RE_TRAILING_REQUEST_ID", "value", "Pattern"),
        ("_clip_card_error", "function", "(text: 'str', limit: 'int' = 200) -> 'str'"),
        (
            "_post_native_question_card",
            "async function",
            "(state: 'Any', slot_key: 'str', tool_input: 'str') -> 'bool'",
        ),
        ("_NATIVE_SUBAGENT_STALE_SECS", "value", "float"),
        ("_native_done_result", "function", "(chunks: \"'list[str] | None'\") -> 'str'"),
        (
            "_append_native_output",
            "function",
            "(buf: 'list[str]', text: 'str', total: 'int', cap: 'int' = 40000, hard: 'int' = 80000) -> 'int'",
        ),
        ("_native_card_feed", "function", "(card_output, card_id: 'str') -> 'str'"),
        (
            "_native_subagent_sync",
            "function",
            "(state, slot, subagents, tracker, card_output=None) -> 'None'",
        ),
        (
            "_register_native_card",
            "function",
            "(state, card_id: 'str', slot_key: 'str', session_id: 'str') -> 'None'",
        ),
        ("_unregister_native_card", "function", "(state, card_id: 'str') -> 'None'"),
        (
            "_native_subagent_close_all",
            "function",
            "(state, slot, tracker, card_output=None, error: 'str | None' = None) -> 'None'",
        ),
        (
            "_retain_terminal_native",
            "function",
            "(tracker: \"'dict[str, dict]'\", keep: 'int' = 50, ttl_secs: 'float' = 3600.0, now: \"'float | None'\" = None) -> \"'dict[str, dict]'\"",
        ),
        ("_safe_native_crew_debug_title", "function", "(title: 'str') -> 'str'"),
    ),
    "tool_rows": (
        ("_persist_tool_result_rows", "async function", "(state: 'Any', slot: 'Any') -> 'Any'"),
        (
            "_refined_tool_row_content",
            "function",
            "(existing: 'str', new_title: 'str') -> 'str | None'",
        ),
        ("_MAX_TCID_SOURCES", "value", "int"),
        ("_MAX_TCID_LEN", "value", "int"),
        ("_tcid_identity_key", "function", "(tcid: 'str | None') -> 'str'"),
        ("_tool_meta", "function", "(event: \"'LLMEvent'\") -> 'dict[str, str] | None'"),
        ("_tool_call_ws_payload", "function", "(event: \"'LLMEvent'\") -> 'dict[str, str | bool]'"),
    ),
    "turn_spec_hooks": (
        (
            "_surface_agent_welcome",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", agent: 'str') -> 'None'",
        ),
        (
            "_prepare_spec_hooks",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", client: 'Any', agent: 'str', *, is_new: 'bool') -> 'tuple[list, bool, str | None]'",
        ),
        (
            "_notice_spec_hooks_run_nowhere",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", client: 'Any', agent: 'str') -> 'None'",
        ),
        (
            "_surface_withheld_project_hooks",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", client: 'Any', agent: 'str', *, user_hooks_run: 'bool') -> 'None'",
        ),
    ),
    "account_identity": (
        (
            "_retire_sessions_on_identity_change",
            "async function",
            "(state: 'Any', *, unattended: 'bool' = False) -> 'None'",
        ),
        ("_mark_kiro_signed_out", "function", "(state: 'Any') -> 'None'"),
    ),
    "decisions_hooks": (
        ("_discard_stale_decision", "function", "(slot: '_ChatSlot') -> 'None'"),
        ("_decisions_strip_meta", "function", "(slot: '_ChatSlot') -> 'dict | None'"),
        (
            "_route_history_source",
            "function",
            "(state: 'DashboardState', session_key: 'str') -> \"'Callable[[], list[dict]]'\"",
        ),
        ("_JEV_ROUTE_AUTO_MODELS", "value", "tuple"),
        ("_jev_route_armed", "function", "(slot: 'Any') -> 'bool'"),
        ("_jev_preview_on", "function", "(session_key: 'str') -> 'bool'"),
        ("_JEV_ROUTE_BASELINE_ATTR", "value", "str"),
        ("_JEV_ROUTE_CAPABILITY_STOPS", "value", "frozenset"),
        (
            "_jev_route_turn_ok",
            "function",
            "(terminal: \"'dict[str, Any] | None'\") -> 'bool | None'",
        ),
        ("_settle_jev_route", "function", "(session_key: 'str', ok: 'bool | None') -> 'None'"),
        ("_active_fallback", "function", "(slot: 'Any', client: 'Any') -> 'tuple[bool, str]'"),
        (
            "_jev_route_baseline",
            "function",
            "(slot: 'Any', client: 'Any') -> 'tuple[str, str, int]'",
        ),
        (
            "_jev_route_premise_moved",
            "function",
            "(slot: 'Any', client: 'Any', *, live_model: 'str', epoch: 'int') -> 'bool'",
        ),
        (
            "_jev_current_window",
            "function",
            "(client: 'Any', model_route: 'Any', live_model: 'str') -> 'int | None'",
        ),
        (
            "_jev_downgrade_refused",
            "function",
            "(state: 'DashboardState', client: 'Any', session_key: 'str', model_route: 'Any', *, current_window: 'int | None', target: 'str', p: 'Any', prompt: 'str', rollback_target: 'str', target_window: 'int | None' = None) -> 'bool'",
        ),
        (
            "_route_model_for_turn",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', client: 'Any', message: 'str', session_key: 'str', *, prompt: 'str', retried: 'bool' = False) -> 'None'",
        ),
        (
            "_session_auto_approves",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot') -> 'bool'",
        ),
        (
            "_tool_risk_meta",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', event: \"'LLMEvent'\", *, session_key: 'str', message: 'str', calls_this_turn: 'int') -> 'dict | None'",
        ),
    ),
    "eager_spawn": (
        ("_EAGER_SPAWN_DEBOUNCE_SECS", "value", "float"),
        ("_EAGER_SPAWN_FAILURE_CAP", "value", "int"),
        ("_EAGER_SPAWN_BACKOFF_BASE_SECS", "value", "float"),
        ("_EAGER_SPAWN_BACKOFF_MAX_SECS", "value", "float"),
        ("_EAGER_SPAWN_ERROR_DETAIL_MAX_CHARS", "value", "int"),
        ("_eager_spawn_backoff_secs", "function", "(failures: 'int') -> 'float'"),
        ("_eager_spawn_held_off", "function", "(slot: \"'_ChatSlot'\") -> 'bool'"),
        (
            "_note_eager_spawn_failure",
            "function",
            "(slot: \"'_ChatSlot'\", exc: 'BaseException') -> 'None'",
        ),
        ("_PRE_SPAWN_CAPABILITY_CODES", "value", "frozenset"),
        ("_is_pre_spawn_refusal", "function", "(exc: 'BaseException') -> 'bool'"),
        ("_clear_eager_spawn_failures", "function", "(slot: \"'_ChatSlot'\") -> 'None'"),
        ("_RESUME_PREFETCH_TTL_SECS", "value", "float"),
        ("_RESUME_PREFETCH_MAX_LIVE", "value", "int"),
        ("_RESERVED", "value", "object"),
        ("_entry_arm_generation", "function", "(value: 'object') -> 'float'"),
        ("_prewarm_allowance", "function", "() -> 'int'"),
        ("_pressure_hold_blocks_prewarm", "function", "(state: \"'DashboardState'\") -> 'bool'"),
        (
            "_evict_prefetches_beyond",
            "async function",
            "(sessions: 'Any', limit: 'int', *, keep: 'str | None' = None, up_to_generation: 'float | None' = None) -> 'bool'",
        ),
        (
            "_admit_prefetch",
            "async function",
            "(sessions: 'Any', new_key: 'str', allowance: 'int', *, signal_generation: 'float | None' = None) -> 'bool'",
        ),
        ("_release_prefetch_reservation", "function", "(new_key: 'str') -> 'None'"),
        (
            "_cap_armed_prefetches",
            "async function",
            "(sessions: 'Any', new_key: 'str', cap: 'int' = 3) -> 'None'",
        ),
        (
            "schedule_eager_spawn",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", *, allow_resume: 'bool' = False, start_priority: 'StartPriority' = <StartPriority.BACKGROUND: 'bg'>) -> \"'asyncio.Task | None'\"",
        ),
        (
            "_slot_binding",
            "function",
            "(slot: \"'_ChatSlot'\") -> 'tuple[str, str, str, str, str, str]'",
        ),
        (
            "_eager_spawn",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", *, allow_resume: 'bool' = False, signal_generation: 'float | None' = None, start_priority: 'StartPriority' = <StartPriority.BACKGROUND: 'bg'>) -> 'None'",
        ),
        (
            "_spawn_admitted_prefetch",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", sessions: 'Any', session_key: 'str', _t0: 'float', *, kiro_agent: 'str | None', crew_alias: 'str', agent_model: 'str', allow_resume: 'bool', _bound: 'tuple', start_priority: 'StartPriority') -> 'None'",
        ),
        (
            "_schedule_prefetch_ttl",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", session_key: 'str') -> 'None'",
        ),
        (
            "_prefetch_ttl",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", session_key: 'str') -> 'None'",
        ),
        (
            "stop_eager_spawns",
            "function",
            "(state: \"'DashboardState'\") -> \"'list[asyncio.Task[None]]'\"",
        ),
    ),
    "prompt_assembly": (
        ("_strip_yaml_frontmatter", "function", "(content: 'str') -> 'str'"),
        (
            "_resolve_prompt_mention",
            "function",
            "(message: 'str', project_dir: 'Path | None') -> 'tuple[str, str, str]'",
        ),
        (
            "_slot_prompt_project",
            "function",
            "(slot: '_ChatSlot', project: 'str | None' = None) -> 'Path | None'",
        ),
        (
            "_surface_prompt_chip",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot', chip: 'str') -> 'None'",
        ),
        (
            "_expand_prompt_mention",
            "function",
            "(message: 'str', state: 'DashboardState', slot: '_ChatSlot') -> 'tuple[str, str]'",
        ),
        (
            "_expand_dollar_skills",
            "function",
            "(message: 'str', state: 'DashboardState', slot: '_ChatSlot', session_key: 'str', project: 'str') -> 'tuple[str, int]'",
        ),
        (
            "_expand_prompt_mention_off_loop",
            "async function",
            "(message: 'str', state: 'DashboardState', slot: '_ChatSlot', *, project: 'str | None' = None) -> 'tuple[str, str]'",
        ),
        (
            "_prewarm_startup_query",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", text: 'str', *, memory_store: 'str | None', execution_context: 'Any', prefetch_memory: 'bool' = False) -> 'None'",
        ),
        (
            "_prefetch_session_memory_off_loop",
            "async function",
            "(builder: 'Any', text: 'str', *, workspace: 'str | None', memory_store: 'str | None') -> 'None'",
        ),
        (
            "_embed_startup_query_off_loop",
            "async function",
            "(builder: 'Any', text: 'str', *, workspace: 'str | None', memory_store: 'str | None') -> 'None'",
        ),
    ),
    "segment_flush": (
        ("_DIRECTIVE_SHAPED_RE", "value", "Pattern"),
        ("_reflow_label_and_audit", "function", "(slot: 'Any', text: 'str') -> 'str'"),
        ("_log_glued_footer_text", "function", "(slot: 'Any', glued: 'list[str]') -> 'None'"),
        (
            "_segment_row_meta",
            "function",
            # links_ordered and text: keyword-only and defaulted, so every old caller keeps its shape.
            "(slot: '_ChatSlot', blocked_links: 'list[dict]', redactions: 'list[dict] | None' = None,"
            " *, links_ordered: 'bool' = False, text: 'str | None' = None) -> 'dict | None'",
        ),
        ("_SEGMENT_RAW_MAX_CHARS", "value", "int"),
        (
            "_accumulate_segment_raw",
            "function",
            "(slot: '_ChatSlot', text: 'str', shown: 'str', removed: 'Iterable[tuple[str, str]]') -> 'None'",
        ),
        (
            "_redact_segment",
            "function",
            # _ordered_out: keyword-only and defaulted, so every old caller keeps its shape.
            "(slot: '_ChatSlot', text: 'str', *, _ordered_out: 'list[bool] | None' = None)"
            " -> 'tuple[str, list[dict], list[dict]]'",
        ),
        (
            "_redact_segment_text",
            "function",
            # judged_sri_values: defaulted, so every old caller keeps its shape.
            # _ordered_out: keyword-only and defaulted, likewise.
            "(slot: '_ChatSlot', text: 'str', judged_sri_values: 'Collection[str]' = (),"
            " *, _ordered_out: 'list[bool] | None' = None) -> 'tuple[str, list[dict], list[dict]]'",
        ),
        ("_append_command_reply", "function", "(slot: '_ChatSlot', text: 'str') -> 'None'"),
        ("_TOOL_GROUP_ROLES", "value", "frozenset"),
        (
            "_flush_segment",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot', assistant_text: 'str', *, broadcast: 'bool' = True, quiet_persist: 'bool' = False, interrupted: 'bool' = False) -> 'dict[str, Any]'",
        ),
        (
            "_schedule_widget_registration",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot', text: 'str', message_ts: 'str') -> 'None'",
        ),
        ("_cleared_segment_text", "function", "(slot: '_ChatSlot') -> 'str'"),
    ),
    "session_reset": (
        ("_PENDING_RESET_RETRY_DELAY_SECS", "value", "float"),
        ("_PENDING_RESET_RETRY_WARN_EVERY", "value", "int"),
        (
            "_arm_pending_reset_retry",
            "function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\") -> 'None'",
        ),
        (
            "_consume_pending_reset",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', *, allow_discard: 'bool' = False) -> 'bool'",
        ),
    ),
    "slash_commands": (
        (
            "_slash_command_backend",
            "async function",
            "(state: 'DashboardState', session_key: 'str') -> 'str'",
        ),
        (
            "_answer_slash_without_channel",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', session_key: 'str', command: 'str', "
            "message: 'str' = '') -> 'str'",
        ),
        (
            "_handle_workflow_command",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", message: 'str', session_key: 'str') -> 'None'",
        ),
        (
            "_handle_agent_command",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", agent: 'str', session_key: 'str') -> 'None'",
        ),
        (
            "_handle_goal_command",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", message: 'str') -> 'None'",
        ),
    ),
    "app_agent_binding": (
        ("app_agent_binding_applies", "function", "(slot: \"'_ChatSlot'\") -> 'bool'"),
        (
            "_retire_app_agent_session",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\", session_key: 'str') -> 'None'",
        ),
        (
            "_recover_app_agent_binding",
            "async function",
            "(cfg: \"'KiroCrewConfig'\", slot: \"'_ChatSlot'\", *, project: 'str | None') -> 'ResolvedBindings'",
        ),
        ("_app_agent_host_allowed", "function", "(provider: 'Any') -> 'bool'"),
        (
            "_app_agent_turn_backend",
            "async function",
            "(state: \"'DashboardState'\", session_key: 'str', cfg: \"'KiroCrewConfig | None'\") -> 'str | None'",
        ),
    ),
    "queue_cycle": (
        (
            "_run_pending_synthesis",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot') -> 'None'",
        ),
        ("_SYNTHESIS_RECHECK_SECS", "value", "float"),
        ("_SYNTHESIS_RECHECK_MAX", "value", "int"),
        ("_launch_synthesis", "function", "(state: 'DashboardState', slot: '_ChatSlot') -> 'None'"),
        ("_drop_single_turn_synthesis", "function", "(slot: '_ChatSlot') -> 'bool'"),
        (
            "_arm_synthesis_recheck",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot') -> 'None'",
        ),
        (
            "_finish_queue_cycle",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', *, allow_automatic_successor: 'bool' = True, drain: 'bool' = True) -> 'None'",
        ),
        (
            "_send_chat_done",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', *, continuing: 'bool' = False, ends_turn: 'bool' = True, payload: 'dict[str, Any] | None' = None) -> 'None'",
        ),
        (
            "_hand_off_queue",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', *, drain: 'bool', allow_automatic_successor: 'bool', predecessor_actor: 'str' = '', predecessor_turn_id: 'str' = '') -> 'None'",
        ),
        (
            "_notify_autonudge_turn_end",
            "function",
            "(slot: '_ChatSlot', *, tool_calls: 'int' = 0, tool_identities: 'tuple' = (), reply_text: 'str' = '', reply_flushed: 'bool' = False, nudge_turn: 'bool' = False) -> 'None'",
        ),
        ("_memory_preparation_settled", "function", "(state: 'DashboardState') -> 'bool'"),
        ("_TurnOutcome", "class", ""),
        ("_TurnExit", "class", ""),
        (
            "_end_turn_tail",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', turn_exit: '_TurnExit', *, setup_failed: 'bool' = False) -> 'None'",
        ),
        ("TURN_FAILED_META", "value", "str"),
        (
            "_note_failed_before_the_try",
            "function",
            "(slot: '_ChatSlot', turn_exit: '_TurnExit') -> 'None'",
        ),
    ),
    "queue_drain": (
        (
            "_arm_queued_delivery_settlement",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot', task: \"'asyncio.Task'\", contents: 'list[str]', consumed: 'list[bool]') -> 'None'",
        ),
        (
            "settle_drained_subagent_notices",
            "function",
            "(state: 'Any', slot: 'Any', notices: 'list[str]') -> 'None'",
        ),
        ("TURN_ACTOR_META_KEY", "value", "str"),
        ("MEMORY_PREPARATION_PARKED_TEXT", "value", "str"),
        ("_memory_preparation_pending", "function", "(state: 'DashboardState') -> 'bool'"),
        (
            "_arm_parked_queue_drain",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot', *, notice: 'bool') -> 'None'",
        ),
        ("_parked_queue_drain_admitted", "function", "() -> 'bool'"),
        (
            "_drain_parked_queues",
            "async function",
            "(state: 'DashboardState', slot_keys: 'list[str]') -> 'None'",
        ),
        (
            "drain_idle_parent_queue_for_stall",
            "async function",
            "(state: 'DashboardState', parent_session_key: 'str') -> 'None'",
        ),
        ("_mark_turn_end", "function", "(slot: '_ChatSlot') -> 'None'"),
        ("_RECOVERY_TURN_META_KEY", "value", "str"),
        (
            "_is_own_recovery",
            "function",
            "(items: \"'list[dict]'\", predecessor_actor: 'str', predecessor_turn_id: 'str') -> 'bool'",
        ),
        ("_REPLAYS_COMPLETION_KEY", "value", "str"),
        (
            "idle_slot_admits_drain",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot') -> 'bool'",
        ),
        (
            "subagents_hold_user_messages",
            "function",
            "(state: 'DashboardState', session_key: 'str') -> 'bool'",
        ),
        (
            "_start_next_queued_turn",
            "async function",
            "(state: 'DashboardState', slot: '_ChatSlot', *, allow_user_during_subagents: 'bool' = False, required_queue_id: 'str | None' = None, predecessor_actor: 'str' = '', predecessor_turn_id: 'str' = '', end_batch: 'bool' = False) -> 'bool'",
        ),
    ),
    "turn_entry": (
        (
            "_turn_start_priority",
            "function",
            "(*, user_origin: 'bool', crew_log_actor: 'str', provenance_restored: 'bool') -> 'StartPriority'",
        ),
    ),
    "turn_reply_seams": (
        ("_turn_reply_segments", "function", "(turn: \"'_ChatTurn'\") -> 'list[str]'"),
        ("_turn_full_reply_text", "function", "(turn: \"'_ChatTurn'\") -> 'tuple[str, str]'"),
    ),
}


#: A default object's address in its repr: lowercase on POSIX, zero-padded uppercase on Windows.
_ADDRESS = re.compile(r" at 0x[0-9A-Fa-f]+>")


def _without_address(shown: str) -> str:
    """*shown* with a default object's address dropped: it differs per process."""
    return _ADDRESS.sub(">", shown)


def test_the_owner_set_is_the_package() -> None:
    assert {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])} == _OWNER_MODULES
    assert set(_BASE_SURFACE) <= _OWNER_MODULES


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
        # A sentinel default shows its address, which differs per process.
        shown = str(inspect.signature(obj))  # type: ignore[arg-type]
        assert _without_address(shown) == signature
    assert getattr(_owner(owner), name) is obj


def test_a_default_objects_address_is_dropped_in_either_spelling() -> None:
    """The check above reads a signature the same way on POSIX and on Windows."""
    for address in ("0x7f8d7451b2d0", "0x000001A9BFA701B0"):
        shown = f"(since: object = <object object at {address}>)"
        assert _without_address(shown) == "(since: object = <object object>)"


def test_the_owners_define_functions_for_the_sweeps_to_check() -> None:
    """Every sweep below proves nothing unless it has functions to sweep."""
    labels = {label for label, _ in _owner_functions()}
    callables = sum(
        kind.endswith("function") for rows in _BASE_SURFACE.values() for _, kind, _ in rows
    )
    assert len(labels) >= callables
    assert {"recipient._resolve_channel_target", "steer_queue._drop_stale_admissions"} <= labels
