"""HTTP API handlers for dashboard chat endpoints."""

from __future__ import annotations

import asyncio
import contextlib  # noqa: F401
import json
import logging
import math  # noqa: F401
import os  # noqa: F401
import tempfile  # noqa: F401
import threading
import time  # noqa: F401
import uuid  # noqa: F401
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from itertools import islice  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import TYPE_CHECKING, Any, NamedTuple  # noqa: F401

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError  # noqa: F401

from kiro_crew import members as members_mod  # noqa: F401
from kiro_crew import model_registry  # noqa: F401
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew.acp.client import AcpModelUnavailable
from kiro_crew.agent_discovery import (  # noqa: F401
    cached_project_agent_names,
    warm_project_agent_names,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS  # noqa: F401
from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP, capabilities_of  # noqa: F401
from kiro_crew.agent_sdk.provider_identity import is_claude_code  # noqa: F401
from kiro_crew.agent_switch_command import switch_announcement  # noqa: F401
from kiro_crew.atomic_write import atomic_write  # noqa: F401
from kiro_crew.config.loader import (  # noqa: F401
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CHAT_TURN_TIMEOUT_MAX,
    CHAT_TURN_TIMEOUT_MIN,
    KiroCrewConfig,
    _workspace_name_for_dir,
    config_dir,
    crew_autocompact_pct,
    default_project_dir,
    default_template,
    dispatch_kiro_agent,
    published_autocompact_pct,
    resolve_agent_bindings,
)
from kiro_crew.config.sections import (  # noqa: F401 -- read by chat_api.slot_settings
    AutocompactPctError,
    parse_autocompact_pct,
)
from kiro_crew.dashboard import display_verdicts  # noqa: F401 -- read by chat_api.slot_detail
from kiro_crew.dashboard.approval_posture import trust_carries  # noqa: F401
from kiro_crew.dashboard.body_numbers import body_int  # noqa: F401
from kiro_crew.dashboard.channel_slots import channel_slot_name, note_slot_closed  # noqa: F401
from kiro_crew.dashboard.chat_api import agent_switch as _owner_agent_switch
from kiro_crew.dashboard.chat_api import app_admission as _owner_app_admission
from kiro_crew.dashboard.chat_api import chat_send as _owner_chat_send
from kiro_crew.dashboard.chat_api import continue_turn as _owner_continue_turn
from kiro_crew.dashboard.chat_api import followup_card as _owner_followup_card
from kiro_crew.dashboard.chat_api import handover_drain as _owner_handover_drain
from kiro_crew.dashboard.chat_api import pending_context as _owner_pending_context
from kiro_crew.dashboard.chat_api import project_lists as _owner_project_lists
from kiro_crew.dashboard.chat_api import prompt_queue as _owner_prompt_queue
from kiro_crew.dashboard.chat_api import resume as _owner_resume
from kiro_crew.dashboard.chat_api import session_relaunch as _owner_session_relaunch
from kiro_crew.dashboard.chat_api import slot_approval as _owner_slot_approval
from kiro_crew.dashboard.chat_api import slot_create as _owner_slot_create
from kiro_crew.dashboard.chat_api import slot_detail as _owner_slot_detail
from kiro_crew.dashboard.chat_api import slot_effort as _owner_slot_effort
from kiro_crew.dashboard.chat_api import slot_guards as _owner_slot_guards
from kiro_crew.dashboard.chat_api import slot_lifecycle as _owner_slot_lifecycle
from kiro_crew.dashboard.chat_api import slot_model as _owner_slot_model
from kiro_crew.dashboard.chat_api import slot_note as _owner_slot_note
from kiro_crew.dashboard.chat_api import slot_settings as _owner_slot_settings
from kiro_crew.dashboard.chat_api import slot_summary as _owner_slot_summary
from kiro_crew.dashboard.chat_api import source_links as _owner_source_links
from kiro_crew.dashboard.chat_api import turn_stop as _owner_turn_stop
from kiro_crew.dashboard.chat_api.agent_switch import (  # noqa: F401
    CREW_MODE_LOCKED,
    SlotAgentSwitchCaller,
    _append_session_switch_notice,
    _crew_mode_lock_refusal,
    _has_user_message,
    _MemberMemoryRequiresNewConversation,
    agent_switch_outcome,
    api_chat_slot_agent,
    clear_slot_model_binding,
    switch_slot_agent,
)
from kiro_crew.dashboard.chat_api.app_admission import (  # noqa: F401
    _acquired_slot_was_judged,
    _app_acquisition_conflict,
    _app_cancel_denied,
    _app_claim_refused,
    _app_may_send_to_slot,
    _app_resume_refusal,
    _app_slot_acquisition_denial,
    _app_slot_acquisition_recheck,
    _deny_app_session_settings,
    _deny_app_yolo,
    _reauthorize_after_await,
    _slot_not_found,
)
from kiro_crew.dashboard.chat_api.chat_send import (  # noqa: F401
    _CREW_PERSON_STEER,
    _HELD_CREW_STEERS,
    CREW_STEER_QUEUE_KEY,
    _api_chat_send,
    _committed_if_accepted,
    _composer_locked_answer,
    _person_crew_worker_root,
    _send_binds_agent,
    _send_harness_command,
    _send_id_of,
    _send_writes_persona,
    _settle_send_race,
    _sweep_stale_permissions,
    _take_held,
    api_chat,
    crew_steer_consumed,
    crew_steer_discarded,
    crew_steer_requeued,
    decided_message_handling,
    hold_crew_steer,
    mark_crew_steer,
    steer_is_auto,
    write_sse_or_abort,
)
from kiro_crew.dashboard.chat_api.continue_turn import (  # noqa: F401
    SlotContinueRefusal,
    _is_interrupted,
    _is_stop_event,
    api_chat_slot_continue,
    api_chat_slot_end_wait,
    continue_slot_turn,
    session_start_failure_streak,
)
from kiro_crew.dashboard.chat_api.followup_card import (  # noqa: F401
    _redact_followup_item,
    api_chat_slot_followup,
)
from kiro_crew.dashboard.chat_api.handover_drain import (  # noqa: F401
    _HandoverDrainResult,
    _owed_prompts_lost_on_line,
    _persist_handover_tail,
    _report_lost_queued_prompts,
)
from kiro_crew.dashboard.chat_api.pending_context import (  # noqa: F401
    _build_pending_context_entry,
    _enqueue_pending_context,
    _is_secret_shaped_context,
    _normalize_source,
    _refuse_secret_shaped_context,
    _source_cap_reached,
    _validate_content,
    _validate_max_age,
    _validate_source,
    api_chat_slot_context,
)
from kiro_crew.dashboard.chat_api.project_lists import (  # noqa: F401
    FavoritesUnreadable,
    _add_favorite_project,
    _favorite_projects_path,
    _favorites_unreadable_response,
    _load_favorite_projects,
    _probe_favorite_candidate,
    _read_favorite_projects,
    _recent_projects_path,
    _remove_favorite_project,
    _save_recent_project,
    _write_favorite_projects,
    api_favorite_project_add,
    api_favorite_project_remove,
    api_favorite_projects,
    api_recent_projects,
)
from kiro_crew.dashboard.chat_api.prompt_queue import (  # noqa: F401
    api_chat_slot_queue_cancel,
    api_chat_slot_queue_edit,
    api_chat_slot_queue_reorder,
    api_chat_slot_steer_retract,
)
from kiro_crew.dashboard.chat_api.resume import (  # noqa: F401
    _hydrate_slot_from_history,
    _live_slot_for_resume,
    _live_slot_resume_payload,
    _materialise_slot_from_history,
    _normalise_structured_content,
    _reconcile_slot_window,
    _redact_history_rows,
    _resume_refusal_response,
    _resume_session_identity,
    api_chat_slot_resume,
    resume_slot_from_history,
)
from kiro_crew.dashboard.chat_api.session_relaunch import (  # noqa: F401
    api_chat_slot_project,
    api_chat_slot_reload,
    api_chat_slot_workspace,
    reload_slot_session,
)
from kiro_crew.dashboard.chat_api.slot_approval import (  # noqa: F401
    _approval_target_required,
    _deny_approval_mode,
    _deny_trust_pattern,
    _elicitation_not_an_approval,
    _end_trust_scope,
    _end_trust_scopes,
    _get_pattern_from_pending,
    _grant_deadline,
    _internal_session_approval_refusal,
    _pending_elicitation_ids,
    _still_owning,
    api_chat_mode,
    api_chat_slot_approve,
    apply_approval_mode,
    bare_approval_decide_allowed,
    deny_session_approval_caller,
)
from kiro_crew.dashboard.chat_api.slot_create import (  # noqa: F401
    _birth_default_project,
    _check_new_slot_assignment,
    _created_slot_row,
    _default_template_stamp,
    _persist_app_agent_owner,
    _retract_failed_newborn,
    api_chat_slot_create,
)
from kiro_crew.dashboard.chat_api.slot_detail import (  # noqa: F401
    _append_unflushed_tail,
    _append_unflushed_tail_from_offset,
    _bounded_slot_page,
    _context_reading,
    _context_snapshot_fields,
    _context_snapshot_fields_inner,
    _durable_prefix_counter,
    _DurablePrefixMismatch,
    _finite_number,
    _is_answered_permission,
    _load_redacted,
    _note_owner_focus,
    _same_persisted_body,
    _snapshot_slot_window,
    api_chat_slot_detail,
    api_chat_slots,
    api_chat_slots_unrestored,
)
from kiro_crew.dashboard.chat_api.slot_effort import (  # noqa: F401
    _configured_backend_for_slot,
    api_chat_slot_reasoning_effort,
    api_chat_slot_selection_capabilities,
)
from kiro_crew.dashboard.chat_api.slot_guards import (  # noqa: F401
    _CommitToken,
    _record_explicit_agent_selection,
    _reset_slot_session_or_warn,
    _slot_replaced_while_queued,
    _subagents_attached_detail,
    _subagents_attached_response,
    _switch_target_busy,
)
from kiro_crew.dashboard.chat_api.slot_lifecycle import (  # noqa: F401
    _await_guarded_history_write,
    _close_slot,
    _drain_file_changes_flush,
    _NudgeRetireFailed,
    _pending_guarded_history_writes,
    _release_closed_execution,
    _restore_slot_nudge_loop,
    _retire_slot_nudge_loop,
    _slot_still_ours,
    api_chat_slot_delete,
    api_chat_slot_repair_images,
    api_chat_slot_reset_conversation,
    api_chat_slots_cleanup,
    close_slot,
)
from kiro_crew.dashboard.chat_api.slot_model import (  # noqa: F401
    _broadcast_context_reset,
    _is_jev_route_pick,
    _model_rejected_reason,
    _reapply_effort_after_live_switch,
    _try_live_model_switch,
    _wire_model_id,
    api_chat_slot_model,
    api_chat_slots_model,
)
from kiro_crew.dashboard.chat_api.slot_note import (  # noqa: F401
    _discard_held_note,
    _note_already_durable,
    _note_delivered_live,
    _persist_deferred_note_hold,
    api_chat_slot_note,
)
from kiro_crew.dashboard.chat_api.slot_settings import (  # noqa: F401
    _autocompact_txn_lock,
    _slot_agent_autocompact_fields,
    _slot_harness_managed_backend,
    _slot_next_agent_autocompact_default,
    _turn_timeout_body_value,
    _turn_timeout_payload,
    api_chat_slot_autocompact,
    api_chat_slot_color,
    api_chat_slot_turn_timeout,
)
from kiro_crew.dashboard.chat_api.slot_summary import (  # noqa: F401
    _generate_state,
    api_chat_slot_summary,
    api_chat_slot_summary_generate,
)
from kiro_crew.dashboard.chat_api.source_links import (  # noqa: F401
    _apply_source_link_unlink,
    _audit_source_link_unlink,
    _source_link_txn_lock,
    api_chat_slot_source_link_unlink,
    api_chat_slot_source_links,
)
from kiro_crew.dashboard.chat_api.turn_stop import (  # noqa: F401
    _cancel_target,
    _compaction_in_flight,
    _make_stop_resolver,
    _open_stop_event_card,
    _rearm_stop_event,
    _reject_pending_approvals,
    _resolve_stop_event,
    _settle_discarded_stage_deliveries,
    api_chat_slot_interrupt,
    api_chat_slot_stop,
    stop_slot_turn,
)
from kiro_crew.dashboard.chat_auto_tag import maybe_auto_tag  # noqa: F401
from kiro_crew.dashboard.chat_delivery import (  # noqa: F401
    STEER_REQUEUED,
    STEER_STEERED,
    TURN_ACTOR_META_KEY,
    attachment_meta,
    normalize_send_id,
    queue_entry_is_user_origin,
    queue_entry_view,
    queue_for_next_turn,
    queued_text_for_display,
    quote_meta,
    start_queue_persist,
    steer_into_running_turn,
)
from kiro_crew.dashboard.chat_folders import (  # noqa: F401
    _unhide_folder,
    resolve_folder_project_dir_off_loop,
)
from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
    _FLUSH_SNAPSHOT_RETRIES,
    _TRANSIENT_ROLES,
    COLOR_HEX_RE,
    _attach_variants,
    _coerce_requested_mode,
    _has_validated_effort_marker,
    _load_restore_cfg,
    _local_turn_generation,
    _local_turn_prompt,
    _rebase_rehydrated_refresh_mark,
    _reconcile_local_turn_marker,
    _rehydrate_slot_title,
    _remember_reasoning_effort_for_restore,
    _restore_dismissed_source_links,
    _restore_model_fields,
    _restored_agent_name,
    _restored_mode,
    _validate_autocompact_pct,
    cap_effort_capability_levels,
    get_reasoning_effort_ordered,
    get_reasoning_effort_values,
    pin_private_agent_store,
    register_reasoning_effort_values,
    release_prewarmed_session,
    remember_unknown_row_fields,
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_runner import (  # noqa: F401
    _context_usage_payload,
    _run_chat,
    _start_next_queued_turn,
    _sync_served_model,
    context_entry_expired,
    schedule_eager_spawn,
    subagents_hold_user_messages,
)
from kiro_crew.dashboard.chat_slack import maybe_auto_link_slack, slot_is_live  # noqa: F401
from kiro_crew.dashboard.chat_summary import (  # noqa: F401
    generate_session_summary,
    read_cached_intent_summary,
)
from kiro_crew.dashboard.chat_tags import (  # noqa: F401
    _bump_slot_tags_revision,
    tags_write_lock,
    validate_folder_tag_ids,
)
from kiro_crew.dashboard.chat_title import _maybe_auto_title  # noqa: F401
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    _MANUAL_CONTINUE_MSG,
    _MANUAL_RESUME_MSG,
    SESSION_START_FAILED_KIND,
    SLOT_DETAIL_MAX_LIMIT,
    SYNTHETIC_RECOVERY_KIND,
    TURN_END_WIRE_CLS,
    _broadcast_expired_oauth_banners,
    _build_stream_chunk,
    _collapse_wire_rows,
    _edit_queued_by_id,
    _emit_agent_assignment,
    _history_key_for,
    _live_child_instance,
    _normalize_model,
    _prepare_messages,
    _redact_for_display,
    _redact_meta,
    _redact_meta_for_role,
    _remove_queued_by_id,
    _resettle_restricted_key,
    _sync_dashboard_slots,
    chunk_generation,
    drained_to_thread,
    effective_session_key,
    history_corpus_unreadable,
    is_harness_slash_command,
)
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    replacement_shares_transcript as _replacement_shares_transcript,
)
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    restore_replacement_if_handover_did_not_land,
    slot_history_key,
    slot_transcript_key,
    subagents_attached_async,
    tighten_live_slot_memory_mode,
)
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    tighten_replacement_to_restricted_original as _tighten_replacement_to_restricted_original,
)
from kiro_crew.dashboard.default_session_folder import (  # noqa: F401
    resolve_default_session_folder,
)
from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
    _SLOT_SCOPED_TRUST_MODES,
    _owner_denial_response,
    cron_creator_admission,
    cron_mode_refusal,
    cron_slot_creator,
    read_bounded_json,
)
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request  # noqa: F401
from kiro_crew.dashboard.relay_archive import relay_archive_refusal  # noqa: F401
from kiro_crew.dashboard.request_priority import owner_start_priority  # noqa: F401
from kiro_crew.dashboard.session_folders import (  # noqa: F401
    SessionFolderError,
    create_session_folder,
)
from kiro_crew.dashboard.slot_buffers import (  # noqa: F401
    MAX_DEFERRED_NOTE_CHARS,
    MAX_DEFERRED_NOTES,
    MAX_SOURCE_LABEL_LEN,
    SOURCE_LABEL_CTRL_RE,
    DeferredHoldFull,
    DeferredHoldRebound,
    note_hold_durable,
    persist_deferred_notes_sync,
)
from kiro_crew.dashboard.slot_color import write_slot_color  # noqa: F401
from kiro_crew.dashboard.slot_ownership import (  # noqa: F401
    SESSION_CONTROL_DENIED,
    app_may_control_session,
    app_new_key_refusal,
    app_owns_slot_session,
    app_owns_transcript_meta,
    app_slot_is_local_user_session,
    audit_app_slot_denial,
    deny_app_session_control,
    deny_app_slot_access,
    deny_app_slot_session_access,
    own_session_key,
    read_session_grant,
    session_grant,
    slot_not_found,
    transcript_acquisition_reason,
)
from kiro_crew.dashboard.slot_projection import (  # noqa: F401
    resolved_row_identity,
    stop_declined_armed,
)
from kiro_crew.dashboard.slot_queue_repository import warn_if_not_durable  # noqa: F401
from kiro_crew.dashboard.slot_retention import (  # noqa: F401
    loop_slot_keys,
    select_idle_slot_keys,
)
from kiro_crew.dashboard.state import (  # noqa: F401
    _MAX_DISMISSED_SOURCE_LINKS,
    DashboardState,
    _ChatSlot,
    _mark_permission_resolved,
    _normalize_slot_key,
    _slots_serialization_note,
    chat_message_frame,
    durable_row_count,
    is_stop_event_row,
    is_turn_interrupted,
    load_window_off_loop,
    note_crew_log_class,
    parse_cls_meta,
    request_slot_origin,
    row_mid,
)
from kiro_crew.dashboard.system_notices import (  # noqa: F401
    SESSION_RELOAD_KIND,
    SESSION_SWITCH_KIND,
    is_system_notice,
    session_switch_notice,
)
from kiro_crew.dashboard.token_auth import folder_principal  # noqa: F401
from kiro_crew.dashboard.turn_dispatch import (  # noqa: F401
    chat_turn_timeout_secs,
    spawn_guarded_turn,
)
from kiro_crew.history import (  # noqa: F401
    HUMAN_TURN_META_KEY,
    carry_provenance,
    is_incognito_transcript,
)
from kiro_crew.history_projection import TranscriptRevisionChanged  # noqa: F401
from kiro_crew.jsonl_util import OversizedRecord, SplitlinesBoundaryRecord  # noqa: F401
from kiro_crew.llm_helpers import (  # noqa: F401
    pick_epoch_host,
    provider_model_pin_refused,
    slot_switch_session_lock,
)
from kiro_crew.memory_startup import (  # noqa: F401
    MemoryStartupUnavailable,
    wait_for_memory_preparation,
)
from kiro_crew.memory_stores import UnknownMemoryStore  # noqa: F401
from kiro_crew.messaging.link import canonical_key, is_channel_session_key  # noqa: F401
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider
from kiro_crew.safety_override import (  # noqa: F401
    approval_mode_permitted,
    safety_override,
    yolo_policy_permits,
)
from kiro_crew.sandbox import voice_runtime_workspace_conflict  # noqa: F401
from kiro_crew.security import (  # noqa: F401
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.session_agent_selection import (  # noqa: F401
    SelectionChange,
    record_agent_selection,
    resolve_session_agent_bindings,
    restore_agent_selection,
    session_agent_selection_name,
)
from kiro_crew.session_lifecycle import compaction_in_flight  # noqa: F401
from kiro_crew.session_summary import count_user_turns_in_records  # noqa: F401
from kiro_crew.start_priority import person_priority  # noqa: F401
from kiro_crew.trust_patterns import (  # noqa: F401
    GrantDeadline,
    base_consent_pattern,
    base_trust_patterns,
    exact_trust_pattern,
    grant_deadline,
    parse_grant_ttl,
    record_trust_grant,
)
from kiro_crew.validation import (  # noqa: F401
    ARTIFACT_SLUG_RE,
    SUGGEST_FOLLOWUP_SCHEMA,
    ValidationError,
    is_registered_agent_name,
    normalize_theme_consent_sha,
    validate_tool_args,
)

if TYPE_CHECKING:  # circular at runtime: autonudge -> dashboard.chat -> chat_handlers
    from kiro_crew.autonudge import NudgeLoop  # noqa: F401
    from kiro_crew.config.sections import ResolvedBindings  # noqa: F401

#: The ACP and provider types the owners read through this module's globals. The
#: agent-SDK boundary allows these three imports in this file and in no owner, and
#: binding them here keeps each import line in use, so its baselined text is unchanged.
_ACP_LAYER_TYPES = (AcpModelUnavailable, AcpProvider, LLMProvider)

logger = logging.getLogger(__name__)

# Sentinel: the authorized transcript's identity (created_at) could NOT be read,
# so a source-link unlink write cannot be proven to target it. Distinct from a
# real created_at of None (a metadata line that simply lacks the field).
_UNPINNED: object = object()

# Feed notice appended by api_chat_slot_reload. A constant, not LLM-derived
# text, so it needs no redaction pass.
_SESSION_RELOAD_NOTICE = (
    "Reloading session: relaunching the agent process with a freshly loaded "
    "agent spec, environment, and MCP servers. The conversation is preserved."
)


#: Row-meta keys a REQUEST may never supply, because the gateway mints them and a
#: surface reads them as the gateway's own claim. ``decisions_strip`` is a Jev
#: decision receipt with a verdict control attached (``decisions/points/
#: message_steer.py``, ``website/src/pages/chat/SteerDecisionLine.tsx``), so a
#: caller-supplied one would render a decision nobody made. ``HUMAN_TURN_META_KEY``
#: is the gateway's own claim that a PERSON typed a row; a caller-supplied one
#: forges human-turn provenance and advances the last-human-turn ranking stamp
#: (``chat_persistence._newest_human_turn_ts``), so an app token owning its slot
#: could displace human sessions. Stripped here so the gateway re-applies it below
#: only for a genuine human send. ``TURN_ACTOR_META_KEY`` is the gateway's record
#: that an app sent the row, which the title counter reads (``chat_title``).
RESERVED_ROW_META_KEYS = frozenset({"decisions_strip", HUMAN_TURN_META_KEY, TURN_ACTOR_META_KEY})

#: The ``steer`` value that means "let Jev choose between the two paths" rather
#: than naming one. A STRING beside the boolean the two manual modes send, so the
#: manual wire is untouched: ``steer: true`` still steers and an absent flag still
#: queues, byte for byte, whatever this build decides about ``auto``.
STEER_AUTO = "auto"


_source_link_unlink_tasks: set[asyncio.Task] = set()


#: Window rows a bounded read must NOT hand back. ``_TRANSIENT_ROLES`` documents
#: itself as being about a window-region DISK line
#: (``chat_persistence.py:1320-1322``) and ``chat_persistence.py:1571`` uses it that
#: way. A bounded read answers a different question — which WINDOW rows does the
#: client still need — and three of those roles are still needed. ``permission``:
#: a pending approval is actionable and the client reads it out of the transcript,
#: so dropping it hides the approval bar while the server is still waiting.
#: ``chunk``/``streaming``: ``_prepare_messages`` does not discard a chunk run, it
#: collapses one into a single ``streaming`` row, and that is the only way in-flight
#: assistant text reaches this endpoint — the client filters raw ``chunk`` itself.
#: ``done`` is discarded by ``_prepare_messages`` regardless, and ``queued`` stays
#: listed because the client rebuilds those bubbles from the payload's ``queue``.
#: ``elicitation`` is carved out for the same reason as ``permission``: a pending
#: ask is actionable and the client reads it out of the transcript.
_UNOWED_WINDOW_ROLES = _TRANSIENT_ROLES - {"permission", "elicitation", "chunk", "streaming"}


# Modes a slot may be CREATED with. A deliberate superset of the mode-SWITCH
# allowlist (chat_folders._VALID_MODES) and the fork override allowlist
# (chat_fork): "design-critique" is an app-worker mode assigned at birth by the
# Design Critique app's openSlot() — the custom mode keeps its throwaway dc-*
# slots off the chat sidebar, which renders only plain "" slots
# (chat/page/pageEnvironment.ts filteredSlots). Switching an existing session INTO an
# app-worker mode, or forking one with it as an override, is not a real flow, so those two
# allowlists deliberately stay narrower — do not "sync" them to this one.
_CREATABLE_MODES = ("", "design-critique")

# Deferral is an optimization, so a request shape added later must stay on the
# synchronous path until its publication ordering has been reviewed explicitly.
_DEFERRED_PLAIN_CREATE_KNOWN_KEYS = frozenset(
    {
        "name",
        "agent",
        "agent_kind",
        "model",
        "folder_id",
        "memory_mode",
        "mode",
        "title",
        "artifact",
        "ephemeral",
        # Only a request; a create that it actually files is not plain,
        # because the handler then sees a folder_id.
        "default_folder",
    }
)


def _unblock_pending_waits(state: DashboardState, slot: _ChatSlot) -> None:
    """Unblock EVERY thing a stop/interrupt could leave the runner waiting on.

    Two independent blocking waits exist per slot and both must be released or
    the cooperative cancel times out into a hard kill:

    * pending tool approvals (:func:`_reject_pending_approvals`)
    * pending agent questions that have a server-side wait
      (:meth:`DashboardState.cancel_questions_for_slot`). The blocking MCP
      ``ask_question`` registers its card in ``_pending_questions`` under the
      slot, so cancelling resolves its future as ``withdrawn`` and the tool call
      returns; the legacy ``POST /api/ask-question`` wait is released the same way.

    They are combined here deliberately: a new blocking wait added later must
    be released from every stop path, and three separate call sites each
    needing their own second line is how one of them gets missed.
    """
    _reject_pending_approvals(slot)
    cancelled = state.cancel_questions_for_slot(slot.key)
    if cancelled:
        logger.info("Stop: cancelled %d pending question(s) on slot %s", cancelled, slot.key)


# Test-only scheduling seam for the session-teardown races. Production leaves it
# None, so each point below costs one global read and an identity comparison, and
# no coroutine is created. It is reachable from no env var and no config key on
# purpose: an operator-facing knob that can suspend a teardown mid-pop is a way to
# wedge a live session, and nothing outside the test suite has a reason to want
# one.
#
# The interleavings it exists to make reachable cannot be driven from outside the
# process. Which of two teardowns lands inside the other's span is decided by
# which coroutine holds the event loop between two awaits; an HTTP client can only
# issue both requests and hope. A test awaits a named point, drives the other
# racer while suspended there, and so fixes the interleaving as a property of the
# test rather than of the scheduler -- the shape-determinism the async-flake rules
# ask for, with no sleep to tune.
#
# The names are the contract. Each marks a boundary the race actually crosses, and
# the comment at each call site says what suspending there is positioned to
# intercept; a point whose boundary no test can otherwise reach is the only kind
# worth adding.
#
# Assign it with monkeypatch, which reverts on teardown even when the test fails.
# ``_no_leaked_interleave_hook`` in test/conftest.py fails any test that leaves it
# set, because nothing legitimately does.
_test_interleave: Callable[[str], Awaitable[None]] | None = None


async def _reset_slot_session(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    *,
    skip_if_busy: bool = False,
) -> bool:
    """Reset a slot's agent session, releasing anything blocked on the old one.

    The switch handlers (agent, model, bulk model, reasoning effort, workspace)
    and the reload endpoint reset the session so the next message starts under
    the new setting. That tears down the agent process — but a pending question
    card lives in dashboard state, not in the session, so without this it
    survives the reset: the card stays on screen inviting an answer, and if it
    is the blocking kind (``POST /api/ask-question``) the open HTTP request
    holds an MCP worker until its own timeout, with no agent left to receive
    the answer it eventually returns.

    Routing every reset through one helper rather than adding a second call at
    each site is deliberate, and is the same reasoning as
    :func:`_unblock_pending_waits`: six call sites each having to remember an
    extra line is how one of them gets missed.

    ``skip_if_busy`` forwards to :meth:`SessionManager.reset`, which evaluates
    busyness atomically with the session pop; False means the reset was
    declined or there was no live session to tear down. The unblock still runs
    first even then: a wait can only be pending from a turn old enough to have
    completed an LLM round-trip, and such a turn is visible to any caller's
    has_active_turn() fast path — so a decline here implies a turn that started
    microseconds ago, which cannot have posted a card yet.

    A successful reset also drops the slot's MCP session report, for the same
    reason the pending card goes: it describes the session being torn down.
    Every caller here changes what the next session will mount (agent, model,
    workspace) or restarts it outright, so keeping the old report would leave
    the UI presenting a dead session's server list as the live one's — the
    stale-evidence failure that report exists to remove.
    """
    from kiro_crew.dashboard.warm_task_brief import cancel_warm_task_brief

    cancel_warm_task_brief(state, slot.key)
    _unblock_pending_waits(state, slot)
    if _test_interleave is not None:
        # The near side of the pop. Every teardown in the process funnels through
        # this one await, and the pop is what decides a race between two of them,
        # so this is the position from which a test can hold one teardown open and
        # put a second in flight over the same key.
        await _test_interleave("reset:pre_pop")
    try:
        reloaded = await state.sessions.reset(session_key, skip_if_busy=skip_if_busy)
    except BaseException:
        # Raised or cancelled mid-teardown: the session is in a state this slot
        # cannot vouch for, so neither is its verdict. Unknown fails open.
        slot.forget_session_model_state()
        raise
    if _test_interleave is not None:
        # The far side of the pop, ahead of the verdict-gated bookkeeping below.
        # That bookkeeping describes the session this call just tore down, and a
        # concurrent teardown can have registered and popped a successor under the
        # same key by the time it runs -- reachable only by suspending here,
        # because the pop and the bookkeeping are otherwise adjacent.
        await _test_interleave("reset:post_pop")
    if reloaded:
        # The withhold verdict describes the session that advertised the model
        # list, not the slot, so it goes with the session. Routed through this one
        # funnel for the reason above: the switch handlers that reset a session
        # are exactly the ones that can change which models the next session will
        # advertise (agent, workspace, and the model pick itself), and a verdict
        # surviving that would label the new session from the old one's
        # entitlement.
        #
        # Gated on the reset having HAPPENED. What decides this is whether the
        # session the verdict describes still exists: `skip_if_busy` DECLINES
        # while a turn is in flight, leaving that session -- and therefore its
        # verdict -- alive and accurate, while a completed teardown ends it. The
        # membership heuristic the frontend falls back to on `null` is not itself
        # the defect this carries a verdict to remove; inferring entitlement from
        # that heuristic WHILE an authoritative answer exists is. Dropping on a
        # decline would throw the authoritative answer away and re-create exactly
        # that.
        slot.forget_session_model_state()
        # The MCP session report rides the same gate for the same reason: it
        # describes the session that was just torn down. Clearing is a courtesy
        # delta push -- correctness rests on the identity projector in
        # serialize_slots -- so it only fires when something was recorded.
        if slot.clear_mcp_report():
            state.broadcast_ws("mcp_report_update", {"slot": slot.key, "mcp_report": None})
    # Freshness push for open tabs, OUTSIDE the `reloaded` gate on purpose: the
    # helper is verdict-driven (it re-resolves the live child and applies the
    # read gate's own predicate), so after a declined or failed teardown the
    # still-live child's banners match and nothing is broadcast. See
    # `_broadcast_expired_oauth_banners` for why no snapshot is needed.
    _broadcast_expired_oauth_banners(state, slot)
    return reloaded


# Advisory response field for a committed switch whose old-session teardown
# raised. One literal shared by every switch handler (agent, reasoning effort,
# model, workspace) so a frontend that ever starts reading it never has to
# match per-handler spellings.
_TEARDOWN_INCOMPLETE_WARNING = "old session teardown incomplete"


#: Roles that OPEN a turn in the transcript grouping. Mirrors
#: ``TURN_OPENER_ROLES`` in ``website/src/pages/chat/groupDisplayItems.ts`` —
#: the two must agree, or a stop chip re-armed "in the same turn" here lands
#: in a different visual turn there. A ``subagent`` row that is not a parsable
#: completion is hidden client-side rather than turn-opening; treating it as a
#: boundary anyway only errs toward append-fresh (the sweep-and-append shape), never
#: toward a wrong-turn re-arm.
_TURN_OPENER_ROLES = frozenset({"user", "nudge", "subagent"})


def _orphan_in_current_turn(slot: _ChatSlot, stop_id: str) -> bool:
    """Whether the orphaned stop card is part of the CURRENT turn.

    Walks the window tail: hitting the orphan first means no turn-opening row
    follows it (same turn — the adjacent-chips shape); hitting a
    turn-opener first means the next turn began below the orphan. An orphan
    whose row is gone from the window answers False, which routes the caller
    to the append fallback it already has.
    """
    for msg in reversed(slot.messages):
        if msg.get("role") in _TURN_OPENER_ROLES:
            return False
        # The grouping's SECOND turn-flushing path is not role-based: a
        # synthesis injection (role "inject" stamped meta.injectKind ==
        # "synthesis" by _run_pending_synthesis) closes the open batch too —
        # mirrors isSynthesisInjection in groupDisplayItems.ts, keyed on the
        # meta wire contract exactly as it is. Plain inject rows
        # (cron/recovery notes) are passive on both sides and walked past.
        if msg.get("role") == "inject" and (msg.get("meta") or {}).get("injectKind") == "synthesis":
            return False
        cls_val = msg.get("cls", "")
        if not cls_val or not isinstance(cls_val, str) or "stop_event" not in cls_val:
            continue
        try:
            cls_data = json.loads(cls_val)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(cls_data, dict) and cls_data.get("id") == stop_id:
            return True
    return False


#: Consecutive tagged session-start failures at which Continue stops re-running
#: the start. Two, not one: a single timed-out start is host weather and the
#: first Resume is exactly the retry it deserves; the second identical failure
#: is the signal that nothing a retry can change is wrong. Mirrored by
#: ``SESSION_START_REPEAT_REFUSAL_AT`` in ``website/src/pages/chat/ErrorCard.tsx``.
_SESSION_START_REPEAT_REFUSAL_AT = 2

#: ``inject`` kinds that begin a turn of their own rather than continuing the
#: one above them -- the mirror of ``INJECT_KIND_OPENS_TURN`` in
#: ``website/src/pages/chat/RecoveryCard.tsx`` (``recovery`` and
#: ``user_replay`` continue the same turn and are deliberately absent).
_TURN_OPENING_INJECT_KINDS = frozenset({"cron", "synthesis"})


def _has_conversation(slot: _ChatSlot) -> bool:
    """True when the transcript holds a real turn to continue FROM.

    The authorization check behind Continue. It is deliberately weak — anything
    a person could look at and say "carry on with that" qualifies — because a
    hard-killed gateway writes no error row, so an interrupted turn is often
    shape-identical to a completed one and no predicate can separate them. The
    button is therefore offered on any idle slot with a transcript, and this
    guard only refuses the one case with nothing to reason about at all: an empty
    slot (or one holding only scaffolding rows such as a compaction notice),
    where a continuation would reach the model with no conversation under it.

    Rows are walked with the same skip rules as ``_is_interrupted`` so the two
    cannot disagree about what counts as the conversation's floor.
    """
    for m in slot.messages:
        if is_system_notice(m.get("role"), m.get("meta")):
            continue
        if m.get("role") in ("user", "assistant") and m.get("content"):
            return True
    return False


class SlotCloseError(Exception):
    """A close that could not complete, carrying the response the tab-✕ path
    would have rendered.

    Extracted alongside :func:`close_slot` so the DELETE endpoint and
    session-control's ``close_target`` map the SAME four failures the same way.
    ``code`` is the machine-readable contract; ``message`` is advisory prose;
    ``status`` is 500 for every close failure (each leaves the tab open and
    every partial step rolled back — a state the user can see and retry).
    """

    def __init__(self, message: str, code: str, status: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# Ceiling on how long a close waits for this slot's guarded history writes to
# finish. The wait is bounded so a genuinely stuck write cannot hang the tab
# close: on breach the close is REFUSED and rolled back, which returns promptly
# and leaves the tab the user still has open, rather than retracting the name
# while a worker thread is still on its way to the rename.
_GUARDED_WRITE_WAIT_SECS = 5.0

#: Seconds a close waits for the turn's in-flight file-change flush before it
#: archives without the chips. The loop watchdog
#: (``dashboard.loop_stall_exit_after_secs``, 25 s) is the bound the same read
#: had when it ran ON the event loop: past it the whole gateway was killed, so a
#: read this long never produced chips there either. Here only this close waits,
#: and past the bound it goes on to archive what the window holds.
_FILE_CHANGES_DRAIN_SECS = 25.0


# Serializes slot SWITCH transactions that share one session, keyed by
# ``effective_session_key``. The per-slot locks the switch handlers take
# (``slot._lock``, ``slot._model_pick_lock``) are created per ``_ChatSlot``,
# so two switches arriving through DIFFERENT alias slots that resolve onto
# ONE session take disjoint locks and neither waits for the other: both
# commit, both reset the shared session, and the two slots' committed
# settings can end up disagreeing with each other and with the live
# provider. Same shape and same reason as ``_autocompact_txn_locks`` below
# ("channel-linked aliases resolve distinct slot names onto one file"), keyed
# by the SESSION the switch handlers probe and reset rather than by the
# transcript.
#
# LOCK ORDER — the one place it is written down. ``slot._lock``, then the
# session lock, then ``slot._model_pick_lock``. Every switch handler acquires
# them in that order and nothing acquires them in the opposite one, so two
# aliases contending on one session cannot cycle: a holder of the session lock
# already holds its own ``slot._lock`` and never waits for another slot's.
# Unrelated slots resolve to DIFFERENT keys and so take different locks: this
# serializes aliases of ONE session, never one slot against another session's
# switch. A WeakValueDictionary so a session's lock is collected once no
# request holds it.
#
# WHY THE SESSION LOCK IS ENTERED SECOND, THROUGH AN ExitStack. Its key is
# ``effective_session_key(slot)``, and that value is only trustworthy once
# ``slot._lock`` is held: a channel/cron rebind can land while a request
# queues, which is why every handler deliberately resolves the key INSIDE its
# lock (pinned by
# ``test_binding_that_lands_while_queued_on_the_lock_is_the_one_switched`` --
# the binding that lands is the one switched). Keying the session lock on any
# EARLIER read would be unsound in exactly that case: the handler would hold
# the lock for the PREVIOUS session while probing and resetting the new one,
# so a concurrent alias switch on the new session would not be serialized
# against it -- and the handlers' post-await re-checks cannot catch it,
# because they compare ``effective_session_key(slot) != session_key`` and
# session_key would already BE the new key. Entering the lock after the
# in-lock read makes the lock key and the acted-on key THE SAME VALUE BY
# CONSTRUCTION, so there is no window to guard and no new decision point to
# get wrong. The ExitStack is what lets a lock be acquired mid-block without
# nesting the whole remaining transaction one level deeper.
#
# The lock itself lives in ``kiro_crew.llm_helpers``
# (``slot_switch_session_lock``) so the chat runner's refusal-fallback
# restore can take the SAME lock — this module imports from the runner, so
# the runner cannot import it from here without a cycle. The local name is
# kept for the acquisition sites below.
_slot_switch_session_lock = slot_switch_session_lock


#: The chat picker's "Auto (Jev)" entry, as the id a client sends for it. NOT a
#: provider model id and never stored in ``slot.model``: it asks the
#: ``model.route`` decision point to pick a difficulty tier for each turn, and
#: until it answers the session runs on the backend default. The prefix is
#: ``auto`` so a client too old to know the entry (or a backend without the point)
#: reads it as the Auto it behaves like, and the colon keeps it outside the model
#: namespace -- no advertised id carries one.
JEV_ROUTE_MODEL = "auto:jev"


# Per-slot transaction locks for the autocompact endpoint. The write span
# below contains awaits (body read, forced save), so two concurrent POSTs for
# one slot can interleave: each captures the other's value as its rollback
# snapshot, and a failed request's compare-and-swap rollback can then erase a
# newer request's acknowledged write (value equality cannot identify
# ownership when both requests carry the same pct). Serializing the whole
# reauthorize -> pin -> mutate -> persist -> rollback -> live-map span per
# slot makes the rollback unambiguous: only one request is ever inside the
# span, so a rollback can only undo its own write. WeakValueDictionary so an
# idle slot's lock is reclaimed with its last reference. Keyed by the
# TRANSCRIPT (slot_history_key), not the slot: channel-linked aliases resolve
# distinct slot names onto one file, and two requests through different alias
# slots must serialize against each other or the loser's rollback/flush can
# overwrite the winner's acknowledged durable write. A rebind mid-request is
# handled by the expected_history_key pin and the post-persist
# reauthorization, not by the lock key.
_autocompact_txn_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)


# Same per-transcript transaction lock, for the source-link unlink write. A
# dismissal is persisted into the shared transcript metadata, so concurrent
# unlinks (or an unlink racing a sibling flush) on alias slots that resolve onto
# one transcript must serialize or a loser's rollback / a stale sibling can
# overwrite the winner's acknowledged commit. Keyed by transcript, like above.
_source_link_txn_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)


