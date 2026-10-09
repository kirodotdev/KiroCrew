"""HTTP API handlers for dashboard chat endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timezone
from itertools import islice  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from kiro_crew import crew_recency
from kiro_crew import members as members_mod
from kiro_crew import model_registry
from kiro_crew.acp.client import AcpModelUnavailable
from kiro_crew.agent_discovery import cached_project_agent_names, warm_project_agent_names
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP, capabilities_of
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.agent_switch_command import switch_announcement
from kiro_crew.config.loader import (
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    KiroCrewConfig,
    _workspace_name_for_dir,
    config_dir,
    default_project_dir,
    published_autocompact_pct,
    resolve_agent_bindings,
)
from kiro_crew.dashboard import chat_api as _chat_api
from kiro_crew.dashboard.channel_slots import channel_slot_name, note_slot_closed  # noqa: F401
from kiro_crew.dashboard.chat_api import resume as _owner_resume
from kiro_crew.dashboard.chat_api import slot_detail as _owner_slot_detail
from kiro_crew.dashboard.chat_api import slot_lifecycle as _owner_slot_lifecycle
from kiro_crew.dashboard.chat_api import source_links as _owner_source_links
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
    _same_persisted_body,
    _snapshot_slot_window,
    api_chat_slot_detail,
    api_chat_slots,
)
from kiro_crew.dashboard.chat_api.slot_lifecycle import (  # noqa: F401
    _await_guarded_history_write,
    _close_slot,
    _NudgeRetireFailed,
    _pending_guarded_history_writes,
    _release_closed_execution,
    _restore_slot_nudge_loop,
    _retire_slot_nudge_loop,
    _slot_still_ours,
    _wake_conductor_for_closed_worker,
    api_chat_slot_delete,
    api_chat_slot_reset_conversation,
    api_chat_slots_cleanup,
    close_slot,
)
from kiro_crew.dashboard.chat_api.source_links import (  # noqa: F401
    _apply_source_link_unlink,
    _audit_source_link_unlink,
    _source_link_txn_lock,
    api_chat_slot_source_link_unlink,
    api_chat_slot_source_links,
)
from kiro_crew.dashboard.chat_auto_tag import maybe_auto_tag
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
from kiro_crew.dashboard.chat_folders import (
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
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_runner import (
    _context_usage_payload,
    _run_chat,
    _start_next_queued_turn,
    _sync_served_model,
    context_entry_expired,
    schedule_eager_spawn,
    subagents_hold_user_messages,
)
from kiro_crew.dashboard.chat_slack import maybe_auto_link_slack, slot_is_live
from kiro_crew.dashboard.chat_summary import generate_session_summary, read_cached_intent_summary
from kiro_crew.dashboard.chat_tags import (
    _bump_slot_tags_revision,
    tags_write_lock,
    validate_folder_tag_ids,
)
from kiro_crew.dashboard.chat_title import _maybe_auto_title
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
    drained_to_thread,
    effective_session_key,
    history_corpus_unreadable,
    is_harness_slash_command,
)
from kiro_crew.dashboard.chat_utils import (
    replacement_shares_transcript as _replacement_shares_transcript,
)
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    restore_replacement_if_handover_did_not_land,
    slot_history_key,
    slot_transcript_key,
    subagents_attached_async,
    tighten_live_slot_memory_mode,
)
from kiro_crew.dashboard.chat_utils import (
    tighten_replacement_to_restricted_original as _tighten_replacement_to_restricted_original,
)
from kiro_crew.dashboard.handlers._shared import (
    _SLOT_SCOPED_TRUST_MODES,
    _owner_denial_response,
    cron_creator_admission,
    cron_mode_refusal,
    cron_slot_creator,
    read_bounded_json,
)
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.interaction_coordinator import native_resolution_target
from kiro_crew.dashboard.relay_archive import relay_archive_refusal
from kiro_crew.dashboard.request_priority import owner_start_priority
from kiro_crew.dashboard.slot_buffers import (
    MAX_DEFERRED_NOTE_CHARS,
    MAX_DEFERRED_NOTES,
    MAX_SOURCE_LABEL_LEN,
    SOURCE_LABEL_CTRL_RE,
    DeferredHoldFull,
    DeferredHoldRebound,
    note_hold_durable,
    persist_deferred_notes_sync,
)
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
from kiro_crew.dashboard.slot_queue_repository import warn_if_not_durable
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
    note_crew_log_class,
    parse_cls_meta,
    request_slot_origin,
    row_mid,
)
from kiro_crew.dashboard.system_notices import SESSION_RELOAD_KIND, is_system_notice
from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn
from kiro_crew.history import (  # noqa: F401
    HUMAN_TURN_META_KEY,
    carry_provenance,
    is_incognito_transcript,
)
from kiro_crew.history_projection import TranscriptRevisionChanged  # noqa: F401
from kiro_crew.jsonl_util import OversizedRecord, SplitlinesBoundaryRecord  # noqa: F401
from kiro_crew.llm_helpers import (
    pick_epoch_host,
    provider_model_pin_refused,
    slot_switch_session_lock,
)
from kiro_crew.memory_startup import MemoryStartupUnavailable, wait_for_memory_preparation
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging.link import canonical_key, is_channel_session_key  # noqa: F401
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider
from kiro_crew.safety_override import (
    approval_mode_permitted,
    safety_override,
    yolo_policy_permits,
)
from kiro_crew.sandbox import voice_runtime_workspace_conflict
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.session_agent_selection import (
    SelectionChange,
    record_agent_selection,
    resolve_session_agent_bindings,
    restore_agent_selection,
    session_agent_selection_name,
)
from kiro_crew.session_lifecycle import compaction_in_flight
from kiro_crew.session_summary import count_user_turns_in_records
from kiro_crew.start_priority import person_priority
from kiro_crew.trust_patterns import (
    base_consent_pattern,
    base_trust_patterns,
    exact_trust_pattern,
)
from kiro_crew.validation import (
    ARTIFACT_SLUG_RE,
    SUGGEST_FOLLOWUP_SCHEMA,
    ValidationError,
    is_registered_agent_name,
    normalize_theme_consent_sha,
    validate_tool_args,
)

if TYPE_CHECKING:  # circular at runtime: autonudge -> dashboard.chat -> chat_handlers
    from kiro_crew.autonudge import NudgeLoop  # noqa: F401
    from kiro_crew.config.sections import ResolvedBindings

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


def _sweep_stale_permissions(slot: "_ChatSlot") -> None:
    """Mark unresolved permissions from prior turns as stale.

    Called once at turn-start, before the new user message is appended.
    Safe: if we're starting a new turn, any prior unresolved permission
    is definitionally orphaned — the LLM that requested it is gone.

    Note: if the same slot is open in multiple tabs, an in-flight pending
    approval in tab A may be marked stale by a turn-start in tab B. The
    failure mode is benign (user re-clicks approve); single-tab use is
    unaffected.
    """
    for msg in slot.messages:
        if msg.get("role") != "permission":
            continue
        try:
            cls = json.loads(msg.get("cls", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls, dict):
            # Valid JSON but not an object (e.g. [], "x", 123, null) — cannot
            # carry a "resolved" key; skip rather than raise TypeError and
            # abort the whole sweep. Mirrors parse_cls_meta() in state.py.
            continue
        if "resolved" in cls:
            continue
        cls["resolved"] = "stale"
        msg["cls"] = json.dumps(cls)
        slot._dirty = True
        sel().log_api_access(
            caller="gateway",
            operation="permission.resolve_stale",
            outcome="allowed",
            source="turn_start_sweep",
            resources=cls.get("request_id", ""),
        )


async def _app_slot_acquisition_denial(
    request: web.Request,
    state: "DashboardState",
    request_app: str,
    slot_name: str,
    operation: str,
    *,
    session_grant_route: bool,
) -> tuple[web.Response | None, Any]:
    """The app-ownership decision for a request that may CREATE the slot it names.

    ``POST /api/chat`` and ``POST /api/chat/slots`` resolve their slot through
    ``get_or_create_slot``, which refuses a memory-mode mismatch or an
    under-construction slot with a 409. That 409 must not tell an app that a
    session it may not see exists, or what its memory mode is, so for an app
    caller this runs first, on a lookup that creates nothing, and answers the
    same 404 for every refusal:

    * a member, cron or workflow key (:func:`app_reserved_key_reason`): an app
      never owns one, and the cron and workflow binders would link a slot under
      that key to the job's or run's own transcript. So is a ``dashboard_`` key,
      whose transcript is another slot's;
    * a key under construction, in any letter case: a session being imported is
      nobody's to name yet (the person still gets the 409; the app gets the 404,
      with no audit row);
    * a key that differs from another live slot's key, or names its transcript,
      only in letter case (:func:`live_case_alias_reason`): on a case-insensitive
      filesystem both slots would write one file;
    * a live slot: the per-slot decision (owner app on its own session and
      transcript, or the ``sessionApproval`` grant when *session_grant_route*);
    * no live slot but a persisted transcript under that key: the transcript must
      record this app (:func:`transcript_acquisition_reason`). Without it a closed
      user session could be re-created as an app-owned slot that reopens and
      appends to the user's transcript.

    *slot_name* is the RAW requested name, normalized here exactly once, as
    ``get_or_create_slot`` will normalize it. Returns ``(refusal, judged)``:
    *judged* is the live slot the decision was made on, or ``None``. It is an
    early out, not the last word: the caller awaits again before it acquires the
    slot, so it re-checks synchronously that the slot it acquires is *judged*
    (:func:`_acquired_slot_was_judged`) and keeps its own post-create check.
    """
    key = _normalize_slot_key(slot_name)
    history_key = _history_key_for(key)
    granted = False
    if session_grant_route:
        # Read before anything about the slot is looked at, for every app caller,
        # so the cost of the answer does not depend on which session was named.
        granted = await session_grant(request, request_app)
    refused, reason = app_new_key_refusal(state, key, history_key)
    if refused:
        if reason:
            audit_app_slot_denial(request_app, operation, key, reason)
        return slot_not_found(), None
    existing = state._slots.get(key)
    if existing is not None:
        allowed = (
            app_may_control_session(request_app, existing, granted)
            if session_grant_route
            else app_owns_slot_session(request_app, existing)
        )
        if allowed:
            return None, existing
        audit_app_slot_denial(request_app, operation, key, SESSION_CONTROL_DENIED)
        return slot_not_found(), None
    log = state.conversation_log
    if log is None:
        return None, None
    reason = await asyncio.to_thread(transcript_acquisition_reason, log, history_key, request_app)
    if not reason:
        return None, None
    audit_app_slot_denial(request_app, operation, key, reason)
    return slot_not_found(), None


def _send_binds_agent(agent: str, slot: Any) -> bool:
    """Whether a ``POST /api/chat`` naming *agent* writes it onto *slot*."""
    return bool(agent) and slot.agent in (None, "")


def _send_writes_persona(body: dict) -> bool:
    """Whether a ``POST /api/chat`` body writes the slot's persona fields.

    ``color_theme`` carries the write; ``theme_consent`` and
    ``theme_consent_sha`` are applied with it and never alone.
    """
    return "color_theme" in body


async def _send_harness_command(message: str) -> str:
    """The harness slash command *message* opens with, or ``""`` for plain text.

    The same predicate the runner forwards on (``is_harness_slash_command``), so
    what this refuses is exactly what would reach the harness as a command. The
    config is read only for a first word that starts with ``/``.
    """
    first_word = message.split()[0] if message.strip() else ""
    if not first_word.startswith("/"):
        return ""
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    cc_provider = is_claude_code(cfg.agent.provider)
    return first_word if is_harness_slash_command(first_word, cc_provider=cc_provider) else ""


async def _app_slot_acquisition_recheck(
    state: "DashboardState", request_app: str, key: str, judged: Any, operation: str
) -> web.Response | None:
    """Recheck a named acquisition after setup, without publishing an unjudged slot.

    A free key is reserved through the off-loop transcript read. Other acquirers
    respect the construction marker, so a create-and-close cannot hide between
    that read and the loop resuming. The caller must acquire synchronously after
    this returns; releasing the marker does not yield.
    """
    history_key = _history_key_for(key)
    refused, reason = app_new_key_refusal(state, key, history_key)
    if not refused and not _acquired_slot_was_judged(state, key, judged):
        refused, reason = True, "slot was replaced while the request was authorized"
    if not refused and judged is None and state._slots.get(key) is None:
        state.begin_slot_construction(key)
        try:
            log = state.conversation_log
            if log is not None:
                reason = await asyncio.to_thread(
                    transcript_acquisition_reason, log, history_key, request_app
                )
                refused = bool(reason)
        finally:
            state.end_slot_construction(key)
        if not refused:
            refused, reason = app_new_key_refusal(state, key, history_key)
        if not refused and state._slots.get(key) is not None:
            refused, reason = True, "slot was replaced while the request was authorized"
    if refused:
        if reason:
            audit_app_slot_denial(request_app, operation, key, reason)
        return slot_not_found()
    return None


def _acquired_slot_was_judged(state: "DashboardState", key: str, judged: Any) -> bool:
    """Whether the slot about to be acquired under *key* is the one ownership was decided on.

    Synchronous, for the moment right before ``get_or_create_slot``. A slot the
    pre-check approved that has since been closed would otherwise be minted
    afresh, app-owned, on the key of a session the app was only granted to act
    in -- and the post-create check passes on the new object. A slot that
    appeared where none was judged is decided by the post-create check; that
    acquisition performs no transcript-read await.
    """
    return judged is None or state._slots.get(key) is judged


def _app_acquisition_conflict(
    state: "DashboardState", request_app: str, key: str, judged: Any, exc: ValueError
) -> web.Response:
    """An app's answer when ``get_or_create_slot`` refused *key* with a ValueError.

    The 409 text stays only for the app's OWN live slot, the one the pre-check
    judged and that still holds *key*: it tells the app nothing it does not
    already know, and a 404 there would tell it its live session is gone. Every
    other conflict is the uniform 404, because its text names a session the app
    may not see.
    """
    if (
        judged is not None
        and state._slots.get(key) is judged
        and key not in getattr(state, "_slots_under_construction", ())
        and app_owns_slot_session(request_app, judged)
    ):
        return web.json_response({"error": str(exc)}, status=409)
    return slot_not_found()


def _deny_app_yolo(request_app: str, operation: str) -> web.Response:
    """App tokens never arm or revoke the process-global YOLO override."""
    sel().log_api_access(
        caller=request_app,
        operation=operation,
        outcome="denied",
        source="app_isolation",
        error="app tokens cannot arm yolo",
    )
    return web.json_response(
        {"ok": False, "error": "app tokens cannot arm yolo", "code": "app_yolo_forbidden"},
        status=403,
    )


def _deny_app_session_settings(request_app: str, slot_key: str, trigger: str) -> web.Response:
    """App tokens send turns to a user's session but never change its settings.

    *trigger* names what the send would have changed (``agent=<name>``,
    ``field=color_theme`` or ``command=<word>``) and rides the SEL row.
    """
    reason = (
        "app cannot change the agent binding or persona settings of a session it "
        "does not own, or run a harness slash command there"
    )
    sel().log_api_access(
        caller=request_app,
        operation="chat_send",
        outcome="denied",
        source="app_isolation",
        resources=f"slot={slot_key} {trigger}",
        error=reason,
    )
    return web.json_response(
        {"error": reason, "code": "app_session_settings_forbidden"}, status=403
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


def steer_is_auto(value: object) -> bool:
    """Whether a send's ``steer`` flag asks Jev to choose the path.

    Only the exact string, case- and space-insensitively. A boolean ``True`` is a
    MANUAL steer and must never read as auto: that flag is what every existing
    client sends, and reading it as a request to decide would put an oracle on a
    path the sender already answered.
    """
    return isinstance(value, str) and value.strip().lower() == STEER_AUTO


async def decided_message_handling(slot: Any, message: str) -> tuple[bool, dict | None]:
    """Ask ``message.steer`` whether *message* queues instead of steering.

    Returns ``(queues, record)``: whether to take the QUEUE path, and the decision
    row to stamp on the persisted user row (``None`` when nothing was decided, or
    when the row itself was refused).

    A BOOLEAN rather than the point's choice string, so the caller holds no copy of
    that vocabulary and cannot drift from it: every refusal -- the seam off, the
    session unsampled, a timeout, an answer outside the two options, a failed
    import -- is ``False``, which is the steer path a manual Steer and the
    composer's default have always taken.

    Called for a send the dashboard's own human made while a turn is running, and
    nowhere else (the busy branch's ``not request_app`` conjunct): an app token, an
    integration or a cron has nobody watching the reply, and every such send
    already falls through to the fail-closed queue. Consent and sampling are the
    seam's own gates, re-checked inside ``decide``.

    Never raises except cancellation. The seam may cost an observation and must
    never cost a send.
    """
    try:
        # Imported HERE, not at module scope: this module is on the gateway's boot
        # path and the decisions package is optional, off by default, and pulls the
        # config loader in behind it.
        from kiro_crew.decisions.points import message_steer

        session_key = effective_session_key(slot)
        decided = await message_steer.steer_or_queue(
            message,
            session_key=session_key,
            # The live list, sliced and read off the loop by the point; nothing is
            # written back.
            rows=getattr(slot, "messages", ()) or (),
        )
        if decided is None:
            return False, None
        # The row is written BEFORE the path is taken, and the receipt is stamped
        # only when it landed: the strip's thumbs POST this turn id, so a receipt
        # from a row `append` refused would invite a verdict about a decision the
        # log does not hold. Off the loop -- the append locks a file.
        record = await asyncio.to_thread(message_steer.record_outcome, session_key, decided)
        return decided.get("choice") == message_steer.CHOICE_QUEUE, record
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("message.steer: taking the shipped default", exc_info=True)
        return False, None


async def api_chat(request: web.Request) -> web.StreamResponse:
    """POST /api/chat — send message to a slot, stream response via SSE."""
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    message = body.get("message", "").strip()
    agent = body.get("agent", "")
    slot_name = body.get("slot")
    color_theme = body.get("color_theme", "")
    user_meta = body.get("meta")  # knowledge/files/pastes metadata from frontend
    if not isinstance(user_meta, dict):
        user_meta = None
    else:
        # The row's decision receipt is SERVER-minted and must never be one a
        # caller can write. `meta` rides verbatim onto the persisted user row
        # (`_redact_meta` redacts string values; it is not an allowlist) and onto
        # the queue entry, and the transcript renders `meta.decisions_strip` as a
        # Jev decision with a verdict control whose POST names the turn id in it.
        # So an app token or any other caller could otherwise stamp a decision
        # nobody made and file feedback against it. Dropped HERE, where the field
        # enters, so every downstream user of `user_meta` -- the dispatch row, the
        # busy-slot queue entry, the sub-agent hold -- is covered by one gate
        # rather than each remembering.
        user_meta = {k: v for k, v in user_meta.items() if k not in RESERVED_ROW_META_KEYS}
        # The whole-message quote is bounded HERE, once, for every path below:
        # the immediate row persists `user_meta` verbatim, so a bound applied
        # only where the queue paths read it would leave that row unbounded.
        # A record the bound refuses is dropped whole; the text beside it still
        # carries the blockquote.
        if "quote" in user_meta:
            # Bounded once here; redacted here too when the sender is not the
            # session's own human (an app token), so every later read of
            # `user_meta` -- immediate row, queue entry, hold entry, frames --
            # carries the same form its text gets.
            bounded_quote = quote_meta(user_meta, user_origin=not bool(request.get("app", "")))
            user_meta.pop("quote")
            user_meta.update(bounded_quote)
        if not user_meta:
            user_meta = None
    theme_consent = body.get("theme_consent") is True
    # Content-bound persona consent: the sha256 hex the user
    # granted in the consent modal. Injection is gated on this matching the
    # persona text read from disk server-side; the legacy boolean above is
    # still parsed (backward-compatible bodies + logging) but does not grant
    # injection by itself. Normalize + full-match to 64 lowercase hex here so a
    # malformed value (non-ASCII "é", wrong length, non-str) becomes None
    # (absent) rather than reaching hmac.compare_digest and crashing the turn
    # with a TypeError.
    theme_consent_sha = normalize_theme_consent_sha(body.get("theme_consent_sha"))
    if not isinstance(color_theme, str) or not (
        color_theme == "" or color_theme.startswith("custom-")
    ):
        color_theme = ""
    if not isinstance(slot_name, str) and slot_name is not None:
        slot_name = None  # coerce non-string slot to auto-generate
    _requested_key = _normalize_slot_key(slot_name) if slot_name else ""
    # Ownership BEFORE get_or_create_slot below, whose memory-mode and
    # under-construction 409s would otherwise answer an app about a session it
    # may not see. Member, cron and workflow keys are refused here too. The
    # post-create check further down stays authoritative.
    acquisition_judged: Any = None
    request_app = request.get("app", "")
    if request_app and slot_name:
        denied, acquisition_judged = await _app_slot_acquisition_denial(
            request,
            state,
            request_app,
            slot_name,
            "chat_send",
            session_grant_route=True,
        )
        if denied is not None:
            return denied
    existing = state._slots.get(_requested_key) if _requested_key else None
    # An app's auto-created slot must not land on a transcript it does not own:
    # its first save would stamp the app onto that transcript's metadata line.
    if (
        _requested_key
        and existing is None
        and await _app_claim_refused(
            state, request.get("app", ""), "chat_send", (_history_key_for(_requested_key),)
        )
    ):
        return slot_not_found()
    if (
        existing is not None
        and existing.mode == members_mod.DM_SLOT_MODE
        and not members_mod.is_dispatchable_member_name(existing.agent)
    ):
        sel().log_api_access(
            caller=request.remote or "",
            operation="chat_send",
            outcome="denied",
            source="member_pin",
            resources=f"slot={existing.key}",
            error="stored member pin is not dispatchable",
        )
        return web.json_response(
            {
                "error": "this thread's crew name cannot be dispatched",
                "code": "member_pin_mismatch",
            },
            status=409,
        )
    member_pin_match = members_mod.member_pin_matches(
        getattr(existing, "mode", None), getattr(existing, "agent", None), agent
    )
    if not isinstance(agent, str) or not (
        agent == ""
        or is_registered_agent_name(agent)
        or member_pin_match
        # A free-form display name is admitted only as the configured member it
        # names (config read off-loop, on this miss path alone); a non-member
        # string that fails the grammar is not a template and stays refused.
        or await asyncio.to_thread(members_mod.is_configured_dispatchable_member, agent)
    ):
        _emit_agent_assignment(str(slot_name or ""), str(agent), outcome="denied_invalid")
        return web.json_response({"error": "invalid agent name"}, status=400)

    # Honor memory_mode from the body when auto-creating a slot (e.g. AgentRock
    # skill dispatch defaults to "temporary"). Only validated values are passed
    # through; anything else is dropped so get_or_create_slot uses its default.
    # If the slot already exists, get_or_create_slot raises on a memory_mode
    # mismatch, matching POST /api/chat/slots semantics.
    requested_memory_mode = body.get("memory_mode")
    if requested_memory_mode not in ("persistent", "incognito", "temporary"):
        requested_memory_mode = None

    # Honor mode from the body when auto-creating a slot, mirroring memory_mode
    # above: an app whose worker slot lives only in gateway memory (e.g. Design
    # Critique) repeats mode on send(), so a slot recreated here after a
    # gateway restart keeps its non-"" surface and stays out of the chat
    # sidebar's surface allowlist. Only creation-allowlisted values pass;
    # anything else is dropped so get_or_create_slot uses its default. Unlike
    # memory_mode there is no mismatch error: get_or_create_slot ignores mode
    # for an already-existing slot.
    requested_mode = body.get("mode")
    if not isinstance(requested_mode, str) or requested_mode not in _CREATABLE_MODES:
        requested_mode = ""

    # member-* keys are RESERVED for member DM threads, which are born only
    # through POST /api/members/{slug}/thread. Auto-creating one here (e.g. a
    # send racing a gateway restart that dropped the live slot, or an app
    # token naming the key) would mint an ordinary unpinned slot on the
    # member key — every pin guard is conditioned on mode=="member", so the
    # squatter bypasses all of them AND 409s the real thread opener forever.
    # Refused, not dropped: the caller must re-open through the member route.
    if _requested_key:
        if _requested_key.casefold().startswith(members_mod.DM_SLOT_KEY_PREFIX):
            if _requested_key not in state._slots:
                return web.json_response(
                    {
                        "error": "member thread slots are created only via the member thread endpoint",
                        "code": "member_slot_reserved",
                    },
                    status=409,
                )

    # `relay=1` was the retired turn relay: an older owner gateway asking this
    # peer to run a turn in one of its slots. Refused whole, before slot
    # creation, so such a send can never auto-create a blank session here.
    if request.query.get("relay") == "1":
        return web.json_response(
            {"error": "the remote turn relay is retired", "code": "remote_relay_retired"},
            status=410,
        )
    cron_creator = await cron_slot_creator(request)
    if cron_creator:
        refusal, _, _ = await cron_creator_admission(request, state, slot_name, cron_creator)
        if refusal is not None:
            return refusal

    if request_app and _requested_key:
        denied = await _app_slot_acquisition_recheck(
            state, request_app, _requested_key, acquisition_judged, "chat_send"
        )
        if denied is not None:
            return denied
    created_in_send = slot_name is None or _normalize_slot_key(slot_name) not in state._slots
    try:
        slot = state.get_or_create_slot(
            slot_name,
            app=request.get("app", ""),
            origin=request_slot_origin(request.get("app", ""), cron_creator=cron_creator),
            mode=requested_mode,
            memory_mode=requested_memory_mode,
            # Human request-layer path: a person sending a chat message. The
            # origin conjunct in state.py still excludes app-token callers. A
            # slot an attested cron opens is not a person's and is not counted.
            count_user_session=not cron_creator,
        )
    except ValueError as exc:
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="chat_send",
            outcome="denied",
            source="memory_mode_mismatch",
            resources=f"slot={slot_name}",
            error=str(exc),
        )
        if request_app:
            return _app_acquisition_conflict(
                state, request_app, _requested_key, acquisition_judged, exc
            )
        return web.json_response({"error": str(exc)}, status=409)
    if cron_creator and created_in_send:
        # Attribute the slot to the cron that opened it, inside the synchronous
        # window after the mint, as session_control.create_session does for an
        # agent cron's child. Never re-stamped on an existing slot a cron names.
        slot._created_by = cron_creator

    # App ownership check (App Kit §5.2): deny-by-default for app tokens.
    # Apps keep access to their own slots. The sessionApproval grant lets an
    # enabled app send a turn into an existing user-owned slot. A response
    # option click is one such turn. The grant never crosses into another
    # app's slot. The grant verdict is the one this request already read.
    denied = await deny_app_session_control(request, request_app, slot, slot.key, "chat_send")
    if denied is not None:
        return denied
    # The dashboard user and the slot's owning app may change the slot's settings
    # through this route; the sessionApproval grant reaches a user's session to
    # send a turn and nothing else.
    may_configure = not request_app or bool(getattr(slot, "_app", ""))
    steer = body.get("steer") if may_configure else None
    if not may_configure:
        # The dedicated agent route refuses an app on a slot it does not own, and
        # persona consent is the user's own grant from the consent modal. Refused
        # rather than dropped, like the agent mismatch below: running the turn on
        # an agent other than the one named would be a silent substitution. A
        # harness slash command is refused here, above the busy branch, so a
        # queued copy cannot run it later.
        command = await _send_harness_command(message)
        # The slot may have closed, or a cron or channel binder may have
        # re-linked it, during the permission and config reads; the grant
        # reaches only a local user session.
        if (
            state._slots.get(slot.key) is not slot
            or slot.is_closing
            or not app_slot_is_local_user_session(slot)
        ):
            audit_app_slot_denial(
                request_app,
                "chat_send",
                slot.key,
                "slot closed or re-linked during the permission read",
            )
            return slot_not_found()
        # No await sits between this check and the agent write below for a slot
        # this grant reaches: the member awaits never run for one, and the
        # mismatch branch's awaits run only on a slot that already has an agent.
        # The persona condition reads only the request body.
        if command:
            return _deny_app_session_settings(request_app, slot.key, f"command={command[:64]}")
        if _send_binds_agent(agent, slot):
            return _deny_app_session_settings(request_app, slot.key, f"agent={agent}")
        if _send_writes_persona(body):
            return _deny_app_session_settings(request_app, slot.key, "field=color_theme")
        sel().log_api_access(
            caller=request_app,
            operation="chat_send",
            outcome="allowed",
            source="app_isolation",
            resources=f"permissions.sessionApproval|slot={slot.key}",
        )
        # The gateway mints the row id for a turn sent onto another's session.
        if user_meta is not None and "mid" in user_meta:
            user_meta = {k: v for k, v in user_meta.items() if k != "mid"} or None
    # The member-pin refusal sits AFTER the app-ownership 404s (a 409 here
    # for an app would be an existence oracle for slots it may not see) and
    # BEFORE the _human_seen attendance mark, so a denied request leaves the
    # slot exactly as it found it.
    if slot.mode == "member" and agent and agent != slot.agent:
        # Member DM threads are pinned to their crew. The generic mismatch
        # branch below would also refuse this, but the pin deserves its own
        # machine-readable refusal — and it must hold even for a member slot
        # whose agent is somehow empty (the elif below would otherwise adopt
        # the request's agent onto the pinned thread).
        _emit_agent_assignment(slot.key, agent, outcome="denied_member_pin")
        return web.json_response(
            {"error": "member thread agent is pinned", "code": "member_thread_agent_pinned"},
            status=409,
        )
    if slot.mode == "member":
        _member_cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if slot.agent not in _member_cfg.agents:
            sel().log_api_access(
                caller=request.remote or "",
                operation="chat_send",
                outcome="denied",
                source="member_pin",
                resources=f"slot={slot.key}",
                error=f"registry no longer names {slot.agent}",
            )
            return web.json_response(
                {
                    "error": "this thread's crew no longer exists",
                    "code": "member_pin_mismatch",
                },
                status=409,
            )
        if slot.key.startswith(members_mod.DM_SLOT_KEY_PREFIX):
            _send_binding = await asyncio.to_thread(members_mod.read_dm_binding_for_slot, slot.key)
            if _send_binding is None or _send_binding.get("member", "") != slot.agent:
                sel().log_api_access(
                    caller=request.remote or "",
                    operation="chat_send",
                    outcome="denied",
                    source="member_pin",
                    resources=f"slot={slot.key}",
                    error="member binding missing or mismatched",
                )
                return web.json_response(
                    {
                        "error": "this thread's binding is missing or no longer matches",
                        "code": "member_binding_missing",
                    },
                    status=409,
                )
    if not request_app:
        # A dashboard user (no app token) typed into this slot, so a human
        # demonstrably has it open. That restores the full 2h approval
        # window even on an app-owned tab — the deny-fast window is for slots
        # nobody is watching. Only a caller with an EMPTY request_app reaches
        # here, so an app cannot forge attendance for its own worker.
        slot._human_seen = True

    if slot.agent not in (None, ""):
        # Slot already has an agent — only reject explicit mismatches (non-empty different agent).
        # Empty agent in request means "use existing" (e.g. follow-up messages from frontend).
        if agent and slot.agent != agent:
            # Different NAMES can still be the same BINDING: slots record the
            # resolved default ALIAS at creation, while a client may send the
            # underlying kiro agent name (the E2E smoke tests do exactly this).
            # 409 only when the two names resolve to different dispatch targets.
            # Identity is EVERY dispatch-relevant binding field — kiro agent,
            # workspace, memory store, and model — because aliases configure
            # memory stores and model pins independently of workspace, and a
            # request landing in another alias's memory store is the exact
            # cross-scoping this guard exists to prevent. An unknown requested
            # name (requested_resolved=False) still 409s — the resolver would
            # silently fall back to the default, which is the same lie.
            # Resolution failure falls back to the strict name comparison
            # (fail closed), reported as its own outcome so a config-load
            # blip is not triaged as an agent-naming problem.
            same_binding = False
            resolution_failed = False
            compared_binding = (
                slot.agent,
                slot.project,
                slot.memory_store,
                effective_session_key(slot),
                slot.workspace,
                slot._app,
            )
            try:
                # Config load is file IO (stat + read + jsonschema validate on a
                # cache miss), so it rides a thread like the member-slot load
                # above — never the event loop.
                _cfg = await asyncio.to_thread(KiroCrewConfig.load)
                # Resolve within the slot's PROJECT scope, exactly as dispatch
                # does (see the agent-switch path below): a project-scoped agent
                # exists only inside slot.project, so resolving without it would
                # fall back to default bindings and falsely equate a
                # project-agent slot with a request naming the default alias.
                # Keep both lookups on one captured selection and off-loop:
                # private store validation reads ownership files even on cache hits.
                await warm_project_agent_names(
                    compared_binding[1] or None, operation="api_chat", source="dashboard"
                )

                def _compare_bindings():
                    return (
                        resolve_session_agent_bindings(
                            resolve_agent_bindings,
                            _cfg,
                            compared_binding[3],
                            compared_binding[0],
                            compared_binding[1] or None,
                        ),
                        resolve_agent_bindings(_cfg, agent, compared_binding[1] or None),
                    )

                _stored, _requested = await asyncio.to_thread(_compare_bindings)
                # Identity itself lives on ResolvedBindings, next to the field
                # set, so a new dispatch-relevant field cannot silently widen
                # this bypass. requested_resolved stays a separate caller-side
                # check: an unknown name would resolve to the default and MATCH
                # a default-bound slot, which is the lie this guard prevents.
                same_binding = _requested.requested_resolved and _stored.same_dispatch_binding(
                    _requested
                )
            except Exception:
                resolution_failed = True
                logger.warning(
                    "agent-conflict binding resolution failed; using strict name comparison",
                    exc_info=True,
                )
            if state._slots.get(slot.key) is not slot or compared_binding != (
                slot.agent,
                slot.project,
                slot.memory_store,
                effective_session_key(slot),
                slot.workspace,
                slot._app,
            ):
                return web.json_response(
                    {"error": "slot changed during agent resolution", "code": "session_rebound"},
                    status=409,
                )
            if not same_binding:
                _emit_agent_assignment(
                    slot.key,
                    agent or "",
                    outcome=(
                        "denied_resolution_failed" if resolution_failed else "denied_mismatch"
                    ),
                )
                return web.json_response({"error": "slot agent mismatch"}, status=409)
            # The one outcome of this guard that overrides a 409 boundary must be
            # auditable alongside the denials and adoptions it sits between.
            _emit_agent_assignment(slot.key, agent, outcome="allowed_same_binding")
            logger.debug(
                "agent names differ but resolve to the same binding: slot=%s stored=%s requested=%s",
                slot.key,
                slot.agent,
                agent,
            )
        else:
            logger.debug("agent match for slot=%s agent=%s", slot.key, agent)
    elif _send_binds_agent(agent, slot):
        # Slot has no agent — set it if not running
        if slot.running:
            _emit_agent_assignment(slot.key, agent, outcome="denied_running")
            return web.json_response(
                {"error": "cannot set agent on running slot"},
                status=409,
            )
        slot.agent = agent
        _emit_agent_assignment(slot.key, agent)
    else:
        # No agent on slot, no agent in request — nothing to enforce.
        pass

    if _send_writes_persona(body):
        slot.color_theme = color_theme
        slot.theme_consent = theme_consent
        slot.theme_consent_sha = theme_consent_sha

    if not message:
        # One guard, above every dispatch branch. An empty wire text reaches
        # here only from programmatic callers (app tokens, curl, integrations)
        # — the dashboard composer always inlines staged files into the
        # message text. Such a send may still carry attachments in `meta`:
        # nothing downstream queues or broadcasts it, so any success receipt
        # would report work that was silently dropped. Refusing here keeps
        # every branch below (steer/queue, crew, subagent-hold, new turn)
        # unable to bypass the check. A guard placed below the busy branch is
        # bypassable, and that is exactly how a false `queued: true` receipt
        # happens. `message_required` is the backend-owned code already used
        # for this refusal (handlers/messaging.py).
        return web.json_response(
            {"error": "message is required", "code": "message_required"}, status=400
        )

    if not request_app and not cron_creator:
        # The person typed this (no app token, no cron attestation; a peer's
        # `relay=1` was refused above): the one signal the Crewmates list orders
        # and filters by (crew_recency). An empty slot agent is the default crew,
        # recorded under "" and resolved by the roster read. Best-effort, off-loop.
        await asyncio.to_thread(crew_recency.record_user_chat, slot.agent or "")

    if slot.turn_running or slot._turn_admission_reserved:
        # Mid-turn steer: inject into the RUNNING turn instead of queueing for
        # the next turn. Gated on an explicit `steer` flag + a live, steer-capable
        # inner AcpClient that _run_chat published on the slot. App-authenticated
        # sends cannot steer because doing so would inherit the live turn's human
        # provenance; they fall through to the fail-closed queue below.
        # Fire-and-forget —
        # the inline steer card materializes when kiro-cli echoes steering_consumed
        # (EVENT_STEER_CONSUMED). If steer is requested but unavailable (no live
        # client / unsupported backend / RPC error), fall through to the queue
        # path so the user's text is NEVER silently dropped.
        #
        # `steer: "auto"` is the composer's third mode: the sender asked Jev which
        # of the two shipped paths this message takes. Decided HERE, above both
        # branches, because the answer chooses between them -- and only here, where
        # a turn IS running (this branch's own condition) and the send is the
        # session's own human, are the point's preconditions already established.
        #
        # Resolved before the steer `if` rather than inside it, so the steer block
        # below keeps its exact shape: the only thing `auto` changes about it is one
        # more conjunct on its condition and the receipt it stamps.
        _auto_strip: dict | None = None
        _auto_queues = False
        if steer_is_auto(steer) and not request_app:
            # The turn the question is ABOUT, captured before the await. The
            # decision is a provider round-trip, so the turn it describes can end
            # while it is in flight -- and an answer about a turn that is gone is
            # not an answer about this send: "interrupt what it is doing" names
            # work that finished, and a successor turn is a different subject.
            # On a change the send takes the manual steer path (this branch's own
            # default) and carries NO receipt, because the decision that was made
            # is not about the turn the message now reaches. The row is still in
            # the log, where it belongs -- the receipt is what would misattribute
            # it.
            _turn_before = slot.task
            _auto_queues, _auto_strip = await decided_message_handling(slot, message)
            if slot.task is not _turn_before:
                _auto_queues = False
                _auto_strip = None
        if steer and not request_app and not _auto_queues:
            # Client-minted send correlation id (the same `meta.sendId`
            # convention the plain send path persists): thread it through the
            # steer so the persisted row and the steer_push echo can be matched
            # back to the optimistic bubble by id rather than by text.
            # Raw client input — the sink (`normalize_send_id` at the top of
            # `steer_into_running_turn`) type-checks and length-bounds it,
            # treating anything unusable as absent (the old-client shape).
            # circular import: session_control imports this package's modules at module level.
            from kiro_crew.dashboard.session_control import containment_meta as _containment_meta

            outcome = await steer_into_running_turn(
                state,
                slot,
                message,
                send_id=user_meta.get("sendId") if user_meta else None,
                # This branch IS the composer: the text was typed into this
                # session's own surface by its authenticated human, which is what
                # earns a requeued entry the exemption from the drain's LINKED drop.
                # Stated rather than defaulted, because the default fails closed.
                user_origin=not bool(request_app),
                # Captured HERE, before the RPC suspends, for the same reason the peer
                # path captures it: the requeue runs in the turn's teardown and a slot
                # read there folds a mirror linked during the suspension into the
                # entry's own admission baseline. The LINKED exemption does not cover
                # that -- a new outbound mirror is never exempt, because the author
                # does not control mirror links -- so the composer needs the stamp too.
                admission=_containment_meta(state, slot),
                # The receipt for an `auto` send that was decided; absent for a
                # manual steer, which is what keeps that row byte-identical.
                decision_strip=_auto_strip,
                attachments=user_meta,
            )
            if outcome == STEER_STEERED:
                return web.json_response({"ok": True, "steered": True})
            if outcome == STEER_REQUEUED:
                # The turn's teardown moved it into the queue while the steer RPC
                # was suspended — queueing again would deliver the same text twice.
                return web.json_response({"ok": True, "queued": True})
            # steer requested but unavailable -> fall through to queue below.
        # Queue the message - return JSON immediately (no SSE needed).
        # The existing SSE reader will pick up queued messages as _run_chat
        # processes the queue in its finally block. The message is non-empty
        # here (hoisted guard above the busy branch), so `queued: true`
        # always reports a real enqueue. `queue_id` lets the sender bind its
        # pre-send composer state to THIS entry (the dashboard's cancel-queued
        # restore), which no content-based key can do: serialization is not
        # injective and other tabs can queue colliding content.
        #
        # The client's `meta.sendId` rides on the entry too (same gate as the
        # steer path above: `normalize_send_id` treats anything unusable as
        # absent). The drain unions entry meta onto the row it writes, so the
        # queued send's row ends up carrying the same id a dispatched send's row
        # gets from `slot.append(..., meta=user_meta)` below -- the only way a
        # sender can prove ITS message landed without matching by text.
        # The attachment lists (`meta.files` / `meta.dirs`) ride the same way:
        # the renderer resolves `[attached_file N]` markers against them, and a
        # drained row without them truncates a spaced path at its first space.
        qid = queue_for_next_turn(
            state,
            slot,
            message,
            directive_user_origin=not bool(request_app),
            # A queued turn reaches the runner through the DRAIN, so the dispatch
            # keyword this handler passes for an IMMEDIATE send cannot carry the
            # actor here. It rides the entry's meta instead, which is what
            # `_actor_for_queue_items` reads; unstamped, the drain falls back to
            # `user` and files an app's send as a person's.
            turn_actor="app" if request_app else "",
            send_id=normalize_send_id(user_meta.get("sendId")) if user_meta else None,
            attachments=attachment_meta(user_meta),
            quote=quote_meta(user_meta).get("quote"),
            # The receipt travels whichever way the send went, including the one
            # case where the two disagree: `auto` answered steer and the steer was
            # UNAVAILABLE, so this path runs with a record saying steer. That is the
            # truth of the decision, and the row's own `steerState` is what says how
            # the delivery ended -- a receipt withheld there would lose the only
            # record that a decision was made at all.
            decision_strip=_auto_strip,
        )
        return web.json_response({"ok": True, "queued": True, "queue_id": qid})

    # Queue a message typed while background sub-agents are still running for
    # this slot. The slot.running queue path above covers the mid-turn case;
    # this covers the idle case (spawn_run is fire-and-forget, so the main slot
    # goes idle while children run). Without the hold, this message would start a
    # main turn immediately and interleave with the [Subagent completion event]
    # injections. Queue it instead (reusing the slot queue) — the queue drain
    # releases it after the last sub-agent finishes (see chat_runner _hold_users).
    # Opt-out: if the user explicitly chose steer mode, honour it — start a new
    # turn immediately so the message is processed without waiting for children.
    # An app on a session it does not own has no opt-out (`steer` is None there).
    _sub_key = effective_session_key(slot)
    _held = subagents_hold_user_messages(state, _sub_key)
    # Messages parked while a child was live stay parked once it is flagged
    # stalled (nothing starts a drain then). A send behind them joins the queue
    # and drains it, so it is answered after them rather than ahead of them.
    _behind_parked = bool(
        not _held
        and slot._queue
        and state.subagents is not None
        and state.subagents.running_agents_for(_sub_key)
    )
    if not steer and (_held or _behind_parked):
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.session_control import containment_meta

        # Same entry-meta contract as the busy-slot branch: the client's `sendId`
        # and attachment lists ride on the queue entry so the drained row
        # carries them.
        _hold_meta: dict = containment_meta(state, slot)
        _hold_sid = normalize_send_id(user_meta.get("sendId")) if user_meta else None
        if _hold_sid:
            _hold_meta["sendId"] = _hold_sid
        _hold_attachments = attachment_meta(user_meta)
        _hold_meta.update(_hold_attachments)
        _hold_meta.update(quote_meta(user_meta))
        if request_app:
            # Same reason as the busy-slot queue above: this entry is drained
            # later, so only its meta can name the actor.
            _hold_meta[TURN_ACTOR_META_KEY] = "app"
        qid = slot.queue_append(
            message,
            meta=_hold_meta,
            directive_user_origin=not bool(request_app),
        )
        _redacted = queued_text_for_display(message, user_origin=not bool(request_app))
        warn_if_not_durable(slot._queue, qid, slot.key)
        # Start the durable write here too, not only in the busy-slot branch.
        # This branch holds an IDLE slot, so no drain is coming to write the
        # prompt's transcript row and no turn-end flush is scheduled: the queue
        # is the only record of the user's words until the last sub-agent
        # finishes, which is unbounded. Waiting for the periodic flush would
        # leave a window as wide as its interval, so the accept and the write
        # start from the same place. Same single-flight and same self-limiting
        # skip as the other caller.
        start_queue_persist(state, slot)
        _hold_push: dict[str, Any] = {
            "slot": slot.key,
            "content": _redacted,
            "ts": datetime.now(timezone.utc).isoformat(),
            "queue_id": qid,
        }
        if _hold_attachments:
            # Same as the busy-slot frame: the card is a cancel's restore source.
            _hold_push["meta"] = _hold_attachments
        if _hold_meta.get("quote"):
            _hold_push.setdefault("meta", {})["quote"] = _hold_meta["quote"]
        state.broadcast_ws("queue_push", _hold_push)
        if _behind_parked:
            await _start_next_queued_turn(state, slot)
        # Same receipt contract as the busy-slot queue branch: `queue_id` binds
        # the sender's pre-send composer state to this exact entry. An entry the
        # durable bounds refuse is reported in the log by the call above, not on
        # the receipt: the on-screen marker belongs with its consumer.
        return web.json_response({"ok": True, "queued": True, "queue_id": qid})

    # WS mode: return JSON immediately, chunks delivered via WebSocket
    ws_mode = request.query.get("ws") == "1"

    # Only block the global broadcast for an HTTP SSE reader that IS the slot's
    # own client. An app streaming a turn on a user's session is not the user's
    # dashboard, which keeps receiving its rows.
    slot._has_reader = not ws_mode and may_configure
    # That app's stream ends with its own turn (`TURN_END_WIRE_CLS`), so the
    # user's queued follow-ups never reach it.
    turn_scoped_stream = not may_configure
    slot._file_changes = []  # Reset file-change accumulator for the new turn
    # ── Sweep orphaned permissions from prior turns ──
    _sweep_stale_permissions(slot)

    # Refuse a relay archive BEFORE the send is recorded: a refusal that appended
    # first would leave a user row in local history for a turn that never ran.
    # Every turn-refusing validation above (app ownership, member reserve,
    # busy/steer/queue) has already run, so their answers still win.
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal

    # No per-message browse marker: browsing is a capability, not a per-turn
    # gate. The agent drives a browser by running `playwright-cli` shell
    # commands, so the capability is simply whether that binary is on PATH. The
    # agent itself decides whether to operate a browser or read with web_fetch
    # (the system prompt and the kirocrew-commands / web-browse skills tell it
    # how), so the backend injects nothing here.

    # A slot created by this send binds to its member's private store BEFORE
    # the user row is appended: a store failure then returns with nothing
    # persisted, and the assignment snapshot (agent, project, workspace,
    # session, message count) proves no other request rebound the slot while
    # the store was being resolved.
    if created_in_send:
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        if is_owner_dashboard_request(request):
            async with slot._lock:
                assignment = (
                    slot.agent,
                    slot.project,
                    slot.workspace,
                    effective_session_key(slot),
                    len(slot.messages),
                )
                selection_change = None
                try:
                    cfg = await asyncio.to_thread(KiroCrewConfig.load)
                    assigned_store = await pin_private_agent_store(
                        state, assignment[3], assignment[0], cfg, memory_mode=slot.memory_mode
                    )
                    chosen = await asyncio.to_thread(
                        resolve_agent_bindings,
                        cfg,
                        assignment[0],
                        assignment[1] or None,
                        validate_memory_files=False,
                    )
                    selection_change = await _record_explicit_agent_selection(
                        assignment[3],
                        assignment[0],
                        chosen,
                        config=cfg,
                        memory_mode=slot.memory_mode,
                        app=slot._app or "",
                    )
                except Exception as exc:
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    return _store_unavailable_response(slot.memory_store, exc)
                if (
                    state._slots.get(slot.key) is not slot
                    or slot.running
                    or assignment
                    != (
                        slot.agent,
                        slot.project,
                        slot.workspace,
                        effective_session_key(slot),
                        len(slot.messages),
                    )
                ):
                    await drained_to_thread(
                        restore_agent_selection, assignment[3], selection_change
                    )
                    return web.json_response(
                        {
                            "error": "Could not save the member assignment. Try again.",
                            "code": "session_rebound",
                        },
                        status=409,
                    )
                if assigned_store:
                    slot.memory_store = assigned_store

    # A dashboard's busy snapshot can suppress its optimistic user bubble even
    # when this send starts a turn. Echo correlated sends BEFORE starting the
    # reply so every pane sees the user row in order, independently of when the
    # HTTP receipt arrives. sendId/mid reconcile an existing optimistic bubble;
    # callers without a correlation id keep their existing delivery contract.
    _user_row_meta = _redact_meta(user_meta) if user_meta else {}
    if user_meta and not request_app and isinstance(user_meta.get("quote"), dict):
        # The human's own quote record stays as typed, like the row's content
        # it must byte-match (`_redact_meta_for_role` keeps the same rule on
        # every later persist and emit); an app's was redacted at the bound.
        _user_row_meta["quote"] = user_meta["quote"]
    if not request_app:
        # A PERSON typed this. Marked explicitly rather than inferred, because the
        # row's role and presentation class cannot tell it apart from a turn the
        # gateway drives on its own (see history.HUMAN_TURN_META_KEY). An app
        # token reaches this same handler, so the marker rides the same
        # app-origin signal as `user_origin` and `turn_actor` above: an app's
        # send is not a human turn and must not advance the ranking stamp.
        _user_row_meta[HUMAN_TURN_META_KEY] = True
    else:
        # The queued path stamps the same actor on its entry, which the drain
        # unions onto the row; the title counter reads it off either row.
        _user_row_meta[TURN_ACTOR_META_KEY] = "app"
    _user_row = slot.append("user", message, "msg msg-u", meta=_user_row_meta)
    _user_mid = _user_row.get("meta", {}).get("mid")
    if not may_configure or (ws_mode and user_meta and user_meta.get("sendId")):
        # Raw user content belongs on the per-client slot-authorized WS path.
        # The global SSE queues have no slot gate. In-band sends keep
        # their existing stream contract and must not gain an extra WS echo.
        # An app's turn on a user's session always reaches the user's open tabs,
        # whose composer never drew it.
        state.broadcast_ws(
            "chat_message",
            chat_message_frame({**_user_row, "slot": slot.key}, include_metadata=True),
        )

    # Note: untitled slots display as "New Session…" via _ChatSlot.display_title
    # (serialization layer), so there's no bare chat-N flash to patch here. The
    # LLM titling is kicked off below, before _run_chat.

    # ── AutoNudge: user input cancels any pending nudge timer (user wins). ──
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_handlers
        )

        _autonudge = _autonudge_get()
        if _autonudge is not None:
            # A person typing also ends an approval hold. Only on positive
            # evidence of a dashboard user: an app, or the internal loopback
            # secret a cron script or MCP tool sends with, is not one.
            _autonudge.notify_user_input(
                slot.key,
                human=request.get("is_dashboard_user") is True
                and request.get("internal_auth") is not True,
            )
    except Exception:
        logger.warning("autonudge.notify_user_input failed", exc_info=True)

    # Drain stale pending messages from previous turns that completed
    # after their SSE reader disconnected. Must happen BEFORE _run_chat
    # so we don't discard the new turn's output.
    slot.drain()

    # Kick off LLM titling now, from the first user message, so the title lands
    # *during* the first turn instead of waiting for the whole response to
    # finish (chat_done). Runs on an isolated background kiro-cli session
    # concurrent with the turn. No-ops once titled / in-flight; the instant
    # 60-char provisional stays as the fallback if the LLM SKIPs or errors.
    # Not from an app's text on a user's session: the user's own turns name it.
    if may_configure and not slot._titled and not slot._title_in_flight:
        _tt = asyncio.create_task(_maybe_auto_title(state, slot))
        # Expose the handle so chat_done's chained title→refresh pass can wait
        # for this attempt to settle (see _title_then_refresh in chat_runner).
        slot._title_task = _tt
        state._background_tasks.add(_tt)
        _tt.add_done_callback(state._background_tasks.discard)

    # Auto-tag: derive a tag from the session's project directory (deterministic,
    # no LLM). Fire-and-forget, same pattern as auto-title.
    if not getattr(slot, "_auto_tagged", False):
        _at = asyncio.create_task(maybe_auto_tag(state, slot))
        state._background_tasks.add(_at)
        _at.add_done_callback(state._background_tasks.discard)

    _reserve_turn_admission = not request_app
    if _reserve_turn_admission:
        slot._turn_admission_reserved = True
    try:
        # Optional Slack thread for a NEW session (slack.auto_link_sessions), made
        # here rather than in the turn: the runner's echo below reads the link at
        # turn start, so the link has to exist before dispatch or the first message
        # never reaches the thread. Awaited, not fire-and-forget, for the same
        # reason; bounded inside. A link that lands after the hold still links,
        # and the thread receives the turns that start after it: this turn is
        # not replayed into it. Only a person's own send qualifies: an app-token
        # send is an injection into the slot, not the person opening it, and the
        # eligibility test rules out every non-dashboard origin besides.
        if _reserve_turn_admission:
            # Counts Stop presses; read before the await so one that lands inside it
            # is seen even though it found no task to cancel and settled at once.
            _stop_gen_before_hold = slot._stop_generation
            await maybe_auto_link_slack(state, slot)
            # The await above is the one suspension between accepting the row and
            # dispatching the turn; a close that lands inside it must not have its
            # turn run on the detached slot.
            if not slot_is_live(state, slot):
                return web.json_response(
                    {"error": "session closed", "code": "slot_closed"}, status=409
                )
            # A Stop pressed during the hold was aimed at this turn: it does not
            # start, and the row stays as a message that was stopped unanswered.
            # A send queued behind it during the hold starts now, as it would at
            # the end of a stopped turn.
            if slot._stop_generation != _stop_gen_before_hold:
                # The reservation stays up across the drain's awaits (the
                # `finally` below drops it), so a send arriving meanwhile queues
                # instead of dispatching a second turn beside the drained one.
                if slot._queue:
                    await _start_next_queued_turn(state, slot)
                return web.json_response({"ok": True, "slot": slot.key, "stopped": True})

        # Edition message observer (CPP seam). Fire-and-forget, fail-safe: a
        # companion uses this to auto-ingest doc links pasted into chat. The public
        # Default is a no-op. Guarded so an observer error never blocks the turn;
        # deferred context read via the sel.py pattern (no platform import at load).
        try:
            from kiro_crew.platform.context import current_context, safe_context_call

            safe_context_call(
                lambda: current_context().dashboard.on_user_message(request.app, message),
                fallback=None,
                log_message="dashboard.on_user_message observer failed",
            )
        except Exception:
            logger.debug("on_user_message observer raised; ignoring", exc_info=True)

        # An unattended app-owned turn runs under the background concurrency
        # cap; run_background_turn passes an attended slot straight through, so the
        # interactive path is unchanged (no semaphore is even created).
        # The attachment ids this handler just accepted, so the ledger names the file
        # instead of leaving the turn's input unexplained. Passed only when there ARE
        # some: an ordinary send then calls `_run_chat` with exactly the arguments it
        # always did, which is what keeps the many test doubles of it valid.
        _accepted_attachment_meta = attachment_meta(user_meta)
        _accepted_attachments = [
            path for paths in _accepted_attachment_meta.values() for path in paths
        ]
        # ``request_app`` is stamped by the app-token auth middleware, not read from
        # the request body, so it is a fact about the caller a person cannot write --
        # which is what lets the turn's actor come from it. Passing it is what keeps
        # an app-authored send out of the ledger's ``user`` bucket: the actor
        # resolver's fallback is ``user``, so a site that observes an app and stays
        # silent records a person who never typed anything.
        _turn_kwargs: dict = {"_directive_user_origin": not bool(request_app)}
        if request_app:
            _turn_kwargs["_turn_actor"] = "app"
        if _accepted_attachments:
            _turn_kwargs["_attachments"] = _accepted_attachments
            # Typed form for the refusal replay: keeps ``dirs`` entries as folders.
            _turn_kwargs["_attachment_meta"] = _accepted_attachment_meta
        task = spawn_guarded_turn(
            state,
            slot,
            state.run_background_turn(slot, _run_chat(state, slot, message, **_turn_kwargs)),
        )
        slot.task = task
    finally:
        if _reserve_turn_admission:
            slot._turn_admission_reserved = False
    slot.recovery_retrigger_count = 0
    state.push_slots_update()

    if ws_mode:
        # Carry the server-minted user-row `mid` back (see the append above). A
        # confirmed dashboard send reconciles it onto the optimistic bubble so
        # the message-pin control lights up immediately instead of only after
        # the chat_done refresh. Omitted when absent so the receipt shape is
        # unchanged for callers that never minted one.
        _receipt: dict[str, Any] = {"ok": True, "slot": slot.key}
        if _user_mid:
            _receipt["mid"] = _user_mid
        return web.json_response(_receipt)

    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    # Declare this reader as the owner of `slot._pending` for as long as it is
    # draining, from before the first await after dispatch. A turn-end chunk
    # release must not run while an SSE reader still has undelivered tokens
    # queued, and `_has_reader` alone cannot carry that: the `done` branch below
    # clears it before this scope ends, and a turn-scoped reader never sets it.
    with slot.pending_consumer():
        try:
            await resp.prepare(request)
        except BaseException:
            # Release the broadcast this reader suppressed; the dispatched turn
            # keeps running, exactly as it does when the reader disconnects
            # mid-stream.
            slot._has_reader = False
            raise
        try:
            while True:
                pending = slot.drain()
                for msg in pending:
                    # Fail closed even if a dispatch path misses turn_end: only
                    # this request's gateway-minted user row belongs to its SSE.
                    foreign_user_row = msg.get("role") == "user" and (
                        not _user_mid or row_mid(msg) != _user_mid
                    )
                    if msg["cls"] == "done" or (
                        turn_scoped_stream and (msg["cls"] == TURN_END_WIRE_CLS or foreign_user_row)
                    ):
                        await resp.write(b"data: [DONE]\n\n")
                        slot._has_reader = False
                        return resp
                    if msg["cls"] == TURN_END_WIRE_CLS:
                        # A boundary for turn-scoped readers, not a row.
                        continue
                    if turn_scoped_stream and msg.get("role") == "queued":
                        # A placeholder for a later turn (a cron notification or
                        # an MCP-App message queued meanwhile), never this one.
                        continue
                    chunk = _build_stream_chunk(msg)
                    await resp.write(f"data: {chunk}\n\n".encode())
                try:
                    await asyncio.wait_for(slot.event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
        except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            slot.drain()
            slot._has_reader = False
    return resp


_source_link_unlink_tasks: set[asyncio.Task] = set()


async def api_chat_slot_summary(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/summary — intent summary for the panel.

    Read-only: it never triggers generation. Summaries are produced at turn end
    by the background pass, deliberately, so that opening the panel cannot spend
    tokens and repeated opening cannot turn into a refresh loop.

    Responses:
      - 200 with ``{enabled, generated_at, stale, intents, constraints, ...}``
      - 200 with ``intents: []`` and ``enabled: false`` when the feature is off,
        so the panel can render an explanatory empty state rather than an error
      - 404 ``slot_not_found`` for an unknown slot, or for a slot an app caller
        does not own (App Kit §5.2 isolation; 404 not 403 for anti-enumeration)
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check (App Kit §5.2), mirroring api_chat_slot_delete: a
    # summary is derived conversation content, so a slot merely existing must
    # not make it readable. Dashboard users carry an explicit empty request_app
    # and are unaffected; an app token may only read summaries for slots it
    # created, never for unscoped slots.
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_summary_read")
    if denied is not None:
        return denied

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    enabled = bool(cfg.session_summary.enabled)

    payload: dict | None = None
    stale = False
    log = state.conversation_log
    # Gate the cache read on the flag as well: turning the feature off has to
    # stop serving summaries, not just stop producing them, or a sidecar written
    # during an earlier opt-in keeps being returned after opt-out.
    if enabled and log is not None:
        payload, stale = await read_cached_intent_summary(log, slot)

    body: dict = {
        "enabled": enabled,
        "stale": stale,
        "intents": (payload or {}).get("intents", []),
        "constraints": (payload or {}).get("constraints", []),
        "generated_at": (payload or {}).get("generated_at"),
        "user_turns": (payload or {}).get("user_turns"),
        "last_activity": (payload or {}).get("last_activity"),
        "generate_state": _generate_state(cfg, slot),
    }
    return web.json_response(body)


def _generate_state(cfg: KiroCrewConfig, slot: Any) -> str:
    """Which on-demand affordance the panel should offer for *slot*.

    Three values, because the panel has three honest things to say and a bool
    could only carry two: ``ready`` (offer the button), ``too_few_turns`` (say so
    plainly and offer nothing -- a click could only fail), and ``unavailable``
    (the feature is off, a pass is already running, or the session is incognito
    and must never leave a durable artifact). Collapsing the last two into
    "not enough messages" would print a reason that is simply untrue for an
    incognito session.

    The turn count is an ESTIMATE from the slot's IN-MEMORY messages, not a
    transcript read: this runs on every panel mount and tab switch, and reading a
    thousand-message session from disk to answer a yes/no question is waste. A
    restored slot keeps only a window of its transcript, and the window is NOT a
    safe proxy for the whole session -- a tail made mostly of assistant replies
    and injected automation messages can hold fewer than the minimum genuine user
    turns while the file holds dozens. So `too_few_turns` is only claimed when the
    window IS the whole session (`_disk_older_count == 0`); a truncated window
    reports `ready` and lets the POST's disk-backed count decide.

    The authoritative gate lives in the generator and reads disk; if this estimate
    is wrong the POST refuses and says why, so the cost is a refused click, never
    a wasted call.

    A turn in flight is deliberately NOT one of these values, even though the
    generator refuses one. This field is only refreshed when a summary is written,
    so a state that begins and ends mid-turn would arrive stale and stay stale: a
    turn that ends without producing a summary (stopped, or gated by cadence)
    pushes no event, and the panel would sit on a dead verdict until it remounted.
    The panel already holds a live per-slot turn signal, so it owns that
    presentation and this field stays limited to what only the server knows.
    """
    if not cfg.session_summary.enabled:
        return "unavailable"
    if getattr(slot, "_summary_in_flight", False):
        return "unavailable"
    if is_incognito_transcript(getattr(slot, "memory_mode", "")):
        return "unavailable"
    turns = count_user_turns_in_records(getattr(slot, "messages", []) or [])
    if turns < cfg.session_summary.min_user_turns and not getattr(slot, "_disk_older_count", 0):
        return "too_few_turns"
    return "ready"


async def api_chat_slot_summary_generate(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/summary — summarize this session on request.

    The companion to the read-only GET. Generation stays off the read path so
    that opening the panel can never spend tokens; this route exists because the
    turn-end trigger alone leaves every session that predates the feature -- or
    that simply has not been touched since it was switched on -- permanently
    empty, with nothing a person can do about it from the panel.

    Explicit consent is the whole justification for the spend, so there is no
    batch form: one request summarizes one session.

    Responses:
      - 200 with the same body as the GET, once a summary exists
      - 409 ``summary_disabled`` / ``summary_in_flight`` / ``summary_unavailable``
        when no summary could be produced, so the panel can say which
      - 404 ``slot_not_found`` for an unknown slot, or one an app does not own
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # Same App Kit §5.2 isolation as the GET: generating is strictly more
    # privileged than reading, so it can never be the laxer of the two.
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_summary_generate")
    if denied is not None:
        return denied

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if request.get("app", "") and state._slots.get(name) is not slot:
        return slot_not_found()
    if not cfg.session_summary.enabled:
        return web.json_response(
            {"error": "session summaries are switched off", "code": "summary_disabled"},
            status=409,
        )
    log = state.conversation_log
    if log is None:
        return web.json_response(
            {"error": "no conversation log", "code": "summary_unavailable"},
            status=409,
        )
    # Reported separately from the generic failure because it is the one the
    # panel can explain as "already working" rather than "could not".
    if getattr(slot, "_summary_in_flight", False):
        return web.json_response(
            {"error": "a summary is already being written", "code": "summary_in_flight"},
            status=409,
        )
    # Likewise distinct: a turn in flight is a wait-and-retry, not a refusal. The
    # generator would decline anyway; saying so here keeps the panel from
    # reporting a transient state as a failure.
    if getattr(slot, "running", False):
        return web.json_response(
            {"error": "this session has a turn in progress", "code": "summary_turn_running"},
            status=409,
        )

    if request.get("app", ""):
        await generate_session_summary(
            state,
            slot,
            cfg=cfg,
            force=True,
            still_current=lambda: state._slots.get(name) is slot,
        )
        if state._slots.get(name) is not slot:
            return slot_not_found()
    else:
        await generate_session_summary(state, slot, cfg=cfg, force=True)

    # Read back rather than trusting the return value: a forced pass returns
    # False both when it produced nothing AND when the cached summary was
    # already current, and those are opposite outcomes for the panel. Through
    # the same gated read as the GET: a pass the generator skipped for
    # ``memory_mode`` must not be answered with a sidecar left over from the
    # key's earlier persistent life.
    payload, stale = await read_cached_intent_summary(log, slot)
    if request.get("app", "") and state._slots.get(name) is not slot:
        return slot_not_found()
    if payload is None:
        return web.json_response(
            {"error": "could not summarize this session", "code": "summary_unavailable"},
            status=409,
        )
    return web.json_response(
        {
            "enabled": True,
            "stale": stale,
            "intents": payload.get("intents", []),
            "constraints": payload.get("constraints", []),
            "generated_at": payload.get("generated_at"),
            "user_turns": payload.get("user_turns"),
            "last_activity": payload.get("last_activity"),
            "generate_state": _generate_state(cfg, slot),
        }
    )


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
_UNOWED_WINDOW_ROLES = _TRANSIENT_ROLES - {"permission", "chunk", "streaming"}


# Modes a slot may be CREATED with. A deliberate superset of the mode-SWITCH
# allowlist (chat_folders._VALID_MODES) and the fork override allowlist
# (chat_fork): "design-critique" is an app-worker mode assigned at birth by the
# Design Critique app's openSlot() — the custom mode keeps its throwaway dc-*
# slots off the chat sidebar, which renders only plain "" slots
# (ChatPage.tsx filteredSlots). Switching an existing session INTO an app-worker
# mode, or forking one with it as an override, is not a real flow, so those two
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
    }
)


async def api_chat_slot_create(request: web.Request) -> web.Response:
    """POST /api/chat/slots — create a new chat slot."""
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # A session on a connected crew is created ON that crew and opened here
    # through its window; this hub never mints or adopts a relay binding.
    # Refused before anything is read or written, so a stale client cannot stamp
    # a remote executor onto a new local slot. The answer is the same for every
    # caller and every state, so it discloses nothing.
    if body.get("instance_id") or body.get("adopt_remote_slot"):
        return web.json_response(
            {
                "error": "sessions on a connected crew are created on that crew, not here",
                "code": "remote_create_retired",
            },
            status=400,
        )
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        # `get_or_create_slot` normalizes the key with string operations, so a
        # non-string name would surface there as an unhandled 500.
        name = str(name)
    agent = body.get("agent", "")
    # The selection NAMESPACE, when the caller states one. "member" names a
    # configured crew, "template" a shared provider template; an omitted kind
    # keeps the legacy name-only resolution. The kind is selection input, never
    # authority: every owner, app and private-memory gate below still applies.
    agent_kind = body.get("agent_kind", "")
    if agent_kind not in ("", "member", "template"):
        return web.json_response(
            {"error": "invalid agent kind", "code": "invalid_agent_kind"}, status=400
        )
    model = body.get("model", "")
    # Folder membership at BIRTH. Assigning it afterwards (client PATCH) is
    # visibly too late: get_or_create_slot broadcasts the new slot before this
    # handler returns, so the dashboard renders it at the top level for a frame
    # or two and it then jumps into the folder. Validated exactly as
    # PATCH /api/chat/slots/{slot}/folder validates it.
    folder_id = str(body.get("folder_id") or "")
    if folder_id and not any(f["id"] == folder_id for f in state._folders):
        return web.json_response(
            {"error": "folder not found", "code": "folder_not_found"}, status=400
        )
    existing_slot = state._slots.get(_normalize_slot_key(str(name))) if name else None
    request_app = request.get("app", "")
    # Ownership of a NAMED slot for an app caller, before any refusal below can
    # answer about it (get_or_create_slot's memory-mode 409 among them) and
    # before anything is written. The post-create check stays authoritative.
    acquisition_judged: Any = None
    if request_app and name:
        denied, acquisition_judged = await _app_slot_acquisition_denial(
            request, state, request_app, str(name), "chat_slot_create", session_grant_route=False
        )
        if denied is not None:
            return denied
        existing_slot = state._slots.get(_normalize_slot_key(str(name)))
    memory_mode = body.get("memory_mode", "persistent")
    if memory_mode not in ("persistent", "incognito", "temporary"):
        return web.json_response({"error": "invalid memory_mode"}, status=400)
    _mode = _coerce_requested_mode(body.get("mode", ""))
    if _mode not in _CREATABLE_MODES:
        return web.json_response({"error": "invalid mode", "code": "invalid_mode"}, status=400)
    # A member-* name is RESERVED for DM threads (born only through the member
    # thread endpoint); `get_or_create_slot` below rejects it with a ValueError
    # that becomes a 409; refused here so the error carries its code. Checked on the
    # normalized key, the form the slot store is built from.
    if name and _normalize_slot_key(str(name)).casefold().startswith(
        members_mod.DM_SLOT_KEY_PREFIX
    ):
        return web.json_response(
            {
                "error": "member thread slots are created only via the member thread endpoint",
                "code": "member_slot_reserved",
            },
            status=409,
        )
    folder_project = ""
    if folder_id and (existing_slot is None or not existing_slot.project):
        folder_snapshot = await state.read_folders(
            lambda folders: [dict(folder) for folder in folders]
        )
        # The chain walk runs on the loop; only a declared project's ``stat``
        # hops to a worker thread (see `resolve_folder_project_dir_off_loop`).
        folder_project, folder_project_error = await resolve_folder_project_dir_off_loop(
            folder_snapshot, folder_id
        )
        if folder_project_error:
            return web.json_response(
                {
                    "error": f"invalid folder project: {folder_project_error}",
                    "code": "folder_project_invalid",
                },
                status=400,
            )
    # Binding resolution checks memory readiness. A fresh conversation gets
    # the same bounded recovery grace as its first turn, before any slot or
    # protected assignment is published.
    if existing_slot is None and is_owner_dashboard_request(request):
        try:
            await wait_for_memory_preparation(getattr(state, "memory_startup_task", None))
        except MemoryStartupUnavailable as exc:
            return web.json_response({"error": str(exc), "code": "store_unavailable"}, status=503)

    # Resolve workspace from agent bindings
    workspace = "default"
    cfg = None
    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        # Infra failure loading config must not block slot creation outright, so
        # validation below is skipped rather than failing closed.
        logger.warning("Failed to load config for slot create", exc_info=True)
    # An agent-less create means "use the default agent": stamp the RESOLVED
    # default alias into the slot instead of storing "", so the slot's
    # metadata records what will actually answer — otherwise the dashboard
    # footer chip renders its literal 'default' fallback while dispatch
    # quietly resolves the real default. Placed BEFORE the normalization
    # below so the stamped alias also gets its workspace resolved by the
    # existing binding path.
    if cfg is not None and not agent:
        agent = cfg.default_agent or ""
    # Normalize an agent nothing will dispatch to the one that WILL answer.
    # Otherwise the name is stored verbatim and resolve_agent_bindings silently
    # falls back to the default agent: the sidebar advertises the requested agent
    # while a different one answers, with none of its tools. Storing the real
    # agent keeps the slot honest, and a caller that requires a specific binding
    # (an app panel verifying the returned agent) can see the mismatch instead of
    # discovering it turns later.
    if cfg is not None and agent:
        resolving_key = _normalize_slot_key(str(name)) if name else ""
        resolving_slot = state._slots.get(resolving_key) if resolving_key else None
        resolving_fields = (
            (
                resolving_slot.agent,
                resolving_slot.project,
                resolving_slot.workspace,
                resolving_slot.memory_store,
                resolving_slot._app,
                effective_session_key(resolving_slot),
            )
            if resolving_slot is not None
            else None
        )
        try:
            # Resolved in the STATED namespace, so a template pick takes the
            # template's workspace rather than a same-name member's. No project
            # scope here: a create carries no slot yet, and the catalog offers a
            # slot-less chat global rows only, so this is the whole choice set.
            bindings = await asyncio.to_thread(
                resolve_agent_bindings,
                cfg,
                agent,
                validate_memory_files=False,
                selection_kind=agent_kind,
            )
            workspace = _workspace_name_for_dir(cfg, bindings.workspace_dir)
            if agent_kind and not bindings.requested_resolved:
                # A stated namespace never falls back to whoever answers by
                # default -- and it is refused HERE, before get_or_create_slot,
                # so a refused create registers no slot. The legacy name-only
                # path below keeps its store-verbatim-and-log behaviour.
                return web.json_response(
                    {
                        "error": "the selected agent choice is not available",
                        "code": "agent_choice_unavailable",
                    },
                    status=409,
                )
            if not bindings.requested_resolved:
                # Log only — the requested binding is the user's intent and is
                # stored VERBATIM. Rewriting it to whatever currently answers was
                # destructive: the resolution behind that decision can be
                # momentarily stale while the overwrite is permanent, so a valid
                # binding could be silently rebound to the default forever, where a
                # verbatim name recovers as soon as it resolves. Surfacing the
                # effective agent to the UI is a separate, non-destructive change.
                logger.info(
                    "Slot %s requested agent %r, which currently resolves to %r",
                    name,
                    agent,
                    bindings.resolved_alias or "(default)",
                )
        except Exception:
            logger.warning("Failed to resolve bindings for slot create", exc_info=True)
        if resolving_key and (
            state._slots.get(resolving_key) is not resolving_slot
            or (
                resolving_slot is not None
                and resolving_fields
                != (
                    resolving_slot.agent,
                    resolving_slot.project,
                    resolving_slot.workspace,
                    resolving_slot.memory_store,
                    resolving_slot._app,
                    effective_session_key(resolving_slot),
                )
            )
        ):
            return web.json_response(
                {"error": "slot changed during agent resolution", "code": "session_rebound"},
                status=409,
            )
    # Resolved before the mint decision below: nothing may await between that
    # read and `get_or_create_slot`, and these are the two awaits this path adds.
    cron_creator = await cron_slot_creator(request)
    persisted_cron_slot = False
    persisted_cron_meta: dict[str, Any] = {}
    if cron_creator and name:
        refusal, persisted_cron_slot, persisted_cron_meta = await cron_creator_admission(
            request, state, str(name), cron_creator
        )
        if refusal is not None:
            return refusal

    # Whether this request will MINT a new live slot, decided before
    # get_or_create_slot runs. `name` can address an already-open slot, which
    # returns unchanged. A persisted key with no live slot still follows the
    # normal create path. Its saved title is handled separately below.
    # Computed on the normalized key, which is the key the slot store is built
    # from; an omitted or degenerate name is always a mint. The creator fence
    # already read any saved cron state before this decision.
    _requested_key = _normalize_slot_key(str(name)) if name else ""
    reopening_persisted_cron = bool(
        cron_creator
        and _requested_key
        and persisted_cron_slot
        and _requested_key not in state._slots
    )
    is_new_slot = not _requested_key or _requested_key not in state._slots
    # A named NEW app slot must not land on a transcript the app does not own:
    # the slot's first save would stamp the app onto that metadata line, which is
    # what /api/sessions trusts.
    if (
        is_new_slot
        and _requested_key
        and await _app_claim_refused(
            state, request_app, "chat_slot_create", (_history_key_for(_requested_key),)
        )
    ):
        return slot_not_found()

    # Coalesce every push inside into ONE broadcast at exit, so the first frame
    # any client sees already carries the folder, title, artifact binding and
    # project. Otherwise each of those is a separate post-create correction the
    # UI renders as a jump.
    #
    # A successful ordinary local New Chat is different: the POST response itself
    # carries the complete slot and the client inserts it before activating the
    # tab. Serializing every open slot before returning only makes that first paint
    # wait. The state-owned 10 ms handoff moves its coalesced full-list frame past
    # the response. Only known request keys may qualify; metadata-bearing,
    # app-owned, mode-specific and nonpersistent creates remain
    # synchronous. Errors before a successful handoff retain request-visible
    # publication semantics; delayed publication failures are logged and retried
    # once by the guarded callback.
    defer_plain_create_broadcast = bool(
        set(body).issubset(_DEFERRED_PLAIN_CREATE_KNOWN_KEYS)
        and is_new_slot
        and not request_app
        and not folder_id
        and not _mode
        and not body.get("title")
        and not body.get("artifact")
        and memory_mode == "persistent"
        and not body.get("ephemeral")
    )
    async with contextlib.AsyncExitStack() as creation_stack:
        request_deferred_flush = creation_stack.enter_context(state.suspend_slots_push())
        if request_app and _requested_key:
            denied = await _app_slot_acquisition_recheck(
                state, request_app, _requested_key, acquisition_judged, "chat_slot_create"
            )
            if denied is not None:
                return denied
            is_new_slot = _requested_key not in state._slots
        try:
            slot = state.get_or_create_slot(
                name,
                agent=agent,
                workspace=workspace,
                model=model,
                mode=_mode,
                memory_mode=memory_mode,
                ephemeral=body.get("ephemeral"),
                app=request.get("app", ""),
                origin=request_slot_origin(request.get("app", ""), cron_creator=cron_creator),
                # Human request-layer path: the dashboard new-chat tab. The
                # origin conjunct in state.py still excludes app-token callers.
                # A slot an attested cron opens is not a person's and is not
                # counted.
                count_user_session=not cron_creator,
            )
        except ValueError as exc:
            if request_app:
                return _app_acquisition_conflict(
                    state,
                    request_app,
                    _normalize_slot_key(str(name)) if name else "",
                    acquisition_judged,
                    exc,
                )
            return web.json_response({"error": str(exc)}, status=409)
        if reopening_persisted_cron:
            # The creator fence already loaded and authorized this metadata
            # snapshot. Reuse it so a closed-session reopen restores the user's
            # saved title provenance without a second store read.
            persisted_title = persisted_cron_meta.get("title")
            if isinstance(persisted_title, str) and persisted_title:
                _rehydrate_slot_title(
                    slot,
                    persisted_title,
                    titled=True,
                    metadata=persisted_cron_meta,
                )
                _rebase_rehydrated_refresh_mark(slot)
        if cron_creator and is_new_slot:
            # Same attribution as the send auto-create: a cron-opened slot
            # carries its creator and never reads as a person's own tab.
            slot._created_by = cron_creator
        if is_new_slot and cfg is not None:
            if is_owner_dashboard_request(request):
                # Take the slot lock before the newborn's first await: a later
                # same-name member pick changes its namespace without changing
                # the agent/project strings checked after publication.
                await creation_stack.enter_async_context(slot._lock)
        if slot.is_restricted:
            logger.info("Slot %s created with memory_mode=%s", slot.key, slot.memory_mode)
        # App ownership check (App Kit §5.2), same deny-by-default rule as
        # api_chat_send. It matters HERE because `name` can address an
        # ALREADY-EXISTING slot: get_or_create_slot returns that slot without
        # consulting ownership, and everything below mutates it (folder, title,
        # artifact binding). Without this an app token could refile or retitle
        # another app's — or the dashboard's — session. A slot this request just
        # created carries `_app == request_app`, so it passes unless
        # get_or_create_slot linked it to another conversation's session (the
        # session half, same rule as every per-slot route); a dashboard caller
        # (empty app) keeps full access.
        # `request_app` is read once at the top of the handler, because the remote
        # binding gate up there needs the same value before the peer is touched.
        # One body for every reason on purpose: a distinct code per reason would
        # turn this 404 into an existence oracle for slots the caller may not
        # know about. The reason stays in the audit row.
        denied = deny_app_slot_session_access(request_app, slot, slot.key, "chat_slot_create")
        if denied is not None:
            return denied
        # Pin title if explicitly provided (prevents auto-title from overwriting).
        # A cron caller uses this route as both create and reopen. Its title names
        # the new session, but a later reopen must not undo a user's rename.
        title = (body.get("title") or "").strip()[:200] if isinstance(body, dict) else ""
        if cron_creator and (not is_new_slot or reopening_persisted_cron):
            title = ""
        if title:
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)
            slot.title = title
        if title:
            # A pinned title is caller-explicit: record origin "user" so the
            # background title refresh never rewrites it (this endpoint can
            # address an ALREADY-auto-titled slot whose origin would otherwise
            # stay "auto"), and bump the epoch so an in-flight background
            # attempt stands down instead of clobbering the pin.
            slot._titled = True
            slot._title_origin = "user"
            slot._title_epoch += 1
        # Bind to an artifact if provided (companion chat). Validate
        # against the artifact slug grammar so an injection-shaped value can never
        # land on the slot; anything invalid is silently dropped. Uniqueness (≤1
        # active bound session per slug) is a frontend-flow convention, not
        # enforced here.
        artifact_slug = body.get("artifact") if isinstance(body, dict) else None
        if isinstance(artifact_slug, str) and ARTIFACT_SLUG_RE.match(artifact_slug):
            slot._artifact = artifact_slug
        # File the slot before the coalesced broadcast, so its first appearance
        # in every client is already inside the folder.
        folder_applied = False
        if folder_id:
            # Mirror PATCH /api/chat/slots/{slot}/folder: a CHANGED folder must
            # re-inject the [FOLDER] breadcrumb on the next turn. `is_new` alone
            # is not enough — `name` can address an already-used slot, whose
            # turn is `is_new=False`, so moving it would otherwise leave the
            # model believing the session is still in its old folder.
            # Harmless on the new-slot path: that turn is `is_new`, so the
            # breadcrumb fires regardless and the flag is consumed there.
            previous_folder = slot.folder_id
            previous_changed = slot._folder_changed
            if folder_id != slot.folder_id:
                slot._folder_changed = True
            slot.folder_id = folder_id
            # Existence is only reliable inside the store lock. If the folder
            # went away, abandon THIS assignment and leave the slot as it was —
            # `name` can address an already-used slot, so clearing outright would
            # unfile a conversation that was sitting in a perfectly good folder
            # of its own. This is a chat turn, so declining the move beats
            # failing the turn. A person opening a chat in the folder (no
            # internal secret: the browser) claims it, so an agent's
            # chat_folder_delete refuses it from then on.
            if not await _unhide_folder(
                state,
                folder_id,
                claim_for_person=(
                    folder_id != previous_folder
                    and request.headers.get("X-Internal-Secret") is None
                ),
            ):
                slot.folder_id = previous_folder
                slot._folder_changed = previous_changed
            else:
                folder_applied = True
                if is_new_slot:
                    # Folder-tag inheritance, creation-only. A brand-new
                    # chat filed into a folder copies that folder's tags by value onto
                    # its own tag list — the folder's tags are an organizational
                    # default for chats started inside it. Follows chat_fork.py's
                    # copy-by-value style.
                    #
                    # Gated on is_new_slot so re-opening an existing session inside
                    # the folder never re-stamps tags, and confirmed only after
                    # _unhide_folder reported the folder EXISTS (its read is under the
                    # store lock, the only race-free place to look it up). Direct
                    # folder only: no ancestor/subfolder transitivity. Ids are
                    # re-validated against the live vocabulary and appended only when
                    # not already present, so a stale id on the folder is dropped
                    # rather than written onto the slot.
                    def _read_folder_tags(folders: list[dict[str, Any]]) -> list[str]:
                        f = next((x for x in folders if x["id"] == folder_id), None)
                        tags = f.get("tags") if f else None
                        return list(tags) if isinstance(tags, list) else []

                    # One shared definition of "an inheritable folder tag id"
                    # (string, in the live vocabulary) — see validate_folder_tag_ids
                    # for why each guard exists. The READ, the intersection AND the
                    # apply all sit under tags_write_lock (the invariant every
                    # consumer follows, matching the channel-filing path): a folder
                    # PATCH or tag deletion committing after an earlier read would
                    # otherwise stamp a stale tag set or resurrect a deleted id onto
                    # the new slot. Lock ordering (tags_write_lock → folder-store
                    # lock) matches the folder create/PATCH paths.
                    async with tags_write_lock(state):
                        inherited = await state.read_folders(_read_folder_tags)
                        appended = False
                        for tid in validate_folder_tag_ids(inherited, state):
                            if tid not in slot.tags:
                                slot.tags.append(tid)
                                appended = True
                        # "tags changed => revision changed": the awaited folder
                        # read above is a window in which a concurrent slots GET
                        # can snapshot the empty newborn under its birth revision;
                        # the inherited list must not ship under that same one.
                        if appended:
                            _bump_slot_tags_revision(slot)
        # A slot with no project filed into a project-linked folder inherits
        # from the nearest configured ancestor before its first broadcast. The
        # server owns this fallback because the client folder cache can be
        # temporarily stale; existing named slots with an explicit project keep
        # it and continue to use the project endpoint for scope changes.
        if folder_project and folder_applied and not slot.project:
            slot.project = folder_project
        # Default project to workspace directory so file search works out of the box
        if not slot.project:
            cfg_proj = cfg.dashboard.default_project if cfg else ""
            if isinstance(cfg_proj, str) and cfg_proj:
                resolved = os.path.realpath(os.path.expanduser(cfg_proj))
                eligible = os.path.isdir(resolved) and not is_sensitive_path(resolved)
                if eligible:
                    # A configured default that overlaps the data home
                    # would be refused at spawn anyway — skip it here like
                    # a sensitive path, falling back to the workspace
                    # default instead of wedging every new slot. Off the
                    # loop, because the shared scan primes runtime paths
                    # (realpath/mkdir) on first use.
                    eligible = (
                        await asyncio.to_thread(voice_runtime_workspace_conflict, resolved)
                    ) is None
                cfg_proj = resolved if eligible else ""
            else:
                cfg_proj = ""
            slot.project = cfg_proj or default_project_dir(workspace)
        if is_new_slot and cfg is not None:
            if is_owner_dashboard_request(request):
                assignment_key = effective_session_key(slot)
                assignment_agent = slot.agent
                assignment_project = slot.project
                await creation_stack.enter_async_context(_slot_switch_session_lock(assignment_key))
                selection_change = None
                try:
                    # An explicit template choice is the shared template even when
                    # a member carries the same name: it never pins member memory.
                    assigned_store = (
                        ""
                        if agent_kind == "template"
                        else await pin_private_agent_store(
                            state, assignment_key, agent, cfg, memory_mode=slot.memory_mode
                        )
                    )
                    chosen = await asyncio.to_thread(
                        resolve_agent_bindings,
                        cfg,
                        assignment_agent,
                        assignment_project or None,
                        validate_memory_files=False,
                        selection_kind=agent_kind,
                    )
                    # Availability was settled before the mint; this records
                    # the namespace the pick was committed in.
                    slot.agent_kind = chosen.selection_kind
                    selection_change = await _record_explicit_agent_selection(
                        assignment_key,
                        assignment_agent,
                        chosen,
                        config=cfg,
                        memory_mode=slot.memory_mode,
                        app=slot._app or "",
                    )
                except Exception as exc:
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    return _store_unavailable_response(slot.memory_store, exc)
                if (
                    state._slots.get(slot.key) is not slot
                    or effective_session_key(slot) != assignment_key
                    or slot.agent != assignment_agent
                    or slot.project != assignment_project
                ):
                    await drained_to_thread(
                        restore_agent_selection, assignment_key, selection_change
                    )
                    return web.json_response(
                        {
                            "error": "Could not save the member assignment. Try again.",
                            "code": "session_rebound",
                        },
                        status=409,
                    )
                if assigned_store:
                    slot.memory_store = assigned_store
        _sync_dashboard_slots(state)
        # Persist INSIDE the suspension, ahead of the coalesced broadcast, the
        # same ordering `session_control.py`'s create span uses ("the whole
        # allocation-to-persist span runs under `suspend_slots_push`", so "a
        # slot whose birth write fails is never broadcast at all"). Two paths
        # mint a slot through this context manager; leaving the durable write
        # outside it is what makes the failure below reachable:
        #
        # `suspend_slots_push`'s `__exit__` flushes the owed push, and on the
        # coalescing window's LEADING edge that flush broadcasts synchronously
        # (`state.push_slots_update`). An exception there — a non-serializable
        # value reaching `json.dumps` is the evidenced shape —
        # escapes `__exit__`, so with the write out here it skipped a metadata
        # mutation the request had already acknowledged: this `force=True` save
        # is the ONLY durable record of a recreate's folder filing or pinned
        # title (see `_save_slot_to_history`'s message-less merge), and the slot
        # itself survives in memory, so nothing later reconciles the two. Every
        # client repairs a dropped FRAME on its next read; none of them repairs
        # a write that never happened.
        #
        # The cost this ordering accepts, named by the comment it replaces: the
        # suspension is process-wide, so other clients' slot updates coalesce
        # (they defer — no caller blocks) until this off-loop write completes,
        # and a contended history lock takes the patient acquire. Accepted for
        # the same reason the twin accepts it, which awaits a cross-process
        # metadata write inside its own suspension; this span already suspends
        # on the workspace-conflict probe above, so it was never await-free.
        #
        # A pinned title must persist too (not just a folder move): without the
        # write, a restart rehydrates the previous title with a refreshable
        # "auto" origin and the background refresh may rewrite the pin.
        if folder_id or title:
            # The create/recreate request has been authorized against this
            # transcript.  Do not let a rebind while the off-loop write waits on
            # the history lock redirect its newly supplied metadata to another
            # session.
            #
            # No slot retraction on failure, unlike the twin: there the write is
            # the newborn's only record, so an unpersisted slot would vanish on
            # restart and retracting is the lesser evil. Here the slot already
            # has a metadata line and `best_effort` (default) logs the failure
            # and marks the slot dirty so the periodic flush retries it, which
            # is the retry the metadata mutation routes rely on.
            await save_slot_off_loop(
                state,
                slot,
                force=True,
                expected_history_key=slot_history_key(slot),
            )
        # Guarantee a frame. get_or_create_slot pushes for a NEW slot, but
        # returns an existing named slot without pushing — and this handler is
        # now the only thing that files a slot (the client sends no follow-up
        # PATCH to supply that push). Without this, re-creating an
        # existing slot name with a different folder_id would move it for the
        # requester while every other connected client kept the stale
        # placement. Inside the suspension this only marks a push owed, so the
        # new-slot path still emits exactly ONE coalesced frame.
        state.push_slots_update()
        if defer_plain_create_broadcast:
            request_deferred_flush(f"create slot {slot.key!r}")
    # Speculative session creation: overlap the ACP handshake with the user's
    # think-time before their first message. No-op unless session.eager_spawn.
    #
    # Skipped for a relay archive: it never runs, so a spawned kiro-cli would
    # idle until it timed out.
    if slot.executor != "remote":
        schedule_eager_spawn(state, slot, start_priority=owner_start_priority(request))
    return web.json_response(state.serialize_slot(slot))


def _reject_pending_approvals(slot: _ChatSlot) -> None:
    """Reject all pending approval futures so the chat runner unblocks.

    When a stop/interrupt is triggered while the agent is waiting for tool
    approval, the chat runner is suspended on the approval future. Without
    resolving it, the stream generator stays paused, _turn_done never fires,
    and the cooperative cancel times out — forcing a hard kill.

    Resolving the future is not enough on its own: the ``permission`` message
    the UI renders the approval bar from must ALSO be marked resolved.
    Otherwise the future is gone while the message still reads pending, so the
    bar survives a history reload and every button on it answers
    ``404 no pending approval`` — an approval card the user cannot action.
    """
    for aid, fut in list(slot._approval_futures.items()):
        if not fut.done():
            # Mark BEFORE resolving. Resolving can wake the runner immediately,
            # and the runner reads this set where it records who decided; marking
            # after would race its own reader. The provenance is already known
            # here -- the SEL line below calls it ``rejected_on_stop`` -- and the
            # resolved value stays a plain "rejected" so no caller of this future
            # has to learn a new one.
            slot._approval_stopped.add(aid)
            fut.set_result("rejected")
            if _mark_permission_resolved(slot.messages, aid, "rejected"):
                slot._dirty = True
            sel().log_tool_invocation(
                session_key=effective_session_key(slot),
                agent=getattr(slot, "agent", "") or "kirocrew",
                source="dashboard",
                tool_name=f"approval_reject:{aid}",
                tool_kind="permission",
                outcome="rejected_on_stop",
            )


class _HandoverDrainResult(NamedTuple):
    """What a hand-over drain did with the state it is the last reader of.

    ``rows_committed`` answers for the transcript rows: True when nothing was
    owed or the write committed, False when rows were owed and did not reach
    disk. ``prompts_lost`` counts the durable-eligible queued prompts whose only
    copy dies with the popped slot — zero when none were owed or when the
    durable line holds them, whichever writer put them there.

    A NamedTuple is always truthy, so a bare ``if not result:`` silently passes
    over a failed drain. Read ``rows_committed``; never test the result itself.
    """

    rows_committed: bool
    prompts_lost: int


async def _owed_prompts_lost_on_line(
    state: DashboardState, name: str, slot: _ChatSlot, history_key: str
) -> int:
    """Count owed queued prompts with no durable future on the shared line.

    The per-slot persistence signature answers "did THIS slot's last commit
    carry the queue"; it cannot see a replacement's own full save rebuilding
    the shared line and clearing ``queued_prompts`` (an owned field, so an
    emptied queue is cleared by absence — and ``POST /api/chat/slots`` persists
    at birth, inside the very window a hand-over spans). Nor is a point-in-time
    read of the line enough on its own: a line still showing the ORIGINAL's
    entries is one full save away from losing them whenever a live
    transcript-sharing holder exists, because that holder's save rebuilds the
    whole ``queued_prompts`` value from its own queue. So survival demands a
    stable owner, not a lucky read:

    * a live holder shares this transcript — an owed entry survives only when
      that holder's own durable queue carries it (a rehydrated holder restores
      the entries as queue cards and re-persists them; a fresh recreate does
      not), because the holder's next save decides the line;
    * no live sharing holder — the line is at rest, so an entry it holds stays
      until an ordinary restore hands it back.

    A line that cannot be read cannot prove survival, so every owed entry
    counts as lost: over-reporting is recoverable by the reader, while silence
    over a real loss is the failure this count exists to end.

    The store read is synchronous file I/O, so it goes through
    ``drained_to_thread`` rather than running on the gateway event loop —
    the same seam every other blocking read this module performs takes.
    """
    owed = slot.durable_queue_entries()
    if not owed:
        return 0
    holder = state._slots.get(name)
    if holder is not None and _replacement_shares_transcript(state, name, slot):
        surviving = {entry.get("id") for entry in holder.durable_queue_entries()}
        return sum(1 for entry in owed if entry.get("id") not in surviving)
    log = state.conversation_log
    if log is None:
        return len(owed)
    persisted, readable = await drained_to_thread(log.get_metadata_status, history_key)
    if not readable:
        return len(owed)
    on_line = persisted.get("queued_prompts")
    if not isinstance(on_line, list):
        return len(owed)
    line_ids = {entry.get("id") for entry in on_line if isinstance(entry, dict)}
    return sum(1 for entry in owed if entry.get("id") not in line_ids)


def _report_lost_queued_prompts(
    state: DashboardState, name: str, count: int, history_key: str
) -> None:
    """Log and post the user-visible notice that a hand-over lost queued prompts.

    The gateway log alone is not reachable by the person whose words were
    dropped; the notification feed is, so the loss is told in both. The body
    carries the COUNT and the slot, never the prompt text: the entries may
    belong to a restricted session, and a notice about losing words must not
    be the thing that leaks them.

    Failure to deliver must not fail the drain — the hand-over has to complete
    for the replacement holding the key either way — so this swallows and logs,
    the same posture every other lifecycle notice takes.
    """
    logger.warning(
        "Slot %s: %d queued prompt(s) were not carried by the hand-over write to "
        "%s; they are lost with the original slot",
        name,
        count,
        history_key,
    )
    try:
        state.notify(
            "agent",
            "Queued prompts lost in a tab hand-over",
            (
                f"{count} queued prompt(s) on tab {name!r} could not be carried "
                f"to {history_key} when the tab was replaced mid-close; they are "
                "not recoverable."
            ),
            meta={"slot": name, "count": count, "history_key": history_key},
        )
    except Exception:
        logger.error(
            "Slot %s: the lost-queued-prompts notification failed to deliver; "
            "the gateway log is the only remaining report of the loss",
            name,
            exc_info=True,
        )


async def _persist_handover_tail(
    state: DashboardState, name: str, slot: _ChatSlot
) -> _HandoverDrainResult:
    """Write a handed-over original's still-unsaved rows before its object is dropped.

    A teardown that yields ``name`` to a concurrent same-key recreate stops
    referencing the original slot: it is out of ``state._slots``, and the periodic
    flush iterates exactly that map, so nothing retries the write for it. Anything
    the original held past its last commit — ``messages[_disk_window_len:]``, plus
    any note the cleanup path is still carrying in ``_deferred_notes`` — would be
    unreachable and never persist. Those rows belong to the ORIGINAL's own
    transcript, whether or not the replacement happens to share it, and this frame
    is the last moment anything can put them there.

    The target is ``slot_history_key(slot)``, never ``_history_key_for(name)``. A
    slot carrying a ``linked_session_key`` — cron-, channel- or workflow-injected —
    stores its conversation under that key, and the forced save resolves its own
    write target the same way and REFUSES the whole write when the caller's
    ``expected_history_key`` names a different transcript. Authorizing
    ``dashboard:{name}`` there would make this drain a silent no-op for exactly the
    slots whose transcript is shared with something outside the dashboard, and would
    name a row-less file in the report.

    Deliberately NOT ``closed=True``. What this frame is finishing is the close of
    the ORIGINAL; the KEY is open, because a live replacement holds it, so the
    durable line has to say so. Stamping ``closed`` on a key someone is still using
    is the harm the surrounding guard exists to prevent. Open-shaped is not the same
    as un-closing, though: on a line the replacement published, ``closed`` is that
    holder's own dismissal, so the save defers it rather than erasing it (see
    ``ROWS_ONLY_OWNED_META_KEYS``). It is only on a line THIS slot published — where
    there is no other holder's flag to lose — that the write clears a stale
    ``closed`` an earlier close of the reused key left behind.

    Non-destructive against the replacement's own rows in both directions. The save
    re-serializes the ORIGINAL's window over the on-disk window region, and the
    save's foreign-append scan classifies every on-disk line that window does not
    represent as another writer's append and carries it through verbatim, so rows a
    replacement already committed survive.

    ``rows_only``, and that is the whole of what this frame claims. The rows are
    owed to the transcript; the METADATA line may not be this slot's to move.
    ``_save_slot_to_history`` is otherwise authoritative for every
    ``SLOT_OWNED_META_KEYS`` field and REBUILDS the line from whichever slot it is
    handed, so a default save here would revert a folder, pinned title, tag or pin
    the replacement had already published (``POST /api/chat/slots`` persists both at
    birth) — silently undoing an acknowledged edit, and for a tab nobody types in
    again undoing it for good. ``rows_only`` keeps the on-disk value for each of
    those — the close flags included — and leaves this write owning only the file's
    identity and accounting, which it carries forward from disk anyway.

    It rides with the write rather than being a caller's choice because the save
    scopes the deferral itself, by the line's ``tab_id``: it holds back only fields
    on a line ANOTHER slot published, and rebuilds normally from a line this slot
    published or from no line at all. That distinction is what keeps the flag from
    costing the original its own uncommitted metadata — a rename, re-file, tag or
    pin is acknowledged the moment it lands in memory and persists on a later
    flush, and this frame is past the pop, so no flush will ever visit this slot
    again.

    Returns a :class:`_HandoverDrainResult`. ``rows_committed`` is True when
    nothing was owed or the write committed, False when rows were owed and did
    not reach disk. Callers MUST honour it: nothing in the process can reach
    these rows again, so a caller that discards the answer reports a close that
    succeeded while the rows became unreachable. The log line names the exact
    count for the same reason.

    ``prompts_lost`` is the other half of the answer. A committed write can
    still defer the queue (the metadata line belongs to the replacement holding
    this key), a failed one leaves the line as it was, and a replacement's own
    full save can rebuild the shared line without the original's entries at any
    moment it remains alive — so survival is judged by who writes the line
    next (see :func:`_owed_prompts_lost_on_line`), not by this slot's
    persistence signature, which only answers for this slot's own last commit.
    An owed entry with no durable future dies with the popped object; the count
    comes back where a caller can surface or tally it, and the drain posts a
    dashboard notification alongside the warning log — the log is not reachable
    by the person whose words were dropped. Carrying the prompts instead would
    mean making ``queued_prompts`` a merge field on the rows-only path, which is
    a change to what a durable metadata line MEANS for a key two slots share;
    the write stays as it is, and the loss is reported rather than silent.
    """
    try:
        tightening = _tighten_replacement_to_restricted_original(state, name, slot)
    except UnknownMemoryStore:
        tightening = None
        logger.warning(
            "Slot %s: replacement rebound twice during tightening; writing the tail "
            "under the ratcheted line without tightening the live replacement",
            name,
        )
    try:
        slot.flush_deferred_notes()
    except Exception:
        # The flush puts the unwritten suffix back before raising, so this count is
        # what is still held. The hold also has a durable copy in the slot's
        # metadata line, so these notes are re-delivered after the NEXT gateway
        # restart rather than dying with the popped object — but nothing
        # in THIS process will visit this slot again, so for this lifetime they
        # are undeliverable and the log must still say so.
        logger.error(
            "Slot %s: %d held note(s) could not be flushed before the key was handed "
            "to a concurrent recreate; they are undeliverable until the persisted "
            "hold replays on the next restart",
            name,
            len(slot._deferred_notes),
            exc_info=True,
        )
    # ``_disk_window_len`` is how much of the current window the last committed save
    # covered, so the difference is exactly what has never reached disk. ``_dirty``
    # covers the other shape of unsaved state: an in-place edit to a row already
    # persisted leaves the length unchanged.
    unsaved = max(0, len(slot.messages) - slot._disk_window_len)
    # A queued prompt is unsaved state that changes NEITHER of those: its row is
    # written by the drain, so the window length is unchanged, and an enqueue does
    # not dirty the slot. Reporting a clean hand-over over that state would send
    # the prompt's only copy away with the discarded object, unremarked.
    history_key = slot_history_key(slot)
    if not unsaved and not slot._dirty and not slot.queue_persist_pending:
        # No write is needed, but "this slot committed its queue" is not the
        # same fact as "the entries have a durable future": a same-key recreate
        # persists at birth, and its full save rebuilds the shared line and
        # clears ``queued_prompts`` by absence. Survival is decided by whoever
        # writes the line next, so that is what gets checked.
        lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
        if lost:
            _report_lost_queued_prompts(state, name, lost, history_key)
        return _HandoverDrainResult(rows_committed=True, prompts_lost=lost)
    try:
        committed = await save_slot_off_loop(
            state,
            slot,
            closed=False,
            best_effort=False,
            expected_history_key=history_key,
            rows_only=True,
            # This frame IS the retraction's drain, so the close's guarded-write
            # fence does not apply to it: the close raised that fence, sequenced
            # this write itself, and nothing in the process can reach these rows
            # again. Refusing here would drop exactly the rows this function
            # exists to save.
            issued_by_the_retraction=True,
        )
    except Exception:
        await restore_replacement_if_handover_did_not_land(state, name, tightening, history_key)
        logger.error(
            "Slot %s: %d unpersisted row(s) could not be written to %s while handing "
            "the key to a concurrent recreate; they are lost with the original slot",
            name,
            unsaved,
            history_key,
            exc_info=True,
        )
        # A failed write leaves the line as it was; whether an owed entry still
        # has a durable future is decided by who writes that line next, not by
        # this slot's own persistence signature, which cannot see a
        # replacement's rebuild clearing the shared key's queue.
        lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
        if lost:
            _report_lost_queued_prompts(state, name, lost, history_key)
        return _HandoverDrainResult(rows_committed=False, prompts_lost=lost)
    if not committed:
        await restore_replacement_if_handover_did_not_land(state, name, tightening, history_key)
        # The save declined without writing: the session was permanently deleted
        # while this write awaited the lock, or the slot's routing moved off the
        # transcript this frame authorized. Neither leaves anywhere for these rows
        # to go, and the object holding them is about to be dropped.
        logger.warning(
            "Slot %s: %d unpersisted row(s) were not written to %s while handing the "
            "key to a concurrent recreate; the save declined the write",
            name,
            unsaved,
            history_key,
        )
        lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
        if lost:
            _report_lost_queued_prompts(state, name, lost, history_key)
        return _HandoverDrainResult(rows_committed=False, prompts_lost=lost)
    # Only a replacement that WRITES THIS TRANSCRIPT follows the committed rows'
    # mode: a slot at ``name`` whose rows route elsewhere (a task-review or
    # workflow slot with a divergent ``linked_session_key``) shares no file with
    # the original and must not be ratcheted for rows it will never read.
    if _replacement_shares_transcript(state, name, slot):
        tighten_live_slot_memory_mode(state, name, slot.memory_mode)
    # The write committed, and it can still leave owed entries with no durable
    # future: a rows-only save over a line another live slot published defers
    # every slot-owned field, ``queued_prompts`` among them
    # (``queue_line_is_ours`` keeps them owed rather than falsely credited),
    # and a live sharing replacement rebuilds the line on its every full save.
    # Survival is decided by who writes the line next — an entry a rehydrated
    # replacement carries in its own queue lives on as a queue card and is not
    # lost. Nothing in this process will visit this slot again, so a loss is
    # said with the count — the same obligation the held-note arm above
    # carries, and for the same reason: these are the user's own words and this
    # frame is their last reader.
    #
    # Carrying them instead would mean making ``queued_prompts`` a merge
    # field on the rows-only path, which is a change to what a durable
    # metadata line MEANS for a key two slots share, not a loop-side
    # ordering fix. The write stays rows-only; the remedy is the report, in
    # every register that can still carry it — the warning log, the
    # notification, and the count in the returned result.
    lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
    if lost:
        _report_lost_queued_prompts(state, name, lost, history_key)
    return _HandoverDrainResult(rows_committed=True, prompts_lost=lost)


def _unblock_pending_waits(state: DashboardState, slot: _ChatSlot) -> None:
    """Unblock EVERY thing a stop/interrupt could leave the runner waiting on.

    Two independent blocking waits exist per slot and both must be released or
    the cooperative cancel times out into a hard kill:

    * pending tool approvals (:func:`_reject_pending_approvals`)
    * pending agent questions that have a server-side wait
      (:meth:`DashboardState.cancel_questions_for_slot`) — the blocked HTTP
      request holds an MCP worker, so resolving the future is what lets that
      socket close and the call return. Only the ``POST /api/ask-question``
      path creates such a wait; the MCP ``ask_question`` tool posts a stateless
      card and ends the turn, so it leaves nothing to release here.

    They are combined here deliberately: a new blocking wait added later must
    be released from every stop path, and three separate call sites each
    needing their own second line is how one of them gets missed.
    """
    _reject_pending_approvals(slot)
    cancelled = state.cancel_questions_for_slot(slot.key)
    if cancelled:
        logger.info("Stop: cancelled %d pending question(s) on slot %s", cancelled, slot.key)


async def _subagents_attached_response(
    state: DashboardState, slot: _ChatSlot, session_key: str, operation: str
) -> web.Response | None:
    """409 while sub-agent children are attached to *session_key*, else None.

    One guard for every endpoint whose action cannot coexist with children —
    dispatching a new turn (continue) interleaves with their writes, and a
    session teardown (reload) kills the shared runtime they run on.

    The probes themselves live in :func:`chat_utils.subagents_attached_async`
    (a coroutine because the queued probe reads the task store), shared
    with the deferred consume in ``chat_runner`` that applies a queued
    conversation discard. That teardown reaches the same runtime without passing
    through any endpoint, so it must apply the same policy — and two copies of
    the probe block is how the two would diverge. This wrapper only shapes the
    refusal.
    """
    if await subagents_attached_async(state, slot, session_key, operation):
        return web.json_response(
            {"error": "sub-agents are running", "code": "slot_subagents_running"},
            status=409,
        )
    return None


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


async def _reset_slot_session_or_warn(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    *,
    switch_kind: str,
) -> bool | None:
    """:func:`_reset_slot_session` for the commit-before-reset switch handlers.

    Returns the reset verdict, or ``None`` when the teardown RAISED after the
    session pop. The model and workspace handlers commit the new setting
    BEFORE this await, and ``SessionManager.reset`` pops the session before
    its shutdown can fail, so a post-pop raise is a success with a degraded
    teardown — the committed value is what every replacement session runs.
    That premise is VERIFIED, not assumed: a raise with the SAME provider
    instance still registered (a pre-pop failure, e.g. in the pending-wait
    unblock) is re-raised, because the old session then survives on the old
    value and a 200 would be a false success. Identity, not presence: a
    successor session registered by a concurrent send after the pop must not
    be misclassified as the unpopped old one. Propagating a post-pop raise
    instead would answer 500 without ever reaching
    ``state.push_slots_update()``, leaving every
    connected client rendering the OLD value over a switch that actually
    happened (the acting tab's ``performSlotSwitch`` also keeps its old store
    value on a non-2xx). ``None`` tells the caller to record the degraded
    teardown and answer 200 with :data:`_TEARDOWN_INCOMPLETE_WARNING` after
    the usual slots push; a caller with a rebind guard
    (``effective_session_key`` — the model and workspace handlers) must still
    FALL THROUGH and run it, so a slot rebound during the raising await keeps
    answering rollback + 409. Shared
    by all four commit-before-reset switch handlers (agent, reasoning effort,
    model, workspace) so none of them repeats the try block: each calls twice
    (first attempt + idle-decline retry). Only the raise path is
    handled here: a normal ``bool`` verdict passes through untouched, so the
    decline (``False``) ladders keep their semantics.

    ``skip_if_busy`` is fixed at True: every caller is a switch handler with
    a decline ladder (agent, reasoning effort, model, workspace), and
    :func:`_reset_slot_session` must decline a busy session atomically with
    the pop rather than tear it down mid-turn — the ladder then disambiguates
    the decline. A caller that wants every raise to propagate keeps using
    :func:`_reset_slot_session` directly.
    """
    # Captured BEFORE the await: the post-raise probe must compare INSTANCE
    # IDENTITY, not mere presence. A concurrent send can register a SUCCESSOR
    # session for the same key after the pop and before the old session's
    # shutdown raises — a bare "is a provider registered?" probe would
    # misclassify that successor as the unpopped old session and answer 500
    # for a committed switch. The successor
    # cold-started from the slot's CURRENT (committed) bindings, so the
    # committed-success answer is truthful for it.
    prior_provider = state.sessions.get_provider(session_key)
    if _test_interleave is not None:
        # Inside the caller's locks, after it committed its new setting, before
        # the old session goes -- the span a concurrent teardown must be able to
        # land in for the ordering to be observable at all. Placed on this shared
        # helper rather than in each handler because all four commit-before-reset
        # switches reach the teardown through here, and a point per handler is how
        # one of them ends up without one.
        await _test_interleave("switch:post_commit")
    try:
        return await _reset_slot_session(state, slot, session_key, skip_if_busy=True)
    except Exception:
        if (
            prior_provider is not None
            and state.sessions.get_provider(session_key) is prior_provider
        ):
            # The SAME instance is still registered: the raise came BEFORE the
            # session pop (e.g. the pending-wait unblock that
            # _reset_slot_session runs first), so the old session is still
            # alive on the old value and a 200 here would be exactly the false
            # success the switch handlers' decline ladders treat as worse than
            # any retryable error. Propagate: the committed-switch answer is
            # only truthful once the pop has happened.
            raise
        logger.exception(
            "Slot %s %s switch: old session teardown incomplete", slot.key, switch_kind
        )
        return None


def _resolve_stop_event(slot: _ChatSlot, outcome: str) -> None:
    """Update the in-flight stop_event message in place with final state."""
    stop_id = slot._stop_event_id
    logger.debug("_resolve_stop_event: outcome=%s stop_id=%r", outcome, stop_id)
    if not stop_id:
        return
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    if outcome == "soft":
        final_state = "stopped"
    elif outcome == "compacting":
        # Nothing was stopped: the session's own /compact turn held it and a
        # cooperative Stop was declined. The card becomes the notice,
        # so the row the press opened tells the user what happened to it.
        final_state = "stop_declined_compacting"
    else:
        final_state = "stop_failed_reset"
    found = False
    for msg in reversed(slot.messages):
        cls_val = msg.get("cls", "")
        if not cls_val:
            continue
        try:
            cls_data = json.loads(cls_val) if isinstance(cls_val, str) else None
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls_data, dict) or cls_data.get("kind") != "stop_event":
            continue
        if cls_data.get("id") != stop_id:
            continue
        cls_data["state"] = final_state
        cls_data["outcome"] = outcome
        cls_data["ts_end"] = now_ts
        serialized = json.dumps(cls_data)
        msg["cls"] = serialized
        msg["content"] = serialized
        slot.invalidate_source_links()
        slot._dirty = True
        found = True
        # Re-broadcast updated stop_event so frontend StopEventCard
        # transitions from "stopping" → "stopped"/"stop_failed_reset".
        on_msg = getattr(slot, "_on_message", None)
        if on_msg:
            try:
                on_msg(slot.key, msg)
            except Exception:
                logger.debug("stop_event re-broadcast failed", exc_info=True)
        break
    if not found:
        logger.debug("_resolve_stop_event: no matching message for stop_id=%s", stop_id)
    slot._stop_event_id = None


def _rearm_stop_event(slot: _ChatSlot, stop_data: dict[str, Any]) -> bool:
    """Reset an orphaned stop card back to "stopping" in place, same id.

    A new press that finds an orphan must not sweep it and append a fresh row:
    the pane upserts stop cards by ``meta.id``, so the settled old row plus the
    new row render as TWO "[Stopped]" chips for one press. Re-arming
    the existing row keeps the id — and therefore the chip — stable, the same
    reuse the escalation path performs via ``slot._stop_escalated_card_id``.

    Returns False when no row carries the id (e.g. the window was trimmed), in
    which case the caller appends the press's one card instead.
    """
    stop_id = stop_data["id"]
    serialized = json.dumps(stop_data)
    for msg in reversed(slot.messages):
        cls_val = msg.get("cls", "")
        if not cls_val:
            continue
        try:
            cls_data = json.loads(cls_val) if isinstance(cls_val, str) else None
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls_data, dict) or cls_data.get("kind") != "stop_event":
            continue
        if cls_data.get("id") != stop_id:
            continue
        msg["cls"] = serialized
        msg["content"] = serialized
        slot.invalidate_source_links()
        slot._dirty = True
        # Re-broadcast so connected panes transition the existing chip back to
        # "stopping" — the same channel _resolve_stop_event settles it on.
        on_msg = getattr(slot, "_on_message", None)
        if on_msg:
            try:
                on_msg(slot.key, msg)
            except Exception:
                logger.debug("stop_event re-arm re-broadcast failed", exc_info=True)
        return True
    logger.debug("_rearm_stop_event: no matching message for stop_id=%s", stop_id)
    return False


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


def _open_stop_event_card(slot: _ChatSlot, state_label: str) -> str:
    """Open this press's ONE stop card and return its id.

    An orphaned card from a prior attempt is REUSED, not swept. The old
    "defensive stale-card sweep" resolved the orphan AND appended a fresh row,
    so one press put two ``stop_event`` rows on the wire and the pane — which
    upserts by ``meta.id`` — renders two "[Stopped]" chips. Re-arming
    the existing row in place keeps the single-card-per-press invariant the
    peer-bound branch of ``stop_slot_turn`` documents, mirroring the
    escalation path's reuse of the open card rather than minting a second one.

    One helper for both cancel routes (``stop_slot_turn`` and
    ``api_chat_slot_interrupt``) so the reuse rule cannot drift between them.

    Reuse is scoped to a SAME-TURN orphan: when a ``user`` row follows the
    orphan, the next turn has begun and re-arming would mutate a row sitting
    in the previous turn's block — the press's chip would then appear (and
    transition) in earlier scrollback, attributing the stop to the wrong turn.
    A cross-turn orphan is settled where it lies and this press's card is
    appended fresh, which never renders two
    ADJACENT chips.
    """
    stale_id = slot._stop_event_id
    if stale_id and not _orphan_in_current_turn(slot, stale_id):
        _resolve_stop_event(slot, "soft")  # settle it where it lies
        stale_id = None
    stop_id = stale_id or f"stop-{uuid.uuid4().hex}"
    slot._stop_event_id = stop_id
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    stop_data = {
        "kind": "stop_event",
        "id": stop_id,
        "state": state_label,
        "outcome": None,
        "ts_start": now_ts,
    }
    # cls must be JSON-encoded so parse_cls_meta() populates meta on the wire.
    # content mirrors the data for backward-compat with any consumer that only
    # reads content.
    stop_msg = json.dumps(stop_data)
    if not (stale_id and _rearm_stop_event(slot, stop_data)):
        if stale_id:
            # The re-arm found no row (lost a race with window trimming):
            # appending under the REUSED id would upsert into a client still
            # holding the old row and land the chip in old scrollback — the
            # failure mode reuse exists to avoid. Mint fresh for the append.
            stop_id = f"stop-{uuid.uuid4().hex}"
            slot._stop_event_id = stop_id
            stop_data["id"] = stop_id
            stop_msg = json.dumps(stop_data)
        # No same-turn orphan to re-arm: this press's one card is a fresh
        # append (a cross-turn or vanished orphan was settled above).
        slot.append("system", stop_msg, stop_msg)
    if stale_id and slot._stop_escalated_card_id == stale_id:
        # A stale escalation marker scoped to the REUSED id would make this
        # press's cooperative ack defer to a hard callback that already fired
        # (or never will), stranding the re-armed card at "stopping" — the
        # exact failure the id-scoped marker exists to remove. A swept card
        # never hit this because the fresh id could not match; reuse must
        # clear it explicitly.
        slot._stop_escalated_card_id = None
    return stop_id


def _make_stop_resolver(
    state: DashboardState, slot: _ChatSlot, outcome: str, card_id: str | None
) -> Callable[[], Awaitable[None]]:
    """Build the stop_turn on_soft/on_hard callback that settles the stop card.

    Key the guard on `_stop_event_id`, not on `_stop_state`. The card id is
    already the idempotency token: `_resolve_stop_event` no-ops when it is None
    and clears it once it has settled the card, so a state gate buys nothing
    there. What the state gate did buy was a bug. A turn tearing down
    concurrently drives `_stop_state` back to "idle" (`_finish_queue_cycle` in
    chat_runner.py, through the `_stopping` setter in state.py), and that
    teardown races the escalation. When teardown won, the hard callback bailed,
    `_resolve_stop_event` never ran, and the card pulsed at "stopping" for the
    rest of the session instead of settling to "stop_failed_reset".

    Precedence needs its own non-racy marker. A cooperative ack that arrives
    after the user escalated must not relabel a hard kill as a clean stop, and
    `_stop_state` cannot carry that fact because the same teardown resets it to
    "idle" from `killing` just as readily as from `soft_pending`. Reading it
    here would reproduce the bug one dimension over: teardown erases the
    escalation, the late soft callback sees a neutral state, and the card
    settles as "stopped" for a session that was killed. So the escalation path
    sets `slot._stop_escalated_card_id`, which teardown never touches, and only
    the soft callback defers on it. `hard` is terminal and nothing outranks it.
    The marker holds an id rather than a flag so it cannot leak onto a later
    card: a bare boolean left set would make the NEXT card's cooperative ack
    defer to a hard callback that never fires, stranding that card at
    "stopping", which is the failure this marker exists to remove.

    Bind to `card_id`, the specific card this callback was created for, and not
    to whatever card happens to be in flight when it fires. `stop_turn` awaits
    these callbacks, so one can still be pending when teardown resets the stop
    posture, a new turn starts, and a second stop opens a card of its own —
    usually a NEW id, but a same-turn orphan is RE-ARMED under this very id
    (`_open_stop_event_card`), which is why the id comparison alone is
    not per-attempt identity; see the generation paragraph below. Reading
    `slot._stop_event_id` at call time would settle the newer stop's card with
    this older outcome and clear its posture, so the newer stop's own callback
    would find nothing left to settle. Callers pass the id they just assigned.

    `card_id` may be None, for a stop that escalated before any card existed.
    Such a callback still releases the stop posture; it simply has no card to
    label. Only a mismatching non-None current id means "someone else owns
    this", so only that case returns without touching the slot.

    Also bind `slot._stop_generation`, captured at creation. Card REUSE
    (`_open_stop_event_card`) makes the id comparison insufficient by
    construction: a press that re-arms an orphaned card carries the SAME id the
    prior press's still-pending callback was bound to, so matching ids no
    longer prove matching stops — the old callback would settle the re-armed
    card with the old outcome and release the new stop's posture. The
    generation counts stop INITIATIONS (the `_stop_state` setter bumps it on
    every idle → active edge and teardown never rewinds it), so "a newer stop
    has initiated since this callback was created" is exactly `generation !=
    slot._stop_generation` — and that newer stop's own callbacks own both the
    card and the posture, including the cardless-posture-release duty above
    (an initiation that rolls back before binding callbacks, like /interrupt's
    refused-body branch, resets the posture itself — its CARD, if a prior
    press's orphan was in flight, can stay at "stopping" until the next press
    sweeps or re-arms it: the generation is monotonic and never rewound, so
    the prior resolver bails. That residual is accepted deliberately — the
    posture is safe, the strand self-corrects on the next press, and settling
    the card from the rollback would label it with an outcome the still-
    pending cancel has not produced).
    """
    generation = slot._stop_generation

    async def _resolve() -> None:
        logger.debug(
            "stop resolver (%s): card_id=%r current=%r stop_state=%r escalated=%r gen=%d/%d",
            outcome,
            card_id,
            slot._stop_event_id,
            slot._stop_state,
            slot._stop_escalated_card_id,
            generation,
            slot._stop_generation,
        )
        # A newer stop initiated after this callback was created: everything —
        # the (possibly re-armed, same-id) card AND the posture — belongs to
        # that stop's own callbacks now. See the generation paragraph above.
        if generation != slot._stop_generation:
            return
        # Bail only when a DIFFERENT card is genuinely in flight, because that
        # card belongs to a later stop that owns the posture. Do not bail merely
        # because this attempt has no card: settling a card and releasing the
        # stop posture are separate jobs, and the posture must be released even
        # when there was never a card to settle. A stop can reach a callback
        # with `card_id` None: `api_chat_slot_interrupt` claims
        # `_stop_state = "soft_pending"` before it awaits the request body and
        # only then opens its card, so a concurrent `/stop` escalates against a
        # slot that has none yet. Skipping the reset there strands `_stop_state`
        # at "killing", which permanently suppresses re-queue
        # (`_should_suppress_requeue`) and rejects every later interrupt. That
        # wedges the slot, which is worse than the mislabel this guard prevents.
        if slot._stop_event_id is not None and slot._stop_event_id != card_id:
            return
        # `card_id is None` cannot mean "escalated": the marker holds a real
        # card id, so comparing None to None would defer a callback that no
        # hard kill will ever follow, and the posture would never be released.
        if outcome == "soft" and card_id is not None and slot._stop_escalated_card_id == card_id:
            logger.debug("stop resolver (soft): escalated to hard kill, deferring to hard")
            return
        # No-ops when there is no card, which is exactly the case above.
        _resolve_stop_event(slot, outcome)
        slot._stop_state = "idle"
        if card_id is not None and slot._stop_escalated_card_id == card_id:
            slot._stop_escalated_card_id = None
        state.push_slots_update()

    return _resolve


def _slot_not_found() -> web.Response:
    """The uniform per-slot 404, :func:`slot_ownership.slot_not_found`."""
    return slot_not_found()


async def _app_may_send_to_slot(request_app: str, slot: Any) -> bool:
    """Whether *request_app* may control *slot* through the session APIs.

    The session-control rule (:func:`app_may_control_session`) on a live read of
    the ``sessionApproval`` grant, judged after that read completes.
    """
    if not request_app:
        return True
    granted = await read_session_grant(request_app)
    return app_may_control_session(request_app, slot, granted)


async def _app_claim_refused(
    state: DashboardState, request_app: str, operation: str, keys: Iterable[str]
) -> bool:
    """True, audited, when an app's NEW slot would bind to a transcript it does not own.

    Any of *keys* (the transcripts the new slot would read or save) that exists
    and does not record *request_app* refuses it; see
    ``handlers.sessions._app_may_claim_transcript``. The caller answers with the
    uniform ``slot_not_found`` 404. A dashboard caller (no app) is never refused.
    """
    log = state.conversation_log
    if not request_app or log is None:
        return False
    # Imported here: this module imports the handlers package at module scope.
    from kiro_crew.dashboard.handlers.sessions import (
        _NOT_TRANSCRIPT_OWNER,
        _app_may_claim_transcript,
        _audit_app_allow,
        _audit_app_denial,
    )

    claimed = list(dict.fromkeys(keys))
    for key in claimed:
        if not await asyncio.to_thread(_app_may_claim_transcript, log, request_app, key):
            _audit_app_denial(request_app, operation, f"session={key}", _NOT_TRANSCRIPT_OWNER)
            return True
    for key in claimed:
        _audit_app_allow(request_app, operation, f"session={key}")
    return False


def _cancel_target(slot: _ChatSlot) -> str:
    """The session a cancel on *slot* must address.

    Never ``_history_key_for(name)``: every slot carrying a
    ``linked_session_key`` — a cron-born tab (``cron:<job_id>``), a channel-born
    tab (``slack:<ts>``), a workflow-born tab — runs its turns under that key,
    while the dashboard-prefixed spelling names a session that never existed.
    ``SessionManager.stop_turn`` then finds nothing and returns ``"idle"``, the
    handler settles the card as "stopped", and the turn keeps streaming, so Stop
    is a silent no-op that reports success once per press.

    Routing alone is not enough either. A running turn owns a stable identity:
    ``_run_chat`` captures the key it acquires and keeps using that one for the
    whole turn, while
    ``linked_session_key`` remains mutable underneath it — a cron injection
    binds an already-live slot with no ``running`` gate. Re-deriving the key at
    cancel time therefore names wherever the slot routes the NEXT turn, which
    after a mid-turn rebind is not the turn the operator is trying to stop.

    Falls back to the routing when no turn is in flight (nothing to have
    captured an identity), which is also what a slot restored from disk answers
    — the field is runtime-only and empty after a restart.
    """
    return getattr(slot, "_active_turn_session_key", "") or effective_session_key(slot)


def _app_cancel_denied(
    request: web.Request | Mapping[str, Any], slot: _ChatSlot, operation: str, target_key: str
) -> web.Response | None:
    """Whether *request* may cancel *target_key*, as an indistinguishable 404.

    Two conditions for an app token, because slot ownership does NOT imply
    ownership of the session the cancel would land on:

    1. the app owns the slot (App Kit §5.2, deny-by-default), and
    2. the session about to be cancelled is still the slot's own session
       (``slot_ownership.own_session_key``), not one the app has no claim on.

    Condition 2 is load-bearing. ``get_or_create_slot`` takes ``app`` and, for a
    name shaped like a channel session stem, resolves ``linked_session_key``
    from the session map in the same call — so an app that names a live channel
    thread ends up owning a slot bound to a conversation it has no claim on.
    Ownership alone would then authorize cancelling that channel's turn, turning
    a slot binding into capability escalation.

    It tests *target_key* — the key the caller will actually cancel — rather
    than re-reading the slot, so authorization and action cannot disagree. That
    is not only a TOCTOU guard: for a turn that started on the app's own session
    and was rebound mid-flight, re-reading would DENY the app its own running
    turn, because the routing now points somewhere it does not own.

    A dashboard caller has no app scope and may cancel either kind.

    Shared by the cancel routes so /stop and /interrupt cannot drift onto two
    policies. Condition 1 is :func:`deny_app_slot_access`, the decision the
    per-slot checkpoint already made; condition 2 is layered on top of it.
    """
    request_app = request.get("app", "")
    denied = deny_app_slot_access(request_app, slot, slot.key, operation)
    if denied is not None or not request_app:
        return denied
    if target_key == own_session_key(slot):
        return None
    audit_app_slot_denial(
        request_app, operation, slot.key, "app does not own the session this slot is linked to"
    )
    return slot_not_found()


async def _settle_discarded_stage_deliveries(
    state: "DashboardState",
    slot: "_ChatSlot",
    contents: list[str],
) -> None:
    """Settle the queued completion debt a discarded queue owed."""
    manager = getattr(state, "subagents", None)
    if manager is None:
        return
    owed = slot.take_pending_subagent_deliveries(contents)
    if owed:
        try:
            settlement = manager.settle_queued_delivery(owed)
            if asyncio.iscoroutine(settlement):
                await settlement
        except Exception:
            logger.warning(
                "Could not settle discarded stage deliveries for slot %s",
                slot.key,
                exc_info=True,
            )


async def stop_slot_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    *,
    force: bool = False,
    source: str = "dashboard",
    cancel_key: str = "",
    escalate: bool = True,
) -> dict[str, Any]:
    """Stop the slot's turn: cooperative cancel, hard kill on a second call.

    First call: soft cancel. A second call while the first is still pending
    escalates to a hard kill, regardless of *force* — the caller's view of the
    stop state can lag the backend's, so the backend's own ``_stop_state`` is
    what decides.

    *escalate* is how a caller says its second call may not be a second
    DECISION. It defaults to True because that is true of the Stop button this
    function was written for: a person pressing again has watched the cooperative
    stop fail to take. It is not true of an RPC, where a client that timed out
    re-sends the same request — so ``session_control.stop_target`` passes False
    for a call it cannot distinguish from a retry, and the repeat falls through to
    the no-op below instead of discarding the target's queue. It
    withholds only the ESCALATION: a stop that finds the slot running still stops
    it either way.

    Inserts a ``stop_event`` card into the slot transcript so whoever is
    watching the session sees the stop, and returns the JSON body the route
    would have sent. *source* labels the SEL audit line with who asked.

    *cancel_key* is the session the stop must land on, resolved ONCE by the
    caller. A caller that authorizes the stop has to pass the very key it
    authorized: re-deriving it here could name a different session if a rebind
    lands between the check and the cancel, which is the whole reason the route
    resolves it up front. Omitted only by callers with nothing to authorize
    against, which fall back to the slot's own routing.
    """
    name = slot.key
    cancel_key = cancel_key or _cancel_target(slot)

    # The second press after a DECLINED Stop (the session was compacting) is
    # the user's escape hatch and takes the escalation branch below, which is
    # the only path to a hard kill. The decline itself leaves ``_stop_state``
    # idle -- the queue drain reads that machine as "a stop is in progress" and
    # would persist a false "Session reset" row on the next queued turn -- so the
    # arming lives in its own marker, consumed here: the claim it makes lasts
    # exactly as long as the escalation needs it, on a press that IS a stop.
    # Gated on the caller's own ``escalate``: a caller that says its call may be
    # a retry (a board stop re-sent by a timed-out browser, the steer-containment
    # stop) must never be turned into a hard kill by a marker it did not set.
    # ...and on the compaction still holding the session: the marker is a
    # memory of a refusal, not proof the refusal still applies. Once the
    # compaction has ended, the next press is an ordinary first press and
    # takes the cooperative path; the stale marker is simply dropped.
    # Set when this press is the ESCAPE from a compaction decline (the armed
    # second press, or an explicit ``?force=true``). The hard kill below then
    # keeps the SESSION queue: on a channel-linked slot that queue is the linked
    # channel's, holding other people's messages the compaction, not the user,
    # is what the Stop is aimed at; ``stop_turn`` parks it for the successor.
    # The slot's own dashboard queue is still discarded, as on any hard kill.
    compaction_escape = False
    if slot.running and slot._stop_state == "idle" and stop_declined_armed(slot):
        if escalate and _compaction_in_flight(state, cancel_key):
            # Consumed by the press it armed. A non-escalating call (a retry, the
            # steer-containment stop) must not spend the user's hatch, and a
            # marker for a compaction that has ended is dropped as stale.
            slot._stop_declined_at = 0.0
            slot._stop_state = "soft_pending"
            compaction_escape = True
        elif not _compaction_in_flight(state, cancel_key):
            slot._stop_declined_at = 0.0
    elif (
        force
        and escalate
        and slot.running
        and slot._stop_state == "idle"
        and _compaction_in_flight(state, cancel_key)
    ):
        # An explicit ``?force=true`` on an idle stop state is the escape hatch
        # from the DECLINE only: it must reach the hard-stop escalation below
        # rather than be declined. Without a compaction, a ``force`` arriving on
        # an idle state is a retried first press whose original was lost on the
        # wire, and it keeps the cooperative path it always had.
        slot._stop_state = "soft_pending"
        compaction_escape = True

    # Escalation path: a second stop press while a cooperative cancel is
    # already pending hard-kills. We escalate on ANY second press — not only
    # when the client computed force=true — because the client derives force
    # from the WS-echoed stop_state, which may lag behind the actual state on a
    # slow connection. The backend's own _stop_state is the authoritative
    # "already soft_pending" signal, so a second press always means "kill it".
    #
    # Unless the caller told us this call may not be a second press at all
    # (*escalate*): a re-sent RPC carries no new intent, and the caller is the
    # only layer that can know whether its second call was a decision or a
    # timeout retry. A withheld escalation falls into the no-op branch below.
    if escalate and slot._stop_state == "soft_pending":
        slot._stop_state = "killing"
        # A decline settled its own card and left no open one; the hard kill
        # that follows needs a row of its own, or the last stop row the user
        # sees still reads "nothing was stopped" for a session that was reset.
        if not slot._stop_event_id:
            _open_stop_event_card(slot, "stopping")
        # Survives turn teardown, which resets _stop_state to "idle". Without
        # it a cooperative ack from the first press could still land and label
        # this hard kill a clean stop. Scoped to this card so it cannot defer
        # a later card's ack.
        slot._stop_escalated_card_id = slot._stop_event_id
        await _settle_discarded_stage_deliveries(
            state,
            slot,
            [str(entry.get("content", "")) for entry in slot._queue],
        )
        slot._queue.clear()
        # Hard kill = "discard everything": drop unconsumed steers too, so the
        # end-of-turn requeue (chat_runner finally) has nothing to resurrect.
        # Mirrors the queue clear above; a soft stop preserves both.
        #
        # Their delivery ids go with them, and that is load-bearing rather than
        # tidiness: `steer_into_running_turn` reconciles an in-flight steer by
        # asking what removed its registration, and a CONSUMED steer leaves its
        # `_steer_delivery_ids` entry in place. Dropping the entry here is
        # therefore what tells the two apart -- absence means this hard kill
        # discarded the text, so the caller is told it was not delivered instead
        # of having a row persisted for a message that never ran.
        for _discarded in slot._pending_steers:
            slot._steer_delivery_ids.pop(_discarded, None)
            # Lockstep with the line above (see `_ChatSlot._steer_send_ids`): a hard
            # kill discards the text, so there is no requeued entry left to carry
            # the client's send id onto.
            slot._steer_send_ids.pop(_discarded, None)
            slot._steer_user_origin.pop(_discarded, None)
            slot._steer_channel_origin.pop(_discarded, None)
            slot._steer_admissions.pop(_discarded, None)
            slot._steer_attachment_meta.pop(_discarded, None)
            slot._steer_decision_strips.pop(_discarded, None)
            slot._steer_possibly_delivered.discard(_discarded)
        slot._pending_steers.clear()
        state.push_slots_update()
        logger.info("Stop (force): hard-killing session for slot %s", name)

        # Escalation reuses the card the first press opened, so bind to it.
        _on_hard_force = _make_stop_resolver(state, slot, "hard", slot._stop_event_id)

        # Unblock chat runner if it's suspended waiting for tool approval or on
        # a pending ask_question card.
        _unblock_pending_waits(state, slot)
        # Stop addresses the SESSION, so it resolves through
        # effective_session_key: a channel-linked slot's turns run under its
        # linked_session_key (slack:<ts>), and handing stop_turn the
        # dashboard:<slot> key names a session no running turn owns — the stop
        # reports success and cancels nothing. The SEL record below stays on the
        # slot-derived key, which identifies the tab the operator pressed.
        await state.sessions.stop_turn(
            cancel_key,
            force=True,
            preserve_queue=compaction_escape,
            on_hard=_on_hard_force,
        )
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="hard",
            # Record what the client requested (force flag) vs. the escalation
            # the backend actually performed (always a hard kill here).
            metadata={"slot": name, "via": source, "force": force, "escalated": True},
        )
        return {"ok": True}

    # Already stopping or not running — no-op (idempotent repeat press guard)
    if slot._stop_state != "idle" or not slot.running:
        if not slot.running:
            logger.info("Stop: slot %s not running, ignoring", name)
            _info = "not running"
        else:
            _info = "stop already in progress"
        _meta: dict[str, Any] = {"slot": name, "via": source, "reason": _info}
        if not escalate and slot._stop_state == "soft_pending":
            # The branch a de-duplicated retry lands on. Recorded so the audit
            # shows an escalation was WITHHELD rather than never asked for --
            # without it there is no record that a retry rather than a decision
            # caused the outcome, and an absorbed retry has to be visible too.
            _meta["escalation_withheld"] = True
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="noop",
            metadata=_meta,
        )
        # ``already_stopping`` separates the two facts this branch merges: a
        # target that was never running has nothing to stop, while one whose
        # cooperative cancel is still in flight IS stopping. Both answer
        # ``info``, and a caller that renders them alike tells the second one the
        # opposite of what happened — which the de-duplicated retry above now
        # reaches routinely.
        return {"ok": True, "info": _info, "already_stopping": bool(slot.running)}

    # A cooperative Stop while the session's own automatic /compact holds it is
    # DECLINED, before any of the soft-stop side effects below run. Cancelling
    # that turn fails the compaction, and the failure arm recycles the session:
    # a user who pressed Stop on what looked like a stalled turn would lose the
    # session's memory to a restart the notice then blames on compaction.
    # The card the press would have opened is opened and settled in
    # one step, so the press still leaves a visible answer in the transcript.
    # A force stop (second press, or ?force=true) is the escape hatch and is
    # never declined: the force branch above runs first, and the decline below
    # arms ``_stop_declined_at`` so the NEXT press reaches that branch instead
    # of being declined again -- a live turn that shares the session with a
    # compaction must stay stoppable. ``_stop_state`` stays idle: nothing is
    # stopping. ``stop_turn`` repeats the compacting check for the race in
    # which a compaction starts between here and the cancel.
    if _compaction_in_flight(state, cancel_key):
        stop_id = _open_stop_event_card(slot, "stopping")
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        if escalate:
            # Only a press that could itself escalate arms the second press; a
            # caller that said "this may be a retry" must not arm a hard kill
            # for the user's next ordinary Stop.
            slot._stop_declined_at = time.monotonic()
        state.push_slots_update()
        logger.info("Stop: declined for slot %s — compaction in flight", name)
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="compacting",
            metadata={"slot": name, "via": source, "force": False, "stop_id": stop_id},
        )
        return {"ok": True, "info": "compacting", "compacting": True}

    # First press: soft stop
    slot._stop_state = "soft_pending"
    # NOTE: Do NOT clear the queue here — stop should only cancel the
    # currently running turn, leaving queued messages intact for the user
    # to process or dismiss individually.

    # One card per press: re-arm an orphaned card in place or append a fresh
    # one (see _open_stop_event_card for why sweeping the orphan rendered two
    # chips).
    stop_id = _open_stop_event_card(slot, "stopping")
    state.push_slots_update()
    logger.info("Stop: cooperative cancel for slot %s (queue=%d)", name, len(slot._queue))

    _on_soft = _make_stop_resolver(state, slot, "soft", stop_id)
    _on_hard = _make_stop_resolver(state, slot, "hard", stop_id)

    # Unblock chat runner if it's suspended waiting for tool approval or on a
    # pending ask_question card.
    _unblock_pending_waits(state, slot)

    outcome = await state.sessions.stop_turn(
        cancel_key,
        force=False,
        preserve_queue=True,
        on_soft=_on_soft,
        on_hard=_on_hard,
    )
    # A genuine in-flight turn whose cooperative cancel does not confirm within
    # the budget answers ``stop_turn`` with a non-acked outcome, which that
    # method escalates to a hard reset on its own -- a dispatched, mid-execution
    # tool call that never acks is reaped there. ``"idle"`` is the opposite
    # signal: the provider holds no active turn to cancel (its model stream
    # reached the done boundary, or no session is registered for the key). When
    # the slot still reads running at that point its turn ended at the provider
    # but the slot has not seen the terminal event settle it. The orphaned card
    # is resolved and the reply names the honest state -- there is no running
    # provider turn here to report as "stopped", and no provider turn to kill.
    _idle_running = outcome == "idle" and slot.running
    if outcome == "idle" and slot._stop_event_id:
        _resolve_stop_event(slot, "soft")
        slot._stop_state = "idle"
        state.push_slots_update()
    elif outcome == "compacting":
        # The race the pre-check above cannot close: the compaction committed
        # between that read and the cancel. Same answer, and the soft-stop side
        # effects taken above are undone: ``_stop_state`` back to idle (nothing
        # is stopping), the decline marker armed so the next press escalates,
        # as on the pre-check path.
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        slot._stop_state = "idle"
        if escalate:
            slot._stop_declined_at = time.monotonic()
        state.push_slots_update()
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_stop",
        tool_kind="command",
        outcome=outcome,
        metadata={"slot": name, "via": source, "force": False},
    )
    if outcome == "compacting":
        return {"ok": True, "info": "compacting", "compacting": True}
    if _idle_running:
        # The provider held no active turn to cancel while the slot still read
        # running: the turn ended at the provider but the slot had not settled.
        # Naming it keeps the reply from reading as "stopped a running turn".
        return {"ok": True, "info": "no active turn"}
    return {"ok": True}


def _compaction_in_flight(state: DashboardState, cancel_key: str) -> bool:
    """The shared pre-stop probe (``session_lifecycle.compaction_in_flight``)."""
    return compaction_in_flight(state.sessions, cancel_key)


async def api_chat_slot_stop(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/stop — cooperative stop with kill fallback.

    The route is where authorization lives, because it is the only layer holding
    the ``request`` an app token rides on. ``stop_slot_turn`` is the mechanism
    and takes a slot, so every caller that reaches it by another path (session
    control) has to establish its own authority — the guard cannot be inherited
    by accident.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_stop")
    if denied is not None:
        return denied
    # Before ANY side effect — the escalation branch inside stop_slot_turn clears
    # the queue and drops pending steers, so a guard placed later would still let
    # a foreign caller mutate the slot. One target, resolved once: the session the
    # in-flight turn actually runs on, so authorization and the stop cannot
    # disagree across a mid-turn rebind.
    cancel_key = _cancel_target(slot)
    denied = _app_cancel_denied(request, slot, "chat_stop", cancel_key)
    if denied is not None:
        return denied
    force = request.query.get("force", "").lower() == "true"
    return web.json_response(await stop_slot_turn(state, slot, force=force, cancel_key=cancel_key))


async def api_chat_slot_continue(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/continue — hand the thread back to the agent.

    Two callers, one mechanism: picking up a turn that was cut short, and asking
    a slot that finished cleanly to carry on. They are one endpoint because they
    are indistinguishable from the transcript — a force-quit runs no ``finally``,
    so a killed turn leaves no error row behind and reads exactly like a
    completed one. ``_has_conversation`` authorizes; ``_is_interrupted`` only
    chooses which of the two continuation bodies the model receives.

    Runs the same synthetic-continuation machinery the runner already uses for
    its own post-transient recovery: queue the continuation at the head, then let
    ``_start_next_queued_turn`` land it as an ``inject`` row and dispatch the
    turn. No bespoke dispatch path, and the row folds into the existing recovery
    card instead of printing machine prose as a user bubble.

    The frontend decides whether to OFFER this (it has the transcript, `running`
    and the queue locally, so it needs no server field for that). This endpoint
    re-checks under ``slot._lock`` because the client's view is a WS snapshot and
    therefore lagging: a press landing in the instant a turn starts, or a second
    browser tab acting on a stale cache, would otherwise dispatch a duplicate
    turn against one slot — real tokens, real tool calls, real repo writes. Every
    other dispatch route guards the same way (see ``api_chat_slot_regenerate``).

    NOT readiness-gated, and that is deliberate — see
    ``kiro_readiness.reject_if_kiro_unverified``. Continue is an ordinary send: it
    queues one synthetic message and lets the runner dispatch it, mutating nothing
    durable up front, so the ACP attempt is its authority and a signed-out install
    reports ``AcpAuthRequired`` in the transcript. Gating it instead put the
    button behind a latch that is refreshed by re-probing ``kiro-cli``, and a
    probe that merely TIMES OUT reads as signed-out: on a host where that probe is
    slow the press was refused with a 503 forever while typing the same request by
    hand worked. The unequal treatment of two paths that dispatch the same turn is
    the bug; the transcript's own error card is the report either way.

    The one refusal that reads the transcript's content is a different thing
    from a readiness gate: ``session_start_repeat`` fires only when the slot's
    OWN tail holds two ``session_start_failed`` error rows with nothing but
    recovery rows between them -- the same start, re-issued by Resume, failed
    twice. That is not a probe that can be wrong forever; it is the record of
    what this endpoint itself just did twice. A typed message stays allowed and
    resets the count, so the refusal never latches the slot.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check (App Kit §5.2): deny-by-default for app tokens, mirroring
    # api_chat. Without it an app token holding /api/chat could resume ANY
    # interrupted slot — including a dashboard user's — and that is not a read: it
    # dispatches an agent turn that runs tools and writes to the repo. Same
    # indistinguishable 404 as the send path, so the response cannot be used to
    # probe which foreign slots exist.
    request_app = request.get("app", "")
    denied = deny_app_slot_access(request_app, slot, slot.key, "chat_continue")
    if denied is not None:
        return denied

    # A relay archive never runs. AFTER the app-ownership 404 above: a foreign
    # app must not be able to tell an archive apart from a missing slot.
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal

    try:
        await continue_slot_turn(
            state,
            slot,
            directive_user_origin=not bool(request.get("app", "")),
            # Same-name registration can change while the lock or child probe waits.
            replaced=lambda: _slot_replaced_while_queued(
                state, slot, name, request, "chat.slot_continue"
            ),
        )
    except SlotContinueRefusal as exc:
        if exc.code == "slot_not_found":
            return _slot_not_found()
        return web.json_response({"error": exc.message, "code": exc.code}, status=exc.status)
    return web.json_response({"ok": True, "slot": slot.key})


class SlotContinueRefusal(Exception):
    """A Continue that was refused, carrying the 409 the endpoint renders.

    Raised by :func:`continue_slot_turn` so the Continue endpoint and
    session-control's ``retry_target`` map the same refusals the same way.
    ``code`` is the machine-readable contract; ``message`` is advisory prose.
    """

    def __init__(self, message: str, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


async def continue_slot_turn(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    directive_user_origin: bool,
    via: str = "",
    require_interrupted: bool = False,
    before_dispatch: Callable[[], None] | None = None,
    extra_meta: dict[str, Any] | None = None,
    replaced: Callable[[], bool] | None = None,
) -> None:
    """Queue the Resume/Continue body at the head of *slot* and dispatch it.

    The mechanism behind ``api_chat_slot_continue``, shared with
    ``session_control.retry_target`` so the button and the ``session_retry``
    tool run one path and refuse the same states. Authorization is the
    caller's: this function checks only whether the slot can take a
    continuation right now, and raises :class:`SlotContinueRefusal` when it
    cannot.

    ``require_interrupted`` refuses a slot whose last turn did not fail
    (``turn_not_failed``); Continue leaves it off. ``before_dispatch`` runs
    synchronously under the slot lock immediately before the queue insert and
    may raise :class:`SlotContinueRefusal`. ``via`` is recorded on the
    ``dashboard_continue`` audit line when set. ``extra_meta`` is merged into
    the queue entry's ``meta`` (session control stamps the sending slot there,
    so a drain-time drop is reported back to it). ``replaced`` is checked after
    each await (the lock, the sub-agent probe) and refuses with a 404
    ``slot_not_found`` when it returns True: the Continue endpoint passes
    ``_slot_replaced_while_queued`` for its slot name.
    """
    async with slot._lock:
        # Same-name registration can change while the lock or child probe waits.
        if replaced is not None and replaced():
            raise SlotContinueRefusal("not found", "slot_not_found", status=404)
        if slot.running:
            raise SlotContinueRefusal("slot is running", "slot_running")
        if slot._stopping or slot._stop_state != "idle":
            raise SlotContinueRefusal("a stop is in progress", "slot_stopping")
        if slot.queue_depth:
            # The runner is about to pick the thread back up on its own; adding a
            # continuation would double-fire.
            raise SlotContinueRefusal("queued messages pending", "slot_queue_pending")
        if any(not f.done() for f in slot._approval_futures.values()):
            raise SlotContinueRefusal("approval pending", "slot_approval_pending")
        # Background sub-agents are still running (or waiting to start) for this
        # slot. `slot.running` is False here — the parent turn ENDS while its
        # children keep going — so nothing above catches this, and the widened
        # gate below makes it the common shape rather than the rare one (before
        # this endpoint accepted a settled transcript, a parent that finished
        # cleanly after `spawn_run` was refused only incidentally, by
        # `_is_interrupted`).
        #
        # It has to be refused HERE rather than left to the queue: a synthetic
        # recovery entry satisfies `is_system_injection_item`, so
        # `_dequeue_next_system_message` drains it straight through the
        # `hold_users` gate that exists to stop exactly this (chat_runner) — the
        # hold only holds plain USER messages. A parent turn would start and
        # interleave tool calls and repository writes with its own children's
        # completion injections. `api_chat` queues instead of dispatching for the
        # same reason; Continue has nowhere to queue to, so it refuses.
        #
        # Children guard — see _subagents_attached_response for the three
        # probes and why each is load-bearing. `effective_session_key`, never
        # `f"dashboard:{slot.key}"`: a channel-born slot's children register
        # under the channel key, and the dashboard-prefixed form silently
        # matches nothing — `_history_key_for`'s own docstring says as much.
        subagents_denied = await _subagents_attached_response(
            state, slot, effective_session_key(slot), "continue"
        )
        if replaced is not None and replaced():
            raise SlotContinueRefusal("not found", "slot_not_found", status=404)
        if subagents_denied is not None:
            raise SlotContinueRefusal("sub-agents are running", "slot_subagents_running")
        if not _has_conversation(slot):
            raise SlotContinueRefusal("nothing to continue", "slot_empty")
        # The same session start has already failed twice in a row with nothing
        # but Resume presses between the attempts. Continue would re-issue the
        # identical ``session/new`` a third time: the turn has no registered
        # session, so it starts one, and nothing about the request changed
        # since the last two walls. This is the one refusal that reads the
        # transcript's CONTENT rather than its shape, and it is not a
        # readiness gate (see the docstring): the evidence is the slot's own
        # two tagged rows, not a probe that can be wrong forever, and typing a
        # message is still allowed -- a user row ends the streak, so a typed
        # retry that fails once gets its Resume back. The first failure keeps
        # today's behaviour exactly: one Resume, same continuation, same words.
        failures = session_start_failure_streak(slot.messages)
        if failures >= _SESSION_START_REPEAT_REFUSAL_AT:
            raise SlotContinueRefusal(
                f"the agent session failed to start {failures} times in a "
                "row; Resume would run the same start again. Restart the "
                "gateway (kirocrew restart), then send your message again.",
                "session_start_repeat",
            )

        # _is_interrupted does not AUTHORIZE the continue — it only picks which
        # body to inject. Both are true statements about their own case, and
        # getting this wrong is not cosmetic: telling a model that finished
        # cleanly that it was "interrupted before it finished" sends it looking
        # for half-done work that does not exist.
        interrupted = _is_interrupted(slot)
        if require_interrupted and not interrupted:
            # session_retry's own refusal: a turn that finished, or that the user
            # ended with Stop, is not a failure to re-run. Continue would carry on
            # from it, but a retry verb must never become a way for another
            # session to make this one produce a fresh answer on top of a good one.
            raise SlotContinueRefusal(
                "the last turn did not fail; there is nothing to retry",
                "turn_not_failed",
            )
        resume = _MANUAL_RESUME_MSG if interrupted else _MANUAL_CONTINUE_MSG
        if before_dispatch is not None:
            # Synchronous, under the lock, with no await between it and the
            # insert below: a caller whose authority was decided before the lock
            # (session control) re-checks it at the last point where refusing
            # still changes nothing.
            before_dispatch()
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.session_control import containment_meta

        # Admission stamp + provenance: recovery-kind entries are subject
        # to drain re-validation like any other externally admitted content, and
        # provenance follows the CALLER — the same request-identity split as
        # api_chat. An app hitting Continue on its own slot must not gain the
        # authenticated-human flag that gates session-mutating effects.
        slot.queue_insert(
            0,
            resume,
            kind=SYNTHETIC_RECOVERY_KIND,
            meta={**containment_meta(state, slot), **(extra_meta or {})},
            directive_user_origin=directive_user_origin,
        )

    # The queue entry is already committed; audit failure must not strand it.
    try:
        sel().log_tool_invocation(
            session_key=_history_key_for(slot.key),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_continue",
            tool_kind="command",
            outcome="ok",
            metadata={"slot": slot.key, **({"via": via} if via else {})},
        )
    except Exception:
        logger.warning("continue: SEL audit failed (slot %s)", slot.key, exc_info=True)
    started = await _start_next_queued_turn(state, slot)
    if not started:
        # Lost a race for the queue entry (a concurrent dequeue consumed it).
        # The turn is running either way, so this is not an error for the caller.
        logger.info("continue: queue entry consumed by a concurrent dequeue (slot %s)", slot.key)
    state.push_slots_update()


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


def session_start_failure_streak(messages: list[dict]) -> int:
    """How many session starts in a row failed at the tail of *messages*.

    Walks back from the newest row counting ``error`` rows stamped with
    ``SESSION_START_FAILED_KIND`` (the structural tag ``chat_runner`` writes
    from the exception, never from the prose). Every row that is not the
    conversation's floor is walked past -- the ``inject`` row a Resume press
    lands as, tool rows, notices -- so two failures separated only by the user
    pressing Resume are consecutive. The walk stops at the first row that IS
    new information: a user or assistant row with content (a typed retry is a
    new attempt and starts the count over), an error row of any OTHER kind (a
    connection-lost row is a different failure, not a third start), or a row
    that OPENS a turn of its own -- a nudge, a sub-agent completion, or an
    ``inject`` whose kind begins new work (``_TURN_OPENING_INJECT_KINDS``,
    the mirror of ``INJECT_KIND_OPENS_TURN`` in ``RecoveryCard.tsx``) -- since
    a failure before such a row belongs to a different turn and must not cost
    this turn its first Resume. A ``recovery`` inject resumes the SAME turn and
    is walked past, which is what makes two Resume-separated failures
    consecutive.

    The transcript is the count because nothing else can hold it: a start
    that never answered registered no session, so the per-session
    ``consecutive_failures`` counter in ``SessionManager.record_failure`` is
    never reached for it. Mirrors ``sessionStartFailureStreak`` in
    ``website/src/pages/chat/ErrorCard.tsx``; the two must agree, or the card
    hides a Resume the server would have honoured (or offers one it refuses).
    """
    streak = 0
    for m in reversed(messages):
        role = m.get("role")
        meta = m.get("meta")
        if role == "error":
            kind = meta.get("kind") if isinstance(meta, dict) else None
            if kind == SESSION_START_FAILED_KIND:
                streak += 1
                continue
            break
        if is_stop_event_row(m):
            break
        if role in ("nudge", "subagent"):
            break
        if role == "inject" and (
            isinstance(meta, dict) and meta.get("injectKind") in _TURN_OPENING_INJECT_KINDS
        ):
            break
        if role in ("user", "assistant") and m.get("content") and not is_system_notice(role, meta):
            break
    return streak


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


def _is_stop_event(m: dict) -> bool:
    """True when *m* is the card recorded because the user pressed Stop.

    Thin alias over ``state.is_stop_event_row`` — the predicate lives there
    (next to ``parse_cls_meta``, its one dependency) so the slot-summary
    builder can share it without importing this handler module.
    """
    return is_stop_event_row(m)


def _is_interrupted(slot: _ChatSlot) -> bool:
    """True when the transcript shows a turn that ended without a reply.

    Thin adapter over ``state.is_turn_interrupted``, which owns the scan and
    its contract (see its docstring). Shared with the slot-summary builder so
    the Continue endpoint, the composer's Resume gate, and the sidebar's
    ``interrupted`` field can never disagree about what an interruption is.
    """
    return is_turn_interrupted(slot.messages)


async def api_chat_slot_end_wait(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/end-wait — ask the sleeping `wait` tool to
    return early. Body: ``{"wait_id": "..."}``.

    Cooperative, and deliberately NOT a cancel. The tool sleeps in a separate
    MCP subprocess that runs no listener, so there is nothing to signal: the
    request is parked on the slot and collected by the tool on its next
    keepalive poll (see WAIT_PING_SECS — bounded at 5s). The turn then continues
    with a normal tool result, which is the whole point of not routing this
    through /stop: /stop can only end a wait as collateral of killing the
    session, losing in-flight results and paying a respawn.

    ``wait_id`` is required and must match the sleep currently in flight. That
    rejects the two races a slot-scoped flag would have accepted: a click landing
    after the wait already elapsed, and a click from a stale tab still showing a
    previous wait's countdown.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_end_wait")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    wait_id = str(body.get("wait_id") or "").strip()
    if not wait_id:
        return web.json_response(
            {"error": "wait_id required", "code": "wait_id_required"}, status=400
        )
    current = slot._wait_state or {}
    if current.get("wait_id") != wait_id:
        return web.json_response(
            {"error": "no such wait in flight", "code": "wait_not_in_flight"}, status=409
        )
    slot._end_wait_request = wait_id
    # The button, not a session: clears any requester a session_end_wait left
    # behind, so the woken tool reports the user as the one who ended it.
    slot._end_wait_by = ""
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_end_wait",
        tool_kind="command",
        outcome="success",
        metadata={"slot": name, "wait_id": wait_id},
    )
    return web.json_response({"ok": True})


async def api_chat_slot_interrupt(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/interrupt — run a selected queued message.

    A running parent turn is stopped while its queue is preserved for the normal
    tail drain. An idle parent requires ``{"queue_id": "..."}`` and dispatches
    that selected queue card directly; this is the explicit override for a user
    who does not want to wait for attached subagents. A running parent accepts
    ``queue_id`` optionally to promote one card before the preserved queue drains.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    # Before the _stop_state claim and the queue promotion below, both of which
    # mutate the slot ahead of stop_turn.
    # Resolved once, before the request-body await below, and used for both the
    # guard and the cancel — see api_chat_slot_stop for the same rule.
    cancel_key = _cancel_target(slot)
    denied = _app_cancel_denied(request, slot, "chat_interrupt", cancel_key)
    if denied is not None:
        return denied
    if not slot.running:
        if not slot._queue:
            return web.json_response({"ok": True, "info": "not running"})
        refusal = relay_archive_refusal(slot)
        if refusal is not None:
            return refusal
        body, body_err = await read_bounded_json(request, allow_absent=True)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        raw_queue_id = body.get("queue_id")
        if raw_queue_id is not None and not isinstance(raw_queue_id, str):
            return web.json_response(
                {"error": "queue_id must be a string", "code": "invalid_queue_id"},
                status=400,
            )
        queue_id = (raw_queue_id or "").strip() or None
        # The idle bypass is the "run THIS selected card while attached
        # subagents keep going" action, and nothing else. Requiring an explicit
        # queue_id keeps `allow_user_during_subagents=True` and the dispatch
        # itself bound to a card the user picked: without one there is no
        # selection to justify bypassing the child-work hold, and dispatching
        # whatever sits at the queue front would run — and acknowledge —
        # unselected work the user never chose. Reject rather than fall back.
        if queue_id is None:
            return web.json_response(
                {"error": "queue_id required for idle interrupt", "code": "invalid_queue_id"},
                status=400,
            )
        async with slot._lock:
            # The request body and lock acquisition both yield. A close followed
            # by same-name recreation during either await must not let this stale
            # object dispatch work into the replacement's session namespace.
            if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_interrupt"):
                return slot_not_found()
            if slot.running:
                return web.json_response(
                    {"error": "slot started running", "code": "slot_running"}, status=409
                )
            if slot._stopping or slot._stop_state != "idle":
                return web.json_response(
                    {"error": "a stop is in progress", "code": "slot_stopping"}, status=409
                )
            # The body read above can race a cron/workflow rebind on this same
            # live slot. Re-authorize the session the queued turn will use while
            # holding the dispatch lock, immediately before starting it.
            denied = _app_cancel_denied(
                request, slot, "chat_interrupt", effective_session_key(slot)
            )
            if denied is not None:
                return denied
            started = await _start_next_queued_turn(
                state,
                slot,
                allow_user_during_subagents=True,
                required_queue_id=queue_id,
            )
        if not started:
            return web.json_response(
                {
                    "error": "queued message is no longer available",
                    "code": "queue_item_unavailable",
                },
                status=409,
            )
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="started",
            metadata={"slot": name, "queue_id": queue_id},
        )
        state.push_slots_update()
        return web.json_response({"ok": True, "outcome": "started"})
    # Idempotent guard: interrupt already in progress. State alone decides —
    # do NOT also require _stop_event_id: after the early soft_pending claim
    # below, a concurrent request can arrive before the stop card is created
    # (event id still None), and a compound condition would let it through.
    if slot._stop_state != "idle":
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="noop",
            metadata={"slot": name, "reason": "stop already in progress"},
        )
        return web.json_response({"ok": True, "info": "stop already in progress"})
    if not slot._queue:
        return web.json_response({"error": "queue empty, use /stop instead"}, status=400)

    # An automatic compaction holds the session: the interrupt is declined
    # BEFORE the claim below mutates the running turn (``_stop_state``) and
    # before ``_unblock_pending_waits`` rejects its pending
    # approvals -- none of that may happen to a turn that is not being stopped.
    # No escalation marker: an interrupt is "run the next queued message", not
    # a Stop, and must not turn the user's next Stop press into a hard kill.
    # ``_stop_state`` stays idle.
    if _compaction_in_flight(state, cancel_key):
        stop_id = _open_stop_event_card(slot, "interrupting")
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        state.push_slots_update()
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="compacting",
            metadata={"slot": name, "stop_id": stop_id},
        )
        return web.json_response({"ok": True, "outcome": "compacting", "compacting": True})

    # Claim the stop slot synchronously BEFORE the await below: the
    # idempotency guard above is check-then-act, and a concurrent /interrupt
    # arriving during the awaited body read below would otherwise still see
    # _stop_state == "idle" and slip past the guard (double stop_turn +
    # double SEL audit for one logical press). /stop is race-safe because it
    # has no await between guard and claim; this makes /interrupt match.
    slot._stop_state = "soft_pending"
    # Per-attempt identity for the claim itself. The stand-down guard below
    # cannot rely on the state VALUE alone: a concurrent /stop can escalate,
    # settle to idle, and a further press can re-claim "soft_pending" — a
    # LATER stop wearing the same value. The generation tells the two apart
    # (`_make_stop_resolver` already establishes it as the only per-attempt
    # identity that survives card reuse); the claim above bumped it, so any
    # later initiation moves it again.
    claim_generation = slot._stop_generation

    # Optionally promote a specific queue item to front. The except is not a
    # parse guard (read_bounded_json owns that): it rolls the claimed stop
    # state back when the body read fails in transit, and the refused-body
    # branch below rolls it back the same way. The rollback is
    # conditional on our claim being intact: a concurrent /stop arriving
    # during the body await may escalate _stop_state (e.g. to "killing"),
    # and an unconditional reset to "idle" would erase that escalation and
    # admit another stop while the hard kill is still running.
    try:
        body, body_err = await read_bounded_json(request, allow_absent=True)
    except Exception:
        # Generation-guarded like the stand-down below: "soft_pending" alone
        # cannot prove the claim is OURS — an escalate-settle-repress sequence
        # during the await leaves a LATER press's live claim wearing the same
        # value, and rolling that back would idle its stop mid-cancel and
        # re-enable auto-run under a real stop.
        if slot._stop_state == "soft_pending" and slot._stop_generation == claim_generation:
            slot._stop_state = "idle"
        raise
    if body_err is not None:
        if slot._stop_state == "soft_pending" and slot._stop_generation == claim_generation:
            slot._stop_state = "idle"
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    queue_id = body.get("queue_id")
    if queue_id and slot._stop_generation == claim_generation:
        # Wire-side field is `queue_id`; stored items carry `id` (the key
        # queue_append/queue_insert write and every *_by_id helper matches).
        # The previous inline loop compared item.get("queue_id"), which is
        # None on every production item — a silent no-op that made the
        # "run this next" click land on whatever happened to be at the
        # front of the queue instead of the selected message.
        #
        # BEFORE the supersede guard below, and GENERATION-GATED, because the
        # supersessions differ: a claim superseded benignly (the running turn
        # ends during the body read, teardown resets the posture, generation
        # unmoved) must still land the user's "run this next" choice; a claim
        # superseded by a LATER stop (generation moved) must NOT — that stop's
        # own /interrupt may have promoted ITS selection, and a stale write
        # here would overwrite it. Escalation keeps the same generation and a
        # cleared queue, so promotion there is a harmless no-op.
        slot.queue_promote_by_id(queue_id)

    # A concurrent /stop can supersede our claim during the body await:
    # escalate it (soft_pending → killing), or escalate-settle-and-be-followed
    # by a FURTHER press whose fresh claim wears the same "soft_pending" value
    # — which is why this compares the GENERATION, our claim's per-attempt
    # identity, not just the state value. Continuing on a superseded claim
    # would open/reuse a card owned by the other stop — and the reuse path's
    # marker-clear would erase a LIVE escalation marker, letting a late
    # cooperative ack relabel the hard kill as a clean stop. The other stop
    # owns the posture now: stand down and answer like the idempotent-repeat
    # branch above. This also fires when the superseding stop has ALREADY settled
    # (state back to "idle"), including the benign case where the running
    # turn simply ended during the body read; queue promotion already
    # happened above, so nothing of the user's intent is dropped.
    if slot._stop_state != "soft_pending" or slot._stop_generation != claim_generation:
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="noop",
            metadata={"slot": name, "reason": "stop claim superseded during body read"},
        )
        return web.json_response({"ok": True, "info": "stop already in progress"})

    # The probe again, AFTER the body await and with no await between here and
    # the pending-wait rejection below. A compaction that committed during the
    # body read would otherwise have the user's pending approval or question
    # rejected (irreversible) by a Stop the compaction then declines. Same
    # answer as the pre-check: the claim is released, nothing is touched.
    if _compaction_in_flight(state, cancel_key):
        slot._stop_state = "idle"
        stop_id = _open_stop_event_card(slot, "interrupting")
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        state.push_slots_update()
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="compacting",
            metadata={"slot": name, "stop_id": stop_id, "reason": "committed during body read"},
        )
        return web.json_response({"ok": True, "outcome": "compacting", "compacting": True})

    # Stop current turn but preserve the queue so dequeue loop fires
    # (soft_pending already claimed above, before the request-body await)

    # One card per press: re-arm an orphaned card in place or append a fresh
    # one (see _open_stop_event_card for why sweeping the orphan rendered two
    # chips).
    stop_id = _open_stop_event_card(slot, "interrupting")
    state.push_slots_update()

    # Built after the card exists so each resolver is bound to this card.
    _on_soft = _make_stop_resolver(state, slot, "soft", stop_id)
    _on_hard = _make_stop_resolver(state, slot, "hard", stop_id)

    # Unblock chat runner if it's suspended waiting for tool approval or on a
    # pending ask_question card.
    _unblock_pending_waits(state, slot)

    outcome = await state.sessions.stop_turn(
        cancel_key,
        force=False,
        preserve_queue=True,
        on_soft=_on_soft,
        on_hard=_on_hard,
    )
    # Resolve orphaned card when provider reports no active turn
    if outcome == "idle" and slot._stop_event_id:
        _resolve_stop_event(slot, "soft")
        slot._stop_state = "idle"
        state.push_slots_update()
    elif outcome == "compacting":
        # The window the two probes above cannot close: a compaction commits
        # between the second probe and ``stop_turn`` taking the registry lock,
        # with no await of ours in between, so only another task's tick can
        # land here. Nothing was interrupted. The pending waits
        # ``_unblock_pending_waits`` rejected cannot be un-rejected, which is
        # why both probes run first. ``_stop_state`` goes back to idle; no
        # escalation marker, for the reason the pre-check gives.
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        slot._stop_state = "idle"
        state.push_slots_update()
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_interrupt",
        tool_kind="command",
        outcome=outcome,
        metadata={"slot": name, "queue_id": queue_id},
    )
    return web.json_response({"ok": True, "outcome": outcome})


async def api_chat_slot_queue_cancel(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot}/queue/{queue_id} — cancel a queued message.

    Removes the message from the backend queue and broadcasts a
    ``queue_cancel`` WebSocket event so the frontend can move the
    text back to the input box.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    queue_id = request.match_info["queue_id"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_cancel")
    if denied is not None:
        return denied
    # Read the entry's origin before removing it: a cancel puts the text back in
    # the composer, so a redacted copy of the user's own words would replace the
    # link they typed with a placeholder.
    _user_origin = queue_entry_is_user_origin(
        next((i for i in slot._queue if i["id"] == queue_id), None)
    )
    content = slot.queue_remove_by_id(queue_id)
    if content is None:
        return web.json_response({"error": "queue item not found"}, status=404)
    _remove_queued_by_id(slot.messages, queue_id)
    slot.invalidate_source_links()
    _redacted = queued_text_for_display(content, user_origin=_user_origin)
    state.broadcast_ws("queue_cancel", {"slot": name, "queue_id": queue_id, "content": _redacted})
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_cancel",
        tool_kind="permission",
        outcome="allowed",
        metadata={"queue_id": queue_id, "slot": name},
    )
    return web.json_response({"ok": True, "content": _redacted})


async def api_chat_slot_queue_edit(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/queue/{queue_id} — edit a queued message.

    Accepts ``{"content": "new text"}`` and replaces the content of the
    matching queue item in place (order preserved).  Broadcasts a
    ``queue_edit`` WebSocket event so all connected clients update in sync.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    queue_id = request.match_info["queue_id"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_edit")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return web.json_response({"error": "content must be a non-empty string"}, status=400)
    if not slot.queue_edit_by_id(
        queue_id,
        content,
        directive_user_origin=not bool(request.get("app", "")),
    ):
        return web.json_response({"error": "queue item not found"}, status=404)
    # The stored text is what the edit normalized to (attachment markers are
    # renumbered when the edit dropped one), so the row and the broadcast echo
    # the ENTRY, not the request body.
    entry = next((i for i in slot._queue if i["id"] == queue_id), None)
    stored = entry.get("content") if entry is not None else None
    if isinstance(stored, str):
        content = stored
    _edit_queued_by_id(slot.messages, queue_id, content)
    slot.invalidate_source_links()
    _redacted = queued_text_for_display(content, user_origin=queue_entry_is_user_origin(entry))
    frame: dict[str, Any] = {"slot": name, "queue_id": queue_id, "content": _redacted}
    # The edit prunes and renumbers the entry's attachment lists alongside the
    # text (`prune_attachment_meta`), so the frame carries the lists the
    # renumbered markers now index -- the client replaces the row's lists from
    # it. Same `meta` shape and redaction as `queue_entry_view`, read straight
    # off the entry so the content is not redacted a second time. Absent when
    # the entry has none left (or never had any): the client reads absence on
    # THIS frame as "no lists", so a row whose markers the edit all removed
    # drops its stale lists too.
    _edit_attachments = attachment_meta(entry.get("meta")) if entry is not None else {}
    if _edit_attachments:
        frame["meta"] = _edit_attachments
    state.broadcast_ws("queue_edit", frame)
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_edit",
        tool_kind="permission",
        outcome="allowed",
        metadata={"queue_id": queue_id, "slot": name},
    )
    return web.json_response({"ok": True, "content": _redacted})


async def api_chat_slot_queue_reorder(request: web.Request) -> web.Response:
    """PUT /api/chat/slots/{slot}/queue/order — reorder queued messages.

    Accepts ``{"order": ["qid1", "qid2", ...]}`` and rearranges the slot's
    ``_queue`` to match the given id sequence.  Broadcasts a ``queue_reorder``
    WebSocket event so all connected clients update in sync.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_queue_reorder")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        return web.json_response({"error": "order must be a list of queue id strings"}, status=400)
    # Build lookup of current queue items by id
    by_id = {item["id"]: item for item in slot._queue}
    # Validate all ids exist
    missing = [qid for qid in order if qid not in by_id]
    if missing:
        return web.json_response({"error": f"unknown queue ids: {missing}"}, status=400)
    # Reorder: place requested ids first in given order, then any remaining
    reordered = [by_id[qid] for qid in order if qid in by_id]
    remaining = [item for item in slot._queue if item["id"] not in set(order)]
    slot._queue[:] = reordered + remaining
    # Reorder the queued messages in the messages list to match
    queued_msgs = [m for m in slot.messages if m.get("role") == "queued"]
    other_msgs = [m for m in slot.messages if m.get("role") != "queued"]
    queued_by_id: dict[str | None, dict] = {}
    for m in queued_msgs:
        try:
            cls = json.loads(m.get("cls", "{}"))
            queued_by_id[cls.get("queue_id")] = m
        except (json.JSONDecodeError, TypeError):
            pass
    reordered_msgs = [queued_by_id[qid] for qid in order if qid in queued_by_id]
    remaining_msgs = [m for m in queued_msgs if m not in reordered_msgs]
    slot.messages[:] = other_msgs + reordered_msgs + remaining_msgs
    slot.invalidate_source_links()
    state.broadcast_ws(
        "queue_reorder", {"slot": name, "order": [item["id"] for item in slot._queue]}
    )
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_reorder",
        tool_kind="permission",
        outcome="allowed",
        metadata={"slot": name, "order_len": len(order)},
    )
    return web.json_response({"ok": True})


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


async def _record_explicit_agent_selection(
    session_key: str,
    agent_name: str | None,
    bindings: ResolvedBindings,
    *,
    config: KiroCrewConfig,
    memory_mode: str = "persistent",
    app: str = "",
) -> SelectionChange | None:
    """Capture the admitted choice and drain publication before cancellation."""
    from kiro_crew.execution_context import (
        ExecutionContext,
        MemoryStoreRef,
        resolve_member_execution,
    )

    selected = agent_name or bindings.resolved_alias
    if bindings.selection_kind == "member":
        bindings.execution_context = resolve_member_execution(
            config, selected, memory_mode=memory_mode, app=app, validate_memory_files=False
        )
    else:
        bindings.execution_context = ExecutionContext(
            None,
            MemoryStoreRef(bindings.memory_store_name or "default"),
            "template",
            bindings.kiro_agent,
            memory_mode,
            app=app,
            selection_name=selected,
        )
    writer = asyncio.create_task(
        asyncio.to_thread(
            record_agent_selection,
            session_key,
            agent_name,
            bindings,
            replace=True,
            memory_mode=memory_mode,
        )
    )
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            change = await asyncio.shield(writer)
            break
        except asyncio.CancelledError as exc:
            if writer.cancelled():
                raise
            cancelled = exc
        except Exception:
            if cancelled is not None:
                raise cancelled from None
            raise
    if cancelled is not None:
        await drained_to_thread(restore_agent_selection, session_key, change)
        raise cancelled
    return change


class _CommitToken(str):
    """A ``str`` whose per-request IDENTITY marks commit ownership.

    ``api_chat_slot_agent`` commits ``slot.agent`` (and the derived
    ``slot.workspace`` / ``slot.project`` / ``slot.memory_store``) before its
    awaits and may have to roll those commits back (session rebound, busy
    decline). Every one of
    those fields has unlocked writers (openai_compat, members, the in-turn
    /agent and set_project directives), so the rollback must not fire when
    one of them wrote during the awaits — including a write of the SAME text,
    which a value compare-and-set cannot distinguish from this handler's own
    commit (the in-turn set_project directive can legitimately write the very
    project this handler derived). A subclass instance compares, hashes,
    serializes and persists exactly like the plain string, but is a distinct
    object per commit: ``slot.<field> is <token>`` is therefore a sound
    "still my write" test with no cooperation needed from the other writers.
    """

    __slots__ = ()


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


def _slot_replaced_while_queued(
    state: DashboardState,
    slot: _ChatSlot,
    name: str,
    request: web.Request | Mapping[str, Any],
    operation: str,
) -> bool:
    """Whether ``name`` registers a different object than *slot* -- checked after a lock await.

    Every switch handler (and reload) reads ``state._slots.get(name)`` before
    its first await, then queues on ``slot._lock``, the session-keyed switch
    lock and, on the model paths, ``slot._model_pick_lock``. Slot removal and
    same-name re-registration take NONE of those locks (a client reconnecting,
    or a different app claiming the name), so by the time a queued request
    resumes, ``name`` can belong to a different slot object. Everything the
    handler does next -- the app-isolation check, the busy probe, the reset --
    reads the STALE object, and an unlinked replacement resolves to the very
    same ``dashboard:<name>`` session key, so the stale request's authorization
    lands its teardown on the replacement's session. Same cross-slot-identity
    gap ``chat_tags.py`` and ``_reauthorize_after_await`` close with this exact
    ``is not slot`` test; reload and its five switch siblings share it here.

    Call it immediately after EVERY lock-acquisition await and before any
    read of ``slot`` that feeds an authorization or a teardown -- not once at
    the end, because each await is its own window. A mismatch is audited as an
    ``api_access`` denial (an app caller's under ``app_isolation``, a dashboard
    caller's like the tags handler's) and the caller answers the same 404 a
    missing slot gets, so a denial cannot be told from a name that never
    existed (``slot_ownership.slot_not_found``).
    """
    if state._slots.get(name) is slot:
        return False
    request_app = request.get("app", "")
    sel().log_api_access(
        caller=request_app or "dashboard",
        operation=operation,
        outcome="denied",
        source="app_isolation" if request_app else "dashboard",
        resources=f"slot={name}",
        error="slot was replaced while the request queued on the switch locks",
    )
    return True


def _switch_target_busy(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    provider: object,
    *,
    own_turn: bool = False,
) -> bool:
    """Whether a turn is in flight on *session_key* -- the switch handlers' pre-commit refusal.

    Every switch handler that resets the live session (agent, model, bulk
    model, reasoning effort, workspace) refuses BEFORE it commits so nothing
    needs rolling back. The refusal reads three signals, and each one sees a
    window the others miss:

    * ``slot.running`` -- set at dispatch, BEFORE the multi-second
      ``provider.start()`` registers a session, so a cold-starting first
      turn dispatched through THIS slot is visible here and nowhere else.
    * ``provider.has_active_turn()`` -- the registered provider's own view,
      which also sees a channel-linked turn that runs under the shared key
      without ever setting this slot's ``task``. *provider* is whatever
      ``state.sessions.get_provider(session_key)`` answered the caller just
      before this call (the caller reads it once and may need it afterwards).
      ``isinstance``, not a None check: the base class documents that
      caller-side guards defend against test doubles that are not
      ``LLMProvider`` instances, and the base default is False so no real
      provider is missed.
    * every OTHER running slot's turn key -- ``_cancel_target`` of each
      slot whose ``task`` is live: the identity its in-flight turn published
      (``_active_turn_session_key``), falling back to its routing when the
      turn has not published yet; compared after ``canonical_key`` so a
      legacy bare Slack key and its ``slack:`` sibling name one session. Two alias slots can drive ONE session (a
      channel-linked slot and its dashboard twin), and a switch issued
      through alias A while alias B is cold-starting sees neither of the
      first two signals: A's ``running`` is False and B's provider is not
      registered yet. Without this scan the switch commits, resets nothing,
      and reports success while the session comes up on B's captured (old)
      bindings -- the header advertises one agent/model/workspace and the
      live process runs another. The TURN key, not the routing: a rebind
      landing on B mid-turn (a cron injection takes no ``running`` gate)
      moves B's routing off the shared session while its turn still runs
      there, and a scan of routings would drop B from the set exactly when
      its turn is what the reset would tear down.

    Call it INSIDE the switch locks, with the *session_key* resolved there
    (the value the probe and the reset act on). Callers keep the atomic
    ``skip_if_busy`` decline in ``SessionManager.reset`` as the backstop for
    a turn that starts after this read: message dispatch takes none of the
    switch locks, so this is a fast path, not the authority.

    *own_turn* is the in-turn ``/agent <name>`` command (``switch_slot_agent``
    called from the slot's own turn, before it acquires a session): that turn
    is the switch itself and never prompts the provider, so its own
    ``running`` flag is not a turn in flight. The provider probe and every
    OTHER slot's turn still refuse.
    """
    if slot.running and not own_turn:
        return True
    if isinstance(provider, LLMProvider) and provider.has_active_turn():
        return True
    # list(): the scan is read-only and message dispatch may register a slot
    # while it runs (the same snapshot every other slot-table scan takes).
    # Compared in CANONICAL spelling: a slot restored from an old transcript
    # can carry the bare legacy Slack ``thread_ts`` while its sibling carries
    # ``slack:<thread_ts>``; SessionManager folds both onto one session, so
    # the comparison must too (``slot_switch_session_lock`` keys the same way).
    target = canonical_key(session_key)
    return any(
        other.running
        and not (own_turn and other is slot)
        and canonical_key(_cancel_target(other)) == target
        for other in list(state._slots.values())
    )


class _MemberMemoryRequiresNewConversation(ValueError):
    """A member pick that would rebind an existing conversation's memory.

    Subclasses ValueError so every existing catch still treats it as the
    selection failure it is; named so the handler can answer it as the
    conversation-boundary refusal it means, instead of wrapping it in the
    store-unavailable 503 the generic except produces.
    """


class SlotAgentSwitchCaller(NamedTuple):
    """Who asked :func:`switch_slot_agent` to switch: an HTTP request, or a turn.

    The HTTP route passes its request (:meth:`for_request`); the chat runner's
    in-turn ``/agent <name>`` command passes :meth:`for_turn`. Everything the
    switch reads about its caller is here, so the two doors run ONE
    transaction instead of two copies that drift.
    """

    #: The app the caller acts for ("" = the dashboard).
    app: str
    #: Peer address, for the audit trail.
    remote: str
    #: Whether the dashboard owner asked. A turn's command never claims it: its
    #: text can come from any allowed channel user, so it gets the permitted-user
    #: rules (templates and V1 stores, never a V2 member store).
    owner: bool
    #: The HTTP request, when there is one.
    request: web.Request | None = None
    #: The switch runs inside the slot's own turn (see ``_switch_target_busy``).
    own_turn: bool = False

    @classmethod
    def for_request(cls, request: web.Request) -> SlotAgentSwitchCaller:
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        return cls(
            app=str(request.get("app", "") or ""),
            remote=request.remote or "",
            owner=is_owner_dashboard_request(request),
            request=request,
        )

    @classmethod
    def for_turn(cls, slot: _ChatSlot) -> SlotAgentSwitchCaller:
        # The slot's own app scope: an app's turn is held to what that app's
        # own request could do, never widened to the dashboard's.
        return cls(app=slot._app or "", remote="", owner=False, own_turn=True)

    @property
    def scope(self) -> web.Request | Mapping[str, Any]:
        """What the app-isolation helpers read (they take ``request.get("app")``)."""
        return self.request if self.request is not None else {"app": self.app}

    async def require_owner(self, operation: str) -> web.Response | None:
        if self.request is not None:
            from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

            return await require_owner_dashboard_request(self.request, operation)
        if self.owner:
            return None
        sel().log_api_access(
            caller=self.app or "turn",
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="non_owner_block",
        )
        return web.json_response(
            {"error": "owner authorization required", "code": "owner_only"}, status=403
        )


async def api_chat_slot_agent(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/agent — set agent for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_agent")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # `announce`: the composer's `/agent <name>` asks for the transcript line
    # the in-turn command appends; the picker sends no flag and appends none.
    announce = body.get("announce", False)
    if not isinstance(announce, bool):
        return web.json_response(
            {"error": "announce must be a boolean", "code": "invalid_announce"}, status=400
        )
    resp = await switch_slot_agent(
        state,
        slot,
        name,
        body.get("agent", ""),
        body.get("agent_kind", ""),
        SlotAgentSwitchCaller.for_request(request),
    )
    if announce:
        ok, outcome = agent_switch_outcome(resp)
        if ok:
            text = switch_announcement(
                str(outcome.get("agent") or slot.agent), str(outcome.get("warning") or "")
            )
            text, _ = redact_credentials(text)
            text, _ = redact_exfiltration_urls(text)
            slot.append("assistant", text, "msg msg-a")
            state.push_slots_update()
    return resp


def agent_switch_outcome(resp: web.Response) -> tuple[bool, dict[str, Any]]:
    """Whether a :func:`switch_slot_agent` response committed, and its JSON body.

    The body is ``{}`` when it is not a JSON object, so a caller reads
    ``error`` / ``agent`` / ``warning`` from it without its own guards.
    """
    try:
        body = json.loads(resp.text or "{}")
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    return resp.status == 200 and bool(body.get("ok")), body


async def switch_slot_agent(
    state: DashboardState,
    slot: _ChatSlot,
    name: str,
    agent_name: Any,
    agent_kind: Any,
    caller: SlotAgentSwitchCaller,
) -> web.Response:
    """Switch *slot* (registered as *name*) to *agent_name*: resolve, reset, commit.

    The transaction behind ``POST /api/chat/slots/{slot}/agent`` and the
    in-turn ``/agent <name>`` command. Answers the route's own responses, so a
    non-HTTP caller reads ``status`` and the JSON ``error``/``code`` body.
    """
    if slot.mode == members_mod.DM_SLOT_MODE and not members_mod.is_dispatchable_member_name(
        slot.agent
    ):
        sel().log_api_access(
            caller=caller.remote,
            operation="chat.slot_agent",
            outcome="denied",
            source="member_pin",
            resources=f"slot={slot.key}",
            error="stored member pin is not dispatchable",
        )
        return web.json_response(
            {
                "error": "this thread's crew name cannot be dispatched",
                "code": "member_pin_mismatch",
            },
            status=409,
        )
    member_pin_match = members_mod.member_pin_matches(slot.mode, slot.agent, agent_name)
    if not isinstance(agent_name, str) or (
        agent_name
        and not is_registered_agent_name(agent_name)
        and not member_pin_match
        # Same widening as the send guard: a configured free-form member (the
        # catalog lists it, the agent cycle sends its bare name) is admissible on
        # an ordinary slot; anything else off-grammar is refused.
        and not await asyncio.to_thread(members_mod.is_configured_dispatchable_member, agent_name)
    ):
        return web.json_response({"error": "invalid agent name"}, status=400)
    # Same contract as the create route: an optional namespace for the name.
    if agent_kind not in ("", "member", "template"):
        return web.json_response(
            {"error": "invalid agent kind", "code": "invalid_agent_kind"}, status=400
        )
    if slot.mode == "member" and (agent_name != slot.agent or agent_kind == "template"):
        # Member DM threads are pinned to their crew: refuse the switch before
        # any state is touched. A same-name "switch" stays allowed — it is a
        # session reset, not a re-bind — but only in the MEMBER namespace: the
        # same name picked as a template would run the shared template and
        # detach the thread from the member's memory, which is a re-bind by
        # another spelling. Audited like every other pin denial
        # (the send path's guard emits the same event), so a probe against the
        # pin is visible in the SEL trail.
        _emit_agent_assignment(slot.key, agent_name, outcome="denied_member_pin")
        return web.json_response(
            {"error": "member thread agent is pinned", "code": "member_thread_agent_pinned"},
            status=409,
        )
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal

    # The whole resolve -> reset -> commit section runs under the slot's
    # lock: the awaits yield the event loop, and an interleaved second switch
    # could otherwise observe (or write) intermediate state. TRANSACTIONAL
    # ordering: the new values are computed into locals, the session reset
    # runs FIRST, and the slot is mutated only after the reset succeeds — a
    # failed request provably changed nothing, the invariant the frontend's
    # slotSwitch failure-recovery relies on, with no rollback machinery to
    # race against concurrent writers (e.g. the project endpoint, which does
    # not take this lock).
    # Two locks, in the order documented at _slot_switch_session_lock:
    # slot._lock, then the session lock. An ExitStack because the session
    # lock's KEY is only known after the in-lock read below, and locking on
    # any earlier read could leave this holding the wrong session lock.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above (see _slot_replaced_while_queued):
        # ``name`` can be recreated for a different app while this request
        # queued, and every read of ``slot`` below would be of the stale one.
        if _slot_replaced_while_queued(state, slot, name, caller.scope, "chat.slot_agent"):
            return slot_not_found()
        # The session the switch resets — ``effective_session_key``, never
        # ``_history_key_for`` (see api_chat_slot_model): a channel- or
        # cron-born slot runs its turns under its linked key, and the
        # dashboard-prefixed spelling names a session that never existed —
        # the reset would "succeed" against nothing while the live process
        # kept the old agent. Resolved INSIDE the lock: the binding can land
        # while this request waits on it.
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot on this same session.
        # slot._lock is created per _ChatSlot and so is DISJOINT across
        # aliases. Keyed on the value resolved just above -- the same one the
        # probe and reset below use -- so the lock provably guards them even if
        # a binding landed while this request waited on slot._lock (see
        # _slot_switch_session_lock).
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        # Second lock-acquisition await, second re-check: a same-name
        # recreate lands during this wait just as easily as during the first.
        if _slot_replaced_while_queued(state, slot, name, caller.scope, "chat.slot_agent"):
            return slot_not_found()
        # App isolation on the SESSION, not just the slot (the cancel routes'
        # policy): slot ownership does not imply ownership of a linked
        # channel session, so an app caller may not switch the agent a
        # channel thread runs on. Denied as an indistinguishable 404.
        denied = _app_cancel_denied(caller.scope, slot, "chat.slot_agent", session_key)
        if denied is not None:
            return denied
        owner_request = caller.owner
        if not owner_request:
            # Permitted chat users keep template and legacy V1 choices, but
            # cannot change a member assignment through aggregate controls. Refuse a V2 choice
            # before any slot, provider or history mutation.
            try:
                choice_cfg = await asyncio.to_thread(KiroCrewConfig.load)
                await warm_project_agent_names(
                    slot.project or None, operation="api_chat_slot_agent", source="dashboard"
                )
                choice = await asyncio.to_thread(
                    resolve_agent_bindings,
                    choice_cfg,
                    agent_name,
                    slot.project or None,
                    validate_memory_files=False,
                    selection_kind=agent_kind,
                )
            except Exception as exc:
                from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                return _store_unavailable_response(slot.memory_store, exc)
            choice_store = choice_cfg.memory_stores.get(choice.memory_store_name)
            if choice_store is not None and choice_store.memory_version == 2:
                denied = await caller.require_owner("chat.slot_agent")
                if denied is not None:
                    return denied
        if agent_name != slot.agent:
            from kiro_crew.execution_context import read_session_execution

            try:
                prior_execution = await asyncio.to_thread(read_session_execution, session_key)
            except (OSError, ValueError):
                return web.json_response(
                    {
                        "error": "This conversation's memory binding could not be read. "
                        "Start a new conversation to choose a different member.",
                        "code": "member_binding_unavailable",
                    },
                    status=503,
                )
            if prior_execution is not None and prior_execution.member_id is not None:
                # Resetting the provider keeps this conversation's member identity.
                # Refuse before changing the agent, its derived fields or history.
                return web.json_response(
                    {
                        "error": "This conversation belongs to its original member. "
                        "Start a new conversation to choose a different member.",
                        "code": "member_session_pinned",
                    },
                    status=409,
                )
        try:
            prior_selection = await asyncio.to_thread(session_agent_selection_name, session_key)
        except Exception as exc:
            from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

            return _store_unavailable_response(slot.memory_store, exc)
        # Never reset under an in-flight turn (the model handler's policy,
        # and the _cancel_target subtlety): a RUNNING turn owns a captured
        # identity because ``linked_session_key`` is mutable, so the key
        # resolved above may not be the turn's — tearing it down would kill
        # the wrong session (or the streaming turn itself). The three signals
        # and why each is needed live on _switch_target_busy. A 409 is
        # retryable once the turn completes. Checked BEFORE the commit below,
        # so nothing needs rolling back.
        busy_provider = state.sessions.get_provider(session_key)
        if _switch_target_busy(state, slot, session_key, busy_provider, own_turn=caller.own_turn):
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        # Rollback baseline for the session_rebound path below. The commit is
        # otherwise deliberately not rolled back on a failing reset (see the
        # teardown_incomplete comment), so this is the ONE case that unwinds.
        prior_agent = slot.agent
        prior_agent_kind = slot.agent_kind
        # Stored verbatim — never rewritten to whatever currently answers. See
        # the same reasoning in api_chat_slot_create.
        new_workspace = slot.workspace
        new_project = slot.project
        new_memory_store = slot.memory_store
        # Compare-and-set baseline, captured BEFORE the first await in this
        # section: the resolution warm-up and the session reset both yield
        # the event loop, and the project/workspace endpoints do not take
        # this lock — a user's explicit pick landing anywhere in that window
        # must win over this switch's DERIVED values (the reverse would
        # silently erase an action that happened after the agent pick).
        pre_await_workspace = slot.workspace
        pre_await_project = slot.project
        pre_await_memory_store = slot.memory_store

        # Resolve workspace from agent bindings. The response value is seeded
        # from the slot's CURRENT workspace, not a "default" literal: if
        # resolution below fails, the response still names this value, and
        # the acting tab writes it into its store — a fabricated
        # "default" would pin the chip to a workspace the slot does not hold
        # (the websocket rebroadcast corrects it only when the socket is up,
        # which is exactly when the optimistic write is load-bearing).
        workspace = slot.workspace or "default"
        assignment_resolved = False
        new_agent_kind = ""
        committed_agent: str | None = None
        committed_agent_kind: str | None = None
        try:
            cfg = KiroCrewConfig.load()
            # Resolve by the name being STORED, which is exactly the name dispatch
            # will resolve later (`chat_runner` -> resolve_agent_bindings(
            # slot.agent)). Looking it up as an alias first and taking THAT
            # alias's workspace disagrees with dispatch whenever the two differ:
            # a name that is merely some alias's `kiro_agent` target, or a
            # materialized app agent, dispatches with the DEFAULT bindings while
            # the slot records the alias's workspace. A materialized agent
            # matches no alias at all, which leaves the slot on the PREVIOUS
            # agent's project.
            # Resolve WITH the captured project scope (warmed off-loop first) so a
            # project agent counts as resolved rather than falling back.
            await warm_project_agent_names(
                pre_await_project or None, operation="api_chat_slot_agent", source="dashboard"
            )
            if owner_request:
                bindings = await asyncio.to_thread(
                    resolve_agent_bindings,
                    cfg,
                    agent_name,
                    pre_await_project or None,
                    validate_memory_files=False,
                    selection_kind=agent_kind,
                )
            else:
                bindings = await asyncio.to_thread(
                    resolve_agent_bindings,
                    cfg,
                    agent_name,
                    pre_await_project or None,
                    selection_kind=choice.selection_kind,
                    validate_memory_files=False,
                )
                selected_store = cfg.memory_stores.get(bindings.memory_store_name)
                if selected_store is not None and selected_store.memory_version == 2:
                    # The member may have moved to V2 during resolution. No
                    # derived fields, reset or history write has committed yet.
                    denied = await caller.require_owner("chat.slot_agent")
                    if denied is not None:
                        return denied
            assignment_resolved = bindings.requested_resolved
            new_agent_kind = bindings.selection_kind if assignment_resolved else ""
            ws_name = _workspace_name_for_dir(cfg, bindings.workspace_dir)
            new_workspace = ws_name
            workspace = ws_name
            new_memory_store = bindings.memory_store_name
            # A project-scope agent exists only inside slot.project: kiro-cli
            # resolves --agent against $PWD/.kiro/agents, so resetting the
            # project here would make the very agent just selected unresolvable
            # on the next turn (slot advertises it, default answers — the
            # silent substitution this resolution exists to remove). Aliases keep the
            # reset: their project comes from their own workspace bindings.
            is_project_agent = agent_name not in cfg.agents and agent_name in (
                cached_project_agent_names(slot.project or None) or frozenset()
            )
            if not is_project_agent:
                # A slot filed into a project-linked folder keeps that
                # folder's directory rather than the new agent's workspace
                # default: the link is an explicit choice about where this
                # chat's tools run, and `api_chat_slot_create` already
                # prefers it over the workspace default — an agent pick must
                # not silently undo it. Resolved through the SAME helper as
                # the create path, which walks the parent_id chain (so a
                # project inherited from an ancestor folder counts too) and
                # RE-VALIDATES the stored path instead of trusting
                # folders.json: a directory recorded there can since have
                # been moved, or become sensitive, and this value becomes
                # the agent subprocess's cwd. Off the loop, as the helper's
                # docstring requires (realpath/isdir priming).
                folder_project = ""
                if slot.folder_id:
                    try:
                        # REVALIDATED against the id the snapshot was taken
                        # for. `read_folders` and the off-loop resolve are two
                        # awaits, and a concurrent assignment can file this
                        # slot into a folder CREATED after the snapshot -- whose
                        # id is then absent from it, resolving to nothing and
                        # committing the workspace default as this chat's
                        # directory. One retry is enough for an assignment that
                        # has already landed; a slot being reassigned faster
                        # than that has no stable answer to commit, so it keeps
                        # the documented fall-through.
                        folder_error: str | None = ""
                        for _ in range(2):
                            folder_id_at_read = slot.folder_id
                            if not folder_id_at_read:
                                break
                            folder_snapshot = await state.read_folders(
                                lambda folders: [dict(folder) for folder in folders]
                            )
                            folder_project, folder_error = (
                                await resolve_folder_project_dir_off_loop(
                                    folder_snapshot, folder_id_at_read
                                )
                            )
                            if slot.folder_id == folder_id_at_read:
                                break
                            # Reassigned mid-resolve: what came back describes a
                            # folder other than the one this slot now holds, so
                            # it is discarded rather than committed.
                            folder_project, folder_error = "", ""
                        if folder_error:
                            # Deliberately NOT the create path's 400: that
                            # validator also rejects a directory that no
                            # longer exists, so failing the request here
                            # would make the agent permanently unswitchable
                            # for any folder whose project was moved or
                            # deleted. Fall through to the workspace default.
                            logger.warning(
                                "Slot %s folder project unusable (%s); "
                                "falling back to the workspace default",
                                name,
                                folder_error,
                            )
                            folder_project = ""
                    except Exception:
                        # Same fall-through for an unreadable or corrupt
                        # folder store: letting it reach the outer handler
                        # would leave the workspace advanced with the
                        # project stale — a half-applied switch.
                        logger.warning(
                            "Failed to resolve folder project for slot %s", name, exc_info=True
                        )
                        folder_project = ""
                if folder_project:
                    new_project = folder_project
                elif ws_name not in ("default", cfg.default_workspace):
                    # Only a workspace the agent RESOLVED TO DELIBERATELY may
                    # retarget the project. Two names fail that test and both
                    # have to be excluded:
                    #
                    # * the literal "default" — `_workspace_name_for_dir`
                    #   answers it both for an agent bound to no workspace and
                    #   for one naming a workspace absent from the config;
                    # * `cfg.default_workspace` — on an install that renames
                    #   its default, the resolver falls back to that NAME, so
                    #   the same "no deliberate choice" case arrives spelled
                    #   differently and a literal-only gate lets it through.
                    #
                    # Either way the agent expressed no workspace preference,
                    # and retargeting on a fallback discards the directory the
                    # user chose and runs the next turn's tools elsewhere.
                    new_project = default_project_dir(workspace)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to resolve agent bindings for %r", agent_name, exc_info=True)

        if agent_kind and not assignment_resolved:
            # A stated namespace never falls back to whoever answers by default.
            return web.json_response(
                {
                    "error": "the selected agent choice is not available",
                    "code": "agent_choice_unavailable",
                },
                status=409,
            )

        if not assignment_resolved and prior_selection is not None:
            # A failed lookup cannot commit a name while retaining a different
            # protected selection. Leave the established conversation usable.
            from kiro_crew.dashboard.handlers.memory import _store_unavailable_response
            from kiro_crew.memory_stores import UnknownMemoryStore

            return _store_unavailable_response(
                slot.memory_store, UnknownMemoryStore("Conversation agent selection is unavailable")
            )

        # Publish the selected name and its resolved namespace together in one
        # no-await section. Resolution may yield while discovering project
        # agents, so publishing the name before it finishes lets a sender see a
        # new name beside the previous namespace. A sender during resolution
        # instead keeps using the complete old selection; the busy re-probe and
        # reset below either refuse that overlap or retire its old session.
        #
        # The two tokens also form one rollback ownership claim. Any concurrent
        # writer replaces at least the field it owns, including a same-value
        # write, so rollback never erases that later selection.
        #
        # Compare-and-set against the pre-resolution baseline BEFORE the commit,
        # the same guard the derived fields below apply to their pre_await_*
        # snapshots. The resolution awaits above yield the event loop, and the
        # unlocked `/v1/chat` openai_compat path (and the in-turn directive
        # writers) can set `slot.agent`/`slot.agent_kind` and dispatch a turn in
        # that window. Committing the tokens here would overwrite that accepted
        # selection, and because no await separates the commit from the
        # `slot.agent is not committed_agent` rebind check below, that check can
        # never see the overwrite — the busy re-probe would then roll back to
        # `prior_agent`, erasing a binding a running turn already chose. Refuse
        # instead, leaving the concurrent selection in place (the pre-move
        # behaviour, which committed before resolving and so returned 409 here).
        if slot.agent is not prior_agent or slot.agent_kind is not prior_agent_kind:
            return web.json_response(
                {"error": "slot changed during agent resolution", "code": "session_rebound"},
                status=409,
            )
        slot.agent = _CommitToken(agent_name)
        slot.agent_kind = _CommitToken(new_agent_kind)
        committed_agent = slot.agent
        committed_agent_kind = slot.agent_kind

        # Derived fields commit BEFORE the reset too, compare-and-set against
        # the pre-await baseline: a send landing during the reset teardown
        # cold-starts the replacement session from the slot's CURRENT
        # bindings, so the full new binding TRIPLE must already be visible or
        # the new agent's session starts in the OLD project and its tools run
        # in the wrong repository. The write-side CAS still protects a
        # concurrent explicit pick that landed during the resolution awaits
        # above; the committed values are identity tokens so the ROLLBACK can
        # prove ownership — a value compare there would erase a concurrent
        # same-value write (the in-turn set_project directive can write the
        # very project this handler derived).
        committed_workspace: str | None = None
        committed_project: str | None = None
        committed_memory_store: str | None = None
        if slot.workspace == pre_await_workspace:
            slot.workspace = _CommitToken(new_workspace)
            committed_workspace = slot.workspace
        if slot.project == pre_await_project:
            slot.project = _CommitToken(new_project)
            committed_project = slot.project
        # The store is the THIRD field of that binding, and leaving it behind
        # splits the slot in half: the turn resolves its store fresh from the new
        # agent's bindings while the consolidator writes to the store recorded at
        # birth, so a switched slot READS the new agent's memory and WRITES the old
        # agent's. No error on either side. Same commit-token CAS as the two
        # above, so the rollback below unwinds the store with the binding it
        # belongs to rather than leaving the slot half-switched.
        if slot.memory_store == pre_await_memory_store:
            slot.memory_store = _CommitToken(new_memory_store)
            committed_memory_store = slot.memory_store

        # Reset session so the next message uses the new agent.
        logger.info(
            "Slot %s agent switched to %r, resetting session", name, agent_name or "kirocrew"
        )

        def _rollback_switch() -> None:
            """Unwind this request's commit — only the values still OURS.

            EVERY field is unwound on IDENTITY of its commit token, never
            value equality: unlocked writers (the in-turn /agent and
            set_project directives in chat_runner, members, openai_compat)
            can write the SAME text during this handler's awaits — the
            in-turn set_project directive can legitimately write the very
            project this handler derived — and a value compare-and-set would
            erase that successful concurrent write. Any write replaces the
            token object, so an identity match proves the field is still
            this commit's; a field this request never committed (the
            write-side CAS lost) has a None token and is never touched.
            """
            if slot.agent is committed_agent and slot.agent_kind is committed_agent_kind:
                slot.agent = prior_agent
                slot.agent_kind = prior_agent_kind
            if committed_workspace is not None and slot.workspace is committed_workspace:
                slot.workspace = pre_await_workspace
            if committed_project is not None and slot.project is committed_project:
                slot.project = pre_await_project
            if committed_memory_store is not None and slot.memory_store is committed_memory_store:
                slot.memory_store = pre_await_memory_store
            # Re-mark unconditionally: the periodic flush writes a slot's
            # metadata line only while _dirty is set, so without this a
            # rollback that follows a persisted provisional binding leaves
            # the rejected values on disk across a restart.
            slot._dirty = True

        if (
            state._slots.get(slot.key) is not slot
            or effective_session_key(slot) != session_key
            or slot.agent is not committed_agent
        ):
            _rollback_switch()
            return web.json_response(
                {"error": "slot changed during agent resolution", "code": "session_rebound"},
                status=409,
            )

        # Children guard, shared with reload/model: the reset tears down the
        # runtime attached sub-agents run on, so a parent that is idle but
        # still has children must refuse rather than discard their work.
        children_409 = await _subagents_attached_response(state, slot, session_key, "slot_agent")
        if children_409 is not None:
            _rollback_switch()
            return children_409
        # Last-instant re-probe in a NO-AWAIT window before the teardown (the
        # model template's rule at its own reset site): the pre-commit check
        # above is separated from this point by the resolution warm-up and
        # the children probe awaits, so a turn — a channel message on the
        # linked session, or a sibling alias cold-starting on it — may have
        # started since it ran. Same predicate as the pre-commit check
        # (_switch_target_busy), so the sibling window it closes there is
        # closed here too. Message dispatch does not take slot._lock, so this
        # fast path plus the atomic skip_if_busy decline below are what keep
        # the teardown off a streaming turn.
        recheck = state.sessions.get_provider(session_key)
        if _switch_target_busy(state, slot, session_key, recheck, own_turn=caller.own_turn):
            _rollback_switch()
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        teardown_incomplete = False
        reset_ok = True
        # The switch is COMMITTED already (slot.agent above), so a POST-POP
        # teardown raise is answered as a success with a degraded-teardown
        # warning — but only once the helper has verified the raise came
        # AFTER the session pop. A PRE-POP raise means the old session
        # survives on the old binding, so the helper propagates it and this
        # request rolls the commit back and answers 500 instead of a false
        # success. The helper resets with skip_if_busy=True, which keeps this
        # handler's decline ladder
        # below: SessionManager.reset evaluates busyness atomically with the
        # session pop, so a turn that slipped into the microsecond residue
        # after the re-check above is declined (reset_ok False) instead of
        # torn down mid-stream. The helper's None verdict marks the degraded
        # post-pop teardown; a bool verdict is the reset outcome the ladder
        # reads.
        try:
            reset_verdict = await _reset_slot_session_or_warn(
                state, slot, session_key, switch_kind="agent"
            )
        except Exception:
            # The identity probe proved the pop never happened: the old
            # session is still alive on the old binding, so the committed
            # values describe a switch that did not take — and the acting tab
            # keeps its OLD store value on the 500, so leaving them would
            # split server state from every client. Roll back the commit
            # (identity-scoped, so a concurrent explicit pick that landed
            # during the raising await keeps its win) and re-push so clients
            # and persisted state land on the rolled-back truth, then let the
            # raise escape as a 500.
            _rollback_switch()
            state.push_slots_update()
            raise
        if reset_verdict is None:
            teardown_incomplete = True
        else:
            reset_ok = reset_verdict
        if not reset_ok and not teardown_incomplete:
            # Disambiguate the decline FAIL-CLOSED, the workspace handler's
            # template: a live provider mid-turn → roll back and 409; a live
            # IDLE session that declined (its turn ended before this re-read)
            # is always safe to tear down, so retry once; a second decline
            # means another turn is genuinely racing. No live provider means
            # there was nothing to tear down — the next message cold-starts
            # under the new binding, which is what the reset would arrange.
            busy_provider = state.sessions.get_provider(session_key)
            if isinstance(busy_provider, LLMProvider):
                if busy_provider.has_active_turn():
                    _rollback_switch()
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                    )
                # Retry through the same probing helper: a pre-pop raise on
                # the retry propagates (roll back + 500) exactly as the first
                # attempt, and a post-pop raise becomes the committed 200 +
                # warning. Left as a bare _reset_slot_session the retry would
                # answer a false success on a pre-pop raise, the divergence
                # the first-attempt guard prevents.
                try:
                    reset_verdict = await _reset_slot_session_or_warn(
                        state, slot, session_key, switch_kind="agent"
                    )
                except Exception:
                    _rollback_switch()
                    state.push_slots_update()
                    raise
                if reset_verdict is None:
                    teardown_incomplete = True
                else:
                    reset_ok = reset_verdict
                if (
                    not reset_ok
                    and not teardown_incomplete
                    and state.sessions.get_provider(session_key) is not None
                ):
                    _rollback_switch()
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                    )

        if effective_session_key(slot) != session_key:
            # The slot was bound to a different session while the resolution
            # warm-up or the reset awaited (a cron/workflow slot gets linked
            # when its first result is injected): the session this request
            # tore down is no longer the slot's, so the committed binding
            # would describe a session that never saw the switch. The
            # teardown itself was harmless (that session was idle and no
            # longer bound); roll back the commit and answer the same 409 the
            # model and workspace handlers use. Checked BEFORE the metadata
            # write below so a rolled-back agent is never persisted for
            # restart.
            _rollback_switch()
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )

        # Persist the new agent so the session resumes under the correct
        # agent after a gateway restart. INSIDE the lock: two racing switches
        # otherwise interleave their metadata writes, and a stalled earlier
        # write finishing last would restore the older agent on restart.
        # Deliberately ``_history_key_for``, NOT ``session_key``: this names
        # the slot's TRANSCRIPT (the .jsonl the restart scan reads), not the
        # live session the reset above addressed — the same history-vs-session
        # split ``_cancel_target`` documents.
        conversation_log = state.conversation_log if not slot.is_restricted else None
        if conversation_log:

            async def _rollback_history_selection() -> None:
                _rollback_switch()
                try:
                    await drained_to_thread(
                        conversation_log.update_metadata,
                        _history_key_for(name),
                        {"agent": str(slot.agent), "agent_kind": slot.agent_kind},
                    )
                except Exception:
                    logger.warning(
                        "Failed to restore agent metadata for slot %s", name, exc_info=True
                    )
                finally:
                    state.push_slots_update()

            try:
                # update_metadata enters _locked (flock + os.close); those are
                # blocking-on-loop-prohibited, so offload to a worker thread rather
                # than run them on the event loop (a wedged peer must never freeze
                # chat/WS/heartbeat).
                await drained_to_thread(
                    conversation_log.update_metadata,
                    _history_key_for(name),
                    {"agent": agent_name, "agent_kind": new_agent_kind},
                )
            except asyncio.CancelledError:
                # Drain the writer before restoring history, and retain both
                # switch locks until restoration settles.
                await _rollback_history_selection()
                raise
            except Exception:
                logger.warning("Failed to persist agent for slot %s", name, exc_info=True)
                await _rollback_history_selection()
                return web.json_response(
                    {
                        "error": "Could not save the agent selection. Try again.",
                        "code": "history_unavailable",
                    },
                    status=503,
                )

        if effective_session_key(slot) != session_key:
            # A binding can land during the metadata await too — the rebound
            # guard above ran BEFORE that await, so it must be re-validated
            # after the last await inside the lock or a workflow binding
            # landing there gets a 200 while the linked session keeps the old
            # agent. Roll back the commit AND the metadata just persisted:
            # the 409 tells the caller nothing changed, so the transcript
            # metadata must agree. Restoring ``slot.agent`` (post-rollback)
            # rather than ``prior_agent`` is deliberate — if a concurrent
            # writer took ownership during the awaits, its value is the
            # truthful current one. The metadata is transcript-scoped and
            # binding-independent, so its restore needs no further re-check.
            _rollback_switch()
            if state.conversation_log and not slot.is_restricted:
                try:
                    await drained_to_thread(
                        state.conversation_log.update_metadata,
                        _history_key_for(name),
                        {"agent": str(slot.agent), "agent_kind": slot.agent_kind},
                    )
                except Exception:
                    logger.warning(
                        "Failed to restore agent metadata for slot %s", name, exc_info=True
                    )
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )

        # Every authorized choice must update both durable records. Only an
        # owner choice can also admit an unbound restored member.
        if slot.agent is committed_agent and assignment_resolved:
            selection_change = None
            selection_error = None

            async def _rollback_owner_selection() -> None:
                _rollback_switch()
                try:
                    await drained_to_thread(restore_agent_selection, session_key, selection_change)
                finally:
                    # The protected drain may re-raise cancellation. History
                    # must still settle before the slot/session locks release.
                    if state.conversation_log and not slot.is_restricted:
                        await drained_to_thread(
                            state.conversation_log.update_metadata,
                            _history_key_for(name),
                            {"agent": str(slot.agent), "agent_kind": slot.agent_kind},
                        )

            try:
                # Validate the old conversation BEFORE publishing its new
                # canonical identity; otherwise that publication would conceal
                # template history from the member-selection guard.
                selected_memory = cfg.memory_stores.get(bindings.memory_store_name)
                from kiro_crew.execution_context import read_session_execution

                initial_execution = await asyncio.to_thread(read_session_execution, session_key)
                changed_member = (
                    initial_execution is None
                    or initial_execution.member_id
                    != getattr(selected_memory, "owner_member_id", None)
                )
                if (
                    selected_memory is not None
                    and selected_memory.memory_version == 2
                    and changed_member
                ):
                    if slot.messages:
                        raise _MemberMemoryRequiresNewConversation(
                            "Open a new conversation to choose member memory."
                        )
                    await release_prewarmed_session(state, session_key, agent_name, cfg)
                    await pin_private_agent_store(
                        state,
                        session_key,
                        agent_name,
                        cfg,
                        memory_mode=slot.memory_mode,
                        validate_only=True,
                    )
                    if (
                        state._slots.get(slot.key) is not slot
                        or slot.agent is not committed_agent
                        or effective_session_key(slot) != session_key
                        or slot.messages
                    ):
                        raise ValueError("The conversation changed during member selection.")
                selection_change = await _record_explicit_agent_selection(
                    session_key,
                    agent_name,
                    bindings,
                    config=cfg,
                    memory_mode=slot.memory_mode,
                    app=slot._app or "",
                )
            except asyncio.CancelledError:
                await _rollback_owner_selection()
                raise
            except Exception as exc:
                selection_error = exc
            if selection_error is not None or (
                state._slots.get(slot.key) is not slot
                or slot.agent is not committed_agent
                or effective_session_key(slot) != session_key
            ):
                await _rollback_owner_selection()
                if selection_error is not None:
                    if isinstance(selection_error, _MemberMemoryRequiresNewConversation):
                        # Not a store fault: this conversation already has history,
                        # so its memory binding cannot move to a member's private
                        # store in place. Name the boundary the user can act on.
                        return web.json_response(
                            {
                                "error": str(selection_error),
                                "code": "member_memory_requires_new_conversation",
                            },
                            status=409,
                        )
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    return _store_unavailable_response(slot.memory_store, selection_error)
                return web.json_response(
                    {
                        "error": "Could not save the agent selection. Try again.",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            # A non-owner choice preserves the recorded session assignment.
            slot._memory_assignment_from_history = not owner_request

        owner_pick = slot.agent is committed_agent and assignment_resolved and caller.owner
        if owner_pick:
            slot._memory_assignment_from_history = False

        # Menu grants are for an EMPTY plain dashboard chat only. Channel,
        # cron and workflow alias tabs are excluded: an injector can set their
        # linked_session_key without the slot lock, and they carry native
        # context the pin helper refuses. With this gate, pin_key is the slot's
        # own transcript key, which nothing rebinds. The helper verifies the
        # transcript is empty before capturing member context; V1 picks pass
        # through without a grant.
        if (
            owner_pick
            and agent_name
            # A shared-template pick has no member memory to grant, even when
            # a member of the same name exists.
            and agent_kind != "template"
            and not slot.messages
            and not slot.linked_session_key
            and not slot.channel_origin
        ):
            pin_key = session_key

            async def _unwind_pin_failure() -> None:
                # A failed grant must restore the protected selection as well
                # as the slot and transcript, before either switch lock releases.
                try:
                    await _rollback_owner_selection()
                finally:
                    state.push_slots_update()

            if (
                state._slots.get(slot.key) is slot
                and slot.agent is committed_agent
                and effective_session_key(slot) == pin_key
                and not slot.messages
            ):
                try:
                    # Off the loop: the create path loads it the same way, and
                    # the in-handler load above is not guaranteed to have run.
                    pin_cfg = await asyncio.to_thread(KiroCrewConfig.load)
                    # The reset above tore this slot's session down but kept its
                    # resume pointer, and a new chat is pre-warmed while it is
                    # still on the default agent. Drop that pointer for a
                    # private pick, or the pin reads it as V1 context and
                    # refuses a chat with no messages in it. Re-checked after
                    # the awaits below for the same reason the pin is.
                    await release_prewarmed_session(state, pin_key, agent_name, pin_cfg)
                    if (
                        state._slots.get(slot.key) is not slot
                        or slot.agent is not committed_agent
                        or effective_session_key(slot) != pin_key
                        or slot.messages
                    ):
                        await _unwind_pin_failure()
                        return web.json_response(
                            {
                                "error": "slot changed during member assignment",
                                "code": "session_rebound",
                            },
                            status=409,
                        )
                    assigned_store = await pin_private_agent_store(
                        state, pin_key, agent_name, pin_cfg, memory_mode=slot.memory_mode
                    )
                except Exception as exc:
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    await _unwind_pin_failure()
                    return _store_unavailable_response(slot.memory_store, exc)
                # Agent committed: a raced message uses the new agent and confirms its grant.
                if assigned_store and (
                    state._slots.get(slot.key) is not slot or slot.agent is not committed_agent
                ):
                    # No unwind: the owner's grant on this slot's own key is valid and immutable.
                    return web.json_response(
                        {
                            "error": "slot changed during member assignment",
                            "code": "session_rebound",
                        },
                        status=409,
                    )
                if assigned_store and slot.memory_store != assigned_store:
                    slot.memory_store = assigned_store

        # Snapshot the response's workspace LAST, immediately before leaving
        # the lock: the metadata await above yields the event loop, so a
        # concurrent /workspace pick can land after the commit — the response
        # (which the acting tab writes into its store) must name the slot's
        # newest reality, not a pre-await snapshot.
        workspace = slot.workspace or "default"
    # The reset destroyed any eagerly created session; picking an agent is
    # itself a strong first-message intent signal (it also resets the
    # project), so re-arm the speculative spawn for the new bindings.
    schedule_eager_spawn(state, slot, start_priority=person_priority(caller.owner))
    state.push_slots_update()
    resp_body: dict = {
        "ok": True,
        "agent": agent_name,
        "agent_kind": slot.agent_kind,
        "workspace": workspace,
    }
    if teardown_incomplete:
        # Advisory only — the switch itself succeeded and the response
        # carries the committed state the acting tab writes optimistically.
        resp_body["warning"] = _TEARDOWN_INCOMPLETE_WARNING
    return web.json_response(resp_body)


def _model_rejected_reason(model_name: str, provider: str | None = None) -> str | None:
    """Reason to reject ``model_name`` for the active provider, or None to allow.

    The dashboard model dropdown falls back to canonical registry keys (e.g.
    ``fable-5-1m``) when /api/models is unavailable (gateway restart / kiro-cli
    cold-start timeout). Those keys are DISPLAY identifiers the ACP CLI rejects
    as model ids (-32603 "model not available") — persisting one into
    ``slot.model`` breaks the next turn. This guard is defense-in-depth behind
    the frontend's auto-only fallback: a stale client, a direct API
    call, or the openai-compat path can never persist a canonical key. ``auto``
    and ``""`` (provider default) always pass; for the ``claude_code`` provider
    canonical keys ARE the wire format, so they pass there too.

    *provider* lets a caller that has already loaded the config supply it, so
    this adds no read of its own: ``KiroCrewConfig.load()`` deep-copies the
    validated dict even on a cache hit, and on a miss it reads and validates
    files — work that must not land on the event loop under a held lock. Omit it
    and the provider is resolved here, preserving the original behaviour.
    """
    if not model_name or model_name == "auto":
        return None
    if provider is None:
        try:
            provider = KiroCrewConfig.load().agent.provider
        except Exception:  # pragma: no cover - config load is resilient
            provider = ""
    if is_claude_code(provider):
        return None
    if model_registry.is_canonical_key(model_name):
        return (
            f"{model_name!r} is a display-only model identifier the "
            f"{provider or 'active'} provider does not accept; "
            f"select a listed model or 'auto'."
        )
    return None


#: The chat picker's "Auto (Jev)" entry, as the id a client sends for it. NOT a
#: provider model id and never stored in ``slot.model``: it asks the
#: ``model.route`` decision point to pick a difficulty tier for each turn, and
#: until it answers the session runs on the backend default. The prefix is
#: ``auto`` so a client too old to know the entry (or a backend without the point)
#: reads it as the Auto it behaves like, and the colon keeps it outside the model
#: namespace -- no advertised id carries one.
JEV_ROUTE_MODEL = "auto:jev"


def _is_jev_route_pick(raw: object) -> bool:
    """Whether a request body's ``model`` is the "Auto (Jev)" sentinel.

    Exact identity on a stripped string. A near-miss spelling is NOT this entry
    and falls through to the ordinary model path, where the guard refuses it --
    resolving it loosely would turn a typo into paid third-party egress.
    """
    return isinstance(raw, str) and raw.strip() == JEV_ROUTE_MODEL


def _wire_model_id(provider: AcpProvider, model_name: str) -> str:
    """Translate a canonical model key into the id THIS backend accepts.

    ``slot.model`` holds a canonical/wire value while ``session/set_model`` only
    accepts the backend's own ids — two namespaces. Mirrors the normalisation the
    warm-pool post-claim switch does in ``SessionManager``: a backend on the
    native ``acp`` namespace wants the bare dotted id via ``to_acp_id`` (which
    translates canonical keys and passes kiro's own ids through unchanged), while
    one on its own provider namespace wants that namespace's id (for
    claude-agent-acp, ``global.anthropic.*``).

    Which namespace is asked as a CAPABILITY, not read off the harness's name:
    ``SessionCapabilities.model_id_namespace``. The same field also answers
    whether "provider default" is expressible, because that is a property of the
    namespace — the native one carries the real id ``auto`` and a provider
    namespace has no id meaning "choose for me".

    Returns "" when the change cannot be expressed as a ``set_model`` on this
    backend, which tells the caller to fall back to a session reset.
    """
    # The dashboard sends "" for Auto, but the literal "auto" also passes the
    # guard (stale clients / direct API calls), so both mean "provider default".
    is_default = model_name in ("", "auto")
    namespace = capabilities_of(provider).model_id_namespace
    if namespace != MODEL_NAMESPACE_ACP:
        # No id on this namespace means "let the server choose", so returning to
        # default needs a reset.
        return "" if is_default else model_registry.to_provider_id(model_name, namespace)
    if is_default:
        # kiro DOES express Auto as a real model id — but only switch to it when
        # this session's backend actually advertised it.
        advertised = {m.get("modelId", "") for m in provider.available_models()}
        return "auto" if "auto" in advertised else ""
    return model_registry.to_acp_id(model_name)


async def _reapply_effort_after_live_switch(
    name: str, slot: _ChatSlot, provider: AcpProvider
) -> bool:
    """Re-apply the slot's reasoning effort to the model we just switched to.

    The kiro effort overlay is written before every (re)spawn, so a cold start
    picks the level up for free. An in-place switch never respawns, so without
    this the new model would run at its own default while the UI still reports
    the slot's level. Pushes it live through the same provider calls
    ``api_chat_slot_reasoning_effort`` uses.

    Returns False to ask the caller for a reset, which re-applies effort through
    the provider factory instead.
    """
    try:
        # Through the class: the shared body the provider's own set_model runs.
        return await AcpProvider.reapply_live_effort(provider, slot.reasoning_effort)
    except Exception as exc:
        logger.warning(
            "Effort re-apply after live model switch failed for slot %s: %s: %s"
            " — falling back to reset",
            name,
            type(exc).__name__,
            exc,
        )
        return False


async def _try_live_model_switch(
    name: str, slot: _ChatSlot, provider: LLMProvider | None, model_name: str
) -> bool:
    """Apply a model change to the LIVE session instead of tearing it down.

    ``session/set_model`` switches the model on a running kiro-cli session.
    Verified against kiro-cli 2.15.1: acked synchronously, carries the existing
    conversation across the switch (including across vendors), sticks over
    subsequent turns, and switches back. That makes a session reset
    unnecessary for an idle slot — and the reset is expensive twice over, since
    it kills the whole process tree now AND forces the next message to
    cold-start and replay a compressed transcript.

    Returns True when the live session owns *model_name*. False means the caller
    must fall back to a reset — including when there is no live session at all,
    where the reset is an O(1) no-op teardown but still routes through
    ``_reset_slot_session``'s pending-wait cleanup.
    """
    if not isinstance(provider, AcpProvider):
        return False
    if provider.has_active_turn():
        # Same hazard api_chat_slot_reasoning_effort documents: awaiting a
        # response mid-turn races the streaming prompt loop on stdout for the
        # non-multiplexed client. api_chat_slot_model answers 409 before
        # reaching here (its check and this call share one no-await window),
        # so this is defense in depth for any future caller — decline the
        # live switch rather than race the stream.
        return False
    wire = _wire_model_id(provider, model_name)
    if not wire:
        return False
    try:
        await provider.client.set_model(wire)
    except AcpModelUnavailable:
        # NOT a "the call didn't land" failure, so the reset fallback below is
        # the wrong recovery: it would tear down the live conversation and then
        # cold-start on a DIFFERENT model while the caller reported success.
        # Propagate so the handler answers 4xx and the slot keeps its old model.
        raise
    except Exception as exc:
        logger.warning(
            "Live set_model(%s) failed for slot %s: %s: %s — falling back to reset",
            wire,
            name,
            type(exc).__name__,
            exc,
        )
        return False
    refused = provider.model_pin_refused if provider_model_pin_refused(provider) else ""
    if refused:
        # The shared-runtime handle applies a model through its config-option
        # ladder NON-strictly: an adapter that refuses every spelling leaves the
        # session on its default and records the refused id instead of raising
        # (the dedicated AcpClient raised above). The user named this exact
        # model, so answer it the way the dedicated runtime does: same error,
        # same 4xx, slot keeps its old model. The handle resets the record at
        # the start of every call, so this is THIS pick's verdict, not a stale
        # one. On a pair-id harness the advertised list is the entitlement, so
        # a bare id the pairs serve being refused is the adapter contradicting
        # its own list, which selects the adapter-mismatch wording.
        raise AcpModelUnavailable.for_recorded_refusal(
            refused, provider.available_models(), backend=provider.client.backend
        )
    # Client-scoped explicit-pick epoch. The pick GENERATION above is
    # slot-local, but two slots can drive one wire session (a channel-born
    # slot and its dashboard alias share `effective_session_key`), and the
    # refusal-fallback restore guard on another slot cannot see this slot's
    # generation. The shared CLIENT is the one object every alias holds, so
    # an explicit pick that lands on the live session stamps it here and the
    # restore compares against its swap-time snapshot. pick_epoch_host
    # resolves the SAME innermost object on both ends — this handler holds
    # the AcpProvider wrapper while the runner can hold the wrapped client.
    # Stamped IMMEDIATELY after set_model lands and BEFORE the effort
    # reapply: the wire session serves the picked model from this point, so
    # a sibling slot's refusal restore can already observe the pick live. If
    # the reapply below fails (reset fallback), a missing stamp would let
    # that restore treat the landed pick as its own swap and overwrite it.
    try:
        _host = pick_epoch_host(provider)
        _host._explicit_pick_epoch = getattr(_host, "_explicit_pick_epoch", 0) + 1
    except Exception:  # pragma: no cover - a frozen/slotted stub client
        pass
    if not await _reapply_effort_after_live_switch(name, slot, provider):
        return False
    logger.info("Slot %s model switched live to %r (session preserved)", name, wire)
    return True


def _broadcast_context_reset(state: "DashboardState", slot_key: str, provider: Any) -> None:
    """Push one ``context_usage`` event so the meter updates on a model switch.

    Without this the frontend keeps the previous model's stored ``{used,
    window}`` until the next turn emits an event. ``reset: true`` tells the
    ``sseContextUsage`` reducer it may REPLACE or DELETE the stored token entry
    (a frame WITHOUT ``reset`` never deletes, so the backend sets ``reset``
    whenever it has no real counts to send). With a live provider the payload
    carries the freshly rebased stats from ``set_model``; without one (the
    session-reset path) it carries no tokens, so the reducer deletes the entry
    and the UI falls back to its own model-derived window for the slot's new
    model. Best-effort: a broadcast failure must not fail the switch.
    """
    try:
        if provider is not None:
            payload = _context_usage_payload(slot_key, provider)
        else:
            payload = {"slot": slot_key, "pct": 0.0}
        payload["reset"] = True
        state.broadcast_context_usage(slot_key, payload)
    except Exception:
        logger.exception("Failed to broadcast context_usage reset for slot %s", slot_key)


async def api_chat_slot_model(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/model — set model for a chat slot.

    Prefers an in-place ``session/set_model`` on the running session and only
    resets when that is impossible (no ACP provider, an unrepresentable
    target, or the live call failing). A turn in flight answers 409 instead:
    the reset fallback would tear down the streaming turn mid-stream.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_model")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # The "Auto (Jev)" entry is resolved HERE and nowhere else: every reader below
    # -- the guard, the live switch, the reset, the session allocation, the
    # composer chip -- takes the id it produces, which is the plain ``auto`` the
    # session actually runs on until a tier is answered. The choice itself
    # survives as ``slot.jev_route`` alone.
    jev_route = _is_jev_route_pick(body.get("model", ""))
    if jev_route and not is_owner_dashboard_request(request):
        # Arming routing is an OWNER action: it hands the per-turn model choice to
        # the oracle, and a dear tier spends the owner's credential on a model the
        # caller never named. The cross-app gate above cannot carry that decision --
        # it admits an allow-listed non-owner whose ``app`` claim is empty -- so the
        # arm is gated on the same predicate the decision seam's consent route uses.
        # Only the arm is refused: a plain pick stays open to the same caller.
        sel().log_api_access(
            caller="non-owner",
            operation="chat_slot_model_jev_route",
            outcome="denied",
            source="owner_only",
            resources=f"slot={name}",
            error="non-owner identity rejected",
        )
        return _owner_denial_response(request, "arming Jev routing is owner-only")
    model_name = "auto" if jev_route else _normalize_model(body.get("model", ""))
    reason = _model_rejected_reason(model_name)
    if reason:
        logger.warning("Slot %s model rejected: %s", name, reason)
        return web.json_response({"error": reason}, status=400)
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal
    # Three locks, always in this order (slot._lock, then the session lock,
    # then _model_pick_lock -- see _slot_switch_session_lock; the bulk handler
    # nests them the same way and nothing takes them in the opposite order).
    # An ExitStack because the session lock's KEY is only known after the
    # in-lock read below:
    # the session lock — two switches arriving through DIFFERENT alias slots
    # resolve onto ONE session but take DISJOINT per-slot locks, so without
    # it neither waits for the other and both reset this same session.
    #
    # slot._lock — same serialization as the agent, effort and workspace
    # switch handlers: the awaits below yield the event loop, and an
    # interleaved second switch could otherwise observe (or write)
    # intermediate state — two racing switches would each commit and reset
    # against the other's half-applied session. Holding it across the
    # provider RPC mirrors the effort handler holding it across change_effort.
    #
    # slot._model_pick_lock — one pick transaction at a time against the
    # model-fallback machinery: the fallback
    # swap and restore probe in chat_runner hold it across their own
    # set_model awaits, and a pick landing inside that window could be
    # overwritten by the swap (or roll back the swap's state). Serialising
    # the whole check → mutate → switch → rollback span makes each pick
    # atomic; the CAS rollback below stays as a backstop against any writer
    # outside both locks.
    #
    # There is deliberately NO unlocked no-op fast path: a serialized switch
    # holding the locks commits slot.model before its RPC and rolls it back
    # on AcpModelUnavailable, so an unlocked equality read could match that
    # transient value and report "already on X" for a model that is then
    # rolled back.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above (see _slot_replaced_while_queued):
        # ``name`` can be recreated for a different app while this request
        # queued, and every read of ``slot`` below would be of the stale one.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_model"):
            return slot_not_found()
        # The session the switch will probe and, on the reset path, tear
        # down. ``effective_session_key``, never ``_history_key_for`` (the
        # reload handler's rule): a channel- or cron-born slot runs its turns
        # under its linked key, and the dashboard-prefixed spelling names a
        # session that never existed — the busy probe would see nothing and
        # the reset would "succeed" against nothing while the live process
        # kept the old model. Resolved INSIDE the lock, not before it: the
        # binding can land while this request waits on the lock (a cron or
        # workflow slot is linked when its first result is injected), and a
        # key read before the wait would then name the wrong session.
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot on this same session.
        # slot._lock is created per _ChatSlot and so is DISJOINT across
        # aliases. Keyed on the value resolved just above -- the same one the
        # probe and reset below use -- so the lock provably guards them even if
        # a binding landed while this request waited on slot._lock (see
        # _slot_switch_session_lock).
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        await _stack.enter_async_context(slot._model_pick_lock)
        # Two more lock-acquisition awaits, one re-check: nothing reads
        # ``slot`` between them, so a check after the last one covers both.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_model"):
            return slot_not_found()
        # App isolation on the SESSION, not just the slot (the cancel
        # routes' policy): slot ownership does not imply ownership of a
        # linked channel session, so an app caller may not switch the model
        # a channel thread runs on. Denied as an indistinguishable 404.
        denied = _app_cancel_denied(request, slot, "chat.slot_model", session_key)
        if denied is not None:
            return denied
        # The routing flag is committed on each SUCCESS path and nowhere else -- see
        # the two writes below. Nothing is written here, because the busy check
        # between this line and the transaction answers 409 without a rollback: a
        # pick that was refused must not change what the next turn runs on.
        #
        # Checked INSIDE the locks only: a serialized predecessor targeting the
        # same model may have committed while this request waited, and acting
        # again would tear down the session that predecessor just set up.
        if (
            slot.model == model_name
            and not slot._active_fallback_model
            and not slot._refusal_fallback_primary
        ):
            # Same-value pick: nothing to switch, but the user's EXPLICIT
            # affirmation of this model must still be recorded — the fallback
            # restore probe reads the pick generation, and without the bump a user
            # who deliberately picks the very model the session fell back to (or
            # that the backfill wrote) would have their choice silently overridden
            # by the next restore probe.
            #
            # NOT taken while a fallback is actively serving the session: the pin
            # may equal the displayed primary while the wire model is the
            # fallback, so "nothing to switch" is false — the normal live-switch
            # path below must run so the pick actually moves the session (an
            # early return here strands the session on the fallback while usage
            # is attributed to the primary). The
            # pick-the-fallback-itself case also flows through the live path,
            # where the switch is a harmless same-model set and the pick-gen bump
            # still protects the choice from the restore probe.
            # The slot-local bump alone is invisible ACROSS aliases: two slots
            # can drive one wire session, and the other slot's refusal-fallback
            # restore compares the shared CLIENT's epoch, not this slot's
            # generation. A same-value pick is still an explicit pick, so stamp
            # the shared epoch exactly as the live-switch success path does —
            # otherwise an alias pinned to the fallback candidate re-picks it,
            # takes this shortcut, and the originating slot's restore silently
            # undoes the choice.
            _provider = state.sessions.get_provider(session_key)
            try:
                _host = pick_epoch_host(_provider)
                _host._explicit_pick_epoch = getattr(_host, "_explicit_pick_epoch", 0) + 1
            except Exception:  # pragma: no cover - a frozen/slotted stub client
                pass
            slot._model_pick_gen += 1
            # The one place the flag is recorded on this path, and the reason it is
            # not written before the branch: an unpinned slot picking "Auto (Jev)"
            # resolves to the ``auto`` it already holds and lands HERE, so a write
            # placed only in the transaction below would never run for the
            # commonest case. This shortcut is a success, so committing is correct.
            slot.jev_route = jev_route
            state.push_slots_update()
            return web.json_response({"ok": True, "model": model_name, "jev_route": jev_route})
        provider = state.sessions.get_provider(session_key)
        if _switch_target_busy(state, slot, session_key, provider):
            # Never tear down an in-flight turn: _try_live_model_switch
            # declines a mid-turn live switch, so falling through would take
            # the reset fallback and kill the streaming turn for any
            # programmatic caller (the UI disables the picker mid-turn, but
            # the API has no such guard). Answer busy instead — same policy
            # as the effort handler's defer-not-reset branch and the bulk
            # handler's skip_running default. The refusal applies to EVERY
            # provider class with an active turn, not only the ACP one that
            # could have gone live: the reset fallback below tears down the
            # in-flight turn regardless of provider type, and a 409 is
            # retryable once the turn completes. The signals it reads, and
            # the sibling-alias window only the third one covers, are on the
            # helper.
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        prior_model = slot.model
        prior_pick_gen = slot._model_pick_gen
        prior_jev_route = slot.jev_route
        slot.model = model_name
        # Set on every pick, not only the Jev one: picking a concrete model is the
        # owner answering the very question the point asks, so it clears the flag
        # rather than leaving a routing that the next turn would apply over the
        # model they just chose. Inside the transaction, so ``_rollback_pick``
        # covers it.
        slot.jev_route = jev_route
        # Explicit user pick: bump the pick generation so the model-fallback
        # restore probe never overrides this choice (automatic backfill does NOT
        # bump it).
        slot._model_pick_gen += 1

        def _rollback_pick() -> None:
            """Undo this request's commit — model AND pick generation together.

            A refused or declined pick changed nothing, and leaving the bump in
            place would make the fallback restore probe read it as an explicit
            choice and silently abandon restoring the primary — the session
            would stay on the fallback with no card and no probe.

            COMPARE-AND-SWAP, not unconditional: both locks make an
            interleaved pick impossible, so this is a backstop against any
            writer outside them. Only restore when
            the state is still exactly ours (our bump, our model); the check
            and both writes are synchronous, so they are atomic on the event
            loop.
            """
            if slot._model_pick_gen == prior_pick_gen + 1 and slot.model == model_name:
                slot.model = prior_model
                slot._model_pick_gen = prior_pick_gen
                # The routing choice is part of the same commit: a refused pick
                # changed nothing, and leaving the flag would route the next turn
                # for a request the caller was told had failed.
                slot.jev_route = prior_jev_route

        def _live_serves_target(candidate: object) -> bool:
            """True when the live session's BACKEND-RESOLVED model already
            equals the requested wire id — the truth-based success exception.

            Not identity guessing: dispatch captures slot.model at its call
            site and registers the session only after provider.start(), so
            neither identity nor registration time proves anything; the
            served model does. A True here means slot.model is consistent
            with what actually runs — the partially-applied live switch
            (set_model landed, then the effort reapply failed) — so rollback
            would publish the OLD model over a live session running the NEW
            one, and a teardown would kill it. Defined once and consumed by
            BOTH the pre-reset busy re-check and the post-decline
            disambiguation, so the two spellings cannot diverge.
            """
            if not isinstance(candidate, AcpProvider):
                return False
            wire = _wire_model_id(candidate, model_name)
            if not wire:
                return False
            if candidate.served_model == wire:
                return True
            if wire == "auto":
                # AcpProvider.served_model collapses the "auto" sentinel to ""
                # on purpose (the fallback canary must never probe a model the
                # backend did not resolve), so a landed switch TO Auto is
                # invisible through it. The session client keeps the raw id —
                # after set_model("auto") lands the handle prefers that
                # explicit assignment — so read it unfiltered for this one
                # value (the same literal _wire_model_id hands out for Auto).
                # Any other non-match stays False (fail-closed).
                raw = getattr(candidate.client, "served_model", "")
                return str(raw or "").strip() == wire
            return False

        # Set on the reset path when the old session's teardown RAISED after
        # the pop: the switch is committed, the response carries an advisory
        # warning (agent-handler precedent via _reset_slot_session_or_warn).
        teardown_incomplete = False
        try:
            went_live = await _try_live_model_switch(name, slot, provider, model_name)
        except AcpModelUnavailable as exc:
            # The live session refused the pick as unavailable to this account.
            # Roll the slot back so the picker keeps showing what is actually
            # running, and answer 4xx — deliberately NOT the reset fallback
            # below, which would destroy the conversation and cold-start on a
            # DIFFERENT model while reporting success. Only the session that
            # owns the advertised list gets to make this call, so there is no
            # pre-emptive gate here to go stale. The rollback runs under the
            # locks, so no serialized successor can observe the transient value.
            _rollback_pick()
            logger.warning("Slot %s model rejected: %s", name, exc)
            return web.json_response({"error": str(exc), "code": "model_unavailable"}, status=400)
        if effective_session_key(slot) != session_key:
            # The slot was bound to a different session while the live switch
            # awaited its provider RPCs (a cron/workflow slot gets linked when
            # its first result is injected). Whatever set_model did landed on
            # a session the slot no longer runs on, and resetting the key this
            # request resolved would tear down (or "succeed" against) the
            # wrong session — either way committing would advertise the new
            # model over a session this handler never touched. Roll back and
            # answer 409; the retry resolves the current binding.
            _rollback_pick()
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
        if went_live:
            # The live session runs the pick; refresh the slot's served-model
            # cache from it so an inheriting chip ("auto") does not keep naming
            # the model the session was spawned with.
            _sync_served_model(slot, provider)
            _broadcast_context_reset(state, slot.key, provider)
        else:
            # LAST-INSTANT busy re-check — the invariant this handler rests on:
            # no destructive step may run while a turn can be live, so idleness
            # must be established within a NO-AWAIT window immediately before
            # the teardown, and the atomic skip_if_busy decline covers only that
            # microsecond residue. The
            # pre-check above is separated from this point by
            # _try_live_model_switch's provider RPCs (seconds on a slow
            # backend), so a send may have started — and even posted an
            # ask_question card — since it ran; _reset_slot_session clears
            # pending waits BEFORE its atomic decline (its docstring's safety
            # argument assumes a caller-side busy check microseconds old), so
            # entering it busy would falsely reject that turn's cards even
            # though the reset itself declines. Busy here → roll back and
            # answer the same 409 the pre-check gives.
            # Children guard, shared with reload/continue: the reset tears
            # down the runtime attached sub-agents run on, so a parent that is
            # idle but still has children (running, queued, or with a
            # completion event in flight) must refuse rather than discard
            # their work. Same probe block as api_chat_slot_reload; only the
            # rollback is added here because this handler committed first.
            children_409 = await _subagents_attached_response(
                state, slot, session_key, "slot_model"
            )
            if children_409 is not None:
                _rollback_pick()
                return children_409
            # Probed AFTER the children await, so this is the NO-AWAIT window
            # the reset needs; same predicate as the pre-check
            # (_switch_target_busy), so a sibling alias that began
            # cold-starting during either await is refused here too.
            recheck = state.sessions.get_provider(session_key)
            if _switch_target_busy(state, slot, session_key, recheck):
                if _live_serves_target(recheck):
                    # The turn that slipped in runs on a session that already
                    # serves the target (set_model landed before the effort
                    # reapply failed): slot.model is truthful, so report
                    # success without teardown — rolling back would publish
                    # the old model while the live turn streams under the new
                    # one.
                    logger.warning(
                        "Slot %s model switch: live session already serves the "
                        "target; skipping the reset under its in-flight turn",
                        name,
                    )
                    _sync_served_model(slot, recheck)
                    _broadcast_context_reset(state, slot.key, recheck)
                    state.push_slots_update()
                    return web.json_response(
                        {"ok": True, "model": model_name, "jev_route": jev_route}
                    )
                _rollback_pick()
                return web.json_response(
                    {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                )
            logger.info(
                "Slot %s model switched to %r, resetting session", name, model_name or "auto"
            )
            # skip_if_busy: the has_active_turn() 409 above is a best-effort
            # fast path — message dispatch does not take slot._lock, so a turn
            # can start between that check and this reset. SessionManager.reset
            # evaluates busyness atomically with the session pop (the same
            # authoritative-backstop split api_chat_slot_reload documents), so
            # a turn that slipped into the window is declined here instead of
            # torn down mid-stream.
            reset_ok = await _reset_slot_session_or_warn(
                state, slot, session_key, switch_kind="model"
            )
            if reset_ok is None:
                # Teardown raised after the session pop: the switch is
                # COMMITTED (see _reset_slot_session_or_warn), so the answer
                # is the committed state with an advisory warning — never a
                # 500 that strands clients on the old value. Deliberately no
                # _rollback_pick(): rollback is only for the decline/409
                # paths, where nothing was torn down. NOT an early return:
                # the rebind guard below must still run, so a slot rebound
                # during the raising await answers the same rollback + 409 as
                # any other rebind.
                teardown_incomplete = True
            elif not reset_ok:
                # Disambiguate the decline FAIL-CLOSED — with one truth-based
                # exception checked first. Provider identity or registration
                # time cannot prove which model a live session runs: dispatch
                # captures slot.model at its call site but registers the
                # session only after a multi-second provider.start(), so a
                # session registered after the commit may still carry the old
                # model. A false 409 is retryable and costs nothing; a false
                # success strands a live session on the old model under the
                # new slot.model.
                busy_provider = state.sessions.get_provider(session_key)
                live_serves_target = _live_serves_target(busy_provider)
                if live_serves_target:
                    # See _live_serves_target: slot.model is consistent with
                    # what actually runs, so success without teardown is the
                    # truthful answer; the un-pushed effort override is
                    # already persisted on the slot and applies on the next
                    # cold start (same degradation the effort handler's defer
                    # branch accepts).
                    logger.warning(
                        "Slot %s model switch: live session already serves the "
                        "target; declined reset left it in place (effort "
                        "override, if any, applies on the next cold start)",
                        name,
                    )
                if not live_serves_target and isinstance(busy_provider, LLMProvider):
                    if busy_provider.has_active_turn():
                        # A turn slipped in: roll back the commit and answer
                        # the same 409 the fast path gives, leaving the turn
                        # running whichever model it captured.
                        _rollback_pick()
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
                    # A live IDLE session declined the reset (its turn ended
                    # before this re-read). Reporting success would leave that
                    # process alive on whatever model it captured. Tearing
                    # down an idle session is always safe — history lives on
                    # the slot, not in the process — so retry once
                    # (api_chat_slot_reload's template for this exact race); a
                    # second decline means another turn is genuinely racing,
                    # which is the turn-in-flight case again.
                    reset_ok = await _reset_slot_session_or_warn(
                        state, slot, session_key, switch_kind="model"
                    )
                    if reset_ok is None:
                        # Retry teardown raised: same committed-switch answer
                        # as the first attempt, and same fall-through to the
                        # rebind guard below.
                        teardown_incomplete = True
                    elif not reset_ok:
                        _rollback_pick()
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
                # No live provider: there was no registered session to tear
                # down — the next message cold-starts under the new model,
                # which is exactly what the reset would have arranged.
            if effective_session_key(slot) != session_key:
                # Same check after the reset await(s) as after the live
                # switch: the session this request tore down is no longer
                # the slot's, so the commit would advertise the new model
                # over a session that never saw the switch. The teardown
                # itself was harmless (that session was idle and no longer
                # bound); roll back the commit and let the retry resolve the
                # current binding.
                _rollback_pick()
                return web.json_response(
                    {
                        "error": "slot session was rebound during the switch",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            _broadcast_context_reset(state, slot.key, None)
    state.push_slots_update()
    model_resp: dict = {"ok": True, "model": model_name, "jev_route": jev_route}
    if teardown_incomplete:
        # Advisory only — the switch itself succeeded and the response
        # carries the committed state (agent-handler precedent).
        model_resp["warning"] = _TEARDOWN_INCOMPLETE_WARNING
    return web.json_response(model_resp)


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


def _autocompact_txn_lock(history_key: str) -> asyncio.Lock:
    lock = _autocompact_txn_locks.get(history_key)
    if lock is None:
        lock = asyncio.Lock()
        _autocompact_txn_locks[history_key] = lock
    return lock


# Same per-transcript transaction lock, for the source-link unlink write. A
# dismissal is persisted into the shared transcript metadata, so concurrent
# unlinks (or an unlink racing a sibling flush) on alias slots that resolve onto
# one transcript must serialize or a loser's rollback / a stale sibling can
# overwrite the winner's acknowledged commit. Keyed by transcript, like above.
_source_link_txn_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)


async def api_chat_slot_autocompact(request: web.Request) -> web.Response:
    """GET/POST /api/chat/slots/{slot}/autocompact — per-session compact threshold.

    GET returns the slot's override (``pct``, null when it follows the global),
    the current global (``global_pct``), and the valid range. POST takes
    ``{"pct": <number|null>}``: a number sets this session's override (rejected
    outside the documented range, matching the global knob's PATCH validation),
    null clears it back to the global. The value applies to the live session
    immediately via the SessionManager override map and persists with the slot
    metadata, so it survives gateway restarts.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    # Session-aware ownership gate, not the slot-only check: the POST writes the
    # override keyed by effective_session_key(slot), so a linked app-owned slot
    # (channel stem) would let an app modify a foreign session's threshold and
    # metadata. deny_app_slot_session_access authorizes the key the write actually
    # lands on, same as /context and /note.
    request_app = request.get("app", "")
    denied = deny_app_slot_session_access(request_app, slot, name, "slot_autocompact")
    if denied is not None:
        return denied
    if request.method == "GET":
        return web.json_response(
            {
                "pct": slot.autocompact_pct,
                "global_pct": published_autocompact_pct(),
                "min": AUTOCOMPACT_PCT_MIN,
                "max": AUTOCOMPACT_PCT_MAX,
            }
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    if "pct" not in body:
        return web.json_response(
            {"error": "pct is required (number or null)", "code": "pct_required"}, status=400
        )
    pct = body["pct"]
    if pct is not None:
        # bool is an int subclass; True would otherwise read as 1.0 and be
        # rejected by range, but reject it explicitly for a clear error.
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            return web.json_response(
                {"error": "pct must be a number or null", "code": "pct_not_a_number"}, status=400
            )
        try:
            pct = float(pct)
        except OverflowError:
            # An int too large for a float; out of range by definition.
            return web.json_response(
                {"error": "pct must be a finite number", "code": "pct_not_finite"}, status=400
            )
        if pct != pct:  # NaN
            return web.json_response(
                {"error": "pct must be a finite number", "code": "pct_not_finite"}, status=400
            )
        if not (AUTOCOMPACT_PCT_MIN <= pct <= AUTOCOMPACT_PCT_MAX):
            return web.json_response(
                {
                    "error": (
                        f"pct must be between {AUTOCOMPACT_PCT_MIN:g} "
                        f"and {AUTOCOMPACT_PCT_MAX:g}"
                    ),
                    "code": "pct_out_of_range",
                },
                status=400,
            )
    # Persist via the same forced-save mechanism every other slot-metadata
    # route uses (tags / folders / pin: save_slot_off_loop(force=True) -- the
    # empty-window merge for message-less slots, the full save otherwise;
    # both read the slot fields INSIDE the transcript's cross-process lock
    # and run the delete-won guard, so a permanent delete racing this write
    # is refused by main's own tested path). A never-saved tab's override
    # lives only in memory until the tab first persists, exactly like its
    # model: the empty-window merge lands only into an existing line.
    #
    # Transactional shape (mirrors the tag-vocabulary delete): mutate the
    # slot field, confirm the durable write with best_effort=False, and on
    # any failure roll the field back and return a coded error -- the
    # SessionManager override map (the live gate) is only touched after the
    # persist verdict, so a failed request leaves live behavior unchanged.
    # Serialize the whole transaction per slot: with awaits inside the write
    # span, a second concurrent POST would otherwise capture this one's value
    # as its rollback snapshot, and value-based rollback cannot tell "my
    # write survived" from "someone else wrote the same number". Under the
    # lock exactly one request is inside the span, so a rollback can only
    # undo its own write. The client's per-slot promise chain orders writes
    # from ONE client; this lock is the cross-client half. Keyed by the
    # TRANSCRIPT so two alias slots resolving onto one file serialize too.
    locked_history_key = slot_history_key(slot)
    async with _autocompact_txn_lock(locked_history_key):
        stale = _reauthorize_after_await(state, slot, name, request_app, "slot_autocompact")
        if stale is not None:
            return stale
        # Pin the write to the transcript this authorization decision covered:
        # the persist await below is a rebind window, and the save derives its
        # target from live routing at write time. expected_history_key makes the
        # save refuse (False, nothing written) if the routing moved, so the
        # durable write can never land on a transcript this request was not
        # authorized against. No await between the reauth above and this read.
        authorized_history_key = slot_history_key(slot)
        if authorized_history_key != locked_history_key:
            # The slot was rebound between the lock-key read and acquisition:
            # this request holds the OLD transcript's lock while the write
            # would target the new one, so the serialization guarantee does
            # not cover it. Same disposition as the mid-persist rebind below.
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        prior_pct = slot.autocompact_pct
        slot.autocompact_pct = pct
        if state.conversation_log:
            try:
                applied = await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    best_effort=False,
                    expected_history_key=authorized_history_key,
                )
            except Exception:
                # Roll back this request's write and mark dirty so the
                # periodic flush reconverges the durable record to the live
                # field (a non-endpoint save may have durably written this
                # rejected value before the failure).
                slot.autocompact_pct = prior_pct
                slot._dirty = True
                logger.exception("Slot %s autocompact_pct persist failed", name)
                return web.json_response(
                    {"error": "could not persist threshold", "code": "persist_failed"}, status=500
                )
            if not applied:
                # The save refused without writing: either the delete-won guard
                # (session permanently deleted while the save awaited the lock)
                # or the routing-moved pin (slot rebound to another transcript
                # mid-request). Do not resurrect, do not write elsewhere, do not
                # mutate live state.
                slot.autocompact_pct = prior_pct
                return web.json_response(
                    {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
                )
            # Mirror the COMMITTED value to every live slot whose current
            # transcript key is the one this write landed on — not just the
            # requesting slot: channel-linked aliases resolve distinct slot
            # names onto one file, and a sibling left holding the old value
            # would persist it back over this acknowledged commit on its next
            # flush (its ordinary save writes ``autocompact_pct``
            # unconditionally from its own field). Membership is re-derived
            # here, NOT assumed: a slot rebound during the persist no longer
            # writes this file, and mirroring it would apply a live change its
            # reauthorization is about to deny. Snapshot values() — the event
            # loop may mutate the dict between iterations. Priors are recorded
            # so the confirm-save failure paths below can undo the mirror.
            mirrored: list = []
            for other in list(state._slots.values()):
                if other is not slot and slot_history_key(other) == authorized_history_key:
                    mirrored.append((other, other.autocompact_pct))
                    other.autocompact_pct = pct
            # The mirror runs on the event loop AFTER the persist returned, but
            # a sibling's already-queued flush can acquire the transcript's
            # file lock in the executor BEFORE the loop resumes here and write
            # its then-stale field over the acknowledged commit (executor
            # threads do not wait for the event loop). The mirror above fixes
            # every live field; this second confirmed save re-orders the
            # durable record after any such interleaved stale write — the file
            # lock serializes it behind the sibling's write, and every field it
            # can read is now the committed value. Same pin, same dispositions.
            try:
                confirmed = await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    best_effort=False,
                    expected_history_key=authorized_history_key,
                )
            except Exception:
                for other, other_prior in mirrored:
                    if slot_history_key(other) == authorized_history_key:
                        other.autocompact_pct = other_prior
                        other._dirty = True
                slot.autocompact_pct = prior_pct
                slot._dirty = True
                logger.exception("Slot %s autocompact_pct confirm-persist failed", name)
                return web.json_response(
                    {"error": "could not persist threshold", "code": "persist_failed"}, status=500
                )
            if not confirmed:
                for other, other_prior in mirrored:
                    if slot_history_key(other) == authorized_history_key:
                        other.autocompact_pct = other_prior
                        other._dirty = True
                slot.autocompact_pct = prior_pct
                return web.json_response(
                    {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
                )
        # INVARIANT for this handler: every write is immediately preceded by an
        # authorization decision with NO await between them. The persist await is
        # a rebind window (same mechanism as the body read), so re-decide before
        # the live override mutation; the slot-field change above rolls back for
        # a rebound slot, whose successor re-derives its state on restore.
        stale = _reauthorize_after_await(state, slot, name, request_app, "slot_autocompact")
        if stale is not None:
            slot.autocompact_pct = prior_pct
            return stale
        # Reauthorization can PASS after a rebind the pin never saw: a rebind
        # landing after the save's internal routing read leaves the durable
        # write correctly on the authorized transcript while the slot now
        # resolves to a different session the caller may also own. Seeding the
        # live map from effective_session_key(slot) would then apply the
        # threshold to a session whose transcript never received it. Refuse:
        # the committed transcript's siblings were mirrored above and its live
        # override re-seeds on hydration; this slot's successor re-derives.
        if slot_history_key(slot) != authorized_history_key:
            slot.autocompact_pct = prior_pct
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        live_pct = slot.autocompact_pct
        state.sessions.set_autocompact_pct(effective_session_key(slot), live_pct)
        logger.info("Slot %s autocompact_pct set to %r", name, live_pct)
        return web.json_response(
            {"ok": True, "pct": live_pct, "global_pct": published_autocompact_pct()}
        )


async def api_chat_slots_model(request: web.Request) -> web.Response:
    """POST /api/chat/slots/model — set the model for ALL chat slots (bulk).

    Body: {"model": "<name>" | "", "skip_running": bool (default True)}.
    "" selects the provider/auto default. Applies the model to every slot
    whose model differs, resetting each affected slot's session. Mid-turn
    policy deliberately differs from ``api_chat_slot_model``: the single-slot
    handler prefers a live in-place switch and answers 409 for a slot
    mid-turn, while this bulk endpoint always resets and skips mid-turn slots
    when ``skip_running`` is true (the default) — passing ``skip_running:
    false`` is an explicit opt-in that still tears down in-flight turns.
    Returns the slot keys that were switched / skipped / unchanged /
    failed; a per-slot reset failure is isolated (that slot is reported in
    ``failed`` and keeps its old model) rather than aborting the whole switch.
    """
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    model_name = _normalize_model(body.get("model", ""))
    reason = _model_rejected_reason(model_name)
    if reason:
        return web.json_response({"error": reason}, status=400)
    skip_running = body.get("skip_running", True)
    if not isinstance(skip_running, bool):
        return web.json_response({"error": "skip_running must be a boolean"}, status=400)
    # Deny-by-default (security-controls): the auth middleware always sets
    # request["app"] on every authenticated path (empty string for dashboard
    # users, app name for app tokens). An ABSENT key means the middleware did
    # not run -- refuse rather than fall through to all-slot access.
    if "app" not in request:
        return web.json_response({"error": "unauthorized"}, status=403)
    request_app = request["app"]
    # Dashboard users are identified by the middleware's EXPLICIT "" assignment.
    # Compare with == "" (not truthiness) so an unexpected falsy value (None, 0)
    # fails closed into the per-slot ownership check instead of bypassing it.
    is_dashboard_user = request_app == ""

    switched: list[str] = []
    skipped_running: list[str] = []
    unchanged: list[str] = []
    # Whether any slot's per-turn routing flag was cleared without its model
    # changing -- the one bulk outcome that is invisible in the three lists below.
    routing_cleared = False
    failed: list[str] = []
    # Snapshot the slot keys up front: sessions.reset awaits, so iterating the
    # live dict directly would risk a concurrent-modification surprise.
    for name, slot in list(state._slots.items()):
        # App Kit ownership isolation: app callers can only switch their own
        # slots (mirrors api_chat_slots_cleanup). Only an explicit dashboard
        # user bypasses the ownership check.
        if not is_dashboard_user and slot._app != request_app:
            continue
        # Same three locks, same order, as the single-slot pick (slot._lock
        # outer, _model_pick_lock inner). ALL classification happens inside
        # them, equality FIRST: a serialized switch commits slot.model before
        # its provider RPC and rolls it back on failure, so an unlocked
        # equality read could match that transient value and report a slot
        # "unchanged" for a model that is then rolled back — and an
        # unlocked running-check ahead of the equality
        # check would classify a running slot that already uses the requested
        # model as skipped_running instead of unchanged. Queuing on the locks
        # is cheap: turns do not hold slot._lock, so a running slot's lock
        # only contends with another switch handler.
        #
        # The session lock is entered AFTER slot._lock, once the key below is
        # known (see _slot_switch_session_lock): per-slot locks are disjoint
        # across aliases, so without it a single-slot switch through another
        # alias could reset this same session concurrently.
        async with contextlib.AsyncExitStack() as _stack:
            await _stack.enter_async_context(slot._lock)
            # Re-authorize after the await above (see
            # _slot_replaced_while_queued): the ownership check ran on the
            # snapshot's object, and ``name`` may now register a different
            # slot -- another app's, or a reconnect. Reported like the
            # rebound case below: skipped, so the caller retries against
            # whatever the name resolves to now, never switched or failed.
            if _slot_replaced_while_queued(state, slot, name, request, "chat.slots_model"):
                skipped_running.append(name)
                continue
            # The session this slot's turns run on — effective_session_key,
            # never _history_key_for (see api_chat_slot_model), resolved
            # INSIDE the lock so a binding that lands while this iteration
            # waits on it is what the reset addresses.
            session_key = effective_session_key(slot)
            # Now serialize against every OTHER alias slot on this session,
            # keyed on the value resolved just above (see
            # _slot_switch_session_lock): per-slot locks are disjoint across
            # aliases. Entered per iteration and released with the stack, so
            # two alias slots in ONE bulk request queue in turn rather than
            # re-entering the same lock.
            await _stack.enter_async_context(_slot_switch_session_lock(session_key))
            await _stack.enter_async_context(slot._model_pick_lock)
            # Two more lock-acquisition awaits, one re-check: nothing reads
            # ``slot`` between them, so a check after the last one covers both.
            if _slot_replaced_while_queued(state, slot, name, request, "chat.slots_model"):
                skipped_running.append(name)
                continue
            if not is_dashboard_user and session_key != _history_key_for(name):
                # Slot ownership does not imply ownership of a linked channel
                # session (the cancel routes' second condition): an app caller
                # does not get to switch the model a channel thread runs on.
                # Skipped silently, like every other slot the app does not own.
                continue
            if slot.model == model_name:
                # The MODEL is unchanged; the routing choice may not be. A slot
                # already on this model but routed per turn is a slot whose turns
                # would still be moved off it, so the flag is cleared here as well
                # -- otherwise the one case where the bulk switch reports "nothing
                # to do" is the one case where it silently did nothing at all.
                # Tracked so the push below fires for a slot whose only change is
                # this: the picker reads the flag, so without a broadcast the chip
                # keeps naming a routing this request has just stopped.
                if slot.jev_route:
                    slot.jev_route = False
                    routing_cleared = True
                unchanged.append(name)
                continue
            # Children guard (api_chat_slot_reload's): the reset tears down the
            # runtime attached sub-agents run on, so a parent with children
            # running, queued, or mid-delivery is skipped rather than have
            # their work discarded — regardless of skip_running, which speaks
            # to the parent's own turn, not to its children.
            if await subagents_attached_async(state, slot, session_key, "slots_model"):
                skipped_running.append(name)
                continue
            # Busy check on the EFFECTIVE session, same predicate as the
            # single-slot handler (_switch_target_busy): slot.running only
            # sees turns dispatched through this slot's task; a
            # channel-linked slot's turn runs under its linked key without
            # setting it, and a sibling alias cold-starting on the shared
            # session has set neither. Probed AFTER the children await: the
            # reset below is the next thing this iteration does, so this is
            # the no-await window. _reset_slot_session clears pending waits
            # BEFORE its atomic decline (its docstring's safety argument
            # assumes a caller-side busy check microseconds old), so entering
            # it against a live linked turn would reject that turn's cards
            # even though the reset itself declines. Gated on skip_running
            # like the atomic decline below: a caller that asked to switch
            # running slots gets the reset regardless.
            live_now = state.sessions.get_provider(session_key)
            if skip_running and _switch_target_busy(state, slot, session_key, live_now):
                skipped_running.append(name)
                continue
            # Reset before flipping the model and isolate per-slot failures: if
            # the reset raises, leave slot.model untouched so the slot is never
            # left on the new model with stale history (the model/history
            # inconsistency), and a single failure doesn't abort the whole bulk
            # switch. skip_if_busy mirrors skip_running: when the caller asked
            # to skip running slots, a turn that started AFTER the checks above
            # (message dispatch does not take slot._lock) is declined at the
            # authoritative point — SessionManager.reset evaluates busyness
            # atomically with the session pop — instead of being torn down;
            # skip_running=false keeps its documented force semantics.
            try:
                reset_ok = await _reset_slot_session(
                    state, slot, session_key, skip_if_busy=skip_running
                )
                if skip_running and not reset_ok:
                    if slot.running:
                        # A first send slipped into the reset await and is still
                        # inside its multi-second provider.start(): visible to
                        # slot.running (set at dispatch) but not yet to
                        # get_provider, so the provider ladder below would read
                        # "no live provider" and commit over a session that
                        # captured the OLD model (this handler commits AFTER the
                        # reset). Classify it as the in-lock pre-check would have.
                        skipped_running.append(name)
                        continue
                    busy_provider = state.sessions.get_provider(session_key)
                    if isinstance(busy_provider, LLMProvider):
                        if busy_provider.has_active_turn():
                            # A turn slipped into the check window: classify it
                            # the same as the pre-check would have, leaving the
                            # turn (and the slot's model) untouched.
                            skipped_running.append(name)
                            continue
                        # A live IDLE session declined the reset: the
                        # slipped-in turn already finished before the re-read.
                        # This handler commits AFTER the reset, so that session
                        # is still on the old model — committing over it would
                        # create the exact model/history inconsistency the
                        # ordering exists to prevent. Retry once
                        # (api_chat_slot_reload's template); a second decline
                        # means another turn is genuinely racing. The retry
                        # runs INSIDE this try so a teardown that raises keeps
                        # the per-slot failure isolation: the slot lands in
                        # failed with its model untouched instead of aborting
                        # the whole bulk switch with a 500.
                        reset_ok = await _reset_slot_session(
                            state, slot, session_key, skip_if_busy=True
                        )
                        if not reset_ok:
                            skipped_running.append(name)
                            continue
                    # No live provider: no session to tear down — the next
                    # message cold-starts under the new model.
            except Exception:
                logger.error("Bulk model switch: session reset failed for %s", name, exc_info=True)
                failed.append(name)
                continue
            if effective_session_key(slot) != session_key:
                # The slot was bound to a different session during the reset
                # await: the session torn down is no longer the slot's, so
                # committing would advertise the new model over one that
                # never saw the switch. Nothing to roll back (bulk commits
                # after the reset); report it as skipped so the caller retries.
                skipped_running.append(name)
                continue
            slot.model = model_name
            # Explicit pick (bulk): same generation bump as the single-slot pick.
            slot._model_pick_gen += 1
            # And the same clearing of the routing choice. This surface takes no
            # "Auto (Jev)" target -- it switches many sessions to one model, which
            # is the opposite of a per-turn tier -- but it is an explicit pick, so
            # leaving the flag set would route the next turn away from the model
            # the owner just chose for this slot.
            slot.jev_route = False
            _broadcast_context_reset(state, slot.key, None)
            switched.append(name)

    if switched:
        logger.info(
            "Bulk model switch to %r: %d switched, %d skipped-running, %d unchanged, %d failed",
            model_name or "auto",
            len(switched),
            len(skipped_running),
            len(unchanged),
            len(failed),
        )
        # Guard the push on real progress so partial switches still broadcast
        # even when a later slot's reset failed.
        state.push_slots_update()
    elif routing_cleared:
        # No model moved, but a routing flag did, and the picker renders that.
        state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "model": model_name,
            "switched": switched,
            "skipped_running": skipped_running,
            "unchanged": unchanged,
            "failed": failed,
        }
    )


async def _configured_backend_for_slot(slot: _ChatSlot) -> str:
    """Resolve a cold slot through the same member-aware gate as the provider factory."""
    config = await asyncio.to_thread(KiroCrewConfig.load)
    from kiro_crew.members import select_provider_backend

    return select_provider_backend(
        effective_session_key(slot),
        config.agent.member_acp_backend,
        config.agent.acp_backend,
    )


async def api_chat_slot_selection_capabilities(request: web.Request) -> web.Response:
    """Report the live ACP session's model and effort selection capabilities.

    An absent session is unknown, not unsupported. The composer can preserve its
    cold-start behavior until the first ACP session has advertised its options.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_selection_capabilities")
    if denied is not None:
        return denied
    provider = state.sessions.get_provider(effective_session_key(slot))
    if not isinstance(provider, AcpProvider):
        # The slot can exist before its ACP session. The configured harness
        # still knows whether model IDs encode effort, so the picker can render
        # the base rows while live effort options are pending.
        backend = await _configured_backend_for_slot(slot)
        return web.json_response(
            {
                "known": False,
                "model_effort_pair_ids": backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
            }
        )
    backend = provider.capabilities.backend
    supported = provider.supports_effort()
    levels = (
        cap_effort_capability_levels(
            (
                level
                for level in provider.get_valid_effort_levels()
                if isinstance(level, str) and level in get_reasoning_effort_values()
            ),
            source="local live",
        )
        if supported
        else []
    )
    if supported and not levels:
        # Some model-scoped harnesses (notably kiro-cli) accept effort but do
        # not advertise an ACP config option. Keep the existing fallback menu.
        levels = cap_effort_capability_levels(
            (
                level
                for level in get_reasoning_effort_ordered()
                if level in get_reasoning_effort_values()
            ),
            source="local fallback",
        )
    return web.json_response(
        {
            "known": True,
            "backend": backend,
            "effort_supported": supported and bool(levels),
            "effort_levels": levels,
            "model_effort_pair_ids": backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
        }
    )


async def api_chat_slot_reasoning_effort(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/reasoning-effort — set reasoning effort.

    Body: {"reasoning_effort": "" | "low" | "medium" | "high" | "xhigh" | "max"}.
    "" = provider default (e.g. CC falls back to its opus heuristic, kiro to
    the model's default).

    Works for both ACP backends (claude-agent-acp and kiro-cli) via the
    provider's ``change_effort`` — which pushes the level live to the running
    session (claude: session/set_config_option, kiro: /effort + cli.json
    overlay). Effort is Opus/Sonnet-only; on a non-capable model this is a
    persisted no-op (no live apply, no session reset).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_reasoning_effort")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    effort = body.get("reasoning_effort", "")
    valid_efforts = get_reasoning_effort_values()
    if not isinstance(effort, str) or effort not in valid_efforts:
        return web.json_response(
            {
                "error": f"reasoning_effort must be one of: {', '.join(sorted(valid_efforts - {''}))}"
            },
            status=400,
        )
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal
    # Same serialization + transactional ordering as the agent switch: the
    # awaits below yield the event loop, so the section runs under the slot's
    # lock, and the slot is mutated only AFTER the switch actually took
    # effect (live update, deferral, or reset) — a failed request provably
    # changed nothing.
    # Two locks, in the order documented at _slot_switch_session_lock:
    # slot._lock, then the session lock. An ExitStack because the session
    # lock's KEY is only known after the in-lock read below, and locking on
    # any earlier read could leave this holding the wrong session lock.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above (see _slot_replaced_while_queued):
        # ``name`` can be recreated for a different app while this request
        # queued, and every read of ``slot`` below would be of the stale one.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_reasoning_effort"):
            return slot_not_found()
        # The session the switch will probe and, on the fallback path, reset —
        # ``effective_session_key``, never ``_history_key_for`` (see
        # api_chat_slot_model): a channel- or cron-born slot runs its turns
        # under its linked key, and the dashboard-prefixed spelling names a
        # session that never existed — the live-effort probe would see
        # nothing and the reset would "succeed" against nothing while the
        # live process kept the old effort. Resolved INSIDE the lock: the
        # binding can land while this request waits on it.
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot on this same session.
        # slot._lock is created per _ChatSlot and so is DISJOINT across
        # aliases. Keyed on the value resolved just above -- the same one the
        # probe and reset below use -- so the lock provably guards them even if
        # a binding landed while this request waited on slot._lock (see
        # _slot_switch_session_lock).
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        # Second lock-acquisition await, second re-check: a same-name
        # recreate lands during this wait just as easily as during the first.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_reasoning_effort"):
            return slot_not_found()
        # App isolation on the SESSION, not just the slot (the cancel routes'
        # policy), BEFORE the same-value fast path so the denial is
        # indistinguishable from a missing slot for every request shape.
        denied = _app_cancel_denied(request, slot, "chat.slot_reasoning_effort", session_key)
        if denied is not None:
            return denied
        try:
            await asyncio.to_thread(_remember_reasoning_effort_for_restore, effort)
        except (OSError, ValueError) as exc:
            logger.warning("Cannot retain effort selection: %s", exc)
            return web.json_response(
                {
                    "error": "reasoning effort persistence unavailable",
                    "code": "effort_marker_unavailable",
                },
                status=503,
            )
        provider = state.sessions.get_provider(session_key)
        legacy_model = slot.model
        split_base, legacy_level = model_registry.split_effort_suffix(legacy_model)
        legacy_base = ""
        if legacy_level:
            backend = (
                provider.capabilities.backend
                if isinstance(provider, AcpProvider)
                else await _configured_backend_for_slot(slot)
            )
            if backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS:
                legacy_base = split_base

        def normalize_legacy_model() -> dict[str, str]:
            if not legacy_base or slot.model != legacy_model:
                return {}
            slot.model = legacy_base
            slot._dirty = True
            return {"model": legacy_base}

        if slot.reasoning_effort == effort:
            # Re-picking the level already shown is still an explicit user
            # choice: bump the effort generation so a pending session_set_model
            # effort yields to it, as the model route bumps its own generation
            # on a same-value pick. Bumped here rather than on the write below,
            # which the setter skips as a same-value write, so a codex pin still
            # carrying a legacy ``[level]`` suffix gets the bump too.
            slot._effort_pick_gen += 1
            if not legacy_base:
                return web.json_response({"ok": True, "reasoning_effort": effort})
        logger.info("Slot %s reasoning_effort switched to %r", name, effort or "default")

        _updated_live: bool | None = False
        if isinstance(provider, AcpProvider) and provider.supports_effort():
            # Guard against racing the in-flight prompt read loop: a live
            # change_effort issues session/set_config_option and its response wait
            # would call stdout.readline() concurrently with the streaming
            # _prompt_loop → dropped/misrouted frame or a stuck turn. The override
            # is already persisted on the slot, so defer the live push to the next
            # turn instead of pushing now or resetting (effort is a cheap knob).
            if provider.has_active_turn():
                logger.info("Slot %s deferred live effort push: turn active", name)
                # This path's success point: the override is recorded on the
                # slot now and pushed to the live session next turn.
                slot.reasoning_effort = effort
                normalized = normalize_legacy_model()
                state.push_slots_update()
                return web.json_response(
                    {"ok": True, "reasoning_effort": effort, "deferred": True, **normalized}
                )
            # change_effort handles both backends and persists the per-model
            # override + overlay. "" clears the override → fall back to model
            # default (kiro: /effort with model default; claude: leave as-is).
            try:
                if effort:
                    _updated_live = await provider.change_effort(effort)
                else:
                    _updated_live = await provider.clear_effort()
            except Exception as exc:
                logger.warning(
                    "change_effort(%s) failed for slot %s: %s: %s — falling back to reset",
                    effort,
                    name,
                    type(exc).__name__,
                    exc,
                )
        elif isinstance(provider, AcpProvider):
            # Model does not support effort — persist the slot value for when the
            # user switches to a capable model, but do not touch the live session.
            _updated_live = True
            logger.info("Slot %s effort persisted (model not effort-capable)", name)

        if _updated_live is None:
            # clear_effort's third outcome: NOTHING changed -- not the workspace
            # overlay, not the provider's map. A bool cannot carry that here,
            # because both of its values commit the new slot value below (the
            # reset branch at the `slot.reasoning_effort = effort` before its
            # teardown, and the success path at the one before the final 200),
            # so either would show "default" while the overlay still holds the
            # old level and a respawn re-applies it. Commit nothing, reset
            # nothing, and let the caller retry once the other writer is done.
            return web.json_response(
                {
                    "error": "the workspace effort overlay is locked by another writer",
                    "code": "effort_overlay_busy",
                },
                status=409,
            )

        if effective_session_key(slot) != session_key and _updated_live:
            # The slot was bound to a different session while change_effort /
            # clear_effort awaited its provider RPC. The push landed on the
            # session the slot WAS bound to, and change_effort already
            # persisted the per-model override + overlay — that cannot be
            # unwound, so a 409 here would claim a rollback that did not
            # happen and leave the slot value contradicting the persisted
            # override. Commit the slot value (it is what the new binding's
            # next cold start reads) and report the rebind as a warning.
            slot.reasoning_effort = effort
            normalized = normalize_legacy_model()
            state.push_slots_update()
            return web.json_response(
                {
                    "ok": True,
                    "reasoning_effort": effort,
                    **normalized,
                    "warning": "slot session was rebound during the switch; "
                    "the new binding applies on its next cold start",
                }
            )

        if not _updated_live:
            if effective_session_key(slot) != session_key:
                # Rebound while a FAILED live push awaited: the reset fallback
                # below would tear down the wrong session, and nothing is
                # committed yet on this path, so there is genuinely nothing
                # to roll back — answer the same 409 the model/workspace
                # handlers use so the retry resolves the current binding.
                return web.json_response(
                    {
                        "error": "slot session was rebound during the switch",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            # Never tear down an in-flight turn (the model handler's policy,
            # and the _cancel_target subtlety: a RUNNING turn owns a captured
            # identity, so the key resolved above may not be the turn's).
            # Children guard, shared with reload/model: the reset tears down
            # the runtime attached sub-agents run on. Nothing is committed
            # yet, so a refusal here changes nothing.
            children_409 = await _subagents_attached_response(
                state, slot, session_key, "slot_reasoning_effort"
            )
            if children_409 is not None:
                return children_409
            # Probed AFTER the change_effort and children awaits above, in
            # the NO-AWAIT window before the commit and reset below; the
            # signals and the sibling-alias window are on _switch_target_busy.
            # The effort-capable live provider's active turn never reaches
            # this — the defer branch above already returned for it. A 409 is
            # retryable once the turn completes; nothing is committed yet.
            recheck = state.sessions.get_provider(session_key)
            if _switch_target_busy(state, slot, session_key, recheck):
                return web.json_response(
                    {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                )
            # No live session (or live update failed): reset so the next cold
            # start picks up the new effort via the provider factory/overlay.
            # The effort is committed BEFORE the reset: a message send landing
            # while the reset await is in flight cold-starts a session from
            # the slot's CURRENT value, so the new effort must already be
            # visible. A failing teardown does NOT undo the switch (the reset
            # pops the session first, so every replacement runs the new
            # value) and is reported as a success with a warning — a 500
            # would make the acting tab keep the OLD store value for a
            # switch that actually happened.
            prior_effort = slot.reasoning_effort
            slot.reasoning_effort = effort
            teardown_incomplete = False
            reset_ok = True
            try:
                # A POST-POP teardown raise does NOT undo the switch (the
                # reset pops the session first, so every replacement runs the
                # new value) and is answered as a success with a warning —
                # but only once the helper has verified the raise came AFTER
                # the session pop. A PRE-POP raise means the old session
                # survives on the old effort, so the helper propagates it and
                # this request restores the prior effort and answers 500.
                # The helper resets with skip_if_busy=True, which keeps this
                # handler's decline ladder below:
                # SessionManager.reset evaluates busyness atomically with the
                # session pop, so a turn that slipped into the residue (or
                # holds the semaphore before its prompt is in flight, which
                # has_active_turn cannot see) is declined here instead of torn
                # down mid-stream. The helper's None verdict marks the
                # degraded post-pop teardown; a bool verdict is the reset
                # outcome the ladder reads.
                reset_verdict = await _reset_slot_session_or_warn(
                    state, slot, session_key, switch_kind="reasoning_effort"
                )
            except Exception:
                # The identity probe proved the pop never happened: the old
                # session is still alive on the old effort, so the committed
                # value describes a switch that did not take and the acting
                # tab keeps its OLD store value on the 500. Restore the prior
                # effort (only if this request's write still stands) and
                # re-push so clients and persisted state land on the truth,
                # then let the raise escape as a 500.
                if slot.reasoning_effort == effort:
                    slot.reasoning_effort = prior_effort
                slot._dirty = True
                state.push_slots_update()
                raise
            if reset_verdict is None:
                teardown_incomplete = True
            else:
                reset_ok = reset_verdict
            if not reset_ok and not teardown_incomplete:
                # Disambiguate the decline FAIL-CLOSED (the workspace
                # handler's template): live provider mid-turn → roll back and
                # 409; live IDLE provider → retry once; no live provider →
                # nothing to tear down, the next message cold-starts under
                # the new effort.
                busy_provider = state.sessions.get_provider(session_key)
                if isinstance(busy_provider, LLMProvider):
                    if busy_provider.has_active_turn():
                        slot.reasoning_effort = prior_effort
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
                    # Retry through the same probing helper: a pre-pop raise on
                    # the retry propagates (restore + 500) exactly as the first
                    # attempt, and a post-pop raise becomes the committed 200 +
                    # warning below.
                    try:
                        reset_verdict = await _reset_slot_session_or_warn(
                            state,
                            slot,
                            session_key,
                            switch_kind="reasoning_effort",
                        )
                    except Exception:
                        if slot.reasoning_effort == effort:
                            slot.reasoning_effort = prior_effort
                        slot._dirty = True
                        state.push_slots_update()
                        raise
                    if reset_verdict is None:
                        teardown_incomplete = True
                    else:
                        reset_ok = reset_verdict
                    if (
                        not reset_ok
                        and not teardown_incomplete
                        and state.sessions.get_provider(session_key) is not None
                    ):
                        slot.reasoning_effort = prior_effort
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
            if effective_session_key(slot) != session_key:
                # Same check after the reset await as after the live push:
                # the session torn down is no longer the slot's, so the
                # commit would advertise an effort a session that never saw
                # the switch does not run. The teardown itself was harmless
                # (that session was idle and no longer bound); roll back and
                # let the retry resolve the current binding.
                slot.reasoning_effort = prior_effort
                return web.json_response(
                    {
                        "error": "slot session was rebound during the switch",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            if teardown_incomplete:
                normalized = normalize_legacy_model()
                state.push_slots_update()
                return web.json_response(
                    {
                        "ok": True,
                        "reasoning_effort": effort,
                        **normalized,
                        "warning": _TEARDOWN_INCOMPLETE_WARNING,
                    }
                )
        # Live-update and deferral paths commit here (the reset path already
        # committed before its reset, and assigning again is a no-op).
        slot.reasoning_effort = effort
        normalized = normalize_legacy_model()
    state.push_slots_update()
    return web.json_response({"ok": True, "reasoning_effort": effort, **normalized})


async def api_chat_slot_reload(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/reload -- relaunch the slot's agent process.

    A live agent process mounts its MCP servers and builds its tool table once,
    at session-init time; config that changes afterwards (a newly added MCP
    server, an env or agent-spec fix) never reaches it. Reload is the in-place
    remedy: tear the process down exactly like the agent/workspace switch
    handlers do, then eagerly re-arm the resume spawn, so the relaunched
    process re-reads its agent spec and environment and re-initializes MCP
    servers via session/load -- with the conversation preserved.

    The teardown itself is :func:`reload_slot_session`, shared with the
    session-control ``session_reload`` verb; this handler supplies the
    dashboard route's own authorization (slot identity and app isolation).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    def _still_ours(_after_reset: bool) -> bool:
        # Re-authorize after every await: ``name`` can be recreated for a
        # DIFFERENT app while this request queued on a lock (slot removal +
        # re-registration under the same name is how a client reconnects), and
        # the stale ``slot`` object's app-isolation check would then authorize
        # this teardown against the NEW slot's session -- the same
        # cross-slot-identity gap the tags/folders/regenerate handlers close
        # with this exact re-check (e.g. chat_tags.py's ``is not slot`` guard).
        return not _slot_replaced_while_queued(state, slot, name, request, "chat.slot_reload")

    def _denied(session_key: str) -> web.Response | None:
        # App isolation, same policy as the cancel routes: reload is a
        # teardown, so an app token must own both the slot and the session the
        # teardown lands on, and a denial is indistinguishable from a missing
        # slot.
        return _app_cancel_denied(request, slot, "chat.slot_reload", session_key)

    def _busy(session_key: str) -> bool:
        provider = state.sessions.get_provider(session_key)
        return provider is not None and provider.has_active_turn()

    return await reload_slot_session(
        state,
        slot,
        name,
        still_ours=_still_ours,
        denied=_denied,
        busy=_busy,
        notice=_SESSION_RELOAD_NOTICE,
    )


async def reload_slot_session(
    state: DashboardState,
    slot: _ChatSlot,
    name: str,
    *,
    still_ours: Callable[[bool], bool],
    denied: Callable[[str], web.Response | None],
    busy: Callable[[str], bool],
    notice: str,
) -> web.Response:
    """Tear down *slot*'s agent process and re-arm its resume spawn.

    The one reload teardown, shared by ``api_chat_slot_reload`` (the tab menu)
    and ``session_control.reload_target`` (the ``session_reload`` MCP verb), so
    the lock order and the re-checks below cannot drift between the two. The
    transcript is not touched: only the agent session is reset, and one notice
    row (*notice*, tagged ``SESSION_RELOAD_KIND``) is appended afterwards.

    The caller supplies its own authorization as three callbacks:

    * ``still_ours(after_reset)`` -- re-run after every await; False answers
      the byte-identical ``slot_not_found`` 404. ``after_reset`` is True only
      for the re-check that follows ``_reset_slot_session``, so a caller can
      tell "refused, nothing was torn down" from "the process is already
      gone" without inferring the phase from call order. It may also RAISE
      (the session-control verb re-runs its gate here and lets a pre-reset
      refusal propagate); the lock stack still unwinds.
    * ``denied(session_key)`` -- the teardown-target check, run once the
      session key is resolved inside both locks; a response it returns is
      returned as-is.
    * ``busy(session_key)`` -- the fast-path busy probe, refusing with
      ``turn_in_flight``.

    Refused with 409 while a turn is in flight (killing an in-flight ACP
    process orphans the streaming prompt: resume refusals, empty responses)
    and while sub-agent children are attached (their shared runtime is torn
    down with the parent session -- see ``SessionManager.reset`` -- so a
    reload under a working child silently discards its work). The ``busy``
    check is a best-effort fast path; the authoritative guard is the reset's
    skip_if_busy, which evaluates busyness atomically with the session pop
    (see _reset_slot_session for why the unblock half of the chokepoint is
    safe even when the guard declines).
    """
    # Two locks, in the order documented at _slot_switch_session_lock:
    # slot._lock, then the session lock. The four commit-before-reset switch
    # handlers hold both across their commit-then-reset span, and reload joins
    # them so its probe-then-teardown is serialized against that span:
    # reload holds no setting to commit, but it tears the session down
    # the same way, and holding neither lock let a reload land inside a
    # switch's span -- the switch could report success on a session reload had
    # already replaced, or vice versa. An ExitStack because the session lock's
    # KEY is only known after the in-lock read below, and locking on any
    # earlier read could leave this holding the wrong session lock.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above. A mismatch here is
        # indistinguishable from a missing slot.
        if not still_ours(False):
            return slot_not_found()
        # The session the reload will tear down. ``effective_session_key``,
        # never ``_history_key_for``: a channel- or cron-born slot runs its
        # turns under its linked key, and the dashboard-prefixed spelling
        # names a session that never existed -- the reset would "succeed"
        # against nothing while the live process kept its stale config.
        # Resolved INSIDE slot._lock, not before it: a channel/cron rebind can
        # land while this request queues on the lock, so keying the session
        # lock on an earlier read would guard the wrong session (see
        # _slot_switch_session_lock).
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot's switch on this same
        # session, keyed on the value resolved just above -- the same one the
        # probe and reset below use.
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        # Re-authorize again: the session-lock wait above is a SECOND await
        # point (real contention when a switch on the same session holds it),
        # and a slot removal + re-registration under this name can land while
        # this request queued on THAT lock just as easily as on slot._lock
        # above. Without this, the 7396 check would guard only the first
        # await and leave the exact gap it exists to close open on the
        # second.
        if not still_ours(False):
            return slot_not_found()
        # Re-derive rather than trust the captured session_key: it names a
        # MUTABLE attribute (slot.linked_session_key), so a cron/channel
        # rebind landing on the SAME slot object during the session-lock wait
        # changes what effective_session_key(slot) resolves to without
        # tripping the identity check above. Mirrors the switch handlers'
        # own post-lock ``effective_session_key(slot) != session_key`` guard.
        if effective_session_key(slot) != session_key:
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
        refusal = denied(session_key)
        if refusal is not None:
            return refusal
        if busy(session_key):
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        # Children guard, shared with api_chat_slot_continue: RUNNING children
        # die with the parent runtime, and _subagents_attached_response
        # documents why queued children and in-flight deliveries count too.
        denied_409 = await _subagents_attached_response(state, slot, session_key, "reload")
        if denied_409 is not None:
            return denied_409
        if _test_interleave is not None:
            # Reload now holds slot._lock and _slot_switch_session_lock across
            # its probe-then-teardown, so its teardown is serialized against a
            # switch's commit-then-reset span. Suspending here holds that
            # span open across another actor's transaction so a test
            # can observe that the other actor now blocks on the session lock
            # instead of interleaving.
            await _test_interleave("reload:pre_reset")
        # Re-authorize once more before the teardown: the children probe (and
        # the test seam) above are awaits too, and a channel link, mirror or
        # slot replacement landing during them would otherwise reach
        # _reset_slot_session on authorization read before that await. This
        # is the last pre-reset check, so a refusal here still means nothing
        # was torn down.
        if not still_ours(False):
            return slot_not_found()
        teardown_incomplete = False

        async def _reset() -> bool:
            # SessionManager.reset pops the session before its shutdown can
            # raise, so a raise after the pop means the old process is gone and
            # the reload has happened with a degraded teardown. Propagating it
            # would answer 500 with no notice and no respawn for a completed
            # teardown. Same identity probe as _reset_slot_session_or_warn: the
            # SAME provider still registered means the raise came before the
            # pop, nothing was torn down, and the raise propagates.
            nonlocal teardown_incomplete
            prior_provider = state.sessions.get_provider(session_key)
            try:
                return await _reset_slot_session(state, slot, session_key, skip_if_busy=True)
            except Exception:
                if (
                    prior_provider is not None
                    and state.sessions.get_provider(session_key) is prior_provider
                ):
                    raise
                logger.exception("Slot %s reload: old session teardown incomplete", name)
                teardown_incomplete = True
                return True

        reloaded = await _reset()
        if not reloaded:
            provider = state.sessions.get_provider(session_key)
            if provider is not None and provider.has_active_turn():
                return web.json_response(
                    {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                )
            if provider is not None:
                # A turn slipped into the guard window and already FINISHED:
                # the declined reset left a live idle session untouched, and
                # falling through would report success while the stale process
                # survives -- the silent failure this endpoint exists to
                # prevent. Retry once; a second decline means another turn is
                # genuinely racing, which is the turn-in-flight case.
                reloaded = await _reset()
                if not reloaded:
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"},
                        status=409,
                    )
        # Re-check once more, still inside both locks: _reset_slot_session
        # (and its one retry above) is itself an await, and _bind_cron_slot
        # writes slot.linked_session_key with NO lock of its own, so a rebind
        # can land during that specific await just as it can during the
        # earlier lock-acquisition waits. Skipping this would report success
        # while the now-current session was never touched -- the exact silent
        # stale-session failure the two earlier checks exist to prevent, just
        # moved one await later.
        #
        # Identity first, same as the 7399/7422 checks above: registry
        # mutation (slot removal + same-name re-registration for a DIFFERENT
        # app) takes no lock of its own, so it can land during this same
        # await exactly as it can during the two earlier lock-acquisition
        # waits those checks guard. A key-only re-check would still pass for
        # a stale ``slot`` object recreated under app B's name whenever B's
        # session happens to resolve to the same key, and the notice/
        # broadcast below would then fire under B's identity -- the same
        # cross-slot-identity gap the two earlier checks close, just moved to
        # this last await. Same response as those checks: a mismatch here is
        # indistinguishable from a missing slot.
        if not still_ours(True):
            return slot_not_found()
        if effective_session_key(slot) != session_key:
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
    logger.info("Slot %s session reloaded (had_live_session=%s)", name, reloaded)
    # Feed notice: the visible confirmation (and the durable record) that the
    # relaunch happened. Tagged so the last-real-message scans skip it on both
    # sides (is_system_notice here, isSystemNoticeKind on the frontend).
    # append() itself broadcasts the row -- with the per-row ``mid`` identity
    # clients dedupe on -- so an explicit broadcast here would deliver the
    # notice twice.
    slot.append(
        "assistant",
        notice,
        "msg msg-a",
        meta={"kind": SESSION_RELOAD_KIND},
    )
    # Respawn + session/load now rather than on the next message, so the fresh
    # process (and its rebuilt toolset) is ready when the user comes back.
    schedule_eager_spawn(state, slot, allow_resume=True)
    state.push_slots_update()
    if teardown_incomplete:
        return web.json_response({"ok": True, "warning": _TEARDOWN_INCOMPLETE_WARNING})
    return web.json_response({"ok": True})


async def api_chat_slot_workspace(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/workspace — set workspace for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_workspace")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    ws_name = body.get("workspace", "default")
    if not isinstance(ws_name, str):
        # The sibling project handler's guard: with the message-count refusal
        # lifted, this value reaches `default_project_dir` and the slot header
        # on a live conversation, so a non-string is rejected at the boundary.
        return web.json_response(
            {"error": "workspace must be a string", "code": "invalid_workspace"}, status=400
        )
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal
    # Same serialization as the agent switch: the reset await yields the event
    # loop, so the mutate-then-reset section runs under the slot's lock — an
    # unlocked write here would interleave with the agent handler's locked
    # compare-and-set on the same workspace/project fields, and two racing
    # workspace switches could each reset against the other's half-applied
    # state. Commit-before-reset ordering per the agent-handler template: a
    # send landing while the reset await is in flight cold-starts a session
    # from the slot's CURRENT bindings, so the new pair must already be
    # visible.
    # Two locks, in the order documented at _slot_switch_session_lock:
    # slot._lock, then the session lock. An ExitStack because the session
    # lock's KEY is only known after the in-lock read below, and locking on
    # any earlier read could leave this holding the wrong session lock.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above (see _slot_replaced_while_queued):
        # ``name`` can be recreated for a different app while this request
        # queued, and every read of ``slot`` below would be of the stale one.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_workspace"):
            return slot_not_found()
        # The session the reset tears down — effective_session_key, never
        # _history_key_for (see api_chat_slot_model), resolved INSIDE the lock
        # so a binding that lands while this request waits on it is what the
        # reset addresses — with the same session-level app isolation the
        # model handler applies.
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot on this same session.
        # slot._lock is created per _ChatSlot and so is DISJOINT across
        # aliases. Keyed on the value resolved just above -- the same one the
        # probe and reset below use -- so the lock provably guards them even if
        # a binding landed while this request waited on slot._lock (see
        # _slot_switch_session_lock).
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        # Second lock-acquisition await, second re-check: a same-name
        # recreate lands during this wait just as easily as during the first.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_workspace"):
            return slot_not_found()
        denied = _app_cancel_denied(request, slot, "chat.slot_workspace", session_key)
        if denied is not None:
            return denied
        # A started conversation is NOT refused. Such a refusal protects
        # nothing the sibling handlers protect: the transcript and its
        # session key are workspace-independent (the name is a metadata
        # field inside the same history file, never part of its path), so a
        # switch costs the LIVE agent context and nothing persisted -- and
        # `api_chat_slot_agent` already re-points
        # ``slot.workspace`` mid-conversation, with no message-count guard,
        # whenever the picked agent carries different bindings. A transcript
        # marker for the restart is deliberately NOT added here: every sibling
        # reset site would need the same row, and that is one design for all of
        # them, not a rider on this endpoint.
        #
        # Same-value re-pick: nothing to switch, so nothing to tear down.
        # Checked INSIDE the locks (the model handler's ordering): a serialized
        # predecessor targeting this workspace may have committed while this
        # request waited, and resetting again would kill the session that
        # predecessor just set up. Answers the same 200 a real switch does,
        # since the slot IS on the requested workspace.
        if slot.workspace == ws_name:
            return web.json_response({"ok": True, "workspace": ws_name})
        # Children attached to this session run on the runtime the reset
        # below tears down, so refuse rather than discard their work -- the
        # same probe every sibling switch (agent, model, effort, reload)
        # applies. Before the commit, so no rollback is needed.
        children_409 = await _subagents_attached_response(
            state, slot, session_key, "slot_workspace"
        )
        if children_409 is not None:
            return children_409
        # Never tear down an in-flight turn: the model handler's early
        # refusal, shared through _switch_target_busy. The reset
        # below calls _unblock_pending_waits BEFORE SessionManager.reset's
        # atomic busy decline, so without this check a turn parked on a
        # pending approval has that approval rejected and only then gets a
        # 409 -- the turn is altered despite the refusal; and without the
        # helper's third signal a switch through this slot while a sibling
        # alias cold-starts would report success while that turn runs on the
        # old project. The atomic skip_if_busy decline stays as the backstop
        # for a turn that starts after this read.
        pre_provider = state.sessions.get_provider(session_key)
        if _switch_target_busy(state, slot, session_key, pre_provider):
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        prior_workspace = slot.workspace
        prior_project = slot.project
        # Commit as identity tokens (the agent handler's _CommitToken
        # precedent): ``slot.project`` has lock-free writers -- the in-turn
        # set_project directive lands during the reset await -- so a rollback
        # must unwind only the value THIS request wrote, never a concurrent
        # write of a different (or even the same) text.
        committed_workspace = _CommitToken(ws_name)
        committed_project = _CommitToken(default_project_dir(ws_name))
        slot.workspace = committed_workspace
        slot.project = committed_project
        logger.info("Slot %s workspace switched to %r, resetting session", name, ws_name)

        def _rollback() -> None:
            """Unwind this request's commit on every 409 path.

            Identity-scoped per field (see the tokens above), then re-marked
            dirty: the periodic flush runs unlocked every few seconds and may
            already have written the provisional bindings to disk during the
            reset await (a started slot is dirty whenever a turn is active),
            so without the re-mark a rejected switch would survive a restart.
            The flush rebuilds the metadata line from the live fields, so the
            re-mark reconverges disk to whatever the rollback left.
            """
            if slot.workspace is committed_workspace:
                slot.workspace = prior_workspace
            if slot.project is committed_project:
                slot.project = prior_project
            slot._dirty = True

        # skip_if_busy: message dispatch does not take slot._lock, so a send
        # can land while this request holds it. SessionManager.reset evaluates busyness
        # atomically with the session pop (the authoritative backstop
        # api_chat_slot_reload documents), so the slipped-in turn is declined
        # here instead of torn down mid-stream.
        # Set when the old session's teardown RAISED after the pop: the
        # switch is committed, the response carries an advisory warning
        # (agent-handler precedent via _reset_slot_session_or_warn).
        teardown_incomplete = False
        reset_ok = await _reset_slot_session_or_warn(
            state, slot, session_key, switch_kind="workspace"
        )
        if reset_ok is None:
            # Teardown raised after the session pop: the switch is COMMITTED
            # (see _reset_slot_session_or_warn), so the answer is the
            # committed state with an advisory warning — never a 500 that
            # strands clients on the old bindings. Deliberately no rollback
            # of slot.workspace/slot.project: rollback is only for the
            # decline/409 paths, where nothing was torn down. NOT an early
            # return: the rebind guard below must still run, so a slot
            # rebound during the raising await answers the same rollback +
            # 409 as any other rebind.
            teardown_incomplete = True
        elif not reset_ok:
            # Disambiguate FAIL-CLOSED, same as the model handler: dispatch
            # captures the slot bindings at its call site but registers the
            # session only after a multi-second provider.start(), so no
            # identity or registration-time reasoning can prove which
            # bindings a live session carries. A false 409 is retryable; a
            # false success strands a live session on the old bindings.
            busy_provider = state.sessions.get_provider(session_key)
            live_serves_target = False
            if isinstance(busy_provider, AcpProvider) and slot.project:
                # Truth-based check, mirroring the model handler: when the
                # live session's actual working directory already equals the
                # COMMITTED project, the session cold-started on the new
                # bindings (a first send captured them after the commit) —
                # rolling back would advertise the old workspace while the
                # live process runs the new one. Success without teardown is
                # the truthful answer.
                live_serves_target = busy_provider.cwd == slot.project
                if live_serves_target:
                    logger.info(
                        "Slot %s workspace switch: live session already runs under %r; "
                        "declined reset left it in place",
                        name,
                        slot.project,
                    )
            if not live_serves_target and isinstance(busy_provider, LLMProvider):
                if busy_provider.has_active_turn():
                    # Roll back the commit (commit-before-reset means the new
                    # pair is already visible) and answer the same 409 the
                    # guard gives.
                    _rollback()
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                    )
                # A live IDLE session declined the reset. Tearing down an idle
                # session is always safe, so retry once
                # (api_chat_slot_reload's template); a second decline means
                # another turn is genuinely racing.
                reset_ok = await _reset_slot_session_or_warn(
                    state, slot, session_key, switch_kind="workspace"
                )
                if reset_ok is None:
                    # Retry teardown raised: same committed-switch answer as
                    # the first attempt, and same fall-through to the rebind
                    # guard below.
                    teardown_incomplete = True
                elif not reset_ok:
                    _rollback()
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                    )
            # No live provider: no registered session to tear down — the next
            # message cold-starts under the new bindings.
        if effective_session_key(slot) != session_key:
            # The slot was bound to a different session during the reset
            # await(s): the session torn down is no longer the slot's, so the
            # committed bindings would describe a session that never saw the
            # switch. Roll back and answer 409; the retry resolves the
            # current binding.
            _rollback()
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
        # Mark for the periodic flush: the flush writes a slot's metadata
        # line only while ``_dirty`` is set, and nothing else on this path
        # sets it. The switch is allowed on a started conversation, so
        # without the mark a gateway crash before the next message restores
        # the OLD workspace/project over a switch the user saw succeed. The
        # same-value no-op changes nothing, so it does not need this.
        slot._dirty = True
    state.push_slots_update()
    ws_resp: dict = {"ok": True, "workspace": ws_name}
    if teardown_incomplete:
        # Advisory only — the switch itself succeeded and the response
        # carries the committed state (agent-handler precedent).
        ws_resp["warning"] = _TEARDOWN_INCOMPLETE_WARNING
    return web.json_response(ws_resp)


async def api_chat_slot_project(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/project — set project directory for file search scoping."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_project")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_project"):
        return _slot_not_found()
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    project = body.get("project", "")
    if not isinstance(project, str):
        return web.json_response({"error": "project must be a string"}, status=400)
    project = project.strip()
    # Session-level app isolation BEFORE any filesystem probing: the
    # isdir / sensitive-path / voice-runtime checks below answer differently
    # for existing vs missing paths, so running them ahead of the denial
    # would hand an app caller that owns a linked slot an unauthorized
    # filesystem existence oracle. Best-effort read outside the lock — the
    # locked re-check below stays authoritative for a binding that moves
    # while this request waits on the lock.
    denied = _app_cancel_denied(request, slot, "chat.slot_project", effective_session_key(slot))
    if denied is not None:
        return denied
    if project:
        project = os.path.realpath(os.path.expanduser(project))
        if not os.path.isdir(project):
            return web.json_response({"error": "Not a directory"}, status=400)
        if is_sensitive_path(project):
            sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="chat_slot_project",
                outcome="denied",
                resources=f"slot={name} project={project}",
                error="sensitive path",
            )
            return web.json_response({"error": "Access denied"}, status=403)
        # Pre-flight the voice-runtime workspace guard: a
        # workspace that contains (or sits inside) the Kiro Crew data home is
        # refused at agent spawn anyway, but only after the session exists and
        # with a spawn-time stack trace. Reject it here, at the moment of
        # choice, with the same actionable message. Off-loop: the check primes
        # the runtime path cache (mkdir/realpath) on first use.
        conflict = await asyncio.to_thread(voice_runtime_workspace_conflict, project)
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_project"):
            return _slot_not_found()
        if conflict is not None:
            sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="chat_slot_project",
                outcome="denied",
                resources=f"slot={name} project={project}",
                error="voice runtime overlap",
            )
            return web.json_response(
                {"error": conflict, "code": "workspace_overlaps_data_home"},
                status=400,
            )
    # Same serialization as the agent/model/workspace switch handlers: this is
    # the one remaining live HTTP mutator of slot.project (the MCP set_project
    # directive in session_directive_apply also writes it, but from inside the
    # slot's own running turn, where this lock is not an option), and unlocked
    # it could interleave with a locked switch's mutate-then-reset section —
    # the workspace handler's rollback would then erase a project pick that
    # landed during its reset await. The lock only serializes the write; the
    # reset stays DEFERRED via the flag (the killpg constraint below), so no
    # reset is awaited while holding the lock beyond what the other switch
    # handlers already hold.
    async with slot._lock:
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_project"):
            return _slot_not_found()
        # The session the deferred reset will address — ``effective_session_key``,
        # never ``_history_key_for`` (see api_chat_slot_model): a channel- or
        # cron-born slot runs its turns under its linked key, and the
        # dashboard-prefixed spelling names a session that never existed, so
        # the deferred reset would "succeed" against nothing while the live
        # process kept the old CWD. Resolved INSIDE the lock (a binding can
        # land while this request waits on it), and the flag below carries
        # THIS key — the one the app gate authorized — so authorization and
        # action cannot disagree. session_directive_apply's set_project path
        # already resolves the flag the same way.
        session_key = effective_session_key(slot)
        # App isolation on the SESSION, not just the slot (the cancel routes'
        # policy): slot ownership does not imply ownership of a linked
        # channel session, so an app caller may not repoint the project a
        # channel thread runs under. Denied as an indistinguishable 404.
        denied = _app_cancel_denied(request, slot, "chat.slot_project", session_key)
        if denied is not None:
            return denied
        old_project = slot.project
        # _CommitToken (identity-gated rollback), the agent handler's pattern:
        # slot.project has unlocked writers (the in-turn set_project directive
        # writes this field without the lock, and may legitimately write the
        # very project this handler sets). A value compare-and-set rollback
        # cannot tell such a same-text write from this handler's own commit and
        # would erase it; a per-request identity token can.
        committed_project = _CommitToken(project)
        slot.project = committed_project
        logger.info("Slot %s project set to %r", name, project)
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="chat_slot_project",
            outcome="allowed",
            resources=f"slot={name} project={project}",
        )
        # Track recent projects
        if project:
            try:
                await asyncio.to_thread(_save_recent_project, project)
            except Exception:
                logger.warning("Failed to save recent project", exc_info=True)
            # A detached slot cannot arm a reset for its same-name successor.
            if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_project"):
                if slot.project is committed_project:
                    slot.project = old_project
                return _slot_not_found()
        # Reset the session so the next message cold-starts with the new CWD and
        # picks up project-level .kiro/steering/**/*.md (mirrors api_chat_slot_agent).
        # Only on an actual change — avoids a needless cold start on a no-op set.
        #
        # Deferred via a flag because this endpoint is reachable over loopback HTTP
        # from inside the kiro-cli process group (the set_project MCP tool); an
        # inline reset would killpg() the caller. Consumed in chat_runner.
        if project != old_project:
            if effective_session_key(slot) != session_key:
                # The slot was bound to a different session while the
                # recent-project save awaited: arming the flag with the key
                # this request resolved would have the consumer tear down a
                # session nobody is on while the slot's ACTUAL session keeps
                # the old CWD — the exact stale-binding class this handler
                # was converted to remove. Re-resolving here instead is not
                # an option either: it would arm a key the app gate above
                # never authorized. Roll back the commit (identity-gated on
                # the _CommitToken — the in-turn set_project directive writes
                # this field without the lock, and a same-value write must not
                # be mistaken for this handler's own commit) and answer the
                # same 409 the sibling switch handlers use.
                if slot.project is committed_project:
                    slot.project = old_project
                return web.json_response(
                    {
                        "error": "slot session was rebound during the switch",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            slot._pending_reset_history_key = session_key
            # Speculatively re-create the session rooted at the new project so the
            # cwd change is paid during think-time. The eager task consumes the
            # deferred reset itself, but only when no turn is running — the
            # same killpg constraint that deferred the reset applies to it.
            schedule_eager_spawn(state, slot, start_priority=owner_start_priority(request))
    state.push_slots_update()
    return web.json_response({"ok": True, "project": project})


# Fields carried per follow-up item on the wire. Kept explicit so a future
# schema addition has to be added here deliberately rather than leaking
# whatever the model happened to send into the broadcast payload.
_FOLLOWUP_TEXT_FIELDS = ("title", "description", "prompt")


def _redact_followup_item(item: dict) -> dict:
    """Return a display-safe copy of one follow-up item.

    Every string is LLM-authored and renders in the dashboard DOM, so it goes
    through the same credential + exfiltration-URL redaction as chat content
    (mirrors the AskUserQuestion path in chat_runner). ``branch`` is omitted
    when absent so the frontend can fall back to deriving one from the title.
    """
    out: dict[str, str] = {}
    for key in _FOLLOWUP_TEXT_FIELDS:
        text = str(item.get(key) or "")
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
        out[key] = text
    branch = item.get("branch")
    if isinstance(branch, str) and branch:
        # `branch` is LLM-authored too, and it travels further than the text
        # fields: into a git ref, a directory name, SEL records and logs. Run the
        # same redactors, and if either one CHANGES it, drop the field rather than
        # ship a mangled ref — the frontend then derives a branch from the title.
        scrubbed, _ = redact_exfiltration_urls(branch)
        scrubbed, _ = redact_credentials(scrubbed)
        if scrubbed == branch:
            out["branch"] = branch
    return out


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
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

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


async def deny_session_approval_caller(request: web.Request, operation: str) -> web.Response | None:
    """Allow the dashboard owner or an app with the live session approval grant.

    The grant verdict is this request's one read (:func:`session_grant`), shared
    with the per-slot checkpoint that already ran for a per-slot route.
    """
    if request.get("internal_auth") is True:
        return None
    request_app = str(request.get("app") or "")
    if not request_app:
        return deny_non_dashboard_caller(request, operation)

    if await session_grant(request, request_app):
        try:
            sel().log_api_access(
                caller=request_app,
                operation=operation,
                outcome="allowed",
                source="app_isolation",
                resources="permissions.sessionApproval",
            )
        except Exception:  # pragma: no cover - audit is best-effort
            logger.debug("SEL audit failed for %s grant", operation, exc_info=True)
        return None
    try:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            error="session approval permission not granted",
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("SEL audit failed for %s denial", operation, exc_info=True)
    return web.json_response(
        {
            "error": "app cannot manage session approvals",
            "code": "session_approval_not_granted",
        },
        status=403,
    )


async def api_chat_slot_followup(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/followup — show an agent-authored follow-up card.

    Backs the ``suggest_followup`` MCP tool. Reachable over loopback HTTP from
    inside the kiro-cli process group, so the payload is re-validated here
    against the same schema the MCP layer used: this endpoint is a trust
    boundary in its own right, not merely a relay.

    The card is ephemeral (broadcast-only, held in frontend state) and one card
    per slot: a second call replaces an unacted-on card rather than stacking.
    """
    state: DashboardState = request.app["state"]
    denied = deny_non_dashboard_caller(request, "chat_slot_followup")
    if denied is not None:
        return denied
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    try:
        cleaned = validate_tool_args(body, SUGGEST_FOLLOWUP_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    items = [_redact_followup_item(item) for item in cleaned.get("items") or []]
    if not items:
        return web.json_response({"error": "items must not be empty"}, status=400)
    # The card is delivered by broadcast only — nothing is stored server-side —
    # so with no WS client attached the suggestions are dropped on the floor.
    # Report the number of sends that COMPLETED instead of an unconditional
    # success, so the MCP tool can tell the model to restate the follow-ups in
    # its reply text rather than being assured they were shown and steered into
    # silence.
    #
    # This send is AWAITED: a socket count is taken before any send runs, so an
    # owner window that disconnects in that window produced a failed send already
    # reported as delivered.
    #
    # OWNER clients only: an app token can open /api/ws, and an all-clients
    # broadcast would hand it another user's complete handoff prompts.
    try:
        clients = int(
            await state.deliver_ws_owners(
                "followup_card",
                {"slot": slot.key, "items": items, "ts": time.time()},
            )
        )
    except Exception:  # pragma: no cover - defensive: delivery must not 500
        logger.debug("Follow-up card delivery failed", exc_info=True)
        clients = 0
    logger.info(
        "Slot %s follow-up card broadcast with %d item(s) to %d client(s)",
        name,
        len(items),
        clients,
    )
    resp: dict[str, Any] = {"ok": True, "count": len(items), "delivered": clients}
    if not getattr(slot, "project", ""):
        # Parity with session_directive_apply._suggest_followup: the card's
        # worktree button renders disabled for an unscoped slot, and the caller
        # (the MCP relay, and through it the model) must hear that from the
        # delivery path — the tool description alone cannot know this slot.
        resp["warning"] = (
            "this session has no project directory, so the card's 'Start in "
            "new worktree' button is disabled; steer the user to 'Add to this "
            "session' or to scoping a project first"
        )
    return web.json_response(resp)


_MAX_RECENT_PROJECTS = 100


def _recent_projects_path() -> Path:
    return config_dir() / "recent_projects.json"


def _save_recent_project(path: str) -> None:
    """Prepend path to recent projects list (deduped, capped)."""

    fp = _recent_projects_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
    except (json.JSONDecodeError, OSError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    existing = [p for p in existing if p != path]
    existing.insert(0, path)
    existing = existing[:_MAX_RECENT_PROJECTS]
    fd, tmp = tempfile.mkstemp(dir=fp.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            tmp_fh.write(json.dumps(existing))
        os.replace(tmp, fp)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def api_recent_projects(request: web.Request) -> web.Response:
    """GET /api/recent-projects — list recently used project directories."""

    def _read_recent_projects() -> list[str]:
        fp = _recent_projects_path()
        try:
            dirs = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
        except Exception:
            dirs = []
        if not isinstance(dirs, list):
            dirs = []
        return [
            d for d in dirs if isinstance(d, str) and os.path.isdir(d) and not is_sensitive_path(d)
        ]

    dirs = await asyncio.to_thread(_read_recent_projects)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="recent_projects",
        outcome="allowed",
        resources=f"count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})


# Favourites are a SEPARATE file from the recents list, not a flag on its rows, because
# the two have different lifetimes: recents is a rolling window capped at
# _MAX_RECENT_PROJECTS and rewritten on every project switch, so a favourite stored
# there would fall off the end of a busy week. There is no cap: only the dashboard owner
# can write the file (both write verbs are owner-gated), so a bound would have no caller
# to defend against, and a list the user curates by hand should not refuse an entry.
#
# Both write verbs are a read-modify-write of one file, run on worker threads, so two
# overlapping requests (two dashboard tabs) would each write back the snapshot they read
# and one change would be lost. One lock serialises the whole transaction for both.
_favorite_projects_lock = threading.Lock()


def _favorite_projects_path() -> Path:
    return config_dir() / "favorite_projects.json"


class FavoritesUnreadable(Exception):
    """`favorite_projects.json` exists but is not a JSON list we can read."""


def _load_favorite_projects(*, strict: bool = False) -> list[str]:
    """The stored favourites, de-duplicated, with NO filesystem probing.

    This is the list the write paths edit, so it keeps entries whose directory is
    currently unreachable: a favourite on a detached volume or a not-yet-cloned
    worktree must survive a round trip through add or remove rather than being
    deleted as a side effect of someone else's write. `_read_favorite_projects`
    applies the display filter instead.

    An absent file is an empty list. A file that exists but cannot be read or is not
    a JSON list (a hand edit with a typo, a permission change) reads as empty for
    DISPLAY, but `strict=True` raises `FavoritesUnreadable` instead: the write paths
    pass it, because writing back an empty snapshot would silently replace the
    user's whole list, which unlike the recents window cannot be rebuilt.
    """

    fp = _favorite_projects_path()
    if not fp.is_file():
        return []
    try:
        stored = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        if strict:
            raise FavoritesUnreadable(str(exc)) from exc
        return []
    if not isinstance(stored, list):
        if strict:
            raise FavoritesUnreadable("not a JSON list")
        return []
    out: list[str] = []
    for entry in stored:
        if isinstance(entry, str) and entry and entry not in out:
            out.append(entry)
    return out


def _read_favorite_projects() -> list[str]:
    """Favourites for display: existing, non-sensitive directories, stored order kept.

    Insertion order is the answer, not recency or the alphabet -- the list is one the
    user curates by hand, so a favourite stays where they put it. The sensitive-path
    filter matches the recents read: a path that became sensitive after it was
    favourited is withheld from the response, and removing it stays possible because
    the delete path reads `_load_favorite_projects` instead of this.
    """

    return [d for d in _load_favorite_projects() if os.path.isdir(d) and not is_sensitive_path(d)]


def _write_favorite_projects(paths: list[str]) -> None:
    """Replace the favourites file atomically (same temp-file dance as the recents write)."""

    fp = _favorite_projects_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=fp.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            tmp_fh.write(json.dumps(paths))
        os.replace(tmp, fp)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _probe_favorite_candidate(path: str) -> tuple[str, bool, bool]:
    """Resolve `path` and answer (resolved, is a directory, is sensitive).

    One function so the add handler can hand every filesystem probe to a worker thread
    in a single call: `realpath` walks the symlink chain and the sensitivity check
    resolves too, and on a stalled network mount either would freeze the event loop.
    """

    resolved = os.path.realpath(os.path.expanduser(path))
    return resolved, os.path.isdir(resolved), is_sensitive_path(resolved)


def _add_favorite_project(path: str) -> list[str]:
    """Append `path` to the favourites; returns the display list.

    Appends rather than prepends so the order the user built stays put. Already
    favourited is a no-op success -- the button is a toggle whose "on" state can be
    requested twice (two tabs, a double click) and neither caller should see an error
    for a list that already says what they asked it to say.
    """

    with _favorite_projects_lock:
        stored = _load_favorite_projects(strict=True)
        if path not in stored:
            stored.append(path)
            _write_favorite_projects(stored)
    return _read_favorite_projects()


def _remove_favorite_project(path: str) -> list[str]:
    """Drop `path` from the favourites; returns the display list.

    Both the path as given and its resolved form are dropped. The add path stores the
    resolved form, but an entry can sit in the file unresolved -- a hand-edited file, or
    a spelling whose symlink chain has since moved -- and a remove that matched only one
    spelling would leave a row the user cannot get rid of. Missing is a no-op.
    """

    resolved = os.path.realpath(os.path.expanduser(path))
    with _favorite_projects_lock:
        stored = _load_favorite_projects(strict=True)
        kept = [p for p in stored if p != path and p != resolved]
        if len(kept) != len(stored):
            _write_favorite_projects(kept)
    return _read_favorite_projects()


def _favorites_unreadable_response() -> web.Response:
    """409 for a write refused because the stored list could not be read.

    Refused rather than written: the write would replace a list the user may have
    spent months curating with a single entry. The message names the file so the
    user can repair it, and nothing on disk is touched.
    """

    return web.json_response(
        {
            "error": (
                f"{_favorite_projects_path().name} could not be read, so it was left "
                "untouched. Fix or remove it, then try again."
            ),
            "code": "favorites_unreadable",
        },
        status=409,
    )


async def api_favorite_projects(request: web.Request) -> web.Response:
    """GET /api/favorite-projects — list the favourited project directories."""

    dirs = await asyncio.to_thread(_read_favorite_projects)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="favorite_projects",
        outcome="allowed",
        resources=f"count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})


async def api_favorite_project_add(request: web.Request) -> web.Response:
    """POST /api/favorite-projects — favourite one project directory.

    Validated the way setting a slot's project is (`api_chat_slot_project`): a real
    directory, resolved, and not a sensitive path. Storing the resolved form is what
    lets the picker's star compare a row against this list by string.

    Owner-gated, unlike the GET beside it: the favourites file is the owner's own
    config, so an installed app whose manifest covers this path may read the list but
    must not edit it. The gate goes BEFORE the body is read and before any filesystem
    probe, so a refused caller gets no directory-existence oracle out of it.
    """

    denied = deny_non_dashboard_caller(request, "favorite_project_add")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    path = body.get("path", "")
    if not isinstance(path, str):
        return web.json_response(
            {"error": "path must be a string", "code": "path_not_a_string"}, status=400
        )
    path = path.strip()
    if not path:
        return web.json_response({"error": "path is required", "code": "path_required"}, status=400)
    path, is_dir, sensitive = await asyncio.to_thread(_probe_favorite_candidate, path)
    if not is_dir:
        return web.json_response(
            {"error": "Not a directory", "code": "not_a_directory"}, status=400
        )
    if sensitive:
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="favorite_project_add",
            outcome="denied",
            resources=f"path={path}",
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied", "code": "denied"}, status=403)
    try:
        dirs = await asyncio.to_thread(_add_favorite_project, path)
    except FavoritesUnreadable as exc:
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="favorite_project_add",
            outcome="denied",
            resources=f"path={path}",
            error=f"favorites file unreadable: {exc}",
        )
        return _favorites_unreadable_response()
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="favorite_project_add",
        outcome="allowed",
        resources=f"path={path} count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})


async def api_favorite_project_remove(request: web.Request) -> web.Response:
    """DELETE /api/favorite-projects?path=… — un-favourite one project directory.

    The path comes from the query string, not a body, because a DELETE body is not
    carried reliably by every client. None of the add path's filesystem checks run
    here: a favourite whose directory was deleted, or which became sensitive after it
    was stored, is exactly the row a user needs to be able to remove, and demanding it
    still be a listable directory would strand it.

    Owner-gated for the same reason as the add above.
    """

    denied = deny_non_dashboard_caller(request, "favorite_project_remove")
    if denied is not None:
        return denied
    path = (request.query.get("path") or "").strip()
    if not path:
        return web.json_response({"error": "path is required", "code": "path_required"}, status=400)
    try:
        dirs = await asyncio.to_thread(_remove_favorite_project, path)
    except FavoritesUnreadable as exc:
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="favorite_project_remove",
            outcome="denied",
            resources=f"path={path}",
            error=f"favorites file unreadable: {exc}",
        )
        return _favorites_unreadable_response()
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="favorite_project_remove",
        outcome="allowed",
        resources=f"path={path} count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})


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


async def _app_resume_refusal(
    state: "DashboardState", request_app: str, name: str, history_key: str, meta: dict
) -> ResumeRefusal | None:
    """Whether an app may resume *history_key* into a slot published as *name*.

    Two transcripts are at stake, and both must be the app's. The one READ is
    *history_key*, whose metadata line *meta* must record this app. The one the
    new slot WRITES is ``dashboard:<name>``, because a resumed slot saves under
    its own key: when that is a different key, the same acquisition rule as
    send and create applies to it (no member, cron or workflow name; no
    transcript there that records anyone else). Without the second check an app
    could load its own transcript under the name of a person's closed session
    and be handed that session's live slot.

    A publish name under construction is the same 404, with no audit row, as on
    send and create: a session being imported is nobody's to name yet. So is one
    that aliases another live slot by letter case, recorded as on send and create.
    """
    log = state.conversation_log
    if not app_owns_transcript_meta(meta, request_app):
        # Re-read to file the refusal under its real reason: an unreadable line
        # is a read fault, not an isolation breach, and no transcript at all is a
        # missing session, which is answered without an audit row.
        reason = ""
        if log is not None:
            reason = await asyncio.to_thread(
                transcript_acquisition_reason, log, history_key, request_app
            )
        if reason:
            audit_app_slot_denial(request_app, "slot_resume", name, reason)
        return _RESUME_APP_NOT_FOUND
    publish_key = _history_key_for(name)
    refused, reason = app_new_key_refusal(state, name, publish_key)
    if not refused and publish_key != history_key and log is not None:
        reason = await asyncio.to_thread(
            transcript_acquisition_reason, log, publish_key, request_app
        )
        refused = bool(reason)
    if reason:
        audit_app_slot_denial(request_app, "slot_resume", name, reason)
    return _RESUME_APP_NOT_FOUND if refused else None


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


async def _end_trust_scopes(slots: list[Any], audit_caller: Callable[[str], str]) -> None:
    """End the app-armed scoped grants these slots carry, when a person picks Normal or Reads.

    The header shows a live ``_trust_scope`` as Trust, so any choice narrower
    than Trust must end that grant too, or the slot keeps auto-approving writes
    under the narrower label: ``SafetyOverride.deactivate()`` only ends the
    process-wide override and never touches a scoped grant. The SEL record names
    the person's choice as the cause, apart from the grant's own
    ``deactivate_scope`` record.

    Only the grants: the caller clears every slot flag and stored policy after
    this returns, in one pass with no await, so a concurrent mode change cannot
    land between a slot's flags and its session's policy.
    """
    for slot in slots:
        scope = str(getattr(slot, "_trust_scope", "") or "")
        if scope:
            await _end_trust_scope(scope, audit_caller(f"dashboard:{slot.key}"))


def _still_owning(state: Any, slots: list[Any], key: str) -> list[Any]:
    """The slots from ``slots`` that are still live in ``state`` and still address ``key``.

    A revoke resolves its slots before awaiting the grant ends; a slot closed and
    re-created on the same key during that await is a different slot that the
    request was never authorized for, so its flags and the session policy stay.
    """
    return [s for s in slots if state._slots.get(s.key) is s and effective_session_key(s) == key]


async def _end_trust_scope(scope: str, caller: str) -> None:
    await asyncio.to_thread(safety_override().deactivate_scope, scope)
    try:
        await asyncio.to_thread(
            sel().log_api_access,
            caller=caller,
            operation="approval_mode.scope_cleared_by_user",
            outcome="disabled",
            resources=f"scope:{scope}",
        )
    except Exception:
        logger.warning("SEL audit failed for scope clear on %s", scope, exc_info=True)


async def api_chat_mode(request: web.Request) -> web.Response:
    """POST /api/chat/mode — set tool approval mode.

    Modes:
      - ``normal``: reset to interactive (ask for each tool)
      - ``trust_reads``: auto-approve reads for active slot
      - ``trust``: auto-approve tools for active slot
      - ``yolo``: auto-approve all tools everywhere

    Unlike the per-tool approve endpoint, this doesn't require a
    pending approval — it preemptively sets the mode for future tools.

    A caller that presents an attested ``cron:<job id>`` key, which is what
    ``ScriptContext.set_session_mode`` does, is held to ``trust`` and
    ``trust_reads`` on one named slot that same cron created: the switch mirror
    in ``private_chat_route_refusal`` answers first while session control is
    off, ``cron_mode_refusal`` refuses every other mode and an unnamed slot, and
    ``cron_creator_admission`` refuses a slot another creator made. An admitted
    call is recorded as ``mode_change:<mode>`` under the cron's own key. Each
    refusal is recorded by ``_audit_cron_chat_denial`` as a ``chat.control``
    denial attributed to ``internal``, naming the route, the slot and the mode,
    the shape every cron-gate refusal on the chat routes shares.
    """
    state: DashboardState = request.app["state"]
    denied = await deny_session_approval_caller(request, "chat_mode")
    if denied is not None:
        return denied
    request_app = str(request.get("app") or "")
    # The attested ``cron:<job id>`` key of a script cron, or ``""``. A cron sets
    # one slot-scoped posture on a session it created and nothing else; the three
    # checks below are the cron gate the other two chat routes already apply, and
    # like those they key on the attested key the caller presents.
    cron_creator = await cron_slot_creator(request)

    def audit_caller(dashboard_label: str) -> str:
        """App tokens are attributed to the app, a cron to its own key; dashboard
        callers keep their original per-site labels (slot, background, mode) so
        SEL history stays comparable across releases."""
        if request_app:
            return f"app:{request_app}"
        return cron_creator or dashboard_label

    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    mode = body.get("mode", "normal")
    # A cron is held to ``trust`` / ``trust_reads`` on one named slot, before the
    # mode reaches governance or the override: its ``yolo`` is refused as a cron's,
    # whatever the policy says, and its unnamed slot is never the all-slots request.
    cron_refused = await cron_mode_refusal(request, cron_creator, mode, body.get("slot"))
    if cron_refused is not None:
        return cron_refused
    # The grant is per-slot: Normal, Reads and Trust on ONE named user session.
    # YOLO is process-global and stays dashboard-only.
    if request_app and mode == "yolo":
        return _deny_app_yolo(request_app, "chat_mode:yolo")
    # Governance gate: the ``approval_modes`` policy scope governs ``yolo`` and
    # only ``yolo``. Refuse a denied mode here, before any mutation, so it is
    # blocked regardless of the UI. ``normal`` is the interactive floor, and
    # ``trust`` / ``trust_reads`` are non-deniable because their live consumption
    # predicates are not gated -- ``approval_mode_permitted`` short-circuits all
    # three, and a policy naming one is refused at parse time. Kept as a general
    # mode check rather than a YOLO special case so that widening the scope needs
    # no change here; YOLO is additionally guarded at arming in ``safety_override``.
    #
    # ``yolo`` reads the PUSHED verdict, which is resolved when a ceiling is installed
    # and so needs no thread: there is one answer for the ceiling in force, and a
    # governance-evaluation error resolved to a deny at that install. Every other mode
    # is non-deniable and short-circuits inside ``approval_mode_permitted`` -- but an
    # unrecognised ``mode`` string does reach governance, so that branch keeps its
    # offload.
    if mode == "yolo":
        if not yolo_policy_permits():
            return _deny_approval_mode(
                caller=audit_caller("dashboard:chat_mode"),
                operation=f"chat_mode:{mode}",
                mode=mode,
                resource=str(body.get("slot") or ""),
            )
    elif not await asyncio.to_thread(approval_mode_permitted, mode):
        return _deny_approval_mode(
            caller=audit_caller("dashboard:chat_mode"),
            operation=f"chat_mode:{mode}",
            mode=mode,
            resource=str(body.get("slot") or ""),
        )
    raw_slot = body.get("slot")
    slot_key = raw_slot or None
    # Test the NORMALIZED value: ``""`` (and any other falsy slot) collapses to
    # ``None`` below, which is the all-slots path -- so an app sending an empty
    # string must be refused exactly like one sending no slot at all.
    if request_app and slot_key is None:
        return web.json_response(
            {
                "ok": False,
                "error": "app mode changes require a slot",
                "code": "slot_required",
            },
            status=400,
        )

    # Refuse an unresolvable slot key BEFORE anything mutates: a slot-scoped
    # request that names a slot which does not exist — or which is not a string
    # at all — must neither widen to every slot nor revoke the global
    # grant, and its refusal must leave grant and slots exactly as they were.
    # Falsy non-strings (``[]``, ``{}``, ``0``, ``False``) are refused on the
    # raw value here, before ``raw_slot or None`` can erase them into the
    # documented all-slots request. The resolved slot reference is what every
    # branch writes through — nothing below re-indexes state._slots[slot_key]
    # after the offloaded deactivate await, so a concurrent slot deletion
    # cannot open a check/use gap. ``yolo`` is global and ignores ``slot``
    # entirely (a stale key must not refuse it).
    slot, denied = None, None
    if mode != "yolo":
        if raw_slot is not None and not isinstance(raw_slot, str):
            denied = web.json_response({"ok": False, "error": "unknown slot"}, status=400)
        elif slot_key is not None:
            # An absent key is the documented "all slots" request; a present
            # key must name a live slot or the whole request is refused here,
            # before any mutation.
            slot = state._slots.get(slot_key)
            if slot is None:
                denied = web.json_response({"ok": False, "error": "unknown slot"}, status=400)
    if denied is not None:
        # An app gets the 404 a slot it may not control gets, so this answer
        # cannot tell it which session names are live.
        return slot_not_found() if request_app else denied

    if cron_creator:
        # The mode rule above refused an unnamed slot and the resolution above
        # refused a dead one, so a cron reaches here with ONE live slot. Judge
        # its creator now, with no await between that resolution and this read,
        # so the fence judges the same slot object every branch below writes
        # through. A slot the cron did not create answers 403 ``not_creator``.
        assert slot is not None  # a cron names a slot, and it resolved above
        refusal, _, _ = await cron_creator_admission(request, state, slot_key, cron_creator)
        if refusal is not None:
            return refusal

    if request_app:
        assert slot is not None  # app requests require and resolve a slot above
        # A FRESH grant read: the body upload and the governance check above
        # can outlast a removal of the flag since the caller check read it.
        denied = await deny_app_session_control(
            request, request_app, slot, str(slot_key), "chat_mode", fresh=True
        )
        if denied is not None:
            return denied

    return await apply_approval_mode(
        state,
        mode=mode,
        slot=slot,
        slot_key=slot_key,
        request_app=request_app,
        cron_creator=cron_creator,
        audit_caller=audit_caller,
    )


async def apply_approval_mode(
    state: DashboardState,
    *,
    mode: str,
    slot: _ChatSlot | None,
    slot_key: str | None,
    request_app: str,
    cron_creator: str,
    audit_caller: Callable[[str], str],
) -> web.Response:
    """Apply an approval mode to ONE resolved slot (or every slot when ``slot`` is None).

    The mutation half of ``POST /api/chat/mode``, after every caller check has
    passed. Shared with the crewmate profile write (``PUT /api/agents/{name}``),
    which applies the stored permission to the crewmate's live DM slot through
    this same path rather than a copy of it.
    """
    # The safety override (YOLO) is PROCESS-GLOBAL while an approval mode is
    # per-slot, so revoking it on behalf of a request that named ONE slot drops
    # every OTHER slot out of YOLO too. That is how a programmatic per-slot
    # `trust` — the call an automation makes when it creates a session — silently
    # ends an operator's live grant minutes after they enabled it.
    #
    # A slot-scoped `trust`/`trust_reads` therefore leaves the grant alone: it
    # asks for auto-approval on one slot and cannot be answered by withdrawing
    # authority elsewhere. Everything else still revokes, so `normal` remains the
    # off-switch at any scope and the dashboard picker (which always names its own
    # slot) keeps working.
    #
    # A grant DECLARED in owner-only config is exempt from the narrowing: it has
    # no TTL, and selecting another approval mode is the one action documented to
    # end it. Identity is the grant's source, never its permanence — an
    # `until_shutdown` ad-hoc pick is equally permanent and must stay protected.
    #
    # An app token never touches the global override: its ``normal`` on one
    # slot must not end the operator's YOLO grant on every other slot.
    #
    # Nor does a ``cron:`` caller. The mode rule above already holds it to a
    # slot-scoped ``trust`` or ``trust_reads``, and ``set_session_mode`` promises
    # that both leave the global override alone, declared or not: a scheduled run
    # asking for auto-approval on the one session it opened is never the
    # documented action that ends the operator's grant everywhere else.
    slot_scoped_trust = slot_key is not None and mode in _SLOT_SCOPED_TRUST_MODES
    if (
        not request_app
        and not cron_creator
        and mode != "yolo"
        and (not slot_scoped_trust or safety_override().is_declared)
    ):
        # deactivate() writes a SEL event, so it is offloaded exactly like the
        # sibling activate() — never run on the gateway loop. Safe after
        # the resolution above: every branch mutates the captured slot, never
        # re-indexing state._slots.
        await asyncio.to_thread(safety_override().deactivate, "dashboard")

    if mode == "yolo":
        result = await asyncio.to_thread(safety_override().activate, "dashboard")
        if not result.active:
            # Arming can be refused for two reasons and the client needs to tell
            # them apart: an ``approval_modes`` deny of ``yolo`` is a permanent
            # policy answer (403, same code the picker already understands),
            # while anything else is a transient activation failure (503).
            if not yolo_policy_permits():
                return _deny_approval_mode(
                    caller=audit_caller("dashboard:chat_mode"),
                    operation="mode_change:yolo",
                    mode="yolo",
                    resource=slot_key or "",
                )
            return web.json_response(
                {"ok": False, "error": "safety override activation refused"},
                status=503,
            )
        try:
            sel().log_api_access(
                caller=audit_caller("dashboard:mode"),
                operation="mode_change:yolo",
                outcome="enabled",
                resources=",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for YOLO mode activation", exc_info=True)
    elif mode == "trust_reads":
        if slot is not None:
            _reads_key = effective_session_key(slot)
            _reads_sharing = [
                s for s in list(state._slots.values()) if effective_session_key(s) == _reads_key
            ]
            await _end_trust_scopes(_reads_sharing, audit_caller)
            _reads_live = _still_owning(state, _reads_sharing, _reads_key)
            for _sharing in _reads_live:
                _sharing._trust = False
                _sharing._trust_reads = True
                _sharing._trust_scope = ""
            if _reads_live:
                state.sessions.set_approval_policy(_reads_key, "")
        else:
            await _end_trust_scopes(list(state._slots.values()), audit_caller)
            for s in state._slots.values():
                s._trust = False
                s._trust_reads = True
                s._trust_scope = ""
                state.sessions.set_approval_policy(effective_session_key(s), "")
        try:
            sel().log_api_access(
                caller=audit_caller("dashboard:mode"),
                operation="mode_change:trust_reads",
                outcome="enabled",
                resources=slot_key or ",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for trust_reads mode activation", exc_info=True)
    elif mode == "trust":
        mgr = getattr(state, "channel_manager", None)
        if slot is not None:
            # Every slot that SHARES the session, matching the revoke below. The
            # policy is per session while the flag is per slot, so setting one of
            # two sharing slots leaves them disagreeing about a session they both
            # address, and the propagation pass would then be decided by slot
            # iteration order rather than by what the operator asked for.
            _granted_key = effective_session_key(slot)
            for _sharing in state._slots.values():
                if effective_session_key(_sharing) == _granted_key:
                    _sharing._trust = True
            state.sessions.set_approval_policy(_granted_key, "auto")
            linked_ch = getattr(slot, "_slack_channel", None)
            if (
                not request_app
                and not cron_creator
                and mgr
                and linked_ch
                and linked_ch in mgr._channels
            ):
                mgr._channels[linked_ch].trusted = True
                mgr._channels[linked_ch]._save()
        else:
            for s in state._slots.values():
                s._trust = True
                state.sessions.set_approval_policy(effective_session_key(s), "auto")
            if mgr:
                for ch in mgr._channels.values():
                    ch.trusted = True
                    ch._save()
        _trusted_chs = (
            [cid for cid, ch in mgr._channels.items() if ch.trusted]
            if mgr and not request_app and not cron_creator
            else []
        )
        try:
            _res = slot_key or ",".join(s.key for s in state._slots.values())
            if _trusted_chs:
                _res += "|channels:" + ",".join(_trusted_chs)
            sel().log_api_access(
                caller=audit_caller("dashboard:mode"),
                operation="mode_change:trust",
                outcome="enabled",
                resources=_res,
            )
        except Exception:
            logger.warning("SEL audit failed for trust mode activation", exc_info=True)
    else:  # normal
        mgr = getattr(state, "channel_manager", None)
        if slot is not None:
            # Several slots can address ONE session (a rehydrated owner slot and
            # the alias its turns run under both resolve to the same effective
            # key), so revoking the selected slot alone leaves the others holding
            # a stale `_trust`, and the propagation below then rewrites the shared
            # session back to "auto" from it. The policy is per SESSION; the flag
            # is per slot; so the revoke has to clear every slot that shares it.
            _revoked_key = effective_session_key(slot)
            _revoked = [
                s for s in list(state._slots.values()) if effective_session_key(s) == _revoked_key
            ]
            await _end_trust_scopes(_revoked, audit_caller)
            _revoked_live = _still_owning(state, _revoked, _revoked_key)
            for _sharing in _revoked_live:
                _sharing._trust = False
                _sharing._trust_reads = False
                _sharing._trust_scope = ""
            if _revoked_live:
                state.sessions.set_approval_policy(_revoked_key, "")
            linked_ch = getattr(slot, "_slack_channel", None)
            if (
                slot in _revoked_live
                and not request_app
                and mgr
                and linked_ch
                and linked_ch in mgr._channels
            ):
                mgr._channels[linked_ch].trusted = False
                mgr._channels[linked_ch]._save()
        else:
            await _end_trust_scopes(list(state._slots.values()), audit_caller)
            for s in state._slots.values():
                s._trust = False
                s._trust_reads = False
                s._trust_scope = ""
                state.sessions.set_approval_policy(effective_session_key(s), "")
            if mgr:
                for ch in mgr._channels.values():
                    ch.trusted = False
                    ch._save()
        try:
            sel().log_api_access(
                caller=audit_caller("dashboard:mode"),
                operation="mode_change:normal",
                outcome="disabled",
                resources=slot_key or ",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for normal mode activation", exc_info=True)

    # If any slot has a pending approval and mode is trust/yolo, auto-approve it
    if mode in ("trust", "yolo"):
        # A slot-scoped ``trust`` grants auto-approval to ONE session only (the
        # target slot and any slot sharing its effective key). The pending-approval
        # sweep MUST honour that scope: sweeping every slot's pending prompt would
        # clear the approval card in unrelated chats — making them LOOK approved —
        # while their ``_trust`` flag stays False, so their very next tool call
        # prompts again. ``yolo`` is process-global and an unscoped ``trust`` (no
        # slot named = the documented all-slots request) still sweeps everything,
        # including background and channel approvals. Only a slot-scoped ``trust``
        # narrows.
        scoped = mode == "trust" and slot is not None
        _target_key = effective_session_key(slot) if slot is not None and scoped else None
        for _slot in state._slots.values():
            if scoped and effective_session_key(_slot) != _target_key:
                continue
            for aid, fut in list(_slot._approval_futures.items()):
                if not fut.done():
                    # Read while still pending: names the row the frame settles.
                    _mid = _slot.approval_instance(aid) or ""
                    fut.set_result("approved")
                    # Persist resolved state into the permission message. The
                    # periodic flush skips non-dirty slots, so the mark must
                    # flag the slot or the write can be lost on restart.
                    if _mark_permission_resolved(_slot.messages, aid, mode):
                        _slot._dirty = True
                    # ``slot`` keys the frame for the slot-scoped WS gate — an
                    # app token cannot receive its own resolution without it.
                    state.broadcast_ws(
                        "approval_resolved",
                        {
                            "id": aid,
                            "approved": True,
                            "slot": _slot.key,
                            **native_resolution_target(_mid),
                        },
                    )
                    try:
                        sel().log_api_access(
                            caller=audit_caller(f"dashboard:{_slot.key}"),
                            operation=f"tool_approval:bulk_{mode}",
                            outcome="approved",
                            resources=aid,
                        )
                    except Exception:
                        logger.warning("SEL audit failed for bulk approval %s", aid, exc_info=True)
        # Background (cron/subagent/taskrunner) and channel approvals are NOT
        # slot-scoped, so a slot-scoped ``trust`` must leave them pending — it
        # asked for auto-approval on one session and cannot answer for unrelated
        # background work. Only ``yolo`` and an all-slots ``trust`` sweep them.
        if not scoped:
            for aid in list(state._approval_futures):
                fut = state._approval_futures[aid]
                if not fut.done():
                    state.resolve_approval(aid, True)
                    try:
                        sel().log_api_access(
                            caller=audit_caller("dashboard:background"),
                            operation=f"tool_approval:bulk_{mode}",
                            outcome="approved",
                            resources=aid,
                        )
                    except Exception:
                        logger.warning("SEL audit failed for bulk approval %s", aid, exc_info=True)
            # Auto-approve pending channel approvals
            mgr = getattr(state, "channel_manager", None)
            if mgr:
                for ch in mgr._channels.values():
                    for agent in ch.members.values():
                        fut = agent._approval_future
                        if fut and not fut.done():
                            fut.set_result("approved")
                            try:
                                sel().log_api_access(
                                    caller=f"channel:{ch.id}:{agent.agent_name}",
                                    operation=f"tool_approval:bulk_{mode}",
                                    outcome="approved",
                                    resources=getattr(fut, "_approval_id", "unknown"),
                                )
                            except Exception:
                                logger.warning(
                                    "SEL audit failed for channel bulk approval",
                                    exc_info=True,
                                )

    # Propagate trust/yolo to session approval policies so subagents inherit.
    #
    # Keyed by ``effective_session_key`` — the SAME derivation every grant above
    # and the approval-card grants in ``api_chat_slot_approve`` use — because a
    # grant and its revoke must address one key. A channel-surfaced or cron-born
    # slot runs its turns under ``linked_session_key``, which is what
    # ``messaging.approval.TextApprovalDecider.trusted()`` reads, so keying by
    # the slot name writes a session nobody consults and leaves the live one
    # holding whatever it was last granted: an un-revokable auto-approve.
    # Safe as a per-slot write ONLY because both branches above apply their change
    # to every slot sharing a session, so two slots addressing one key always agree
    # by the time this runs and iteration order cannot pick a winner.
    for slot in state._slots.values():
        policy = "auto" if slot._trust or safety_override().is_active() else ""
        state.sessions.set_approval_policy(effective_session_key(slot), policy)

    state.push_slots_update()
    return web.json_response({"ok": True, "mode": mode})


def _get_pattern_from_pending(slot: _ChatSlot, request_id: str, field: str) -> str:
    """Extract a pattern field from the permission message matching request_id."""
    if not request_id:
        return ""
    for msg in reversed(slot.messages):
        if msg.get("role") == "permission" and msg.get("cls"):
            try:
                meta = json.loads(msg["cls"])
                if not isinstance(meta, dict):
                    continue
                if meta.get("request_id") == request_id:
                    return meta.get(field, "")
            except (json.JSONDecodeError, TypeError):
                continue
    return ""


def _deny_approval_mode(
    *,
    caller: str,
    operation: str,
    mode: str,
    resource: str = "",
) -> web.Response:
    """Refuse a policy-denied approval mode, audited, WITHOUT any mutation.

    One helper for every surface that can arm an auto-approve mode, so a refusal
    always lands in the security event log. A governance refusal that leaves no
    trace is indistinguishable from the request never having been made, which is
    exactly the record an operator needs after an attempted escalation. The audit
    is best-effort: an SEL write failure must not turn a refusal into a grant.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="approval_mode_denied_by_policy",
            resources=resource or mode,
            error="mode_disabled_by_policy",
        )
    except Exception:
        logger.warning("SEL audit failed for policy-refused approval mode %s", mode, exc_info=True)
    return web.json_response(
        {
            "ok": False,
            "error": f"approval mode {mode!r} is disabled by your organization's policy",
            "code": "mode_disabled_by_policy",
            "mode": mode,
        },
        status=403,
    )


def _deny_trust_pattern(name: str, request_id: str, action: str, code: str) -> web.Response:
    """Refuse and audit a command-scoped trust grant without resolving it."""
    try:
        sel().log_api_access(
            caller=f"dashboard:{name}",
            operation=f"tool_approval:{action}",
            outcome="trust_pattern_denied",
            resources=request_id,
            error=code,
        )
    except Exception:
        logger.warning("SEL audit failed for refused trust grant %s", request_id, exc_info=True)
    errors = {
        "pattern_required": "pattern required for command-scoped trust",
        "pattern_underivable": "the pending tool has no grantable command scope",
        "approval_superseded": "pattern does not match the pending command",
        "approval_not_slot_owned": "command-scoped trust requires a live slot approval",
    }
    return web.json_response({"error": errors[code], "code": code}, status=400)


# What a strict native decide may carry. Trust verbs are included so a pending
# card's standing grant is bound to the exact request (``request_mid``) the card
# showed; ``yolo`` widens the whole session rather than one request and stays
# off the strict path.
_STRICT_NATIVE_ACTIONS = frozenset(
    {"approved", "rejected", "rejected_once", "trust", "trust_reads", "trust_command", "trust_base"}
)


async def api_chat_slot_approve(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/approve — resolve a pending tool approval."""
    state: DashboardState = request.app["state"]
    denied = await deny_session_approval_caller(request, "chat_slot_approve")
    if denied is not None:
        return denied
    request_app = str(request.get("app") or "")
    name = request.match_info["slot"]

    def audit_caller() -> str:
        return f"app:{request_app}" if request_app else f"dashboard:{name}"

    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    action = body.get("action", "rejected")
    original_action = action
    request_id = body.get("request_id", "")
    if request_app and original_action == "yolo":
        return _deny_app_yolo(request_app, "tool_approval:yolo")
    # A FRESH grant read: the body upload above can take long enough for the
    # flag to have been removed from the manifest since the checkpoint read it.
    denied = await deny_app_session_control(
        request, request_app, slot, name, "chat_slot_approve", fresh=True
    )
    if denied is not None:
        return denied
    strict_native = "origin" in body
    if strict_native:
        request_mid = body.get("request_mid")
        if (
            body["origin"] != "native"
            or not isinstance(request_id, str)
            or not request_id
            or not isinstance(request_mid, str)
            or not request_mid
            # A non-string action (a list, an object) is unhashable: test the
            # type first so it is refused as a bad target, not raised as a 500.
            or not isinstance(action, str)
            or action not in _STRICT_NATIVE_ACTIONS
        ):
            return web.json_response(
                {"error": "invalid approval target", "code": "invalid_approval_target"}, status=400
            )
    # Locate the slot that OWNS the pending approval future. It is usually the
    # addressed slot, but under session-sharing or a rehydrated/replaced slot the
    # future can live on a different slot object under a different key. All
    # slot-scoped side-effects (trust flags, trusted patterns, approval policy)
    # and the resolved outcome MUST land on the OWNER slot — the one whose
    # session loop consumes the future and gates subsequent tools — or the trust
    # opt-in silently fails on the running session while the UI reports success.
    owner = slot
    if request_id:
        fut = slot._approval_futures.get(request_id)
        if not fut or fut.done():
            # The future can live on a DIFFERENT slot object only under
            # session-sharing / rehydration — i.e. a slot that resolves to the
            # SAME session identity as the addressed one. ACP request_ids are
            # connection-scoped and can collide across unrelated sessions, so a
            # bare id-match scan could approve (and, for trust, auto-approve) an
            # unrelated slot's pending tool. Guard the scan on session identity:
            # only a candidate whose effective session key equals the addressed
            # slot's is a legitimate owner.
            want_session = effective_session_key(slot)
            for s in state._slots.values():
                cand = s._approval_futures.get(request_id)
                if not cand or cand.done():
                    continue
                cand_session = effective_session_key(s)
                if cand_session != want_session:
                    continue
                owner, fut = s, cand
                break
    else:
        pending = [(k, f) for k, f in slot._approval_futures.items() if not f.done()]
        if len(pending) == 1:
            request_id, fut = pending[0]
        else:
            fut = None
    if request_app and owner is not slot:
        denied = await deny_app_session_control(
            request, request_app, owner, owner.key, "chat_slot_approve"
        )
        if denied is not None:
            return denied
    if strict_native and (
        not fut or fut.done() or owner.approval_instance(request_id) != body.get("request_mid")
    ):
        # A strict native decide names the runner request its card showed: the
        # owner's live future under that id must still carry the row's mid, so
        # a stale card never selects a same-id coordinator approval or a later
        # request that reused the id. The owner is the addressed slot or one
        # sharing its session identity, as above.
        return web.json_response(
            {"error": "no pending approval", "code": "approval_not_pending"}, status=404
        )
    if request_app and (not fut or fut.done()):
        # No slot-level future means the id names (at most) a STATE-level
        # approval. Those are raised only by background sources -- cron,
        # autonudge, subagent, taskrunner -- and merely parked in the user's
        # tab, so the grant, which reaches the user's own session only, never
        # resolves one. The user's own tool prompts live on the slot future
        # handled above. Same 404 as any other out-of-scope target.
        return slot_not_found()
    # A state-level approval carries only a boolean decision and has no owning
    # slot, canonical command card, or scoped-pattern store.  Do not let a
    # durable-trust action fall through to ``resolve_state_approval`` as ``True``:
    # that would approve the tool after skipping every scope check.  Truly
    # missing IDs retain the 404 from the common fallback below; this explicit
    # denial covers a live state owner.
    if original_action in ("trust", "trust_command", "trust_base") and (not fut or fut.done()):
        state_fut = state._approval_futures.get(request_id) if request_id else None
        if state_fut and not state_fut.done():
            return _deny_trust_pattern(name, request_id, original_action, "approval_not_slot_owned")
    # Trust: auto-approve remaining tools for this slot. The approval policy MUST
    # be keyed by the OWNER's EFFECTIVE session key — a linked cron/workflow or
    # channel-surfaced slot runs under ``linked_session_key``, not
    # ``dashboard:{key}``, so writing the raw slot key would leave the running
    # session on its old policy and the trust decision would silently not take.
    # ``effective_session_key`` is the one derivation shared with ``api_chat_mode``'s
    # grants AND revokes, so an off-switch always addresses the key a grant wrote.
    if action == "trust":
        # A pending-card trust decision may widen the slot only when this exact
        # live card carries the server's durable-grant proof.  This check MUST
        # precede every side effect: a forged/expired/state-owned request id
        # must not leave _trust or the session policy enabled before the common
        # resolver eventually returns 400/404.  Explicit session-mode changes
        # use api_chat_mode and remain independent of this card-bound proof.
        grantable = _get_pattern_from_pending(owner, request_id, "trust_grantable")
        if not fut or fut.done():
            # No state-level fallback for a trust grant.  The early state-owner
            # guard above returns 400; a genuinely missing/expired id keeps the
            # endpoint's existing 404 below without mutating anything.
            action = "trust"
        elif grantable != "1":
            return _deny_trust_pattern(name, request_id, original_action, "pattern_underivable")
        else:
            owner._trust = True
            state.sessions.set_approval_policy(effective_session_key(owner), "auto")
            action = "approved"
    # Trust-reads: auto-approve read-only bash commands for this slot
    # Defer setting _trust_reads until after the approval future is consumed
    # to prevent the frontend from seeing trust_reads=true while still pending.
    elif action == "trust_reads":
        action = "approved_trust_reads"
    # Trust-command: bind the grant to the SERVER-DERIVED pending command.  The
    # client pattern is only proof that the card the user clicked describes the
    # same command; it never supplies authority.
    elif action == "trust_command":
        if fut and not fut.done():
            pattern = body.get("pattern", "")
            expected = _get_pattern_from_pending(owner, request_id, "full_command")
            trust_key = _get_pattern_from_pending(owner, request_id, "trust_command_key")
            grantable = _get_pattern_from_pending(owner, request_id, "trust_command_grantable")
            if not isinstance(pattern, str) or not pattern:
                return _deny_trust_pattern(name, request_id, original_action, "pattern_required")
            if grantable != "1" or not expected or not trust_key:
                return _deny_trust_pattern(name, request_id, original_action, "pattern_underivable")
            if pattern != expected:
                return _deny_trust_pattern(name, request_id, original_action, "approval_superseded")
            # ``_trusted_patterns`` is the existing fnmatch store.  Escape every
            # metacharacter so an exact grant for ``rm *.tmp`` cannot authorize
            # ``rm secret.tmp``.
            owner._trusted_patterns.add(exact_trust_pattern(trust_key))
        action = "approved"
    # Trust-base: derive bases from the same canonical pending command, never
    # from the client pattern or model-authored title.
    elif action == "trust_base":
        if fut and not fut.done():
            pattern = body.get("pattern", "")
            base = _get_pattern_from_pending(owner, request_id, "base_command")
            grantable = _get_pattern_from_pending(owner, request_id, "trust_base_grantable")
            if not isinstance(pattern, str) or not pattern:
                return _deny_trust_pattern(name, request_id, original_action, "pattern_required")
            if grantable != "1" or not base:
                return _deny_trust_pattern(name, request_id, original_action, "pattern_underivable")
            if pattern != base_consent_pattern(base):
                return _deny_trust_pattern(name, request_id, original_action, "approval_superseded")
            owner._trusted_patterns.update(base_trust_patterns(base))
        action = "approved"
    # YOLO: auto-approve all tools globally (all slots)
    elif action == "yolo":
        result = await asyncio.to_thread(safety_override().activate, "dashboard")
        if not result.active:
            # Same two-reason split as ``api_chat_mode``: an ``approval_modes``
            # deny of ``yolo`` is a permanent policy answer the client can render
            # (403 + the code the picker already understands), while anything else
            # is a transient activation failure worth retrying (503).
            if not yolo_policy_permits():
                return _deny_approval_mode(
                    caller=audit_caller(),
                    operation="tool_approval:yolo",
                    mode="yolo",
                    resource=request_id,
                )
            return web.json_response(
                {"ok": False, "error": "safety override activation refused"},
                status=503,
            )
        for s in state._slots.values():
            # Same effective-session-key rule as the single-slot trust above: a
            # linked cron/workflow or channel-surfaced slot runs under its
            # linked_session_key.
            state.sessions.set_approval_policy(effective_session_key(s), "auto")
        # Reconcile against a policy deny that landed while this was writing.
        #
        # This write is the grant's inherited half -- ``admission.parent_trusted``
        # reads the slot's approval policy directly rather than any flag in
        # ``safety_override`` -- and it happens OUTSIDE the lock the revocation takes.
        # So a denying ceiling installed after the arm returned can revoke the grant
        # and run its ``_on_expired`` cleanup, and this loop then puts the inherited
        # trust straight back with nothing left to clear it: a subagent spawned under
        # it is auto-approved, and is not un-spawned by the next event either.
        #
        # The two halves are complete together: a write that lands BEFORE the
        # cleanup is cleared by the cleanup, and one that lands after -- or
        # interleaved with it -- is cleared here. Standing trust is preserved on the
        # same rule ``_on_override_expired`` uses, since a Trust press is a separate,
        # longer-lived decision that no yolo deny expires.
        if not yolo_policy_permits():
            for s in state._slots.values():
                if not (s._trust or s._trust_reads):
                    state.sessions.set_approval_policy(effective_session_key(s), "")
            state.push_slots_update()
        action = "approved"
    resolved = (
        action if action in ("approved", "approved_trust_reads", "rejected_once") else "rejected"
    )
    approved = resolved in ("approved", "approved_trust_reads")
    if not fut or fut.done():
        # Distinguish ambiguous (multiple pending) from truly empty
        if not request_id and slot._approval_futures:
            pending_ids = [k for k, f in slot._approval_futures.items() if not f.done()]
            if len(pending_ids) > 1:
                return web.json_response(
                    {
                        "error": "multiple approvals pending, specify request_id",
                        "pending": pending_ids,
                    },
                    status=400,
                )
        # No slot owns this future — fall back to the STATE-LEVEL-ONLY resolver so
        # a background approval (cron/subagent/gateway) is still dismissed instead
        # of 404-ing. MUST be resolve_state_approval, NOT resolve_approval: the
        # latter re-scans every slot's futures by bare id-match, which would let a
        # request-id collision resolve an unrelated slot's pending tool — exactly
        # the cross-slot approval the session-identity owner scan above prevents.
        # State-level futures have no per-slot trust semantics, so the bool
        # coercion loses nothing.
        if not strict_native and request_id and state.resolve_state_approval(request_id, approved):
            return web.json_response({"ok": True})
        return web.json_response({"error": "no pending approval"}, status=404)
    # Read while still pending: names the row the frame below settles.
    resolved_mid = (owner.approval_instance(request_id) or "") if request_id else ""
    fut.set_result(resolved)
    # Persist resolved state into the permission message so it survives tab
    # switches — on the owner slot, whose messages hold the permission card.
    # Flagging the slot dirty is required for it to survive a RESTART too: the
    # periodic flush skips non-dirty slots.
    if request_id:
        if _mark_permission_resolved(
            (
                [message for message in owner.messages if row_mid(message) == request_mid]
                if strict_native
                else owner.messages
            ),
            request_id,
            original_action if original_action in ("trust", "trust_reads") else resolved,
        ):
            owner._dirty = True
    # Broadcast first to ensure frontend is unblocked
    if request_id:
        state.broadcast_ws(
            "approval_resolved",
            {
                "id": request_id,
                "approved": approved,
                # Keys the frame for the slot-scoped WS gate (see
                # ws_event_scope._SLOT_SCOPED_EVENTS).
                "slot": owner.key,
                **native_resolution_target(resolved_mid),
            },
        )
    state.push_slots_update()
    # SEL audit (best-effort — must not block the UI-unblocking path above)
    try:
        sel().log_api_access(
            caller=audit_caller(),
            operation=f"tool_approval:{original_action}",
            outcome=resolved,
            resources=request_id,
        )
    except Exception:
        logger.warning("SEL audit failed for approval %s", request_id, exc_info=True)
    return web.json_response({"ok": True})


MAX_COLOR_INDEX = 20


async def api_chat_slot_color(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/color — set session color.

    Accepts ``color_index`` (int 0..MAX_COLOR_INDEX or null, resolved
    client-side against the viewer's generated palette) and/or ``color_hex``
    (``#rrggbb`` or null, a theme-independent custom color). The two are
    mutually exclusive: setting a non-null value for one clears the other, so
    a slot can never carry both and clients need no precedence rule. Keys are
    ``in body``-gated so an old client sending only ``color_index`` cannot
    silently null an existing hex.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    has_ci = "color_index" in body
    has_ch = "color_hex" in body
    ci = body.get("color_index")
    if ci is not None and (
        isinstance(ci, bool) or not isinstance(ci, int) or ci < 0 or ci > MAX_COLOR_INDEX
    ):
        return web.json_response(
            {"error": f"color_index must be a non-negative integer <= {MAX_COLOR_INDEX} or null"},
            status=400,
        )
    ch = body.get("color_hex")
    if ch is not None and (not isinstance(ch, str) or not COLOR_HEX_RE.match(ch)):
        return web.json_response(
            {"error": "color_hex must be #RRGGBB or null", "code": "invalid_color_hex"},
            status=400,
        )
    if has_ci:
        slot.color_index = ci
        if ci is not None:
            slot.color_hex = None
    if has_ch:
        slot.color_hex = ch.lower() if isinstance(ch, str) else None
        if ch is not None:
            slot.color_index = None
    slot._dirty = True
    state.push_slots_update()
    return web.json_response(
        {"ok": True, "color_index": slot.color_index, "color_hex": slot.color_hex}
    )


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


def _validate_content(content: object) -> web.Response | None:
    """Shared content validation for /context and /note.

    Validating at the request boundary in ONE place is what keeps the two entry
    points from drifting. Returns a 400 response on a bad value, else None.
    """
    if not isinstance(content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "invalid_content"},
            status=400,
        )
    if not content:
        return web.json_response(
            {"error": "content is required", "code": "empty_content"},
            status=400,
        )
    if len(content) > _MAX_CONTEXT_CONTENT:
        return web.json_response(
            {
                "error": f"content exceeds {_MAX_CONTEXT_CONTENT} char limit",
                "code": "content_too_long",
            },
            status=400,
        )
    return None


def _normalize_source(source: object) -> str:
    """Trim a caller source to its stored form: a stripped str.

    Non-str / None / blank collapse to ``""`` and the caller then applies its own
    default. Shared by validation and the /note default so a whitespace-only
    label cannot produce a blank drain frame, and so a padded label shares one
    per-source cap bucket with its trimmed form.
    """
    if not isinstance(source, str):
        return ""
    return source.strip()


def _validate_source(source: object) -> web.Response | None:
    """Shared source-label validation.

    Returns a 400 response on a bad value, else None. An empty, absent, or
    whitespace-only source is allowed here; the caller defaults it.
    """
    if source is not None and not isinstance(source, str):
        return web.json_response(
            {"error": "source must be a string", "code": "source_not_a_string"},
            status=400,
        )
    # Checked BEFORE the strip, which would otherwise silently drop a leading or
    # trailing tab/newline the documented contract says is a 400.
    if isinstance(source, str) and _SOURCE_CTRL_RE.search(source):
        return web.json_response(
            {
                "error": "source must not contain control characters or newlines",
                "code": "invalid_source",
            },
            status=400,
        )
    normalized = _normalize_source(source)
    if normalized == "":
        return None
    if len(normalized) > _MAX_SOURCE_LEN:
        return web.json_response(
            {"error": f"source exceeds {_MAX_SOURCE_LEN} char limit", "code": "source_too_long"},
            status=400,
        )
    if _SOURCE_CTRL_RE.search(normalized):
        return web.json_response(
            {
                "error": "source must not contain control characters or newlines",
                "code": "invalid_source",
            },
            status=400,
        )
    return None


def _validate_max_age(max_age: object) -> web.Response | None:
    """Shared maxAge validation. Returns a 400 response on a bad value, else None.

    ``drain_pending_context`` computes ``injected_at + max_age``, so a
    non-numeric value raises a TypeError on the user's NEXT send -- far from the
    request that introduced it. Rejecting it here turns that into a 400 at the
    boundary. Both callers validate UNCONDITIONALLY, not only when an entry is
    actually enqueued, so a visible-only note with a malformed maxAge is a 400
    rather than a silent ignore.

    ``bool`` is rejected because ``isinstance(True, int)`` is True but a boolean
    TTL is a caller bug. ``None`` is allowed, and both callers reach it from an
    omitted key as well as an explicit null -- they tell those apart themselves.
    """
    if max_age is None:
        return None
    if isinstance(max_age, bool) or not isinstance(max_age, (int, float)):
        return web.json_response(
            {"error": "maxAge must be a number (seconds) or omitted", "code": "invalid_max_age"},
            status=400,
        )
    # NaN and Infinity are floats that slip past the <= 0 check (NaN <= 0 is
    # False) and then make injected_at + max_age non-comparable at drain, so the
    # entry would never expire. Reject them at the boundary.
    # An arbitrary-precision int passes the isinstance check above, then
    # OverflowErrors inside isfinite's float conversion — same 400, not a 500.
    try:
        finite = math.isfinite(max_age)
    except OverflowError:
        finite = False
    if not finite:
        return web.json_response(
            {"error": "maxAge must be a finite number", "code": "non_finite_number"},
            status=400,
        )
    if max_age <= 0:
        return web.json_response(
            {"error": "maxAge must be positive", "code": "value_out_of_range"},
            status=400,
        )
    return None


def _reauthorize_after_await(
    state: DashboardState, slot: _ChatSlot, name: str, request_app: str, operation: str
) -> web.Response | None:
    """Re-authorize *slot* after an await, immediately before touching it.

    The ownership gate necessarily runs before the request body is read, and
    that ``await`` is a window rather than a formality: ``linked_session_key``
    is rebound on ALREADY-LIVE slots with no ``running`` gate -- a cron
    completion (``cron_inject.py:96``), a workflow injection
    (``workflow_inject.py:156``) -- so a slow caller can be authorized against
    its own session and land on somebody else's conversation. The same identity
    check ``_app_cancel_denied`` makes for /stop, moved to the point of use.

    Requires the same slot OBJECT, not just the same name: a delete and
    re-create under one name would pass an ownership re-check while being a
    different conversation. Callers must run this before the first read of slot
    state too, since ``running`` and the hold queue belong to whichever
    conversation the slot now routes to.
    """
    if state._slots.get(name) is not slot:
        if request_app:
            sel().log_api_access(
                caller=request_app,
                operation=operation,
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="slot was replaced while the request body was read",
            )
        return slot_not_found()
    return deny_app_slot_session_access(request_app, slot, name, operation)


def _source_cap_reached(slot: _ChatSlot, source: str) -> bool:
    """True if ``source`` already holds the max pending context entries.

    An empty source is uncapped (it shares no bucket). Shared by
    ``_enqueue_pending_context`` and the /note handler, which uses it to keep the
    visible transcript line independent of the context-queue cap.

    Expired entries do not count. They are dropped by ``drain_pending_context``
    but stay in the list until the next drain, so counting them would let ten
    already-dead notes lock a source out of fresh context indefinitely -- and the
    caller is told nothing, because the note still returns 200 with
    ``contextSkipped``. The same predicate decides both, so a count and a drain
    cannot disagree about which entries are live.

    Entries HELD for the deferred-note flush count as well. They are not in the
    queue yet, so a cap that reads the queue alone admits every one of them:
    ten same-source notes posted during one turn each see a clear cap, and the
    flush then promotes all ten at once, carrying that source past its
    per-source ceiling. Counting the held halves here holds the ceiling at
    admission so the flush never overfills the bucket.
    """
    if not source:
        return False
    now = time.time()
    held = [n["context"] for n in slot._deferred_notes if n.get("context") is not None]
    pending = sum(
        1
        for e in (*slot._pending_context, *held)
        if e.get("source") == source and not context_entry_expired(e, now)
    )
    return pending >= _MAX_CONTEXT_PER_SOURCE


def _enqueue_pending_context(
    slot: _ChatSlot,
    content: str,
    source: str,
    ephemeral: bool,
    max_age: int | float | None,
) -> web.Response | None:
    """Build, cap, and append a ``_pending_context`` entry.

    Returns a 4xx response on a bad request (429 could not be queued, 400
    invalid ``max_age``), else None on success. The entry is consumed on the next
    user-initiated message via ``drain_pending_context``.

    A 400 leaves the queue untouched. The two 429s come from different places:
    ``_build_pending_context_entry`` returns the per-source ``capacity_reached``
    429 before any append, while the whole-queue ``context_not_queued`` 429 is
    decided by ``append_pending_context``, which reclaims expired entries on the
    way, so that refusal can have dropped dead entries -- nothing live is ever
    evicted.

    ``max_age`` is the resolved seconds-to-live, or None for no expiry. HTTP
    callers already validate it via ``_validate_max_age``; the same guard runs
    again here so a direct (non-HTTP) caller cannot slip a non-numeric TTL
    through to the drain.

    """
    entry, err = _build_pending_context_entry(slot, content, source, ephemeral, max_age)
    if err is not None:
        return err
    assert entry is not None
    # Asked once, of the authority: the append owns the ceiling and reports
    # whether the entry was seated, so a preflight here would ask twice.
    if not slot.append_pending_context(entry):
        return web.json_response(
            {
                "error": (
                    "pending context could not be queued for this session: the "
                    "queue is full, or the entry expired before it was queued"
                ),
                "code": "context_not_queued",
            },
            status=429,
        )
    return None


def _build_pending_context_entry(
    slot: _ChatSlot,
    content: str,
    source: str,
    ephemeral: bool,
    max_age: int | float | None,
) -> tuple[dict[str, object] | None, web.Response | None]:
    """Validate and build one context entry WITHOUT touching the queue.

    Returns ``(entry, None)`` or ``(None, 4xx response)``. Split from the append
    so /note can run every rejection synchronously -- the caller still gets its
    400 or 429 on the POST -- while HOLDING the entry until the running turn
    ends. Queueing it at the POST instead would hand it to the turn already in
    flight, since that turn drains the queue after its task is assigned.
    """
    bad_age = _validate_max_age(max_age)
    if bad_age is not None:
        return None, bad_age
    if _source_cap_reached(slot, source):
        return None, web.json_response(
            {
                "error": f"source {source!r} has {_MAX_CONTEXT_PER_SOURCE} pending entries",
                "code": "capacity_reached",
            },
            status=429,
        )
    entry: dict[str, object] = {
        "content": content,
        "source": source,
        "ephemeral": ephemeral,
        "injectedAt": time.time(),
    }
    if max_age is not None:
        entry["maxAge"] = max_age
    return entry, None


async def api_chat_slot_context(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/context — inject silent background context.

    Adds a ContextEntry to the slot's ``_pending_context`` queue.
    The content is consumed on the next user-initiated message via
    ``ctx_builder.build_message()`` and prepended to the LLM prompt.

    No LLM turn is triggered, no WS event is broadcast, and no visible
    message is appended to the slot's chat history.

    Body::

        {
            "content": "...",
            "source": "watch-check",   // optional
            "ephemeral": true,         // optional, default true
            "maxAge": 300              // optional, seconds
        }
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        # Same body as the ownership denial below: two shapes would let an app
        # token tell "not mine" from "does not exist" and enumerate slot names.
        return slot_not_found()

    request_app = request.get("app", "")
    denied = deny_app_slot_session_access(request_app, slot, name, "context_inject")
    if denied is not None:
        return denied

    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    content = body.get("content", "")
    bad = (
        _validate_content(content)
        or _validate_source(body.get("source"))
        or _validate_max_age(body.get("maxAge"))
    )
    if bad is not None:
        return bad

    # Same window as /note: authorized before the body read, so re-decide against
    # the slot as it is now, ahead of the only write.
    stale = _reauthorize_after_await(state, slot, name, request_app, "context_inject")
    if stale is not None:
        return stale

    # Normalize the source the same way /note does, so a whitespace-padded label
    # renders a clean drain frame and shares one cap bucket with its trimmed
    # form. /context keeps empty-source-uncapped and applies no default label: a
    # sourceless context injection is intentionally bucket-free.
    err = _enqueue_pending_context(
        slot,
        content,
        _normalize_source(body.get("source")),
        body.get("ephemeral", True),
        body.get("maxAge"),
    )
    if err is not None:
        return err

    # SEL audit logging
    sel().log_api_access(
        caller=request_app or request.get("user", "dashboard"),
        operation="context_inject",
        outcome="ok",
        source="app_kit",
        resources=f"slot={name}",
    )

    return web.json_response({"ok": True, "pending": len(slot._pending_context)})


def _discard_held_note(slot: _ChatSlot, note: dict[str, object]) -> None:
    """Remove *note* from the live hold by IDENTITY, if it is still there.

    Identity, never equality: two same-content notes from a capped source are
    byte-identical dicts (their context half is None), and ``list.remove``
    would evict the FIRST equal one — a sibling note that already received its
    durable 200 — leaving this failed note behind to be persisted by the next
    successful write. A note the flush already drained is simply absent; that
    absence carries NO meaning here (it can be delivered OR dropped at the
    rebind seam), which is why the caller answers from positive evidence, not
    from this function.
    """
    for i, held in enumerate(slot._deferred_notes):
        if held is note:
            del slot._deferred_notes[i]
            return


def _note_delivered_live(slot: _ChatSlot, note: dict[str, object]) -> bool:
    """True when a delivered row stamped with this note's id is in the slot's
    LIVE message list — evidence clause (a): the flush delivered the note this
    lifetime, and the save that commits the row retires its durable entry.
    In-memory and synchronous, so every branch can afford it. A note with no
    id has no row stamp to look for and reads as not-delivered, toward the
    branch's refusal (retryable, never a silent unkept promise)."""
    note_id = note.get("id")
    if not isinstance(note_id, str) or not note_id:
        return False
    for row in slot.messages:
        row_meta = row.get("meta")
        if isinstance(row_meta, dict) and row_meta.get("noteId") == note_id:
            return True
    return False


async def _persist_deferred_note_hold(
    state: DashboardState,
    slot: _ChatSlot,
    note: dict[str, object],
    authorized_history_key: str,
) -> web.Response | None:
    """Make a just-held /note durable before the 200 acknowledges it.

    Returns ``None`` on success (or when there is nothing durable to keep the
    promise against) and a non-200 the caller must return otherwise.
    Semantics — durable-before-200, honestly degraded at the edges:

    - **No conversation log** (memory-only deployment, bare test states):
      nothing survives a restart at all, so a 200 keeps its original
      "accepted for this gateway lifetime" meaning. No write, no failure.
    - **No metadata line yet** (the guard refuses the merge): the SLOT has no
      durable identity, so a restart drops the tab itself and there is no
      restored slot the note could outlive. Accepted without a durable copy —
      but ONLY when the slot never had one: a file that existed when this
      write began and is gone under the lock means a concurrent permanent
      delete won, and that is refused with the uniform not-found shape
      instead, because the 200's durable promise was just destroyed along
      with the session itself.
    - **Rebind in the window** (DeferredHoldRebound): the slot no longer
      routes to the transcript this note was authorized against, so the write
      was refused rather than landing app content in a foreign transcript's
      metadata. Refused with the endpoint's uniform not-found shape — the
      same answer the ownership gate gives, so nothing an unauthorized caller
      can observe distinguishes the cases. A note the concurrent flush
      DROPPED at the rebind seam takes this 404 too: it was never delivered
      and has no durable copy, so a 200 would be a delivery promise nothing
      owns.
    - **Hold full** (DeferredHoldFull): admitting the entry would evict a
      retained one — the only durable copy of an already acknowledged note —
      so the NEW note is refused with the live cap's retryable 429.
    - **Write raises** (lock timeout, I/O error): the promise cannot be kept,
      so the note is discarded and the caller gets a retryable 503 rather
      than a 200 that lies about durability.

    On EVERY branch — the success path included — the 200 stands only on
    POSITIVE EVIDENCE that the note has an owner, never on inference from a
    negative signal (reading absent-from-the-hold as "delivered" misreads a
    rebind-dropped note; reading a written merge as "durable" misreads a
    flush-side drop under a diverged channel-origin key). The evidence
    clauses, any one sufficient:

    (a) a delivered row stamped ``meta.noteId`` is in the slot's LIVE message
        list — the flush delivered it this lifetime (checked here,
        in-memory);
    (b) its id is in the durable hold — on disk already (a sibling's merge
        writer commits the WHOLE live list), or in the merge this write
        committed (resolved under the lock, carried on the outcome and both
        hold exceptions);
    (c) its delivered row is in the COMMITTED transcript (same locked
        resolver).

    With evidence, an error answer would make the caller re-post a note that
    is already delivered or already durable, and the restore would then
    produce a duplicate. Without evidence, the branch's refusal is the honest
    answer. The generic failure branch carries no resolver verdict (the
    writer may have failed before its guard ran), so it makes the durable
    observation itself with :func:`_note_already_durable`.
    """
    conversation_log = getattr(state, "conversation_log", None)
    if conversation_log is None:
        return None
    # Whether the slot HAS a durable identity, decided before the write. The
    # slot-side flag is MONOTONIC and synchronous — a permanent delete racing
    # this worker cannot unwind it, so a delete landing even before the probe
    # below still reads as "the slot had an identity" and a no-line outcome is
    # refused rather than 200-acknowledged. The mtime probe supplements it for
    # a file that exists without this slot object ever having observed it.
    # A slot that never had a file keeps the documented
    # accepted-without-durable-copy semantics.
    had_durable_identity = bool(getattr(slot, "_disk_meta_observed", False)) or (
        await asyncio.to_thread(conversation_log.mtime_of, authorized_history_key) is not None
    )
    try:
        outcome = await asyncio.to_thread(
            persist_deferred_notes_sync,
            conversation_log,
            slot,
            note,
            authorized_history_key,
        )
    except DeferredHoldRebound as exc:
        if exc.evidence.durable or exc.evidence.committed or _note_delivered_live(slot, note):
            return None
        sel().log_api_access(
            caller=str(note.get("session", "")) or "dashboard",
            operation="note_post",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="slot was rebound to another session while the hold was persisting",
        )
        _discard_held_note(slot, note)
        return slot_not_found()
    except DeferredHoldFull as exc:
        if exc.evidence.durable or exc.evidence.committed or _note_delivered_live(slot, note):
            return None
        _discard_held_note(slot, note)
        return web.json_response(
            {
                "error": "slot's durable deferred-note hold is full until its rows are saved",
                "code": "deferred_notes_full",
            },
            status=429,
        )
    except Exception:
        logger.error(
            "Failed to persist the deferred-note hold for slot %s", slot.key, exc_info=True
        )
        # No resolver verdict travels with a generic failure (the writer may
        # have failed before its guard ran), so make the durable observation
        # here — it is still an observation, never an inference. A recorded
        # drop dominates it (same rule as the resolver): a drop-marked
        # durable entry is retired row-lessly by the next save, so it cannot
        # back a delivery promise.
        note_id = note.get("id")
        recorded_dropped = isinstance(note_id, str) and note_id in getattr(
            slot, "_dropped_note_ids", set()
        )
        if (
            not recorded_dropped
            and await _note_already_durable(conversation_log, authorized_history_key, note)
        ) or _note_delivered_live(slot, note):
            return None
        _discard_held_note(slot, note)
        return web.json_response(
            {
                "error": "failed to persist the held note; retry the request",
                "code": "deferred_note_persist_failed",
            },
            status=503,
        )
    if outcome.written:
        if (
            outcome.evidence.durable
            or outcome.evidence.committed
            or _note_delivered_live(slot, note)
        ):
            return None
        # The merge landed but carries NO representation of this note: the
        # concurrent flush dropped it at the rebind seam while the slot's
        # history key still matched (the two keys diverge for a
        # channel-origin slot), so nothing will ever deliver or replay it.
        # Same refusal as the rebind branch — the note's authorization no
        # longer matches where the slot routes.
        sel().log_api_access(
            caller=str(note.get("session", "")) or "dashboard",
            operation="note_post",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="held note was dropped at the rebind seam while the hold was persisting",
        )
        _discard_held_note(slot, note)
        return slot_not_found()
    if had_durable_identity:
        # The guard saw NO metadata line for a slot whose file existed when
        # this write began: a permanent delete won the lock. The session and
        # any durable copy are gone, so a 200 here would acknowledge a note
        # that can never be delivered or restored. Refuse with the endpoint's
        # uniform not-found shape (what the caller would have seen had the
        # delete landed a moment earlier).
        if outcome.evidence.committed or _note_delivered_live(slot, note):
            return None
        _discard_held_note(slot, note)
        return slot_not_found()
    return None


async def _note_already_durable(
    conversation_log: object, authorized_history_key: str, note: dict[str, object]
) -> bool:
    """True when a concurrent sibling's merge writer already persisted *note*.

    Off the loop (locked file read). When it answers True the note must NOT
    be rolled back or error-answered: its durable entry is real, the restore
    will replay it, and a 503/429 would make the caller re-post a duplicate.
    """
    note_id = note.get("id")
    if not isinstance(note_id, str) or not note_id:
        return False
    return await asyncio.to_thread(
        note_hold_durable, conversation_log, authorized_history_key, note_id
    )


async def api_chat_slot_note(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/note — visible transcript line + silent next-turn context.

    A background actor (a cron, an app) uses this to drop a short, DECLARATIVE
    note into a chat that is both (a) visible in the transcript right away and
    (b) known to the agent if the user later asks about it -- WITHOUT firing an
    LLM turn.

    A plain transcript append is not enough on its own: a live provider holds its
    own in-memory conversation state and a normal send forwards only the new user
    message, so a row written via ``slot.append()`` alone is never seen by the
    model. The channel that IS seen is ``_pending_context``, which is drained and
    prepended to the next user message. So the endpoint does two writes against
    the same slot:

    1. visible line -- ``slot.append(role="inject", cls="reconcile-note")`` so it
       renders in the transcript and persists.
    2. context entry -- a ``_pending_context`` entry (the same channel
       ``/context`` uses) drained onto the user's next manual message exactly
       once, then cleared.

    Both writes always happen. A context-only write is ``POST /context``, which
    already exists; there is no visible-only mode, because no caller wanted one.

    A session reset in between can replay the transcript row into the new
    session, so the model may see the note twice in one prompt. The queued copy
    is kept regardless: the replay is char-budget bounded, so dropping it would
    lose an older note the replay had already trimmed away.

    Notes are meant to be declarative -- state what happened, never ask. An
    interrogative note rides along as background context and may get answered on
    the next unrelated turn. The context half defaults to a 24h ``maxAge`` so a
    never-followed-up note self-expires; the visible line is permanent.

    Body::

        {
            "content": "...",         // required, declarative, non-empty string
            "source": "board-sync",   // optional frame label + per-source cap bucket;
                                      //   <=64 chars, no control chars; empty -> "note"
            "maxAge": 86400,          // optional seconds; omitted -> 24h default.
                                      //   Explicit null -> no expiry, as on /context.
            "ephemeral": true         // optional, default true (passed to the context entry)
        }

    Returns ``{"ok", "appended", "visibleDeferred", "contextSkipped", "pending"}``.
    If the source's per-source context cap is already full the visible line is
    still written and ``contextSkipped`` is true: the cap protects the context
    queue, not the transcript, so the call is NOT 429'd.

    When a turn is already running BOTH halves are held and written at that
    turn's end, so ``appended`` is false and ``visibleDeferred`` is true. Its
    order is preserved, and the hold is DURABLE: it is persisted
    into the slot's own metadata line before the 200 is returned, replayed by
    both slot-restore paths after a gateway restart, and retired by the save
    that commits the delivered rows. A caller therefore never needs to re-post
    after a restart; the one retry signal is a 503 ``deferred_note_persist_failed``,
    which means the hold could not be made durable and was not accepted.
    Appending mid-turn would take the row the replay path skips and cause the
    user's own request to be replayed; queueing the context mid-turn would let
    the turn already in flight drain it, so the note would shape the request it
    was written after and the next turn would find nothing. Every rejection
    still happens on the POST. ``pending`` counts held entries too, so it always
    reports what the model will receive. Holding more than
    ``_MAX_DEFERRED_NOTES`` on one turn is a 429 ``deferred_notes_full`` (also
    returned when the durable hold still carries delivered-but-unsaved entries
    at its ceiling), and a held note's content is bounded at
    ``MAX_DEFERRED_NOTE_CHARS`` — a 413 ``deferred_note_too_large`` — because
    the durable copy is persisted verbatim and must replay exactly what the
    200 accepted.
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        # Byte-identical to the ownership denial below, or an app token could tell
        # "not mine" from "does not exist" and enumerate foreign slot names.
        return slot_not_found()

    request_app = request.get("app", "")
    denied = deny_app_slot_session_access(request_app, slot, name, "note_post")
    if denied is not None:
        return denied

    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    content = body.get("content", "")
    bad = (
        _validate_content(content)
        or _validate_source(body.get("source"))
        or _validate_max_age(body.get("maxAge"))
    )
    if bad is not None:
        return bad

    # Default an empty, absent, or whitespace-only source to "note" so the drain
    # frame reads [Background context from "note"] rather than empty quotes.
    source = _normalize_source(body.get("source")) or "note"

    # The VISIBLE source label (the "Note from ..." pill) is stamped from the
    # AUTHENTICATED caller, never from the body. ``request_app`` is set by the
    # app-token auth middleware from the validated token record (an app's
    # registered slug), so it cannot be spoofed: app A posting
    # ``source="Kiro"`` or another app's name cannot make the bubble attribute
    # the note to anyone but A. A dashboard user carries an EMPTY ``request_app``
    # and gets no author pill — the note is their own and needs no attribution.
    # Because the label is a trusted, slug-shaped identity and not
    # caller-controlled free text, it needs no credential/exfil redaction (an
    # app slug cannot carry a spliced credential span), which is why the
    # two-pass ``redact_caller_text`` and ``slot_buffers``'s redactor-sink
    # allowlist entry both drop out with this change. The body's ``source`` is
    # still read above for the internal drain-frame cap bucket only.
    #
    # An app name has no length bound at the single-name contract and the
    # import path caps it at 120 chars, so a slug longer than
    # ``MAX_SOURCE_LABEL_LEN`` is admissible. Apply the SAME structural bound
    # the restore sanitizer applies (length + control char -> "") HERE, so what
    # the admit path stamps always survives a persist/restore round-trip
    # unchanged: without it a 65+ char app name would persist verbatim, then be
    # collapsed to "" on restore, and the flushed note would silently lose its
    # author pill for that app forever.
    display_source = request_app
    if len(display_source) > MAX_SOURCE_LABEL_LEN or SOURCE_LABEL_CTRL_RE.search(display_source):
        display_source = ""

    # Ownership was decided before the body read. Re-decide it here, against the
    # slot as it is NOW, because that await is long enough for a rebind.
    stale = _reauthorize_after_await(state, slot, name, request_app, "note_post")
    if stale is not None:
        return stale

    # A turn in flight owns the tail of the transcript: the replay path skips
    # exactly one recall-eligible row to drop the current-turn user message, and
    # an `inject` row appended now would take that slot and get skipped in its
    # place, replaying the user's request twice. So the visible line is HELD and
    # written at the turn's end, which is why `appended` is reported separately.
    # This is decided BEFORE either write: a note rejected for a full hold must
    # not leave its context half behind to reach the next turn anyway.
    deferred = slot.running
    if deferred and len(slot._deferred_notes) >= _MAX_DEFERRED_NOTES:
        return web.json_response(
            {
                "error": f"slot already holds {_MAX_DEFERRED_NOTES} deferred notes",
                "code": "deferred_notes_full",
            },
            status=429,
        )
    if deferred and len(content) > MAX_DEFERRED_NOTE_CHARS:
        # A held note is persisted VERBATIM before the 200, so
        # what the 200 accepts is exactly what a restart replays — truncating
        # the durable copy would replay altered content for an acknowledged
        # note. The bound therefore sits at the boundary, where the caller can
        # act on it: shorten the note, or wait for the turn to end (immediate
        # notes keep the larger shared content bound).
        return web.json_response(
            {
                "error": (
                    f"a note posted during a running turn is capped at "
                    f"{MAX_DEFERRED_NOTE_CHARS} characters; shorten it or wait "
                    "for the turn to end"
                ),
                "code": "deferred_note_too_large",
            },
            status=413,
        )

    # The per-source cap protects the context QUEUE, not the transcript. So when
    # the context half is capped we still write the VISIBLE line -- the audit
    # record the caller came for -- and report contextSkipped=true, rather than
    # 429-ing the whole request and losing the visible note too. This matters
    # most for the default source="note" bucket, which every sourceless caller
    # shares. An omitted maxAge takes this endpoint's 24h default; an explicit
    # null means no expiry, the same as it does on /context.
    context_skipped = False
    context_entry: dict[str, object] | None = None
    if _source_cap_reached(slot, source):
        context_skipped = True
    elif deferred and not slot.has_pending_context_seat():
        # The HELD path must refuse the context half at admission, the same way
        # the immediate arm does when ``append_pending_context`` returns False.
        # A held note's context is seated later, by ``flush_deferred_notes`` ->
        # ``append_pending_context`` at turn end; if the queue is already at the
        # seat ceiling now, that append refuses, and the only trace is a log
        # warning -- nothing the caller ever sees. So the note would be
        # acknowledged (200, ``visibleDeferred``) with its context silently
        # dropped: the acknowledge-then-lose behaviour this endpoint
        # exists to remove, moved onto the held path. ``has_pending_context_seat``
        # counts live entries PLUS each held note's reserved context half, so the
        # check is accurate: a later ``/context`` arrival cannot take the seat the
        # flush would need, and expiry between now and the flush can only free
        # seats, never consume them. We hold the VISIBLE line regardless (the
        # audit record the caller came for) and report contextSkipped=true.
        context_skipped = True
    else:
        max_age = body.get("maxAge", _UNSET)
        if max_age is _UNSET:
            max_age = _NOTE_CONTEXT_MAX_AGE
        context_entry, err = _build_pending_context_entry(
            slot, content, source, body.get("ephemeral", True), max_age
        )
        if err is not None:
            return err
        assert context_entry is not None
        # A held note's context is queued by the flush, not here. The drain runs
        # inside the turn and after its task is assigned, so an entry queued now
        # is read by the turn already running -- the note would shape the request
        # it was written after, and the next turn would find nothing.
        if not deferred:
            # Both immediate halves resolve their destination LATE, so each
            # records the session it was authorized against -- same reason the
            # deferred arm below does, and checked at those later seams.
            context_entry["noteSession"] = effective_session_key(slot)
            if not slot.append_pending_context(context_entry):
                # The visible line is still written; contextSkipped carries the
                # refused half, the same surface the per-source cap uses.
                context_skipped = True

    # Caller-controlled content reaching the visible transcript (SSE plus the
    # on-disk JSONL). Redact at this sink so a secret or exfil URL cannot land
    # in user-visible history. The context half stays raw: that is the
    # trusted-caller boundary inherited from /context. Attribution scope: the
    # note CONTENT runs through the exfiltration → credential redaction pair and
    # nothing more — a note carries a source LABEL, not a rewritten body, so no
    # format-char normalization is layered on the content here.
    visible_content, _ = redact_exfiltration_urls(content)
    visible_content, _ = redact_credentials(visible_content)
    if deferred and len(visible_content) > MAX_DEFERRED_NOTE_CHARS:
        # The bound must hold on the PERSISTED string, not just the raw input:
        # redaction can GROW content (each flagged URL becomes a longer
        # [REDACTED: ...] tag), and a persisted entry over the bound is dropped
        # fail-closed by the restore sanitizer — a 200 here would be an
        # acknowledgement the restart silently breaks. The raw-content check
        # above still stands on its own: the context half persists the RAW
        # string, and the sanitizer applies the same bound to it.
        return web.json_response(
            {
                "error": (
                    f"a note posted during a running turn is capped at "
                    f"{MAX_DEFERRED_NOTE_CHARS} characters after redaction; "
                    "shorten it or wait for the turn to end"
                ),
                "code": "deferred_note_too_large",
            },
            status=413,
        )
    if deferred:
        note: dict[str, object] = {
            # Identity for the durable hold's merge (slot_buffers.
            # persist_deferred_notes_sync): a disk entry whose id is absent
            # from the in-memory hold was delivered or dropped, never lost.
            "id": uuid.uuid4().hex[:12],
            "content": visible_content,
            "cls": "reconcile-note",
            "context": context_entry,
            # The AUTHENTICATED caller identity (``request_app``), carried
            # through the durable hold so the flushed visible row can say which
            # app wrote the note -- the same spoof-proof value an immediate note
            # stamps below. Empty for a dashboard user, so the flush renders no
            # "from ..." pill.
            "source": display_source,
            # The session this note was authorized against. The gate above
            # only admits a slot that still routes to its own session, but
            # an unbound slot can acquire a foreign binding while the note
            # is held, and the flush resolves its target late.
            "session": effective_session_key(slot),
        }
        # The transcript this authorization resolves to, captured in the SAME
        # routing observation as the session stamp above: the durable write
        # targets this key and re-verifies the slot still resolves to it
        # under the store lock, so a rebind during the persist window cannot
        # land app-authorized content in a foreign transcript's metadata.
        authorized_history_key = slot_history_key(slot)
        slot._deferred_notes.append(note)
        # Make the hold durable BEFORE the 200 acknowledges it:
        # ``visibleDeferred: true`` is a delivery promise for a transcript
        # line, and an in-memory-only hold silently voids it on a gateway
        # restart. The write persists the CURRENT hold into the slot's own
        # metadata line under the history lock, off the event loop, and the
        # restore paths replay it into ``_deferred_notes`` on the first boot
        # after a restart.
        err = await _persist_deferred_note_hold(state, slot, note, authorized_history_key)
        if err is not None:
            return err
    else:
        slot.append(
            role="inject",
            content=visible_content,
            cls="reconcile-note",
            broadcast=True,
            meta={
                "noteSession": effective_session_key(slot),
                # Attribute the note through the SAME app-label pill an app
                # inject row already uses (``meta.appLabel`` ->
                # ``components.mcpApp.from_app``): ``display_source`` is the
                # authenticated caller identity (``request_app``), so a reader
                # sees "Sent by app {X}" on the note bubble. Omit the key when
                # the caller gave no identity (a dashboard user), since the
                # renderer draws the pill only on a truthy value -- a sourceless
                # note stays unattributed.
                **({"appLabel": display_source} if display_source else {}),
            },
        )

    sel().log_api_access(
        caller=request_app or request.get("user", "dashboard"),
        operation="note_post",
        outcome="ok",
        source="app_kit",
        resources=f"slot={name}",
    )

    # A hold is delivered only if the slot still routes to the same session at
    # flush; a rebind during the hold drops it. An IMMEDIATE note is equally
    # conditional while the slot is UNBOUND, because both halves resolve their
    # destination late and every binding site claims an EMPTY binding
    # (``if not slot.linked_session_key``) -- so an already-bound slot cannot be
    # re-claimed and its immediate note is genuinely unconditional.
    delivery_conditional = deferred or not slot.linked_session_key
    return web.json_response(
        {
            "ok": True,
            "appended": not deferred,
            "visibleDeferred": deferred,
            "deliveryConditional": delivery_conditional,
            "contextSkipped": context_skipped,
            "pending": len(slot._pending_context) + slot.deferred_context_count(),
        }
    )


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.dashboard.chat_handlers.<name>`` reaches it wherever it lives; see
# ``kiro_crew.dashboard.chat_api``. Run once, after this body has bound every name.
_chat_api.compose(
    globals(),
    (
        _owner_resume,
        _owner_slot_detail,
        _owner_slot_lifecycle,
        _owner_source_links,
    ),
)
