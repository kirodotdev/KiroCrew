"""The composition contract of ``kiro_crew.dashboard.chat_persistence``.

The dashboard slot save and restore are split between the facade, which keeps the
orchestration and the process-wide state, and the owners in
``kiro_crew.dashboard.slot_persistence``. This file pins what that split must not
change:

* the surface: every name the facade bound before the split still resolves on it,
  imported names are their home module's objects, a moved name is its owner's
  object, and every function keeps its signature;
* the composition: the facade loads every owner when it loads, last, and the
  owners import only what the facade already imports;
* the seams: no owner binds or bare-reads a name a test rebinds on the facade, so
  such a patch reaches the moved code (derived from the test corpus, with a scan
  that is proven able to see each spelling);
* the bytes: a full save, an empty-window merge, a foreign-append merge, a
  rows-only hand-over, a truncating rewrite and the refusals write exactly what
  they wrote before the split.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
from chat_test_helpers import _make_state
from source_corpus import repo_files_named, repo_root

import kiro_crew
from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.subprocess_utf8 import UTF8_TEXT

FACADE = "kiro_crew.dashboard.chat_persistence"
FACADE_PATH = Path(cp.__file__).resolve()
OWNER_PACKAGE = "kiro_crew.dashboard.slot_persistence"
OWNER_DIR = FACADE_PATH.with_name("slot_persistence")

#: The owners the facade composes. Adding or removing one changes the
#: composition, so the set is spelled out rather than globbed.
OWNER_MODULES = frozenset(
    {
        "message_entries",
        "metadata_codec",
        "metadata_line",
        "restore_inputs",
        "transcript_merge",
        "turn_marker",
        "write_guards",
    }
)

#: Every name ``chat_persistence`` DEFINED at the base this split was cut from.
_FACADE_DEFINED = frozenset(
    {
        "COLOR_HEX_RE",
        "MAX_EFFORT_LEVELS_PER_CAPABILITY",
        "MAX_RETAINED_REASONING_EFFORT_VALUES",
        "_COERCED_REQUEST_MODES",
        "_ENTRY_MAX_CACHEABLE_BYTES",
        "_FLUSH_SNAPSHOT_RETRIES",
        "_IDENTITY_UNRESOLVED",
        "_LOCAL_TURN_PROMPT_MAX_ATTACHMENTS",
        "_LOCAL_TURN_PROMPT_MAX_BYTES",
        "_LOCAL_TURN_PROMPT_MAX_FIELD_CHARS",
        "_LOCAL_TURN_PROMPT_META_KEYS",
        "_LOCAL_TURN_PROMPT_ROLES",
        "_MAX_HISTORY_CHARS",
        "_META_LAST_USER_AT",
        "_PENDING_MEMORY_MODE_LOCK",
        "_REASONING_EFFORT_FALLBACK",
        "_REASONING_EFFORT_FALLBACK_ORDER",
        "_REASONING_EFFORT_VALUES",
        "_RESTART_INTERRUPTION_KIND",
        "_RESTART_INTERRUPTION_MSG",
        "_RETIRED_MODES",
        "_SAFE_EFFORT_RE",
        "_SKIP_MEMBER_RESTORE",
        "_VALIDATED_EFFORT_DIR",
        "_apply_recent_session",
        "_apply_restored_open_slot",
        "_approx_window_payload_bytes",
        "_archive_dropped_lines",
        "_attach_variants",
        "_build_history_prefix",
        "_build_kiro_model_map",
        "_build_message_entry",
        "_build_message_entry_uncached",
        "_capped_dismissed_line",
        "_coerce_requested_mode",
        "_deletion_during_read",
        "_diff_dropped_message_lines",
        "_effort_marker_path",
        "_entry_cache",
        "_entry_cache_bounds",
        "_entry_cache_bounds_cached",
        "_entry_cache_bounds_read_warned",
        "_entry_cache_bytes",
        "_entry_cache_config_sub",
        "_entry_cache_lock",
        "_foreign_tail_ts",
        "_frozen_prefix_and_foreign_appends",
        "_has_validated_effort_marker",
        "_interleave_foreign_lines",
        "_is_app_owned_channel_row",
        "_keep_owed_after_refusal",
        "_latest_stamp",
        "_latest_turn_was_deliberately_stopped",
        "_line_is_this_slots",
        "_load_restore_cfg",
        "_local_turn_generation",
        "_local_turn_prompt",
        "_member_private_selection",
        "_member_restore_identity",
        "_newest_human_turn_ts",
        "_on_config_change",
        "_pin_private_agent_assignment",
        "_prefetch_recent_session",
        "_prefetch_rehydrate_inputs",
        "_queue_snapshot_is_stale",
        "_read_mcp_app_claims",
        "_read_open_slots_keys",
        "_reasoning_effort_marked",
        "_reasoning_effort_ordered",
        "_reasoning_effort_values",
        "_rebase_rehydrated_refresh_mark",
        "_recent_session_slot_name",
        "_reconcile_local_turn_marker",
        "_reconcile_mcp_app_claims",
        "_record_pending_memory_mode",
        "_recover_mcp_app_claims",
        "_recover_mcp_app_claims_async",
        "_rehydrate_slot_from_history",
        "_rehydrate_slot_title",
        "_rehydrate_title_low_signal",
        "_rehydrate_title_origin",
        "_rehydrate_title_refresh_mark",
        "_remember_reasoning_effort_for_restore",
        "_restore_dismissed_source_links",
        "_restore_open_slots_steps",
        "_restore_recent_sessions_steps",
        "_restored_agent_name",
        "_restored_mode",
        "_retain_reasoning_effort_values",
        "_sanitize_open_slot_key",
        "_save_slot_to_history",
        "_stable_durable_queue",
        "_tighten_carried_execution",
        "_validate_autocompact_pct",
        "_validate_reasoning_effort",
        "_window_holds_row",
        "cap_effort_capability_levels",
        "get_reasoning_effort_ordered",
        "get_reasoning_effort_values",
        "local_turn_prompt_within_bounds",
        "logger",
        "member_store_ownership_holds",
        "pending_slot_memory_mode",
        "pin_private_agent_store",
        "register_guarded_history_write",
        "register_reasoning_effort_values",
        "rehydrate_slot_from_history_async",
        "release_prewarmed_session",
        "restore_open_slots",
        "restore_open_slots_async",
        "restore_recent_sessions",
        "restore_recent_sessions_async",
        "save_all_slots_to_history",
        "save_slot_off_loop",
        "session_transcript_remains",
        "session_was_deleted",
        "update_reasoning_effort_values",
        "watch_config",
    }
)

#: Every name it IMPORTED there, with the module and attribute it came from.
_FACADE_IMPORTS: dict[str, tuple[str, str | None]] = {
    "ARTIFACT_SLUG_RE": ("kiro_crew.validation", "ARTIFACT_SLUG_RE"),
    "ATTACHMENT_LIST_MAX_ITEMS": ("kiro_crew.dashboard.chat_delivery", "ATTACHMENT_LIST_MAX_ITEMS"),
    "ATTACHMENT_PATH_MAX_LEN": ("kiro_crew.dashboard.chat_delivery", "ATTACHMENT_PATH_MAX_LEN"),
    "AUTOCOMPACT_PCT_MAX": ("kiro_crew.config.loader", "AUTOCOMPACT_PCT_MAX"),
    "AUTOCOMPACT_PCT_MIN": ("kiro_crew.config.loader", "AUTOCOMPACT_PCT_MIN"),
    "Any": ("typing", "Any"),
    "CHAT_ENTRY_CACHE_BYTES_DEFAULT": ("kiro_crew.config.loader", "CHAT_ENTRY_CACHE_BYTES_DEFAULT"),
    "CHAT_ENTRY_CACHE_ENTRIES_DEFAULT": (
        "kiro_crew.config.loader",
        "CHAT_ENTRY_CACHE_ENTRIES_DEFAULT",
    ),
    "ConversationLog": ("kiro_crew.history", "ConversationLog"),
    "DashboardState": ("kiro_crew.dashboard.state", "DashboardState"),
    "EFFORT_LEVELS": ("kiro_crew.effort", "EFFORT_LEVELS"),
    "EFFORT_VALUES": ("kiro_crew.effort", "EFFORT_VALUES"),
    "EXECUTION_CONTEXT_KEY": ("kiro_crew.execution_context", "EXECUTION_CONTEXT_KEY"),
    "HUMAN_TURN_META_KEY": ("kiro_crew.history", "HUMAN_TURN_META_KEY"),
    "ImageBudget": ("kiro_crew.chat_attachments", "ImageBudget"),
    "Iterable": ("collections.abc", "Iterable"),
    "Iterator": ("collections.abc", "Iterator"),
    "KiroCrewConfig": ("kiro_crew.config.loader", "KiroCrewConfig"),
    "MEMORY_MODES": ("kiro_crew.execution_context", "MEMORY_MODES"),
    "METADATA_LINE_CORRUPT": ("kiro_crew.history", "METADATA_LINE_CORRUPT"),
    "Mapping": ("collections.abc", "Mapping"),
    "OrderedDict": ("collections", "OrderedDict"),
    "Path": ("pathlib", "Path"),
    "ROWS_ONLY_DEFERRED_META_KEYS": ("kiro_crew.history", "ROWS_ONLY_DEFERRED_META_KEYS"),
    "ROWS_ONLY_OWNED_META_KEYS": ("kiro_crew.history", "ROWS_ONLY_OWNED_META_KEYS"),
    "SLOT_OWNED_META_KEYS": ("kiro_crew.history", "SLOT_OWNED_META_KEYS"),
    "STRICTEST_MEMORY_MODE": ("kiro_crew.execution_context", "STRICTEST_MEMORY_MODE"),
    "UnknownMemoryStore": ("kiro_crew.memory_stores", "UnknownMemoryStore"),
    "_ChatSlot": ("kiro_crew.dashboard.state", "_ChatSlot"),
    "_MAX_DISMISSED_SOURCE_LINKS": ("kiro_crew.dashboard.state", "_MAX_DISMISSED_SOURCE_LINKS"),
    "_TITLE_ORIGINS": ("kiro_crew.dashboard.chat_title", "_TITLE_ORIGINS"),
    "_TRANSIENT_ROLES": ("kiro_crew.dashboard.state", "_TRANSIENT_ROLES"),
    "_archive_lines": ("kiro_crew.history", "_archive_lines"),
    "_normalize_model": ("kiro_crew.dashboard.chat_utils", "_normalize_model"),
    "_normalize_slot_key": ("kiro_crew.dashboard.state", "_normalize_slot_key"),
    "_note_authorized_elsewhere": ("kiro_crew.dashboard.state", "_note_authorized_elsewhere"),
    "_redact_meta_for_role": ("kiro_crew.dashboard.chat_utils", "_redact_meta_for_role"),
    "_rehydrated_refresh_mark": ("kiro_crew.dashboard.chat_title", "_rehydrated_refresh_mark"),
    "_sync_dashboard_slots": ("kiro_crew.dashboard.chat_utils", "_sync_dashboard_slots"),
    "agent_model_map": ("kiro_crew.agent_discovery", "agent_model_map"),
    "annotations": ("__future__", "annotations"),
    "apply_pending_slot_memory_mode": (
        "kiro_crew.dashboard.chat_utils",
        "apply_pending_slot_memory_mode",
    ),
    "asyncio": ("asyncio", None),
    "atomic_write": ("kiro_crew.atomic_write", "atomic_write"),
    "canonical_memory_mode": ("kiro_crew.execution_context", "canonical_memory_mode"),
    "carry_provenance": ("kiro_crew.history", "carry_provenance"),
    "carry_unowned_metadata": ("kiro_crew.history", "carry_unowned_metadata"),
    "chain": ("itertools", "chain"),
    "committed_filtered_note_ids": (
        "kiro_crew.dashboard.slot_buffers",
        "committed_filtered_note_ids",
    ),
    "config_dir": ("kiro_crew.config.loader", "config_dir"),
    "deque": ("collections", "deque"),
    "drop_committed_restored_notes": (
        "kiro_crew.dashboard.slot_buffers",
        "drop_committed_restored_notes",
    ),
    "drop_records_without_placeholders": (
        "kiro_crew.dashboard.chat_utils",
        "drop_records_without_placeholders",
    ),
    "durable_row_count": ("kiro_crew.dashboard.state", "durable_row_count"),
    "effective_session_key": ("kiro_crew.dashboard.chat_utils", "effective_session_key"),
    "file_lock": ("kiro_crew.platform_compat", "file_lock"),
    "hashlib": ("hashlib", None),
    "is_channel_session_key": ("kiro_crew.messaging.link", "is_channel_session_key"),
    "is_stop_event_row": ("kiro_crew.dashboard.state", "is_stop_event_row"),
    "is_turn_interrupted": ("kiro_crew.dashboard.state", "is_turn_interrupted"),
    "islice": ("itertools", "islice"),
    "json": ("json", None),
    "kiro_agents_dir_path": ("kiro_crew.agent", "kiro_agents_dir_path"),
    "latest_transcript_ts": ("kiro_crew.history", "latest_transcript_ts"),
    "logging": ("logging", None),
    "mcp_apps_render": ("kiro_crew", "mcp_apps_render"),
    "model_registry": ("kiro_crew", "model_registry"),
    "named_store_or_empty": ("kiro_crew.memory_stores", "named_store_or_empty"),
    "os": ("os", None),
    "persist_inline_images": ("kiro_crew.chat_attachments", "persist_inline_images"),
    "queue_persist_signature": (
        "kiro_crew.dashboard.slot_queue_repository",
        "queue_persist_signature",
    ),
    "re": ("re", None),
    "read_session_execution": ("kiro_crew.execution_context", "read_session_execution"),
    "redact_credentials": ("kiro_crew.security", "redact_credentials"),
    "redact_display_content": ("kiro_crew.dashboard.chat_utils", "redact_display_content"),
    "redact_exfiltration_urls": ("kiro_crew.security", "redact_exfiltration_urls"),
    "redact_log_via_context": ("kiro_crew.platform.context", "redact_log_via_context"),
    "row_mid": ("kiro_crew.dashboard.state", "row_mid"),
    "same_text_modulo_images": ("kiro_crew.chat_attachments", "same_text_modulo_images"),
    "sanitize_restored_deferred_notes": (
        "kiro_crew.dashboard.slot_buffers",
        "sanitize_restored_deferred_notes",
    ),
    "sanitize_restored_queue": (
        "kiro_crew.dashboard.slot_queue_repository",
        "sanitize_restored_queue",
    ),
    "sel": ("kiro_crew.sel", "sel"),
    "serialize_deferred_notes": ("kiro_crew.dashboard.slot_buffers", "serialize_deferred_notes"),
    "session_agent_selection_name": (
        "kiro_crew.session_agent_selection",
        "session_agent_selection_name",
    ),
    "session_key_for": ("kiro_crew.dashboard.chat_utils", "session_key_for"),
    "slot_closed_since": ("kiro_crew.dashboard.channel_slots", "slot_closed_since"),
    "slot_history_key": ("kiro_crew.dashboard.chat_utils", "slot_history_key"),
    "slot_transcript_key": ("kiro_crew.dashboard.chat_utils", "slot_transcript_key"),
    "stricter_memory_mode": ("kiro_crew.execution_context", "stricter_memory_mode"),
    "threading": ("threading", None),
    "time": ("time", None),
    "transcript_sort_key": ("kiro_crew.history", "transcript_sort_key"),
    "union_deferred_notes": ("kiro_crew.dashboard.slot_buffers", "union_deferred_notes"),
    "update_metadata_off_loop": ("kiro_crew.history", "update_metadata_off_loop"),
    "with_bounded_redaction_records": (
        "kiro_crew.dashboard.chat_utils",
        "with_bounded_redaction_records",
    ),
}

#: The defined names that moved, and the owner each one lives in.
_MOVED: dict[str, str] = {
    "COLOR_HEX_RE": "metadata_codec",
    "_FLUSH_SNAPSHOT_RETRIES": "write_guards",
    "_LOCAL_TURN_PROMPT_MAX_ATTACHMENTS": "turn_marker",
    "_LOCAL_TURN_PROMPT_MAX_BYTES": "turn_marker",
    "_LOCAL_TURN_PROMPT_MAX_FIELD_CHARS": "turn_marker",
    "_LOCAL_TURN_PROMPT_META_KEYS": "turn_marker",
    "_LOCAL_TURN_PROMPT_ROLES": "turn_marker",
    "_META_LAST_USER_AT": "metadata_line",
    "_PENDING_MEMORY_MODE_LOCK": "metadata_line",
    "_RESTART_INTERRUPTION_KIND": "turn_marker",
    "_RESTART_INTERRUPTION_MSG": "turn_marker",
    "_RETIRED_MODES": "metadata_codec",
    "_approx_window_payload_bytes": "message_entries",
    "_archive_dropped_lines": "transcript_merge",
    "_attach_variants": "message_entries",
    "_build_kiro_model_map": "restore_inputs",
    "_build_message_entry_uncached": "message_entries",
    "_capped_dismissed_line": "metadata_line",
    "_deletion_during_read": "restore_inputs",
    "_diff_dropped_message_lines": "transcript_merge",
    "_foreign_tail_ts": "transcript_merge",
    "_frozen_prefix_and_foreign_appends": "transcript_merge",
    "_interleave_foreign_lines": "transcript_merge",
    "_is_app_owned_channel_row": "restore_inputs",
    "_keep_owed_after_refusal": "write_guards",
    "_latest_stamp": "metadata_line",
    "_latest_turn_was_deliberately_stopped": "turn_marker",
    "_line_is_this_slots": "write_guards",
    "_load_restore_cfg": "restore_inputs",
    "_local_turn_generation": "turn_marker",
    "_local_turn_prompt": "turn_marker",
    "_newest_human_turn_ts": "metadata_line",
    "_queue_snapshot_is_stale": "write_guards",
    "_read_mcp_app_claims": "restore_inputs",
    "_read_open_slots_keys": "restore_inputs",
    "_rebase_rehydrated_refresh_mark": "metadata_codec",
    "_recent_session_slot_name": "restore_inputs",
    "_reconcile_local_turn_marker": "turn_marker",
    "_reconcile_mcp_app_claims": "restore_inputs",
    "_record_pending_memory_mode": "metadata_line",
    "_recover_mcp_app_claims": "restore_inputs",
    "_rehydrate_slot_title": "metadata_codec",
    "_rehydrate_title_low_signal": "metadata_codec",
    "_rehydrate_title_origin": "metadata_codec",
    "_rehydrate_title_refresh_mark": "metadata_codec",
    "_restore_dismissed_source_links": "metadata_codec",
    "_restored_agent_name": "restore_inputs",
    "_restored_mode": "metadata_codec",
    "_sanitize_open_slot_key": "restore_inputs",
    "_stable_durable_queue": "write_guards",
    "_tighten_carried_execution": "metadata_line",
    "_validate_autocompact_pct": "metadata_codec",
    "_window_holds_row": "turn_marker",
    "local_turn_prompt_within_bounds": "turn_marker",
    "pending_slot_memory_mode": "metadata_line",
    "register_guarded_history_write": "write_guards",
    "session_transcript_remains": "write_guards",
    "session_was_deleted": "write_guards",
}

#: The signature of every function the facade defined, as it was at that base.
_SIGNATURES: dict[str, str] = {
    "_apply_recent_session": "(state: 'DashboardState', key: 'str', slot_name: 'str', session: 'dict', meta: 'dict', messages: 'list[dict]', *, conv_log: \"'ConversationLog'\", kiro_model_map: 'dict[str, str]', restore_cfg: \"'KiroCrewConfig | None'\", member_identity: 'tuple[str, str] | None' = ('', '__unresolved__'), agent: 'str | None' = None, effort_marker: 'bool' = False) -> 'None'",
    "_apply_restored_open_slot": "(state: 'DashboardState', key: 'str', *, meta: 'dict', readable: 'bool', messages: 'list[dict] | None', model_map: 'dict[str, str] | None', unrestored: 'set[str]', member_identity: 'tuple[str, str] | None' = ('', '__unresolved__'), agent: 'str | None' = None, effort_marker: 'bool' = False, conv_log: 'ConversationLog | None' = None, started: 'float | None' = None, preserve_remote_only: 'bool' = False) -> 'int'",
    "_approx_window_payload_bytes": "(window: 'list[dict]') -> 'int'",
    "_archive_dropped_lines": "(state: 'DashboardState', history_key: 'str', old_lines: 'list[str]', new_lines: 'list[str]') -> 'None'",
    "_attach_variants": "(slot: '_ChatSlot', m: 'dict') -> 'None'",
    "_build_history_prefix": "(slot: '_ChatSlot', *, conversation_log: 'ConversationLog | None' = None, current_message: 'dict | None' = None, model_window: 'int | None' = None) -> 'str'",
    "_build_kiro_model_map": "() -> 'dict[str, str]'",
    "_build_message_entry": "(m: 'dict', *, attachments: 'tuple[Path, str] | None' = None) -> 'dict | None'",
    "_build_message_entry_uncached": "(m: 'dict', *, attachments: 'tuple[Path, str] | None' = None) -> 'dict | None'",
    "_capped_dismissed_line": "(keys: 'Iterable[str]') -> 'list[str]'",
    "_coerce_requested_mode": "(raw: 'Any') -> 'Any'",
    "_deletion_during_read": "(conv_log: 'ConversationLog', history_key: 'str', pre_meta: 'dict', pre_messages: 'list[dict] | None') -> 'str | None'",
    "_diff_dropped_message_lines": "(old_lines: 'list[str]', new_lines: 'list[str]') -> 'list[str]'",
    "_effort_marker_path": "(directory: 'Path', level: 'str') -> 'Path'",
    "_entry_cache_bounds": "() -> 'tuple[int, int]'",
    "_foreign_tail_ts": "(foreign_lines: 'list[str]') -> 'str | None'",
    "_frozen_prefix_and_foreign_appends": "(slot: '_ChatSlot', path, disk_older: 'int', window_entries: 'list[dict]', *, collect_foreign: 'bool' = True) -> 'tuple[str, list[str], list[str]]'",
    "_has_validated_effort_marker": "(raw: 'object') -> 'bool'",
    "_interleave_foreign_lines": "(window_entries: 'list[dict]', window_lines: 'list[str]', foreign_lines: 'list[str]') -> 'list[str]'",
    "_is_app_owned_channel_row": "(meta: 'dict', history_key: 'str') -> 'bool'",
    "_keep_owed_after_refusal": "(slot: '_ChatSlot') -> 'None'",
    "_latest_stamp": "(*candidates: 'str') -> 'str'",
    "_latest_turn_was_deliberately_stopped": "(messages: 'list[dict]') -> 'bool'",
    "_line_is_this_slots": "(slot: '_ChatSlot', existing_meta: 'dict') -> 'bool'",
    "_load_restore_cfg": "() -> \"'KiroCrewConfig | None'\"",
    "_local_turn_generation": "(meta: 'Mapping[str, object]') -> 'int'",
    "_local_turn_prompt": "(meta: 'Mapping[str, object]') -> 'dict | None'",
    "_member_private_selection": "(agent: 'str', config: 'KiroCrewConfig', *, authorized_store: 'str | None' = None) -> 'tuple[str, str]'",
    "_member_restore_identity": "(slot_name: 'str') -> 'tuple[str, str] | None'",
    "_newest_human_turn_ts": "(rows: \"'list[dict] | tuple[dict, ...]'\") -> 'str'",
    "_on_config_change": "(change: 'object') -> 'None'",
    "_pin_private_agent_assignment": "(session_key: 'str', agent: 'str', config: 'KiroCrewConfig', *, conversation_log=None, native_context: 'bool' = False, authorized_store: 'str | None' = None, memory_mode: 'str' = 'persistent', validate_only: 'bool' = False) -> 'str'",
    "_prefetch_recent_session": "(conv_log: 'ConversationLog', key: 'str', session: 'dict', *, folders_only: 'bool', cutoff: 'float | None') -> 'tuple[dict | None, list[dict] | None, tuple[str, str] | None, str | None, bool]'",
    "_prefetch_rehydrate_inputs": "(conv_log: 'ConversationLog', history_key: 'str', *, adopt_closed: 'bool' = False, kiro_model_map: 'dict[str, str] | None' = None, with_status: 'bool' = False) -> 'tuple[dict, bool, list[dict] | None, dict[str, str] | None, tuple[str, str] | None, str | None, bool]'",
    "_queue_snapshot_is_stale": "(slot: '_ChatSlot', queue_write_basis: 'str') -> 'bool'",
    "_read_mcp_app_claims": "(session_key: 'str') -> 'list[set[str]]'",
    "_read_open_slots_keys": "() -> 'list[object]'",
    "_rebase_rehydrated_refresh_mark": "(slot: '_ChatSlot') -> 'None'",
    "_recent_session_slot_name": "(key: 'str') -> 'str | None'",
    "_reconcile_local_turn_marker": "(slot: '_ChatSlot', generation: 'int', prompt: 'Mapping[str, object] | None' = None, *, persisted: 'Iterable[Mapping[str, object]] | None' = None) -> 'bool'",
    "_reconcile_mcp_app_claims": "(slot: '_ChatSlot', claims: 'list[set[str]]') -> 'None'",
    "_record_pending_memory_mode": "(slot: '_ChatSlot', memory_mode: 'str') -> 'None'",
    "_recover_mcp_app_claims": "(slot: '_ChatSlot') -> 'None'",
    "_recover_mcp_app_claims_async": "(slot: '_ChatSlot') -> 'None'",
    "_rehydrate_slot_from_history": "(state: 'DashboardState', slot_name: 'str', *, kiro_model_map: 'dict[str, str] | None' = None, adopt_closed: 'bool' = False, _prefetched_meta: 'dict | None' = None, _prefetched_messages: 'list[dict] | None' = None, _prefetched_member_identity: 'tuple[str, str] | None' = ('', '__unresolved__'), _prefetched_agent: 'str | None' = None, _prefetched_effort_marker: 'bool' = False) -> '_ChatSlot | None'",
    "_rehydrate_slot_title": "(slot: '_ChatSlot', raw_title: 'str', *, titled: 'bool', metadata: 'Mapping[str, object]') -> 'None'",
    "_rehydrate_title_low_signal": "(stored: 'object') -> 'bool'",
    "_rehydrate_title_origin": "(titled: 'bool', stored: 'object') -> 'str'",
    "_rehydrate_title_refresh_mark": "(stored: 'object') -> 'int'",
    "_remember_reasoning_effort_for_restore": "(level: 'str') -> 'None'",
    "_restore_dismissed_source_links": "(slot: \"'_ChatSlot'\", raw: 'object') -> 'None'",
    "_restore_open_slots_steps": "(state: 'DashboardState') -> \"'Iterator[int]'\"",
    "_restore_recent_sessions_steps": "(state: 'DashboardState', window_minutes: 'int' = 30, *, folders_only: 'bool' = False) -> \"'Iterator[int]'\"",
    "_restored_agent_name": "(session_key: 'str', meta: 'dict') -> 'str'",
    "_restored_mode": "(raw: 'object') -> 'str'",
    "_retain_reasoning_effort_values": "(acp_levels: 'list[str]', *, source: 'str') -> 'list[str]'",
    "_sanitize_open_slot_key": "(raw: 'object') -> 'str | None'",
    "_save_slot_to_history": "(state: 'DashboardState', slot: '_ChatSlot', messages: 'list[dict] | None' = None, *, closed: 'bool' = False, closed_at: 'float | None' = None, force: 'bool' = False, rewrite: 'bool' = False, expected_history_key: 'str | None' = None, expected_disk_older_count: 'int | None' = None, expected_slot_name: 'str | None' = None, rows_only: 'bool' = False, pending_mode_slot: '_ChatSlot | None' = None, mutes_opened_override: 'bool | None' = None, after_commit_under_lock: 'Callable[[], None] | None' = None, refuse_stale_empty_merge: 'bool' = False, takeover_basis: 'TakeoverBasis | None' = None) -> 'bool'",
    "_stable_durable_queue": "(slot: '_ChatSlot') -> 'tuple[list[dict], int]'",
    "_tighten_carried_execution": "(meta_line: 'dict', mode: 'str') -> 'None'",
    "_validate_autocompact_pct": "(raw: 'object') -> 'float | None'",
    "_validate_reasoning_effort": "(raw: 'object', *, persisted_marker: 'bool' = False) -> 'str'",
    "_window_holds_row": "(messages: 'Iterable[Mapping[str, object]]', prompt: 'Mapping[str, object]') -> 'bool'",
    "cap_effort_capability_levels": "(levels: 'Iterable[object]', *, source: 'str') -> 'list[str]'",
    "get_reasoning_effort_ordered": "() -> 'list[str]'",
    "get_reasoning_effort_values": "() -> 'frozenset[str]'",
    "local_turn_prompt_within_bounds": "(prompt: 'Mapping[str, object]') -> 'bool'",
    "member_store_ownership_holds": "(config: 'KiroCrewConfig', member: 'str', entry_store: 'str') -> 'bool'",
    "pending_slot_memory_mode": "(slot: '_ChatSlot') -> 'str | None'",
    "pin_private_agent_store": "(state: 'DashboardState', session_key: 'str', agent: 'str', config: 'KiroCrewConfig', *, memory_mode: 'str' = 'persistent', validate_only: 'bool' = False) -> 'str'",
    "register_guarded_history_write": "(slot: '_ChatSlot', save: \"'asyncio.Future[bool]'\") -> 'None'",
    "register_reasoning_effort_values": "(acp_levels: 'list[str]') -> 'list[str]'",
    "rehydrate_slot_from_history_async": "(state: 'DashboardState', slot_name: 'str', *, kiro_model_map: 'dict[str, str] | None' = None, adopt_closed: 'bool' = False) -> '_ChatSlot | None'",
    "release_prewarmed_session": "(state: 'DashboardState', session_key: 'str', agent: 'str', config: 'KiroCrewConfig') -> 'bool'",
    "restore_open_slots": "(state: 'DashboardState') -> 'int'",
    "restore_open_slots_async": "(state: 'DashboardState') -> 'int'",
    "restore_recent_sessions": "(state: 'DashboardState', window_minutes: 'int' = 30, *, folders_only: 'bool' = False) -> 'int'",
    "restore_recent_sessions_async": "(state: 'DashboardState', window_minutes: 'int' = 30, *, folders_only: 'bool' = False) -> 'int'",
    "save_all_slots_to_history": "(state: 'DashboardState') -> 'None'",
    "save_slot_off_loop": "(state: 'DashboardState', slot: '_ChatSlot', messages: 'list[dict] | None' = None, *, closed: 'bool' = False, closed_at: 'float | None' = None, force: 'bool' = False, rewrite: 'bool' = False, best_effort: 'bool' = True, expected_history_key: 'str | None' = None, expected_slot_name: 'str | None' = None, rows_only: 'bool' = False, issued_by_the_retraction: 'bool' = False, mutes_opened_override: 'bool | None' = None, after_commit_under_lock: 'Callable[[], None] | None' = None, takeover_basis: 'TakeoverBasis | None' = None) -> 'bool'",
    "session_transcript_remains": "(state: 'DashboardState', slot: '_ChatSlot') -> 'bool'",
    "session_was_deleted": "(state: 'DashboardState', slot: '_ChatSlot') -> 'bool'",
    "update_reasoning_effort_values": "(acp_levels: 'list[str]') -> 'None'",
    "watch_config": "() -> 'None'",
}

#: The coroutine functions among them.
_ASYNC = frozenset(
    {
        "_recover_mcp_app_claims_async",
        "pin_private_agent_store",
        "rehydrate_slot_from_history_async",
        "release_prewarmed_session",
        "restore_open_slots_async",
        "restore_recent_sessions_async",
        "save_slot_off_loop",
    }
)


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{OWNER_PACKAGE}.{stem}")


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(OWNER_DIR.glob("*.py"))
        if path.stem != "__init__"
    }


def _typing_only(tree: ast.Module) -> set[int]:
    return {
        id(sub)
        for block in tree.body
        if isinstance(block, ast.If) and ast.unparse(block.test) == "TYPE_CHECKING"
        for sub in ast.walk(block)
    }


def _module_scope_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """``(lineno, module)`` for each import statement at module scope, typing-only aside."""
    typing_only = _typing_only(tree)
    found: list[tuple[int, str]] = []
    for node in tree.body:
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.ImportFrom):
            found.append((node.lineno, "." * node.level + (node.module or "")))
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
    return found


class TestSurface:
    def test_the_package_holds_exactly_the_composed_modules(self) -> None:
        assert set(_owner_sources()) == OWNER_MODULES

    def test_every_name_the_facade_defined_still_resolves(self) -> None:
        assert [name for name in sorted(_FACADE_DEFINED) if not hasattr(cp, name)] == []

    def test_every_name_the_facade_imported_still_resolves_to_its_home_object(self) -> None:
        wrong = []
        for name, (home, attr) in sorted(_FACADE_IMPORTS.items()):
            expected = (
                importlib.import_module(home)
                if attr is None
                else getattr(importlib.import_module(home), attr)
            )
            if getattr(cp, name, None) is not expected:
                wrong.append(name)
        assert wrong == []

    def test_a_moved_name_is_its_owners_object_not_a_copy(self) -> None:
        assert set(_MOVED.values()) == OWNER_MODULES
        wrong = [
            name
            for name, stem in sorted(_MOVED.items())
            if getattr(cp, name) is not vars(_owner(stem))[name]
        ]
        assert wrong == []

    def test_a_name_lives_in_exactly_one_place(self) -> None:
        homes: dict[str, list[str]] = {}
        for stem in sorted(OWNER_MODULES):
            for name in vars(_owner(stem)):
                if name in _FACADE_DEFINED and name != "logger":
                    homes.setdefault(name, []).append(stem)
        assert {name: [_MOVED.get(name)] for name in homes} == homes
        stayed = sorted(_FACADE_DEFINED - set(_MOVED))
        functions = [name for name in stayed if isinstance(getattr(cp, name), types.FunctionType)]
        assert [name for name in functions if getattr(cp, name).__module__ != FACADE] == []

    def test_moved_functions_name_their_owner_module(self) -> None:
        wrong = {
            name: obj.__module__
            for name, stem in sorted(_MOVED.items())
            if isinstance(obj := getattr(cp, name), types.FunctionType)
            and obj.__module__ != f"{OWNER_PACKAGE}.{stem}"
        }
        assert wrong == {}

    def test_every_function_keeps_its_signature_and_kind(self) -> None:
        current = {name: str(inspect.signature(getattr(cp, name))) for name in _SIGNATURES}
        assert current == _SIGNATURES
        coroutine_functions = {
            name for name in _SIGNATURES if inspect.iscoroutinefunction(getattr(cp, name))
        }
        assert coroutine_functions == _ASYNC

    def test_the_facade_is_a_plain_module(self) -> None:
        """Every name is an ordinary binding: no ``__getattr__`` forwarding, no class
        swap and no ``__all__``, so a patch and its undo are plain attribute writes,
        mypy reads each name from its import, and a star import exports what it did."""
        assert type(cp) is types.ModuleType
        assert "__getattr__" not in vars(cp)
        assert "__all__" not in vars(cp)
        assert all("__getattr__" not in vars(_owner(stem)) for stem in OWNER_MODULES)

    def test_a_star_import_still_binds_every_public_name(self, tmp_path: Path) -> None:
        probe_path = tmp_path / "chat_persistence_star_probe.py"
        probe_path.write_text(f"from {FACADE} import *\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("chat_persistence_star_probe", probe_path)
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        public = [name for name in [*_FACADE_DEFINED, *_FACADE_IMPORTS] if not name.startswith("_")]
        assert public
        assert [name for name in sorted(public) if not hasattr(probe, name)] == []

    def test_the_package_ships_with_the_wheel(self) -> None:
        import configparser

        config = configparser.ConfigParser()
        config.read(FACADE_PATH.parents[3] / "setup.cfg", encoding="utf-8")
        assert config.get("options", "packages").strip() == "find:"
        assert config.get("options.packages.find", "where").strip() == "src"
        assert (OWNER_DIR / "__init__.py").is_file()


class TestEagerOwners:
    def test_the_facade_imports_every_owner_last(self) -> None:
        """At module scope and after every other statement, so loading the owners
        cannot reorder anything the facade itself imports or binds."""
        tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
        owner_imports = [
            index
            for index, node in enumerate(tree.body)
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(OWNER_PACKAGE)
        ]
        assert owner_imports and owner_imports == list(
            range(owner_imports[0], len(tree.body))
        ), "every statement after the first owner import must itself be an owner import"
        loaded = {
            (node.module or "").removeprefix(OWNER_PACKAGE).lstrip(".") or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(OWNER_PACKAGE)
            for alias in node.names
        }
        assert OWNER_MODULES <= loaded

    def test_every_owner_is_loaded_by_a_fresh_import_of_the_facade(self, tmp_path: Path) -> None:
        probe = (
            "import json, sys\n"
            f"import {FACADE}\n"
            f"print(json.dumps(sorted(m for m in sys.modules if m.startswith({OWNER_PACKAGE + '.'!r}))))\n"
        )
        env = dict(os.environ)
        src = str(Path(kiro_crew.__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=True,
            env=env,
            cwd=tmp_path,
            timeout=120,
            **UTF8_TEXT,
        )
        loaded = json.loads(out.stdout.splitlines()[-1])
        assert loaded == sorted(f"{OWNER_PACKAGE}.{stem}" for stem in OWNER_MODULES), out.stderr

    def test_owners_import_only_modules_the_facade_already_imports(self) -> None:
        """So the facade's import closure, and its order, are what they were."""
        facade = {
            module
            for _, module in _module_scope_imports(
                ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
            )
        }
        offenders = {}
        for stem, source in _owner_sources().items():
            extra = [
                f"{line}: {module}"
                for line, module in _module_scope_imports(ast.parse(source))
                if module.startswith(("kiro_crew", ".")) and module not in facade
            ]
            if extra:
                offenders[stem] = extra
        assert offenders == {}

    def test_the_package_init_imports_nothing(self) -> None:
        tree = ast.parse((OWNER_DIR / "__init__.py").read_text(encoding="utf-8"))
        assert [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))] == []


