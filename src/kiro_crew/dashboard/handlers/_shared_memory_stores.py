"""Which memory store a request reads and writes, and the store objects behind it.

The ``store`` query parameter and the startup refusal, the lesson store a caller's
session resolves to, the markdown and vector store tiers a named store opens, and
the audited admin-store resolution of the memory admin routes.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared_access import require_owner_dashboard_request
from kiro_crew.dashboard.handlers._shared_member_scope import (
    member_request_scope,
    require_private_memory_session,
)
from kiro_crew.dashboard.handlers._shared_scrub import _redact_memory_field
from kiro_crew.dashboard.handlers._shared_workspace import _get_memory
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger("kiro_crew.dashboard.handlers._shared")


def _session_memory_store(state: DashboardState, session_key: str) -> str:
    """The NAMED silo *session_key* writes to, or ``""`` for the global store.

    Reads the session's own recorded binding, which is the same source the
    consolidator uses (``context.store_of_session``) — so a
    durable write the AGENT makes lands in the silo its consolidations land in.
    Without this a crew's ``learn_add`` wrote into the global lessons table that
    every OTHER crew reads on every turn, while the crew's own context injected
    only its silo's lessons: one crew steering every crew, and its own correction
    never reaching its later turns.

    Unavailable recorded identity raises; it never turns into a global write.

    A thin adapter over ``context.store_of_session``, which is where the resolution
    lives: the channel surfaces hold a ``ContextBuilder`` rather than a
    ``DashboardState``, and two copies of "read the recorded binding" are how the
    dashboard's answer and a channel's answer drift apart for one session.
    """
    from kiro_crew.context import store_of_session

    return store_of_session(state.conversation_log, session_key)


async def resolve_lesson_memory_store(
    request: web.Request, state: DashboardState, operation: str
) -> tuple[str, web.Response | None]:
    """Authorize a lessons request before it uses a session's named store.

    Lessons are an agent-facing API, so verified internal calls retain their
    recorded binding. A browser or app token cannot borrow that authority by
    supplying another session's X-Session-Key: a named store then requires the
    dashboard owner. Only authentication middleware publishes internal_auth;
    a request header claiming to carry an internal secret is not evidence.

    No named binding preserves the existing global/workspace lessons contract.
    """
    from kiro_crew.memory_startup import MemoryStartupUnavailable

    try:
        if request.get("internal_auth") is True:
            scope = await member_request_scope(request)
            if not scope.verified:
                raise ValueError("Execution identity is unavailable")
            store = scope.store or ""
        else:
            store = await asyncio.to_thread(
                _session_memory_store, state, request.headers.get("X-Session-Key", "")
            )
    except MemoryStartupUnavailable as exc:
        return "", web.json_response(
            {"error": _redact_memory_field(str(exc)), "code": "store_unavailable"}, status=503
        )
    except (ValueError, OSError):
        startup_refusal = memory_startup_refusal()
        if startup_refusal is not None:
            return "", startup_refusal
        return "", web.json_response(
            {
                "error": "The member's memory binding is unavailable; global memory was not used.",
                "code": "store_unavailable",
            },
            status=503,
        )
    if store and request.get("internal_auth") is not True:
        denial = await require_owner_dashboard_request(request, operation)
        if denial is not None:
            return "", denial
    elif request.get("internal_auth") is True:
        refusal = await require_private_memory_session(request, store, operation)
        if refusal is not None:
            return "", refusal
    return store, memory_startup_refusal(store)


#: Query parameter naming the memory store a request addresses. One spelling,
#: read only by :func:`resolve_requested_memory_store`, because the whole
#: security property below rests on "the parameter is present" and a second
#: hand-typed spelling is how one route starts answering for a store the gate
#: never saw.
MEMORY_STORE_PARAM = "store"


def memory_startup_refusal(
    store: str = "default", *, allow_failed: bool = False
) -> web.Response | None:
    """Structured refusal shared by data routes; recovery controls stay usable."""
    from kiro_crew.memory_startup import MemoryStartupUnavailable, require_memory_ready

    try:
        require_memory_ready(store, allow_failed=allow_failed)
    except MemoryStartupUnavailable as exc:
        return web.json_response(
            {"error": _redact_memory_field(str(exc)), "code": "store_unavailable"}, status=503
        )
    return None


async def resolve_requested_memory_store(
    request: web.Request,
    state: DashboardState,
    operation: str,
    *,
    require_ready: bool = True,
    allow_failed: bool = False,
) -> tuple[str, web.Response | None]:
    """Route internal tools through their captured execution; owners may select a store.

    Browser requests without a store retain the Global default. An explicit
    store parameter always requires the ordinary owner permission, including
    when it names Global. Unknown selections never fall back to another store.
    """
    from kiro_crew.memory_stores import (
        DEFAULT_MEMORY_STORE,
        declared_store_names,
        named_store_or_empty,
    )

    if MEMORY_STORE_PARAM not in request.query:
        store = ""
        if request.get("internal_auth") is True:
            scope = await member_request_scope(request)
            if not scope.verified:
                return "", web.json_response(
                    {
                        "error": "The execution identity is unavailable; Global was not used.",
                        "code": "member_identity_unavailable",
                    },
                    status=409,
                )
            store = scope.store or ""
        refusal = (
            memory_startup_refusal(store, allow_failed=allow_failed) if require_ready else None
        )
        return store, refusal

    denial = await require_owner_dashboard_request(request, operation)
    if denial is not None:
        return "", denial

    requested = request.query[MEMORY_STORE_PARAM].strip() or DEFAULT_MEMORY_STORE
    if requested not in declared_store_names():
        return "", web.json_response(
            {
                "error": f"no memory store named {requested!r} is declared",
                "code": "unknown_memory_store",
            },
            status=404,
        )
    return named_store_or_empty(requested), (
        memory_startup_refusal(requested, allow_failed=allow_failed) if require_ready else None
    )


#: Guards the per-store caches below. ONE lock rather than one per store: building
#: a store happens once per store per gateway lifetime, so contention is
#: irrelevant, while a per-store lock map needs its own lock to be built safely
#: and buys nothing.
_store_tier_lock = LoopBoundLock()


async def markdown_memory_for_store(state: DashboardState, store: str):
    """Return manual documents plus the selected store's learning history facade."""
    from kiro_crew.memory_startup import require_memory_ready

    require_memory_ready(store)
    if not store:
        return _get_memory(state)
    from kiro_crew.memory_stores import require_memory_store

    def validate_target() -> int:
        from kiro_crew.config.loader import KiroCrewConfig

        require_memory_ready(store)
        config = KiroCrewConfig.load()
        record = config.memory_stores.get(store)
        version = getattr(record, "memory_version", 1)
        require_memory_store(store, config=config, require_directory=version == 2)
        return version

    version = await asyncio.to_thread(validate_target)
    vector = await vector_memory_for_store(state, store) if version == 2 else None
    cache: dict[str, Any] = getattr(state, "_store_markdown", None) or {}
    if store in cache:
        return cache[store]
    async with _store_tier_lock:
        # Deletion may have committed while this request awaited the tier lock.
        version = await asyncio.to_thread(validate_target)
        vector = await vector_memory_for_store(state, store) if version == 2 else None
        cache = getattr(state, "_store_markdown", None) or {}
        if store in cache:
            return cache[store]
        from kiro_crew.memory import MemoryStore
        from kiro_crew.memory_stores import memory_index_path_for, memory_store_dir_for

        def build_memory():
            import weakref

            from kiro_crew.member_memory_backup import (
                acquire_store_use_lock,
                release_store_use_lock,
            )
            from kiro_crew.memory_stores import MEMORY_DB_FILE

            workspace = memory_store_dir_for(store)
            fd = acquire_store_use_lock(workspace / MEMORY_DB_FILE) if version == 1 else None
            try:
                mem = MemoryStore(
                    workspace=workspace,
                    index_db=memory_index_path_for(store),
                    memory_version=version,
                    vector_store=vector,
                )
                mem.init()
                # Keep admission while either the cache or an in-flight request owns
                # this object, including V1 Markdown-only stores with no vector tier.
                if fd is not None:
                    weakref.finalize(mem, release_store_use_lock, fd)
                return mem
            except BaseException:
                release_store_use_lock(fd)
                raise

        mem = await asyncio.to_thread(build_memory)
        cache[store] = mem
        state._store_markdown = cache  # type: ignore[attr-defined]
        return mem


