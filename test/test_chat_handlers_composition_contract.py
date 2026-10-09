"""The chat HTTP handlers keep their surface and contracts while their owners move.

``kiro_crew.dashboard.chat_handlers`` is the dashboard chat API's import path and its
patch surface. Part of what it defined now lives in the modules of
``kiro_crew.dashboard.chat_api``, one responsibility each, and ``chat_api.compose``
runs every function they define on the facade's globals. These tests pin:

* the surface: every name the facade bound before the split still resolves on it, the
  route table still dispatches to the facade's objects, and the six runner seams it
  imports are still the runner's objects;
* contracts of the moved endpoint families that no other test pins: the bare 404 a
  delete answers, the resume refusal without a conversation log, the off-loop restore
  prefetch, the close path's conductor wake, and the resume record shapes.
"""

from __future__ import annotations

import ast
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
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from source_corpus import repo_files_named, repo_root

import kiro_crew.dashboard.chat_handlers as ch
from kiro_crew.dashboard import chat_api, slot_ownership
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = ch.__name__
_FACADE_PATH = Path(ch.__file__).resolve()

#: Every module-level name ``chat_handlers`` bound at the base the split was cut
#: from: what it defined and what it imported, private names included, because tests
#: and production read private names off it too. The per-slot app ownership helpers
#: that ``slot_ownership`` holds instead are not all in it.
_BASE_NAMES = frozenset("""
        ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
        ARTIFACT_SLUG_RE AUTOCOMPACT_PCT_MAX AUTOCOMPACT_PCT_MIN AcpModelUnavailable AcpProvider
        Any Awaitable COLOR_HEX_RE Callable
        ClientConnectionResetError DashboardState DeferredHoldFull DeferredHoldRebound
        HUMAN_TURN_META_KEY JEV_ROUTE_MODEL KiroCrewConfig LLMProvider MAX_COLOR_INDEX
        MAX_DEFERRED_NOTES MAX_DEFERRED_NOTE_CHARS MODEL_NAMESPACE_ACP MemoryStartupUnavailable
        NamedTuple OversizedRecord Path RESERVED_ROW_META_KEYS ResumeOutcome
        ResumeRefusal SESSION_RELOAD_KIND SESSION_START_FAILED_KIND SLOT_DETAIL_MAX_LIMIT
        STEER_AUTO STEER_REQUEUED STEER_STEERED SUGGEST_FOLLOWUP_SCHEMA SYNTHETIC_RECOVERY_KIND
        SelectionChange SlotCloseError SplitlinesBoundaryRecord TURN_ACTOR_META_KEY
        TYPE_CHECKING TranscriptRevisionChanged UnknownMemoryStore ValidationError
        _CREATABLE_MODES _ChatSlot _CommitToken _DEFERRED_PLAIN_CREATE_KNOWN_KEYS
        _DurablePrefixMismatch _FLUSH_SNAPSHOT_RETRIES _FOLLOWUP_TEXT_FIELDS
        _GUARDED_WRITE_WAIT_SECS _HandoverDrainResult _MANUAL_CONTINUE_MSG _MANUAL_RESUME_MSG
        _MAX_CONTEXT_CONTENT _MAX_CONTEXT_PER_SOURCE _MAX_DEFERRED_NOTES
        _MAX_DISMISSED_SOURCE_LINKS _MAX_RECENT_PROJECTS _MAX_SOURCE_LEN _NOTE_CONTEXT_MAX_AGE
        _NudgeRetireFailed _SESSION_RELOAD_NOTICE _SESSION_START_REPEAT_REFUSAL_AT
        _SLOT_SCOPED_TRUST_MODES _SOURCE_CTRL_RE _STRUCTURED_CONTENT_MAX_CHARS
        _STRUCTURED_CONTENT_PLACEHOLDER _TEARDOWN_INCOMPLETE_WARNING _TRANSIENT_ROLES
        _TURN_OPENER_ROLES _TURN_OPENING_INJECT_KINDS _UNOWED_WINDOW_ROLES _UNPINNED _UNSET
        _app_cancel_denied _app_may_send_to_slot
        _append_unflushed_tail _append_unflushed_tail_from_offset
        _apply_source_link_unlink _attach_variants
        _audit_source_link_unlink _autocompact_txn_lock _autocompact_txn_locks
        _await_guarded_history_write _bounded_slot_page _broadcast_context_reset
        _broadcast_expired_oauth_banners _build_pending_context_entry _build_stream_chunk
        _bump_slot_tags_revision _cancel_target _close_slot
        _coerce_requested_mode _collapse_wire_rows _compaction_in_flight
        _configured_backend_for_slot _context_reading _context_snapshot_fields
        _context_snapshot_fields_inner _context_usage_payload _deny_app_yolo _deny_approval_mode
        _deny_trust_pattern _discard_held_note
        _durable_prefix_counter _edit_queued_by_id _emit_agent_assignment _end_trust_scope
        _end_trust_scopes _enqueue_pending_context _finite_number _generate_state
        _get_pattern_from_pending _has_conversation _has_validated_effort_marker
        _history_key_for _hydrate_slot_from_history _is_answered_permission _is_interrupted
        _is_jev_route_pick _is_stop_event _live_child_instance _live_slot_for_resume
        _live_slot_resume_payload _load_redacted _load_restore_cfg _local_turn_generation
        _local_turn_prompt _make_stop_resolver _mark_permission_resolved
        _materialise_slot_from_history _maybe_auto_title _model_rejected_reason
        _normalise_structured_content _normalize_model _normalize_slot_key _normalize_source
        _note_already_durable _note_delivered_live _open_stop_event_card _orphan_in_current_turn
        _owed_prompts_lost_on_line _owner_denial_response _pending_guarded_history_writes
        _persist_deferred_note_hold _persist_handover_tail _prepare_messages
        _reapply_effort_after_live_switch _rearm_stop_event _reauthorize_after_await
        _rebase_rehydrated_refresh_mark _recent_projects_path _reconcile_local_turn_marker
        _reconcile_slot_window _record_explicit_agent_selection _redact_followup_item
        _redact_for_display _redact_history_rows _redact_meta _redact_meta_for_role
        _rehydrate_slot_title _reject_pending_approvals _release_closed_execution
        _remember_reasoning_effort_for_restore _remove_queued_by_id
        _replacement_shares_transcript _report_lost_queued_prompts _reset_slot_session
        _reset_slot_session_or_warn _resettle_restricted_key _resolve_stop_event
        _restore_dismissed_source_links _restore_model_fields _restore_slot_nudge_loop
        _restored_agent_name _restored_mode _resume_refusal_response _resume_session_identity
        _retire_slot_nudge_loop _run_chat _same_persisted_body _save_recent_project
        _settle_discarded_stage_deliveries _slot_not_found _slot_replaced_while_queued
        _slot_still_ours _slot_switch_session_lock _slots_serialization_note
        _snapshot_slot_window _source_cap_reached _source_link_txn_lock _source_link_txn_locks
        _source_link_unlink_tasks _start_next_queued_turn _still_owning
        _subagents_attached_response _sweep_stale_permissions _switch_target_busy
        _sync_dashboard_slots _sync_served_model _test_interleave
        _tighten_replacement_to_restricted_original _try_live_model_switch
        _unblock_pending_waits _unhide_folder _validate_autocompact_pct _validate_content
        _validate_max_age _validate_source _wake_conductor_for_closed_worker _wire_model_id
        _workspace_name_for_dir annotations api_chat api_chat_mode
        api_chat_slot_agent api_chat_slot_approve api_chat_slot_autocompact api_chat_slot_color
        api_chat_slot_context api_chat_slot_continue api_chat_slot_create api_chat_slot_delete
        api_chat_slot_detail api_chat_slot_end_wait api_chat_slot_followup
        api_chat_slot_interrupt api_chat_slot_model api_chat_slot_note api_chat_slot_project
        api_chat_slot_queue_cancel api_chat_slot_queue_edit api_chat_slot_queue_reorder
        api_chat_slot_reasoning_effort api_chat_slot_reload api_chat_slot_reset_conversation
        api_chat_slot_resume api_chat_slot_selection_capabilities
        api_chat_slot_source_link_unlink api_chat_slot_source_links api_chat_slot_stop
        api_chat_slot_summary api_chat_slot_summary_generate api_chat_slot_workspace
        api_chat_slots api_chat_slots_cleanup api_chat_slots_model
        api_chat_slots_unrestored api_recent_projects
        approval_mode_permitted asyncio attachment_meta
        base_consent_pattern base_trust_patterns cached_project_agent_names canonical_key
        cap_effort_capability_levels capabilities_of carry_provenance channel_slot_name
        chat_message_frame close_slot compaction_in_flight config_dir context_entry_expired
        contextlib count_user_turns_in_records datetime
        decided_message_handling default_project_dir deny_non_dashboard_caller
        deny_session_approval_caller drained_to_thread
        durable_row_count effective_session_key exact_trust_pattern
        generate_session_summary
        get_reasoning_effort_ordered get_reasoning_effort_values history_corpus_unreadable
        is_channel_session_key is_claude_code is_incognito_transcript is_owner_dashboard_request
        is_registered_agent_name is_sensitive_path is_stop_event_row is_system_notice
        is_turn_interrupted islice json logger logging math maybe_auto_tag members_mod
        model_registry normalize_send_id normalize_theme_consent_sha note_crew_log_class
        note_hold_durable note_slot_closed os owner_start_priority parse_cls_meta
        persist_deferred_notes_sync pick_epoch_host pin_private_agent_store
        published_autocompact_pct queue_entry_is_user_origin queue_entry_view
        queue_for_next_turn queued_text_for_display read_bounded_json
        read_cached_intent_summary record_agent_selection redact_credentials
        redact_exfiltration_urls register_reasoning_effort_values
        release_prewarmed_session reload_slot_session
        request_slot_origin resolve_agent_bindings
        resolve_folder_project_dir_off_loop resolve_session_agent_bindings resolved_row_identity
        restore_agent_selection restore_replacement_if_handover_did_not_land
        resume_slot_from_history row_mid safety_override save_slot_off_loop schedule_eager_spawn
        sel session_agent_selection_name session_start_failure_streak slot_history_key
        slot_switch_session_lock spawn_guarded_turn start_queue_persist
        steer_into_running_turn steer_is_auto stop_declined_armed stop_slot_turn
        subagents_attached_async tags_write_lock tempfile tighten_live_slot_memory_mode time
        timezone uuid validate_folder_tag_ids validate_tool_args
        voice_runtime_workspace_conflict wait_for_memory_preparation warm_project_agent_names
        warn_if_not_durable weakref web yolo_policy_permits
    """.split())

