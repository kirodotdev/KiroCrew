"""``dashboard/state.py`` keeps its BASE surface while its code lives in ``state_parts``.

``kiro_crew.dashboard.state`` was one module. Its helpers, the ``_ChatSlot`` class and
``DashboardState`` now live in ``kiro_crew.dashboard.state_parts`` (see that package's
docstring), and the
facade imports them back and installs ``kiro_crew._facade_mirror`` so a patch of
``kiro_crew.dashboard.state.<name>`` reaches every owner that reads it. These tests
freeze what the split must not change, measured on the one-module file:

* every module-level name it bound still resolves through the facade, to the object
  the owner holds;
* ``_ChatSlot`` keeps every member with the same kind, coroutine-ness and signature,
  and each method bound from a collaborator IS that collaborator's function, reads as a
  method of the class (``__qualname__``, no ``self`` annotation at run time) and does
  not depend on the class body it left (no zero-argument ``super()``, ``__class__`` or
  name-mangled attribute);
* a fresh slot carries the same default in every field, and its constructor runs the
  collaborators' ``init_<x>_fields`` in the order their fields were first set;
* owners never import the facade, nothing else in ``src`` imports an owner, and a
  global rebound with ``global`` has exactly one holder.

``DashboardState``'s own inventory is frozen in ``test_state_parts_dashboard_state.py``.
Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
import typing
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.dashboard import state as state_module
from kiro_crew.dashboard import state_parts
from kiro_crew.dashboard.state import _ChatSlot

pytestmark = pytest.mark.xdist_group(name="tree_scan_state_parts_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("class_body_check_can_fail",)

_SRC = Path(kiro_crew.__file__).resolve().parent
_PARTS = Path(state_parts.__file__).resolve().parent
_PARTS_PKG = "kiro_crew.dashboard.state_parts"

#: Every module-level name ``dashboard/state.py`` bound before the split, except the
#: stdlib and typing imports below.
_BASE_MODULE_NAMES = frozenset("""
    Any AppRateLimiter ApprovalCoordinator Awaitable BUSY_RECOVERY_PREFIX
    COMPACTION_RECOVERY_PREFIX COMPACT_OUTCOME_CANCELLED COMPACT_OUTCOME_COMPACTED
    COMPACT_OUTCOME_RECYCLED COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE
    COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS CONN_RECOVERY_PREFIX CRON_NOTIFY_END
    CRON_NOTIFY_PREFIX CRON_NOTIFY_RE Callable ChannelLink ChannelSettings CredentialEvidence
    CrewLogPrevious DASHBOARD_PORT DENY_CAUSE_APPROVAL_NO_BUDGET DENY_CAUSE_APPROVAL_TIMEOUT
    DENY_CAUSE_APPROVAL_UNDELIVERABLE DENY_CAUSE_BATCH_CASCADE DENY_CAUSE_HOOK_ERROR
    DENY_CAUSE_INVALID_NAME DENY_CAUSE_POLICY DENY_CAUSE_SURFACE_POLICY
    DashboardPersistenceCoordinator DashboardState EMPTY_QUEUE_SIGNATURE
    EMPTY_RESPONSE_RECOVERY_PREFIX FOLDERS_FILE FileTooLargeError FolderRepository
    HOOK_CONTINUATION_RECOVERY_PREFIX HOOK_HALTED_RECOVERY_PREFIX Iterable Iterator
    KnowledgeStore LoopBoundLock MANUAL_RESUME_RECOVERY_PREFIX MAX_LIVE_SLOTS
    MAX_SLOTS_PER_CREATOR MEMORY_MODES MONITOR_WAKE_PREFIX NATIVE_SUBAGENT_DONE_RESULT_CAP
    NATIVE_SUBAGENT_DONE_TRUNC_MARKER NATIVE_SUBAGENT_OUTPUT_HARD NATIVE_SUBAGENT_OUTPUT_TAIL
    NATIVE_SUBAGENT_TERMINAL_KEEP NATIVE_SUBAGENT_TERMINAL_TTL_SECS NEW_SESSION_TITLE
    NotificationBus NotificationCoordinator NotificationValidationError OPTIONS_RE_LINE
    PERSISTED_SUBAGENT_REPLAY_KEEP PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS
    POSTTOKEN_RECOVERY_PREFIX PROMISE_ONLY_RECOVERY_PREFIX Path QUIET_END_SERVER QUIET_END_TOOL
    QuestionCoordinator REFUSAL_FALLBACK_RECOVERY_PREFIX REFUSAL_INBAND_RECOVERY_PREFIX
    REFUSAL_RECOVERY_PREFIX RecoveryReplays ResourcePressureNotifier SLACK_NAMESPACE
    SLOT_PATCH_WS_FLAG STALE_RECOVERY_PREFIX STEER_NOTICE_BOUND_SECS
    SUBAGENT_BATCH_COMPLETION_PREFIX SUBAGENT_COMPLETION_PREFIX SUBAGENT_COMPLETION_PREFIXES
    SUBAGENT_SYNTHESIS_PREFIX SUBAGENT_SYNTHESIS_PROMPT SideState SlotBufferCoordinator
    SlotOrigin SlotProjection SlotQueueRepository SlotRegistry TOOL_STALL_RECOVERY_PREFIX
    TURN_OPENING_INJECT_KINDS TYPE_CHECKING UNBIND_REASON_DASHBOARD_UNLINK
    UNBIND_REASON_ENTRY_DELETED UNBIND_REASON_ORIGIN_REBIND UNBIND_REASON_PRUNED_STALE
    UNBIND_REASON_SESSION_DESTROYED UNBIND_REASON_UNSPECIFIED UNBIND_REASON_USER_UNLINK
    VALID_MEMORY_MODES WebSocketHub WsPayload _AUTO_COMPACT_CANCELLED_NOTICE
    _AUTO_COMPACT_FAILED_NOTICE _AUTO_COMPACT_NOTICE _AUTO_COMPACT_WAITING_NOTICE
    _AUTO_RECYCLE_NOTICE _AUTO_RESTART_UNCOMPACTABLE_NOTICE _BUNDLE_ID_CACHE _CHANNEL_LABELS
    _CHAT_N_RE _CHIP_STATUS_KEYS _ChatSlot _DEFAULT_PORT _DEFERRED_SLOTS_FLUSH_DELAY_S
    _DENY_CAUSE_TEXT _DurableTagSnapshot _FOLDER_REPOSITORY _INBOUND_UNBIND_NOTICE
    _INBOUND_UNBIND_WHY _INBOUND_UNBIND_WHY_DEFAULT _LAZY_WINDOW_LOCK
    _MAX_DISMISSED_SOURCE_LINKS _MAX_PENDING_CONTEXT _MAX_PENDING_SUBAGENT_DELIVERIES
    _MAX_PERSISTED_NOTIFICATIONS _MAX_SLOT_MESSAGES _MAX_SOURCE_LINKS_PER_SLOT
    _NON_DURABLE_SOURCE_LINK_ROLES _NOTIFICATIONS_FILE _OPTIONS_RE _PROMPT_ROLES
    _PUBLIC_STATUS_DENY_AUDIT _PUBLIC_STATUS_GRANT_AUDIT _PUBLIC_STATUS_GRANT_WINDOW_SECS
    _QUESTION_RETIRING_ROLES _REDIRECT_TARGET_RE _RESTART_MEMORY_TAIL
    _SERIALIZED_SOURCE_LINKS_PER_SLOT _SERVED_INDEX _SESSION_RECYCLED_NOTICE
    _SLOTS_BROADCAST_BYTES_PER_S _SLOTS_BROADCAST_INTERVAL_S _SLOTS_BROADCAST_MAX_INTERVAL_S
    _SLOT_KEY_FILENAME_UNSAFE_RE _SLOT_KEY_HASH_CHARS _SLOT_KEY_KEEP_CHARS _SLOT_KEY_MAX_CHARS
    _SLOT_KEY_TITLE_RE _SLOT_PATCH_FIELDS _SOURCE_ACTIVE_WINDOW_SECS _SSE_INTERVAL_SECS
    _STUCK_TURN_NOTICE _T _TAGS_REVISION_EPOCH _TAGS_REVISION_EPOCH_FILE
    _TAGS_REVISION_EPOCH_UNPERSISTED _TAGS_REVISION_SEQ _TAGS_REVISION_SEQ_LOCK _TRANSIENT_ROLES
    _TURN_CONTINUING_INJECT_KINDS _TURN_INJECT_KINDS _UNSERVABLE_NOTIFICATION_CAP_DIVISOR
    _WIRE_ONLY_ROLES _approvals_for _ascii_slot_key _attach_slot_parents
    _audit_public_status_denied _audit_public_status_grant _budgeted_source_links _build_info
    _cached_check_status _claim_tags_revision_epoch _compaction_keep_record _delivery_key
    _drop_expired_public_status_audits _expected_binding _fmt_duration _fold_line_breaks
    _governance_status _is_genuine_slack_link _is_turn_inject _lineage_failure_warned
    _lineage_seed_in_flight _lineage_seed_lock _link_label _load_notifications
    _log_task_exception _mark_permission_resolved _maybe_trim_notifications _mint_slot_key
    _mirror_link_nonce _new_card_store _new_dashboard_persistence _new_notification_coordinator
    _new_websocket_hub _normalize_slot_key _note_authorized_elsewhere _note_ts_epoch
    _notification_io_executor _notification_io_pool _notification_io_pool_lock
    _notifications_for _notifications_path _on_event_loop _parse_options _permission_marker
    _persist_notification _persistence_for _project_source_links _published_queued_by_slot
    _questions_for _raw_config _read_notification_lines _record_crew_log_class _redact
    _redact_note_value _redacted_link_target _registry_for _release_channel_of_build
    _repo_is_public _request_lineage_seed _rewrite_notifications _safe_folder_tree
    _servable_note _slack_link_nonce _slot_index_from_key _slots_broadcast_clock
    _slots_broadcast_interval_for _slots_serialization_note _slots_ws_frame
    _source_links_by_kind _todo_canonical_text _websocket_for annotations append_and_surface
    asyncio atomic_write binding_token build_infra_retry_prompt build_refusal_recovery_prompt
    build_refusal_steer_notice build_stale_recovery_prompt build_tool_stall_recovery_prompt
    cached_disabled_approval_modes channel_namespace_of chat_message_frame chat_message_note
    config_dir context_entry_expired contextlib datetime deliver_channel_compaction_notice
    display_safe durable_queue_entries durable_queue_view durable_row_count
    ensure_tags_revision_epoch extract_log_redirect_target fsync_dir hashlib
    increment_user_session_count_off_loop is_channel_session_key is_quiet_end_row
    is_stop_event_row is_system_notice is_turn_interrupted json latest_transcript_ts
    load_window_off_loop logger logging mint_row_mid mint_tags_revision mkdirs_owner_only
    monotonic_transcript_ts native_subagent_output_tail normalize_note note_crew_log_class os
    owner_only_opener parse_cls_meta parse_hook_continuations payload_from_legacy
    pinned_session_order queue_persist_signature redact_credentials redact_exfiltration_urls
    remediation_for request_slot_origin resolve_effective_agent row_mid
    safe_read_file_bytes_nolink safety_override seed_default_grants seed_status_identity_rows
    sel set_build_info should_queue_hook_continuation should_queue_refusal_recovery
    split_namespaced_channel_id steer_refusal_notice strip_markdown_preview stuck_turn_notice
    sweep_expired_notifications sys threading time turn_ceiling uuid weakref web
    """.split())
#: BASE imports of stdlib and typing names that nothing reads through the facade (no
#: importer, no patch target), so the facade does not import them.
_DROPPED_UNREAD_IMPORTS = frozenset("""
    Coroutine NamedTuple TypeVar concurrent dataclass math re timezone traceback
    """.split())

#: ``_ChatSlot`` members before the split: name -> (kind, coroutine, async generator,
#: ``inspect.signature``).
_BASE_SLOT_MEMBERS: dict[str, tuple[str, bool, bool, str]] = {
    "__init__": (
        "function",
        False,
        False,
        "(self, key: 'str', title: 'str' = '', agent: 'str' = '', workspace: 'str' = "
        "'default', model: 'str' = '', mode: 'str' = '', memory_mode: 'str' = "
        "'persistent', ephemeral: 'bool' = False) -> 'None'",
    ),
    "_dirty": ("property", False, False, "(self) -> 'bool'"),
    "_has_reader": ("property", False, False, "(self) -> 'bool'"),
    "_note_enqueue": ("function", False, False, "(self) -> 'None'"),
    "_pr_source_links": ("function", False, False, "(self) -> 'list[dict]'"),
    "_retry_deferred_release": ("function", False, False, "(self) -> 'int'"),
    "_stop_state": ("property", False, False, "(self) -> 'str'"),
    "_stopping": ("property", False, False, "(self) -> 'bool'"),
    "_summary_source_links": ("function", False, False, "(self) -> 'list[dict]'"),
    "_todo_override_row_matches": (
        "function",
        False,
        False,
        "(self, task: 'dict[str, Any]', stored: 'str') -> 'bool'",
    ),
    "_todo_text_line": (
        "staticmethod",
        False,
        False,
        "(task: 'dict[str, Any]', *, with_id: 'bool' = False) -> 'str'",
    ),
    "append": (
        "function",
        False,
        False,
        "(self, role: 'str', content: 'str', cls: 'str' = '', ts: 'str' = '', *, "
        "broadcast: 'bool' = True, broadcast_user: 'bool' = False, meta: 'dict | None' = "
        "None, mint_mid: 'bool' = True) -> 'dict[str, Any]'",
    ),
    "append_pending_context": (
        "function",
        False,
        False,
        "(self, entry: 'dict[str, Any]') -> 'bool'",
    ),
    "approval_instance": (
        "function",
        False,
        False,
        "(self, request_id: 'str', message: 'dict | None' = None) -> 'str | " "None'",
    ),
    "begin_close": ("function", False, False, "(self) -> 'None'"),
    "blocks_reads": ("property", False, False, "(self) -> 'bool'"),
    "bump_tags_revision": ("function", False, False, "(self) -> 'str'"),
    "cancel_close": ("function", False, False, "(self) -> 'None'"),
    "clear_mcp_report": ("function", False, False, "(self) -> 'bool'"),
    "clear_todo_recovery_pending": ("function", False, False, "(self) -> 'None'"),
    "deferred_context_count": ("function", False, False, "(self) -> 'int'"),
    "dismiss_source_link": ("function", False, False, "(self, identity_key: 'str') -> 'bool'"),
    "display_title": ("property", False, False, "(self) -> 'str'"),
    "drain": ("function", False, False, "(self) -> 'list[dict[str, str]]'"),
    "drop_foreign_authorized_notes": ("function", False, False, "(self) -> 'int'"),
    "durable_queue_entries": ("function", False, False, "(self) -> 'list[dict[str, Any]]'"),
    "durable_queue_view": (
        "function",
        False,
        False,
        "(self) -> 'tuple[list[dict[str, Any]], int]'",
    ),
    "enqueue_or_run_prompt": (
        "function",
        False,
        False,
        "(self, prompt: 'str', run_chat_coro: 'Callable[[DashboardState, "
        "_ChatSlot, str], Coroutine[Any, Any, None]]', state: "
        "'DashboardState', *, extra_meta: 'dict[str, Any] | None' = None) "
        "-> 'bool'",
    ),
    "flush_deferred_notes": ("function", False, False, "(self) -> 'int'"),
    "forget_session_model_state": ("function", False, False, "(self) -> 'None'"),
    "has_pending_context_seat": ("function", False, False, "(self) -> 'bool'"),
    "invalidate_source_links": ("function", False, False, "(self) -> 'None'"),
    "is_closing": ("property", False, False, "(self) -> 'bool'"),
    "is_restricted": ("property", False, False, "(self) -> 'bool'"),
    "latch_crew_log_previous": (
        "function",
        False,
        False,
        "(self, sid: 'str', *, undecided: 'bool | None' = None, "
        "from_mapping: 'bool' = False) -> 'None'",
    ),
    "linked_turn_running": ("property", False, False, "(self) -> 'bool'"),
    "load_window": ("function", False, False, "(self, *prefetched: 'Any') -> 'None'"),
    "loaded_messages": ("function", False, False, "(self) -> 'list[dict[str, Any]]'"),
    "mark_permission_resolved": (
        "function",
        False,
        False,
        "(self, approval_id: 'str', decision: 'str' = 'approved') -> " "'None'",
    ),
    "mark_todo_edits_stated": (
        "function",
        False,
        False,
        "(self, rendered: 'tuple[tuple[str, str, bool], ...]') -> 'None'",
    ),
    "mark_todo_recovery_pending": ("function", False, False, "(self) -> 'None'"),
    "mcp_report_payload": ("function", False, False, "(self) -> 'dict[str, Any] | None'"),
    "mentions_source_identity": ("function", False, False, "(self, identity_key: 'str') -> 'bool'"),
    "messages": ("property", False, False, "(self) -> 'list[dict[str, Any]]'"),
    "model_withheld": ("property", False, False, "(self) -> 'bool | None'"),
    "note_disk_tail": ("function", False, False, "(self, *candidates: 'str | None') -> 'None'"),
    "note_pending_subagent_delivery": (
        "function",
        False,
        False,
        "(self, content: 'str', deliveries: " "'list[SubagentDelivery]') -> 'None'",
    ),
    "owes_subagent_delivery": ("function", False, False, "(self, contents: 'list[str]') -> 'bool'"),
    "pending_consumer": ("function", False, False, "(self) -> 'Iterator[None]'"),
    "pending_has_consumer": ("property", False, False, "(self) -> 'bool'"),
    "pin_completed_todo_rows": ("function", False, False, "(self) -> 'None'"),
    "purge_chunks": ("function", False, False, "(self) -> 'int'"),
    "push_wire_frame": ("function", False, False, "(self, cls: 'str', content: 'str') -> 'None'"),
    "queue_append": (
        "function",
        False,
        False,
        "(self, content: 'str', kind: 'str' = '', meta: 'dict | None' = None, *, "
        "directive_user_origin: 'bool' = False, directive_channel_origin: 'bool' = "
        "False) -> 'str'",
    ),
    "queue_depth": ("property", False, False, "(self) -> 'int'"),
    "queue_edit_by_id": (
        "function",
        False,
        False,
        "(self, queue_id: 'str', content: 'str', *, directive_user_origin: "
        "'bool' = False, directive_channel_origin: 'bool' = False) -> 'bool'",
    ),
    "queue_insert": (
        "function",
        False,
        False,
        "(self, index: 'int', content: 'str', kind: 'str' = '', payload: 'str' = '', "
        "meta: 'dict | None' = None, on_consumed: 'Callable[[bool], None] | None' = "
        "None, on_irreversibly_consumed: 'Callable[[], Awaitable[None] | None] | "
        "None' = None, directive_user_origin: 'bool' = False, "
        "directive_channel_origin: 'bool' = False) -> 'str'",
    ),
    "queue_persist_pending": ("property", False, False, "(self) -> 'bool'"),
    "queue_pop": ("function", False, False, "(self, index: 'int' = 0) -> 'dict[str, Any]'"),
    "queue_promote_by_id": ("function", False, False, "(self, queue_id: 'str') -> 'bool'"),
    "queue_remove_by_id": ("function", False, False, "(self, queue_id: 'str') -> 'str | None'"),
    "reasoning_effort": ("property", False, False, "(self) -> 'str'"),
    "record_model_withheld": (
        "function",
        False,
        False,
        "(self, withheld: 'bool | None') -> 'None'",
    ),
    "record_served_model": ("function", False, False, "(self, model_id: 'str | None') -> 'None'"),
    "register_approval": (
        "function",
        False,
        False,
        "(self, request_id: 'str', future: 'asyncio.Future[str]', "
        "permission_row: 'dict') -> 'None'",
    ),
    "release_pending_chunks": ("function", False, False, "(self) -> 'int'"),
    "running": ("property", False, False, "(self) -> 'bool'"),
    "set_mcp_report": (
        "function",
        False,
        False,
        "(self, report: 'dict[str, Any] | None', session_id: 'str' = '') -> " "'bool'",
    ),
    "set_todo": ("function", False, False, "(self, todo: 'dict[str, Any] | None') -> 'bool'"),
    "set_todo_task_completed": (
        "function",
        False,
        False,
        "(self, task_id: 'str', completed: 'bool') -> 'bool'",
    ),
    "sidebar_messages": ("function", False, False, "(self) -> 'list[dict[str, Any]]'"),
    "source_links_payload": (
        "function",
        False,
        False,
        "(self, *, include_check_status: 'bool' = False, dashboard_user: "
        "'bool' = False) -> 'dict'",
    ),
    "source_links_view": (
        "function",
        False,
        False,
        "(self, *, include_check_status: 'bool' = False, dashboard_user: 'bool' "
        "= False) -> 'list[dict]'",
    ),
    "take_crew_log_previous": (
        "function",
        False,
        False,
        "(self, *, now_writing: 'str', replay_pending: 'bool' = False) -> " "\"'CrewLogPrevious'\"",
    ),
    "take_pending_subagent_deliveries": (
        "function",
        False,
        False,
        "(self, contents: 'list[str]') -> " "'list[SubagentDelivery]'",
    ),
    "task": ("property", False, False, "(self) -> 'asyncio.Task[Any] | None'"),
    "to_dict": (
        "function",
        False,
        False,
        "(self, *, include_check_status: 'bool' = False, dashboard_user: 'bool' = False) "
        "-> 'dict'",
    ),
    "todo_payload": ("function", False, False, "(self) -> 'dict[str, Any] | None'"),
    "todo_recovery_pending": ("property", False, False, "(self) -> 'bool'"),
    "todo_recovery_prompt": ("function", False, False, "(self) -> 'str'"),
    "todo_sync_prompt": ("function", False, False, "(self) -> 'str'"),
    "todo_sync_rendered": (
        "property",
        False,
        False,
        "(self) -> 'tuple[tuple[str, str, bool], ...]'",
    ),
    "todo_task_text": ("function", False, False, "(self, task_id: 'str') -> 'str | None'"),
    "turn_running": ("property", False, False, "(self) -> 'bool'"),
    "unattended": ("property", False, False, "(self) -> 'bool'"),
    "unregister_approval": (
        "function",
        False,
        False,
        "(self, request_id: 'str', future: 'asyncio.Future[str]') -> 'bool'",
    ),
    "update_message": (
        "function",
        False,
        False,
        "(self, ts: 'str', *, content: 'str | None' = None, meta: 'dict | None' = "
        "None, mid: 'str | None' = None) -> 'dict | None'",
    ),
    "window_pending": ("property", False, False, "(self) -> 'bool'"),
}

#: A fresh ``_ChatSlot("k")`` before the split: field -> ("value", repr) for a plain
#: value, or ("type", qualified type name) for an object, a clock or a minted id.
_BASE_SLOT_DEFAULTS: dict[str, tuple[str, str | None]] = {
    "_acp_client": ("value", "None"),
    "_acp_pipe_death_retries": ("value", "0"),
    "_active_fallback_model": ("value", "''"),
    "_active_turn_session_key": ("value", "''"),
    "_app": ("value", "''"),
    "_approval_futures": ("value", "{}"),
    "_approval_instances": ("value", "{}"),
    "_approval_stopped": ("value", "set()"),
    "_artifact": ("value", "''"),
    "_auto_tagged": ("value", "False"),
    "_batch_rejected": ("value", "False"),
    "_batch_rejected_cause": ("value", "''"),
    "_buffers": ("type", "kiro_crew.dashboard.slot_buffers.SlotBufferCoordinator"),
    "_carried_ttft_clock": ("value", "None"),
    "_channel_folder_filed": ("value", "False"),
    "_channel_runtime_origin": ("value", "False"),
    "_channel_window_mtime": ("value", "0.0"),
    "_chunk_seq": ("value", "0"),
    "_closing": ("value", "0"),
    "_compacting": ("value", "False"),
    "_preparing_count": ("value", "0"),
    "_compaction_continue_retries": ("value", "0"),
    "_compaction_fail_cooldown_until": ("value", "0.0"),
    "_compaction_fail_streak": ("value", "0"),
    "_compaction_failed_retries": ("value", "0"),
    "_coordinator_approvals": ("value", "None"),
    "_created_by": ("value", "''"),
    "_created_by_sid": ("value", "''"),
    "_crew_log_opened_sid": ("value", "''"),
    "_crew_log_pending_channel": ("value", "False"),
    "_crew_log_previous_from_mapping": ("value", "False"),
    "_crew_log_previous_sid": ("value", "''"),
    "_crew_log_previous_undecided": ("value", "None"),
    "_cycle_reached_provider": ("value", "False"),
    "_dashboard_card_exempt": ("value", "False"),
    "_dashboard_card_identity": ("type", "builtins.str"),
    "_deferred_model_switch_key": ("value", "''"),
    "_deferred_notes": ("value", "[]"),
    "_detail_render_lock": ("type", "asyncio.locks.Lock"),
    "_dirty_flag": ("value", "False"),
    "_dirty_gen": ("value", "0"),
    "_disk_meta_created_at": ("value", "''"),
    "_disk_meta_observed": ("value", "False"),
    "_disk_older_count": ("value", "0"),
    "_disk_older_durable_count": ("value", "0"),
    "_disk_tail_ts": ("value", "None"),
    "_disk_window_len": ("value", "0"),
    "_dismissed_hydrated": ("value", "True"),
    "_dismissed_source_links": ("value", "set()"),
    "_dismissed_txn_depth": ("value", "0"),
    "_dismissed_txn_pending": ("value", "set()"),
    "_dropped_note_ids": ("value", "set()"),
    "_eager_spawn_failures": ("value", "0"),
    "_eager_spawn_retry_at": ("value", "0.0"),
    "_eager_spawn_task": ("value", "None"),
    "_effort_pick_gen": ("value", "0"),
    "_empty_episode_productive": ("value", "False"),
    "_empty_response_retries": ("value", "0"),
    "_end_wait_by": ("value", "''"),
    "_end_wait_request": ("value", "None"),
    "_ephemeral": ("value", "False"),
    "_fallback_candidate_idx": ("value", "0"),
    "_fallback_client_pick_epoch": ("value", "0"),
    "_fallback_pick_gen": ("value", "0"),
    "_fallback_primary_model": ("value", "''"),
    "_fallback_slot_model": ("value", "''"),
    "_fallback_walked": ("value", "[]"),
    "_file_changes": ("value", "[]"),
    "_file_changes_flush": ("value", "None"),
    "_folder_changed": ("value", "False"),
    "_folder_suggested": ("value", "False"),
    "_foreign_reported": ("value", "frozenset()"),
    "_fork_lock": ("type", "asyncio.locks.Lock"),
    "_frozen_prefix_cache": ("value", "None"),
    "_git_repo_touch_overflow": ("value", "False"),
    "_git_repo_touch_pending": ("value", "{}"),
    "_git_repo_touch_terminal": ("value", "{}"),
    "_git_repo_walk_pending": ("value", "[]"),
    "_git_repo_walk_task": ("value", "None"),
    "_git_repos": ("value", "[]"),
    "_git_repos_evicted": ("value", "[]"),
    "_git_repos_evicted_overflow": ("value", "0"),
    "_git_repos_incomplete": ("value", "False"),
    "_git_repos_seeded": ("value", "False"),
    "_guarded_history_writes": ("value", "set()"),
    "_has_reader_flag": ("value", "False"),
    "_hook_continuation_depth": ("value", "0"),
    "_human_seen": ("value", "False"),
    "_infra_retries": ("value", "0"),
    "_last_enqueue_ts": ("value", "''"),
    "_last_stop_reason": ("value", "''"),
    "_last_turn_auth_required": ("value", "False"),
    "_last_turn_awaiting_permission": ("value", "False"),
    "_last_turn_children_announced": ("value", "False"),
    "_last_turn_model_refused_loop_gen": ("value", "0"),
    "_last_turn_model_refused_loop_id": ("value", "''"),
    "_last_turn_structural_terminal": ("value", "False"),
    "_last_turn_structural_terminal_loop_gen": ("value", "0"),
    "_last_turn_structural_terminal_loop_id": ("value", "''"),
    "_lazy_tail": ("value", "None"),
    "_lazy_window": ("value", "None"),
    "_lineage_minted": ("value", "False"),
    "_linked_session_busy": ("value", "None"),
    "_reports_to_creator": ("value", "False"),
    "_lock": ("type", "asyncio.locks.Lock"),
    "_mcp_report": ("value", "None"),
    "_mcp_report_session_id": ("value", "''"),
    "_member_approval_seeded": ("value", "False"),
    "_memory_assignment_from_history": ("value", "False"),
    "_messages": ("value", "[]"),
    "_metadata_persist_inflight": ("value", "0"),
    "_model_access_fallback_used": ("value", "False"),
    "_model_pick_gen": ("value", "0"),
    "_model_pick_lock": ("type", "asyncio.locks.Lock"),
    "_model_runtime_pick_gen": ("value", "None"),
    "_model_withheld": ("value", "False"),
    "_model_withheld_for": ("value", "''"),
    "_native_subagent_output": ("value", "{}"),
    "_native_subagent_tracker": ("value", "{}"),
    "_on_card_event": ("value", "None"),
    "_on_message": ("value", "None"),
    "_on_question_retired": ("value", "None"),
    "_on_row": ("value", "None"),
    "_origin": ("value", "''"),
    "_owed_segment_cut": ("value", "None"),
    "_pending": ("value", "[]"),
    "_pending_consumers": ("value", "0"),
    "_pending_context": ("value", "[]"),
    "_pending_discard_conversation_key": ("value", "None"),
    "_pending_discard_from_wake": ("value", "False"),
    "_pending_memory_mode": ("value", "None"),
    "_pending_model_pick": ("value", "None"),
    "_pending_release_deferred": ("value", "False"),
    "_pending_reset_history_key": ("value", "None"),
    "_pending_rewrite": ("value", "False"),
    "_pending_steers": ("value", "[]"),
    "_pending_subagent_failures": ("value", "[]"),
    "_pending_synthesis": ("value", "False"),
    "_pending_variants": ("value", "[]"),
    "_poisoned_reset_used": ("value", "False"),
    "_posttoken_retry_used": ("value", "False"),
    "_prefetch_ttl_task": ("value", "None"),
    "_prestream_exhausted_cycles": ("value", "0"),
    "_projection": ("type", "kiro_crew.dashboard.slot_projection.SlotProjection"),
    "_promise_only_retries": ("value", "0"),
    "_prompt_busy_retries": ("value", "0"),
    "_question_pending": ("value", "{}"),
    "_queue": ("value", "[]"),
    "_queue_persist_inflight": ("value", "False"),
    "_queue_persist_owed": ("value", "False"),
    "_queue_persisted_sig": ("str", None),
    "_queue_repository": ("type", "kiro_crew.dashboard.slot_queue_repository.SlotQueueRepository"),
    "_reasoning_effort": ("value", "''"),
    "_refusal_client_pick_epoch": ("value", "0"),
    "_refusal_fallback_attempted": ("value", "False"),
    "_refusal_fallback_candidate": ("value", "''"),
    "_refusal_fallback_primary": ("value", "''"),
    "_refusal_fallback_session_key": ("value", "''"),
    "_refusal_pick_gen": ("value", "0"),
    "_regenerate_target": ("value", "None"),
    "_responsive": ("value", "None"),
    "_resumed_count": ("value", "0"),
    "_revived_by": ("value", "''"),
    "_saved_created_by": ("value", "''"),
    "_session_not_found_retry_used": ("value", "False"),
    "_session_requested_model": ("value", "None"),
    "_side": ("value", "None"),
    "_slack_channel": ("value", "''"),
    "_slack_linked": ("value", "False"),
    "_slack_thread_ts": ("value", "''"),
    "_source_links_cache": ("value", "None"),
    "_source_links_revision": ("value", "0"),
    "_stale_recovery_exhausted_emitted": ("value", "False"),
    "_stale_recovery_retries": ("value", "0"),
    "_steer_act_now": ("value", "set()"),
    "_steer_admissions": ("value", "{}"),
    "_steer_attachment_meta": ("value", "{}"),
    "_steer_audience_fence_holders": ("value", "{}"),
    "_steer_audience_fences": ("value", "{}"),
    "_steer_automation": ("value", "{}"),
    "_steer_channel_origin": ("value", "{}"),
    "_steer_clear": ("value", "None"),
    "_steer_confirmed": ("value", "set()"),
    "_steer_decision_strips": ("value", "{}"),
    "_steer_delivery_ids": ("value", "{}"),
    "_steer_possibly_delivered": ("value", "set()"),
    "_steer_retract": ("value", "None"),
    "_steer_rpc_in_flight": ("value", "set()"),
    "_native_steer_in_flight": ("value", "set()"),
    "_steer_segment_cut": ("value", "None"),
    "_steer_send_ids": ("value", "{}"),
    "_steer_user_origin": ("value", "{}"),
    "_stop_declined_at": ("value", "0.0"),
    "_stop_escalated_card_id": ("value", "None"),
    "_stop_event_id": ("value", "None"),
    "_stop_generation": ("value", "0"),
    "_stop_state_raw": ("value", "'idle'"),
    "_subagent_deliveries_inflight": ("value", "0"),
    "_subagent_delivery_pending": ("value", "{}"),
    "_subagents_inline_collected": ("value", "set()"),
    "_summary_in_flight": ("value", "False"),
    "_summary_turn_mark": ("value", "0"),
    "_swap_in_flight": ("value", "False"),
    "_synthesis_armers": ("value", "set()"),
    "_synthesis_completion_turns": ("value", "0"),
    "_synthesis_inflight": ("value", "False"),
    "_synthesis_recheck": ("value", "None"),
    "_synthesis_rechecks": ("value", "0"),
    "_tab_id": ("value", "''"),
    "_task": ("value", "None"),
    "_title_epoch": ("value", "0"),
    "_title_in_flight": ("value", "False"),
    "_title_low_signal": ("value", "False"),
    "_title_origin": ("value", "''"),
    "_title_refresh_mark": ("value", "0"),
    "_title_retry_pending": ("value", "False"),
    "_title_task": ("value", "None"),
    "_titled": ("value", "False"),
    "_todo": ("value", "None"),
    "_todo_overrides": ("value", "{}"),
    "_todo_rebuild_expected": ("value", "False"),
    "_todo_recovery_pending": ("value", "False"),
    "_todo_sync_rendered": ("value", "()"),
    "_tool_stall_exhausted_emitted": ("value", "False"),
    "_tool_stall_retries": ("value", "0"),
    "_transient_5xx_retries": ("value", "0"),
    "_transient_ladder_started": ("value", "0.0"),
    "_trust": ("value", "False"),
    "_trust_card_scoped": ("value", "False"),
    "_trust_reads": ("value", "False"),
    "_posture_inherited": ("value", "False"),
    "_trust_scope": ("value", "''"),
    "_trusted_pattern_deadlines": ("value", "{}"),
    "_trusted_patterns": ("value", "set()"),
    "_turn_admission_reserved": ("value", "False"),
    "_turn_channel_narrowed": ("value", "False"),
    "_turn_channel_origin": ("value", "False"),
    "_turn_generation": ("value", "0"),
    "_turn_in_flight_generation": ("value", "0"),
    "_turn_in_flight_prompt": ("value", "None"),
    "_turn_reply_mids": ("value", "[]"),
    "_turn_retries": ("value", "{}"),
    "_turn_timeout_hydrated": ("value", "True"),
    "_turn_user_sent": ("value", "False"),
    "_unknown_row_fields_bytes": ("value", "0"),
    "_unstarted_generation": ("value", "-1"),
    "_wait_contested": ("value", "False"),
    "_wait_last_ping": ("value", "0.0"),
    "_wait_state": ("value", "None"),
    "_wait_steer_baseline": ("value", "0.0"),
    "_wake_starting_task": ("value", "None"),
    "_welcomed_agent": ("value", "''"),
    "_window_loader_thread": ("value", "None"),
    "_window_swap_applying": ("value", "None"),
    "_window_swap_epoch": ("value", "0"),
    "agent": ("value", "''"),
    "agent_kind": ("value", "''"),
    "crew_mode_agent": ("value", "''"),
    "app_agent_owner": ("value", "''"),
    "autocompact_pct": ("value", "None"),
    "channel_origin": ("value", "False"),
    "color_hex": ("value", "None"),
    "color_index": ("value", "None"),
    "color_theme": ("value", "''"),
    "created_at": ("type", "builtins.str"),
    "credential_evidence": ("type", "kiro_crew.security.credential_sources.CredentialEvidence"),
    "event": ("type", "asyncio.locks.Event"),
    "executor": ("value", "'local'"),
    "folder_id": ("value", "''"),
    "forked_from": ("value", "None"),
    "instance_id": ("value", "''"),
    "jev_route": ("value", "False"),
    "key": ("value", "'k'"),
    "linked_session_key": ("value", "''"),
    "memory_mode": ("value", "'persistent'"),
    "memory_store": ("value", "''"),
    "mode": ("value", "''"),
    "model": ("value", "''"),
    "muted": ("value", "False"),
    "mutes_opened": ("value", "False"),
    "pinned": ("value", "False"),
    "project": ("value", "''"),
    "remote_slot": ("value", "''"),
    "replays": ("type", "kiro_crew.dashboard.recovery_replays.RecoveryReplays"),
    "segment_message_id": ("value", "''"),
    "segment_raw_text": ("value", "''"),
    "segment_removed": ("value", "{}"),
    "segment_shown_sha": ("type", "_hashlib.HASH"),
    "served_model": ("value", "''"),
    "tags": ("value", "[]"),
    "tags_revision": ("type", "builtins.str"),
    "theme_consent": ("value", "False"),
    "theme_consent_sha": ("value", "None"),
    "title": ("value", "'k'"),
    "total_messages": ("value", "0"),
    "turn_timeout_secs": ("value", "None"),
    "workspace": ("value", "'default'"),
}

#: The collaborators whose ``init_<x>_fields`` the constructor calls, in the order their
#: first field was set before the split.
_INIT_ORDER = (
    "slot_session",
    "slot_turns",
    "slot_transcript",
    "source_links",
    "slot_sidebar",
    "slot_todo",
    "slot_ladder",
)

#: Globals a function rebinds with ``global``; each must have exactly one holder.
_SINGLE_HOLDER = {
    "_TAGS_REVISION_EPOCH": "slot_sidebar",
    "_TAGS_REVISION_SEQ": "slot_sidebar",
    "_notification_io_pool": "notification_log",
    "_lineage_seed_in_flight": "slots_frame",
    "_lineage_failure_warned": "slots_frame",
    "_build_info": "dashboard_state",
}


def _owner_modules() -> dict[str, object]:
    return {
        info.name: importlib.import_module(f"{_PARTS_PKG}.{info.name}")
        for info in pkgutil.iter_modules([str(_PARTS)])
    }


def _kind(cls: type, name: str) -> tuple[str, object]:
    raw = inspect.getattr_static(cls, name)
    if isinstance(raw, property):
        return "property", raw.fget
    if isinstance(raw, staticmethod):
        return "staticmethod", raw.__func__
    if isinstance(raw, classmethod):
        return "classmethod", raw.__func__
    if inspect.isfunction(raw):
        return "function", raw
    return "attribute", None


def _member_table(cls: type) -> dict[str, tuple[str, bool, bool, str]]:
    out = {}
    skip = {"__dict__", "__weakref__", "__module__", "__doc__", "__slots__", "__annotations__"}
    for name in vars(cls):
        if name in skip:
            continue
        kind, fn = _kind(cls, name)
        if fn is None:
            continue
        out[name] = (
            kind,
            inspect.iscoroutinefunction(fn),
            inspect.isasyncgenfunction(fn),
            str(inspect.signature(fn)),
        )
    return out


def _bound_functions(cls: type) -> dict[str, object]:
    """Class members whose function lives in a collaborator, not in the class body."""
    shell = cls.__module__
    out = {}
    for name in vars(cls):
        kind, fn = _kind(cls, name)
        if fn is not None and fn.__module__ != shell:
            out[name] = fn
    return out


def _default_of(value: object) -> tuple[str, str | None]:
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 40:
            return ("str", None)
        return ("value", repr(value))
    if isinstance(value, (list, dict, set, frozenset, tuple)) and not value:
        return ("value", repr(value))
    return ("type", type(value).__module__ + "." + type(value).__qualname__)


def _class_body_dependencies(source: str) -> list[str]:
    """What a function's source needs from the class body it was defined in."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "super" and not node.args:
                found.append("super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            found.append("__class__")
        elif isinstance(node, ast.Attribute) and re.fullmatch(r"__\w+(?<!__)", node.attr):
            found.append(f"mangled {node.attr}")
    return found


def _owner_imports(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == _PARTS_PKG or node.module.startswith(_PARTS_PKG + "."):
                found.append(node.module)
            elif node.module == "kiro_crew.dashboard" and "state_parts" in {
                a.name for a in node.names
            }:
                found.append("kiro_crew.dashboard.state_parts")
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.startswith(_PARTS_PKG)]
    return found


# -- module surface -------------------------------------------------------------------


def test_every_base_module_name_still_resolves_through_the_facade() -> None:
    assert sorted(n for n in _BASE_MODULE_NAMES if not hasattr(state_module, n)) == []


def test_the_unread_stdlib_imports_are_not_bound_on_the_facade() -> None:
    assert sorted(n for n in _DROPPED_UNREAD_IMPORTS if n in vars(state_module)) == []


def _defined_at_top(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_a_moved_name_is_the_object_its_owner_holds() -> None:
    checked = 0
    for owner in _owner_modules().values():
        defined = _defined_at_top(Path(owner.__file__).read_text(encoding="utf-8"))
        for name in sorted(_BASE_MODULE_NAMES & defined):
            checked += 1
            assert getattr(state_module, name) is vars(owner)[name], name
    assert checked >= 100, "the inventory check matched almost nothing -- name drift"


def test_the_facade_forwards_no_name_and_reads_sys_modules_first() -> None:
    """The one-module file was a plain module: no forwarding table, no import per read."""
    index = vars(state_module)["__facade_mirror__"]
    assert not index.forwards
    assert index.forward_reads == "sys_modules_first"


def test_a_rebound_global_has_exactly_one_holder_and_is_read_live() -> None:
    index = vars(state_module)["__facade_mirror__"]
    for name, owner in _SINGLE_HOLDER.items():
        assert name not in vars(state_module), f"the facade must not bind {name}"
        assert index.holders[name] == (f"{_PARTS_PKG}.{owner}",), name
        holder = importlib.import_module(f"{_PARTS_PKG}.{owner}")
        assert getattr(state_module, name) is vars(holder)[name], name


# -- the _ChatSlot class -------------------------------------------------------------


def test_the_slot_keeps_every_base_member_with_its_kind_and_signature() -> None:
    assert _member_table(_ChatSlot) == _BASE_SLOT_MEMBERS


def test_the_inventory_check_sees_a_changed_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    def drain(self, extra: int = 0):  # pragma: no cover - never called
        return []

    monkeypatch.setattr(_ChatSlot, "drain", drain)
    assert _member_table(_ChatSlot)["drain"] != _BASE_SLOT_MEMBERS["drain"]


def test_each_bound_member_is_its_collaborators_function() -> None:
    bound = _bound_functions(_ChatSlot)
    assert len(bound) > 60, "almost nothing is bound -- the shell drifted"
    owners = {m.__name__: m for m in _owner_modules().values()}
    for name, fn in bound.items():
        owner = owners[fn.__module__]
        assert vars(owner)[name] is fn or getattr(vars(owner)[name], "__wrapped__", None), name
        target = vars(owner)[name]
        assert inspect.unwrap(target) is inspect.unwrap(fn), name


def test_a_bound_member_reads_as_a_method_of_the_class() -> None:
    for name, fn in _bound_functions(_ChatSlot).items():
        assert fn.__qualname__ == f"_ChatSlot.{name}", name
        assert "self" not in fn.__annotations__, name


#: Members whose hints ``typing.get_type_hints`` could not resolve in the one-module
#: file either: they name ``SubagentDelivery``, imported only under ``TYPE_CHECKING``.
_HINTS_UNRESOLVED_AT_BASE = frozenset(
    {"note_pending_subagent_delivery", "take_pending_subagent_deliveries"}
)
#: Members whose hints stop resolving because of the split: ``enqueue_or_run_prompt``
#: names ``DashboardState``, which the shell cannot import (an owner never imports
#: its facade). The class itself now declares its fields in the class body, and
#: those annotations name ``TYPE_CHECKING``-only classes such as ``GrantDeadline``.
_HINTS_UNRESOLVED_BY_THE_SPLIT = frozenset({"enqueue_or_run_prompt"})


def test_type_hints_resolve_as_before_except_the_named_members() -> None:
    unresolved = set()
    for name, member in vars(_ChatSlot).items():
        fn = member.fget if isinstance(member, property) else getattr(member, "__func__", member)
        if not inspect.isfunction(fn):
            continue
        try:
            typing.get_type_hints(fn)
        except NameError:
            unresolved.add(name)
    assert unresolved == _HINTS_UNRESOLVED_AT_BASE | _HINTS_UNRESOLVED_BY_THE_SPLIT
    with pytest.raises(NameError):
        typing.get_type_hints(_ChatSlot)


def test_no_bound_member_depends_on_the_class_body_it_left() -> None:
    offenders = {
        name: deps
        for name, fn in _bound_functions(_ChatSlot).items()
        if (deps := _class_body_dependencies(inspect.getsource(inspect.unwrap(fn))))
    }
    assert offenders == {}


def test_a_fresh_slot_has_every_base_default() -> None:
    slot = _ChatSlot("k")
    got = {name: _default_of(getattr(slot, name)) for name in _ChatSlot.__slots__}
    for name in ("created_at", "tags_revision", "_dashboard_card_identity"):
        got[name] = ("type", "builtins.str")
    assert got == _BASE_SLOT_DEFAULTS


def test_the_constructor_runs_the_field_groups_in_base_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    for stem in _INIT_ORDER:
        owner = importlib.import_module(f"{_PARTS_PKG}.{stem}")
        name = next(n for n in vars(owner) if n.startswith("init_") and n.endswith("_fields"))
        original = vars(owner)[name]

        def record(self, _stem=stem, _original=original):
            calls.append(_stem)
            _original(self)

        monkeypatch.setattr(owner, name, record)
    _ChatSlot("k")
    assert tuple(calls) == _INIT_ORDER


# -- source pins ---------------------------------------------------------------------


def test_the_slot_held_options_field_stays_gone_from_every_owner() -> None:
    """``test_slack_options_lifecycle`` reads ``inspect.getsource(state)`` for this; the
    same absence is asserted over the facade joined with every owner, which holds the
    code the one-module file held."""
    offenders = [
        path.name
        for path in (Path(state_module.__file__), *sorted(_PARTS.glob("*.py")))
        if "_slack_options_posted" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_origin_derivation_pins_hold_over_the_facade_and_every_owner() -> None:
    """``test_ws_event_scoping`` reads ``state.py``'s text for these two facts; the same
    halves are asserted over the facade joined with every owner, so a copy of the
    derivation cannot come back in a file that pin does not read."""
    joined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path(state_module.__file__), *sorted(_PARTS.glob("*.py")))
    )
    assert "slot._origin = origin or (SlotOrigin.APP if app else SlotOrigin.USER)" not in joined
    assert joined.count("SlotOrigin.APP if app else SlotOrigin.USER") == 1


def test_the_served_index_is_the_path_the_one_module_file_resolved() -> None:
    """``_SERVED_INDEX`` is ``__file__``-relative; its owner is one directory deeper."""
    want = Path(state_module.__file__).resolve().parent.parent / "static" / "dist" / "index.html"
    assert state_module._SERVED_INDEX == want


# -- import direction ----------------------------------------------------------------
# That no owner imports the facade is the shared facade-mirror guards' check.


def test_only_the_facade_imports_an_owner() -> None:
    allowed = {_SRC / "dashboard" / "state.py"} | set(_PARTS.glob("*.py"))
    offenders = {
        str(path.relative_to(_SRC)): found
        for path in sorted(_SRC.rglob("*.py"))
        if path not in allowed
        and "state_parts" in (text := path.read_text(encoding="utf-8", errors="replace"))
        and (found := _owner_imports(text))
    }
    assert offenders == {}


def test_the_owner_import_check_can_fail() -> None:
    source = "from kiro_crew.dashboard.state_parts.slot_todo import set_todo\n"
    assert _owner_imports(source) == ["kiro_crew.dashboard.state_parts.slot_todo"]
