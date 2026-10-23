"""The native Slack handler keeps its wire behaviour and its surface while its owners move.

``kiro_crew.slack.handler`` is the native Slack turn path's import path and its patch
surface. The goldens in ``test_slack_handler_composition_contract_golden.py`` drive its
public callbacks against the recording doubles of
``slack_handler_composition_contract_helpers.py`` and pin what reaches Slack, the SEL
ledger, the stats counters, the session manager and the provider, in order, as one
transcript per scenario.

This file pins the surface and its composition: every base name resolving on the facade,
where each definition lives (the facade or one owner), every owner function running on the
facade's globals, the owners' import edges, the reach of the suite's facade patches, and
the files that hold the constructs repository guards read.
"""

from __future__ import annotations

import ast
import builtins
import dis
import hashlib
import importlib
import importlib.util
import inspect
import json
import pkgutil
import re
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from source_corpus import repo_root

from kiro_crew._facade_compose import compose as facade_compose
from kiro_crew.slack import handler, handler_runtime
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# ── the surface ───────────────────────────────────────────────────────────────

#: Every module-level name ``handler.py`` bound at the base this split was cut from:
#: what it defined and what it imported, private names included, because the
#: gateway, the dashboard, the messaging transport and the tests read private names
#: off it. A name bound only by ``import <module>`` of a stdlib module is left out
#: (SD39): nothing reads ``json`` or ``re`` off the facade, and pinning one would
#: fail on the removal of an unused import. The project module objects it imports
#: ARE pinned, because owner functions read them as facade globals.
_BASE_NAMES = frozenset("""
    ACTIVATION_REVIEW APPROVAL_AUTO APPROVAL_INTERACTIVE AcpError AcpProcessDied
    AcpPromptBusy AcpTimeoutError Any ConfigReadError ContextBuilder ConversationLog
    CronService DEFAULT_PROVIDER DELIVERY_DEBT_NOTICE DENY_CAUSE_APPROVAL_TIMEOUT
    DENY_CAUSE_POLICY EVENT_COMPLETE EVENT_PERMISSION_REQUEST EVENT_TEXT_CHUNK
    EVENT_THINKING_CHUNK EVENT_TOOL_CALL HOOK_REPLY HUMAN_TURN_META_KEY HistoryConsolidator
    InboundRoute Iterator KiroCrewConfig LLMEvent LLMProvider MessageContext
    OUTCOME_REJECTED_TRANSPORT_FLOOR PROVIDER_SYSTEM Path PostedOptions
    SESSIONS_INCLUDE_ENDED_ARGS SLACK_MSG_LIMIT STEER_NOTICE_BOUND_SECS
    STOP_DECLINED_COMPACTING_TEXT STOP_REASON_CANCELLED STOP_REASON_COMPACTION_FAILED
    STOP_REASON_END_TURN SafetyOverride SensitiveAgentSpecPathError SessionClosingError
    SessionManager SlackClientOps StartPriority Stats StatusReactionController
    StreamRedactor SubagentManager TOOL_AUTO_APPROVE TOOL_DENY TRUNCATION_NOTICE
    TYPE_CHECKING Task TaskRunner ThreadReplies TurnCeilingExceeded UnknownMemoryStore
    _ACTION_APPROVE _ACTION_REJECT _ACTION_TRUST _APPROVAL_TIMEOUT _BANG_TO_SLASH
    _CC_AGENT_NAME_RE _CIRCUIT_BREAKER_THRESHOLD _CODING_KINDS _CODING_TOOLS
    _COMMENT_HOLD_MAX _COMPACTION_FAILED_RETRIES _COMPACTION_RETRY_NOTICE _CURSOR
    _CompactionReplay _DEFAULT_PHASE_EMOJIS _EDIT_INTERVAL _IMMEDIATE_PHASES
    _INCOGNITO_TOKEN_RE _LinkedApproval _LinkedApprovalEvent _NO_RESPONSE _OUTCOME_APPROVED
    _OUTCOME_REJECTED _PHASE_DEBOUNCE_SECS _PHASE_EMOJIS _PendingApproval
    _RESTRICTED_WRITE_MSG _REVIEW_DRAFT_MAX _REVIEW_DRAFT_TTL _REVIEW_PLACEHOLDER_TS
    _SLACK_OWNED_FIELDS _SLACK_SECTION_TEXT_LIMIT _STALL_EMOJI_HARD _STALL_EMOJI_SOFT
    _STALL_HARD_SECS _STALL_SOFT_SECS _STATUS_WORKING _STEER_NOTICE_BOUND_SECS
    _STREAM_CONTINUED _TEMPORARY_TOKEN_RE _TERMINAL_PHASES _THINKING _THINKING_PLACEHOLDER
    _THINKING_PREVIEW_LIMIT _TRUNCATION_MARKER _VoiceConfig _WEB_KINDS _WEB_TOOLS
    _YOLO_TTL_SECS _add_phase_reaction _add_trusted_session _allowed_users
    _append_footer_actions _apply_privacy_mode _at_tag_line_start _background_tasks
    _build_approval_blocks _build_phase_emojis _build_sessions_blocks _build_title_prompt
    _cached_default_agent _collect_recent_sessions_off_loop _comment_hold_is_protocol
    _condense_thinking _convert_tables _dashboard_state _discover_project_agents
    _display_redactor _filter_options_brackets _get_agent_for_session _get_auto_title_lock
    _get_default_agent _grant_linked_trust _handle_compact_command _handle_cron_command
    _handle_run_command _handle_sessions_command _handle_slash_command
    _handle_spawn_command _hydrate_conv_flags _hydrate_thread_overrides _hydrated_sessions
    _is_sessions_keyword _is_slack_restricted _iter_cc_agent_names _linked_approvals
    _linked_slots_for _linked_trust_grantable _list_all_agent_names _mark_incognito
    _mark_temporary _mark_titled _maybe_auto_title_slack _message_surface_limit
    _open_channels _orch_cfg _orphan_rejects _owner_id _pending_approvals
    _persist_channel_config _read_thread_overrides _reject_orphaned_tool _reload_orch_cfg
    _request_approval _resolve_agent_name _resolve_cc_agent_name _resolve_comment_hold
    _review_drafts _review_drafts_get _review_drafts_pop _review_drafts_set
    _safe_final_update _safe_update _safe_voice_reply _set_default_agent
    _shared_trusted_sessions _should_auto_approve_spawn _steer_host_deny _str_or_default
    _thread_agents _thread_incognito _thread_projects _thread_temporary _titled_threads
    _tool_to_phase _tracking_channels _trusted_sessions _tts_available
    _vc _voice_reply_fn add_trusted_session admit_inbound_callback
    adopt_slack_config agent_spec_stems annotations append_and_surface
    apply_config_duration auto_title await_replay_gap build_cancelled_turn_preamble
    build_session_replay build_timing_footer build_working_blocks cancel_background_tasks
    canonical_key cast channel_inbound_permitted clear_trusted_sessions
    compact_unsupported_backend compact_unsupported_reply compaction_in_flight config_path
    consume_reinjection consume_stop_declined copy_slack_fields count_redaction_tags
    cron_command_reply current_context dataclass decline_stop deprecation_warning_block
    describe_grant_lifetime describe_new_grant disable_yolo effective_session_key
    enable_yolo_with_ttl event_is_spawn_run expire_slack_options extract_options
    fetch_thread_parent get_dashboard_state get_orch_cfg grant_declared_yolo
    handle_interaction handle_message has_noted_turn has_prior_turns hook_gate_kwargs
    is_allowed_user is_control_tag_tail is_markdown_spec is_open_channel is_owner
    is_sensitive_path is_session_trusted is_slack_born is_slack_session_trusted
    is_thread_incognito is_thread_temporary is_tracked_channel is_wait_identity
    is_yolo_mode iter_agent_spec_files kiro_agents_dir load_voice_reply_config logger
    maybe_apply_privacy_modifiers maybe_handle_keyword_command maybe_route_linked_thread
    mint_options_token name_grant note_turn note_user_stop options_control_is_stale
    parent_prompt_text peek_data_home phase_emojis post_linked_approval privacy_mode
    project_agent_files project_agent_name publish_turn_identity read_agent_spec_strict
    read_config_text rearm_reinjection record_interaction_event record_thread_parent redact
    redact_credentials redact_exfiltration_urls redact_for_display redact_local_paths
    redaction_notice refresh_phase_emojis remember_slack_options render_one_for_slack
    replies_since_last_turn resolve_configured_provider resolve_linked_approval
    rollback_skill_bodies run_config_write run_in_embed_pool runtime_death
    safe_read_file_bytes safety_override save_conversation_turn_off_loop sel
    session_stop_generation session_store_for_turn sessions_include_ended set_allowed_users
    set_dashboard_state set_open_channels set_orch_cfg set_owner_id set_tracking_channels
    set_yolo_mode slack_cfg spawn_command_reply split_message steer_refusal_notice
    stop_reason_landed strip_control_comments strip_thinking_tags task_command_reply
    track_background_task turn_ceiling update_config_locked validated_config_string
    window_for_provider_client yolo_policy_permits
""".split())