#: Every route the gateway registers whose handler is a ``chat_handlers`` object, as
#: ``(method, path, handler name)``; aiohttp adds a ``HEAD`` beside each ``GET``.
_BASE_ROUTES = (
    ("DELETE", "/api/chat/slots/{slot}", "api_chat_slot_delete"),
    ("DELETE", "/api/chat/slots/{slot}/queue/{queue_id}", "api_chat_slot_queue_cancel"),
    (
        "DELETE",
        "/api/chat/slots/{slot}/source-links/{identity}",
        "api_chat_slot_source_link_unlink",
    ),
    ("GET", "/api/chat/slots", "api_chat_slots"),
    ("GET", "/api/chat/slots/unrestored", "api_chat_slots_unrestored"),
    ("GET", "/api/chat/slots/{slot}", "api_chat_slot_detail"),
    ("GET", "/api/chat/slots/{slot}/autocompact", "api_chat_slot_autocompact"),
    (
        "GET",
        "/api/chat/slots/{slot}/selection-capabilities",
        "api_chat_slot_selection_capabilities",
    ),
    ("GET", "/api/chat/slots/{slot}/source-links", "api_chat_slot_source_links"),
    ("GET", "/api/chat/slots/{slot}/summary", "api_chat_slot_summary"),
    ("GET", "/api/recent-projects", "api_recent_projects"),
    ("GET", "/api/favorite-projects", "api_favorite_projects"),
    ("POST", "/api/favorite-projects", "api_favorite_project_add"),
    ("DELETE", "/api/favorite-projects", "api_favorite_project_remove"),
    ("PATCH", "/api/chat/slots/{slot}/color", "api_chat_slot_color"),
    ("PATCH", "/api/chat/slots/{slot}/queue/{queue_id}", "api_chat_slot_queue_edit"),
    ("POST", "/api/chat", "api_chat"),
    ("POST", "/api/chat/mode", "api_chat_mode"),
    ("POST", "/api/chat/slots", "api_chat_slot_create"),
    ("POST", "/api/chat/slots/cleanup", "api_chat_slots_cleanup"),
    ("POST", "/api/chat/slots/model", "api_chat_slots_model"),
    ("POST", "/api/chat/slots/{slot}/agent", "api_chat_slot_agent"),
    ("POST", "/api/chat/slots/{slot}/approve", "api_chat_slot_approve"),
    ("POST", "/api/chat/slots/{slot}/autocompact", "api_chat_slot_autocompact"),
    ("POST", "/api/chat/slots/{slot}/context", "api_chat_slot_context"),
    ("POST", "/api/chat/slots/{slot}/continue", "api_chat_slot_continue"),
    ("POST", "/api/chat/slots/{slot}/end-wait", "api_chat_slot_end_wait"),
    ("POST", "/api/chat/slots/{slot}/followup", "api_chat_slot_followup"),
    ("POST", "/api/chat/slots/{slot}/interrupt", "api_chat_slot_interrupt"),
    ("POST", "/api/chat/slots/{slot}/model", "api_chat_slot_model"),
    ("POST", "/api/chat/slots/{slot}/note", "api_chat_slot_note"),
    ("POST", "/api/chat/slots/{slot}/project", "api_chat_slot_project"),
    ("POST", "/api/chat/slots/{slot}/reasoning-effort", "api_chat_slot_reasoning_effort"),
    ("POST", "/api/chat/slots/{slot}/reload", "api_chat_slot_reload"),
    ("POST", "/api/chat/slots/{slot}/reset-conversation", "api_chat_slot_reset_conversation"),
    ("POST", "/api/chat/slots/{slot}/resume", "api_chat_slot_resume"),
    ("POST", "/api/chat/slots/{slot}/stop", "api_chat_slot_stop"),
    ("POST", "/api/chat/slots/{slot}/summary", "api_chat_slot_summary_generate"),
    ("POST", "/api/chat/slots/{slot}/workspace", "api_chat_slot_workspace"),
    ("PUT", "/api/chat/slots/{slot}/queue/order", "api_chat_slot_queue_reorder"),
)