async def release_markdown_memory_store(state: DashboardState, store: str) -> None:
    """Drop a deleted member's dashboard cache after any in-flight construction."""
    async with _store_tier_lock:
        cache = getattr(state, "_store_markdown", None)
        memory = cache.pop(store, None) if cache is not None else None
    if memory is not None:
        memory.vector_store = None
        memory._invalidate_history_cache()


def _store_name(store: str) -> str:
    """The store's NAME, given the request seam's spelling of it.

    The seam spells the global store ``""`` (see
    :func:`resolve_requested_memory_store`), while every resolver in
    ``memory_stores`` and ``memory_backup`` takes a name and RAISES on an empty
    one — ``validate_memory_store_name("")`` is a refusal. So the two spellings
    are not interchangeable, and translating between them in one named place is
    what keeps a route from handing ``""`` to a path resolver.
    """
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

    return store or DEFAULT_MEMORY_STORE


async def _audit(request: web.Request, operation: str, outcome: str, resources: str) -> None:
    """Record a memory-store mutation in the security event log. Best-effort.

    Off the loop because the FIRST ``sel()`` of a process CONSTRUCTS the log
    (trust-dir creation, key validation, an owner-only DACL on Windows) and the
    owner gate only pays that cost on a denial — so on a gateway whose first
    admin request succeeds, this is the construction site.

    Never changes the outcome: the mutation has already landed when this runs, and
    losing the audit line is strictly better than turning a completed restore into
    a 500 the operator would retry.
    """
    from kiro_crew.sel import sel  # deferred: sel imports config

    caller = str(request.get("user") or "unknown")
    try:
        await asyncio.to_thread(
            lambda: sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome=outcome,
                source="dashboard",
                resources=resources,
            )
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


