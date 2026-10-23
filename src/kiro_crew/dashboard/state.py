"""Dashboard shared state — ChatSlot and DashboardState."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
import weakref
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.loader import (
    DASHBOARD_PORT,
    _raw_config,
    config_dir,
    resolve_effective_agent,
)
from kiro_crew.constants import (  # noqa: F401 -- DENY_CAUSE_* / STEER_NOTICE_BOUND_SECS re-exported
    DENY_CAUSE_APPROVAL_NO_BUDGET,
    DENY_CAUSE_APPROVAL_TIMEOUT,
    DENY_CAUSE_APPROVAL_UNDELIVERABLE,
    DENY_CAUSE_BATCH_CASCADE,
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_INVALID_NAME,
    DENY_CAUSE_POLICY,
    DENY_CAUSE_SURFACE_POLICY,
    OPTIONS_RE_LINE,
    STEER_NOTICE_BOUND_SECS,
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
)
from kiro_crew.dashboard import pinned_session_order
from kiro_crew.dashboard.chat_compaction_notice import deliver_channel_compaction_notice
from kiro_crew.dashboard.chat_tag_grants import seed_default_grants, seed_status_identity_rows
from kiro_crew.dashboard.dashboard_persistence import DashboardPersistenceCoordinator
from kiro_crew.dashboard.folder_repository import FOLDERS_FILE, FolderRepository
from kiro_crew.dashboard.interaction_coordinator import (
    ApprovalCoordinator,
    QuestionCoordinator,
)
from kiro_crew.dashboard.notification_coordinator import NotificationCoordinator
from kiro_crew.dashboard.recovery_replays import RecoveryReplays
from kiro_crew.dashboard.session_pulse_counter import increment_user_session_count_off_loop
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.dashboard.slot_buffers import SlotBufferCoordinator
from kiro_crew.dashboard.slot_projection import SlotProjection
from kiro_crew.dashboard.slot_queue_repository import (
    EMPTY_QUEUE_SIGNATURE,
    SlotQueueRepository,
    durable_queue_entries,
    durable_queue_view,
    queue_persist_signature,
)
from kiro_crew.dashboard.slot_registry import SlotRegistry
from kiro_crew.dashboard.system_notices import is_system_notice
from kiro_crew.dashboard.websocket_hub import SLOT_PATCH_WS_FLAG, WebSocketHub, WsPayload
from kiro_crew.deny_guidance import remediation_for
from kiro_crew.deny_notice import (  # noqa: F401 -- re-exported for dashboard importers
    _DENY_CAUSE_TEXT,
    build_refusal_steer_notice,
    steer_refusal_notice,
)
from kiro_crew.history import (
    MEMORY_MODES,
    latest_transcript_ts,
    mint_row_mid,
    monotonic_transcript_ts,
)
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging import turn_ceiling
from kiro_crew.messaging.link import (
    SLACK_NAMESPACE,
    UNBIND_REASON_DASHBOARD_UNLINK,
    UNBIND_REASON_ENTRY_DELETED,
    UNBIND_REASON_ORIGIN_REBIND,
    UNBIND_REASON_PRUNED_STALE,
    UNBIND_REASON_SESSION_DESTROYED,
    UNBIND_REASON_UNSPECIFIED,
    UNBIND_REASON_USER_UNLINK,
    ChannelLink,
    binding_token,
    channel_namespace_of,
    is_channel_session_key,
    split_namespaced_channel_id,
)
from kiro_crew.messaging.renderer import display_safe
from kiro_crew.notifications.bus import (
    NotificationBus,
    NotificationValidationError,
    normalize_note,
    payload_from_legacy,
)
from kiro_crew.notifications.rate_limit import AppRateLimiter
from kiro_crew.notifications.resource_pressure import ResourcePressureNotifier
from kiro_crew.notifications.settings import ChannelSettings
from kiro_crew.owner_only_files import mkdirs_owner_only, owner_only_opener
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.release_channel import channel as _release_channel_of_build
from kiro_crew.safety_override import cached_disabled_approval_modes, safety_override
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.security.credential_sources import CredentialEvidence
from kiro_crew.sel import sel
from kiro_crew.session_compaction import (
    COMPACT_OUTCOME_CANCELLED,
    COMPACT_OUTCOME_COMPACTED,
    COMPACT_OUTCOME_RECYCLED,
    COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE,
    COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard._types import (  # noqa: F401
        ContextBuilder,
        ConversationLog,
        CronService,
        HistoryConsolidator,
        LessonStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
    )
    from kiro_crew.dashboard.listener_guard import ListenerGuard  # noqa: F401
    from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog  # noqa: F401
    from kiro_crew.messaging.transport import MessagingTransport  # noqa: F401
    from kiro_crew.power import SleepInhibitor  # noqa: F401
    from kiro_crew.slack.outbound import PostedOptions  # noqa: F401
    from kiro_crew.subagent import SubagentDelivery  # noqa: F401
    from kiro_crew.trust_patterns import GrantDeadline  # noqa: F401

logger = logging.getLogger(__name__)


# Every name the owners in state_parts/ define is re-exported here.
import importlib as _importlib  # noqa: E402
import sys as _sys  # noqa: E402

from kiro_crew import _facade_mirror  # noqa: E402
from kiro_crew.dashboard.state_parts.chat_slot import (  # noqa: E402,F401 -- re-exported; see state_parts
    _ChatSlot,
)
from kiro_crew.dashboard.state_parts.dashboard_services import (  # noqa: E402,F401 -- re-exported; see state_parts
    _approvals_for,
    _new_dashboard_persistence,
    _new_notification_coordinator,
    _new_websocket_hub,
    _notifications_for,
    _permission_marker,
    _persistence_for,
    _questions_for,
    _registry_for,
    _websocket_for,
)
from kiro_crew.dashboard.state_parts.dashboard_state import (  # noqa: E402,F401 -- re-exported; see state_parts
    _DEFAULT_PORT,
    _SSE_INTERVAL_SECS,
    DashboardState,
    set_build_info,
)
from kiro_crew.dashboard.state_parts.injected_turns import (  # noqa: E402,F401 -- re-exported; see state_parts
    _REDIRECT_TARGET_RE,
    BUSY_RECOVERY_PREFIX,
    COMPACTION_RECOVERY_PREFIX,
    CONN_RECOVERY_PREFIX,
    CRON_NOTIFY_END,
    CRON_NOTIFY_PREFIX,
    CRON_NOTIFY_RE,
    EMPTY_RESPONSE_RECOVERY_PREFIX,
    HOOK_CONTINUATION_RECOVERY_PREFIX,
    HOOK_HALTED_RECOVERY_PREFIX,
    MANUAL_RESUME_RECOVERY_PREFIX,
    MONITOR_WAKE_PREFIX,
    POSTTOKEN_RECOVERY_PREFIX,
    PROMISE_ONLY_RECOVERY_PREFIX,
    REFUSAL_FALLBACK_RECOVERY_PREFIX,
    REFUSAL_INBAND_RECOVERY_PREFIX,
    REFUSAL_RECOVERY_PREFIX,
    STALE_RECOVERY_PREFIX,
    SUBAGENT_COMPLETION_PREFIXES,
    SUBAGENT_SYNTHESIS_PREFIX,
    SUBAGENT_SYNTHESIS_PROMPT,
    TOOL_STALL_RECOVERY_PREFIX,
    build_infra_retry_prompt,
    build_refusal_recovery_prompt,
    build_stale_recovery_prompt,
    build_tool_stall_recovery_prompt,
    extract_log_redirect_target,
    parse_hook_continuations,
    should_queue_hook_continuation,
    should_queue_refusal_recovery,
)
from kiro_crew.dashboard.state_parts.notification_log import (  # noqa: E402,F401 -- re-exported; see state_parts
    _MAX_PERSISTED_NOTIFICATIONS,
    _NOTIFICATIONS_FILE,
    _UNSERVABLE_NOTIFICATION_CAP_DIVISOR,
    _load_notifications,
    _maybe_trim_notifications,
    _note_ts_epoch,
    _notification_io_executor,
    _notification_io_pool_lock,
    _notifications_path,
    _persist_notification,
    _read_notification_lines,
    _redact_note_value,
    _rewrite_notifications,
    _servable_note,
    sweep_expired_notifications,
)
from kiro_crew.dashboard.state_parts.session_events import (  # noqa: E402,F401 -- re-exported; see state_parts
    _AUTO_COMPACT_CANCELLED_NOTICE,
    _AUTO_COMPACT_FAILED_NOTICE,
    _AUTO_COMPACT_NOTICE,
    _AUTO_COMPACT_WAITING_NOTICE,
    _AUTO_RECYCLE_NOTICE,
    _AUTO_RESTART_UNCOMPACTABLE_NOTICE,
    _INBOUND_UNBIND_NOTICE,
    _INBOUND_UNBIND_WHY,
    _INBOUND_UNBIND_WHY_DEFAULT,
    _RESTART_MEMORY_TAIL,
    _SESSION_RECYCLED_NOTICE,
    _STUCK_TURN_NOTICE,
    _record_crew_log_class,
    note_crew_log_class,
    stuck_turn_notice,
)
from kiro_crew.dashboard.state_parts.sidebar_stores import (  # noqa: E402,F401 -- re-exported; see state_parts
    _FOLDER_REPOSITORY,
    _T,
    _DurableTagSnapshot,
    _safe_folder_tree,
)
from kiro_crew.dashboard.state_parts.slot_directory import (  # noqa: E402,F401 -- re-exported; see state_parts
    _CHAT_N_RE,
    _SLOT_KEY_FILENAME_UNSAFE_RE,
    _SLOT_KEY_HASH_CHARS,
    _SLOT_KEY_KEEP_CHARS,
    _SLOT_KEY_MAX_CHARS,
    MAX_LIVE_SLOTS,
    MAX_SLOTS_PER_CREATOR,
    NATIVE_SUBAGENT_DONE_RESULT_CAP,
    NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
    NATIVE_SUBAGENT_OUTPUT_HARD,
    NATIVE_SUBAGENT_OUTPUT_TAIL,
    NATIVE_SUBAGENT_TERMINAL_KEEP,
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    SlotOrigin,
    _ascii_slot_key,
    _is_genuine_slack_link,
    _mint_slot_key,
    _normalize_slot_key,
    _slot_index_from_key,
    native_subagent_output_tail,
    request_slot_origin,
)
from kiro_crew.dashboard.state_parts.slot_list_hold import (  # noqa: E402,F401 -- re-exported; see state_parts
    _SLOT_LIST_PARTIAL_DEADLINE_S,
)
from kiro_crew.dashboard.state_parts.slot_session import (  # noqa: E402,F401 -- re-exported; see state_parts
    _SLOT_KEY_TITLE_RE,
    NEW_SESSION_TITLE,
    VALID_MEMORY_MODES,
    CrewLogPrevious,
)
from kiro_crew.dashboard.state_parts.slot_sidebar import (  # noqa: E402,F401 -- re-exported; see state_parts
    _TAGS_REVISION_EPOCH_FILE,
    _TAGS_REVISION_EPOCH_UNPERSISTED,
    _TAGS_REVISION_SEQ_LOCK,
    _claim_tags_revision_epoch,
    ensure_tags_revision_epoch,
    mint_tags_revision,
)
from kiro_crew.dashboard.state_parts.slot_todo import (  # noqa: E402,F401 -- re-exported; see state_parts
    _fold_line_breaks,
    _todo_canonical_text,
)
from kiro_crew.dashboard.state_parts.slot_transcript import (  # noqa: E402,F401 -- re-exported; see state_parts
    _LAZY_WINDOW_LOCK,
    _MAX_SLOT_MESSAGES,
    _OPTIONS_RE,
    _PROMPT_ROLES,
    _QUESTION_RETIRING_ROLES,
    _on_event_loop,
    _parse_options,
    _redact,
    load_window_off_loop,
)
from kiro_crew.dashboard.state_parts.slot_turns import (  # noqa: E402,F401 -- re-exported; see state_parts
    _MAX_PENDING_CONTEXT,
    _MAX_PENDING_SUBAGENT_DELIVERIES,
    PERSISTED_SUBAGENT_REPLAY_KEEP,
    PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
    _delivery_key,
    _log_task_exception,
    _note_authorized_elsewhere,
    context_entry_expired,
)
from kiro_crew.dashboard.state_parts.slots_frame import (  # noqa: E402,F401 -- re-exported; see state_parts
    _CHANNEL_LABELS,
    _SLOTS_BROADCAST_BYTES_PER_S,
    _SLOTS_BROADCAST_INTERVAL_S,
    _SLOTS_BROADCAST_MAX_INTERVAL_S,
    _attach_slot_parents,
    _expected_binding,
    _lineage_seed_lock,
    _link_label,
    _mirror_link_nonce,
    _published_queued_by_slot,
    _redacted_link_target,
    _request_lineage_seed,
    _slack_link_nonce,
    _slots_broadcast_interval_for,
    _slots_serialization_note,
    _slots_ws_frame,
)
from kiro_crew.dashboard.state_parts.slots_push import (  # noqa: E402,F401 -- re-exported; see state_parts
    _DEFERRED_SLOTS_FLUSH_DELAY_S,
    _SLOT_PATCH_FIELDS,
    _new_card_store,
    _slots_broadcast_clock,
)
from kiro_crew.dashboard.state_parts.source_links import (  # noqa: E402,F401 -- re-exported; see state_parts
    _CHIP_STATUS_KEYS,
    _MAX_DISMISSED_SOURCE_LINKS,
    _MAX_SOURCE_LINKS_PER_SLOT,
    _NON_DURABLE_SOURCE_LINK_ROLES,
    _PUBLIC_STATUS_DENY_AUDIT,
    _PUBLIC_STATUS_GRANT_AUDIT,
    _PUBLIC_STATUS_GRANT_WINDOW_SECS,
    _SERIALIZED_SOURCE_LINKS_PER_SLOT,
    _SOURCE_ACTIVE_WINDOW_SECS,
    _audit_public_status_denied,
    _audit_public_status_grant,
    _budgeted_source_links,
    _cached_check_status,
    _drop_expired_public_status_audits,
    _project_source_links,
    _repo_is_public,
    _source_links_by_kind,
)
from kiro_crew.dashboard.state_parts.status_payload import (  # noqa: E402,F401 -- re-exported; see state_parts
    _BUNDLE_ID_CACHE,
    _SERVED_INDEX,
    _fmt_duration,
    _governance_status,
)
from kiro_crew.dashboard.state_parts.transcript_rows import (  # noqa: E402,F401 -- re-exported; see state_parts
    _TRANSIENT_ROLES,
    _TURN_CONTINUING_INJECT_KINDS,
    _TURN_INJECT_KINDS,
    _WIRE_ONLY_ROLES,
    QUIET_END_SERVER,
    QUIET_END_TOOL,
    TURN_OPENING_INJECT_KINDS,
    _compaction_keep_record,
    _is_turn_inject,
    _mark_permission_resolved,
    append_and_surface,
    chat_message_frame,
    chat_message_note,
    durable_row_count,
    is_quiet_end_row,
    is_stop_event_row,
    is_turn_interrupted,
    parse_cls_meta,
    row_mid,
)

# The owners in state_parts/ hold the moved code; this module stays the import
# path and the patch surface (see state_parts/__init__.py).
#: BASE imports this module binds for its importers and for the patches the mirror
#: routes through it, though its own code does not read them.
_BOUND_FOR_IMPORTERS = (
    Any,
    AppRateLimiter,
    ApprovalCoordinator,
    Awaitable,
    COMPACT_OUTCOME_CANCELLED,
    COMPACT_OUTCOME_COMPACTED,
    COMPACT_OUTCOME_RECYCLED,
    COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE,
    COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS,
    Callable,
    ChannelLink,
    ChannelSettings,
    CredentialEvidence,
    DASHBOARD_PORT,
    DashboardPersistenceCoordinator,
    EMPTY_QUEUE_SIGNATURE,
    FOLDERS_FILE,
    FileTooLargeError,
    FolderRepository,
    Iterable,
    Iterator,
    KnowledgeStore,
    LoopBoundLock,
    MEMORY_MODES,
    NotificationBus,
    NotificationCoordinator,
    NotificationValidationError,
    Path,
    QuestionCoordinator,
    RecoveryReplays,
    ResourcePressureNotifier,
    SLACK_NAMESPACE,
    SLOT_PATCH_WS_FLAG,
    SideState,
    SlotBufferCoordinator,
    SlotProjection,
    SlotQueueRepository,
    SlotRegistry,
    UNBIND_REASON_DASHBOARD_UNLINK,
    UNBIND_REASON_ENTRY_DELETED,
    UNBIND_REASON_ORIGIN_REBIND,
    UNBIND_REASON_PRUNED_STALE,
    UNBIND_REASON_SESSION_DESTROYED,
    UNBIND_REASON_UNSPECIFIED,
    UNBIND_REASON_USER_UNLINK,
    WebSocketHub,
    WsPayload,
    _raw_config,
    _release_channel_of_build,
    asyncio,
    atomic_write,
    binding_token,
    cached_disabled_approval_modes,
    channel_namespace_of,
    config_dir,
    contextlib,
    datetime,
    deliver_channel_compaction_notice,
    display_safe,
    durable_queue_entries,
    durable_queue_view,
    fsync_dir,
    hashlib,
    increment_user_session_count_off_loop,
    is_channel_session_key,
    is_system_notice,
    json,
    latest_transcript_ts,
    mint_row_mid,
    mkdirs_owner_only,
    monotonic_transcript_ts,
    normalize_note,
    os,
    owner_only_opener,
    payload_from_legacy,
    pinned_session_order,
    queue_persist_signature,
    redact_credentials,
    redact_exfiltration_urls,
    remediation_for,
    resolve_effective_agent,
    safe_read_file_bytes_nolink,
    safety_override,
    seed_default_grants,
    seed_status_identity_rows,
    sel,
    split_namespaced_channel_id,
    strip_markdown_preview,
    sys,
    threading,
    time,
    turn_ceiling,
    uuid,
    weakref,
    web,
)
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.state_parts.transcript_rows",
    "kiro_crew.dashboard.state_parts.injected_turns",
    "kiro_crew.dashboard.state_parts.notification_log",
    "kiro_crew.dashboard.state_parts.source_links",
    "kiro_crew.dashboard.state_parts.slot_sidebar",
    "kiro_crew.dashboard.state_parts.slot_todo",
    "kiro_crew.dashboard.state_parts.slot_session",
    "kiro_crew.dashboard.state_parts.slot_turns",
    "kiro_crew.dashboard.state_parts.slot_ladder",
    "kiro_crew.dashboard.state_parts.slot_transcript",
    "kiro_crew.dashboard.state_parts.chat_slot",
    "kiro_crew.dashboard.state_parts.dashboard_services",
    "kiro_crew.dashboard.state_parts.session_events",
    "kiro_crew.dashboard.state_parts.status_payload",
    "kiro_crew.dashboard.state_parts.slot_directory",
    "kiro_crew.dashboard.state_parts.sidebar_stores",
    "kiro_crew.dashboard.state_parts.slots_frame",
    "kiro_crew.dashboard.state_parts.slots_push",
    "kiro_crew.dashboard.state_parts.slot_list_hold",
    "kiro_crew.dashboard.state_parts.dashboard_state",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Globals one owner rebinds with ``global``: not bound here, served live from it.
_SERVED_LIVE = (
    "_TAGS_REVISION_EPOCH",
    "_TAGS_REVISION_SEQ",
    "_build_info",
    "_lineage_failure_warned",
    "_lineage_seed_in_flight",
    "_notification_io_pool",
)
#: Indexed name the facade does not bind -> the owner that answers a read of it.
#: The surface is every name this module binds plus those globals: a helper the
#: split introduced in an owner is not part of the facade.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(
        _sys.modules[__name__], _OWNERS, excluded=(), surface={*globals(), *_SERVED_LIVE}
    ).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.state_parts.dashboard_state import (  # noqa: F401
        _build_info,
    )
    from kiro_crew.dashboard.state_parts.notification_log import (  # noqa: F401
        _notification_io_pool,
    )
    from kiro_crew.dashboard.state_parts.slot_sidebar import (  # noqa: F401
        _TAGS_REVISION_EPOCH,
        _TAGS_REVISION_SEQ,
    )
    from kiro_crew.dashboard.state_parts.slots_frame import (  # noqa: F401
        _lineage_failure_warned,
        _lineage_seed_in_flight,
    )