#: The six runner seams ``chat_handlers`` imports by name.
_RUNNER_SEAMS = (
    "_context_usage_payload",
    "_run_chat",
    "_start_next_queued_turn",
    "_sync_served_model",
    "context_entry_expired",
    "schedule_eager_spawn",
)


# ── the surface ───────────────────────────────────────────────────────────────


def test_every_name_the_handlers_bound_at_the_base_still_resolves() -> None:
    """Callers, the ``dashboard.chat`` facade and tests read private names off the
    handlers module as well as public ones, so every module-level binding survives."""
    assert len(_BASE_NAMES) > 350
    assert sorted(name for name in _BASE_NAMES if not hasattr(ch, name)) == []


def test_a_fresh_interpreter_sees_every_base_public_name(tmp_path: Path) -> None:
    """The public names resolve in a process that imports nothing else first, and the
    ``dashboard.chat`` re-exports are the facade's own objects there too."""
    public = sorted(name for name in _BASE_NAMES if not name.startswith("_"))
    assert len(public) > 180
    script = """
        import sys
        import kiro_crew.dashboard.chat_handlers as ch
        from kiro_crew.dashboard import chat
        missing = [n for n in sys.argv[1:] if not hasattr(ch, n)]
        assert missing == [], missing
        foreign = [n for n in sys.argv[1:] if hasattr(chat, n) and getattr(chat, n) is not getattr(ch, n)]
        assert foreign == [], foreign
        print("ok")
        """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *public],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_every_route_still_dispatches_to_the_facade_handler() -> None:
    """The route table names the same handlers, and each is the facade's object."""
    from kiro_crew.dashboard import routes

    app = web.Application()
    routes.register_all(app)
    found = sorted(
        (route.method, route.resource.canonical, route.handler.__name__)
        for route in app.router.routes()
        if route.method != "HEAD"
        and getattr(ch, getattr(route.handler, "__name__", ""), None) is route.handler
    )
    assert found == sorted(_BASE_ROUTES)
    api_names = {name for name in vars(ch) if name.startswith("api_")}
    assert api_names == {name for _, _, name in _BASE_ROUTES}


def test_the_runner_seams_are_the_runner_objects() -> None:
    from kiro_crew.dashboard import chat_runner

    assert [
        name for name in _RUNNER_SEAMS if getattr(ch, name) is not getattr(chat_runner, name)
    ] == []


def test_the_chat_facade_reexports_the_handler_objects() -> None:
    from kiro_crew.dashboard import chat

    tree = ast.parse(Path(chat.__file__).read_text(encoding="utf-8"))
    names = [
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == _FACADE
        for alias in node.names
    ]
    assert len(names) >= 30
    assert [name for name in names if getattr(chat, name) is not getattr(ch, name)] == []


# ── contracts of the moved families ───────────────────────────────────────────


def _app_with(state, *routes_: tuple[str, str, object], app_claim: str = "") -> web.Application:
    """An app serving *routes_* to a dashboard owner, or to *app_claim*'s token."""

    @web.middleware
    async def _claims(request: web.Request, handler):
        request["app"] = app_claim
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_claims])
    app["state"] = state
    for method, path, handler in routes_:
        app.router.add_route(method, path, handler)
    return app


@pytest.mark.asyncio
async def test_deleting_a_missing_slot_answers_the_uniform_not_found(tmp_path) -> None:
    """The tab-close client reads any 404 as "already gone"; the body is the uniform one."""
    state = _make_state(tmp_path)
    app = _app_with(state, ("DELETE", "/api/chat/slots/{slot}", ch.api_chat_slot_delete))
    async with TestClient(TestServer(app)) as client:
        resp = await client.delete("/api/chat/slots/nope")
        assert resp.status == 404
        assert await resp.json() == {"error": "not found", "code": "slot_not_found"}


