"""Memory API handlers — preferences, projects, history, settings, semantic, episodic, embeddings, graph."""

from __future__ import annotations

import asyncio
import functools
import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiohttp import web

from kiro_crew import _facade_mirror, memory_schema
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.config.memory_sections import memory_writes_off_key
from kiro_crew.dashboard.body_numbers import body_int
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.dashboard.handlers.memory_graph import _build_memory_graph
from kiro_crew.dashboard.handlers.memory_services import _get_vector_store_async, _sel
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.embeddings import (
    make_sync_embed_fn,
)
from kiro_crew.executors import run_in_embed_pool, run_with_recall_deadline
from kiro_crew.history import is_incognito_transcript, transcript_privacy_mode
from kiro_crew.hooks import FileTooLargeError
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.memory import normalize_projects_document, projects_cap_overflow
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.security import redact_credentials, redact_exfiltration_urls, redact_local_paths
from kiro_crew.validation import clamp_query_offset
from kiro_crew.workflow_memory import WorkflowMemoryError

from ._shared import (
    _get_memory,
    _is_restricted_session,
    _read_session_key,
    _redact_memory_field,
    markdown_memory_for_store,
    read_bounded_json,
    require_owner_dashboard_request,
    resolve_lesson_memory_store,
    resolve_requested_memory_store,
    resolve_session_memory_mode,
    vector_memory_for_store,
)
from .cron import _recognize_session

logger = logging.getLogger("kiro_crew.dashboard.handlers.memory")


