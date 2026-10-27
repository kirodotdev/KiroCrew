"""Context builder — assembles memory, skills, and hooks into prompt context.

``ContextBuilder`` is the assembly owner: ``build_session_context`` and
``build_message`` decide the order of every block and which lifecycle carries it.
The class is defined here with its state, its store handles (``get_memory_for``,
``get_lessons_for``, ``ensure_store``) and its constants; the bodies of its other
methods live in owner modules and are bound onto the class, so ``ContextBuilder.<m>``
is the moved function itself. This module also owns the per-target memory store
caches, session store routing, the transcript reads a prompt replays, the redaction
of the session replay, and each name callers rebind on it.

The rest is composed from the owners in :mod:`kiro_crew.context_assembly`:
``markers`` (marker and fence neutralization), ``budget`` (character caps, model
windows, background admission), ``sections`` (stable conduct sections),
``replay`` (replay and recall projection), ``inclusion`` (context groups, skills
and steering), ``member`` (member identity and the V2 essentials envelope),
``store_admission`` (which store answers each memory and lesson section),
``turn`` (follow-up turn additions and the user's own turn text) and
``output_contract`` (the critical rules and the UI-language steer). The builder's
methods are composed from ``session_start`` (``build_session_context``),
``turn_message`` (``build_message``), ``agent_spec`` (the agent prompt, its templates
and the agent-spec reads), ``delivery_records`` (what each session was already sent)
and ``member_gates`` (the member desk and dispatch gates and the member entry
points). Their names stay importable from here, and the names callers rebind on
this module are read through it at call time.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib  # noqa: F401 - kept bound on the facade
import inspect
import json  # noqa: F401 - kept bound on the facade
import logging
import os  # noqa: F401 - kept bound on the facade
import re
import threading
import time  # noqa: F401 - kept bound on the facade
import unicodedata
from collections import OrderedDict, defaultdict, deque  # noqa: F401 - kept bound
from collections.abc import Awaitable, Callable, Collection, Iterator  # noqa: F401 - kept bound
from collections.abc import Set as AbstractSet  # noqa: F401 - kept bound on the facade
from contextlib import contextmanager  # noqa: F401 - kept bound on the facade
from dataclasses import dataclass  # noqa: F401 - kept bound on the facade
from datetime import datetime  # noqa: F401 - kept bound on the facade
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar  # noqa: F401 - kept bound on the facade

import kiro_crew.agent as _agent_module  # noqa: F401

# Bindings below that only the ``context_assembly`` owners read stay bound here:
# callers rebind them on this module, and the owners read them through it at call
# time, so such a patch still reaches the code that moved.
from kiro_crew import model_registry, resource_status  # noqa: F401
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.agent import _prompt_path, _shipped_prompt, is_managed_prompt  # noqa: F401
from kiro_crew.agent_discovery import WHOLE_CATALOG_TEMPLATES, agent_skill_globs
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP, is_claude_code
from kiro_crew.agent_spec_format import iter_agent_spec_files, parse_agent_spec_text  # noqa: F401
from kiro_crew.board_tag_grammar import is_grantable_tag_id
from kiro_crew.config import live  # noqa: F401
from kiro_crew.config.loader import KiroCrewConfig, workspace_dir_for
from kiro_crew.config.paths import kiro_agents_dir  # noqa: F401
from kiro_crew.context_assembly import agent_spec as _agent_spec
from kiro_crew.context_assembly import budget as _budgets  # noqa: F401 - loaded eagerly
from kiro_crew.context_assembly import delivery_records as _delivery_records
from kiro_crew.context_assembly import inclusion as _inclusion  # noqa: F401 - loaded eagerly
from kiro_crew.context_assembly import markers as _markers  # noqa: F401 - loaded eagerly
from kiro_crew.context_assembly import member as _member  # noqa: F401 - loaded eagerly
from kiro_crew.context_assembly import member_gates as _member_gates
from kiro_crew.context_assembly import output_contract as _output_contract  # noqa: F401
from kiro_crew.context_assembly import replay as _replay
from kiro_crew.context_assembly import sections as _sections  # noqa: F401 - loaded eagerly
from kiro_crew.context_assembly import session_start as _session_start  # noqa: F401
from kiro_crew.context_assembly import store_admission as _store_admission  # noqa: F401
from kiro_crew.context_assembly import tool_names as _tool_names  # noqa: F401 - loaded eagerly
from kiro_crew.context_assembly import turn as _turn  # noqa: F401 - loaded eagerly
from kiro_crew.context_assembly import turn_message as _turn_message  # noqa: F401 - loaded eagerly
from kiro_crew.context_assembly.agent_spec import (  # noqa: F401
    _load_steering_resources,
    _read_include_crew_context,
    _read_prompt_file,
    steering_target_admissible,
)
from kiro_crew.context_assembly.budget import (  # noqa: F401
    _COMPRESSED_HISTORY_CAP,
    _CONTEXT_BUDGET_BASE,
    _EPISODIC_INJECT_CAP,
    _EPISODIC_MEMORY_CAP,
    _HISTORY_BUDGET_CHARS,
    _HISTORY_REFERENCE_BASE,
    _LESSON_EXPERIENCE_CAP,
    _LESSONS_CAP,
    _LESSONS_STARTUP_CAP,
    _MEMORY_HISTORY_CAP,
    _MEMORY_PREFS_CAP,
    _MEMORY_PROJECTS_CAP,
    _MIN_CONTEXT_BUDGET_BASE,
    _PER_MESSAGE_CAP,
    _PREAMBLE_HEADROOM,
    _PREFS_STARTUP_CAP,
    _PROTECTED_CONTEXT_CHARS_PER_TOKEN,
    _PROTECTED_CONTEXT_FLOOR,
    _PROTECTED_CONTEXT_WINDOW_FRACTION,
    _REFERENCE_WINDOW_TOKENS,
    _SEMANTIC_MEMORY_CAP,
    _SKILLS_CAP,
    _STEERING_CAP,
    _budget,
    _effective_window,
    _prompt_build_embedding_deadline,
    _resolve_caps_cached,
    _ResolvedCaps,
    resolve_model_window,
    window_for_provider_client,
)
from kiro_crew.context_assembly.inclusion import (  # noqa: F401
    _GROUP_DESCRIPTIONS,
    CONTEXT_GROUP_LESSONS,
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_PROJECT,
    SWITCHABLE_CONTEXT_GROUPS,
    _build_context_scope_section,
    _group_included,
    _render_folder_steering_section,
)
from kiro_crew.context_assembly.markers import (  # noqa: F401
    _CALENDAR_FENCE_CLOSE_RE,
    _CALENDAR_FENCE_OPEN_RE,
    _MARKER_IGNORABLE_RANGES,
    _MEMBER_MARKER_RES,
    _MULTIBYTE_TABLE,
    _REPLY_FORMAT_RULES_MARKER,
    _REPLY_FORMAT_RULES_RE,
    _STRUCTURAL_MARKER_NEUTRALIZED,
    _STRUCTURAL_MARKER_RES,
    _THREAD_FENCE_CLOSE,
    _THREAD_FENCE_CLOSE_RE,
    _THREAD_FENCE_NEUTRALIZED,
    _THREAD_FENCE_OPEN,
    _THREAD_FENCE_OPEN_RE,
    _TODO_FENCE_CLOSE_RE,
    _TODO_FENCE_OPEN_RE,
    _TURN_LESSON_FRAME_RES,
    _UNTRUSTED_FENCE_RES,
    UNTRUSTED_CALENDAR_FENCE_CLOSE,
    UNTRUSTED_CALENDAR_FENCE_OPEN,
    UNTRUSTED_TODO_FENCE_CLOSE,
    UNTRUSTED_TODO_FENCE_OPEN,
    _apply_marker_spans,
    _fence_marker_regex,
    _is_marker_ignorable,
    _map_offset_through_spans,
    _marker_spans,
    _member_normalized_view,
    _merge_overlapping_spans,
    _neutralize_fence_markers,
    _neutralize_reply_format_markers,
    _scrub_member_payload,
    _scrub_turn_lesson,
    _structural_marker_spans,
    neutralize_untrusted_text,
)
from kiro_crew.context_assembly.member import (  # noqa: F401
    _MEMBER_BRIEFING_ITEM,
    _MEMBER_BRIEFING_ITEM_UNAVAILABLE,
    _MEMBER_DASHBOARD_ITEM,
    _MEMBER_HOW_YOU_WORK,
    _MEMBER_HOW_YOU_WORK_COMMON,
    _fit_folder_steering_into_envelope,
)
from kiro_crew.context_assembly.member_gates import (  # noqa: F401
    _desk_withheld,
    _member_dispatch_held,
    _template_selected_on_member_store,
)
from kiro_crew.context_assembly.output_contract import (  # noqa: F401
    _CRITICAL_RULES,
    _CRITICAL_RULES_CHANNEL,
    _CRITICAL_RULES_HEAD,
    _CRITICAL_RULES_SUBAGENT,
    _CRITICAL_RULES_TAIL,
    _CRITICAL_RULES_VARIANTS,
    _DIFF_RULE_CHANNEL,
    _DIFF_RULE_DASHBOARD,
    _UI_LANGUAGE_CATALOGS,
    _UI_LANGUAGE_TAG_RE,
    _build_ui_language_section,
    _critical_rules_for,
    normalize_ui_language_tag,
)
from kiro_crew.context_assembly.replay import (  # noqa: F401
    _CODE_BLOCK_RE,
    _JSON_BLOB_RE,
    _MODE_IDENTITY_RE,
    _RECALL_FALLBACK_MAX_ROWS,
    _REPLAY_BUDGET_CHARS,
    _REPLAY_CONVERSATION_MAX_ROWS,
    _REPLAY_INJECT_BUDGET_DIVISOR,
    _REPLAY_INJECT_CAP_CHARS,
    _REPLAY_INJECT_MAX_ROWS,
    _STOP_EVENT_CAP,
    _STOP_EVENT_RESOLVED_STATES,
    _TURN_OPENER_ROLES,
    RECALL_ROLES,
    _compress_assistant_message,
    _interrupted_opener_kind,
    _merge_replay_rows,
    _replay_identity,
    build_interrupted_turn_preamble,
)
from kiro_crew.context_assembly.sections import (  # noqa: F401
    _RESPONSE_PREFERENCES_FOOTER,
    _RESPONSE_PREFERENCES_HEADER,
    _ROLE_OTHER_MAX_LEN,
    _ROLE_PUNCT_ALLOWED,
    _RUNTIME_DISPLAY,
    _TECHNICAL_LEVEL_DESCRIPTIONS,
    _USER_ROLE_DESCRIPTIONS,
    _build_response_preferences_section,
    _build_user_profile_section,
    _is_allowed_role_char,
    _reply_style_rules,
    _resolve_runtime_source,
    _response_preferences_apply,
    _role_description,
    _runtime_display_name,
    _sanitize_free_text_role,
)
from kiro_crew.context_assembly.session_start import (  # noqa: F401
    _emit_context_section_timings,
)
from kiro_crew.context_assembly.session_start import build_session_context as _build_session_context
from kiro_crew.context_assembly.store_admission import (  # noqa: F401
    _LESSONS_SHOWN_PER_SESSION,
    _LESSONS_SHOWN_SESSIONS,
    _TURN_LESSONS_CHARS,
    _TURN_LESSONS_MAX,
    _ShownLessons,
    discard_memory_prefetch,
    embed_startup_query,
    prefetch_session_memory,
    reserve_memory_prefetch,
)
from kiro_crew.context_assembly.turn_message import build_message as _build_message
from kiro_crew.context_blocks import measure_prompt  # noqa: F401
from kiro_crew.cron import get_local_tz  # noqa: F401
from kiro_crew.folder_steering import (  # noqa: F401
    FOLDER_STEERING_OMISSION_SOURCE,
    SteeringCollection,
    collect_folder_steering,
    render_folder_steering,
    render_omission_notice,
)
from kiro_crew.hooks import (  # noqa: F401
    HOOK_INJECT_CONTEXT,
    HOOK_MODIFY,
    FileTooLargeError,
    HookManager,
    HookResult,
    safe_read_file,
    safe_read_file_bytes_nolink,
)
from kiro_crew.learn import LessonStore
from kiro_crew.member_essential_context import (  # noqa: F401
    _MAX_DOCUMENTS,
    ESSENTIAL_MAX_CHARS,
    MemberEssentialContextError,
    member_context_identity,
    member_inherits_default_resources,
    render_essentials,
)
from kiro_crew.members import (  # noqa: F401
    MemberLifecycle,
    MemberSlugError,
    member_briefing_path,
    member_briefing_supported,
    member_lifecycle,
    member_turn_context,
    read_member_briefing,
    read_member_rules,
    slug_for_name,
)
from kiro_crew.memory import MemoryStore
from kiro_crew.metrics.provider import get_recorder  # noqa: F401
from kiro_crew.quick_prompts import expand_quick_prompt  # noqa: F401
from kiro_crew.security import (  # noqa: F401
    audit_injection_dropped,
    contains_injection,
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.session_surface import has_dashboard_surface  # noqa: F401
from kiro_crew.skill_scope_pin import (  # noqa: F401 - read as ctx.<name>
    pinned_session_skill_spec,
    pinned_skill_globs,
)
from kiro_crew.skills import PROJECT_SKILL_BODY_CAP, SkillsLoader
from kiro_crew.warm_task_brief_contract import is_warm_task_brief_message

if TYPE_CHECKING:
    from kiro_crew.channel_history import ChannelHistory
    from kiro_crew.history import ConversationLog
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

# The value type of a per-session prompt-token reading (`ContextBuilder._session_reading`).
_ReadingT = TypeVar("_ReadingT")

# Lazy caches of per-target stores. The key is NOT a bare name: a memory store
# and a workspace are two namespaces, and a single key cannot hold both. A crew
# bound to store "acme" and a workspace also called "acme" collapsed into one
# cache slot and one path resolution, so whichever arrived first decided where
# the other one read. ``_target_key`` is the only thing that mints these keys:
#
#   "default"        the global v1 store -- UNCHANGED spelling, seeded eagerly
#                    in ``ContextBuilder.__init__`` and what every existing
#                    caller resolves to.
#   "ws:<name>"      a named workspace on the v1 path (markdown under that
#                    workspace, vectors shared with the global store).
#   "store:<name>"   a named store. V2 learned records and vectors share one
#                    member SQLite database; its manual profile stays Markdown.
#
# ``:`` cannot appear in a store name (``validate_memory_store_name``), so the
# two prefixes cannot collide with each other or with "default".
_memory_stores: dict[str, MemoryStore] = {}
_lesson_stores: dict[str, LessonStore] = {}
# Lazy cache of per-store VectorMemoryStore instances, keyed by RESOLVED store
# name (never a workspace). One instance per db_path is an invariant, not an
# optimization: two instances over one file do not share ``_db_lock``, which
# voids the serialization the store's own writes depend on. Populated only by
# ``ContextBuilder.ensure_store``, which is async because ``init()`` is blocking
# file IO end to end.
_vector_stores: dict[str, "VectorMemoryStore"] = {}
_store_cache_generation = 0

# Serializes lazy store creation: build_message runs on worker threads
# (run_in_embed_pool at every async call site), so two threads can race the
# check-then-insert for the same workspace key. Double-checked with the lock.
_stores_lock = threading.Lock()

#: Cache key for the global v1 store. The literal spelling is load-bearing:
#: ``ContextBuilder.__init__`` seeds it and every existing caller resolves to it.
_DEFAULT_KEY = "default"
_WS_KEY_PREFIX = "ws:"
_STORE_KEY_PREFIX = "store:"


def cached_vector_store_entries() -> tuple[tuple[str, "VectorMemoryStore"], ...]:
    """Snapshot existing handles only; users must revalidate ownership before use."""
    with _stores_lock:
        return tuple(_vector_stores.items())


def validated_cached_vector_stores() -> tuple["VectorMemoryStore", ...]:
    """Snapshot live named stores after revalidating their persisted identity."""
    from kiro_crew.memory_stores import require_memory_store

    snapshot = cached_vector_store_entries()
    for name, _store in snapshot:
        require_memory_store(name)
    return tuple(store for _name, store in snapshot)


def reset_memory_caches(memory: MemoryStore) -> None:
    """Drop a previous gateway's handles before its successor activates restores.

    The gateway calls this only inside its closed startup barrier and off-loop.
    Keep the newly wired Global object, which all service references share.
    """
    global _store_cache_generation
    with _stores_lock:
        _store_cache_generation += 1
        vectors = {id(store): store for store in _vector_stores.values()}
        for cached in _memory_stores.values():
            if cached is not memory and cached.vector_store is not None:
                vectors[id(cached.vector_store)] = cached.vector_store
        _vector_stores.clear()
        _memory_stores.clear()
        _lesson_stores.clear()
        _memory_stores[_DEFAULT_KEY] = memory
    for store in vectors.values():
        store.close()


def release_cached_memory_store(name: str) -> None:
    """Release an archived member's handles off-loop, preserving all disk data."""
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, validate_memory_store_name

    if name in ("", DEFAULT_MEMORY_STORE):
        return
    validate_memory_store_name(name)
    global _store_cache_generation
    with _stores_lock:
        _store_cache_generation += 1
        memory = _memory_stores.pop(_STORE_KEY_PREFIX + name, None)
        lessons = _lesson_stores.pop(_STORE_KEY_PREFIX + name, None)
        vector = _vector_stores.pop(name, None)
    vectors = {id(vector): vector} if vector is not None else {}
    if memory is not None:
        if memory.vector_store is not None:
            vectors[id(memory.vector_store)] = memory.vector_store
        memory.vector_store = None
        memory._invalidate_history_cache()
    if lessons is not None:
        with lessons._lock:
            lessons._cache = None
    for store in vectors.values():
        store.close()