@pytest.mark.parametrize("owner", ["other-app", ""], ids=["foreign", "unscoped"])
@pytest.mark.asyncio
async def test_an_app_cannot_delete_a_slot_it_does_not_own(
    tmp_path, monkeypatch, owner: str
) -> None:
    """A slot an app does not own reads exactly like a missing one, and the true
    reason goes to the audit log instead, recorded by the ownership decision."""
    audit = MagicMock()
    monkeypatch.setattr(slot_ownership, "sel", lambda: audit)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot._app = owner
    app = _app_with(
        state, ("DELETE", "/api/chat/slots/{slot}", ch.api_chat_slot_delete), app_claim="my-app"
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.delete("/api/chat/slots/s1")
        assert resp.status == 404
        assert await resp.json() == {"error": "not found", "code": "slot_not_found"}
    assert state._slots.get("s1") is slot
    audit.log_api_access.assert_called_once_with(
        caller="my-app",
        operation="slot_delete",
        outcome="denied",
        source="app_isolation",
        resources="slot=s1",
        error="app does not own this slot" if owner else "app cannot access unscoped slots",
    )


@pytest.mark.asyncio
async def test_resume_without_a_conversation_log_is_refused_by_the_route_and_the_core(
    tmp_path,
) -> None:
    state = _make_state(tmp_path)
    state.conversation_log = None
    app = _app_with(state, ("POST", "/api/chat/slots/{slot}/resume", ch.api_chat_slot_resume))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
        assert resp.status == 400
        assert await resp.json() == {"error": "no conversation log", "code": "no_conversation_log"}
    outcome = await ch.resume_slot_from_history(state, name="s1")
    assert outcome == ch.ResumeOutcome(
        refusal=ch.ResumeRefusal("no conversation log", "no_conversation_log", 400)
    )


def test_the_resume_records_keep_their_fields() -> None:
    """``session_control`` builds and reads these by field name and by position."""
    assert ch.ResumeRefusal._fields == ("error", "code", "status")
    assert ch.ResumeOutcome._fields == ("refusal", "slot", "already_live", "total")
    assert ch.ResumeOutcome() == (None, None, False, 0)


@pytest.mark.asyncio
async def test_resume_reads_the_restore_inputs_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """The provider config and the effort marker are disk reads, taken on a worker
    thread before the slot is built, and the marker vouches only for the effort it read."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    loop_thread = threading.get_ident()
    seen: list[tuple[str, bool, object]] = []
    real_cfg, real_marker = ch._load_restore_cfg, ch._has_validated_effort_marker

    def _cfg():
        seen.append(("cfg", threading.get_ident() == loop_thread, None))
        return real_cfg()

    def _marker(raw):
        seen.append(("marker", threading.get_ident() == loop_thread, raw))
        return real_marker(raw)

    monkeypatch.setattr(ch, "_load_restore_cfg", _cfg)
    monkeypatch.setattr(ch, "_has_validated_effort_marker", _marker)
    state = _make_state(tmp_path)
    state.conversation_log.append("dashboard:r1", "user", "hello")
    state.conversation_log.update_metadata("dashboard:r1", {"reasoning_effort": "high"})
    outcome = await ch.resume_slot_from_history(state, name="r1", history_key="dashboard:r1")
    assert outcome.refusal is None and outcome.slot is not None
    assert seen == [("cfg", False, None), ("marker", False, "high")]


def _closing_state(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("w1")
    slot.append("user", "do the item")
    slot.drain()
    return state, slot


@pytest.mark.parametrize("committed", [True, False], ids=["committed", "rows-lost"])
@pytest.mark.asyncio
async def test_the_handover_exit_wakes_the_conductor_only_after_a_committed_drain(
    tmp_path, monkeypatch, committed: bool
) -> None:
    """A close that yields its archive to a recreated slot drains the tail OPEN; the
    conductor is told once the drain committed, and never when the rows were lost."""
    events: list[str] = []

    async def _drain(_state, name, _slot):
        events.append(f"drain:{name}")
        return ch._HandoverDrainResult(rows_committed=committed, prompts_lost=0)

    async def _wake(name: str) -> None:
        events.append(f"wake:{name}")

    async def _save(*_a, **_kw) -> None:
        events.append("archival-save")

    monkeypatch.setattr(ch, "_replacement_shares_transcript", lambda *_a: True)
    monkeypatch.setattr(ch, "_persist_handover_tail", _drain)
    monkeypatch.setattr(ch, "_resettle_restricted_key", lambda *_a: None)
    monkeypatch.setattr(ch, "_wake_conductor_for_closed_worker", _wake)
    monkeypatch.setattr(ch, "save_slot_off_loop", _save)
    state, slot = _closing_state(tmp_path)
    if committed:
        await ch.close_slot(state, slot, "w1")
        assert events == ["drain:w1", "wake:w1"]
    else:
        with pytest.raises(ch.SlotCloseError) as raised:
            await ch.close_slot(state, slot, "w1")
        assert (raised.value.code, raised.value.status) == ("history_save_failed", 500)
        assert events == ["drain:w1"]


def _module_level_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def _handler_files() -> list[Path]:
    """The facade and, once it exists, every module of ``chat_api``."""
    owners = _FACADE_PATH.parent / "chat_api"
    return [_FACADE_PATH, *sorted(owners.glob("*.py"))]


def test_conductor_wake_is_imported_where_it_wakes() -> None:
    """``conductor_wake`` imports the dashboard back, so a module-scope import of it
    would close a cycle; the close path's wake helper imports it in its own body."""
    holders = []
    for path in _handler_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_level = {
            name
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.If, ast.Try))
            for name in _module_level_imports(ast.Module(body=[node], type_ignores=[]))
        }
        assert "kiro_crew.conductor_wake" not in module_level, path.name
        for node in tree.body:
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_wake_conductor_for_closed_worker"
            ):
                local = {
                    f"{inner.module}.{alias.name}"
                    for inner in ast.walk(node)
                    if isinstance(inner, ast.ImportFrom)
                    for alias in inner.names
                }
                assert "kiro_crew.conductor_wake" in local
                holders.append(path.name)
    assert len(holders) == 1


# ── the composition ───────────────────────────────────────────────────────────

_OWNER_PACKAGE = chat_api.__name__
_OWNER_DIR = Path(chat_api.__file__).resolve().parent
_SRC = _FACADE_PATH.parents[2]
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"})

#: The owners the facade composes. Adding or removing one changes the composition,
#: so the set is spelled out rather than globbed.
_OWNER_MODULES = frozenset({"resume", "slot_detail", "slot_lifecycle", "source_links"})