def memory_recall_deadline(
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Bound recall even when the HTTP server does not cancel disconnected clients."""

    @functools.wraps(handler)
    async def bounded(request: web.Request) -> web.Response:
        try:
            return await run_with_recall_deadline(handler(request))
        except asyncio.TimeoutError:
            return web.json_response(
                {"error": "Memory recall timed out.", "code": "memory_recall_timeout"},
                status=504,
            )

    return bounded


# Per-endpoint write serialization for the offloaded markdown saves below.
# asyncio.to_thread hands each PUT to an executor worker, and workers can
# acquire the store's file lock OUT OF REQUEST ORDER — a rapid pair of saves
# could commit the older content last. These locks make that ordering explicit
# while keeping the blocking I/O off the loop.
#
# ONE lock per endpoint, deliberately NOT one per (endpoint, store), even though
# a ``?store=`` PUT addresses a different file: the lock exists for commit
# ORDER within a single document, and two stores write two different documents,
# so a silo's save waiting behind the global store's costs it one small atomic
# write of latency and nothing a caller can observe. A per-store lock map is the
# strictly worse trade — it needs its own lock to be built safely (the same trade
# ``_shared._store_tier_lock`` makes) in exchange for concurrency on a path whose
# whole cost is a single file replace.
_prefs_write_lock = LoopBoundLock()
_projects_write_lock = LoopBoundLock()
_profile_write_lock = LoopBoundLock()
_history_write_lock = LoopBoundLock()


class _MemoryDocumentRedacted(Exception):
    """The current whole-document source contains hidden sensitive content."""


def _memory_document_response(content: str) -> web.Response:
    """Return display-safe document text and whether the response was transformed."""
    safe = _redact_memory_field(content)
    assert isinstance(safe, str)
    return web.json_response({"content": safe, "content_redacted": safe != content})


def _require_editable_memory_document(content: str) -> None:
    """Refuse whole-document replacement when its source cannot be shown exactly."""
    if _redact_memory_field(content) != content:
        raise _MemoryDocumentRedacted


def _memory_document_redacted_response() -> web.Response:
    return web.json_response(
        {
            "error": "sensitive values are hidden; this memory document is read-only",
            "code": "memory_document_redacted",
        },
        status=409,
    )


def _memory_document_changed_response() -> web.Response:
    return web.json_response(
        {
            "error": "memory changed while this document was being saved",
            "code": "memory_document_changed",
        },
        status=409,
    )


def _memory_document_undecodable_response(name: str) -> web.Response:
    """Refuse a document whose bytes are not UTF-8; the file is left as it is."""
    return web.json_response(
        {
            "error": f"{name} is not valid UTF-8 and cannot be shown or saved",
            "code": "memory_document_undecodable",
            "file": name,
        },
        status=409,
    )


async def _memory_write_gate(
    state: DashboardState, request: web.Request, operation: str
) -> web.Response | None:
    """The memory-mutation authorization cascade, or ``None`` when the call may proceed.

    The single implementation for every durable-memory write in this module. Order is
    the control: the session-recognition probe first, because the restricted-mode
    check answers False for an UNKNOWN key, so a route carrying only that half admits
    a forged or never-established ``X-Session-Key``. Both refusals are SEL-audited and
    both carry a machine-readable ``code``, since backend strings have no i18n catalog
    path.

    The key comes from ``_read_session_key`` rather than the raw header so both halves
    compare the same canonical form: ``_is_restricted_session`` normalizes, so reading
    the header directly here would let the two halves disagree on trailing whitespace
    and would record an un-normalized ``caller`` in the audit trail.

    ``blocks_persisted_mode=is_incognito_transcript`` because every caller mutates
    durable memory: writes block every private persisted mode.
    """
    # Owner first: every caller of this gate writes the owner's durable memory,
    # and the store resolution below only asks for the owner when ``?store=``
    # is present.
    owner_denied = await require_owner_dashboard_request(request, operation)
    if owner_denied is not None:
        return owner_denied
    if operation != "memory.consolidate":
        _, refusal = await resolve_requested_memory_store(request, state, operation)
        if refusal is not None:
            return refusal
    sk = _read_session_key(request)
    refusal = await _recognize_session(
        state,
        sk,
        operation,
        blocks_persisted_mode=is_incognito_transcript,
    )
    if refusal is not None:
        return refusal
    if _is_restricted_session(state, request):
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
        )
        return web.json_response(
            {
                "error": "Memory writes are not allowed in this session mode.",
                "code": "restricted_session",
            },
            status=403,
        )
    return None


def _store_unavailable_response(store: str, error: Exception | None = None) -> web.Response:
    """A redacted, named 503 for unavailable memory, including startup recovery."""
    message = f"the vector store for memory store {store!r} is unavailable"
    if error is not None:
        message = f"memory store {store or 'default'!r} is unavailable: {error}"
    message, _ = redact_local_paths(message)
    return web.json_response(
        {
            "error": _redact_memory_field(message),
            "code": "store_unavailable",
        },
        status=503,
    )


def _private_profile_unavailable_response(store: str, error: Exception) -> web.Response:
    """Keep identity/filesystem diagnostics in the log, not the profile response."""
    logger.warning(
        "Private memory profile write refused: %s",
        redact_log_via_context(f"store={store!r}: {type(error).__name__}: {error}"),
    )
    return web.json_response(
        {
            "error": "Private memory profile is unavailable. Check the gateway log before retrying.",
            "code": "store_unavailable",
        },
        status=503,
    )


async def _vector_tier_for_request(
    request: web.Request,
    state: DashboardState,
    operation: str,
) -> tuple[Any, str, web.Response | None]:
    """``(vector tier, store name, refusal)`` for the store this request addresses.

    The two steps every store-scoped vector route takes, in the order it needs
    them: resolve the store through the shared owner-gated seam, then stand up
    that store's tier. The third element is a response to return AS-IS — an owner
    denial, an undeclared ``?store=``, or the 503 above — and a caller that reads
    the first element without checking it answers from a store it was refused.

    An ABSENT ``?store=`` resolves the GLOBAL store, and ``""`` routes straight to
    :func:`_get_vector_store_async` — so a request that names no store addresses
    the same object an unscoped route resolves, down to the cached instance. That
    equivalence is the property store scoping stands on: the operator's own memory
    does not move.

    Session headers cannot select a silo: their identity is unverified on TCP.
    Every content route therefore uses the same global-store default.
    """
    store, denial = await resolve_requested_memory_store(request, state, operation)
    if denial is not None:
        return None, "", denial
    try:
        vector = await vector_memory_for_store(state, store)
    except (UnknownMemoryStore, OSError) as exc:
        return None, store, _store_unavailable_response(store, exc)
    if vector is None:
        return None, store, _store_unavailable_response(store)
    return vector, store, None


def _validate_private_profile_update(
    state: DashboardState, store: str, filename: str, content: str
) -> None:
    """Validate the candidate anchors before either profile file is replaced."""
    from datetime import datetime

    from kiro_crew.config.loader import resolve_agent_bindings
    from kiro_crew.member_essential_context import ESSENTIAL_MAX_CHARS, MemberEssentialContextError

    if len(content) > ESSENTIAL_MAX_CHARS:
        raise MemberEssentialContextError(
            f"{filename} exceeds the {ESSENTIAL_MAX_CHARS}-character essential context budget"
        )

    builder = state.context_builder
    if builder is None:
        raise MemberEssentialContextError("The member context cannot be validated right now")
    if filename == "projects.md":
        content = normalize_projects_document(content, today=datetime.now().strftime("%Y-%m-%d"))
    cfg = KiroCrewConfig.load()
    record = cfg.memory_stores.get(store)
    if record is None:
        raise UnknownMemoryStore(f"Memory store {store!r} is not configured")
    owner = record.owner_member
    bindings = resolve_agent_bindings(cfg, owner)
    builder._build_v2_essentials(
        store,
        member=record.owner_member_id,
        project=str(bindings.workspace_dir),
        profile_overrides={filename: content},
    )


async def api_memory_preferences(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/preferences — the store named by ``?store=``, else the
    GLOBAL store."""
    state: DashboardState = request.app["state"]
    # Resolved before the method branch because the GET reads the store the PUT
    # writes. For a store-bearing PUT that puts the parameter's owner gate ahead
    # of the write gate below, which is the precedence it should have: naming
    # another store is the OPERATOR's question, and a caller that may not ask it
    # should not have its body read either. With no ``?store=`` the resolver cannot
    # refuse at all, so the write gate is still the first thing such a PUT meets.
    operation = "preferences.write" if request.method == "PUT" else "preferences.read"
    store, denial = await resolve_requested_memory_store(request, state, operation)
    if denial is not None:
        return denial
    try:
        mem = await markdown_memory_for_store(state, store)
    except (UnknownMemoryStore, OSError) as exc:
        return _store_unavailable_response(store, exc)
    if request.method == "PUT":
        # The PUT overwrites the whole preferences document, so it is a durable
        # memory write and takes the same gate as the semantic write route. The
        # GET below is a read path and is deliberately left alone.
        gate = await _memory_write_gate(state, request, "preferences.write")
        if gate is not None:
            return gate
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        content = body.get("content", "")
        if not isinstance(content, str):
            return web.json_response(
                {"error": "content must be a string", "code": "invalid_memory_content"}, status=400
            )
        # Offloaded to a worker thread: write_preferences does synchronous
        # atomic file I/O plus an FTS index update, and this handler runs on
        # the gateway event loop — inline, a slow filesystem stalls every
        # other gateway task. asyncio.to_thread (not the embed pool): this
        # write does no embedding, and the embed bulkhead's workers can all
        # be parked behind a hung embedding endpoint, which would make a
        # Memory-tab Save wait on unrelated embed traffic. The endpoint lock
        # keeps rapid successive saves committing in request order (workers
        # can otherwise acquire the store's file lock out of order — see
        # module top).
        private = getattr(mem, "_memory_version", 1) == 2
        write_lock = _profile_write_lock if private else _prefs_write_lock
        async with write_lock:
            if private:
                try:

                    def validate_private_preferences(normalized: str) -> None:
                        current = mem.read_preferences()
                        _require_editable_memory_document(current)
                        _validate_private_profile_update(state, store, "preferences.md", normalized)

                    await asyncio.to_thread(
                        mem.write_private_profile_validated,
                        "preferences.md",
                        content,
                        validate_private_preferences,
                    )
                except _MemoryDocumentRedacted:
                    return _memory_document_redacted_response()
                except (UnknownMemoryStore, OSError) as exc:
                    return _private_profile_unavailable_response(store, exc)
                except ValueError as exc:
                    return web.json_response(
                        {
                            "error": _redact_memory_field(str(exc)),
                            "code": "essential_context_invalid",
                        },
                        status=400,
                    )
            else:
                try:

                    def write_preferences() -> bool:
                        current = mem.read_preferences()
                        _require_editable_memory_document(current)
                        return mem.write_preferences(content, expected_baseline=current)

                    wrote = await asyncio.to_thread(write_preferences)
                    if not wrote:
                        return _memory_document_changed_response()
                except _MemoryDocumentRedacted:
                    return _memory_document_redacted_response()
                except UnicodeDecodeError:
                    return _memory_document_undecodable_response("preferences.md")
                except (UnknownMemoryStore, OSError) as exc:
                    return _store_unavailable_response(store, exc)
        return web.json_response({"ok": True})
    try:
        content = await asyncio.to_thread(mem.read_preferences)
    except UnicodeDecodeError:
        return _memory_document_undecodable_response("preferences.md")
    except (UnknownMemoryStore, OSError) as exc:
        return _store_unavailable_response(store, exc)
    return _memory_document_response(content)


async def api_memory_projects(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/projects — the store named by ``?store=``, else the
    GLOBAL store."""
    state: DashboardState = request.app["state"]
    # Resolved ahead of the method branch for the same reason as the preferences
    # route above.
    operation = "projects.write" if request.method == "PUT" else "projects.read"
    store, denial = await resolve_requested_memory_store(request, state, operation)
    if denial is not None:
        return denial
    try:
        mem = await markdown_memory_for_store(state, store)
    except (UnknownMemoryStore, OSError) as exc:
        return _store_unavailable_response(store, exc)
    if request.method == "PUT":
        # Gated like the preferences PUT above: a whole-document overwrite of
        # durable memory. The GET is a read path and stays ungated.
        gate = await _memory_write_gate(state, request, "projects.write")
        if gate is not None:
            return gate
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        content = body.get("content", "")
        if not isinstance(content, str):
            return web.json_response(
                {"error": "content must be a string", "code": "invalid_memory_content"}, status=400
            )
        # Offloaded for the same reason as api_memory_preferences above.
        private = getattr(mem, "_memory_version", 1) == 2
        write_lock = _profile_write_lock if private else _projects_write_lock
        async with write_lock:
            if private:
                try:

                    def validate_private_projects(normalized: str) -> None:
                        current = mem.read_projects()
                        _require_editable_memory_document(current)
                        _validate_private_profile_update(state, store, "projects.md", normalized)

                    await asyncio.to_thread(
                        mem.write_private_profile_validated,
                        "projects.md",
                        content,
                        validate_private_projects,
                    )
                except _MemoryDocumentRedacted:
                    return _memory_document_redacted_response()
                except (UnknownMemoryStore, OSError) as exc:
                    return _private_profile_unavailable_response(store, exc)
                except ValueError as exc:
                    return web.json_response(
                        {
                            "error": _redact_memory_field(str(exc)),
                            "code": "essential_context_invalid",
                        },
                        status=400,
                    )
            else:
                try:

                    def write_projects() -> bool:
                        current = mem.read_projects()
                        _require_editable_memory_document(current)
                        return mem.write_projects(content, expected_baseline=current)

                    wrote = await asyncio.to_thread(write_projects)
                    if not wrote:
                        return _memory_document_changed_response()
                except _MemoryDocumentRedacted:
                    return _memory_document_redacted_response()
                except UnicodeDecodeError:
                    return _memory_document_undecodable_response("projects.md")
                except (UnknownMemoryStore, OSError) as exc:
                    return _store_unavailable_response(store, exc)
                from datetime import datetime

                today = datetime.now().strftime("%Y-%m-%d")
                overflow = projects_cap_overflow(normalize_projects_document(content, today=today))
                if overflow:
                    # Saved whole, but session startup injects only the head: say so.
                    return web.json_response(
                        {
                            "ok": True,
                            "warning": "Saved, but sessions only load the start of Active "
                            f"Projects: {overflow} chars past the limit are cut off.",
                            "overflow_chars": overflow,
                        }
                    )
        return web.json_response({"ok": True})
    try:
        content = await asyncio.to_thread(mem.read_projects)
    except UnicodeDecodeError:
        return _memory_document_undecodable_response("projects.md")
    except (UnknownMemoryStore, OSError) as exc:
        return _store_unavailable_response(store, exc)
    return _memory_document_response(content)


async def api_memory_history(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/history — today's V2 document or V1 recent summaries,
    for the store named by ``?store=`` or the GLOBAL store."""
    state: DashboardState = request.app["state"]
    # Resolved ahead of the method branch for the same reason as the preferences
    # route above. The dated file the PUT writes is the RESOLVED store's, so a
    # silo's daily summary never lands in the global store's history tree.
    operation = "history.write" if request.method == "PUT" else "history.read"
    store, denial = await resolve_requested_memory_store(request, state, operation)
    if denial is not None:
        return denial
    try:
        mem = await markdown_memory_for_store(state, store)
    except (UnknownMemoryStore, OSError) as exc:
        return _store_unavailable_response(store, exc)
    if request.method == "PUT":
        # Gated like the two PUTs above: it overwrites today's summary file, a
        # durable memory write. The GET is a read path and stays ungated.
        gate = await _memory_write_gate(state, request, "history.write")
        if gate is not None:
            return gate
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        content = body.get("content", "")
        if not isinstance(content, str):
            return web.json_response(
                {"error": "content must be a string", "code": "invalid_memory_content"}, status=400
            )

        # Write to today's history file. Offloaded like the two handlers
        # above (synchronous file I/O on the event loop stalls every other
        # gateway task), and routed through the store's atomic writer:
        # write_text would follow a planted symlink at the dated name and
        # tear under concurrent PUTs; the atomic replace commits whole
        # versions and never traverses a link at the temp path.
        def write_history() -> bool:
            current = mem.read_editable_history()
            _require_editable_memory_document(current)
            return mem.write_today_history(
                content,
                expected_baseline=current,
                validate_current=_require_editable_memory_document,
            )

        async with _history_write_lock:
            try:
                wrote = await asyncio.to_thread(write_history)
                if not wrote:
                    return _memory_document_changed_response()
            except _MemoryDocumentRedacted:
                return _memory_document_redacted_response()
            except (UnknownMemoryStore, OSError, UnicodeError, FileTooLargeError) as exc:
                return _store_unavailable_response(store, exc)
        return web.json_response({"ok": True})
    try:
        content = await asyncio.to_thread(mem.read_editable_history)
    except (UnknownMemoryStore, OSError, FileTooLargeError) as exc:
        return _store_unavailable_response(store, exc)
    return _memory_document_response(content)


async def api_memory_settings(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/settings — memory consolidation config."""
    cfg = KiroCrewConfig.load()
    if request.method == "PUT":
        # The body may carry `migrated`, which is the same install-wide flag
        # /api/memory/migrate flips, so this PUT is a durable memory write and takes
        # the same gate. Gated before the body is read, so a refused request costs
        # nothing. The GET below is a read path and stays outside.
        gate = await _memory_write_gate(request.app["state"], request, "settings.write")
        if gate is not None:
            return gate
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        # Read existing config, update memory section only
        # Validated BEFORE the transaction: none of it reads the config, and a
        # 400 should not have taken the lock or occupied a worker.
        updates: dict[str, Any] = {}
        if "history_idle_hours" in body:
            try:
                updates["history_idle_hours"] = max(0.5, float(body["history_idle_hours"]))
            except (ValueError, TypeError):
                return web.json_response(
                    {"error": "history_idle_hours must be numeric"}, status=400
                )
        if "history_max_days" in body:
            try:
                updates["history_max_days"] = max(7, body_int(body["history_max_days"]))
            except (ValueError, TypeError):
                return web.json_response(
                    {"error": "history_max_days must be an integer"}, status=400
                )
        if "migrated" in body:
            updates["migrated"] = bool(body["migrated"])

        def _apply(data: dict) -> dict | None:
            # Nothing recognised in the body: skip the write rather than reach
            # into the memory section at all. A config whose `memory` is not an
            # object (`{"memory": []}` -- the top level is all
            # read_config_for_update validates) would otherwise raise
            # AttributeError from `.update` and 500 a request that answered a
            # successful no-op before. `None` tells update_config_locked there
            # is no change to persist.
            if not updates:
                return None
            data.setdefault("memory", {}).update(updates)
            return data

        try:
            await run_config_write(update_config_locked, config_path(), mutate=_apply)
        except ConfigReadError:
            # Fail closed: writing back a {} baseline would drop every other setting.
            logger.exception("Refusing to save memory settings: config unreadable")
            return web.json_response(
                {"error": "failed to read config file", "code": "config_unreadable"},
                status=500,
            )
        # Apply to running consolidator. The config watcher does this for every
        # writer, but this route answers only after the value is in force, so it
        # pushes directly rather than making the caller wait out a poll tick.
        state: DashboardState = request.app["state"]
        if state.consolidator:
            state.consolidator.reconfigure(await asyncio.to_thread(KiroCrewConfig.load))
        return web.json_response({"ok": True})
    return web.json_response(
        {
            "history_idle_hours": cfg.memory.history_idle_hours,
            "history_max_days": cfg.memory.history_max_days,
            "migrated": cfg.memory.migrated,
        }
    )


async def api_memory_semantic(request: web.Request) -> web.Response:
    """GET /api/memory/semantic — list semantic memory entries (paginated).

    Server-capped via ``min(limit, 1000)`` + ``offset`` so a single GET can't
    serialize the whole (continuously-written) semantic table (CWE-770). The
    optional ``q`` filters keys and decoded values before pagination, including
    memories outside the currently visible page.

    ``?store=`` addresses a store other than the GLOBAL one, owner-gated by the
    shared resolver.
    """
    state: DashboardState = request.app["state"]
    store, _store_name, denial = await _vector_tier_for_request(request, state, "semantic.read")
    if denial is not None:
        return denial
    query = request.query.get("q", "")
    from kiro_crew.vector_memory import MAX_MEMORY_SEARCH_QUERY

    if len(query) > MAX_MEMORY_SEARCH_QUERY:
        return web.json_response(
            {
                "error": "Memory search query must be at most 2000 characters",
                "code": "invalid_memory_query",
            },
            status=400,
        )
    try:
        limit = min(int(request.query.get("limit", "1000")), 1000)
        # Clamped before the store binds it: see ``clamp_query_offset``.
        offset = clamp_query_offset(int(request.query.get("offset", "0")))
    except (ValueError, TypeError):
        return web.json_response({"error": "limit/offset must be integers"}, status=400)
    entries = []
    # Offload: the fetch serializes on the store's _db_lock, and a
    # worker holding it (e.g. backfill's locked FAISS rebuild) would otherwise
    # block the gateway event loop here.
    search = {"q": query} if query.strip() else {}
    rows = await asyncio.to_thread(store.get_all_semantic, limit=limit, offset=offset, **search)
    for e in rows:
        d = {k: v for k, v in dict(e).items() if not isinstance(v, (bytes, memoryview))}
        entries.append(_redact_memory_field(d))
    return web.json_response({"entries": entries})


async def api_memory_semantic_write(request: web.Request) -> web.Response:
    """PUT /api/memory/semantic — create/update a semantic entry in the store named
    by ``?store=``, else the GLOBAL store."""
    state: DashboardState = request.app["state"]
    # Session-recognition gate (shared with the lessons routes): the
    # restricted-mode check below returns False for an unknown key, so without
    # this gate a forged or never-established X-Session-Key could write
    # semantic memory that create-style routes refuse. Writes block
    # every private persisted mode, mirroring ``api_lessons_create``.
    gate = await _memory_write_gate(state, request, "semantic.write")
    if gate is not None:
        return gate
    # Resolution sits BEHIND the write gate, where the unscoped resolution sits.
    # Both orders refuse a caller that fails either gate, and only this one leaves
    # a request that names no store meeting its refusals in the original order.
    store, _store_name, denial = await _vector_tier_for_request(request, state, "semantic.write")
    if denial is not None:
        return denial
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    key = body.get("key", "")
    value = body.get("value")
    confidence = (
        float(body.get("confidence", 1.0))
        if isinstance(body.get("confidence"), (int, float))
        else 1.0
    )
    source = body.get("source", "user_explicit")
    if not key or value is None:
        return web.json_response({"error": "key and value required"}, status=400)
    # set_semantic may embed via blocking in-process model inference (and a
    # ~1s model load on first call); offload so it can't stall the event loop.
    err = await asyncio.to_thread(store.set_semantic, key, value, confidence, source)
    if err is not None:
        code, message = err
        # Imported here, not at module scope: ``vector_memory`` pulls
        # snowballstemmer plus the optional numpy/faiss imports (measured 175ms
        # and ~200 modules) and this enum is the module's ONLY use of it, on one
        # error branch. The enum lives in ``vector_memory_runtime/write_outcomes.py``
        # and its public path is ``vector_memory``; deferring the import keeps the
        # cost off the import path.
        from kiro_crew.vector_memory import SemanticRejectCode

        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk,
            operation="semantic.write",
            outcome="rejected",
            source="dashboard",
            resources=f"{code.value}:{key}",
        )
        status = 409 if code == SemanticRejectCode.CONFLICT else 422
        msg, _ = redact_exfiltration_urls(message)
        msg, _ = redact_credentials(msg)
        return web.json_response({"error": msg}, status=status)
    sk = request.headers.get("X-Session-Key", "")
    _sel().log_api_access(
        caller=sk,
        operation="semantic.write",
        outcome="success",
        source="dashboard",
        resources=key,
    )
    return web.json_response({"ok": True})


_FORGET_BODY_FIELDS = frozenset({"expect_value_json", "superseded_by"})


async def api_memory_semantic_delete(request: web.Request) -> web.Response:
    """DELETE /api/memory/semantic/{key} — tombstone a semantic entry in the store
    named by ``?store=``, else the GLOBAL store.

    An optional JSON body carries two fields, both strings, for the CLI's
    ``kirocrew memory forget``. ``expect_value_json`` makes it a compare-and-delete:
    a row whose stored body differs from that value is kept and answers 409
    ``value_changed``. ``superseded_by`` names the live key that replaces this one;
    it must differ from *key* and be live, or the call answers 400/409. The
    dashboard sends no body, and a bodiless call behaves exactly as before.
    """
    state: DashboardState = request.app["state"]
    # The shared write gate rejects unknown callers before checking session
    # mode: the restricted-mode check alone returns False for an unknown key.
    # Known incognito and temporary sessions cannot persist memory changes.
    gate = await _memory_write_gate(state, request, "semantic.delete")
    if gate is not None:
        return gate
    # Behind the write gate, as on the write route above: the default path's
    # refusal order is what must not change.
    store, _store_name, denial = await _vector_tier_for_request(request, state, "semantic.delete")
    if denial is not None:
        return denial
    key = request.match_info["key"]
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    if not set(body) <= _FORGET_BODY_FIELDS or not all(
        isinstance(value, str) for value in body.values()
    ):
        return web.json_response(
            {
                "error": "body may hold only string expect_value_json and superseded_by",
                "code": "invalid_forget_body",
            },
            status=400,
        )
    expect = body.get("expect_value_json")
    winner = body.get("superseded_by")
    if winner is not None:
        if winner == key:
            return web.json_response(
                {"error": "superseded_by names the key itself", "code": "superseded_by_self"},
                status=400,
            )
        if await asyncio.to_thread(store.get_semantic, winner) is None:
            return web.json_response(
                {"error": "superseded_by is not a live key", "code": "superseded_by_absent"},
                status=409,
            )
    options: dict[str, str] = {}
    if expect is not None:
        options["expect_value_json"] = expect
    if winner is not None:
        options["superseded_by"] = winner
    try:
        # Offload: acquires _db_lock internally — see api_memory_semantic.
        ok = await asyncio.to_thread(store.delete_semantic, key, source="user_explicit", **options)
    except ValueError:
        # delete_semantic refuses a multi-line or over-long superseded_by.
        return web.json_response(
            {"error": "superseded_by must be a short key", "code": "invalid_forget_body"},
            status=400,
        )
    if not ok:
        if expect is not None and await asyncio.to_thread(store.get_semantic, key) is not None:
            return web.json_response(
                {"error": "value changed", "code": "value_changed"}, status=409
            )
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})


async def api_memory_carve(request: web.Request) -> web.Response:
    """GET /api/memory/carve — count or list a store's rows by carve facet.

    Read-only. Query parameters are the five facet names, ``kind``, ``count_by``,
    ``limit`` and ``offset``; ``count_by`` switches the response from ``entries``
    to ``counts``. A facet given as an empty value (``?crew=``) selects the rows
    no writer attributed, which is a different question from omitting it.

    **``?store=`` requires the dashboard owner's identity.** Without it, this
    route addresses the global store, ignoring ``X-Session-Key`` like every
    sibling content route. That header is unverified on TCP, so a recorded
    binding alone does not prove that its session belongs to this caller.
    The operator can also inspect a specific store with
    ``kirocrew memory carve --store``, from the host, where they already hold
    every silo's bytes.

    A request naming no store addresses the global store, which is on the
    v1 lineage and therefore refuses — the same refusal, with the same ``code``,
    that the CLI prints. Every non-2xx body carries a machine-readable ``code``.

    The filter mapping is keyed from ``memory_schema.FACET_NAMES`` and the query is
    consulted for MEMBERSHIP, so no caller string reaches the builder as a column
    name. An unrecognized query key is therefore ignored rather than refused, which
    it has to be: a request legitimately carries keys that are not filters
    (``?token=`` among them), and 400-ing on those would break query-token auth.
    ``count_by`` and ``kind`` are the two parameters whose VALUE lands in a name or
    a closed set, so those are validated and answer 400.
    """
    state: DashboardState = request.app["state"]
    # ``silo`` is echoed in both response shapes below, so it must be the store
    # actually read — the shared resolver's answer — and never the requested name.
    store, silo, denial = await _vector_tier_for_request(request, state, "carve.read")
    if denial is not None:
        return denial
    filters = {
        name: request.query[name] for name in memory_schema.FACET_NAMES if name in request.query
    }
    kind = request.query.get("kind", "")
    group_by = request.query.get("count_by", "")
    try:
        limit = int(request.query.get("limit", str(memory_schema.DEFAULT_FACET_PAGE)))
        # Not clamped here: ``memory_schema.facet_page_query`` bounds both the
        # page and the offset for this route and the CLI verb alike.
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        return web.json_response(
            {"error": "limit/offset must be integers", "code": "invalid_pagination"}, status=400
        )
    try:
        # Offload: both methods serialize on the store's _db_lock, and a worker
        # holding it would otherwise block the gateway event loop here.
        if group_by:
            counts = await asyncio.to_thread(store.count_by_facet, group_by, filters, kind=kind)
            return web.json_response({"store": silo, "counts": counts})
        rows = await asyncio.to_thread(
            store.list_by_facets, filters, kind=kind, limit=limit, offset=offset
        )
    except memory_schema.FacetsUnsupported as exc:
        return web.json_response({"error": str(exc), "code": "facets_unsupported"}, status=409)
    except memory_schema.UnknownFacet as exc:
        return web.json_response({"error": str(exc), "code": "unknown_facet"}, status=400)
    return web.json_response(
        {"store": silo, "entries": [_redact_memory_field(dict(row)) for row in rows]}
    )


async def api_memory_events(request: web.Request) -> web.Response:
    """GET /api/memory/events — paginated audit trail for the store named by
    ``?store=``, else the GLOBAL store."""
    state: DashboardState = request.app["state"]
    store, _store_name, denial = await _vector_tier_for_request(request, state, "events.read")
    if denial is not None:
        return denial
    try:
        limit = min(int(request.query.get("limit", "50")), 200)
        offset = clamp_query_offset(int(request.query.get("offset", "0")))
    except (ValueError, TypeError):
        return web.json_response({"error": "limit/offset must be integers"}, status=400)
    # Offload: serializes on _db_lock — see api_memory_semantic.
    events = await asyncio.to_thread(store.get_events, limit=limit, offset=offset)
    return web.json_response({"events": _redact_memory_field(events)})


_migrate_lock = LoopBoundLock()


async def _set_migrated(value: bool) -> None:
    """Set memory.migrated in config.json.

    Routed through the repo's designated config-write path: ``run_config_write``
    holds the loop-side asyncio lock while ``update_config_locked`` performs the
    read-modify-write on a worker under the sidecar advisory flock, so this
    serializes against BOTH writer generations -- the dashboard's other handlers
    and the CLI / boot-refresh / other-process writers -- and none of it runs on
    the gateway loop.

    If an existing config.json can't be parsed, do NOT write -- overwriting it
    with only the migration flag would destroy every other recoverable setting.
    Boot-time auto-migration calls this on every startup while migrated is false,
    so a malformed config must fail closed (skip the flag, keep the file) and let
    a later boot retry once the user has repaired it, rather than silently
    clobbering their config. ``update_config_locked`` defaults to
    ``on_corrupt="fail"``, which is exactly that contract.
    """

    def _apply(data: dict) -> dict:
        data.setdefault("memory", {})["migrated"] = value
        return data

    try:
        await run_config_write(update_config_locked, config_path(), mutate=_apply)
    except ConfigReadError:
        logger.warning(
            "config.json is unparseable; skipping memory.migrated write to "
            "avoid clobbering other settings — will retry next boot"
        )


async def api_memory_episodic_search(request: web.Request) -> web.Response:
    """GET /api/memory/episodic/search?q=...&tags=t1,t2 — search episodic memories in
    the store named by ``?store=``, else the GLOBAL store."""
    state: DashboardState = request.app["state"]
    store, _store_name, denial = await _vector_tier_for_request(request, state, "episodic.read")
    if denial is not None:
        return denial
    query = request.query.get("q", "")[:500]
    try:
        limit = min(int(request.query.get("limit", "20")), 50)
    except (ValueError, TypeError):
        limit = 20
    tag_filter = [t.strip() for t in request.query.get("tags", "").split(",") if t.strip()] or None
    # _try_embed runs blocking in-process model inference (and a ~1s model
    # load on first call); offload to keep the dashboard event loop responsive.
    emb = await asyncio.to_thread(store._try_embed, query) if store.embed_fn and query else None
    results = []
    # Offload: search_episodic serializes on _db_lock — see
    # api_memory_semantic.
    hits = await asyncio.to_thread(
        store.search_episodic,
        query_embedding=emb,
        query_text=query,
        limit=limit,
        tag_filter=tag_filter,
    )
    for e in hits:
        d = {k: v for k, v in dict(e).items() if not isinstance(v, (bytes, memoryview))}
        results.append(_redact_memory_field(d))
    return web.json_response({"results": results})


async def api_memory_episodic_list(request: web.Request) -> web.Response:
    """GET /api/memory/episodic?tags=t1,t2 — paginated list of episodic memories from
    the store named by ``?store=``, else the GLOBAL store."""
    state: DashboardState = request.app["state"]
    store, _store_name, denial = await _vector_tier_for_request(request, state, "episodic.read")
    if denial is not None:
        return denial
    query = request.query.get("q", "")
    from kiro_crew.vector_memory import MAX_MEMORY_SEARCH_QUERY

    if len(query) > MAX_MEMORY_SEARCH_QUERY:
        return web.json_response(
            {
                "error": "Memory search query must be at most 2000 characters",
                "code": "invalid_memory_query",
            },
            status=400,
        )
    try:
        limit = min(int(request.query.get("limit", "50")), 100)
        offset = clamp_query_offset(int(request.query.get("offset", "0")))
    except (ValueError, TypeError):
        return web.json_response({"error": "limit/offset must be integers"}, status=400)
    tag_filter = [t.strip() for t in request.query.get("tags", "").split(",") if t.strip()] or None
    # Offload: serializes on _db_lock; see api_memory_semantic.
    search = {"q": query} if query.strip() else {}
    rows = await asyncio.to_thread(
        store.get_episodic_list, limit=limit, offset=offset, tag_filter=tag_filter, **search
    )
    entries = [_redact_memory_field(dict(e)) for e in rows]
    return web.json_response({"entries": entries})


async def api_memory_episodic_delete(request: web.Request) -> web.Response:
    """DELETE /api/memory/episodic/{id} — tombstone an episodic memory in the store
    named by ``?store=``, else the GLOBAL store."""
    state: DashboardState = request.app["state"]
    # Tombstoning an episodic row is a durable memory write, so it takes the same
    # gate as its semantic siblings.
    gate = await _memory_write_gate(state, request, "episodic.delete")
    if gate is not None:
        return gate
    # Behind the write gate, as on the semantic routes above.
    store, _store_name, denial = await _vector_tier_for_request(request, state, "episodic.delete")
    if denial is not None:
        return denial
    mem_id = request.match_info["id"]
    # Offload: acquires _db_lock internally — see api_memory_semantic.
    ok = await asyncio.to_thread(store.delete_episodic, mem_id)
    if not ok:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})


async def api_memory_stats(request: web.Request) -> web.Response:
    """GET /api/memory/stats — statistics for the store named by ``?store=``, else the
    GLOBAL store.

    Only the counts are per store. The three fields appended below stay
    INSTALL-wide: the ``memory.*`` embedding provider, the migration flag and the
    legacy-markdown probe describe one machine's configuration rather than one
    store's rows, so a ``?store=`` does not change them.
    """
    state: DashboardState = request.app["state"]
    store, _store_name, denial = await _vector_tier_for_request(request, state, "stats.read")
    if denial is not None:
        return denial
    # Offload: serializes on _db_lock — see api_memory_semantic.
    stats = await asyncio.to_thread(store.memory_stats)
    # Add embedding status. The shadowing import is deliberate: resolving
    # ``KiroCrewConfig`` at call time is what lets a test substitute it on the
    # source module.
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811

    cfg = KiroCrewConfig.load()
    stats["embedding_provider"] = cfg.memory.embedding_provider
    stats["migrated"] = cfg.memory.migrated
    # Legacy markdown presence (diagnostics; migration is automatic at boot).
    # Offload: it reads two markdown files, globs history/ and stats lessons.jsonl.
    from kiro_crew.memory import legacy_memory_present  # noqa: F811

    stats["has_legacy_memory"] = await asyncio.to_thread(legacy_memory_present)
    return web.json_response(stats)


async def api_memory_migrate(request: web.Request) -> web.Response:
    """POST /api/memory/migrate — migrate legacy markdown memory to vector store."""
    state: DashboardState = request.app["state"]
    # A full markdown -> structured migration writes semantic AND episodic rows and
    # can flip memory.migrated for the whole install, so it takes the same gate as
    # the semantic write route rather than none at all.
    gate = await _memory_write_gate(state, request, "memory.migrate")
    if gate is not None:
        return gate
    store_name, denial = await resolve_requested_memory_store(request, state, "memory.migrate")
    if denial is not None:
        return denial
    if store_name:
        return web.json_response(
            {
                "error": "Legacy Markdown migration is only available for Global Memory V1. "
                "Use Choose starting knowledge to copy selected records into a member's memory.",
                "code": "migration_requires_global_memory",
            },
            status=400,
        )
    store = await _get_vector_store_async(state)

    async with _migrate_lock:
        prev_embed_fn = store.embed_fn
        # Embeddings are always-on — wire the embed_fn for migration vectors.
        store.embed_fn = make_sync_embed_fn()

        # Run in executor to avoid blocking event loop (can take 30+ seconds)
        loop = asyncio.get_running_loop()
        try:
            counts = await loop.run_in_executor(None, store.migrate_from_markdown)
        finally:
            store.embed_fn = prev_embed_fn  # restore previous, don't clobber
    # Auto-set migrated=true if migration produced entries
    if counts.get("semantic", 0) > 0 or counts.get("episodic", 0) > 0:
        await _set_migrated(True)
        if state.consolidator:
            state.consolidator._migrated = True
    return web.json_response(counts)


async def api_memory_import(request: web.Request) -> web.Response:
    """POST /api/memory/import — import memory from JSON (export format)."""
    state: DashboardState = request.app["state"]
    # The restricted-mode half alone was LESS protection than its siblings carry: it
    # answers False for an unrecognised key, so a forged X-Session-Key reached the
    # import. The recognition probe inside the shared gate closes that.
    gate = await _memory_write_gate(state, request, "memory.import")
    if gate is not None:
        return gate
    store, _store_name, denial = await _vector_tier_for_request(request, state, "memory.import")
    if denial is not None:
        return denial
    data, data_err = await read_bounded_json(request, max_bytes=None)
    if data_err is not None:
        return data_err
    assert data is not None  # read_bounded_json returns (dict, None) on success
    # import_memory embeds each imported entry via blocking in-process model
    # inference (unbounded — one per entry); offload so a large import can't
    # stall the gateway event loop.
    counts = await run_in_embed_pool(store.import_memory, data)
    return web.json_response(counts)


async def api_memory_context_preview(request: web.Request) -> web.Response:
    """GET /api/memory/context-preview?q=... — preview what gets injected into prompts."""
    store, _store_name, refusal = await _vector_tier_for_request(
        request, request.app["state"], "memory.context-preview"
    )
    if refusal is not None:
        return refusal
    query = request.query.get("q", "")[:500]
    if store.algorithm_version == "v2":
        preview = await run_in_embed_pool(store.get_context_preview, query_text=query)
        return web.json_response(
            _redact_memory_field(
                {
                    "semantic_context": preview["semantic_context"],
                    "episodic_context": preview["episodic_context"],
                }
            )
        )
    # Offload: the fetch serializes on _db_lock; see api_memory_semantic.
    # (No query_text is passed, so this is the recency path — no embed calls.)
    semantic_ctx = await asyncio.to_thread(store.get_semantic_context)
    # Filter semantic context by query if provided
    if query and semantic_ctx:
        lines = semantic_ctx.split("\n")
        q_lower = query.lower()
        filtered = [ln for ln in lines if q_lower in ln.lower() or ln.startswith("[")]
        semantic_ctx = "\n".join(filtered) if any(not ln.startswith("[") for ln in filtered) else ""
    # get_episodic_context embeds the query via blocking in-process model
    # inference; offload to keep the dashboard event loop responsive.
    episodic_ctx = (
        await run_in_embed_pool(store.get_episodic_context, query_text=query) if query else ""
    )
    return web.json_response(
        {
            "semantic_context": semantic_ctx,
            "episodic_context": episodic_ctx,
        }
    )


async def api_memory_consolidate(request: web.Request) -> web.Response:
    """POST /api/memory/consolidate — trigger immediate consolidation for testing."""
    state: DashboardState = request.app["state"]
    # Consolidation writes memory, and the recognition half is what stops a forged
    # or never-established X-Session-Key from dispatching a BILLED consolidation
    # LLM turn against a session it does not own.
    _, identity_refusal = await resolve_lesson_memory_store(request, state, "memory.consolidate")
    if identity_refusal is not None:
        return identity_refusal
    gate = await _memory_write_gate(state, request, "memory.consolidate")
    if gate is not None:
        return gate
    if not state.consolidator:
        return web.json_response({"error": "consolidator not available"}, status=503)
    # Global persistence switch (memory.persistence_enabled) and the
    # writes-only switch (memory.writes_enabled). The
    # inner _consolidate gate would refuse anyway; refusing here tells the
    # dashboard caller WHY instead of returning a generic refusal, and spends
    # no transcript read on a request that cannot proceed. The denial is
    # SEL-recorded: the request passed identity and the write gate, so the
    # refusal is a config-state decision an audit trail has to show rather than
    # an unauthenticated caller being turned away upstream.
    writes_off = memory_writes_off_key(KiroCrewConfig.load().memory)
    if writes_off is not None:
        code = (
            "persistence_disabled"
            if writes_off == "memory.persistence_enabled"
            else "writes_disabled"
        )
        _sel().log_api_access(
            caller=request.headers.get("X-Session-Key", ""),
            operation="memory.consolidate",
            outcome="denied",
            source="dashboard",
            resources=code,
        )
        return web.json_response(
            {
                "error": "Consolidation is paused: persistent memory writes are "
                f"disabled ({writes_off} is false).",
                "code": code,
            },
            status=403,
        )
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    key = body.get("key", "")
    if not isinstance(key, str):
        return web.json_response(
            {"error": "session key must be a string", "code": "invalid_session_key"},
            status=400,
        )
    key = key.strip()
    if not key:
        return web.json_response({"error": "session key required"}, status=400)
    from kiro_crew.context import store_of_session

    from ._shared import require_private_memory_session

    try:
        target_store = await asyncio.to_thread(store_of_session, state.conversation_log, key)
    except (ValueError, OSError):
        return web.json_response(
            {"error": "The member memory binding is unavailable.", "code": "store_unavailable"},
            status=503,
        )
    refusal = await require_private_memory_session(
        request, target_store, "memory.consolidate", session_key=key
    )
    if refusal is not None:
        return refusal
    # The write gate above tests the CALLER's mode; ``key`` names the TARGET.
    # Without this check a persistent caller consolidating a temporary or
    # incognito session persists semantic keys and episodic fragments for a
    # conversation the memory modes promise leaves no durable trace.
    # The target's mode is resolved the way the other routes resolve a key that
    # is not the request's own (``_headless_mode_refusal``): the live slot
    # first -- the authoritative record for an open dashboard tab, which the
    # consolidator cannot see -- then the persisted execution record and
    # transcript header for a closed session. Refused BEFORE the running claim,
    # the message-count read and the retry probe, so a refused target costs no
    # message-count or retry-probe read and never occupies the key.
    #
    # An UNRESOLVABLE mode proceeds. A live session always resolves, and for a
    # persisted one ``_consolidate`` re-reads the execution record and header of
    # the exact transcript it consolidates and refuses on either -- and every
    # durable write of the pass runs under the transcript's publication hold,
    # which re-validates the header -- so proceeding leaves no write path
    # unguarded -- while refusing here would turn a key
    # with no transcript at all (a harmless no-op today) or a stem the probe
    # finds ambiguous into a 403 on every "Consolidate all".
    #
    # A channel transcript reaches this route as its filename stem
    # (``slack_<ts>``: ``list_sessions`` hands out stems), while the thread's
    # durable incognito/temporary flag is keyed by its live ``slack:<ts>`` key
    # in the session map. Resolve the stem to that key first, or the flag is
    # invisible and the thread reads as persistent. Only the session map can
    # unfold a stem (the ``:``-to-``_`` fold is not reversible); it answers ""
    # for a stem it does not hold, which keeps the original key. The transcript
    # itself is still read under ``key``, the name the caller supplied.
    mode_key = key
    if is_channel_session_key(key) and state.sessions is not None:
        unfolded = state.sessions.channel_key_for_stem(key)
        if isinstance(unfolded, str) and is_channel_session_key(unfolded):
            mode_key = unfolded
    try:
        target_mode: str | None = await resolve_session_memory_mode(state, mode_key)
    except (OSError, ValueError, WorkflowMemoryError):
        target_mode = None
    if target_mode is None or not is_incognito_transcript(target_mode):
        # The live resolution above covers what only the gateway knows (a
        # dashboard slot's mode, an inherited subagent mode) and, for a channel
        # key, answers off the session map alone -- ``persistent`` for every
        # thread the map does not flag, without probing the transcript. The
        # transcript header is then read under ``key`` exactly as
        # ``_consolidate`` reads it (the same ``get_metadata``, the same
        # predicate): a thread whose stem the map could not unfold, or whose
        # entry the map does not hold, is refused on its header here and not
        # passed to a consolidator that refuses it -- the 200-for-a-refusal
        # the Memory tab would count as summarized. Read NORMALIZED, so the
        # body names the mode the header records rather than its spelling.
        log = state.conversation_log
        if log is not None:
            metadata = await asyncio.to_thread(log.get_metadata, key)
            if isinstance(metadata, dict):
                header_mode = transcript_privacy_mode(metadata.get("memory_mode"))
                if header_mode:
                    target_mode = header_mode
    if target_mode is not None and is_incognito_transcript(target_mode):
        # ``_read_session_key``, not the raw header: the audit record carries the
        # same canonical caller as the write gate's records.
        _sel().log_api_access(
            caller=_read_session_key(request),
            operation="memory.consolidate",
            outcome="denied",
            source="dashboard",
            resources=f"restricted_target_session:{target_mode}",
        )
        return web.json_response(
            {
                "error": f"Consolidation is not allowed for a {target_mode} session: "
                "it leaves no durable memory.",
                "code": "restricted_target_session",
                # The mode as a field, not only inside the sentence: the Memory
                # tab's tally names the mode a skipped session was in, and must
                # not parse English prose to learn it. Nothing new is disclosed;
                # the sentence already carries the word.
                "mode": target_mode,
            },
            status=403,
        )
    include_history = body.get("include_history", True)
    # Claim the key before the eligibility probe below, which awaits. Testing
    # membership and adding must happen with no yield between them: the probe
    # offloads a transcript read, and a check-then-act spanning that await lets
    # two concurrent POSTs both pass the guard and both dispatch, double-billing
    # an LLM turn on the same span. The claim is released again on every path
    # that does not hand the key to _consolidate, which discards it in its own
    # finally once the task ends.
    if state.consolidator._busy(key):  # another spelling of this transcript counts too
        return web.json_response({"error": "consolidation already running"}, status=409)
    state.consolidator._running.add(key)
    dispatched = False
    try:
        # The manual trigger honours the same durable retry accounting as the idle
        # sweep and the expiry sweep. A span whose consolidation keeps failing is in
        # exponential backoff (or abandoned at the attempt cap), and re-firing it by
        # hand spends another billed LLM turn on the same failure — so a bypass here
        # would reopen the unbounded-retry hole from the UI.
        #
        # The extent the cap is scoped to needs the transcript's message total, and
        # reading it is blocking file IO — offload it rather than stalling the loop on
        # a large transcript.
        _total = None
        if include_history:
            try:
                _total = (
                    await asyncio.to_thread(state.consolidator._log.consolidation_counts, key)
                )[0]
            except Exception:
                # No count means the extent test is skipped and the cap stands, which
                # only ever refuses a turn — never spends one on an unverified premise.
                logger.warning("Could not read message count for %s", key, exc_info=True)
        if include_history and not state.consolidator.retry_eligible(key, message_count=_total):
            return web.json_response(
                {
                    "error": "consolidation is in retry backoff for this session",
                    "code": "consolidation_retry_backoff",
                },
                status=429,
            )
        task = asyncio.create_task(state.consolidator._consolidate(key, include_history))
        dispatched = True
        state.consolidator._tasks.add(task)
        task.add_done_callback(state.consolidator._tasks.discard)
        return web.json_response({"ok": True, "key": key})
    finally:
        if not dispatched:
            state.consolidator._running.discard(key)


async def api_memory_observability(request: web.Request) -> web.Response:
    """GET /api/memory/observability — memory health metrics and context preview."""
    store, _store_name, refusal = await _vector_tier_for_request(
        request, request.app["state"], "memory.observability"
    )
    if refusal is not None:
        return refusal
    query = request.query.get("q", "")[:500]
    # Offload: both serialize on _db_lock — see api_memory_semantic.
    stats = await asyncio.to_thread(store.memory_stats)
    rejections = await asyncio.to_thread(store.get_rejection_stats)
    # get_context_preview with a query embeds the query AND every non-lesson
    # semantic row (blocking urllib per row) — the worst on-loop amplification
    # in the store; offload so it can't stall the gateway event loop.
    preview = await run_in_embed_pool(store.get_context_preview, query_text=query)
    # Read LAST, deliberately: the counters then include the reads this very
    # request performed, so a caller can issue ?q=... twice and compare the two
    # `reads` objects to see whether the second identical search re-read the
    # population. Offloaded like the others — it takes _db_lock.
    reads = await asyncio.to_thread(store.read_counters)
    return web.json_response(
        {
            "stats": stats,
            "rejections": rejections,
            "context_preview": preview,
            "reads": reads,
        }
    )


async def api_memory_promote(request: web.Request) -> web.Response:
    """POST /api/memory/promote — promote repeated episodic patterns to semantic facts."""
    state: DashboardState = request.app["state"]
    # Promotion writes semantic facts and TOMBSTONES the episodic rows it folded in,
    # so it is a destructive durable write and takes the same gate as the semantic
    # write route rather than none at all.
    gate = await _memory_write_gate(state, request, "memory.promote")
    if gate is not None:
        return gate
    store, _store_name, denial = await _vector_tier_for_request(request, state, "memory.promote")
    if denial is not None:
        return denial
    if store.algorithm_version == "v2":
        return web.json_response(
            {
                "error": "Automatic episode promotion is not available for member memory. "
                "Review and edit the member's records explicitly.",
                "code": "promotion_unavailable_for_member_memory",
            },
            status=400,
        )
    # allow_absent: every field below has a default, so a bodyless POST is
    # legitimate. A body that is present but malformed is still a 400:
    # answering 200-with-defaults to a client typo would silently run a
    # different promotion than the caller asked for.
    body, body_err = await read_bounded_json(request, max_bytes=None, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    try:
        min_count = body_int(body.get("min_count", 5))
        min_sim = float(body.get("min_sim", 0.75))
    # OverflowError: float() of a JSON integer beyond float range raises it.
    except (ValueError, TypeError, OverflowError):
        return web.json_response({"error": "min_count/min_sim must be numeric"}, status=400)
    # Run in executor (can take 10+ seconds)
    loop = asyncio.get_running_loop()
    promoted = await loop.run_in_executor(None, store.promote_episodic_patterns, min_count, min_sim)
    return web.json_response({"ok": True, "promoted": promoted})


async def api_memory_graph(request: web.Request) -> web.Response:
    """GET /api/memory/graph — return all memory as nodes + edges for graph visualization."""
    state: DashboardState = request.app["state"]
    mem = _get_memory(state)

    try:
        partial: dict[str, bool] = {}
        nodes, edges = await asyncio.to_thread(
            lambda: _build_memory_graph(mem, state.lessons.load_all(), partial)
        )

        for n in nodes:
            n["label"] = _redact_memory_field(n["label"])
            n["title"] = _redact_memory_field(n["title"])

        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="memory_graph", outcome="success"
        )
        # ``partial`` says which capped populations left rows out of the graph.
        return web.json_response({"nodes": nodes, "edges": edges, "partial": partial})
    except Exception:
        logging.getLogger(__name__).exception("memory_graph failed")
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="memory_graph", outcome="failure"
        )
        return web.json_response({"error": "failed to build memory graph"}, status=500)


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.memory_embeddings import (  # noqa: F401
        _MODEL_LOAD_TIMEOUT_SECS,
        _PIP_STDERR_LOG_CHARS,
        _SETUP_STEP_LEGACY,
        _apply_embedding_model,
        _embedding_repair_status,
        _embedding_setup_status,
        _ensure_pip_available,
        _faiss_install_lock,
        _probe_model_path,
        _redact_pip_stderr,
        _store_progress_adapter,
        _write_embed_model_config,
        api_memory_disable_embeddings,
        api_memory_embedding_model,
        api_memory_embedding_status,
        api_memory_enable_embeddings,
    )
    from kiro_crew.dashboard.handlers.memory_graph import (  # noqa: F401
        GRAPH_LESSON_LIMIT,
        GRAPH_SEMANTIC_LIMIT,
    )
    from kiro_crew.dashboard.handlers.memory_services import (  # noqa: F401
        _get_vector_store,
    )


if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.apps.registry import (  # noqa: F401
        minimal_env,
    )
    from kiro_crew.context import (  # noqa: F401
        validated_cached_vector_stores,
    )
    from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
        memory_startup_refusal,
    )
    from kiro_crew.dashboard.handlers.files import (  # noqa: F401
        _PathProbeBusy,
        _probe_busy_response,
        _run_path_probe,
    )
    from kiro_crew.embeddings import (  # noqa: F401
        DOWNLOAD_ATTEMPTS_INTERACTIVE,
        LEGACY_EMBEDDING_WARNING,
        ReembedProgress,
        _custom_model_id,
        _load_llama_class,
        _model_file_stamp,
        _platform_libs_dirname,
        _read_memory_config,
        activate_shared_embedder,
        active_embedding_space_signature,
        build_gated_bundled,
        build_gated_candidate,
        embedding_backend_serving,
        embedding_rebuild_generation,
        get_shared_embedder,
        install_shared_embedder,
        legacy_embedding_ids,
        model_download_manager,
        model_file_present,
        reconcile_store_embedding_space,
        reembed_progress,
        reset_shared_embedder,
        resolve_custom_model,
        validate_custom_model_path,
    )
    from kiro_crew.executors import (  # noqa: F401
        embed_executor,
    )
    from kiro_crew.platform_compat import (  # noqa: F401
        isolated_python_argv,
        kill_and_reap,
    )
    from kiro_crew.sandbox import (  # noqa: F401
        SandboxUnavailableError,
        cgroup_scope_argv,
        create_subprocess_limited,
        wrap_argv,
        wrap_argv_async,
    )


# This module is the facade of the memory API handlers: every name is reached through it
# as ``kiro_crew.dashboard.handlers.memory.<name>``. A read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module.
# The routes above stay here: the documents, settings, semantic and episodic tiers,
# migration, consolidation and the graph route. New work goes to the owner of its
# responsibility:
#
#   ``memory_services`` -- the SEL and the vector store every route reaches
#   ``memory_embeddings`` -- the embedding model: choose, report, enable
#   ``memory_graph`` -- building the memory graph's nodes and edges
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.memory_services",
    "kiro_crew.dashboard.handlers.memory_embeddings",
    "kiro_crew.dashboard.handlers.memory_graph",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
