"""The ``kiro_crew.context`` facade and the ``context_assembly`` owners behind it.

The assembly lives in owner modules, so callers and tests keep reaching every name
through ``kiro_crew.context``. These tests pin that surface: every name the facade
defined or imported still resolves on it (an imported one as its home module's object, a
moved one as its owner's), the frozen public signatures and ``ContextBuilder`` members,
the owner set and that a fresh import of the facade loads every owner. They pin the
placement rules that keep a patch on the facade effective once the code has moved: no
Kiro Crew import at an owner's module scope, no owner importing a sibling's attribute, an
acyclic owner graph, the facade logger, and no owner binding or bare-reading a name the
tests rebind on the facade (that set is derived from the test tree). And for a sample of
those seams they show a patch on the facade reaching the moved code that reads it. The
byte matrix and the
directly asserted boundaries are in ``test_context_composition_contract.py``; the hermetic
builder comes from ``context_composition_contract_helpers.py``.
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
from context_composition_contract_helpers import (  # noqa: F401 -- the fixture is requested by name
    _Rig,
    rig_fixture,
)
from source_corpus import repo_files_named, repo_root

import kiro_crew
from kiro_crew import context as ctx
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import default_timeout

pytestmark = [
    pytest.mark.xdist_group("context_composition_contract"),
    pytest.mark.usefixtures("ample_host_resources", "close_skills_loaders"),
]


# ── Frozen surface ─────────────────────────────────────────────────────────────

#: Every name kiro_crew/context.py defined before its owners moved out. Each must
#: keep resolving on the facade, whichever module now holds it.
_FACADE_DEFINED = """
    CONTEXT_GROUP_LESSONS CONTEXT_GROUP_MEMORY CONTEXT_GROUP_PROJECT ContextBuilder
    RECALL_ROLES SWITCHABLE_CONTEXT_GROUPS UNTRUSTED_CALENDAR_FENCE_CLOSE
    UNTRUSTED_CALENDAR_FENCE_OPEN UNTRUSTED_TODO_FENCE_CLOSE UNTRUSTED_TODO_FENCE_OPEN
    _BUNDLED_DOCS_DIR _CALENDAR_FENCE_CLOSE_RE _CALENDAR_FENCE_OPEN_RE _CODE_BLOCK_RE
    _COMPRESSED_HISTORY_CAP _CONTEXT_BUDGET_BASE _CRITICAL_RULES _CRITICAL_RULES_CHANNEL
    _CRITICAL_RULES_HEAD _CRITICAL_RULES_TAIL _DEFAULT_KEY _DIFF_RULE_CHANNEL
    _DIFF_RULE_DASHBOARD _EPISODIC_INJECT_CAP _EPISODIC_MEMORY_CAP _GROUP_DESCRIPTIONS
    _HISTORY_BUDGET_CHARS _HISTORY_REFERENCE_BASE _INCLUDE_CREW_CONTEXT_CACHE
    _JSON_BLOB_RE _LESSONS_CAP _LESSONS_SHOWN_PER_SESSION _LESSONS_SHOWN_SESSIONS
    _LESSONS_STARTUP_CAP _LESSON_EXPERIENCE_CAP _MARKER_IGNORABLE_RANGES
    _MAX_CONTEXT_CHARS _MEMBER_BRIEFING_ITEM _MEMBER_BRIEFING_ITEM_UNAVAILABLE
    _MEMBER_HOW_YOU_WORK _MEMBER_HOW_YOU_WORK_COMMON _MEMBER_MARKER_RES
    _MEMORY_HISTORY_CAP _MEMORY_PREFS_CAP _MEMORY_PROJECTS_CAP _MIN_CONTEXT_BUDGET_BASE
    _MODE_IDENTITY_RE _MULTIBYTE_TABLE _PER_MESSAGE_CAP _PINNED_PROJECT_BODY_CAP
    _PREAMBLE_HEADROOM _PREFS_STARTUP_CAP _PROMPT_BUILD_EMBED_TIMEOUT_SECS
    _PROTECTED_CONTEXT_CHARS_PER_TOKEN _PROTECTED_CONTEXT_FLOOR
    _PROTECTED_CONTEXT_WINDOW_FRACTION _RECALL_FALLBACK_MAX_ROWS
    _REFERENCE_WINDOW_TOKENS _REPLAY_BUDGET_CHARS _REPLAY_CONVERSATION_MAX_ROWS
    _REPLAY_INJECT_BUDGET_DIVISOR _REPLAY_INJECT_CAP_CHARS _REPLAY_INJECT_MAX_ROWS
    _REPLY_FORMAT_RULES_MARKER _REPLY_FORMAT_RULES_RE _RESPONSE_PREFERENCES_FOOTER
    _RESPONSE_PREFERENCES_HEADER _ROLE_OTHER_MAX_LEN _ROLE_PUNCT_ALLOWED
    _RUNTIME_DISPLAY _ResolvedCaps _SEMANTIC_MEMORY_CAP _SKILLS_CAP _STEERING_CAP
    _STOP_EVENT_CAP _STOP_EVENT_RESOLVED_STATES _STORE_KEY_PREFIX
    _STRUCTURAL_MARKER_NEUTRALIZED _STRUCTURAL_MARKER_RES _ShownLessons
    _TECHNICAL_LEVEL_DESCRIPTIONS _THREAD_FENCE_CLOSE _THREAD_FENCE_CLOSE_RE
    _THREAD_FENCE_NEUTRALIZED _THREAD_FENCE_OPEN _THREAD_FENCE_OPEN_RE
    _TODO_FENCE_CLOSE_RE _TODO_FENCE_OPEN_RE _TURN_LESSONS_CHARS _TURN_LESSONS_MAX
    _TURN_LESSON_FRAME_RES _TURN_OPENER_ROLES _UI_LANGUAGE_CATALOGS _UI_LANGUAGE_TAG_RE
    _UNTRUSTED_FENCE_RES _USER_ROLE_DESCRIPTIONS _WS_KEY_PREFIX
    _agent_includes_crew_context _apply_marker_spans _board_safe_tag_name _budget
    _build_context_scope_section _build_docs_section _build_response_preferences_section
    _build_stop_event_notes _build_store_vectors _build_ui_language_section
    _build_user_profile_section _compress_assistant_message _config_scoped_groups
    _critical_rules_for _desk_withheld _effective_window _emit_context_section_timings
    _fence_marker_regex _fit_folder_steering_into_envelope _group_included _interrupted_opener_kind
    _is_allowed_role_char _is_marker_ignorable _lesson_stores _load_steering_resources
    _map_offset_through_spans _marker_spans _member_backend_can_dispatch
    _member_marker_spans _member_normalized_view _memory_stores _merge_overlapping_spans
    _merge_replay_rows _neutralize_fence_markers _neutralize_reply_format_markers
    _neutralize_structural_markers _project_steering_delivered
    _prompt_build_embedding_deadline _read_include_crew_context _read_prompt_file
    _recall_rows _render_folder_steering_section _replay_identity _replay_rows
    _reply_style_rules _resolve_caps _resolve_caps_cached _resolve_runtime_source
    _resolved_store_name _response_preferences_apply _role_description
    _runtime_display_name _sanitize_free_text_role _scrub_member_payload
    _scrub_turn_lesson _shared_embed_fn _skills_injection_plan _store_cache_generation
    _stores_lock _structural_marker_spans _target_key _template_selected_on_member_store
    _vector_stores build_cancelled_turn_preamble build_interrupted_turn_preamble
    build_session_replay cached_vector_store_entries inherit_session_memory
    invalidate_include_crew_context_cache logger neutralize_untrusted_text
    normalize_ui_language_tag prepare_store_vectors release_cached_memory_store
    reset_memory_caches resolve_model_window session_store_for_turn
    steering_target_admissible store_of_session ui_language_tag
    validated_cached_vector_stores window_for_provider_client