#: Each moved name, the owner that holds it, and its kind and signature in the
#: one-module file (captured from it before the split).
_BASE_SURFACE: dict[str, tuple[tuple[str, str, str], ...]] = {
    "source_links": (
        (
            "api_chat_slot_source_links",
            "async function",
            "(request: 'web.Request') -> 'web.Response'",
        ),
        (
            "_audit_source_link_unlink",
            "function",
            "(name: 'str', outcome: 'str', **fields: 'Any') -> 'None'",
        ),
        (
            "api_chat_slot_source_link_unlink",
            "async function",
            "(request: 'web.Request') -> 'web.Response'",
        ),
        (
            "_apply_source_link_unlink",
            "async function",
            "(request: 'web.Request') -> 'web.Response'",
        ),
        ("_source_link_txn_lock", "function", "(history_key: 'str') -> 'asyncio.Lock'"),
    ),
    "slot_detail": (
        ("api_chat_slots", "async function", "(request: 'web.Request') -> 'web.Response'"),
        (
            "api_chat_slots_unrestored",
            "async function",
            "(request: 'web.Request') -> 'web.Response'",
        ),
        ("_finite_number", "function", "(value: 'Any') -> 'float | None'"),
        (
            "_context_reading",
            "function",
            "(pct: 'Any', used: 'Any', window: 'Any', *, stale: 'bool') -> 'dict[str, Any]'",
        ),
        (
            "_context_snapshot_fields",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\") -> 'dict[str, Any]'",
        ),
        (
            "_context_snapshot_fields_inner",
            "async function",
            "(state: \"'DashboardState'\", slot: \"'_ChatSlot'\") -> 'dict[str, Any]'",
        ),
        ("_load_redacted", "function", "(body: 'str') -> 'str'"),
        (
            "_same_persisted_body",
            "function",
            "(disk_body: 'str', window_body: 'str', role: 'str', disk_ts: 'str' = '', window_ts: 'str' = '') -> 'bool'",
        ),
        ("_is_answered_permission", "function", "(m: 'dict') -> 'bool'"),
        (
            "_snapshot_slot_window",
            "function",
            "(slot: \"'_ChatSlot'\") -> 'tuple[int, list[dict]]'",
        ),
        (
            "_append_unflushed_tail_from_offset",
            "function",
            "(slot: \"'_ChatSlot'\", all_msgs: 'list[dict]', *, disk_offset: 'int', snapshot: 'tuple[int, list[dict]] | None' = None) -> 'list[dict]'",
        ),
        (
            "_append_unflushed_tail",
            "function",
            "(slot: \"'_ChatSlot'\", all_msgs: 'list[dict]', *, snapshot: 'tuple[int, list[dict]] | None' = None) -> 'list[dict]'",
        ),
        ("_DurablePrefixMismatch", "class", ""),
        ("_durable_prefix_counter", "function", "(slot: \"'_ChatSlot'\") -> 'int'"),
        (
            "_bounded_slot_page",
            "function",
            "(conversation_log: 'Any', slot: \"'_ChatSlot'\", history_key: 'str', *, limit: 'int', before: 'int | None', snapshot: 'tuple[int, list[dict]]', durable_prefix_count: 'int') -> 'tuple[list[dict], int, bool, int]'",
        ),
        ("api_chat_slot_detail", "async function", "(request: 'web.Request') -> 'web.Response'"),
    ),
    "resume": (
        (
            "_reconcile_slot_window",
            "async function",
            "(state: 'DashboardState', slot: \"'_ChatSlot'\") -> 'None'",
        ),
        (
            "_resume_session_identity",
            "function",
            "(state: 'DashboardState', history_key: 'str') -> 'str'",
        ),
        (
            "_live_slot_for_resume",
            "async function",
            "(state, request_app: 'str', history_key: 'str', name: 'str', caller_label: 'str' = '') -> \"'ResumeOutcome | None'\"",
        ),
        ("_live_slot_resume_payload", "async function", "(state, existing) -> 'dict'"),
        ("_normalise_structured_content", "function", "(value: 'object') -> 'str'"),
        (
            "_redact_history_rows",
            "function",
            "(rows: 'list[dict]', *, window_limit: 'int | None' = None) -> 'list[dict]'",
        ),
        (
            "_materialise_slot_from_history",
            "function",
            "(state: 'DashboardState', *, name: 'str | None', history_key: 'str', meta: 'dict', all_messages: 'list[dict]', app: 'str' = '', request_title: 'str' = '', member_binding: 'dict | None' = None, folder_unhidden: 'bool' = True, folder_checked_id: 'str' = '', window_limit: 'int | None' = 500, disk_meta_observed: 'bool' = True, broadcast_rows: 'bool' = True, mint_missing_mids: 'bool' = False) -> '_ChatSlot'",
        ),
        (
            "_hydrate_slot_from_history",
            "function",
            "(state: 'DashboardState', slot: '_ChatSlot', *, meta: 'dict', all_messages: 'list[dict]', request_title: 'str' = '', member_binding: 'dict | None' = None, folder_unhidden: 'bool' = True, folder_checked_id: 'str' = '', window_limit: 'int | None' = 500, disk_meta_observed: 'bool' = True, broadcast_rows: 'bool' = True, mint_missing_mids: 'bool' = False) -> 'None'",
        ),
        ("api_chat_slot_resume", "async function", "(request: 'web.Request') -> 'web.Response'"),
        ("_resume_refusal_response", "function", "(refusal: 'ResumeRefusal') -> 'web.Response'"),
        (
            "resume_slot_from_history",
            "async function",
            "(state: \"'DashboardState'\", *, name: 'str', history_key: 'str | None' = None, request_app: 'str' = '', caller_label: 'str' = '', request_title: 'str' = '', containment: \"'Callable[[_ChatSlot], Awaitable[ResumeRefusal | None]] | None'\" = None, final_check: \"'Callable[[_ChatSlot], ResumeRefusal | None] | None'\" = None) -> 'ResumeOutcome'",
        ),
    ),
    "slot_lifecycle": (
        (
            "_slot_still_ours",
            "function",
            "(state: 'DashboardState', name: 'str', slot: '_ChatSlot') -> 'bool'",
        ),
        ("_NudgeRetireFailed", "class", ""),
        ("_retire_slot_nudge_loop", "async function", "(name: 'str') -> \"'NudgeLoop | None'\""),
        (
            "_restore_slot_nudge_loop",
            "async function",
            "(loop: \"'NudgeLoop | None'\", admission_check: 'Callable[[], bool]') -> 'None'",
        ),
        (
            "api_chat_slot_reset_conversation",
            "async function",
            "(request: 'web.Request') -> 'web.Response'",
        ),
        (
            "_release_closed_execution",
            "function",
            "(state: 'DashboardState', slot: \"'_ChatSlot'\", session_key: 'str', execution) -> 'None'",
        ),
        ("_pending_guarded_history_writes", "function", "(slot: \"'_ChatSlot'\") -> 'set'"),
        (
            "_await_guarded_history_write",
            "async function",
            "(slot: \"'_ChatSlot'\", name: 'str') -> 'bool'",
        ),
        ("_wake_conductor_for_closed_worker", "async function", "(name: 'str') -> 'None'"),
        (
            "close_slot",
            "async function",
            "(state: 'DashboardState', slot: \"'_ChatSlot'\", name: 'str', *, pre_pop_check: 'Callable[[], None] | None' = None) -> 'None'",
        ),
        (
            "_close_slot",
            "async function",
            "(state: 'DashboardState', slot: \"'_ChatSlot'\", name: 'str', *, pre_pop_check: 'Callable[[], None] | None' = None) -> 'None'",
        ),
        ("api_chat_slot_delete", "async function", "(request: 'web.Request') -> 'web.Response'"),
        ("api_chat_slots_cleanup", "async function", "(request: 'web.Request') -> 'web.Response'"),
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


def test_the_owner_set_is_the_package() -> None:
    assert {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])} == _OWNER_MODULES
    assert set(_BASE_SURFACE) <= _OWNER_MODULES


def _member(name: str) -> tuple[object, str]:
    obj = getattr(ch, name)
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
    file, lives in the owner its responsibility names, and is ONE object: the
    facade attribute and the owner's are the same."""
    obj, found_kind = _member(name)
    assert found_kind == kind
    if kind != "class":
        assert str(inspect.signature(obj)) == signature  # type: ignore[arg-type]
    assert getattr(_owner(owner), name) is obj


#: Module-level state and the definitions specs and the conftest name in
#: ``dashboard/chat_handlers.py``. Owner functions reach the state by name through
#: the facade's namespace, so a test that rebinds one on the facade is the binding
#: every function sees -- which holds only while no owner keeps a copy.
_FACADE_STATE = (
    "_test_interleave",
    "_source_link_unlink_tasks",
    "_source_link_txn_locks",
    "_autocompact_txn_locks",
    "_slot_switch_session_lock",
    "_GUARDED_WRITE_WAIT_SECS",
    "_CREATABLE_MODES",
    "RESERVED_ROW_META_KEYS",
    "logger",
)