# Fields carried per follow-up item on the wire. Kept explicit so a future
# schema addition has to be added here deliberately rather than leaking
# whatever the model happened to send into the broadcast payload.
_FOLLOWUP_TEXT_FIELDS = ("title", "description", "prompt")


def deny_non_dashboard_caller(request: web.Request, operation: str) -> web.Response | None:
    """403 unless this is the dashboard OWNER's own request, else None.

    Deny-by-default, matching ``api_chat_slots_model``'s reasoning: the auth
    middleware sets ``request["app"]`` on every authenticated path (``""`` for
    dashboard users, the app name for app tokens), so an ABSENT key means the
    middleware did not run and must refuse rather than fall through.

    An app claim of ``""`` is necessary but NOT sufficient. Every surface guarded
    here acts on owner-scoped resources — the card renders in the owner's composer,
    and the worktree allow-list is built from every slot's project — so identity
    is checked with ``is_owner_dashboard_request``, the same predicate the source
    provider mutations use: the caller must match the configured ``owner_id``, or
    be a signed local bootstrap subject when no owner is configured (the
    standalone-local case, where the browser's own token is minted for
    ``local-app``). A dashboard token issued for a different subject would
    otherwise mutate repositories it does not own.

    ONE exception, and it is the path every MCP call arrives on: a request that
    presented a valid ``X-Internal-Secret`` from loopback is granted by the
    middleware WITHOUT an app claim (there is no app identity to set), so it
    carries ``request["internal_auth"] is True`` instead. Refusing that would
    403 ``suggest_followup`` outright — the tool could never raise a card.
    """
    if request.get("internal_auth") is True:
        return None
    # Imported here, not at module scope: source_providers imports chat state
    # helpers, so a top-level import would close a cycle (same pattern as the
    # owner-only check-status gate in api_chat_slots).
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response  # noqa: F811
    from kiro_crew.dashboard.handlers.source_providers import (  # noqa: F811
        is_owner_dashboard_request,
    )

    if not is_owner_dashboard_request(request):
        # Domain-specific audit kept here rather than delegated to
        # ``require_owner_dashboard_request``: this record carries its own
        # ``error`` reason and no ``resources``, and the ``sel`` it reaches is
        # this module's, which is what the coverage tests patch. Only the denial
        # TAIL (stale-session relabel + 403) is shared.
        try:
            sel().log_api_access(
                caller=str(request.get("user") or "anonymous"),
                operation=operation,
                outcome="denied",
                source="dashboard",
                error="not the dashboard owner",
            )
        except Exception:  # pragma: no cover - audit is best-effort
            logger.debug("SEL audit failed for %s denial", operation, exc_info=True)
        # Deny decision made above; only the response label changes for a
        # signed pre-owner bootstrap subject (see stale_owner_session_response).
        return _owner_denial_response(request, "forbidden")
    return None