#: Each definition moved out of ``handler.py`` and the owner its responsibility puts
#: it in.
_BASE_OWNERS: dict[str, tuple[str, ...]] = {
    "access": tuple("""
        _reload_orch_cfg add_trusted_session adopt_slack_config cancel_background_tasks
        copy_slack_fields disable_yolo enable_yolo_with_ttl get_dashboard_state
        get_orch_cfg is_allowed_user is_open_channel is_owner is_slack_session_trusted
        is_tracked_channel is_yolo_mode set_allowed_users set_dashboard_state
        set_open_channels set_orch_cfg set_owner_id set_tracking_channels set_yolo_mode
        slack_cfg track_background_task
        """.split()),
    "agent_names": tuple("""
        _discover_project_agents _list_all_agent_names _resolve_agent_name
        _resolve_cc_agent_name
        """.split()),
    "approvals": tuple("""
        _LinkedApproval _LinkedApprovalEvent _PendingApproval _build_approval_blocks
        _grant_linked_trust _linked_slots_for _linked_trust_grantable post_linked_approval
        resolve_linked_approval
        """.split()),
    "commands": tuple("""
        _handle_compact_command _handle_cron_command _handle_run_command
        _handle_slash_command _handle_spawn_command _is_sessions_keyword
        """.split()),
    "delivery": tuple("""

        """.split()),
    "dispatch": tuple("""
        _handle_sessions_command maybe_handle_keyword_command
        """.split()),
    "finalize": tuple("""
        _append_footer_actions _maybe_auto_title_slack _review_drafts_get
        _review_drafts_pop _review_drafts_set build_timing_footer
        """.split()),
    "inbound": tuple("""
        _apply_privacy_mode _get_agent_for_session _get_default_agent _hydrate_conv_flags
        _hydrate_thread_overrides _is_slack_restricted _persist_channel_config
        _read_thread_overrides _set_default_agent maybe_apply_privacy_modifiers
        maybe_route_linked_thread
        """.split()),
    "permission_ladder": tuple("""
        _reject_orphaned_tool _request_approval _should_auto_approve_spawn _steer_host_deny
        handle_interaction
        """.split()),
    "reactions": tuple("""
        StatusReactionController _add_phase_reaction _tool_to_phase phase_emojis
        refresh_phase_emojis
        """.split()),
    "stream": tuple("""
        _at_tag_line_start _comment_hold_is_protocol _filter_options_brackets
        _resolve_comment_hold _safe_final_update _safe_update
        """.split()),
    "turn": tuple("""

        """.split()),
    "turn_context": tuple("""

        """.split()),
    "voice": tuple("""
        _safe_voice_reply _str_or_default load_voice_reply_config
        """.split()),
}