#: Every spelling a test uses to reach the facade module.
_MENTIONS_THE_FACADE = re.compile(
    r"kiro_crew\.dashboard\.chat_persistence\b"
    r"|from kiro_crew\.dashboard import [^\n]*\bchat_persistence\b"
)
_PATCH_CALLS = ("setattr", "patch.object", "delattr")


def _facade_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to the facade in *tree*, resolved to a fixed point."""
    aliases = {FACADE}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.dashboard":
            aliases |= {a.asname or a.name for a in node.names if a.name == "chat_persistence"}
        elif isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == FACADE and a.asname}
    loaders = {
        f'importlib.import_module("{FACADE}")',
        f"importlib.import_module('{FACADE}')",
        f'sys.modules["{FACADE}"]',
        f"sys.modules['{FACADE}']",
    }
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and (ast.unparse(node.value) in aliases or ast.unparse(node.value) in loaders)
                and node.targets[0].id not in aliases
            ):
                aliases.add(node.targets[0].id)
                changed = True
    return aliases


def _patched_names_in(text: str) -> set[str]:
    """Names one test source rebinds on the facade itself.

    A dotted target (``chat_persistence.KiroCrewConfig.load``) patches an attribute
    of a shared object rather than a facade binding, so every holder of that object
    sees it; only first-level facade names count.
    """
    if not _MENTIONS_THE_FACADE.search(text):
        return set()
    tree = ast.parse(text)
    aliases = _facade_aliases(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and len(node.args) >= 2:
            target, name = node.args[0], node.args[1]
            if (
                ast.unparse(target) in aliases
                and isinstance(name, ast.Constant)
                and isinstance(name.value, str)
                and ast.unparse(node.func).endswith(_PATCH_CALLS)
            ):
                found.add(name.value)
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and ast.unparse(target.value) in aliases:
                    found.add(target.attr)
    found |= set(re.findall(r"""["']kiro_crew\.dashboard\.chat_persistence\.(\w+)["']""", text))
    return found


def _facade_patched_names() -> set[str]:
    """Names any test rebinds on the facade."""
    tests = repo_root() / "test"
    here = Path(__file__).resolve()
    found: set[str] = set()
    for path in repo_files_named(".py"):
        in_tests = path.is_relative_to(tests) or "/tests/" in path.as_posix()
        if not in_tests or path.resolve() == here:
            continue
        found |= _patched_names_in(path.read_text(encoding="utf-8", errors="replace"))
    return found


def _bare_loads(source: str, names: set[str]) -> list[str]:
    """Reads of *names* inside owner functions that a patch on the facade would miss.

    Such a name is read as ``cp.<name>`` after a function-local import of the
    facade. A bare global load, or a function-local import binding the name, reads
    some other binding.
    """
    tree = ast.parse(source)
    annotations: set[int] = set()
    for node in ast.walk(tree):
        for field in ("annotation", "returns"):
            sub = getattr(node, field, None)
            if sub is not None:
                annotations.update(id(part) for part in ast.walk(sub))
    hits = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        local = {arg.arg for arg in ast.walk(function) if isinstance(arg, ast.arg)}
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                local.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound = (alias.asname or alias.name).split(".")[0]
                    imported = alias.name.split(".")[-1]
                    local.add(bound)
                    if {bound, imported} & names:
                        hits.append(f"{node.lineno}: import binds {imported}")
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in names
                and node.id not in local
                and id(node) not in annotations
            ):
                hits.append(f"{node.lineno}: {node.id}")
    return sorted(set(hits), key=lambda hit: (int(hit.split(":")[0]), hit))