def _resolved_store_name(memory_store: str | None) -> str:
    """Resolve a recorded identity. Only absent/default identity uses V1.

    An invalid, missing or unreadable named memory is an explicit error, even
    when a cached instance still exists. It must never widen into global memory.
    """
    from kiro_crew.memory_startup import require_memory_ready
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, require_memory_store

    require_memory_ready(memory_store or DEFAULT_MEMORY_STORE)
    if memory_store is None or memory_store in ("", DEFAULT_MEMORY_STORE):
        return ""
    return require_memory_store(memory_store)


def _target_key(workspace: str | None, memory_store: str | None) -> tuple[str, str]:
    """``(cache key, resolved store name)`` for a memory target.

    A non-empty store name always wins over *workspace*: a crew's silo is the
    tighter scope, and the two are separate namespaces rather than two spellings
    of one thing. The second element is ``""`` on the v1 path, which is what
    every caller branches on.
    """
    store_name = _resolved_store_name(memory_store)
    if store_name:
        return _STORE_KEY_PREFIX + store_name, store_name
    key = workspace or _DEFAULT_KEY
    if key == _DEFAULT_KEY:
        return _DEFAULT_KEY, ""
    return _WS_KEY_PREFIX + key, ""


async def prepare_store_vectors(
    ctx_builder: object, memory_store: str | None, *, session_key: str = ""
) -> None:
    """Prepare a named store's vector tier before a turn or run.

    Member V2 preparation is a precondition: unavailable identity, directory,
    database or vector tier raises ``UnknownMemoryStore`` with a reason. A
    declared legacy V1 store retains its Markdown/keyword preparation fallback;
    this never selects the global store in its place.

    Lives here, next to :meth:`ContextBuilder.ensure_store`, because both the
    dashboard turn and the subagent run need it and a second copy is how the two
    drift on what a failed prepare costs. ``subagent.py`` imports it at module
    scope, which is what ``bind_component_globals`` needs: it rebinds every
    ``*_impl`` function's globals to that module's namespace, so the name has to
    resolve THERE -- an import satisfies that exactly as a definition would.

    DEBUG, not warning: on the default store the step is a no-op on every turn,
    and a per-turn warning is how a log stops being read.
    """
    from kiro_crew.memory_startup import require_memory_ready

    execution = None
    if session_key:
        from kiro_crew.execution_context import read_session_execution

        execution = await asyncio.to_thread(read_session_execution, session_key)
        if execution is not None and execution.memory_mode == "temporary":
            return
        if execution is not None and execution.store.store_id != (memory_store or "default"):
            raise ValueError("The selected memory differs from this execution's recorded store")
    require_memory_ready(memory_store or "default")
    if memory_store in (None, "", "default"):
        return
    from kiro_crew.memory_stores import UnknownMemoryStore, memory_store_version

    def _resolve_identity() -> tuple[str, bool]:
        from kiro_crew.memory_stores import require_memory_store

        name = require_memory_store(memory_store or "default", require_directory=False)
        return name, memory_store_version(name) == 2

    name, private = await asyncio.to_thread(_resolve_identity)
    if private and session_key and (execution is None or execution.member_id is None):
        raise UnknownMemoryStore("This session has no canonical member execution identity")
    ensure = getattr(ctx_builder, "ensure_store", None)
    if ensure is None:
        if private:
            raise UnknownMemoryStore("The member memory cannot be prepared by this context builder")
        return
    try:
        result = ensure(memory_store)
        if inspect.isawaitable(result):
            result = await result
        if private and result is None:
            raise UnknownMemoryStore(f"The member memory {name!r} is unavailable")
    except Exception as exc:
        if private:
            if isinstance(exc, UnknownMemoryStore):
                raise
            raise UnknownMemoryStore(
                f"The member memory {name!r} could not be prepared: {exc}"
            ) from exc
        logger.debug(
            "could not prepare the vector store for memory store %r; it reads from "
            "markdown and keyword scoring instead",
            memory_store,
            exc_info=True,
        )