_MOVED = frozenset(name for names in _BASE_OWNERS.values() for name in names)

#: Functions and classes the split introduced: phases of ``handle_message``,
#: ``handle_interaction`` and ``_handle_slash_command`` that each moved out as one
#: piece, and the turn object ``handle_message``'s phases share, each with the owner it
#: lives in.
_SPLIT_HELPERS: dict[str, str] = {
    "_AnswerStream": "stream",
    "_SlackTurn": "turn",
    "_answer_permission_request": "permission_ladder",
    "_bang_agent": "commands",
    "_bang_allowlist": "commands",
    "_bang_channel": "commands",
    "_bang_dashboard": "commands",
    "_bang_link_to_dashboard": "commands",
    "_bang_project": "commands",
    "_bang_stop": "commands",
    "_bang_thread_agent": "commands",
    "_bang_title": "commands",
    "_bang_voice": "commands",
    "_bang_yolo": "commands",
    "_deliver_answer": "delivery",
    "_dispatch_before_turn": "dispatch",
    "_grant_late_trust": "approvals",
    "_mirror_to_dashboard": "finalize",
    "_post_review_draft": "finalize",
    "_reply_by_voice": "voice",
    "_resolve_linked_click": "approvals",
    "_route_bang_command": "commands",
    "_run_turn": "turn",
    "_screen_tool_call": "permission_ladder",
    "_thread_context": "turn_context",
    "_thread_meta_fallback": "turn_context",
}

#: SHA-256 of the sorted ``"<name> <kind> <signature>"`` lines of every moved name,
#: captured from the one-module file before the split.
_BASE_SHAPE_DIGEST = "6c782deaca93e4395ca8d4f47f8b25ce32071d2cd1a36e7828a746e227ab45f7"

#: Definitions that stay in the facade file, each because a repository guard, a
#: contract or the import-time body reads it there. The reason per entry is in
#: ``docs/system-specs/modules/slack-gateway.md`` ("Native handler composition").
_FACADE_DEFS = (
    "_display_redactor",
    "_condense_thinking",
    "_build_phase_emojis",
    "_VoiceConfig",
    "_CompactionReplay",
    "_iter_cc_agent_names",
    "MessageContext",
    "handle_message",
)


_FACADE = handler.__name__
_FACADE_PATH = Path(handler.__file__).resolve()
_OWNER_PACKAGE = handler_runtime.__name__
_OWNER_DIR = Path(handler_runtime.__file__).resolve().parent
_ROOT = repo_root()
_SRC = _ROOT / "src"


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{_OWNER_PACKAGE}.{stem}")