#: Definitions that stay in the facade file: the summary routes the
#: session-summary spec names there, the Continue predicate the session spec
#: mirrors, the shared protocol records, the hand-over drain several guards read by
#: path, ``api_chat_slot_create`` and ``reload_slot_session``, whose eager-spawn
#: priority ``test_start_priority`` reads by function name in this file, and the
#: reload route kept beside its ``reload_slot_session`` core.
_FACADE_DEFS = (
    "api_chat_slot_summary",
    "api_chat_slot_summary_generate",
    "_has_conversation",
    "_persist_handover_tail",
    "api_chat_slot_create",
    "api_chat_slot_reload",
    "reload_slot_session",
    "ResumeRefusal",
    "ResumeOutcome",
    "SlotCloseError",
)


@pytest.mark.parametrize("name", _FACADE_STATE)
def test_facade_state_stays_on_the_facade(name: str) -> None:
    assert name in vars(ch)
    assert [o.__name__ for o in _owners() if name in vars(o)] == []


@pytest.mark.parametrize("name", _FACADE_DEFS)
def test_a_facade_definition_stays_in_the_facade_file(name: str) -> None:
    obj = getattr(ch, name)
    code = getattr(obj, "__code__", None)
    if code is not None:
        assert Path(code.co_filename).resolve() == _FACADE_PATH
    else:
        assert obj.__module__ == _FACADE
    assert [o.__name__ for o in _owners() if name in vars(o)] == []


def test_the_owners_log_as_the_facade() -> None:
    """Log capture keyed to ``kiro_crew.dashboard.chat_handlers`` keeps seeing the
    moved sites: an owner function logs through the facade's ``logger``."""
    assert ch.logger.name == _FACADE
    readers = [
        label for label, fn in _owner_functions() if "logger" in set(_global_names(fn.__code__))
    ]
    assert len(readers) >= 14


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.dashboard.chat_handlers.<name>`` reaches an owner
    function only because the function reads the facade's globals, not its own."""
    labels = {label for label, _ in _owner_functions()}
    assert len(labels) >= sum(
        kind.endswith("function") for rows in _BASE_SURFACE.values() for _, kind, _ in rows
    )
    strays = [
        label
        for label, fn in _owner_functions()
        if fn.__globals__ is not vars(ch) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_handlers_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_handlers_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from
    the facade surfaces only when its line runs -- often inside an ``except`` that
    turns the NameError into a refusal. The sweep makes it a test failure instead."""
    namespace = vars(ch)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_facade_reaches_an_owner_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract the rebinding exists for, exercised end to end."""
    monkeypatch.setattr(ch, "_normalise_structured_content", lambda value: f"normalised:{value}")
    rows = [{"role": "assistant", "content": ["a", "b"]}]
    assert ch._redact_history_rows(rows) == [
        {"role": "assistant", "content": "normalised:['a', 'b']"}
    ]


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs and pickling by reference read as before the split: every owner
    function resolves back through its own ``__module__`` and ``__qualname__``,
    and an owner's classes keep their own module."""
    wrong = []
    for label, fn in _owner_functions():
        target: object = sys.modules[fn.__module__]
        for part in fn.__qualname__.split("."):
            target = (
                vars(target).get(part) if isinstance(target, type) else getattr(target, part, None)
            )
            target = getattr(target, "__func__", target)
        if target is not fn:
            wrong.append(label)
    assert wrong == []
    assert ch._NudgeRetireFailed.__module__ == f"{_OWNER_PACKAGE}.slot_lifecycle"


def test_a_moved_function_reads_its_source_from_its_owner() -> None:
    source = inspect.getsource(ch.close_slot)
    assert source.startswith("async def close_slot(")
    assert inspect.getsourcefile(ch.close_slot) == _owner("slot_lifecycle").__file__


# ── one edge ──────────────────────────────────────────────────────────────────


def _package_of(path: Path) -> str:
    """The package a relative import in a file under ``src/`` resolves against."""
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def _import_targets(tree: ast.Module, package: str) -> list[tuple[ast.AST, str]]:
    """``(node, dotted module)`` for every module a tree imports, spelled any way:
    ``import a.b``, ``from a import b``, relative imports resolved against
    *package*, and a string-literal ``import_module(...)`` / ``__import__(...)``."""
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


def _within(target: str, module: str) -> bool:
    return target == module or target.startswith(f"{module}.")


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
    ``TYPE_CHECKING`` too."""
    tree = ast.parse(source)
    return sorted(
        {
            node.lineno
            for node, target in _import_targets(tree, package)
            if _within(target, _OWNER_PACKAGE)
        }
    )


def _owner_runtime_edges(source: str, package: str) -> list[int]:
    """Lines where an owner imports the facade or an owner outside ``TYPE_CHECKING``."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    return sorted(
        {
            node.lineno
            for node, target in _import_targets(tree, package)
            if (_within(target, _FACADE) or _within(target, _OWNER_PACKAGE))
            and id(node) not in guarded
        }
    )