def store_of_session(conversation_log: object, session_key: str) -> str:
    """Return recorded routing without opening or repairing a memory database."""
    if not session_key:
        return ""
    from kiro_crew.execution_context import execution_from_record, read_session_execution
    from kiro_crew.memory_stores import UnknownMemoryStore, require_memory_store

    execution = read_session_execution(session_key)
    if execution is not None:
        return execution.store.legacy_name
    if conversation_log is None:
        return ""
    status = getattr(conversation_log, "get_metadata_status", None)
    if callable(status):
        metadata, readable = status(session_key)
        if not readable:
            raise UnknownMemoryStore(
                "The session's execution record is unreadable; Global was not used"
            )
    else:
        getter = getattr(conversation_log, "get_metadata", None)
        metadata = getter(session_key) if callable(getter) else {}
    if not isinstance(metadata, dict):
        raise UnknownMemoryStore("The session's execution record is malformed; Global was not used")
    execution = execution_from_record(metadata, required=False)
    if execution is not None:
        return execution.store.legacy_name
    store = metadata.get("memory_store", "")
    if not isinstance(store, str):
        raise UnknownMemoryStore("The session's memory identity is malformed; Global was not used")
    if not store or store == "default":
        return ""
    config = KiroCrewConfig.load()
    declaration = config.memory_stores.get(store)
    if declaration is not None and declaration.memory_version == 2:
        raise UnknownMemoryStore("The member's execution identity is missing; Global was not used")
    return require_memory_store(store, config=config, require_directory=False)


async def session_store_for_turn(ctx_builder: object, session_key: str) -> str:
    """Capture session routing and prepare optional learned memory off the loop.

    A temporary session never opens memory. If a member database is unavailable,
    essentials still build and the prompt names the unavailable learned section.
    Explicit memory tools keep their own strict availability checks.
    """

    store = await asyncio.to_thread(
        store_of_session, getattr(ctx_builder, "conversation_log", None), session_key
    )
    modes = getattr(ctx_builder, "_session_memory_modes", None)
    if not isinstance(modes, dict) or modes.get(session_key) != "temporary":
        try:
            await prepare_store_vectors(ctx_builder, store, session_key=session_key)
        except (OSError, ValueError, sqlite3.Error):
            from kiro_crew.execution_context import read_session_execution

            execution = await asyncio.to_thread(read_session_execution, session_key)
            if execution is None or execution.member_id is None:
                raise
            logger.info("Member learned memory unavailable during context preparation")
    return store


async def inherit_session_memory(
    ctx_builder: object, parent_session_key: str, session_key: str
) -> str:
    """Freeze inherited member and privacy before a continuation can start."""
    from kiro_crew.execution_context import (
        ExecutionContext,
        MemoryStoreRef,
        bind_session_execution,
        read_session_execution,
        stricter_memory_mode,
    )
    from kiro_crew.workflows.registry import _await_owned

    parent = await asyncio.to_thread(read_session_execution, parent_session_key)
    if parent is None:
        store = await asyncio.to_thread(
            store_of_session, getattr(ctx_builder, "conversation_log", None), parent_session_key
        )
        parent = ExecutionContext(None, MemoryStoreRef(store or "default"), "template", "kirocrew")
    resolver = getattr(ctx_builder, "memory_mode_for_session", None)
    parent_mode = await resolver(parent_session_key) if resolver is not None else parent.memory_mode
    modes = getattr(ctx_builder, "_session_memory_modes", None)
    child_mode = modes.get(session_key, "persistent") if isinstance(modes, dict) else "persistent"
    inherited = parent.with_mode(stricter_memory_mode(parent_mode, child_mode))
    if isinstance(modes, dict):
        modes[session_key] = inherited.memory_mode
    await _await_owned(
        asyncio.create_task(asyncio.to_thread(bind_session_execution, session_key, inherited))
    )
    return await session_store_for_turn(ctx_builder, session_key)