def _owners() -> list[types.ModuleType]:
    return [_owner(info.name) for info in pkgutil.iter_modules([str(_OWNER_DIR)])]


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(_OWNER_DIR.glob("[!_]*.py"))
    }


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines, methods included."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members += [(f"{name}.{m}", v) for m, v in vars(value).items()]
            for label, member in members:
                fn = getattr(member, "__func__", member)
                if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                    found.append((f"{owner.__name__.rsplit('.', 1)[-1]}.{label}", fn))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"):
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _run_child(tmp_path: Path, script: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        capture_output=True,
        timeout=180,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_every_name_the_facade_bound_at_the_base_still_resolves() -> None:
    assert len(_BASE_NAMES) == 321
    assert sorted(name for name in _BASE_NAMES if not hasattr(handler, name)) == []


def test_a_fresh_interpreter_sees_the_surface_and_loads_every_owner(tmp_path: Path) -> None:
    """Importing the facade imports every owner with it, so none loads lazily on a
    later call and the import order stays the one the one-module file had; the
    public names resolve in a process that imports nothing else first."""
    public = sorted(name for name in _BASE_NAMES if not name.startswith("_"))
    assert len(public) > 100
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.slack.handler as handler
        owners = sys.argv[1].split(",")
        missing = [n for n in sys.argv[2:] if not hasattr(handler, n)]
        unloaded = [o for o in owners if f"kiro_crew.slack.handler_runtime.{o}" not in sys.modules]
        assert missing == [] and unloaded == [], (missing, unloaded)
        print("ok")
        """,
        ",".join(_BASE_OWNERS),
        *public,
    )


def test_no_owner_function_keeps_its_own_globals_once_the_entry_points_load(
    tmp_path: Path,
) -> None:
    """The facade binds every owner's names (its re-export blocks) before ``compose`` runs
    at its foot, so an import cycle that reached an owner function in between would hold
    one still running on the owner's globals. Load the gateway and the dashboard server
    first, as a process does, then sweep every live function compiled from an owner."""
    _run_child(
        tmp_path,
        """
        import gc, os, types
        import kiro_crew.slack.gateway
        import kiro_crew.dashboard.server
        import kiro_crew.slack.handler as handler

        def norm(path):
            return os.path.normcase(os.path.realpath(path))

        # Only a REACHABLE function can be called. compose replaces each owner function,
        # and an import can leave the replaced one in a reference cycle (a caught
        # exception's frames, say) that only the collector frees; whether it has run
        # yet depends on allocation, so collect before looking.
        gc.collect()
        root = norm(os.path.join(os.path.dirname(handler.__file__), "handler_runtime"))
        package = norm(os.path.join(root, "__init__.py"))
        owned = [
            fn
            for fn in gc.get_objects()
            if isinstance(fn, types.FunctionType)
            and norm(fn.__code__.co_filename).startswith(root + os.sep)
            and norm(fn.__code__.co_filename) != package
        ]
        stray = sorted(fn.__qualname__ for fn in owned if fn.__globals__ is not vars(handler))
        assert len(owned) > 100 and stray == [], (len(owned), stray)
        print("ok")
        """,
    )


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """The facade declares no ``__all__`` (nor did the one-module file), so every
    public binding goes out. Loaded through ``exec_module`` from a probe file."""
    assert not hasattr(handler, "__all__")
    probe = tmp_path / "slack_handler_star_probe.py"
    probe.write_text("from kiro_crew.slack.handler import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("slack_handler_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("is_tracked_channel", "post_linked_approval", "build_timing_footer", "slack_cfg"):
        assert getattr(module, name) is getattr(handler, name)


def test_the_shared_trackers_are_still_the_shared_objects() -> None:
    """The aliases conftest clears and the channel-neutral owners read are the very
    objects ``messaging`` keeps, not copies an owner could hold."""
    from kiro_crew.config import paths
    from kiro_crew.messaging import auto_title, privacy_mode, session_trust

    assert handler._trusted_sessions is session_trust._trusted_sessions
    assert handler._thread_temporary is privacy_mode._temporary
    assert handler._thread_incognito is privacy_mode._incognito
    assert handler._titled_threads is auto_title._titled
    assert handler.is_thread_temporary is privacy_mode.is_temporary
    assert handler.is_thread_incognito is privacy_mode.is_incognito
    assert handler._get_auto_title_lock is auto_title.get_lock
    assert handler.kiro_agents_dir is paths.kiro_agents_dir


def _facade_importers() -> dict[str, set[str]]:
    """``{module path: names}`` every source file imports from the facade by name."""
    found: dict[str, set[str]] = {}
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        if path.resolve() == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if _FACADE not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == _FACADE:
                found.setdefault(path.relative_to(_SRC).as_posix(), set()).update(
                    alias.name for alias in node.names
                )
    return found


def test_every_name_a_production_module_imports_from_the_facade_resolves() -> None:
    """Many of these imports are function-local, so a name that stopped resolving
    would first fail at request time rather than at import."""
    importers = {
        path: names
        for path, names in _facade_importers().items()
        if not path.startswith("kiro_crew/slack/handler_runtime/")
    }
    assert len(importers) >= 12
    imported = {name for names in importers.values() for name in names}
    assert sorted(name for name in imported if not hasattr(handler, name)) == []
    assert {"handle_message", "is_tracked_channel", "_vc", "load_voice_reply_config"} <= imported


# ── the composition ───────────────────────────────────────────────────────────


def test_the_owner_set_is_the_package() -> None:
    assert {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])} == set(_BASE_OWNERS)


def _shape(obj: object) -> str:
    if inspect.isclass(obj):
        return "class"
    prefix = "async def " if inspect.iscoroutinefunction(obj) else "def "
    return prefix + str(inspect.signature(obj))


def test_every_moved_name_is_one_object_in_its_owner() -> None:
    """The facade's binding of a moved name is the owner's object, in the owner its
    responsibility names, and no name is defined by two owners."""
    placed = {**{n: o for o, names in _BASE_OWNERS.items() for n in names}, **_SPLIT_HELPERS}
    strays = [
        f"{owner}:{name}"
        for name, owner in placed.items()
        if getattr(handler, name) is not vars(_owner(owner)).get(name)
    ]
    assert strays == []
    names = [name for group in _BASE_OWNERS.values() for name in group]
    assert len(names) == len(set(names)) == 81
    assert not set(_SPLIT_HELPERS) & set(names)


def test_the_moved_names_keep_their_base_shapes() -> None:
    lines = sorted(f"{name} {_shape(getattr(handler, name))}" for name in _MOVED)
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert digest == _BASE_SHAPE_DIGEST, "\n".join(lines)


def _module_assignments(source: str) -> set[str]:
    names = set()
    for node in ast.parse(source).body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return names


def test_facade_state_stays_on_the_facade() -> None:
    """Owner functions reach module state by name through the facade's namespace, so
    a test that rebinds one there is the binding every function sees -- which holds
    only while no owner keeps a copy."""
    state = _module_assignments(_FACADE_PATH.read_text(encoding="utf-8"))
    assert {"_pending_approvals", "_linked_approvals", "_vc", "_owner_id", "logger"} <= state
    assert {"_thread_agents", "_PHASE_EMOJIS", "_trusted_sessions", "_orphan_rejects"} <= state
    owned = {stem: sorted(_module_assignments(src)) for stem, src in _owner_sources().items()}
    assert {stem: names for stem, names in owned.items() if names} == {}


@pytest.mark.parametrize("name", _FACADE_DEFS)
def test_a_facade_definition_stays_in_the_facade_file(name: str) -> None:
    obj = getattr(handler, name)
    code = getattr(obj, "__code__", None)
    if code is not None:
        assert Path(code.co_filename).resolve() == _FACADE_PATH
    else:
        assert obj.__module__ == _FACADE
    assert [o.__name__ for o in _owners() if name in vars(o)] == []


def test_every_definition_is_in_exactly_one_place() -> None:
    """The facade keeps what ``_FACADE_DEFS`` names and the owners hold the rest:
    together they are the one-module file's definitions, each once, plus the phases
    the split lifted out of three long functions."""
    defined = {
        node.name
        for node in ast.parse(_FACADE_PATH.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert defined == set(_FACADE_DEFS)
    assert len(defined | _MOVED) == len(defined) + len(_MOVED) == 89


def test_the_owners_log_as_the_facade() -> None:
    """Log capture keyed to ``kiro_crew.slack.handler`` keeps seeing the moved sites."""
    assert handler.logger.name == _FACADE
    readers = [
        label for label, fn in _owner_functions() if "logger" in set(_global_names(fn.__code__))
    ]
    assert len(readers) >= 30


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.slack.handler.<name>`` reaches an owner function only
    because the function reads the facade's globals, not its own."""
    functions = _owner_functions()
    assert len(functions) >= 110
    strays = [
        label
        for label, fn in functions
        if fn.__globals__ is not vars(handler) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_handler_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_handler_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from the
    facade surfaces only when its line runs -- often inside an ``except`` that turns
    the NameError into a logged failure. The sweep makes it a test failure instead."""
    namespace = vars(handler)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_facade_reaches_an_owner_function() -> None:
    """The contract the rebinding exists for, on seams the suite actually patches:
    the owner id the access predicates read, the redactor the approval prompt runs,
    and the default-agent writer the ``!agent`` command calls."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handler, "_owner_id", "UPATCHED")
        assert handler.is_allowed_user("UPATCHED") is True
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handler, "redact_credentials", lambda text: ("<scrubbed>", []))
        blocks = handler._build_approval_blocks(
            SimpleNamespace(request_id=1, title="t", tool_input="", tool_purpose="")
        )
        assert "<scrubbed>" in json.dumps(blocks)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handler, "is_yolo_mode", lambda: True)
        assert handler.is_yolo_mode() is True