""".split()


#: The standard-library, typing and __future__ names it bound; kept bound so a
#: patch spelled through the facade (context.unicodedata.normalize) resolves.
_FACADE_STDLIB = """
    AbstractSet Any Awaitable Callable Iterator OrderedDict Path TYPE_CHECKING
    annotations asyncio contextmanager dataclass datetime defaultdict deque functools
    hashlib inspect json logging os re threading time unicodedata
""".split()


#: facade name -> (home module, attribute) for each Kiro Crew name it imported.
_FACADE_IMPORTS: dict[str, tuple[str, str]] = {
    "ESSENTIAL_MAX_CHARS": ("kiro_crew.member_essential_context", "ESSENTIAL_MAX_CHARS"),
    "FOLDER_STEERING_OMISSION_SOURCE": (
        "kiro_crew.folder_steering",
        "FOLDER_STEERING_OMISSION_SOURCE",
    ),
    "FileTooLargeError": ("kiro_crew.hooks", "FileTooLargeError"),
    "HOOK_INJECT_CONTEXT": ("kiro_crew.hooks", "HOOK_INJECT_CONTEXT"),
    "HOOK_MODIFY": ("kiro_crew.hooks", "HOOK_MODIFY"),
    "HookManager": ("kiro_crew.hooks", "HookManager"),
    "HookResult": ("kiro_crew.hooks", "HookResult"),
    "KiroCrewConfig": ("kiro_crew.config.loader", "KiroCrewConfig"),
    "LessonStore": ("kiro_crew.learn", "LessonStore"),
    "MemberEssentialContextError": (
        "kiro_crew.member_essential_context",
        "MemberEssentialContextError",
    ),
    "MemberLifecycle": ("kiro_crew.members", "MemberLifecycle"),
    "MemberSlugError": ("kiro_crew.members", "MemberSlugError"),
    "MemoryStore": ("kiro_crew.memory", "MemoryStore"),
    "PROJECT_SKILL_BODY_CAP": ("kiro_crew.skills", "PROJECT_SKILL_BODY_CAP"),
    "PROVIDER_ACP": ("kiro_crew.agent_sdk.provider_identity", "PROVIDER_ACP"),
    "SkillsLoader": ("kiro_crew.skills", "SkillsLoader"),
    "SteeringCollection": ("kiro_crew.folder_steering", "SteeringCollection"),
    "_MAX_DOCUMENTS": ("kiro_crew.member_essential_context", "_MAX_DOCUMENTS"),
    "_prompt_path": ("kiro_crew.agent", "_prompt_path"),
    "_shipped_prompt": ("kiro_crew.agent", "_shipped_prompt"),
    "acp_driver": ("kiro_crew.agent_sdk.drivers", "acp"),
    "agent_skill_globs": ("kiro_crew.agent_discovery", "agent_skill_globs"),
    "audit_injection_dropped": ("kiro_crew.security", "audit_injection_dropped"),
    "collect_folder_steering": ("kiro_crew.folder_steering", "collect_folder_steering"),
    "contains_injection": ("kiro_crew.security", "contains_injection"),
    "expand_quick_prompt": ("kiro_crew.quick_prompts", "expand_quick_prompt"),
    "get_local_tz": ("kiro_crew.cron", "get_local_tz"),
    "get_recorder": ("kiro_crew.metrics.provider", "get_recorder"),
    "has_dashboard_surface": ("kiro_crew.session_surface", "has_dashboard_surface"),
    "is_claude_code": ("kiro_crew.agent_sdk.provider_identity", "is_claude_code"),
    "is_grantable_tag_id": ("kiro_crew.board_tag_grammar", "is_grantable_tag_id"),
    "is_managed_prompt": ("kiro_crew.agent", "is_managed_prompt"),
    "is_sensitive_path": ("kiro_crew.security", "is_sensitive_path"),
    "iter_agent_spec_files": ("kiro_crew.agent_spec_format", "iter_agent_spec_files"),
    "kiro_agents_dir": ("kiro_crew.config.paths", "kiro_agents_dir"),
    "live": ("kiro_crew.config", "live"),
    "measure_prompt": ("kiro_crew.context_blocks", "measure_prompt"),
    "member_briefing_path": ("kiro_crew.members", "member_briefing_path"),
    "member_briefing_supported": ("kiro_crew.members", "member_briefing_supported"),
    "member_context_identity": ("kiro_crew.member_essential_context", "member_context_identity"),
    "member_inherits_default_resources": (
        "kiro_crew.member_essential_context",
        "member_inherits_default_resources",
    ),
    "member_lifecycle": ("kiro_crew.members", "member_lifecycle"),
    "member_turn_context": ("kiro_crew.members", "member_turn_context"),
    "model_registry": ("kiro_crew", "model_registry"),
    "parse_agent_spec_text": ("kiro_crew.agent_spec_format", "parse_agent_spec_text"),
    "read_member_briefing": ("kiro_crew.members", "read_member_briefing"),
    "read_member_rules": ("kiro_crew.members", "read_member_rules"),
    "redact_credentials": ("kiro_crew.security", "redact_credentials"),
    "redact_exfiltration_urls": ("kiro_crew.security", "redact_exfiltration_urls"),
    "render_essentials": ("kiro_crew.member_essential_context", "render_essentials"),
    "render_folder_steering": ("kiro_crew.folder_steering", "render_folder_steering"),
    "render_omission_notice": ("kiro_crew.folder_steering", "render_omission_notice"),
    "resource_status": ("kiro_crew", "resource_status"),
    "safe_read_file": ("kiro_crew.hooks", "safe_read_file"),
    "safe_read_file_bytes_nolink": ("kiro_crew.hooks", "safe_read_file_bytes_nolink"),
    "slug_for_name": ("kiro_crew.members", "slug_for_name"),
    "sqlite3": ("kiro_crew._sqlite_compat", "sqlite3"),
    "workspace_dir_for": ("kiro_crew.config.loader", "workspace_dir_for"),
}


#: ContextBuilder members: kind and signature, frozen before the split.
_BUILDER_MEMBERS: dict[str, tuple[str, str]] = {
    "_CAP_FIGURE_SESSIONS": ("attr", "512"),
    "_COMPUTER_USE_TOKEN": ("attr", "'{{COMPUTER_USE_BLOCK}}'"),
    "_MAX_SUBAGENTS_TOKEN": ("attr", "'{{MAX_SUBAGENTS}}'"),
    "_SENT_SKILL_BODY_ENTRIES": ("attr", "64"),
    "_SENT_SKILL_BODY_SESSIONS": ("attr", "512"),
    "_SKILL_DELIVERY_AUDIT_NAMES": ("attr", "64"),
    "_SKILL_DELIVERY_AUDIT_NAME_LEN": ("attr", "128"),
    "__init__": (
        "method",
        "(self, memory: 'MemoryStore | None' = None, skills: 'SkillsLoader | None' = None, hooks: 'HookManager | None' = None, lessons: 'LessonStore | None' = None, conversation_log: \"'ConversationLog | None'\" = None, channel_history: \"'ChannelHistory | None'\" = None, bot_name: 'str' = '')",
    ),
    "_build_member_section": (
        "method",
        "(self, member: 'str', *, strict: 'bool' = False, include_briefing: 'bool' = True, desk_withheld: 'bool' = False) -> 'str'",
    ),
    "_build_v2_essentials": (
        "method",
        "(self, memory_store: 'str | None', *, member: 'str' = '', member_is_id: 'bool' = True, project: 'str | None' = None, workspace: 'str | None' = None, blocks_reads: 'bool' = False, context_groups: 'frozenset[str] | None' = None, profile_overrides: 'dict[str, str] | None' = None, native_documents: 'dict[str, str] | None' = None, native_envelope_out: 'list[str] | None' = None, execution_template: 'str' = '', member_template: 'str' = '', conditional_index: 'bool' = False, trigger_text: 'str' = '', steering_dirs: 'tuple[str, ...]' = (), desk_withheld: 'bool' = False, provider_type: 'str | None' = None) -> 'str'",
    ),
    "_cap_memo_key": ("static", "(session_key: 'str') -> 'str'"),
    "_dedup_triggered_bodies": (
        "method",
        "(self, session_key: 'str | None', agent: 'str | None', reset: 'bool', candidates: 'list[tuple[str, str]]') -> 'set[str]'",
    ),
    "_forget_shown_lessons": ("method", "(self, session_key: 'str') -> 'None'"),
    "_live_cap_figure": ("static", "() -> 'str'"),
    "_live_computer_use_gate": ("static", "() -> 'bool'"),
    "_live_shown_lessons": ("method", "(self, session_key: 'str') -> '_ShownLessons'"),
    "_load_agent_prompt": (
        "static",
        "(agent: 'str', project: 'str | None' = None, *, owner_template: 'str' = '') -> 'str'",
    ),
    "_remember_startup_lessons": ("method", "(self, session_key: 'str', block: 'str') -> 'None'"),
    "_resolve_agent_prompt": (
        "method",
        "(self, agent: 'str | None', *, project: 'str | None', mode: 'str', session_key: 'str | None', is_cc: 'bool', private_owner: 'bool', session_start: 'bool') -> 'str'",
    ),
    "_resolve_prompt_templates": (
        "static",
        "(prompt: 'str', session_key: 'str', cap_figure: 'str' = '') -> 'str'",
    ),
    "_session_cap_figure": ("method", "(self, session_key: 'str', *, refresh: 'bool') -> 'str'"),
    "_session_computer_use_gate": (
        "method",
        "(self, session_key: 'str', *, refresh: 'bool') -> 'bool'",
    ),
    "_session_reading": (
        "method",
        "(self, memo: 'dict[str, _ReadingT]', lock: 'threading.Lock', session_key: 'str', live: 'Callable[[], _ReadingT]', *, refresh: 'bool') -> '_ReadingT'",
    ),
    "_substitute_bot_name": ("method", "(self, prompt: 'str') -> 'str'"),
    "_turn_lessons_block": (
        "method",
        "(self, text: 'str', session_key: 'str', *, workspace: 'str | None', memory_store: 'str | None', project: 'str | None', member: 'str', execution_context: 'Any', context_groups: 'frozenset[str] | None') -> 'str'",
    ),
    "build_message": (
        "method",
        "(self, text: 'str', is_new_session: 'bool', session_key: 'str | None' = None, channel_id: 'str | None' = None, interactive: 'bool' = True, agent: 'str | None' = None, resumed: 'bool' = False, thread_ts: 'str | None' = None, workspace: 'str | None' = None, project: 'str | None' = None, memory_store: 'str | None' = None, user_display_name: 'str | None' = None, compressed_history: 'str | None' = None, mode: 'str' = '', blocks_reads: 'bool' = False, action_context: 'str | None' = None, thread_parent_text: 'str | None' = None, thread_meta: 'str | None' = None, provider_type: 'str | None' = None, minimal_context: 'bool' = False, *, runtime_source: 'str | None' = None, request_prefix_context: 'str | None' = None, exclude_last_n: 'int' = 0, thread_replies_text: 'str | None' = None, folder_path: 'str | None' = None, model_window: 'int | None' = None, board_tags: 'list[tuple[str, str]] | None' = None, user_text_range: 'tuple[int, int] | None' = None, user_span_out: 'list[int] | None' = None, needs_reinjection: 'bool' = False, context_groups: 'frozenset[str] | None' = None, member: 'str' = '', execution_context: 'Any' = None, context_provider: \"'ContextPromptProvider | None'\" = None, steering_dirs: 'tuple[str, ...]' = ()) -> 'tuple[str, HookResult]'",
    ),
    "build_session_context": (
        "method",
        "(self, session_key: 'str | None' = None, agent: 'str | None' = None, resumed: 'bool' = False, workspace: 'str | None' = None, memory_store: 'str | None' = None, compressed_history: 'str | None' = None, mode: 'str' = '', blocks_reads: 'bool' = False, provider_type: 'str | None' = None, minimal_context: 'bool' = False, *, runtime_source: 'str | None' = None, exclude_last_n: 'int' = 0, model_window: 'int | None' = None, context_groups: 'frozenset[str] | None' = None, query_text: 'str' = '', project: 'str | None' = None, member: 'str' = '', execution_context: 'Any' = None, steering_dirs: 'tuple[str, ...]' = (), _v2_essentials: 'str | None' = None) -> 'str'",
    ),
    "commit_skill_bodies": ("method", "(self, session_key: 'str | None') -> 'None'"),
    "ensure_store": ("static", "(memory_store: 'str | None') -> \"'VectorMemoryStore | None'\""),
    "get_lessons_for": (
        "static",
        "(workspace: 'str | None' = None, memory_store: 'str | None' = None) -> 'LessonStore'",
    ),
    "get_memory_for": (
        "static",
        "(workspace: 'str | None' = None, memory_store: 'str | None' = None) -> 'MemoryStore'",
    ),
    "rollback_skill_bodies": ("method", "(self, session_key: 'str | None') -> 'None'"),
}


#: Public functions defined on the facade, and their signatures.
_PUBLIC_SIGNATURES = {
    "build_cancelled_turn_preamble": "(conversation_log: \"'ConversationLog'\", session_key: 'str', *, user_cap: 'int' = 2000, assist_cap: 'int' = 2000) -> 'str'",
    "build_interrupted_turn_preamble": "(messages: 'list[dict]', current: 'dict | None' = None, *, opener_inject_kinds: 'AbstractSet[str]' = frozenset(), user_cap: 'int' = 8000, assist_cap: 'int' = 4000) -> 'str'",
    "build_session_replay": "(conversation_log: \"'ConversationLog | None'\", session_key: 'str', *, exclude_last_n: 'int' = 0, model_window: 'int | None' = None, pending_messages: 'list[dict] | None' = None, current_message: 'dict | None' = None) -> 'str | None'",
    "cached_vector_store_entries": "() -> \"tuple[tuple[str, 'VectorMemoryStore'], ...]\"",
    "inherit_session_memory": "(ctx_builder: 'object', parent_session_key: 'str', session_key: 'str') -> 'str'",
    "invalidate_include_crew_context_cache": "() -> 'None'",
    "neutralize_untrusted_text": "(text: 'str') -> 'str'",
    "normalize_ui_language_tag": "(value: 'object', *, source: 'str' = 'language') -> 'str'",
    "prepare_store_vectors": "(ctx_builder: 'object', memory_store: 'str | None', *, session_key: 'str' = '') -> 'None'",
    "release_cached_memory_store": "(name: 'str') -> 'None'",
    "reset_memory_caches": "(memory: 'MemoryStore') -> 'None'",
    "resolve_model_window": "(model: 'str | None') -> 'int | None'",
    "session_store_for_turn": "(ctx_builder: 'object', session_key: 'str') -> 'str'",
    "steering_target_admissible": "(resolved: 'Path', base: 'Path | None' = None) -> 'bool'",
    "store_of_session": "(conversation_log: 'object', session_key: 'str') -> 'str'",
    "ui_language_tag": "(cfg: \"'KiroCrewConfig'\") -> 'str'",
    "validated_cached_vector_stores": "() -> \"tuple['VectorMemoryStore', ...]\"",
    "window_for_provider_client": "(client: 'object') -> 'int | None'",
}


# ── The facade and its owners ─────────────────────────────────────────────────

OWNER_PACKAGE = "kiro_crew.context_assembly"
OWNER_DIR = Path(ctx.__file__).resolve().with_name("context_assembly")
FACADE_PATH = Path(ctx.__file__).resolve()
FACADE_LOGGER = "kiro_crew.context"

#: The owners the facade composes. A module added or removed changes the
#: composition, so the set is spelled out rather than globbed.
OWNER_MODULES = frozenset(
    """agent_spec budget delivery_records inclusion markers member member_gates
    output_contract replay sections session_start store_admission tool_names turn
    turn_message""".split()
)


def _frozen_view(live: inspect.Signature, frozen: str) -> str:
    """Render ``live`` as the frozen string would see it, minus compatible additions.

    The frozen strings pin what callers depend on. A parameter a later change adds
    as keyword-only WITH a default breaks no caller, so it is dropped from the live
    side before the comparison; anything else -- a removed, renamed, reordered or
    retyped parameter, a new positional one, a new required one, a changed return
    annotation -- still renders differently and reds. Without this, a branch that
    grows ``build_message`` by one keyword and a branch that records the list one
    keyword earlier are each green alone and red together on main.
    """
    kept = [
        p
        for p in live.parameters.values()
        if p.kind is not inspect.Parameter.KEYWORD_ONLY
        or p.default is inspect.Parameter.empty
        or re.search(rf"(?:\(|, ){re.escape(p.name)}(?=[:=,)])", frozen)
    ]
    return str(live.replace(parameters=kept))


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(OWNER_DIR.glob("*.py"))
        if path.stem != "__init__"
    }


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{OWNER_PACKAGE}.{stem}")


def _typing_only(tree: ast.Module) -> set[int]:
    return {
        id(sub)
        for block in tree.body
        if isinstance(block, ast.If) and ast.unparse(block.test) == "TYPE_CHECKING"
        for sub in ast.walk(block)
    }


def _imported_module(node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    return OWNER_PACKAGE + (f".{node.module}" if node.module else "")


class TestSurface:
    def test_the_package_holds_exactly_the_composed_modules(self) -> None:
        assert set(_owner_sources()) == OWNER_MODULES

    def test_every_name_the_facade_defined_still_resolves(self) -> None:
        assert [name for name in _FACADE_DEFINED if not hasattr(ctx, name)] == []

    def test_every_name_the_facade_imported_still_resolves(self) -> None:
        names = [*_FACADE_STDLIB, *_FACADE_IMPORTS]
        assert [name for name in names if not hasattr(ctx, name)] == []

    def test_an_imported_name_is_its_home_modules_object(self) -> None:
        wrong = [
            name
            for name, (home, attr) in _FACADE_IMPORTS.items()
            if getattr(ctx, name) is not getattr(importlib.import_module(home), attr)
        ]
        assert wrong == []

    def test_a_moved_name_is_its_owner_object_not_a_copy(self) -> None:
        moved = []
        for stem in sorted(OWNER_MODULES):
            for name, value in vars(_owner(stem)).items():
                if name in _FACADE_DEFINED and not isinstance(value, types.ModuleType):
                    assert getattr(ctx, name) is value, (stem, name)
                    moved.append(name)
        # Non-vacuous: most of what the facade defined now lives in an owner.
        assert len(moved) > 100

    def test_a_name_lives_in_exactly_one_owner(self) -> None:
        homes: dict[str, list[str]] = {}
        for stem in sorted(OWNER_MODULES):
            for name, value in vars(_owner(stem)).items():
                if name == "logger" or isinstance(value, types.ModuleType):
                    continue
                if name in _FACADE_DEFINED:
                    homes.setdefault(name, []).append(stem)
        assert {name: stems for name, stems in homes.items() if len(stems) > 1} == {}

    def test_builder_members_keep_their_kind_and_signature(self) -> None:
        current = {}
        for name, raw in vars(ctx.ContextBuilder).items():
            if name.startswith("__") and name != "__init__":
                continue
            frozen = _BUILDER_MEMBERS.get(name, ("", ""))[1]
            if isinstance(raw, staticmethod):
                current[name] = ("static", _frozen_view(inspect.signature(raw.__func__), frozen))
            elif callable(raw):
                current[name] = ("method", _frozen_view(inspect.signature(raw), frozen))
            else:
                current[name] = ("attr", repr(raw))
        assert current == _BUILDER_MEMBERS

    def test_public_functions_keep_their_signatures(self) -> None:
        current = {
            name: _frozen_view(inspect.signature(getattr(ctx, name)), frozen)
            for name, frozen in _PUBLIC_SIGNATURES.items()
        }
        assert current == _PUBLIC_SIGNATURES

    def test_only_a_compatible_addition_is_tolerated(self) -> None:
        """The tolerance above must not swallow a change a caller would feel."""

        def base(self, a: str, *, b: int = 0) -> str: ...

        frozen = str(inspect.signature(base))

        def added_optional_keyword(self, a: str, *, b: int = 0, c: int = 1) -> str: ...

        def added_required_keyword(self, a: str, *, b: int = 0, c: int) -> str: ...

        def added_positional(self, a: str, c: int = 1, *, b: int = 0) -> str: ...

        def renamed(self, a: str, *, bb: int = 0) -> str: ...

        def retyped_return(self, a: str, *, b: int = 0) -> int: ...

        assert _frozen_view(inspect.signature(added_optional_keyword), frozen) == frozen
        for broken in (added_required_keyword, added_positional, renamed, retyped_return):
            assert _frozen_view(inspect.signature(broken), frozen) != frozen, broken.__name__

    def test_moved_classes_keep_their_owner_module(self) -> None:
        assert ctx._ResolvedCaps.__module__ == f"{OWNER_PACKAGE}.budget"
        assert ctx._ShownLessons.__module__ == f"{OWNER_PACKAGE}.store_admission"
        assert ctx.ContextBuilder.__module__ == "kiro_crew.context"

    def test_the_facade_is_a_plain_module(self) -> None:
        """Every name is an ordinary binding: no ``__getattr__`` forwarding, no class
        swap, so a patch on the facade and its undo are plain attribute writes, and
        mypy reads each name from the explicit imports."""
        assert type(ctx) is types.ModuleType
        assert "__getattr__" not in vars(ctx)
        assert "__all__" not in vars(ctx)

    def test_a_star_import_still_binds_every_public_name(self, tmp_path: Path) -> None:
        path = tmp_path / "context_star_probe.py"
        path.write_text("from kiro_crew.context import *\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("context_star_probe", path)
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        public = [name for name in [*_FACADE_DEFINED, *_FACADE_IMPORTS] if not name.startswith("_")]
        assert [name for name in public if not hasattr(probe, name)] == []

    def test_the_package_ships_with_the_wheel(self) -> None:
        import configparser

        config = configparser.ConfigParser()
        config.read(FACADE_PATH.parents[2] / "setup.cfg", encoding="utf-8")
        assert config.get("options", "packages").strip() == "find:"
        assert config.get("options.packages.find", "where").strip() == "src"
        assert (OWNER_DIR / "__init__.py").is_file()


class TestEagerOwners:
    def test_the_facade_imports_every_owner_when_it_loads(self) -> None:
        tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == OWNER_PACKAGE
            for alias in node.names
        }
        assert imported == OWNER_MODULES

    def test_every_owner_is_loaded_by_a_fresh_import_of_the_facade(self, tmp_path: Path) -> None:
        bound = default_timeout()
        # A hang writes nothing on its own, so the child dumps its stack shortly
        # before the bound and the assertion below can name the import it stopped in.
        dump_after = bound - min(5.0, bound / 4)
        probe = (
            f"import faulthandler; faulthandler.dump_traceback_later({dump_after!r}, exit=False)\n"
            "import json, sys\n"
            "import kiro_crew.context\n"
            f"print(json.dumps(sorted(m for m in sys.modules if m.startswith({OWNER_PACKAGE + '.'!r}))))\n"
        )
        env = dict(os.environ)
        src = str(Path(kiro_crew.__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        try:
            out = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                check=True,
                env=env,
                cwd=tmp_path,
                timeout=bound,
                **UTF8_TEXT,
            )
        except subprocess.TimeoutExpired as exc:
            # On POSIX the re-raised TimeoutExpired carries undecoded bytes even with text=True.
            stderr = exc.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            raise AssertionError(
                f"a fresh import of kiro_crew.context did not finish within {exc.timeout:g} s; "
                f"stderr so far:\n{stderr}"
            ) from None
        loaded = json.loads(out.stdout.splitlines()[-1])
        assert loaded == sorted(f"{OWNER_PACKAGE}.{stem}" for stem in OWNER_MODULES), out.stderr

    def test_the_package_init_imports_nothing(self) -> None:
        tree = ast.parse((OWNER_DIR / "__init__.py").read_text(encoding="utf-8"))
        assert [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))] == []


#: Every spelling a test uses to reach the facade module.
_MENTIONS_THE_FACADE = re.compile(r"kiro_crew\.context\b|from kiro_crew import [^\n]*\bcontext\b")
_PATCH_CALLS = ("setattr", "patch.object", "delattr")


def _facade_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to ``kiro_crew.context`` in *tree*, resolved to a fixed point."""
    aliases = {"kiro_crew.context"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
            aliases |= {a.asname or a.name for a in node.names if a.name == "context"}
        elif isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == "kiro_crew.context" and a.asname}
    loaders = {
        'importlib.import_module("kiro_crew.context")',
        "importlib.import_module('kiro_crew.context')",
        'sys.modules["kiro_crew.context"]',
        "sys.modules['kiro_crew.context']",
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

    Dotted targets (``context.KiroCrewConfig.load``) patch an attribute of a shared
    object rather than a facade binding, so every holder of that object sees them;
    only first-level facade names count.
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
    found |= set(re.findall(r"""["']kiro_crew\.context\.(\w+)["']""", text))
    return found


def _facade_patched_names() -> set[str]:
    """Names any test rebinds on ``kiro_crew.context``."""
    root = repo_root()
    tests = root / "test"
    found: set[str] = set()
    for path in repo_files_named(".py"):
        in_tests = path.is_relative_to(tests) or "/tests/" in path.as_posix()
        if not in_tests or path.resolve() == Path(__file__).resolve():
            continue
        found |= _patched_names_in(path.read_text(encoding="utf-8", errors="replace"))
    return found - {"ContextBuilder"}


def _sibling_name_imports(source: str) -> list[str]:
    """Every import, at any depth, binding an owner's attribute rather than the owner."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = _imported_module(node)
        if module.startswith(OWNER_PACKAGE + "."):
            hits.extend(f"{node.lineno}: {module}.{alias.name}" for alias in node.names)
        elif module == OWNER_PACKAGE:
            hits.extend(
                f"{node.lineno}: {module}.{alias.name}"
                for alias in node.names
                if alias.name not in OWNER_MODULES
            )
    return hits


def _sibling_modules(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and _imported_module(node) == OWNER_PACKAGE
        for alias in node.names
        if alias.name in OWNER_MODULES
    }


def _bare_seam_loads(source: str, seams: set[str]) -> list[str]:
    """Reads of *seams* inside owner functions that a patch on the facade would miss.

    A seam is read as ``ctx.<name>`` after a function-local
    ``from kiro_crew import context as ctx``. A bare global load or a function-local
    import binding the name reads some other binding.
    """
    tree = ast.parse(source)
    annotations: set[int] = set()
    for node in ast.walk(tree):
        for field in ("annotation", "returns"):
            sub = getattr(node, field, None)
            if sub is not None:
                annotations.update(id(part) for part in ast.walk(sub))
    function_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    hits = []
    for function in ast.walk(tree):
        if not isinstance(function, function_types):
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
                    if {bound, imported} & seams:
                        hits.append(f"{node.lineno}: import binds {imported}")
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in seams
                and node.id not in local
                and id(node) not in annotations
            ):
                hits.append(f"{node.lineno}: {node.id}")
    return sorted(set(hits), key=lambda hit: (int(hit.split(":")[0]), hit))


class TestPlacement:
    """The rules that keep a patch on the facade effective once the code has moved."""

    def test_owners_import_no_kiro_crew_module_at_module_scope(self) -> None:
        """Every Kiro Crew name an owner needs is read through the facade at call
        time, so a patch there reaches it and the facade's import closure is
        unchanged. Sibling owners and ``TYPE_CHECKING`` imports are the exceptions."""
        for stem, source in _owner_sources().items():
            tree = ast.parse(source)
            typing_only = _typing_only(tree)
            for node in tree.body:
                if id(node) in typing_only:
                    continue
                if isinstance(node, ast.ImportFrom) and _imported_module(node).startswith(
                    "kiro_crew"
                ):
                    assert _imported_module(node) == OWNER_PACKAGE, (stem, node.module)
                elif isinstance(node, ast.Import):
                    assert not [a.name for a in node.names if a.name.startswith("kiro_crew")], stem

    def test_owners_reach_the_facade_as_a_module_at_call_time(self) -> None:
        imports = 0
        for stem, source in _owner_sources().items():
            tree = ast.parse(source)
            typing_only = _typing_only(tree)
            for node in ast.walk(tree):
                if id(node) in typing_only or not isinstance(node, ast.ImportFrom):
                    continue
                assert node.module != "kiro_crew.context", f"{stem}:{node.lineno}"
                if node.module == "kiro_crew" and "context" in [a.name for a in node.names]:
                    assert node.col_offset > 0, f"{stem}:{node.lineno} imports the facade early"
                    imports += 1
        # Non-vacuous: the owners do reach the facade's seams.
        assert imports >= 15

    def test_no_owner_imports_a_siblings_attribute(self) -> None:
        offenders = {
            stem: hits
            for stem, source in _owner_sources().items()
            if (hits := _sibling_name_imports(source))
        }
        assert offenders == {}

    @pytest.mark.parametrize(
        "planted",
        [
            "from kiro_crew.context_assembly.markers import _apply_marker_spans\n",
            "from kiro_crew.context_assembly.budget import _ResolvedCaps as Caps\n",
            "def f():\n    from kiro_crew.context_assembly.replay import _quota_tail\n",
            "from kiro_crew.context_assembly import _quota_tail\n",
            "from .sections import widget_block\n",
        ],
    )
    def test_the_sibling_import_scan_catches_a_planted_import(self, planted: str) -> None:
        assert _sibling_name_imports(planted)

    @pytest.mark.parametrize(
        "allowed",
        [
            "from kiro_crew.context_assembly import markers as _markers\n",
            "from . import budget\n",
        ],
    )
    def test_the_sibling_import_scan_allows_a_module_import(self, allowed: str) -> None:
        assert _sibling_name_imports(allowed) == []

    def test_owner_imports_form_a_dag(self) -> None:
        graph = {
            stem: _sibling_modules(ast.parse(source)) for stem, source in _owner_sources().items()
        }
        assert any(graph.values())
        done: set[str] = set()

        def visit(stem: str, path: tuple[str, ...]) -> None:
            assert stem not in path, f"import cycle: {' -> '.join(path + (stem,))}"
            if stem in done:
                return
            for dep in graph[stem]:
                visit(dep, path + (stem,))
            done.add(stem)

        for stem in graph:
            visit(stem, ())

    def test_no_owner_binds_a_name_tests_rebind_on_the_facade(self) -> None:
        """The contract the placement rules stand for, derived from the tests: an
        owner that defined or imported a rebound name would keep using its own
        binding, and the patch would silently stop applying there."""
        patched = _facade_patched_names()
        # Non-vacuous: the scan sees seams the context tests rebind.
        assert {
            "_neutralize_structural_markers",
            "_member_marker_spans",
            "_resolve_caps",
            "agent_skill_globs",
            "datetime",
        } <= patched
        for stem in sorted(OWNER_MODULES):
            bound = {
                n for n, v in vars(_owner(stem)).items() if not isinstance(v, types.ModuleType)
            }
            assert bound & patched == set(), stem

    def test_no_owner_reads_a_rebound_name_as_a_bare_global(self) -> None:
        patched = _facade_patched_names()
        offenders = {
            stem: hits
            for stem, source in _owner_sources().items()
            if (hits := _bare_seam_loads(source, patched))
        }
        assert offenders == {}

    def test_the_patch_scan_reads_every_spelling(self) -> None:
        planted = (
            "import importlib\n"
            "import kiro_crew.context as cm\n"
            "from kiro_crew import context as ctx_mod\n"
            "facade = importlib.import_module('kiro_crew.context')\n"
            "alias = facade\n"
            "def test(monkeypatch):\n"
            "    monkeypatch.setattr(cm, 'first', 1)\n"
            "    monkeypatch.setattr(ctx_mod, 'second', 2)\n"
            "    patch.object(alias, 'third')\n"
            "    ctx_mod.fourth = 4\n"
            "    monkeypatch.setattr('kiro_crew.context.fifth', 5)\n"
            "    monkeypatch.setattr('kiro_crew.context.Shared.attr', 6)\n"
            "    monkeypatch.setattr(ctx_mod.Shared, 'attr', 7)\n"
            "    monkeypatch.setattr(other, 'not_facade', 8)\n"
        )
        assert _patched_names_in(planted) == {"first", "second", "third", "fourth", "fifth"}

    def test_the_seam_scan_catches_a_planted_bare_read(self) -> None:
        planted = (
            "def f(path: 'datetime') -> '_resolve_caps':\n"
            "    from kiro_crew import context as ctx\n"
            "    good = ctx._resolve_caps(None)\n"
            "    return _resolve_caps(None), datetime\n"
            "def g():\n"
            "    from kiro_crew.cron import get_local_tz\n"
            "    from kiro_crew.agent import _prompt_path as path\n"
            "    return get_local_tz, path\n"
        )
        seams = {"_resolve_caps", "datetime", "get_local_tz", "_prompt_path"}
        assert [hit.split(": ")[1] for hit in _bare_seam_loads(planted, seams)] == [
            "_resolve_caps",
            "datetime",
            "import binds get_local_tz",
            "import binds _prompt_path",
        ]

    def test_every_owner_logs_under_the_facade_logger(self) -> None:
        for stem, source in _owner_sources().items():
            for call in ast.walk(ast.parse(source)):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "getLogger"
                ):
                    assert [ast.literal_eval(arg) for arg in call.args] == [FACADE_LOGGER], stem
            if "logger" in vars(_owner(stem)):
                assert _owner(stem).logger is ctx.logger is logging.getLogger(FACADE_LOGGER)

    def test_no_owner_anchors_a_path_on_its_own_file(self) -> None:
        for stem, source in _owner_sources().items():
            names = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)]
            assert not [n for n in names if n.id == "__file__"], stem

    def test_no_owner_defines_an_async_function(self) -> None:
        """Prompt assembly runs on a worker thread; the async store preparation
        that offloads its blocking reads stays on the facade."""
        for stem, source in _owner_sources().items():
            nodes = ast.walk(ast.parse(source))
            assert not [n for n in nodes if isinstance(n, ast.AsyncFunctionDef)], stem


class TestPatchReach:
    """A patch on the facade reaches the moved code that consumes it."""

    def test_structural_scrub(self, monkeypatch) -> None:
        calls: list[str] = []

        def _spy(text: str) -> str:
            calls.append(text)
            return "<scrubbed>"

        monkeypatch.setattr(ctx, "_neutralize_structural_markers", _spy)
        assert ctx.neutralize_untrusted_text("x") == "<scrubbed>"
        assert ctx._scrub_turn_lesson("lesson").startswith("<scrubbed>")
        restored = ctx.build_interrupted_turn_preamble([{"role": "user", "content": "q"}])
        assert "Interrupted request:\n<scrubbed>" in restored
        assert len(calls) >= 4

    def test_member_span_finder(self, monkeypatch) -> None:
        monkeypatch.setattr(ctx, "_member_marker_spans", lambda text: [])
        out = ctx._scrub_member_payload("keep Ａ.txt ［ＰＥＲＭＡＮＥＮＴ ＲＵＬＥＳ］ y")
        assert out == "keep A.txt [marker-removed] y"

    def test_embedding_deadline(self, monkeypatch) -> None:
        from kiro_crew.embeddings import embedding_work

        monkeypatch.setattr(ctx, "_PROMPT_BUILD_EMBED_TIMEOUT_SECS", 1000.0)
        with ctx._prompt_build_embedding_deadline(True):
            work = embedding_work.get()
            assert work is not None
            import time as _time

            # flake-ok: pre-existing deadline margin moved verbatim; a 1000 s deadline is read back at least 900 s ahead
            assert work.deadline - _time.monotonic() > 900

    def test_model_registry(self, monkeypatch) -> None:
        from types import SimpleNamespace

        monkeypatch.setattr(ctx, "model_registry", SimpleNamespace(model_window=lambda m: 42))
        assert ctx.resolve_model_window("some-model") == 42

    def test_folder_steering_reader_and_cap(self, monkeypatch) -> None:
        seen: dict[str, object] = {}

        def _collect(dirs, *, project, skip_delivered_roots, delivered_sources):
            seen["collect"] = (dirs, project, skip_delivered_roots, delivered_sources)
            return "COLLECTION"

        def _render(collection, *, max_chars, scrub):
            seen["render"] = (collection, max_chars, scrub)
            return "RENDERED"

        monkeypatch.setattr(ctx, "collect_folder_steering", _collect)
        monkeypatch.setattr(ctx, "render_folder_steering", _render)
        out = ctx._render_folder_steering_section(
            ("d",), "/p", 77, skip_delivered_roots=False, delivered_sources=("/s.md",)
        )
        assert out == "RENDERED"
        assert seen["collect"] == (("d",), "/p", False, ("/s.md",))
        assert seen["render"] == ("COLLECTION", 77, ctx._neutralize_structural_markers)

    def test_thread_and_folder_screens(self, rig: _Rig, monkeypatch) -> None:
        audits: list[str] = []
        monkeypatch.setattr(ctx, "contains_injection", lambda text: True)
        monkeypatch.setattr(
            ctx, "audit_injection_dropped", lambda **kw: audits.append(kw["surface"])
        )
        text, _ = rig.builder.build_message(
            "hi",
            False,
            session_key="slack:C1:1.1",
            channel_id="C1",
            thread_ts="1.1",
            thread_parent_text="anything",
            folder_path="Work",
        )
        assert audits == ["slack_thread_parent", "chat_folder_path"]
        assert "WITHHELD" in text and "[FOLDER]" not in text

    def test_member_briefing_gate(self, monkeypatch) -> None:
        monkeypatch.setattr(ctx, "member_briefing_supported", lambda: False)
        from types import SimpleNamespace

        crew = SimpleNamespace(description="Writes.", triggers="drafts", member_id="")
        cfg = SimpleNamespace(agents={"writer": crew})
        monkeypatch.setattr(ctx.KiroCrewConfig, "load", lambda *a, **k: cfg)
        monkeypatch.setattr(ctx, "read_member_rules", lambda slug, member: "")
        section = ctx.ContextBuilder._build_member_section(object(), "writer")  # type: ignore[arg-type]
        assert "[CURRENT ASSIGNMENT — not available on this platform]" in section

    def test_dashboard_surface_and_crew_context_gates(self, monkeypatch) -> None:
        monkeypatch.setattr(ctx, "has_dashboard_surface", lambda key: False)
        assert len(_guidance("dashboard:x")) == 1
        monkeypatch.setattr(ctx, "has_dashboard_surface", lambda key: True)
        assert len(_guidance("dashboard:x")) == 3
        monkeypatch.setattr(ctx, "_agent_includes_crew_context", lambda agent: False)
        assert len(_guidance("dashboard:x")) == 1

    def test_post_compaction_caps(self, rig: _Rig, monkeypatch) -> None:
        rig.seed_skill("alpha")
        windows: list[object] = []
        real = ctx._resolve_caps

        def _spy(window):
            windows.append(window)
            return real(window)

        monkeypatch.setattr(ctx, "_resolve_caps", _spy)
        rig.builder.build_message(
            "next", False, session_key="dashboard:c", needs_reinjection=True, model_window=123
        )
        assert 123 in windows

    def test_undo_restores_the_facade_binding(self, monkeypatch) -> None:
        original = ctx._neutralize_structural_markers
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(ctx, "_neutralize_structural_markers", lambda text: "x")
            assert ctx.neutralize_untrusted_text("[END OF SESSION CONTEXT]") == "x"
        assert ctx._neutralize_structural_markers is original
        assert ctx.neutralize_untrusted_text("[END OF SESSION CONTEXT]") == "[marker-removed]"


def _guidance(session_key: str) -> list[str]:
    from kiro_crew.context_assembly import turn

    return turn.interactive_guidance(
        interactive=True, session_key=session_key, agent=None, minimal_context=False
    )