@functools.lru_cache(maxsize=1)
def _shared_embed_fn() -> "Callable[[str], list[float] | None]":
    """The same bounded process-wide embedding callable used by every store.

    The embedding module owns cache bounds, duplicate coalescing and backend
    replacement. Store signatures still decide whether persisted vectors are
    comparable; sharing a callable never mixes stored rows between members.
    """
    from kiro_crew.embeddings import make_sync_embed_fn

    return make_sync_embed_fn()


class _InflightStoreOpen:
    """One shared open of a named store, the cache generation it opens under, and
    the count of callers awaiting it."""

    __slots__ = ("task", "generation", "waiters")

    def __init__(self, task: "asyncio.Task[VectorMemoryStore | None]", generation: int) -> None:
        self.task = task
        self.generation = generation
        self.waiters = 0


#: In-flight opens by RESOLVED store name, touched only on the event-loop thread
#: (``_build_store_vectors`` is a coroutine and never awaits between its check and
#: its insert). One open per store at a time: a burst of first-touch requests on
#: an uncached store -- the 65-fact seeding in ``member-memory.spec.ts`` sends five
#: ``PUT /api/memory/semantic`` at once -- shares a single ``_prepare`` worker.
#: Without the sharing each request runs its own: two ``KiroCrewConfig.load()``,
#: an identity check with ``PRAGMA quick_check``, and a SQLite open plus
#: migrations under the exclusive namespace lock, after which every worker but
#: the first discards its store at publication.
_inflight_store_opens: dict[str, _InflightStoreOpen] = {}


async def _build_store_vectors(name: str) -> "VectorMemoryStore | None":
    """Construct, init and wire a named store's own VectorMemoryStore, once per burst.

    Concurrent callers for the same uncached store share one open: the first
    starts it, the rest await the same task. Each awaiter is shielded, so one
    request's cancellation does not cancel the open under the others. The open
    itself is cancelled only when its LAST awaiter is, which keeps the single
    caller's contract: a cancelled open never publishes, and the worker closes
    the store (and releases its lock FD) once ``init()`` returns.

    An open is shared only within the cache generation it started under. A
    release or reset that lands while it is in flight bumps the generation and
    makes that open refuse at publication (``Memory cache changed during
    preparation``); a caller arriving after the bump starts a fresh open under
    the new generation instead of joining the one that is about to refuse.
    """
    with _stores_lock:
        generation = _store_cache_generation
    entry = _inflight_store_opens.get(name)
    if entry is None or entry.generation != generation:
        task = asyncio.get_running_loop().create_task(_open_store_vectors(name, generation))
        entry = _InflightStoreOpen(task, generation)
        _inflight_store_opens[name] = entry

        def _forget(_task: "asyncio.Task[VectorMemoryStore | None]") -> None:
            if _inflight_store_opens.get(name) is entry:
                del _inflight_store_opens[name]

        task.add_done_callback(_forget)
    entry.waiters += 1
    try:
        return await asyncio.shield(entry.task)
    except asyncio.CancelledError:
        entry.waiters -= 1
        if entry.waiters == 0 and not entry.task.done():
            # Drop the entry BEFORE cancelling: the task finishes (and
            # ``_forget`` runs) only on a later loop step, and a caller arriving
            # in between must start a fresh open rather than join one whose
            # shield is about to hand it a CancelledError nobody sent it.
            if _inflight_store_opens.get(name) is entry:
                del _inflight_store_opens[name]
            entry.task.cancel()
        raise


async def _open_store_vectors(name: str, generation: int) -> "VectorMemoryStore | None":
    """The one open behind :func:`_build_store_vectors`, under *generation*.

    Every blocking step is offloaded. ``_stores_lock`` is held only around the
    cache check-and-insert, never across ``init()``, so a slow first touch of one
    store cannot serialize every embed worker. Publication refuses when the cache
    generation differs from *generation*.
    """
    from kiro_crew.embeddings import (
        make_sync_embed_fn,
        model_file_present,
        reconcile_store_embedding_space,
    )
    from kiro_crew.memory_stores import UnknownMemoryStore, require_memory_store, resolve_store_path
    from kiro_crew.vector_memory import VectorMemoryStore, declared_store

    cancelled = threading.Event()

    def _prepare() -> VectorMemoryStore | None:
        # The worker owns the connection until cache publication. Cancellation
        # cannot close it under init(), or strand its lock FD after init returns.
        store: VectorMemoryStore | None = None
        published = False
        try:
            cfg = KiroCrewConfig.load()
            mem = cfg.memory
            declaration = cfg.memory_stores[name]
            # Admit the declared store before opening SQLite. A member store is
            # provisioned only by explicit creation; a missing directory or
            # database must remain a visible loss rather than being recreated by
            # the lazy opener.
            require_memory_store(name)
            # Tuning comes from `config=cfg`, applied through the same `reconfigure`
            # the live reload calls, so boot and reload cannot drift apart on a
            # hand-copied list. `declared_store` picks the member opener for a V2
            # declaration, the same one `kirocrew memory carve/export --store` use.
            store = declared_store(
                resolve_store_path(name), store_id=name, config=cfg, embedding_dim=mem.embedding_dim
            )
            store.init()
            if cancelled.is_set():
                return None
            store.embed_fn_factory = make_sync_embed_fn
            if model_file_present():
                store.embed_fn = _shared_embed_fn()
            if declaration.memory_version != 2:
                try:
                    reconcile_store_embedding_space(store)
                except Exception:
                    logger.debug(
                        "could not stamp the embedding space for store %r", name, exc_info=True
                    )
            # Revalidate at the publication edge. Restore, retirement, or an
            # operator edit may have changed the declaration while initialization
            # was running; never publish a handle for a store that does not
            # resolves to the declared member database.
            require_memory_store(name)
            with _stores_lock:
                if cancelled.is_set():
                    return None
                if generation != _store_cache_generation:
                    raise UnknownMemoryStore(
                        "Memory cache changed during preparation; retry the member turn"
                    )
                existing = _vector_stores.get(name)
                if existing is not None:
                    return existing
                _vector_stores[name] = store
                published = True
                cached = _memory_stores.get(_STORE_KEY_PREFIX + name)
                if cached is not None:
                    cached.vector_store = store
            return store
        finally:
            if store is not None and not published:
                store.close()

    try:
        return await asyncio.to_thread(_prepare)
    except asyncio.CancelledError:
        # Serialize cancellation against publication; a published instance is
        # owned by the cache, otherwise the worker's finally owns its cleanup.
        with _stores_lock:
            cancelled.set()
        raise


# Confined project bodies share the skills module's byte bound; still a
# descriptor-pinned byte/read bound, not unlimited project-file injection.
_PINNED_PROJECT_BODY_CAP = PROJECT_SKILL_BODY_CAP


def _member_marker_spans(text: str) -> list[tuple[int, int]]:
    """Merged spans of forgeable member-authority markers, in ORIGINAL coords.

    The matching view mirrors :func:`_member_normalized_view` — NFKC first,
    then default-ignorable drops, ``_MULTIBYTE_TABLE`` punctuation folds and
    ``Pd`` dashes to ``-`` — but is built PER COMBINING
    SEQUENCE (base character plus its trailing combining marks) with an origin
    map back to original offsets, the same mechanism
    :func:`_structural_marker_spans` uses for its view.

    Sequences — not lone characters — are the normalization unit because
    canonical composition happens ACROSS characters within one sequence:
    ``I`` + U+0307 composes to ``İ`` (U+0130) under whole-string NFKC, and
    ``İ`` case-folds to ASCII ``i``, so a marker word carrying an embedded
    combining mark matches the case-insensitive patterns on the whole-string
    view. A per-character view cannot compose the pair, leaves the mark
    splitting the word, misses the match, and strands the scrub on the
    whole-payload fail-closed floor — corrupting legitimate content the
    span-local rewrite exists to protect.

    Residual divergences from the whole-string view (e.g. Hangul jamo, where
    STARTERS compose with each other) survive this grouping, but every such
    composition yields a non-ASCII char with no ASCII case fold, so it cannot
    reach the marker alphabet; :func:`_scrub_member_payload` still re-checks
    its result against the whole-string view and fails CLOSED regardless.

    A single original char may fold to several view chars (``㎢`` → ``km2``),
    and a sequence's marks travel with its base; a match touching any part of
    the fold maps to the WHOLE original sequence, so spans only ever
    over-cover — the deny direction.
    """
    if text.isascii():  # pure ASCII cannot contain confusables — match directly
        raw = [m.span() for pattern in _MEMBER_MARKER_RES for m in pattern.finditer(text)]
    else:
        norm: list[str] = []
        origin: list[tuple[int, int]] = []  # (start, end] original span per view char
        i = 0
        length = len(text)
        while i < length:
            if unicodedata.category(text[i]) == "Cf":
                i += 1  # invisible for matching; still inside any marker's original span
                continue
            # Extend through the base char's combining marks (Mn/Mc/Me). A Cf
            # char terminates the sequence exactly as it blocks composition in
            # the whole-string view (NFKC runs before the Cf drop there).
            end = i + 1
            while end < length and unicodedata.category(text[end]).startswith("M"):
                end += 1
            seq = text[i:end]
            if seq.isascii():  # single ASCII char, no marks: no fold possible
                norm.append(seq)
                origin.append((i, end))
            else:
                for c in unicodedata.normalize("NFKC", seq):
                    if _is_marker_ignorable(c):
                        continue
                    for folded in c.translate(_MULTIBYTE_TABLE):
                        norm.append("-" if unicodedata.category(folded) == "Pd" else folded)
                        origin.append((i, end))
            i = end

        norm_str = "".join(norm)
        raw = []
        for pattern in _MEMBER_MARKER_RES:
            for m in pattern.finditer(norm_str):
                s, e = m.span()
                # Through the last matched sequence, in original coordinates.
                raw.append((origin[s][0], origin[e - 1][1]))

    return _merge_overlapping_spans(raw)