def test_a_global_write_from_an_owner_lands_on_the_facade() -> None:
    """``set_owner_id``, ``set_allowed_users`` and ``set_dashboard_state`` declare
    ``global`` and live in an owner. ``compose`` replaces a function's
    ``__globals__``, so the ``STORE_GLOBAL`` writes the facade's namespace."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(handler, "_owner_id", "")
        mp.setattr(handler, "_allowed_users", set())
        mp.setattr(handler, "_dashboard_state", None)
        handler.set_owner_id("UWRITE")
        handler.set_allowed_users({"UWRITE"})
        sentinel = object()
        handler.set_dashboard_state(sentinel)
        assert handler._owner_id == "UWRITE" and handler.is_owner("UWRITE")
        assert handler._allowed_users == {"UWRITE"}
        assert handler.get_dashboard_state() is sentinel
        assert "_owner_id" not in vars(_owner("access"))


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs and pickling by reference read as before the split: every owner module
    function resolves back through its own ``__module__`` and ``__qualname__``."""
    wrong = []
    for label, fn in _owner_functions():
        if "." in fn.__qualname__:
            continue  # a method: resolved through its class below
        if getattr(sys.modules[fn.__module__], fn.__qualname__, None) is not fn:
            wrong.append(label)
    assert wrong == []


def test_a_moved_class_keeps_its_owner_module() -> None:
    """SD35(5): a moved class names the module that defines it; nothing renders the
    class text to a user, so it is not relabelled."""
    assert handler.StatusReactionController.__module__ == f"{_OWNER_PACKAGE}.reactions"
    assert handler._PendingApproval.__module__ == f"{_OWNER_PACKAGE}.approvals"
    assert handler._AnswerStream.__module__ == f"{_OWNER_PACKAGE}.stream"
    method = handler.StatusReactionController.set_phase
    assert method.__globals__ is vars(handler)


def test_a_moved_function_reads_its_source_from_its_owner() -> None:
    source = inspect.getsource(handler.post_linked_approval)
    assert source.startswith("async def post_linked_approval(")
    assert inspect.getsourcefile(handler.post_linked_approval) == _owner("approvals").__file__