_MAX_RECENT_PROJECTS = 100


# Favourites are a SEPARATE file from the recents list, not a flag on its rows, because
# the two have different lifetimes: recents is a rolling window capped at
# _MAX_RECENT_PROJECTS and rewritten on every project switch, so a favourite stored
# there would fall off the end of a busy week. There is no cap: only the dashboard owner
# can write the file (both write verbs are owner-gated), so a bound would have no caller
# to defend against, and a list the user curates by hand should not refuse an entry.
#
# Both write verbs are a read-modify-write of one file, run on worker threads, so two
# overlapping requests (two dashboard tabs) would each write back the snapshot they read
# and one change would be lost. One lock serialises the whole transaction for both, and
# the display read loads under it too: on Windows a rename cannot replace a file another
# handle has open, so a read open during a write's rename failed that write.
_favorite_projects_lock = threading.Lock()


# Bound for normalising the non-string ``content`` a legacy or hand-edited
# transcript row can carry (nested dict/list of multi-part content). We do NOT
# redact-in-place and keep the structure: two independent problems make that
# unsafe. (1) Redacting only string LEAVES leaves dict KEYS unscrubbed, so a
# credential sitting in a key reaches the broadcaster. (2) The downstream
# persistence/display paths (``_build_message_entry_uncached``, ``_prepare_messages``)
# call the string-only redactors on ``content`` directly and raise ``TypeError``
# on a non-string, so a structured row the slot accepted cannot be saved -- the
# crash the parent revision had is only moved, not removed. Instead we NORMALISE
# such content to a single JSON string and redact THAT, which scrubs keys and
# values alike and yields a row every downstream path can serialise. Bounded so
# a corrupt/hostile row cannot blow the stack: ``json.dumps`` with a depth-safe
# default; on any failure we fall back to a fixed placeholder rather than raise.
# Cap on the serialised size of a normalised structured-content row. A single
# legitimate transcript row is far below this; a serialisation larger than the
# transfer-bounds ceiling is a malformed/hostile row and is dropped to the
# placeholder rather than run through the GIL-held redactors.
_STRUCTURED_CONTENT_MAX_CHARS = 20_000_000
_STRUCTURED_CONTENT_PLACEHOLDER = "[unsupported structured content removed]"