def _neutralize_structural_markers(text: str) -> str:
    """Strip forgeable primary boundary markers from untrusted prompt content.

    Matching is case-insensitive and whitespace-tolerant between the marker's
    words, so ``[ end  of   session context ]`` and mixed case are neutralized
    too. Must NOT be applied to the trusted ``_CRITICAL_RULES`` block, which
    legitimately carries these markers.

    SPAN-LOCAL: only a matched marker span is rewritten; every other byte of the
    input is preserved verbatim. See :func:`_structural_marker_spans` for how
    exotic-character forgeries are caught without mutating legitimate text.
    """
    return _apply_marker_spans(text, _structural_marker_spans(text))


def _board_safe_tag_name(raw: object) -> str:
    """Admit one board tag handle onto the trusted [BOARD] context line.

    ALLOWLIST, not sanitize-then-screen — the terminal form of this guard.
    The board line carries tag IDS (machine handles, the same strings
    ``chat_tag`` consumes); prose was never legitimate here. The admitted
    grammar is the CLOSED set of ids a grant can exist for at all
    (``is_grantable_tag_id``): a 12-hex id the dashboard minted, or one of the
    code-level default workflow states. That grammar has no room for words —
    an instruction cannot be spelled in twelve hex digits, and the defaults
    are five known constants — so an agent-authored id planted in
    agent-writable ``tags.json`` can neither acquire a grant (the PATCH mint
    refuses it) nor be rendered here even if a row for it somehow existed.
    Nothing is rewritten, so no strip can reconstruct a payload. The
    injection heuristic below is kept as a redundant second screen, not as
    the defense. Rejected handles are dropped by the caller.
    """
    value = raw if isinstance(raw, str) else ""
    if not is_grantable_tag_id(value):
        return ""
    if contains_injection(re.sub(r"[-_./]", " ", value)):
        return ""
    return value


# Per-section limits are derived below; shared admission never sums them.
def _member_backend_can_dispatch(cfg: "KiroCrewConfig | None" = None) -> bool:
    """Whether the configured member backend can mount the dispatch tools.

    The member operating-mode block teaches ``session_*`` tools that arrive as
    a per-session mount — a capability only wire-capable backends have. When
    ``agent.member_acp_backend`` resolves outside that set (governance refusal,
    unknown value degrading to kiro), the tools are simply not mounted, and
    injecting instructions for tools the session does not hold would send the
    member chasing refusals. Fail-safe both ways: on any resolution error the
    block is withheld, which degrades to plain chat rather than to a lie.

    ``cfg`` lets a caller that already loaded the config share the handle —
    the context builder calls this once per member turn, so a second disk
    read would be pure waste.
    """
    try:
        from kiro_crew.acp_backends import (
            ACP_BACKENDS_MEMBER_DISPATCH,
            resolve_selected_backend,
        )

        if cfg is None:
            from kiro_crew.config import KiroCrewConfig

            cfg = KiroCrewConfig.load()
        backend = resolve_selected_backend(cfg.agent.member_acp_backend)
        return backend in ACP_BACKENDS_MEMBER_DISPATCH
    except Exception:
        logger.debug("member backend capability check failed", exc_info=True)
        return False


# A fresh V1 prompt can query semantic, episodic, and lesson memory in order.
# All three share one model and must share one deadline: resetting the budget per
# section would let concurrent starts pay the queue wait repeatedly and approach
# the gateway's 25-second loop-stall hard-exit budget. A missed vector degrades
# to each retrieval path's existing lexical fallback. A build that finds its first
# message's embedding already running waits ``memory.startup_recall_timeout_secs``.
_PROMPT_BUILD_EMBED_TIMEOUT_SECS = 5.0


def _resolve_caps(window_tokens: int | None) -> _ResolvedCaps:
    """Fixed activity/discovery caps with separate window-scaled thread caps.

    No character count is converted to a model token or cost estimate here.
    """
    return _resolve_caps_cached(_effective_window(window_tokens))


# Global ceiling at the reference (1M) window — the historical
# ``_MAX_CONTEXT_CHARS``, now DERIVED from the single section-sum in
# ``_ResolvedCaps.max_context`` so it can never drift from the per-section caps.
_MAX_CONTEXT_CHARS = _resolve_caps(_REFERENCE_WINDOW_TOKENS).max_context


def _build_stop_event_notes(conversation_log: "ConversationLog", session_key: str) -> str:
    """Render recent resolved stop_events as short system notes for LLM context."""
    # Bound the scan: only the last _STOP_EVENT_CAP stop events matter,
    # and stop events from hundreds of turns ago are not actionable context.
    # Matches the pattern used by ``build_cancelled_turn_preamble`` below.
    messages = conversation_log.recent(session_key, max_messages=20)
    return _replay.stop_event_notes(messages)


# Docs directory bundled inside the kiro_crew package
_BUNDLED_DOCS_DIR = Path(__file__).resolve().parent / "docs"


def _config_scoped_groups(
    context_groups: frozenset[str] | None, cfg: "KiroCrewConfig | None" = None
) -> frozenset[str] | None:
    """The caller-passed scope intersected with the operator's config toggles.

    ``memory.inject_memory`` / ``memory.inject_lessons`` (with
    ``memory.persistence_enabled`` as the global switch) withhold a group on
    EVERY surface. Intersecting here — instead of at each call site — keeps a
    new context entry point from silently escaping the config;
    ``build_session_context``, the v2 essentials builder and the
    post-compaction re-injection all route through this.
    Subagent narrowing is preserved: config can only remove groups from the
    caller-passed scope, never add one back. Only the memory and lessons groups
    are ever subtracted, so a project-group gate reads the caller scope
    directly. The ``[CONTEXT SCOPE]`` block
    stays keyed to the caller-passed value, because "your parent withheld"
    describes per-spawn narrowing, not the operator's standing choice.
    """
    if cfg is None:
        cfg = KiroCrewConfig.load()
    withheld: set[str] = set()
    if not (cfg.memory.persistence_enabled and cfg.memory.inject_memory):
        withheld.add(CONTEXT_GROUP_MEMORY)
    if not (cfg.memory.persistence_enabled and cfg.memory.inject_lessons):
        withheld.add(CONTEXT_GROUP_LESSONS)
    if not withheld:
        return context_groups
    base = SWITCHABLE_CONTEXT_GROUPS if context_groups is None else context_groups
    return frozenset(base) - withheld


def _build_docs_section() -> str:
    """Build a lightweight docs pointer for session context.

    Resolves the bundled docs path from the installed Python package.
    Returns empty string if the docs directory doesn't exist.
    """
    if not _BUNDLED_DOCS_DIR.is_dir():
        return ""
    return (
        "[DOCUMENTATION]\n"
        f"KiroCrew docs: {_BUNDLED_DOCS_DIR}\n"
        "\n"
        "For KiroCrew behavior, commands, config, or architecture: "
        "consult local docs first.\n"
        "When diagnosing issues, run `kirocrew status` or "
        "`kirocrew doctor` yourself when possible.\n"
        "[END DOCUMENTATION]\n\n"
    )