def test_the_facade_composes_through_the_shared_helper() -> None:
    """The facade composes its owners with the one shared helper,
    ``kiro_crew._facade_compose.compose``, calling it exactly once, and the owner
    package keeps no copy of its own: its ``__init__`` defines no function."""
    assert handler._facade_compose is facade_compose
    source = Path(handler.__file__).read_text(encoding="utf-8")
    assert source.count("\n_facade_compose(\n") == 1
    package = ast.parse(Path(handler_runtime.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in package.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []


# ── one edge ──────────────────────────────────────────────────────────────────


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    return {
        id(sub)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }


def _runtime_project_imports(source: str) -> list[int]:
    """Lines where a module imports a project module at runtime (outside
    ``TYPE_CHECKING`` and outside function bodies), spelled any way."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    in_functions = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(node)
    }
    lines = []
    for node in ast.walk(tree):
        if id(node) in guarded or id(node) in in_functions:
            continue
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = [("." * node.level) + (node.module or "")]
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            targets = [str(node.args[0].value)]
        if any(t.startswith(("kiro_crew", ".")) for t in targets):
            lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import stream\n", True),
        ("from .stream import _AnswerStream\n", True),
        ("from kiro_crew.slack import handler\n", True),
        ("import kiro_crew.slack.handler as handler\n", True),
        ("import importlib\nimportlib.import_module('kiro_crew.slack.handler')\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.slack.handler import sel\n", False),
        ("def f():\n    from kiro_crew.slack.blocks import review_draft_blocks\n", False),
        ("import asyncio\nfrom typing import Any\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_runtime_project_imports(source)) is flagged


def test_an_owner_imports_project_modules_only_for_type_checking() -> None:
    """An owner's module-level imports are stdlib only, plus its ``TYPE_CHECKING``
    names from the facade: the gateway imports the handler at boot, so an owner adds
    no import edge to that path, and the agent-SDK boundary sees no new edge."""
    offenders = {
        stem: lines
        for stem, source in _owner_sources().items()
        if (lines := _runtime_project_imports(source))
    }
    assert offenders == {}
    for source in _owner_sources().values():
        tree = ast.parse(source)
        guarded = [
            node
            for node in ast.walk(tree)
            if id(node) in _type_checking_nodes(tree) and isinstance(node, ast.ImportFrom)
        ]
        assert {node.module for node in guarded} <= {_FACADE}


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The facade is the one import path and the one patch surface."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if _OWNER_DIR in resolved.parents or resolved == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "handler_runtime" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = " ".join(a.name for a in node.names)
            if _OWNER_PACKAGE in module:
                importers.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert importers == []


def _non_literal_defaults(source: str) -> list[str]:
    """Owner defaults evaluated from a name: an owner's own namespace binds no state,
    so such a default would be read at owner import from the wrong module, or fail."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None and not isinstance(default, ast.Constant):
                    found.append(f"{node.name}:{node.lineno}")
    return found


def test_an_owner_default_is_a_literal() -> None:
    assert _non_literal_defaults("def f(a=LIMIT):\n    return a\n") == ["f:1"]
    assert _non_literal_defaults("def f(a=3, *, b=None):\n    return a\n") == []
    assert {
        stem: d for stem, src in _owner_sources().items() if (d := _non_literal_defaults(src))
    } == {}


# ── the patch reach ───────────────────────────────────────────────────────────


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression a test module binds to the facade, to a fixed point."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.slack":
            aliases |= {a.asname or a.name for a in node.names if a.name == "handler"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target, value = node.targets[0], node.value
            if not isinstance(target, ast.Name) or target.id in aliases:
                continue
            if ast.unparse(value) in aliases or (
                isinstance(value, ast.Call)
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == _FACADE
                and ast.unparse(value.func).endswith(("import_module", "__import__"))
            ):
                aliases.add(target.id)
                changed = True
    return aliases


def _dotted_target(expr: ast.AST) -> str | None:
    """The facade attribute a dotted-string patch target names, if it is the facade's."""
    if not (isinstance(expr, ast.Constant) and isinstance(expr.value, str)):
        return None
    prefix = f"{_FACADE}."
    rest = expr.value[len(prefix) :] if expr.value.startswith(prefix) else ""
    return rest.split(".", 1)[0] or None


def _facade_writes(source: str) -> set[str]:
    """Every facade attribute *source* patches, rebinds, deletes or asserts identity on:
    ``monkeypatch.setattr``/``delattr``/``setitem`` in the object and dotted-string
    forms, ``mock.patch`` / ``patch.object`` through any alias, plain attribute
    assignment, and ``is`` comparisons."""
    tree = ast.parse(source)
    aliases = _facade_aliases(tree)
    found: set[str] = set()

    def _attr_of(node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute) and ast.unparse(node.value) in aliases:
            return node.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found |= {a for a in (_attr_of(t) for t in targets) if a}
        elif isinstance(node, ast.Delete):
            found |= {a for a in (_attr_of(t) for t in node.targets) if a}
        elif isinstance(node, ast.Call):
            call = ast.unparse(node.func)
            kwargs = {k.arg: k.value for k in node.keywords}
            positional = list(node.args)
            if call.endswith(
                ("setattr", "delattr", "setitem", "object", "multiple")
            ) or re.fullmatch(r"(\w+\.)*(patch|mock)", call):
                target = kwargs.get("target") or (positional[0] if positional else None)
                if target is None:
                    continue
                name = _dotted_target(target)
                if name:
                    found.add(name)
                elif ast.unparse(target) in aliases:
                    attr = kwargs.get("attribute") or (
                        positional[1] if len(positional) > 1 else None
                    )
                    if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                        found.add(attr.value)
                    else:
                        found |= {k for k in kwargs if k not in ("target", "attribute")}
        elif isinstance(node, ast.Compare) and any(isinstance(op, ast.Is) for op in node.ops):
            for side in (node.left, *node.comparators):
                attr = _attr_of(side)
                if attr:
                    found.add(attr)
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import kiro_crew.slack.handler as h\nmonkeypatch.setattr(h, '_EDIT_INTERVAL', 0)\n",
            {"_EDIT_INTERVAL"},
        ),
        ("from kiro_crew.slack import handler\nmonkeypatch.setattr(handler, 'os', 1)\n", {"os"}),
        ("monkeypatch.setattr('kiro_crew.slack.handler.is_owner', None)\n", {"is_owner"}),
        ("from unittest import mock\nmock.patch('kiro_crew.slack.handler.sel')\n", {"sel"}),
        ("patch('kiro_crew.slack.handler.Path.home')\n", {"Path"}),
        (
            "from kiro_crew.slack import handler as h\npatch.object(h, '_PendingApproval')\n",
            {"_PendingApproval"},
        ),
        ("import kiro_crew.slack.handler as h\nh.sel = lambda: None\n", {"sel"}),
        (
            "import kiro_crew.slack.handler as h\nassert h._trusted_sessions is t\n",
            {"_trusted_sessions"},
        ),
        ("import kiro_crew.slack.handler as h\nx = h._EDIT_INTERVAL\n", set()),
        ("monkeypatch.setattr('kiro_crew.slack.events.is_owner', None)\n", set()),
        ("monkeypatch.setattr(other, '_owner_id', 1)\n", set()),
    ],
)
def test_the_patch_target_reader_sees_every_spelling(source: str, expected: set[str]) -> None:
    """The detector finds each form and ignores a read, a sibling module's target and
    an unrelated object, so the sweep below is neither vacuous nor over-broad."""
    assert _facade_writes(source) == expected