#: Packages no owner imports in any spelling, ``TYPE_CHECKING`` included: the
#: agent-SDK boundary check counts a type-only edge too, so an owner names an ACP or
#: provider type through the facade; and nothing but the runner imports a
#: ``chat_turn`` module.
_OWNER_FORBIDDEN = ("kiro_crew.acp", "kiro_crew.providers", "kiro_crew.dashboard.chat_turn")


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import resume\n", True),
        ("from .resume import resume_slot_from_history\n", True),
        ("from .. import chat_handlers\n", True),
        ("from kiro_crew.dashboard import chat_handlers\n", True),
        ("import kiro_crew.dashboard.chat_handlers as handlers\n", True),
        ("def f():\n    from kiro_crew.dashboard.chat_handlers import sel\n", True),
        ("import importlib\nimportlib.import_module('kiro_crew.dashboard.chat_handlers')\n", True),
        ("__import__('kiro_crew.dashboard.chat_api.resume')\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.dashboard.chat_handlers import sel\n", False),
        ("from kiro_crew.dashboard import chat_utils\n", False),
        ("import kiro_crew.dashboard.chat_handlers_elsewhere\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_owner_runtime_edges(source, _OWNER_PACKAGE)) is flagged


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The facade is the one import path and the one patch surface, so no
    production module reaches past it."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if _OWNER_DIR in resolved.parents or resolved == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "chat_api" not in text:
            continue
        importers.extend(
            f"{path.relative_to(_SRC)}:{line}" for line in _owner_importers(text, _package_of(path))
        )
    assert importers == []


def test_an_owner_imports_the_facade_and_its_siblings_only_for_type_checking() -> None:
    """The facade imports the owners and nothing points back, so there is no
    import cycle to order."""
    offenders = [
        f"{stem}:{line}"
        for stem, source in _owner_sources().items()
        for line in _owner_runtime_edges(source, _OWNER_PACKAGE)
    ]
    assert offenders == []


def test_no_owner_imports_an_acp_provider_or_turn_module() -> None:
    offenders = [
        f"{stem}:{node.lineno}:{target}"
        for stem, source in _owner_sources().items()
        for node, target in _import_targets(ast.parse(source), _OWNER_PACKAGE)
        if any(_within(target, forbidden) for forbidden in _OWNER_FORBIDDEN)
    ]
    assert offenders == []
    probe = "if TYPE_CHECKING:\n    from kiro_crew.providers.acp import AcpProvider\n"
    assert [t for _, t in _import_targets(ast.parse(probe), _OWNER_PACKAGE)][
        0
    ] == "kiro_crew.providers.acp"


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


def test_a_fresh_facade_import_loads_every_owner(tmp_path: Path) -> None:
    """Importing the facade imports every owner with it: none loads lazily on a
    later call, so the import order stays the one the one-module file had."""
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.chat_handlers
        missing = [n for n in sys.argv[1:] if f"kiro_crew.dashboard.chat_api.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """,
        *sorted(_OWNER_MODULES),
    )


def test_a_second_facade_import_recomposes_the_owners_onto_it(tmp_path: Path) -> None:
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.chat_handlers as first
        del sys.modules["kiro_crew.dashboard.chat_handlers"]
        import kiro_crew.dashboard.chat_handlers as second
        from kiro_crew.dashboard.chat_api import resume
        fn = second.resume_slot_from_history
        assert second is not first
        assert fn.__globals__ is vars(second)
        assert resume.resume_slot_from_history is fn
        print("ok")
        """,
    )


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """The facade declares no ``__all__``, so every public binding goes out."""
    assert not hasattr(ch, "__all__")
    probe = tmp_path / "chat_handlers_star_probe.py"
    probe.write_text(
        "from kiro_crew.dashboard.chat_handlers import *  # noqa: F401,F403\n", encoding="utf-8"
    )
    spec = importlib.util.spec_from_file_location("chat_handlers_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (
        "resume_slot_from_history",
        "close_slot",
        "api_chat_slot_detail",
        "api_chat_slot_delete",
    ):
        assert getattr(module, name) is getattr(ch, name)


# ── the patch reach ───────────────────────────────────────────────────────────

_PATCH_CALLS = ("setattr", "patch.object", "delattr")
_MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_FACADE_STRING = re.compile(r"""^kiro_crew\.dashboard\.chat_handlers\.(\w+)$""")


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression spelling a test module binds to the facade, to a fixed point."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.dashboard":
            aliases |= {a.asname or a.name for a in node.names if a.name == "chat_handlers"}
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
            imported = (
                isinstance(value, ast.Call)
                and ast.unparse(value.func).endswith("import_module")
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == _FACADE
            )
            if ast.unparse(value) in aliases or imported:
                aliases.add(target.id)
                changed = True
    return aliases


def _parametrized_strings(function: ast.AST) -> dict[str, set[str]]:
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
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in params:
        return set(params[node.id])
    return None


def _resolve_target(node: ast.AST, params: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        match = _FACADE_STRING.match(node.value)
        return {match.group(1)} if match else set()
    if isinstance(node, ast.JoinedStr) and ast.unparse(node).startswith(f"f'{_FACADE}."):
        tail = node.values[-1]
        if len(node.values) == 2 and isinstance(tail, ast.FormattedValue):
            return _resolve_name(tail.value, params)
        return None
    return set()


def _patched_names_in(text: str) -> set[str]:
    """First-level names one test source rebinds on the facade; ``<dynamic>`` for
    a spelling the scan cannot resolve, which fails the reach test closed."""
    if "chat_handlers" not in text:
        return set()
    tree = ast.parse(text)
    aliases = _facade_aliases(tree)
    found: set[str] = set()
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    seen: set[int] = set()
    for scope, params in reversed(
        [(tree, {})] + [(fn, _parametrized_strings(fn)) for fn in functions]
    ):
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
                    elif func.endswith("patch.multiple"):
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
    found |= set(re.findall(r"""["']kiro_crew\.dashboard\.chat_handlers\.(\w+)["']""", text))
    return found


def _facade_patched_names() -> set[str]:
    root = repo_root()
    here = Path(__file__).resolve()
    found: set[str] = set()
    for path in repo_files_named(".py"):
        parts = path.relative_to(root).parts
        in_tests = parts[0] == "test" or (parts[0] == "src" and "tests" in parts)
        if in_tests and path.resolve() != here:
            found |= _patched_names_in(path.read_text(encoding="utf-8", errors="replace"))
    return found


def _captured_names(source: str) -> set[str]:
    """Names an owner module binds or evaluates when it LOADS, outside
    ``TYPE_CHECKING``: everything a later patch of the facade cannot reach."""
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
        "import kiro_crew.dashboard.chat_handlers as handlers\n"
        "from kiro_crew.dashboard import chat_handlers as ch\n"
        "facade = importlib.import_module('kiro_crew.dashboard.chat_handlers')\n"
        "alias = facade\n"
        "held = sys.modules['kiro_crew.dashboard.chat_handlers']\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(handlers, 'first', 1)\n"
        "    monkeypatch.setattr(ch, 'second', 2)\n"
        "    patch.object(alias, 'third')\n"
        "    ch.fourth = 4\n"
        "    monkeypatch.setattr('kiro_crew.dashboard.chat_handlers.fifth', 5)\n"
        "    monkeypatch.setattr(ch.Shared, 'attr', 7)\n"
        "    monkeypatch.setattr(other, 'not_the_facade', 8)\n"
        "    monkeypatch.delattr(ch, 'sixth')\n"
        "    patch.object(target=alias, attribute='seventh')\n"
        "    patch.multiple(held, eighth=1, create=True)\n"
        "@pytest.mark.parametrize('which', ['ninth', 'tenth'])\n"
        "def test_param(which):\n"
        "    patch(f'kiro_crew.dashboard.chat_handlers.{which}')\n"
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
    }
    dynamic = (
        "from kiro_crew.dashboard import chat_handlers as ch\n"
        "def test(monkeypatch, name):\n"
        "    monkeypatch.setattr(ch, name, 1)\n"
    )
    assert _patched_names_in(dynamic) == {"<dynamic>"}


def test_the_capture_scan_flags_what_an_owner_evaluates_when_it_loads() -> None:
    planted = (
        '"""An owner."""\n'
        "from typing import TYPE_CHECKING\n"
        "import asyncio as aio\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.dashboard.chat_handlers import sel\n"
        "LIMIT = _CAP * 2\n"
        "def f(x=_DEFAULT, *, y=_KW):\n"
        "    return sel(), save_slot_off_loop\n"
        "class C(_Base):\n"
        "    attr = _CLASS_BODY\n"
    )
    captured = _captured_names(planted)
    assert {"aio", "asyncio", "_CAP", "_DEFAULT", "_KW", "_Base", "_CLASS_BODY"} <= captured
    assert {"sel", "save_slot_off_loop"} & captured == set()


def test_no_owner_captures_a_name_tests_rebind_on_the_facade() -> None:
    """An owner that imported, defaulted or evaluated a rebound name when it loaded
    would keep that object, and a patch of the facade would silently stop applying
    there. An owner may DEFINE one: the facade's binding of it is the rebound copy,
    and every caller reads it through the facade's globals."""
    patched = _facade_patched_names()
    assert {
        "sel",
        "save_slot_off_loop",
        "_retire_slot_nudge_loop",
        "_wake_conductor_for_closed_worker",
        "_reauthorize_after_await",
        "resume_slot_from_history",
        "_GUARDED_WRITE_WAIT_SECS",
        "_test_interleave",
    } <= patched
    assert len(patched) >= 60
    assert "<dynamic>" not in patched, "a test patches a facade name the scan cannot resolve"
    for stem, source in _owner_sources().items():
        assert _captured_names(source) & patched == set(), stem
        defined = {name for name in vars(_owner(stem)) if name in patched}
        assert all(getattr(ch, name) is vars(_owner(stem))[name] for name in defined), stem


# ── the path-keyed guards keep their reach ────────────────────────────────────

#: Constructs repository guards read in ``dashboard/chat_handlers.py`` by path or
#: through the facade's module source: the agent-SDK boundary's three baselined
#: imports, the approval-mode writers, the
#: queue-clear the session-control doc counts, the app-actor turn kwargs, the
#: hold-branch persist, the stop chokepoints, the
#: synthesis boundary the frontend mirrors, the permission-resolution sites with
#: their dirty flags, and the approval-resolved broadcasts. An owner that grew one
#: would move it out of such a guard's sight, so each stays in the facade.
_STAYS_IN_THE_FACADE = (
    r"(?m)^from kiro_crew\.acp\.client import AcpModelUnavailable$",
    r"(?m)^from kiro_crew\.providers\.acp import AcpProvider$",
    r"(?m)^from kiro_crew\.providers\.base import LLMProvider$",
    r"safety_override\(\)\.activate",
    r"\._queue\.clear\(\)",
    r'_turn_kwargs\["_turn_actor"\]',
    r"warn_if_not_durable\(slot\._queue, qid, slot\.key\)",
    r"(?m)^def _unblock_pending_waits\(",
    r"(?m)^async def _reset_slot_session\(",
    r"(?m)^def _orphan_in_current_turn\(",
    r"_mark_permission_resolved\(",
    r'"approval_resolved"',
    r"issued_by_the_retraction=True",
)


@pytest.mark.parametrize("pattern", _STAYS_IN_THE_FACADE)
def test_a_construct_a_guard_reads_in_the_facade_stays_there(pattern: str) -> None:
    assert re.search(pattern, _FACADE_PATH.read_text(encoding="utf-8"))
    holders = [stem for stem, source in _owner_sources().items() if re.search(pattern, source)]
    assert holders == []


#: Calls guards check through the facade's own module source or file only: the
#: turn dispatch the turn-ceiling scan counts, and the permission-resolution
#: sites beside it.
#: The facade makes each call and no owner may, so a moved caller cannot slip past
#: a guard that never reads ``chat_api``.
_FACADE_ONLY_CALLS = frozenset(
    {
        "_run_chat",
        "spawn_guarded_turn",
        "_mark_permission_resolved",
    }
)

#: Harness-identity reads the agent-SDK ratchets ban in ``dashboard/chat_handlers.py``
#: (``test_agent_sdk_capabilities`` and ``test_agent_sdk_provider_identity`` read
#: that file only), banned in the owners too.
_IDENTITY_ATTRS = frozenset({"is_claude_backend", "_is_claude"})


def _called_names(tree: ast.AST) -> set[str]:
    return {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }


def _identity_reads(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _IDENTITY_ATTRS:
            found.append(f"{node.lineno}:.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in _IDENTITY_ATTRS | {"ACP_BACKEND_CLAUDE"}:
            found.append(f"{node.lineno}:{node.id}")
        elif isinstance(node, ast.Constant) and node.value in _IDENTITY_ATTRS:
            found.append(f"{node.lineno}:{node.value!r}")
        elif isinstance(node, ast.Compare) and any(
            isinstance(side, ast.Constant) and side.value == "claude_code"
            for side in (node.left, *node.comparators)
        ):
            found.append(f"{node.lineno}:== 'claude_code'")
    return found


def test_a_call_a_facade_guard_reads_is_made_only_by_the_facade() -> None:
    facade = _called_names(ast.parse(_FACADE_PATH.read_text(encoding="utf-8")))
    assert sorted(_FACADE_ONLY_CALLS - facade) == []
    callers = {
        stem: sorted(_FACADE_ONLY_CALLS & _called_names(ast.parse(source)))
        for stem, source in _owner_sources().items()
    }
    assert {stem: names for stem, names in callers.items() if names} == {}


def test_no_owner_reads_a_harness_identity() -> None:
    planted = (
        "def f(provider, backend):\n"
        "    if provider.is_claude_backend or getattr(provider, '_is_claude', False):\n"
        "        return backend == ACP_BACKEND_CLAUDE or provider.id == 'claude_code'\n"
    )
    assert len(_identity_reads(ast.parse(planted))) == 4
    reads = {stem: _identity_reads(ast.parse(source)) for stem, source in _owner_sources().items()}
    assert {stem: found for stem, found in reads.items() if found} == {}


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
        "chat_api_compose_owner_probe",
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
    namespace = {"__name__": "chat_api_compose_host_probe", "VALUE": "host"}
    namespace["helper"] = owner.helper
    chat_api.compose(namespace, (owner,))

    assert namespace["helper"]() == "host" and owner.helper is namespace["helper"]
    assert owner.outer()() == "host"
    assert owner.Tally().method() == "host"
    assert owner.Tally.static() == "host"
    assert owner.Tally().value == "host"
    assert owner.join.__module__ != "chat_api_compose_host_probe"
    assert owner.helper.__module__ == "chat_api_compose_host_probe"
    assert owner.Tally.__module__ == "chat_api_compose_owner_probe"
    namespace["VALUE"] = "patched"
    assert namespace["helper"]() == "patched"

    fresh = {"__name__": "chat_api_compose_host_probe", "VALUE": "fresh"}
    chat_api.compose(fresh, (owner,))
    assert owner.helper() == "fresh"
