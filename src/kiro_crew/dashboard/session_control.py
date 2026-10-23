"""Session control: letting one chat session observe and interrupt another.

Four operations — create a session, stop its turn, close (archive) it, and read
its transcript — plus the authorization that decides whether a caller may address
a target at all. The operations are deliberately thin: they reuse the same
creation, stop, close and history paths the dashboard itself uses, so a controlled
session behaves exactly like one a human is typing into.

**Two verbs that live behind the ``dashboard/session_control`` facade, in
``dashboard/session_control_parts/``, write into another session's conversation:
``session_send`` and ``session_broadcast``.**
Reading returns a transcript tail; stopping cancels an in-flight turn the way the
Stop button does; closing archives the session the way the tab ✕ does (the
conversation is saved to history and can be reopened — closing is not deletion);
creating opens an empty session in the user's sidebar; sending
delivers a message the target runs as its next turn, redacted through
``sanitize_outbound`` and prefixed with a ``[sent by session … via <verb>]``
envelope so it can never render as something the person typed — and the verb is
named in it, so a worker can tell a message addressed to it alone from one every
sibling also received. Broadcasting is not a second write path: it resolves an
audience and hands each target to the same delivery, so every bound below holds
per target unchanged. An IDLE target runs
it under the authorization that admitted it; a BUSY target queues it, and the
generic drain re-asserts the target-side containment before the entry becomes a
turn: producers stamp the constraints that held at admission
(:func:`containment_meta`), and ``chat_runner``'s drain drops — with a visible
notice and an SEL record — any entry for which a constraint holds at delivery
that did not hold at admission. A dropped CROSS-SESSION delivery is reported back
to its sender too, since the target's own notice is on a transcript the sender
does not read: the entry also carries the sending session, as a slot key plus
that slot's tab identity (:func:`send_origin_meta`), and the drop appends a
notice there (:func:`notify_send_origin_dropped`). The two deliberate discards
of a queued entry, a person cancelling its queue card and a hard stop clearing
the queue, report back the same way with their own wording
(:func:`notify_send_origin_discarded`). A human-typed queued message
shares the same window and the same re-check, and carries no sender to report to.

Authorization is deny-by-default and checked in one place
(:func:`authorize_target`) for the three operations that take a target — stop,
close and read — so a guard cannot be present on one verb and missing on another.
``session_create`` has no target; it checks the caller's own eligibility with the
same refusals.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# The facade over ``session_control_parts``
# --------------------------------------------------------------------------- #
# ``kiro_crew.dashboard.session_control`` is the import path and the patch seam; the
# code it re-exports lives in the owner modules below, one responsibility each. The owners
# import each other's names, lowest layer first, so a name a test patches here is read in
# more than one module. ``_facade_mirror`` keeps that patch whole: a write or delete of a
# name through this module lands in every owner that binds it (and here, when this module
# binds it too), and a name this module does not bind is read from the first owner that
# does. No owner imports this module.
#: Owner modules, lowest layer first: the first one binding a name answers a read of it.
_OWNERS = (
    "kiro_crew.dashboard.session_control_parts.caller_access",
    "kiro_crew.dashboard.session_control_parts.delivery_containment",
    "kiro_crew.dashboard.session_control_parts.target_authorization",
    "kiro_crew.dashboard.session_control_parts.turn_control",
    "kiro_crew.dashboard.session_control_parts.model_switch",
    "kiro_crew.dashboard.session_control_parts.session_titles",
    "kiro_crew.dashboard.session_control_parts.session_messaging",
    "kiro_crew.dashboard.session_control_parts.session_create",
    "kiro_crew.dashboard.session_control_parts.session_tree",
    "kiro_crew.dashboard.session_control_parts.session_handoff",
    "kiro_crew.dashboard.session_control_parts.session_status",
    "kiro_crew.dashboard.session_control_parts.session_revive",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Owner-bound name this module does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# Derived, not listed: what ``from session_control import *`` binds is every public name
# of the family, as it was when the family was one module.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name this module forwards, for type checkers
    from kiro_crew.dashboard.session_control_parts.caller_access import (  # noqa: F401
        APP_CRON_OWNER_PREFIX,
        CRON_LINK_PREFIX,
        CRON_SLOT_PREFIX,
        HANDOFF_WIDENED_OPERATIONS,
        MAX_MANAGER_CHAIN,
        UNATTENDED_SLOT_PREFIXES,
        WORKFLOW_SLOT_PREFIX,
        SessionControlError,
        _app_owned_cron_refusal,
        _audit,
        _audit_denials,
        _caller_is_ownership_fenced,
        _channel_link_of,
        _created_by_other,
        _cron_caller,
        _delegation_lineage_fenced,
        _manager_chain_on,
        _member_bypass,
        _member_caller,
        _outside_reach,
        _sel_off_loop,
        _store_is_member_owned,
        caller_slot_key,
        manager_chain_enabled,
        manager_chain_snapshot,
        member_admitted_to_scoped_surface,
        member_dispatch_enabled,
        member_owns_slot,
        prewarm_enabled_check,
        refresh_manager_chain,
        session_control_enabled,
    )
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (  # noqa: F401
        _AUDIENCE_CONSTRAINTS,
        _CONTAINMENT_CHANGE_LABELS,
        _NON_CONSTRAINT_KEYS,
        _SEND_DISCARD_REASONS,
        AUDIENCE_ADMISSION_KEY_PREFIX,
        CHANNEL_RECIPIENT_META_KEY,
        MIRROR_IDENTITY_SEPARATOR,
        QUEUED_CONTAINMENT_META_KEY,
        SEND_DISCARD_HARD_STOP,
        SEND_DISCARD_QUEUE_CANCEL,
        SEND_DROP_EXCERPT_CHARS,
        SEND_ORIGIN_META_KEY,
        _audit_queue_drain,
        _has_channel_mirror,
        _in_runner_turn,
        _meta_from_probe,
        _mirror_identity_of,
        _probe_channel_mirror,
        _probe_channel_mirror_for_key,
        _read_mirror_binding,
        _send_origin_field,
        _send_origin_recipient,
        _slack_thread_of,
        _snapshot_from_probe,
        audit_queued_allow,
        audit_queued_drop,
        channel_recipient_meta,
        channel_recipient_of,
        containment_meta,
        containment_snapshot,
        describe_containment_change,
        mirror_audience,
        newly_held_constraints,
        notify_channel_recipient_dropped,
        notify_send_origin_discarded,
        notify_send_origin_dropped,
        publication_withheld,
        record_audience_admission,
        send_drop_excerpt,
        send_origin_meta,
        send_origin_slot,
        send_origin_tab,
    )
    from kiro_crew.dashboard.session_control_parts.model_switch import (  # noqa: F401
        _JEV_ROUTE_SPELLINGS,
        PendingModelPick,
        _another_alias_is_mid_turn,
        _caller_tab_id,
        _pending_pick_view,
        _pick_audit_fields,
        _pick_fields,
        _target_busy_error,
        apply_deferred_model_switch,
        apply_pending_model_pick,
        arm_model_switch_reset,
        effort_commit_needs_reset,
        legacy_effort_base,
        model_switch_deferred,
        set_model_target,
    )
    from kiro_crew.dashboard.session_control_parts.session_create import (  # noqa: F401
        _STOCK_WORKER_AGENT,
        _caller_member_workspace,
        _conducts_for_a_worker,
        _dispatching_crewmate,
        _hands_off_to_own_worker,
        _inherited_pin_foreign_to_child,
        _model_pin_refusal,
        _record_spawn,
        _register_spawn_pending_edge,
        _store_s_crewmate,
        create_session,
    )
    from kiro_crew.dashboard.session_control_parts.session_handoff import (  # noqa: F401
        _HANDOFF_LOCK,
        _manages_up_the_line,
        _move_tree_edge,
        _note_in_transcript,
        hand_back_on_close,
        handoff_target,
    )
    from kiro_crew.dashboard.session_control_parts.session_messaging import (  # noqa: F401
        _FORGED_PROVENANCE_RE,
        _ORPHANED_STEER_DELIVERIES,
        _PS_OPENERS,
        _QUOTED_PROVENANCE,
        _SEND_PROVENANCE,
        _TIMEOUT_NOT_HANDED_OVER,
        _TIMEOUT_RETAINED,
        _TIMEOUT_UNKNOWN,
        BROADCAST_MODES,
        BROADCAST_TIMEOUT_CODE,
        BROADCAST_VIA,
        MAX_SEND_MESSAGE_CHARS,
        _DeliveryProgress,
        _neutralize_send_provenance,
        _retain_orphaned_steer_delivery,
        _start_auto_tag,
        _timeout_delivery_observation,
        _too_many_targets,
        broadcast_audience,
        broadcast_to_targets,
        send_to_target,
    )
    from kiro_crew.dashboard.session_control_parts.session_revive import (  # noqa: F401
        _PENDING_REBINDS,
        DEFAULT_HISTORY_LIST_ROWS,
        MAX_HISTORY_FOLDER_ID_CHARS,
        MAX_HISTORY_LIST_ROWS,
        MAX_HISTORY_SCAN_ENTRIES,
        UNVERIFIED_CREATOR,
        _archived_link_and_mirror,
        _archived_session_is_channel_linked,
        _archived_session_is_mirrored,
        _archived_target_refusal,
        _bounded_folder_id,
        _creation_parent,
        _fold_slot_key,
        _rebind_in_background,
        _resolve_archived_target,
        _revived_slot_refusal,
        _scan_archived_candidates,
        _scan_revivable_history,
        list_archived_sessions,
        revive_own_archived_worker,
        revive_session,
    )
    from kiro_crew.dashboard.session_control_parts.session_status import (  # noqa: F401
        _FINISHED_CLOSE_REASONS,
        DEFAULT_READ_MESSAGES,
        MAX_PENDING_APPROVAL_TOOL_CHARS,
        MAX_READ_CONTENT_CHARS,
        MAX_READ_MESSAGES,
        MAX_SUMMARY_CHARS,
        MAX_SUMMARY_INTENTS,
        MAX_SUMMARY_ITEMS,
        MAX_SUMMARY_NOTES,
        TRANSIENT_ROLES,
        _bounded_status_title,
        _bounded_summary,
        _created_history_roster,
        _created_tree_roster,
        _gone_slot_lifecycles,
        _lifecycle_is_closed,
        _pending_approval_fields,
        _summary_text,
        created_session_status,
        process_state,
        read_messages,
        read_summary,
    )
    from kiro_crew.dashboard.session_control_parts.session_titles import (  # noqa: F401
        _ALLOWED_FORMAT_CHARS,
        _BIDI_CONTROLS,
        _BLANK_GLYPHS,
        _MAX_RENAME_TITLE_CHARS,
        _admit_generate_title,
        _clean_new_session_title,
        _clean_rename_title,
        _is_allowed_format_char,
        _refuse_hidden_title_characters,
        _shows_nothing,
        generate_title_target,
        rename_target,
    )
    from kiro_crew.dashboard.session_control_parts.session_tree import (  # noqa: F401
        _MAX_FORK_TITLE_CHARS,
        _TREE_APPEND_TIMEOUT,
        _fork_refusal,
        _freshest_sid,
        _live_sid_of,
        _note_pending_edge,
        _reaches_through_pending,
        _recorded_sid_of,
        _refuse_if_append_pending,
        _replay_pending,
        _slot_object,
        _slot_opened_sid,
        _slot_tree_parent,
        _target_replaced,
        _tree_append_landed,
        _tree_append_waiter,
        _tree_mutation_lock,
        _tree_mutation_locks,
        _tree_pending_edges,
        _tree_pending_slots,
        adopt_target,
        fork_session,
        release_target,
    )
    from kiro_crew.dashboard.session_control_parts.target_authorization import (  # noqa: F401
        MIRROR_PEER_NOT_ON_RECORD,
        NOT_THE_SOLE_OWNER,
        ORIGIN_NOT_ON_RECORD,
        OWNER_DM_CONDUCTOR_SURFACES,
        SLACK_THREAD_BESIDE,
        Deny,
        OwnerDmVerdict,
        _broadcast_resolution_slots,
        _check_caller_slot_fields,
        _deny_factory,
        _live_target_refusal,
        _not_creator_reason,
        _owner_dm_clauses,
        _refuse_ineligible_creator,
        _refuse_moved_caller_identity,
        _resolve_slot,
        _sole_owner,
        authorize_target,
        judge_owner_dm,
        load_target_window,
        owner_dm_refusal,
        refuse_caller_identity,
        refuse_caller_surface,
        session_owner_dm_refusal,
    )
    from kiro_crew.dashboard.session_control_parts.turn_control import (  # noqa: F401
        _continue_turn_of_target,
        _reload_busy_error,
        _reload_route_refusal,
        close_target,
        continue_target,
        end_wait_target,
        reload_target,
        retry_target,
        stop_target,
    )


if TYPE_CHECKING:  # names this module re-exported from other modules
    from kiro_crew import model_registry  # noqa: F401
    from kiro_crew import model_scope  # noqa: F401
    from kiro_crew.agent_files import WORKER_AGENT_FILENAME  # noqa: F401
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS  # noqa: F401
    from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP  # noqa: F401
    from kiro_crew.agent_sdk.capabilities import capabilities_for  # noqa: F401
    from kiro_crew.agent_sdk.provider_identity import is_claude_code  # noqa: F401
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401
    from kiro_crew.config.loader import _workspace_name_for_dir  # noqa: F401
    from kiro_crew.config.loader import default_project_dir  # noqa: F401
    from kiro_crew.config.loader import member_template_id  # noqa: F401
    from kiro_crew.config.loader import resolve_agent_bindings  # noqa: F401
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG  # noqa: F401
    from kiro_crew.config.sections import DEFAULT_MODEL  # noqa: F401
    from kiro_crew.config.sections import AutocompactPctError  # noqa: F401
    from kiro_crew.config.sections import KiroCrewAgentConfig  # noqa: F401
    from kiro_crew.config.sections import coerce_effort  # noqa: F401
    from kiro_crew.config.sections import parse_autocompact_pct  # noqa: F401
    from kiro_crew.config.sections import worker_session_defaults  # noqa: F401
    from kiro_crew.context_assembly.markers import _apply_marker_spans  # noqa: F401
    from kiro_crew.context_assembly.markers import _marker_spans  # noqa: F401
    from kiro_crew.crew_log import emit as crew_log_emit  # noqa: F401
    from kiro_crew.crew_log.session_tree_projection import projection  # noqa: F401
    from kiro_crew.dashboard import session_managers  # noqa: F401
    from kiro_crew.dashboard.chat_delivery import sanitize_outbound  # noqa: F401
    from kiro_crew.dashboard.chat_folders import _folder_declared_project  # noqa: F401
    from kiro_crew.dashboard.chat_folders import _resolve_folder_project_dir  # noqa: F401
    from kiro_crew.dashboard.chat_folders import _slot_meta_txn_lock  # noqa: F401
    from kiro_crew.dashboard.chat_folders import _unhide_folder  # noqa: F401
    from kiro_crew.dashboard.chat_folders import note_folder_filed  # noqa: F401
    from kiro_crew.dashboard.chat_fork import _FORK_DIRECTION_HEAD  # noqa: F401
    from kiro_crew.dashboard.chat_fork import ForkResult  # noqa: F401
    from kiro_crew.dashboard.chat_fork import ForkSource  # noqa: F401
    from kiro_crew.dashboard.chat_fork import fork_slot  # noqa: F401
    from kiro_crew.dashboard.chat_fork import resolve_fork_source  # noqa: F401
    from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
        _TRANSIENT_ROLES as _PERSISTENCE_TRANSIENT_ROLES,
    )
    from kiro_crew.dashboard.chat_persistence import _recent_session_slot_name  # noqa: F401
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop  # noqa: F401
    from kiro_crew.dashboard.chat_title import apply_manual_title  # noqa: F401
    from kiro_crew.dashboard.chat_title import apply_regenerated_title  # noqa: F401
    from kiro_crew.dashboard.chat_title import generate_manual_title  # noqa: F401
    from kiro_crew.dashboard.chat_utils import _history_key_for  # noqa: F401
    from kiro_crew.dashboard.chat_utils import _normalize_model  # noqa: F401
    from kiro_crew.dashboard.chat_utils import drained_to_thread  # noqa: F401
    from kiro_crew.dashboard.chat_utils import effective_session_key  # noqa: F401
    from kiro_crew.dashboard.chat_utils import slot_history_key  # noqa: F401
    from kiro_crew.dashboard.create_rate_limit import GENERATE_TITLE  # noqa: F401
    from kiro_crew.dashboard.create_rate_limit import SESSION_CREATE  # noqa: F401
    from kiro_crew.dashboard.create_rate_limit import WINDOW_SECS  # noqa: F401
    from kiro_crew.dashboard.create_rate_limit import allow_create  # noqa: F401
    from kiro_crew.dashboard.create_rate_limit import has_create_budget  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import _scrub_text  # noqa: F401
    from kiro_crew.dashboard.relay_archive import RELAY_ARCHIVE_CODE  # noqa: F401
    from kiro_crew.dashboard.relay_archive import RELAY_ARCHIVE_ERROR  # noqa: F401
    from kiro_crew.dashboard.relay_archive import is_relay_archive  # noqa: F401
    from kiro_crew.dashboard.state import MAX_LIVE_SLOTS  # noqa: F401
    from kiro_crew.dashboard.state import MAX_SLOTS_PER_CREATOR  # noqa: F401
    from kiro_crew.dashboard.state import SlotOrigin  # noqa: F401
    from kiro_crew.dashboard.state import _normalize_slot_key  # noqa: F401
    from kiro_crew.dashboard.state import _safe_folder_tree  # noqa: F401
    from kiro_crew.dashboard.state import append_and_surface  # noqa: F401
    from kiro_crew.dashboard.state import load_window_off_loop  # noqa: F401
    from kiro_crew.dashboard.stop_retry import allow_escalation  # noqa: F401
    from kiro_crew.effort import EFFORT_LEVELS  # noqa: F401
    from kiro_crew.effort import is_valid_effort  # noqa: F401
    from kiro_crew.execution_context import ExecutionContext  # noqa: F401
    from kiro_crew.execution_context import MemoryStoreRef  # noqa: F401
    from kiro_crew.execution_context import bind_session_execution  # noqa: F401
    from kiro_crew.execution_context import member_config_for_id  # noqa: F401
    from kiro_crew.execution_context import read_session_execution  # noqa: F401
    from kiro_crew.execution_context import read_vouched_session_execution  # noqa: F401
    from kiro_crew.execution_context import refresh_vouched_session_execution  # noqa: F401
    from kiro_crew.execution_context import resolve_member_execution  # noqa: F401
    from kiro_crew.execution_context import revouch_at_verified_admission  # noqa: F401
    from kiro_crew.history import metadata_now_iso  # noqa: F401
    from kiro_crew.history import transcript_stem  # noqa: F401
    from kiro_crew.llm_helpers import slot_switch_session_lock  # noqa: F401
    from kiro_crew.members import select_provider_backend  # noqa: F401
    from kiro_crew.memory_stores import named_store_or_empty  # noqa: F401
    from kiro_crew.messaging.link import CHAT_TYPE_DIRECT  # noqa: F401
    from kiro_crew.messaging.link import ChannelLink  # noqa: F401
    from kiro_crew.messaging.link import parse_session_key  # noqa: F401
    from kiro_crew.messaging.transport import DM_TARGET_PREFIX  # noqa: F401
    from kiro_crew.messaging.transport import sole_direct_target  # noqa: F401
    from kiro_crew.metrics.sessions import END_REASON_REMOVED  # noqa: F401
    from kiro_crew.metrics.turns import turn_outcome  # noqa: F401
    from kiro_crew.security import redact  # noqa: F401
    from kiro_crew.security import redact_and_truncate  # noqa: F401
    from kiro_crew.sel import sel  # noqa: F401
    from kiro_crew.session_summary import derive_state  # noqa: F401
    from kiro_crew.terminal_safe import _is_invisible  # noqa: F401
    from kiro_crew.validation import _MODEL_NAME_RE  # noqa: F401
    from kiro_crew.validation import BROADCAST_TARGET_ALLOWANCE_SECS  # noqa: F401
    from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN  # noqa: F401
    from kiro_crew.validation import MAX_BROADCAST_TARGETS  # noqa: F401
    from kiro_crew.validation import MAX_LONG_STRING  # noqa: F401
    from kiro_crew.validation import MAX_SESSION_STATUS_ROWS  # noqa: F401
    from kiro_crew.validation import MAX_SESSION_STATUS_TITLE_CHARS  # noqa: F401
    from kiro_crew.validation import MAX_SHORT_STRING  # noqa: F401