def _test_sources() -> list[tuple[str, str]]:
    paths = sorted((_ROOT / "test").rglob("*.py"))
    out = []
    for path in paths:
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _FACADE in text or "kiro_crew.slack import handler" in text:
            out.append((path.relative_to(_ROOT).as_posix(), text))
    return out


def _patched_facade_names() -> set[str]:
    return {name for _, text in _test_sources() for name in _facade_writes(text)}


def test_the_corpus_sweep_finds_the_seams_the_suite_is_known_to_patch() -> None:
    patched = _patched_facade_names()
    assert len(patched) >= 60, sorted(patched)
    assert {
        "is_yolo_mode",
        "is_tracked_channel",
        "sel",
        "_build_approval_blocks",
        "Path",
        "is_owner",
        "_set_default_agent",
        "_PendingApproval",
        "is_allowed_user",
        "publish_turn_identity",
        "_get_default_agent",
        "_resolve_agent_name",
        "disable_yolo",
        "_pending_approvals",
        "KiroCrewConfig",
        "_collect_recent_sessions_off_loop",
        "is_sensitive_path",
        "os",
        "_owner_id",
        "kiro_agents_dir",
        "_EDIT_INTERVAL",
        "_STALL_SOFT_SECS",
        "_trusted_sessions",
    } <= patched, sorted(patched)
    assert sorted(name for name in patched if not hasattr(handler, name)) == []


def _runtime_shadows(source: str) -> dict[str, list[int]]:
    """Names an owner binds in a way that would shadow the facade's at call time: a
    FUNCTION-LOCAL import (it wins over the facade global for that call) or a
    MODULE-LEVEL assignment (owner state ``compose`` never moves). A module-level
    import is inert and is not reported; nor is a ``global``-declared write."""
    tree = ast.parse(source)
    inside_a_function = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(node)
    }
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) in inside_a_function:
            for alias in node.names:
                found.setdefault((alias.asname or alias.name).split(".")[0], []).append(node.lineno)
    for name in _module_assignments(source):
        found.setdefault(name, []).append(0)
    return found


def test_the_binding_sweep_reports_a_shadow_and_ignores_an_inert_import() -> None:
    assert "sel" in _runtime_shadows(
        "def f():\n    from kiro_crew.sel import sel\n\n    return sel\n"
    )
    assert "CAP" in _runtime_shadows("CAP = 7\n")
    assert "os" not in _runtime_shadows("import os\n\n\ndef f():\n    return os.name\n")
    assert "X" not in _runtime_shadows("def f(v):\n    global X\n\n    X = v\n")


def test_no_owner_binds_a_name_the_tests_patch_on_the_facade() -> None:
    """The seam the technique rests on: a patch of ``handler.<name>`` reaches a moved
    call site only while that site reads the name out of the facade's globals."""
    patched = _patched_facade_names()
    found = {
        stem: sorted(name for name in _runtime_shadows(source) if name in patched)
        for stem, source in _owner_sources().items()
    }
    assert {stem: names for stem, names in found.items() if names} == {}


# ── the placement guards ──────────────────────────────────────────────────────


def _calls_in(source: str, function: str, attr: str) -> int:
    """``.attr(...)`` calls directly in *function*'s own body in *source*, nested defs
    excluded -- the shape ``test_transport_permission_floor`` counts per enclosing
    function."""
    tree = ast.parse(source)
    node = next(
        n
        for n in tree.body
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == function
    )
    count = 0
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Call) and getattr(child.func, "attr", None) == attr:
            count += 1
        stack.extend(ast.iter_child_nodes(child))
    return count