def ui_language_tag(cfg: "KiroCrewConfig") -> str:
    """Return ``dashboard.language`` as a validated, *shipped* tag, or ``""``.

    Public because the UI language now steers more than the session-context block
    below: the dashboard's auto-titler asks a background model for a session name
    that renders in the sidebar, so it needs the same tag resolved the same way.
    One resolver keeps the two from disagreeing about what counts as a usable
    value (see ``_UI_LANGUAGE_TAG_RE`` for why the shape is re-checked here even
    though the writer validates it).

    Beyond shape, the tag must name a catalog the dashboard actually ships
    (``_UI_LANGUAGE_CATALOGS``). A shape-valid tag with no catalog — e.g. a
    persisted ``ar``, or a language later removed from the frontend registry —
    renders the chrome in English (the SPA falls back to detection), so steering
    the agent to it would put tool-call purpose pills, and the Slack/Discord task
    titles derived from them, in a language the UI around them cannot render.
    Those purposes persist in session history and are inherited by forked
    sessions, so the mismatch is durable. A non-catalog tag therefore takes the
    identical path to ``""``: inject nothing, and the model mirrors the
    conversation instead.

    ``""`` means "the backend does not know" — nothing was chosen (the
    "follow the browser" sentinel, resolved in the SPA's ``resolveLanguage()``),
    the stored value is not tag-shaped, or it names no shipped catalog. Callers
    must treat it as unknown rather than as English. A caller that CAN learn an
    unconfigured browser's resolved language (a request-scoped hint) validates it
    through the same :func:`normalize_ui_language_tag` gate this delegates to,
    so config and hint can never disagree about what counts as usable.
    """
    return normalize_ui_language_tag(cfg.dashboard.language, source="dashboard.language")


def _project_steering_delivered(
    provider_type: str | None, native_steering: bool, project: str | None
) -> bool:
    """Whether the project/global ``.kiro/steering`` trees already reach the model.

    Three paths exist and this names all of them, so the folder-steering dedup
    skips those trees ONLY where one of them is in effect: kiro-cli (the ACP
    label) loads an agent's declared ``resources`` under either
    setting, and while *project* inherits kiro-cli's default resources the
    project and global ``.kiro/steering`` trees arrive wholesale too (a
    workspace that sets ``chat.disableInheritingDefaultResources`` gets only
    the declared resources, so the folder carries the trees minus the
    documents in the launch record); the Claude Code
    seam receives the explicit ``[Steering resources]`` load in ``build_message`` (gated on
    ``is_cc``); KAS reports ``native_steering`` on
    its session provider. Every other harness -- Codex, OpenCode, Pi, Goose,
    DeepSeek -- has NO path for those trees today, so a folder that declares one
    of them must deliver its documents itself rather than skip them as "already
    delivered" with nothing arriving in their place. A harness that is not
    named (``None``) is not assumed to be kiro-cli: it matches no path here, so
    the folder carries the trees whole. The opt-out is read only
    on the kiro-cli disjunct: a kiro-cli setting changes nothing on another
    harness. The driver is asked directly, with no admission check on
    *project*: this path also serves non-member sessions and must not raise.
    """
    return (
        (provider_type == PROVIDER_ACP and acp_driver.inherits_default_resources(project))
        or is_claude_code(provider_type)
        or bool(native_steering)
    )


# Per-agent opt-out cache for the dashboard-contract context (``_CRITICAL_RULES``
# + the dashboard tool nudges). ``build_message`` reads the flag on EVERY turn, so
# a cold JSON scan there would be a per-turn cost; memoize by agent name. Staleness
# within a process is acceptable — the same trade the un-cached ``_load_agent_prompt``
# read already makes (an agent's spec is not edited mid-process in practice).
_INCLUDE_CREW_CONTEXT_CACHE: dict[str, bool] = {}


def _agent_includes_crew_context(agent: str | None) -> bool:
    """Whether to inject the Crew's dashboard-contract context for *agent*.

    Opt-out, defaulting to inject. The built-in ``kirocrew`` agent and an empty
    agent always return ``True`` (never a custom agent, so nothing to opt out of).
    A CUSTOM agent injects unless its materialized JSON explicitly sets
    ``includeCrewContext: false`` — so a plain custom agent with no flag still gets
    the critical rules, exactly as it did before the opt-out existed. Memoized by
    agent name to keep the per-turn ``build_message`` read off the JSON scan path.
    """
    if not agent or agent == "kirocrew":
        return True
    cached = _INCLUDE_CREW_CONTEXT_CACHE.get(agent)
    if cached is None:
        cached = _read_include_crew_context(agent)
        _INCLUDE_CREW_CONTEXT_CACHE[agent] = cached
    return cached


def invalidate_include_crew_context_cache() -> None:
    """Drop the memoized ``includeCrewContext`` reads.

    Called when the materialized-agent snapshot is rescanned
    (``refresh_materialized_agents``): an app install/upgrade rewrites an agent's
    JSON mid-process via ``_register_agents``, so a value cached before that write
    — including a default ``True`` cached on a first read that raced ahead of the
    not-yet-written spec — would otherwise stay wrong until a gateway restart, the
    exact restart-heals failure class this fix exists to remove. Clearing forces
    the next ``build_session_context`` / ``build_message`` to re-read the flag.
    """
    _INCLUDE_CREW_CONTEXT_CACHE.clear()


def build_cancelled_turn_preamble(
    conversation_log: "ConversationLog",
    session_key: str,
    *,
    user_cap: int = 2000,
    assist_cap: int = 2000,
) -> str:
    """Build a preamble describing the most recent cancelled turn, if any.

    kiro-cli does not persist cancelled turns to its ACP conversation log,
    so after a soft-stop the LLM has no memory of what the user asked or
    what it had started saying. Scan the persisted ``conversation_log``
    backwards for a ``stop_event`` marker, then find the user message
    immediately before it plus any assistant text in between. Return a
    short bracketed preamble. Returns "" if nothing to inject.

    Called by both dashboard and Slack callers after ``prev_turn_cancelled``
    is observed on the session.
    """
    try:
        # Whole rows: ``recent()`` keeps only role and content, and the walk reads
        # ``meta`` to step past a steer the person took back. Both callers run this
        # in a worker thread, since it reads the whole transcript.
        rows = _without_warm_task_brief_messages(conversation_log.read_messages(session_key))
    except Exception:
        return ""
    return _replay.cancelled_turn_preamble(rows[-20:], user_cap=user_cap, assist_cap=assist_cap)


# ── Provider-Agnostic Session Replay ──


def _without_warm_task_brief_messages(messages: list[dict]) -> list[dict]:
    return [row for row in messages if not is_warm_task_brief_message(row)]


def _replay_rows(
    conversation_log: "ConversationLog | None",
    session_key: str,
    *,
    exclude_last_n: int = 0,
    pending_messages: list[dict] | None = None,
    current_message: dict | None = None,
) -> list[dict]:
    """Tail of the chain under per-role quotas, in chronological order.

    Conversation rows get the full quota whatever the inject volume, which a
    single bounded query cannot guarantee.
    """
    messages = conversation_log.read_messages_chained(session_key) if conversation_log else []
    if pending_messages is not None or current_message is not None:
        messages = _merge_replay_rows(messages, pending_messages or [], current_message)
    elif exclude_last_n > 0:
        messages = messages[:-exclude_last_n]
    messages = _without_warm_task_brief_messages(messages)
    messages = [row for row in messages if not _replay.is_context_cap_notice(row)]
    return _replay._quota_tail(
        messages, conv_max=_REPLAY_CONVERSATION_MAX_ROWS, inject_max=_REPLAY_INJECT_MAX_ROWS
    )


def _recall_rows(
    conversation_log: "ConversationLog",
    session_key: str,
    *,
    conv_max: int,
    inject_max: int = _REPLAY_INJECT_MAX_ROWS,
    exclude_last_n: int = 0,
) -> list[dict]:
    """Bounded recall under per-role quotas, in chronological order.

    ``recent()`` role-filters and then takes a plain tail slice, so a run of
    ``inject`` rows longer than the bound is the entire read and conversation
    disappears. Quotas are counted separately here, so notes reach the model
    without competing with user/assistant turns for the same slots.

    ``exclude_last_n`` drops trailing raw entries BEFORE role filtering, matching
    ``recent()``.

    Rows are handed out with their image references stripped
    (:func:`~kiro_crew.image_refs.strip_image_refs`). A row's picture
    belonged to an earlier turn and cannot travel in a text vehicle, so the
    reference is the only thing that would arrive: either as a path the prompt
    builder re-inlines -- resurrecting an image a compaction already dropped --
    or, once the file is gone, as prose naming a picture the model cannot see.
    Stripping HERE is what makes the guarantee hold for its consumer, the
    thread-history fallback in ``build_session_context``.
    """
    messages = conversation_log.read_messages(session_key)
    if exclude_last_n > 0:
        messages = messages[:-exclude_last_n]
    messages = _without_warm_task_brief_messages(messages)
    return _replay._quota_tail(messages, conv_max=conv_max, inject_max=inject_max)