class TestPlacement:
    """The rules that keep a patch on the facade effective once the code has moved."""

    def test_owners_reach_the_facade_as_a_module_at_call_time(self) -> None:
        imports = 0
        for stem, source in _owner_sources().items():
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.ImportFrom):
                    continue
                assert node.module != FACADE, f"{stem}:{node.lineno} imports a facade name"
                if node.module == "kiro_crew.dashboard" and "chat_persistence" in [
                    a.name for a in node.names
                ]:
                    assert node.col_offset > 0, f"{stem}:{node.lineno} imports the facade early"
                    assert [a.asname for a in node.names] == ["cp"], f"{stem}:{node.lineno}"
                    imports += 1
        # Non-vacuous: the owners do reach the facade's seams.
        assert imports >= 10

    def test_no_owner_imports_another_owner(self) -> None:
        """A name from another owner is read through the facade like any other, so
        a patch on the facade reaches it and the owners form no import graph."""
        offenders = []
        for stem, source in _owner_sources().items():
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.ImportFrom) and (
                    node.level or (node.module or "").startswith(OWNER_PACKAGE)
                ):
                    offenders.append(f"{stem}:{node.lineno}")
        assert offenders == []

    def test_no_owner_imports_a_name_tests_rebind_on_the_facade(self) -> None:
        """The contract the placement rules stand for, derived from the tests: an
        owner that imported a rebound name would keep using its own binding, and the
        patch would silently stop applying there. An owner may DEFINE one -- the
        facade's binding of it is the seam -- because no owner reads such a name
        bare (the next test), so every caller goes through the facade."""
        patched = _facade_patched_names()
        # Non-vacuous: the scan sees seams the persistence tests rebind.
        assert {
            "_save_slot_to_history",
            "save_slot_off_loop",
            "atomic_write",
            "redact_credentials",
            "persist_inline_images",
            "_prefetch_rehydrate_inputs",
            "_entry_cache_bounds_cached",
            "_reasoning_effort_values",
        } <= patched
        for stem, source in _owner_sources().items():
            imported = {
                (alias.asname or alias.name).split(".")[0]
                for node in ast.walk(ast.parse(source))
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            assert imported & patched == set(), stem
            defined = {name for name in vars(_owner(stem)) if name in patched}
            assert all(getattr(cp, name) is vars(_owner(stem))[name] for name in defined), stem

    def test_no_owner_reads_a_rebound_name_as_a_bare_global(self) -> None:
        patched = _facade_patched_names()
        offenders = {
            stem: hits
            for stem, source in _owner_sources().items()
            if (hits := _bare_loads(source, patched))
        }
        assert offenders == {}

    def test_no_owner_reads_another_owners_name_as_a_bare_global(self) -> None:
        for stem, source in _owner_sources().items():
            own = set(vars(_owner(stem)))
            elsewhere = (_FACADE_DEFINED - own) | {
                name for name in _FACADE_IMPORTS if name not in own
            }
            assert _bare_loads(source, elsewhere) == [], stem

    def test_the_patch_scan_reads_every_spelling(self) -> None:
        planted = (
            "import importlib\n"
            "import kiro_crew.dashboard.chat_persistence as pm\n"
            "from kiro_crew.dashboard import chat_persistence as cpm\n"
            "facade = importlib.import_module('kiro_crew.dashboard.chat_persistence')\n"
            "alias = facade\n"
            "def test(monkeypatch):\n"
            "    monkeypatch.setattr(pm, 'first', 1)\n"
            "    monkeypatch.setattr(cpm, 'second', 2)\n"
            "    patch.object(alias, 'third')\n"
            "    cpm.fourth = 4\n"
            "    monkeypatch.setattr('kiro_crew.dashboard.chat_persistence.fifth', 5)\n"
            "    monkeypatch.setattr('kiro_crew.dashboard.chat_persistence.Shared.attr', 6)\n"
            "    monkeypatch.setattr(cpm.Shared, 'attr', 7)\n"
            "    monkeypatch.setattr(other, 'not_facade', 8)\n"
            "    monkeypatch.delattr(cpm, 'sixth')\n"
        )
        assert _patched_names_in(planted) == {
            "first",
            "second",
            "third",
            "fourth",
            "fifth",
            "sixth",
        }

    def test_the_seam_scan_catches_a_planted_bare_read(self) -> None:
        planted = (
            "def f(path: 'atomic_write') -> 'sel':\n"
            "    from kiro_crew.dashboard import chat_persistence as cp\n"
            "    good = cp.atomic_write(path, '')\n"
            "    return atomic_write(path, ''), sel\n"
            "def g():\n"
            "    from kiro_crew.security import redact_credentials\n"
            "    from kiro_crew.config.loader import config_dir as home\n"
            "    return redact_credentials, home\n"
        )
        seams = {"atomic_write", "sel", "redact_credentials", "config_dir"}
        assert [hit.split(": ")[1] for hit in _bare_loads(planted, seams)] == [
            "atomic_write",
            "sel",
            "import binds redact_credentials",
            "import binds config_dir",
        ]

    def test_every_owner_logs_under_the_facade_logger(self) -> None:
        """Log capture filters on the facade's logger name, so a moved warning keeps it."""
        logged = 0
        for stem, source in _owner_sources().items():
            for call in ast.walk(ast.parse(source)):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "getLogger"
                ):
                    assert [ast.literal_eval(arg) for arg in call.args] == [FACADE], stem
            if "logger" in vars(_owner(stem)):
                assert _owner(stem).logger is cp.logger is logging.getLogger(FACADE)
                logged += 1
        assert logged >= 5

    def test_no_owner_anchors_a_path_on_its_own_file(self) -> None:
        for stem, source in _owner_sources().items():
            names = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)]
            assert not [n for n in names if n.id == "__file__"], stem

    def test_no_owner_defines_an_async_function(self) -> None:
        """The async restore drivers and the on-loop save entry point stay on the
        facade, where the off-loop read gate scans them; owners hold sync rules."""
        for stem, source in _owner_sources().items():
            nodes = ast.walk(ast.parse(source))
            assert not [n for n in nodes if isinstance(n, ast.AsyncFunctionDef)], stem

    def test_no_owner_dispatches_a_save(self) -> None:
        """Owners hold the rules a save consults; only the facade starts a save, so
        the fences around who may dispatch one stay where they are enforced."""
        savers = {"save_slot_off_loop", "_save_slot_to_history", "save_all_slots_to_history"}
        for stem, source in _owner_sources().items():
            called = {
                (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call)
            }
            assert called & savers == set(), stem

    def test_the_orchestration_stays_on_the_facade(self) -> None:
        """Gates key these by the facade's path or read its text, so the code itself
        stays here: the save transaction, its on-loop entry point, the restore
        drivers and slot builders, the prefetch reads and the member assignment."""
        stays = {
            "_save_slot_to_history",
            "save_slot_off_loop",
            "save_all_slots_to_history",
            "_rehydrate_slot_from_history",
            "rehydrate_slot_from_history_async",
            "_apply_recent_session",
            "_prefetch_rehydrate_inputs",
            "_prefetch_recent_session",
            "restore_open_slots",
            "restore_open_slots_async",
            "restore_recent_sessions",
            "restore_recent_sessions_async",
            "_recover_mcp_app_claims_async",
            "_pin_private_agent_assignment",
            "_build_message_entry",
        }
        assert {name: getattr(cp, name).__module__ for name in stays} == dict.fromkeys(
            stays, FACADE
        )

    def test_process_state_stays_on_the_facade(self) -> None:
        """Fixtures and tests reset and rebind these on the facade module object."""
        state = {
            "_entry_cache",
            "_entry_cache_lock",
            "_entry_cache_bytes",
            "_entry_cache_bounds_cached",
            "_entry_cache_bounds_read_warned",
            "_entry_cache_config_sub",
            "_ENTRY_MAX_CACHEABLE_BYTES",
            "_reasoning_effort_values",
            "_reasoning_effort_ordered",
            "_reasoning_effort_marked",
        }
        assert state <= set(vars(cp))
        for stem in sorted(OWNER_MODULES):
            assert state & set(vars(_owner(stem))) == set(), stem