#: Constructs repository guards read by path, AST, text or ``inspect.getsource``, each
#: mapped to the files that hold it -- the facade (``handler``) or an owner stem --
#: which are the files those guards read. A holder that grew one, or lost one, would
#: move it out of such a guard's sight without failing it, so each set is exact (an
#: empty set means no file may hold it); a set changes in the commit that points the
#: token's guards at its new home (``slack-gateway.md``, "Native handler composition").
_GUARDED_TOKEN_HOMES: dict[str, frozenset[str]] = {
    # test_turn_ceiling: the gate, its refusal branch and the Working block it deletes.
    "turn_ceiling.gate(": frozenset({"turn"}),
    "except TurnCeilingExceeded": frozenset({"turn"}),
    "_working_ts = await": frozenset({"turn"}),
    "sessions.begin_turn(session_key)": frozenset({"turn"}),
    "begin_turn(": frozenset({"turn"}),
    # test_reinjection_gate: the trigger beside its consume and rearm.
    "sessions.check_context_usage(session_key, client)": frozenset({"turn"}),
    "check_context_usage(": frozenset({"turn"}),
    "compact_if_needed(": frozenset(),
    "consume_reinjection(": frozenset({"turn"}),
    "rearm_reinjection(": frozenset({"turn"}),
    # test_runtime_death_is_a_process_event: the except arms' attribution and streak.
    "runtime_death.caused_by_this_session(client)": frozenset({"turn"}),
    "runtime_death.note_shared_death(": frozenset({"turn"}),
    "caused_by_this_session": frozenset({"turn"}),
    "note_shared_death": frozenset({"turn"}),
    # test_memory_v2_isolation: the store resolved for the routed key, handed to the build.
    "_memory_store = await session_store_for_turn(context_builder, candidate_key)": frozenset(
        {"turn"}
    ),
    "memory_store=_memory_store": frozenset({"turn"}),
    "session_store_for_turn(": frozenset({"turn"}),
    # test_options_click_validation: the thread claim a pinned answer never takes.
    "if thread_owner_key is None and not route_pinned:": frozenset({"turn"}),
    # test_security_posture (the "Slack messages" sink row and the baseline log census).
    "StreamRedactor": frozenset({"delivery", "handler", "stream"}),
    "introduced by reply decorator": frozenset({"delivery"}),
    # the SAST baseline: the two credential log lines.
    'logger.warning("Credential redacted in response: %s", w)': frozenset({"delivery"}),
    'logger.warning("Credential redacted in thinking: %s", w)': frozenset({"delivery"}),
    # test_messaging_auto_title: pin, deferred verdict, persist and claim in one file.
    "auto_title.pin_record(conversation_log, session_key)": frozenset({"delivery"}),
    "auto_title.try_claim(session_key)": frozenset({"delivery"}),
    "try_claim(": frozenset({"delivery"}),
    "_options_verdict_deferred = bool(": frozenset({"delivery"}),
    "_turn_row_ts = await save_conversation_turn_off_loop(": frozenset({"delivery"}),
    # test_persist_off_loop: every persistence site.
    "save_conversation_turn_off_loop(": frozenset({"delivery", "dispatch"}),
    # test_options_click_validation / test_slack_options_lifecycle.
    "mint_options_token(": frozenset({"delivery"}),
    "_options_owner =": frozenset({"delivery"}),
    "maybe_handle_keyword_command": frozenset({"commands", "dispatch", "handler"}),
    "await expire_slack_options(": frozenset({"delivery", "dispatch", "turn"}),
    # test_update_check_install_aware_background_work: inbound admission, in handle_message.
    "admit_inbound_callback(": frozenset({"handler"}),
    # test_hooks / test_transport_permission_floor / test_messaging_deny_notice.
    "**hook_gate_kwargs(event)": frozenset({"permission_ladder"}),
    ".approve_tool(": frozenset({"permission_ladder"}),
    ".reject_tool(": frozenset({"permission_ladder"}),
    ".on_tool_call(": frozenset({"permission_ladder"}),
    "await _steer_host_deny(": frozenset({"permission_ladder"}),
    # test_name_grant_surfaces: the one off-loop entry point, the event-identity rung.
    "asyncio.to_thread(name_grant": frozenset(),
    'title == "spawn_run"': frozenset(),
    # test_agent_spec_hardened_reads: the labelled call sites.
    "project_agent_files(": frozenset({"agent_names"}),
    "read_agent_spec_strict(": frozenset({"agent_names"}),
    "_CC_AGENT_NAME_RE": frozenset({"handler"}),
    # the internal content scan: the companion-plugin agent home is named in the facade.
    '/ "cc-plugins"': frozenset({"handler"}),
    # test_safety_override / test_messaging_privacy_mode: shapes no file may hold.
    "remaining // 60": frozenset(),
    "resolve_pre_turn(": frozenset(),
    "mark_temporary(": frozenset(),
    "mark_incognito(": frozenset(),
}


def _holder_sources() -> dict[str, str]:
    """The facade's and every owner's source, keyed by stem."""
    return {"handler": _FACADE_PATH.read_text(encoding="utf-8"), **_owner_sources()}


@pytest.mark.parametrize("token", sorted(_GUARDED_TOKEN_HOMES))
def test_a_construct_a_guard_reads_stays_where_the_guard_reads_it(token: str) -> None:
    holders = {stem for stem, source in _holder_sources().items() if token in source}
    assert holders == _GUARDED_TOKEN_HOMES[token]


def test_a_counted_construct_keeps_its_count_in_its_home() -> None:
    """The guards that COUNT a construct count it in the file its home names."""
    ladder = _owner_sources()["permission_ladder"]
    assert _calls_in(ladder, "_answer_permission_request", "approve_tool") == 4
    assert _calls_in(ladder, "handle_interaction", "approve_tool") == 1  # permission floor
    assert ladder.count(".reject_tool(") == 4  # test_messaging_deny_notice
    assert ladder.count("await _steer_host_deny(") == 2  # test_messaging_deny_notice
    hook_gates = r"await run_in_tool_gate_pool\(\s*context_builder\.hooks\.on_tool_call,"
    assert len(re.findall(hook_gates, ladder)) == 2  # test_hooks
    persists = [s.count("save_conversation_turn_off_loop(") for s in _holder_sources().values()]
    assert sum(persists) >= 7  # test_persist_off_loop


def test_the_import_time_phase_table_is_built_in_the_facade() -> None:
    """The facade's body runs before ``compose``, so the function that builds its
    import-time phase table is defined there."""
    tree = ast.parse(_FACADE_PATH.read_text(encoding="utf-8"))
    assert "_build_phase_emojis" in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_a_frame_in_an_owner_is_attributed_to_slack() -> None:
    """A loop stall whose outermost Kiro Crew frame is an owner function still reads
    as the Slack surface (``stall_attribution``'s slack rule names the package)."""
    from kiro_crew import stall_attribution

    frames = stall_attribution.parse_frames(
        [
            '  File "/opt/venv/lib/python3.12/site-packages/kiro_crew/slack/handler_runtime/'
            'stream.py", line 1 in on_text'
        ]
    )
    assert stall_attribution.classify_surface(frames) == "slack"