def build_session_replay(
    conversation_log: "ConversationLog | None",
    session_key: str,
    *,
    exclude_last_n: int = 0,
    model_window: int | None = None,
    pending_messages: list[dict] | None = None,
    current_message: dict | None = None,
    budget_chars: int = _REPLAY_BUDGET_CHARS,
) -> str | None:
    """Build session replay from KiroCrew's conversation_log.

    Keeps as many recent messages as fit within *budget_chars*
    (``_REPLAY_BUDGET_CHARS`` unless the conductor context cap's trim asks for
    its larger tail), prioritizing the most recent exchanges (tail-heavy). Used
    when a session is picked up by a different provider or after process death.

    Same-provider resume uses native ACP session/load instead (full fidelity
    without needing this injection).

    With *pending_messages*, merge the disk and live window by delivery identity
    and exclude *current_message* explicitly before applying quotas or budgets.
    The legacy *exclude_last_n* applies only when no live snapshot is supplied.

    *model_window* scales the replay budget to the active model's context
    window (the dashboard's primary history vehicle — it must shrink on a
    smaller model just like the capped sections do, or it would dominate a 200K
    window). ``None`` ⇒ the 1M reference (unchanged default). The budget is
    scaled by the same factor as the section caps and floored to one message.
    """
    messages = _replay_rows(
        conversation_log,
        session_key,
        exclude_last_n=exclude_last_n,
        pending_messages=pending_messages,
        current_message=current_message,
    )
    if not messages:
        return None

    replay = _replay.replay_text(messages, model_window, budget_chars=budget_chars)
    replay, _ = redact_exfiltration_urls(replay)
    replay, _ = redact_credentials(replay)
    return replay.translate(_MULTIBYTE_TABLE)


def _skills_injection_plan(
    agent: str | None,
    *,
    is_cc: bool,
    project_dir: str | Path | None = None,
    provider_type: str | None = None,
    session_key: str | None = None,
) -> tuple[bool, list[str]]:
    """Whether to inject skills for *agent*, plus the glob restriction to apply.

    A session whose skill scope is pinned to one spec file (``skill_scope_pin``: a
    palette-opened app agent on kiro-cli) is scoped by that file, never by a name
    lookup that could reach a checkout's same-named spec.

    THE single source of truth for the agent-scoping rule, shared by the
    session-start injection and the post-compaction re-injection. Mapped agents
    receive the same scoped directory on either backend; an unmapped default
    agent gets the startup directory, and an unmapped custom agent on kiro-cli
    (*provider_type* is the ACP label) gets the skill trees kiro-cli lets it
    inherit unless its workspace opted out. No other harness lends those trees,
    so there an unmapped custom agent still gets none.

    Deliberately one function rather than the same expression written twice: a
    hand-copied second gate is exactly what let the re-injection path ship
    without scoping, handing a mapped agent the catalog its mapping excludes.
    """
    from kiro_crew.agent_files import is_kirocrew_default_agent

    inherit = provider_type == PROVIDER_ACP
    pinned = pinned_skill_globs(session_key, project_dir=project_dir, inherit_defaults=inherit)
    globs = (
        pinned
        if pinned is not None
        else (
            agent_skill_globs(
                agent,
                project_dir=project_dir,
                inherit_defaults=inherit,
                session_key=session_key or "",
            )
            if agent
            else []
        )
    )
    is_custom = (
        bool(agent)
        and not is_kirocrew_default_agent(agent)
        and agent not in WHOLE_CATALOG_TEMPLATES
    )
    return (bool(globs) or not is_custom), globs