#: Extra off-loop re-reads a resume gives its OWN post-reopen metadata reads
#: (the ``clear_closed`` verification read and the final identity re-read) when
#: they come back unreadable. Those reads open a file this resume just rewrote,
#: and on Windows a just-written file is briefly unopenable while an indexer or
#: AV scanner holds it; on a loaded runner that hold can outlast
#: ``get_metadata_status``'s own bounded retry. An unreadable read stays
#: fail-closed (the handler refuses, never publishes blind), so these extra
#: tries only widen the window the transient has to clear before that refusal —
#: they never accept an unreadable line. Sized well above the few-ms transient
#: without stalling a genuinely-gone or corrupt line for long: 8 tries at 25 ms
#: is a 200 ms ceiling, paid only on the rare unreadable path.
_REOPEN_REREAD_EXTRA_ATTEMPTS = 8
_REOPEN_REREAD_RETRY_SECS = 0.025


class ResumeRefusal(NamedTuple):
    """One refusal of :func:`resume_slot_from_history`, shaped for the wire.

    Every refusal carries a machine-readable ``code`` (the error-code contract),
    including the missing conversation log (``no_conversation_log``) and the
    app-isolation 404 on a live slot (``slot_not_found``), so the wire wrapper
    can emit one transparent coded body for every status.
    """

    error: str
    code: str
    status: int