class TestPatchReach:
    """A patch on the facade reaches the moved code that consumes it."""

    def test_title_restore_redacts_through_the_facade(self, monkeypatch) -> None:
        from types import SimpleNamespace

        monkeypatch.setattr(cp, "redact_exfiltration_urls", lambda text: (f"<u>{text}", []))
        monkeypatch.setattr(cp, "redact_credentials", lambda text: (f"<c>{text}", []))
        slot = SimpleNamespace()
        cp._rehydrate_slot_title(slot, "t", titled=True, metadata={})
        assert slot.title == "<c><u>t"

    def test_the_row_projection_redacts_and_copies_images_through_the_facade(
        self, monkeypatch, tmp_path
    ) -> None:
        seen: list[str] = []

        def _persist(content, *, sessions_dir, stem, budget):
            seen.append(stem)
            return content + "!"

        monkeypatch.setattr(cp, "persist_inline_images", _persist)
        monkeypatch.setattr(cp, "redact_exfiltration_urls", lambda text: (text, []))
        monkeypatch.setattr(cp, "redact_credentials", lambda text: (text.upper(), []))
        entry = cp._build_message_entry_uncached(
            {"role": "assistant", "content": "hi", "ts": "t"}, attachments=(tmp_path, "s1")
        )
        assert entry is not None and entry["content"] == "HI!" and seen == ["s1"]

    def test_the_open_slot_screen_and_reads_go_through_the_facade(
        self, monkeypatch, tmp_path
    ) -> None:
        (tmp_path / "open_slots.json").write_text('{"keys": ["a"]}', encoding="utf-8")
        monkeypatch.setattr(cp, "config_dir", lambda: tmp_path)
        assert cp._read_open_slots_keys() == ["a"]
        monkeypatch.setattr(cp, "_normalize_slot_key", lambda raw: f"n-{raw}")
        assert cp._sanitize_open_slot_key("k") == "n-k"

    def test_the_model_map_reads_agents_through_the_facade(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(cp, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr(
            cp, "agent_model_map", lambda *, agents_dir, operation, source: {"a": str(agents_dir)}
        )
        assert cp._build_kiro_model_map() == {"a": str(tmp_path)}

    def test_claim_recovery_reads_the_spool_through_the_facade(self, monkeypatch) -> None:
        from types import SimpleNamespace

        asked: list[str] = []
        monkeypatch.setattr(cp, "_read_mcp_app_claims", lambda key: asked.append(key) or [])
        slot = SimpleNamespace(key="k", linked_session_key="", messages=[], _dirty=False)
        cp._recover_mcp_app_claims(slot)
        assert asked == ["dashboard:k"]

    def test_the_note_filter_and_its_audit_go_through_the_facade(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard.slot_persistence import write_guards

        audited: list[dict] = []
        monkeypatch.setattr(cp, "_note_authorized_elsewhere", lambda meta, key: bool(meta))
        monkeypatch.setattr(
            cp, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: audited.append(kw))
        )
        slot = SimpleNamespace(key="k")
        window = [{"role": "user", "meta": None}, {"role": "note", "meta": {"x": 1}}]
        assert write_guards.drop_notes_authorized_elsewhere(slot, window, "dashboard:k") == [
            window[0]
        ]
        assert [row["operation"] for row in audited] == ["note_save_drop"]

    def test_the_payload_builds_rows_through_the_facades_builder(
        self, monkeypatch, tmp_path
    ) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("reach-builder")
        slot.append("user", "hi", "msg msg-u", "2026-01-02T03:04:05", broadcast=False)
        built: list[str] = []
        monkeypatch.setattr(cp, "_entry_cache_bounds", lambda: (0, 0))
        monkeypatch.setattr(
            cp,
            "_build_message_entry_uncached",
            lambda m, *, attachments=None: built.append(m["content"])
            or {"role": "user", "content": "X"},
        )
        assert cp._save_slot_to_history(state, slot, force=True) is True
        assert built == ["hi"]

    def test_the_line_fold_asks_the_facade(self, monkeypatch, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("reach-line")
        slot.append("user", "hi", "msg msg-u", "2026-01-02T03:04:05", broadcast=False)
        slot.reasoning_effort = "high"
        remembered: list[str] = []
        asked: list[str] = []
        monkeypatch.setattr(cp, "_remember_reasoning_effort_for_restore", remembered.append)
        monkeypatch.setattr(cp, "_line_is_this_slots", lambda s, meta: asked.append(s.key) or True)
        assert cp._save_slot_to_history(state, slot, force=True, rows_only=True) is True
        assert remembered == ["high"] and asked == ["reach-line"]

    def test_undo_restores_the_facade_binding(self) -> None:
        original = cp.redact_credentials
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(cp, "redact_credentials", lambda text: ("x", []))
            entry = cp._build_message_entry_uncached({"role": "assistant", "content": "AKIA"})
            assert entry is not None and entry["content"] == "x"
        assert cp.redact_credentials is original


# ── Persisted bytes ───────────────────────────────────────────────────────────
#
# Each scenario drives the real save against a real ConversationLog in a temp
# home, with every clock-, uuid- and id-derived value pinned, and returns what it
# wrote. ``_GOLDEN`` is what the same scenario wrote before the split, so a change
# to the order a field is folded, a key the merge carries or a line a merge keeps
# is a byte difference here.

_T0 = "2026-01-02T03:04:05"


def _ts(seconds: int) -> str:
    return f"2026-01-02T03:04:{seconds:02d}"


def _fresh_slot(state, name: str):
    slot = state.get_or_create_slot(name)
    slot.created_at = _T0
    slot._tab_id = f"tab-{name}"
    return slot


def _rows(slot, *rows: tuple[str, str, int, dict | None]) -> None:
    for role, content, second, meta in rows:
        slot.append(role, content, "", _ts(second), broadcast=False, meta=meta)


def _file(state, slot) -> str:
    return state.conversation_log._path(cp.slot_history_key(slot)).read_text(encoding="utf-8")


def _scenario_full_save(state) -> dict:
    slot = _fresh_slot(state, "golden-full")
    slot.agent = "writer"
    slot.title = "Golden"
    slot._titled = True
    slot._title_origin = "auto"
    slot._title_refresh_mark = 3
    slot.model = "model-x"
    slot.autocompact_pct = 55.0
    slot.folder_id = "folder-1"
    slot.tags = ["tag-1"]
    slot.pinned = True
    slot.color_index = 2
    slot.color_hex = "#aabbcc"
    slot.project = "/srv/project"
    slot.mode = "focus"
    _rows(
        slot,
        ("user", "hello", 6, {"mid": "m-1", HUMAN_TURN_META_KEY: True}),
        ("assistant", "the key is AKIAIOSFODNN7EXAMPLE", 7, {"mid": "m-2"}),
        ("system", "a notice", 8, {"mid": "m-3"}),
    )
    ok = cp._save_slot_to_history(state, slot, force=True)
    return {"ok": ok, "file": _file(state, slot), "window_len": slot._disk_window_len}


def _scenario_empty_window_merge(state) -> dict:
    slot = _fresh_slot(state, "golden-empty")
    key = cp.slot_history_key(slot)
    state.conversation_log.update_metadata(
        key, {"created_at": _T0, "title": "Old", "rotation_generation": 4, "other_layer": "kept"}
    )
    slot.title = "New"
    slot._titled = True
    slot.folder_id = "folder-2"
    slot.tags = ["a", "b"]
    slot._dismissed_source_links = set()
    ok = cp._save_slot_to_history(state, slot, force=True, closed=True, closed_at=1767225600.0)
    return {"ok": ok, "file": _file(state, slot)}


def _scenario_foreign_append(state, caplog) -> dict:
    slot = _fresh_slot(state, "golden-foreign")
    _rows(slot, ("user", "first", 6, {"mid": "m-1"}), ("assistant", "second", 8, {"mid": "m-2"}))
    cp._save_slot_to_history(state, slot, force=True)
    path = state.conversation_log._path(cp.slot_history_key(slot))
    foreign = {"role": "assistant", "content": "from cron", "ts": _ts(7), "source_thread": "cron"}
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(foreign) + "\n")
    _rows(slot, ("user", "third", 9, {"mid": "m-3"}))
    with caplog.at_level(logging.WARNING, logger=FACADE):
        caplog.clear()
        ok = cp._save_slot_to_history(state, slot)
        first = [r.getMessage() for r in caplog.records if "another writer" in r.getMessage()]
        caplog.clear()
        _rows(slot, ("assistant", "fourth", 10, {"mid": "m-4"}))
        again = cp._save_slot_to_history(state, slot)
        second = [r.getMessage() for r in caplog.records if "another writer" in r.getMessage()]
    return {"ok": [ok, again], "file": _file(state, slot), "warned": [first, second]}


def _scenario_rows_only_handover(state) -> dict:
    slot = _fresh_slot(state, "golden-handover")
    key = cp.slot_history_key(slot)
    state.conversation_log.update_metadata(
        key,
        {
            "created_at": _T0,
            "title": "Theirs",
            "tab_id": "tab-replacement",
            "folder_id": "their-folder",
            "memory_mode": "persistent",
        },
    )
    slot.title = "Mine"
    slot._titled = True
    slot.folder_id = "my-folder"
    _rows(slot, ("user", "unsaved tail", 6, {"mid": "m-1"}))
    ok = cp._save_slot_to_history(state, slot, rows_only=True)
    return {"ok": ok, "file": _file(state, slot)}


def _scenario_truncating_rewrite(state) -> dict:
    slot = _fresh_slot(state, "golden-rewrite")
    _rows(
        slot,
        ("user", "q1", 6, {"mid": "m-1"}),
        ("assistant", "a1", 7, {"mid": "m-2"}),
        ("assistant", "a2 to drop", 8, {"mid": "m-3"}),
    )
    cp._save_slot_to_history(state, slot, force=True)
    snapshot = list(slot.messages[:2])
    ok = cp._save_slot_to_history(
        state, slot, snapshot, expected_disk_older_count=slot._disk_older_count
    )
    archive = sorted((state.conversation_log._dir / "archive").glob("*.jsonl"))
    archived = [p.read_text(encoding="utf-8").splitlines()[1:] for p in archive]
    return {"ok": ok, "file": _file(state, slot), "archived": archived}


def _scenario_refusals(state) -> dict:
    slot = _fresh_slot(state, "golden-refuse")
    _rows(slot, ("user", "q1", 6, {"mid": "m-1"}))
    cp._save_slot_to_history(state, slot, force=True)
    before = _file(state, slot)
    drift = cp._save_slot_to_history(
        state, slot, list(slot.messages), expected_disk_older_count=slot._disk_older_count + 1
    )
    moved = cp._save_slot_to_history(
        state, slot, force=True, expected_history_key="dashboard:somewhere-else"
    )
    slot._dirty = False
    state._slots["golden-refuse"] = object()
    replaced = cp._save_slot_to_history(state, slot, force=True, expected_slot_name="golden-refuse")
    owed = slot._dirty
    state._slots["golden-refuse"] = slot
    return {
        "refused": [drift, moved, replaced],
        "owed_after_recreate_won": owed,
        "unchanged": _file(state, slot) == before,
    }


def _run(name: str, tmp_path, monkeypatch, caplog) -> dict:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    scenario = globals()[f"_scenario_{name}"]
    params = inspect.signature(scenario).parameters
    out = scenario(state, caplog) if "caplog" in params else scenario(state)
    root = str(tmp_path)
    return json.loads(json.dumps(out).replace(root, "<tmp>"))


_SCENARIOS = (
    "full_save",
    "empty_window_merge",
    "foreign_append",
    "rows_only_handover",
    "truncating_rewrite",
    "refusals",
)


@pytest.mark.parametrize("name", _SCENARIOS)
def test_the_save_writes_the_same_bytes(name, tmp_path, monkeypatch, caplog) -> None:
    assert _run(name, tmp_path, monkeypatch, caplog) == _GOLDEN[name]


def test_the_golden_bytes_are_sensitive_to_the_line_fold(tmp_path, monkeypatch, caplog) -> None:
    """Red check: a fold that writes one more field is a byte difference here."""
    real = cp._metadata_line.build_full_line

    def _louder(*args, **kwargs):
        line, *rest = real(*args, **kwargs)
        return ({**line, "extra": True}, *rest)

    monkeypatch.setattr(cp._metadata_line, "build_full_line", _louder)
    assert _run("full_save", tmp_path, monkeypatch, caplog) != _GOLDEN["full_save"]


#: What each scenario wrote before the split (``_run``'s normalized output).
_GOLDEN: dict[str, dict] = {
    "full_save": {
        "file": (
            '{"_type": "metadata", "created_at": "2026-01-02T03:04:05", "last_consolidated": 0, "memory_mode": "persistent", "title": "Golden", "title_origin": "auto", "title_refresh_mark": 3, "title_low_signal": false, "agent": "writer", "model": "model-x", "autocompact_pct": 55.0, "mode": "focus", "project": "/srv/project", "folder_id": "folder-1", "pinned": true, "color_index": 2, "color_hex": "#aabbcc", "tags": ["tag-1"], "last_user_at": "2026-01-02T03:04:06", "tab_id": "tab-golden-full"}\n'
            '{"role": "user", "content": "hello", "ts": "2026-01-02T03:04:06", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-1", "human": true}}\n'
            '{"role": "assistant", "content": "the key is [REDACTED: credential]", "ts": "2026-01-02T03:04:07", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-2"}}\n'
            '{"role": "system", "content": "a notice", "ts": "2026-01-02T03:04:08", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-3"}}\n'
        ),
        "ok": True,
        "window_len": 3,
    },
    "empty_window_merge": {
        "file": (
            '{"_type": "metadata", "created_at": "2026-01-02T03:04:05", "last_consolidated": 0, "title": "New", "rotation_generation": 4, "other_layer": "kept", "folder_id": "folder-2", "tags": ["a", "b"], "pinned": false, "mutes_opened": false, "mode": "", "artifact": "", "reasoning_effort": "", "color_index": null, "color_hex": "", "color_theme": "", "memory_mode": "persistent", "model": "", "queued_prompts": [], "autocompact_pct": null, "title_low_signal": false, "workspace": "default", "memory_store": "", "agent_kind": "", "project": "", "turn_in_flight_generation": 0, "turn_in_flight_prompt": null, "tab_id": "tab-golden-empty", "closed": true, "closed_at": 1767225600.0, "dismissed_source_links": [], "deferred_notes": []}\n'
        ),
        "ok": True,
    },
    "foreign_append": {
        "file": (
            '{"_type": "metadata", "created_at": "2026-01-02T03:04:05", "last_consolidated": 0, "memory_mode": "persistent", "model": "", "autocompact_pct": null, "tab_id": "tab-golden-foreign"}\n'
            '{"role": "user", "content": "first", "ts": "2026-01-02T03:04:06", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-1"}}\n'
            '{"role": "assistant", "content": "from cron", "ts": "2026-01-02T03:04:07", "source_thread": "cron"}\n'
            '{"role": "assistant", "content": "second", "ts": "2026-01-02T03:04:08", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-2"}}\n'
            '{"role": "user", "content": "third", "ts": "2026-01-02T03:04:09", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-3"}}\n'
            '{"role": "assistant", "content": "fourth", "ts": "2026-01-02T03:04:10", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-4"}}\n'
        ),
        "ok": [True, True],
        "warned": [
            ["Slot golden-foreign save found 1 new line(s) another writer appended; keeping 1"],
            [],
        ],
    },
    "rows_only_handover": {
        "file": (
            '{"_type": "metadata", "created_at": "2026-01-02T03:04:05", "last_consolidated": 0, "title": "Theirs", "tab_id": "tab-replacement", "folder_id": "their-folder", "memory_mode": "persistent"}\n'
            '{"role": "user", "content": "unsaved tail", "ts": "2026-01-02T03:04:06", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-1"}}\n'
        ),
        "ok": True,
    },
    "truncating_rewrite": {
        "archived": [
            [
                '{"role": "assistant", "content": "a2 to drop", "ts": "2026-01-02T03:04:08", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-3"}}'
            ]
        ],
        "file": (
            '{"_type": "metadata", "created_at": "2026-01-02T03:04:05", "last_consolidated": 0, "memory_mode": "persistent", "model": "", "autocompact_pct": null, "tab_id": "tab-golden-rewrite", "rotation_generation": 1}\n'
            '{"role": "user", "content": "q1", "ts": "2026-01-02T03:04:06", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-1"}}\n'
            '{"role": "assistant", "content": "a1", "ts": "2026-01-02T03:04:07", "source_thread": "dashboard", "source_user": "dashboard", "meta": {"mid": "m-2"}}\n'
        ),
        "ok": True,
    },
    "refusals": {
        "owed_after_recreate_won": True,
        "refused": [False, False, False],
        "unchanged": True,
    },
}