class ContextBuilder:
    """Builds context for injection into ACP prompts.

    Assembles memory, skills, and hook-injected context into a single
    string that gets prepended to the user's message on the first turn
    of a session (or after a context reset).
    """

    # The delegation-capacity token a prompt carries, and how many sessions'
    # readings of it are held at once.
    _MAX_SUBAGENTS_TOKEN = "{{MAX_SUBAGENTS}}"
    _CAP_FIGURE_SESSIONS = 512
    # Bounds for the sent-skill-body record. The session count matches the
    # `_cap_figures` bound (one record per live session; the oldest evicts past
    # it), and the per-session entry count bounds the inner dict so one session
    # matching an unbounded set of distinct skills cannot grow without limit.
    # Both evict oldest-first, and an eviction only costs a re-inject on the next
    # match (the fail-safe direction), never a silent miss.
    _SENT_SKILL_BODY_SESSIONS = _CAP_FIGURE_SESSIONS
    _SENT_SKILL_BODY_ENTRIES = 64
    # One shared bound for every name list the ``skill_delivery`` audit row
    # persists (bodies / pointers / demoted). A single turn can match at most
    # ``max_triggered`` skills, but that is operator-configurable, so cap the
    # audit explicitly rather than trust the matcher's bound: a row never grows
    # past this many names per field, and an ``*_omitted`` count records how
    # many were dropped so the audit stays honest about what it truncated.
    _SKILL_DELIVERY_AUDIT_NAMES = 64
    # Per-name character budget for the audit row: a name is a skill key or a
    # 64-hex digest, so this is generous, but it caps the BYTES a single name
    # contributes -- without it the count cap alone lets one arbitrarily long
    # name grow the row unboundedly, which the count cap was meant to prevent.
    _SKILL_DELIVERY_AUDIT_NAME_LEN = 128
    # The Computer Use section's slot. Its reading is held per session under the
    # same bound as the cap figure; see `_session_computer_use_gate`.
    _COMPUTER_USE_TOKEN = "{{COMPUTER_USE_BLOCK}}"

    @staticmethod
    def get_memory_for(
        workspace: str | None = None, memory_store: str | None = None
    ) -> MemoryStore:
        """Return a MemoryStore for a workspace or a NAMED memory store.

        *memory_store* wins when it names a non-default store, because a crew's
        silo is the tighter scope; *workspace* is the v1 path and stays the
        meaning of a lone positional argument. Pass the store name already
        resolved (``ResolvedBindings.memory_store_name``) — this does not derive a
        store from an agent name, since ``agent=`` at every call site carries a
        kiro-cli template id, a namespace disjoint from ``cfg.agents``.

        V2 learned records use one prepared member SQLite service; the facade
        never reads learned Markdown or JSONL. Manual profiles are independent
        of database readiness. An unprepared legacy V1 store may still answer
        from its own Markdown and keyword scoring. Prepared stores come
        from :meth:`ensure_store`, which the caller must await first; see there
        for why this method cannot do it.

        Thread-safe: build_message runs on worker threads (offloaded via
        run_in_embed_pool from every async call site), so concurrent first
        requests for the same target must not double-init the store.
        """
        key, store_name = _target_key(workspace, memory_store)
        if key not in _memory_stores:
            with _stores_lock:
                if key not in _memory_stores:
                    if store_name:
                        from kiro_crew.memory_stores import (
                            UnknownMemoryStore,
                            ensure_memory_store_dir,
                            memory_index_path_for,
                            memory_store_version,
                        )

                        version = memory_store_version(store_name)
                        store = MemoryStore(
                            workspace=ensure_memory_store_dir(store_name),
                            index_db=memory_index_path_for(store_name),
                            memory_version=version,
                            vector_store=_vector_stores.get(store_name),
                        )
                        store.init()
                        # NO shared-vector hop. Attaching the global store here is
                        # what made the crew editor's Memory Store control a
                        # read-side illusion: markdown split while every crew's
                        # semantic, episodic and lesson rows stayed in one table.
                        store.vector_store = _vector_stores.get(store_name)
                        if memory_store_version(store_name) == 2 and store.vector_store is None:
                            raise UnknownMemoryStore(
                                f"Prepare member memory {store_name!r} before building its context"
                            )
                    else:
                        ws_path = workspace_dir_for(workspace or _DEFAULT_KEY)
                        store = MemoryStore(workspace=ws_path)
                        store.init()
                        # Share the global VectorMemoryStore so all agents get
                        # semantic/episodic reads
                        default = _memory_stores.get(_DEFAULT_KEY)
                        if default is not None and default.vector_store is not None:
                            store.vector_store = default.vector_store
                    _memory_stores[key] = store
        return _memory_stores[key]

    @staticmethod
    def get_lessons_for(
        workspace: str | None = None, memory_store: str | None = None
    ) -> LessonStore:
        """Return a LessonStore for a workspace or a NAMED memory store.

        Same target resolution as :meth:`get_memory_for`. A named store's lessons
        live in its own directory, which ``LessonStore`` accepts because that
        directory is one it owns (see ``learn._is_owned_store_root``).

        Thread-safe — same double-checked locking as :meth:`get_memory_for`.
        """
        key, store_name = _target_key(workspace, memory_store)
        if key not in _lesson_stores:
            with _stores_lock:
                if key not in _lesson_stores:
                    if store_name:
                        from kiro_crew.memory_stores import ensure_memory_store_dir

                        base = ensure_memory_store_dir(store_name)
                    else:
                        base = workspace_dir_for(workspace or _DEFAULT_KEY)
                    _lesson_stores[key] = LessonStore(base_dir=base)
        return _lesson_stores[key]

    @staticmethod
    async def ensure_store(memory_store: str | None) -> "VectorMemoryStore | None":
        """Open or reuse a store off the event loop.

        V2 opens its declared SQLite database without provisioning, migration,
        index rebuilding or embedding reconciliation. V1 retains its legacy
        initialization and embedding alignment. The default store is prepared
        at gateway startup and returns None here. Unavailable member memory
        raises; the context caller may retain manual essentials with a diagnostic.
        """
        # The shared resolver normalizes global aliases and rejects unknown names.
        name = await asyncio.to_thread(_resolved_store_name, memory_store)
        if not name:
            return None
        from kiro_crew.embeddings import align_store_embedding_space

        try:
            store = _vector_stores.get(name)
            if store is None:
                store = await _build_store_vectors(name)
            if store is not None and store.algorithm_version != "v2":
                await asyncio.to_thread(align_store_embedding_space, store)
            return store
        except Exception:
            from kiro_crew.memory_stores import memory_store_version

            if await asyncio.to_thread(memory_store_version, name) == 2:
                raise
            logger.warning(
                "could not prepare the vector store for memory store %r; it will "
                "answer from markdown and keyword scoring only",
                name,
                exc_info=True,
            )
            return None

    def __init__(
        self,
        memory: MemoryStore | None = None,
        skills: SkillsLoader | None = None,
        hooks: HookManager | None = None,
        lessons: LessonStore | None = None,
        conversation_log: "ConversationLog | None" = None,
        channel_history: "ChannelHistory | None" = None,
        bot_name: str = "",
    ):
        self.memory = memory or MemoryStore()
        self.skills = skills or SkillsLoader()
        self.hooks = hooks or HookManager()
        self.lessons = lessons or LessonStore()
        self.conversation_log = conversation_log
        self.channel_history = channel_history
        # One reading of the delegation cap per session key; see
        # `_session_cap_figure`. One builder serves every session in the
        # gateway and is reached from a thread executor, so the memo's
        # read-evict-insert transaction is guarded.
        self._cap_figures: dict[str, str] = {}
        self._cap_figures_lock = threading.Lock()
        # The same, for the Computer Use spec-gate reading; see
        # `_session_computer_use_gate`.
        self._computer_use_gates: dict[str, bool] = {}
        self._computer_use_gates_lock = threading.Lock()
        # The same, for the browse-gate reading the Browser section follows; see
        # `context_assembly.agent_spec._resolve_agent_prompt`.
        self._browser_gates: dict[str, bool] = {}
        self._browser_gates_lock = threading.Lock()
        # Captured for the Jev decision point at `skills.select`. Production
        # reaches `build_message` only through `run_in_embed_pool`, a thread
        # executor with no running loop, so the point cannot obtain one where it
        # fires; every ContextBuilder construction site runs inside `async def`,
        # so this is where a loop exists to capture. Same shape and same reason
        # as `HistoryConsolidator._event_loop`. `None` outside a loop -- a sync
        # test, a script -- means the point simply does not run and trigger
        # matching's own selection ships, which is the seam's normal refusal
        # rather than an error.
        try:
            self._decisions_loop: "asyncio.AbstractEventLoop | None" = asyncio.get_running_loop()
        except RuntimeError:
            self._decisions_loop = None
        self.memory_mode_for_session: Callable[[str], Awaitable[str]] | None = None
        self.live_memory_mode_for_session: Callable[[str], str | None] | None = None
        self._session_memory_modes: dict[str, str] = {}
        # Lessons each session has already been shown, keyed by the fixed-size
        # digest of its session key: its session-start block plus every per-message
        # block since, so `memory.inject_lessons_per_turn` skips them. In memory
        # and bounded; see `_ShownLessons`.
        self._lessons_shown: OrderedDict[str, _ShownLessons] = OrderedDict()
        self._lessons_shown_lock = threading.Lock()
        # Per-session record of triggered-skill BODIES injected, so a later match
        # in the same provider window sends the cheap pointer line instead of the
        # whole body again. Recorded at build time and keyed by a fixed-size
        # digest of the session key (`_cap_memo_key`) -> {skill_key_digest:
        # body_sha256}, both keys digested so the caps govern bytes, not just
        # entries: `_SENT_SKILL_BODY_SESSIONS` bounds how many sessions are held,
        # `_SENT_SKILL_BODY_ENTRIES` bounds the
        # skills held per session, and both evict oldest-first. The hash lets an
        # edited skill re-inject. One builder serves every session and runs on a
        # thread executor, so the read-check-write is guarded, like `_cap_figures`.
        # The record fails SAFE in both directions: a rare turn that never lands
        # demotes the skill to its POINTER next turn (the agent still learns the
        # skill applies), not to silence; and the record drops for a session whose
        # provider window cannot hold the bodies — a fresh session and the first
        # turn after a compaction — tracked alongside the agent last seen so an
        # agent switch on the same key also resets.
        #
        # The record is written at build time so the dedup holds at every
        # `build_message` caller with no per-caller wiring. A build-time write
        # can name a body a turn never delivered (a provider error or cancel
        # after the prompt was built), so each build also stashes the prior
        # value of every entry it touched into `_sent_skill_bodies_undo`, keyed
        # by the same digest. A caller whose turn does not land calls
        # `rollback_skill_bodies(session_key)` at its turn `finally` — the same
        # seam that re-arms the post-compaction flag — to restore that prior
        # state, so the record never outlives the provider copy it names. Turns
        # on one session key are serialized by the per-session turn permit, so
        # the single last-build undo entry per session is not raced. A caller
        # that never rolls back keeps the fail-safe above (a pointer next turn,
        # never silence), so the rollback is a refinement, not a correctness
        # dependency.
        self._sent_skill_bodies: dict[str, dict[str, str]] = {}
        self._sent_skill_agents: dict[str, str | None] = {}
        # session_key_digest -> {skill_key_digest: prior body_sha256 | None},
        # the single most recent build's undo entry for that session; None means
        # the entry did not exist before this build (rollback removes it).
        self._sent_skill_bodies_undo: dict[str, dict[str, str | None]] = {}
        self._sent_skill_bodies_lock = threading.Lock()
        if bot_name:
            self._bot_name = bot_name
        else:
            cfg = KiroCrewConfig.load()
            provider = cfg.agent.provider
            # The joined spelling is the {bot_name} value the prompt
            # substitutes, not prose about the product: respelling it would
            # change what the model is told to answer to.
            self._bot_name = "KiroCrew" if is_claude_code(provider) else "Kiro"  # brand-ok
        # Register default memory in the workspace cache
        _memory_stores[_DEFAULT_KEY] = self.memory

    # ── Per-session delivery records (context_assembly/delivery_records.py) ──
    _remember_startup_lessons = _delivery_records._remember_startup_lessons
    _forget_shown_lessons = _delivery_records._forget_shown_lessons
    _live_shown_lessons = _delivery_records._live_shown_lessons
    _turn_lessons_block = _delivery_records._turn_lessons_block
    _dedup_triggered_bodies = _delivery_records._dedup_triggered_bodies
    rollback_skill_bodies = _delivery_records.rollback_skill_bodies
    commit_skill_bodies = _delivery_records.commit_skill_bodies
    # ── The agent spec and prompt (context_assembly/agent_spec.py) ──
    _substitute_bot_name = _agent_spec._substitute_bot_name
    _live_cap_figure = staticmethod(_agent_spec._live_cap_figure)
    _cap_memo_key = staticmethod(_agent_spec._cap_memo_key)
    _session_cap_figure = _agent_spec._session_cap_figure
    _session_reading = _agent_spec._session_reading
    _live_computer_use_gate = staticmethod(_agent_spec._live_computer_use_gate)
    _session_computer_use_gate = _agent_spec._session_computer_use_gate
    _resolve_prompt_templates = staticmethod(_agent_spec._resolve_prompt_templates)
    _load_agent_prompt = staticmethod(_agent_spec._load_agent_prompt)
    _resolve_agent_prompt = _agent_spec._resolve_agent_prompt
    # ── Member entry points (context_assembly/member_gates.py) ──
    _build_member_section = _member_gates._build_member_section
    _build_v2_essentials = _member_gates._build_v2_essentials
    # ── Session start and the turn message (session_start.py, turn_message.py) ──
    # Bound through names the facade imports: the memory-store seam gate reads an
    # attribute spelled like either method, outside a call, as a call site.
    build_session_context = _build_session_context
    build_message = _build_message