#: The one refusal an app gets from resume for a session it may not open. It is
#: the body the per-slot checkpoint answers, so resume cannot tell an app "not
#: yours" from "does not exist" either.
_RESUME_APP_NOT_FOUND = ResumeRefusal("not found", "slot_not_found", 404)


class ResumeOutcome(NamedTuple):
    """What :func:`resume_slot_from_history` decided.

    Exactly one of ``refusal`` / ``slot`` is set. ``already_live`` marks the
    dedup arm: the session was already open, so ``slot`` is the EXISTING slot
    and nothing was hydrated; ``total`` is the effective hydrated length on the
    hydrate arm (durable rows plus a recovered interruption row when one was
    appended), what the wrapper's ``next_before`` is derived from.
    """

    refusal: ResumeRefusal | None = None
    slot: "_ChatSlot | None" = None
    already_live: bool = False
    total: int = 0


MAX_COLOR_INDEX = 20


_MAX_CONTEXT_PER_SOURCE = 10
_MAX_CONTEXT_CONTENT = 40000
# Default expiry for a note's context half: if the user never sends a follow-up
# within 24h, the stale entry is dropped at drain rather than attaching itself to
# some far-future unrelated message. The visible transcript line has no maxAge.
_NOTE_CONTEXT_MAX_AGE = 86400
# Bounds the visible lines a caller can park on one in-flight turn. Matches the
# per-source context cap so neither half of /note outlives the other by much.
# Shared with the persistence restore path (which enforces the same cap on notes
# read back from disk), so the value lives in slot_buffers.
_MAX_DEFERRED_NOTES = MAX_DEFERRED_NOTES