async def _admin_store(
    request: web.Request, operation: str, body: dict[str, Any] | None = None
) -> tuple[str, web.Response | None]:
    """The store this admin request addresses, or a refusal to return AS-IS.

    Same two answers as :func:`resolve_requested_memory_store` — ``""`` for the
    global store, a name for a silo — and the same 404 ``unknown_memory_store``
    for a name nobody declared.

    A POST names its store in the BODY rather than the query string, because that
    is where the rest of its arguments are, so the body is consulted first and
    the query resolver handles the absent case. The body key is
    :data:`MEMORY_STORE_PARAM` deliberately: one spelling for both transports is
    what keeps a route from starting to answer for a store under a second name
    the seam never sees.

    Only the DECLARED-name half of the seam is re-expressed here, never the gate:
    every caller of this function has already taken
    :func:`require_owner_dashboard_request` unconditionally, which is strictly
    stronger than the presence gate the query resolver applies.

    A value that is present but not a string REFUSES rather than falling through
    to the caller's binding, and gets the same 404 an undeclared name gets. Both
    halves matter: a mutation whose store field was mistyped must not silently
    land on the default store, and distinguishing malformed from unknown would
    report whether a given name is declared.
    """
    from kiro_crew.memory_stores import (
        DEFAULT_MEMORY_STORE,
        declared_store_names,
        named_store_or_empty,
    )

    require_ready = operation not in {"memory.backups.list", "memory.restore.cancel"}
    allow_failed = operation == "memory.restore"
    if body is not None and MEMORY_STORE_PARAM in body:
        raw = body[MEMORY_STORE_PARAM]
        # ``""`` is never a declared name, so a non-string lands on the 404 below.
        requested = (raw.strip() or DEFAULT_MEMORY_STORE) if isinstance(raw, str) else ""
        if requested not in declared_store_names():
            return "", web.json_response(
                {
                    "error": f"no memory store named {requested!r} is declared",
                    "code": "unknown_memory_store",
                },
                status=404,
            )
        return named_store_or_empty(requested), (
            memory_startup_refusal(requested, allow_failed=allow_failed) if require_ready else None
        )
    state: DashboardState = request.app["state"]
    return await resolve_requested_memory_store(
        request, state, operation, require_ready=require_ready, allow_failed=allow_failed
    )


def _store_unavailable(store: str) -> web.Response:
    """The 503 every route returns for a store it cannot address.

    One answer for both ways that happens — a vector tier that would not stand
    up, and a name that does not resolve to its own file — because the caller's
    remedy is the same and neither is a fact about the request.

    Never a fall back to the global store: serving the operator's own memory
    under a crew's name is invisible in the response, which is the one failure
    the file boundary exists to prevent.
    """
    return web.json_response(
        {
            "error": f"the vector store for memory store {_store_name(store)!r} is unavailable",
            "code": "store_unavailable",
        },
        status=503,
    )


async def vector_memory_for_store(state: DashboardState, store: str):
    """The VECTOR tier for *store*, or ``None`` when a silo's cannot be stood up.

    ``None`` is returned ONLY for a silo, and a caller must report it rather than
    falling back to the global store: serving the operator's own memory under a
    crew's name is the one failure the file boundary exists to prevent, and it is
    invisible in the response.
    """
    from kiro_crew.memory_startup import require_memory_ready

    require_memory_ready(store)
    if not store:
        from kiro_crew.dashboard.handlers.memory import _get_vector_store_async

        return await _get_vector_store_async(state)
    from kiro_crew.context import ContextBuilder

    return await ContextBuilder.ensure_store(store)