# Distinguishes "key absent" from an explicit JSON null, which `body.get("maxAge")`
# alone cannot: both yield None, so the two cannot mean different things without it.
_UNSET = object()

# Source label bounds. The label is interpolated into the
# ``[Background context from "{source}"]`` prompt frame at drain, so disallow
# control chars and newlines to keep a crafted label from breaking out of the
# frame line, and cap the length. Defense-in-depth: the real free-form surface
# is ``content``, not ``source``. The bound lives once in ``slot_buffers``
# (admit side here, restore side there read the SAME pair) so they cannot drift.
_MAX_SOURCE_LEN = MAX_SOURCE_LABEL_LEN
_SOURCE_CTRL_RE = SOURCE_LABEL_CTRL_RE


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.dashboard.chat_handlers.<name>`` reaches it wherever it lives; see
# ``kiro_crew.dashboard.chat_api``. Run once, after this body has bound every name.
_facade_compose(
    globals(),
    (
        _owner_agent_switch,
        _owner_app_admission,
        _owner_chat_send,
        _owner_continue_turn,
        _owner_followup_card,
        _owner_handover_drain,
        _owner_pending_context,
        _owner_project_lists,
        _owner_prompt_queue,
        _owner_resume,
        _owner_session_relaunch,
        _owner_slot_approval,
        _owner_slot_create,
        _owner_slot_detail,
        _owner_slot_effort,
        _owner_slot_guards,
        _owner_slot_lifecycle,
        _owner_slot_model,
        _owner_slot_note,
        _owner_slot_settings,
        _owner_slot_summary,
        _owner_source_links,
        _owner_turn_stop,
    ),
)


# How long one SSE write may wait for the client to take the bytes. aiohttp parks
# a write once the socket and its own buffer are full, and resumes it when the
# client has read the buffer down by about 48 KiB, so a reader that is merely
# slow (a few hundred bytes a second) finishes every write well inside this.
_SSE_WRITE_DEADLINE_SECS = 120.0
