"""Memory API handlers — preferences, projects, history, settings, semantic, episodic, embeddings, graph."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.embeddings import (
    DOWNLOAD_ATTEMPTS_INTERACTIVE,
    activate_shared_embedder,
    active_embedding_space_signature,
    build_gated_bundled,
    build_gated_candidate,
    embedding_backend_serving,
    get_shared_embedder,
    install_shared_embedder,
    make_sync_embed_fn,
    model_download_manager,
    model_file_present,
    reconcile_store_embedding_space,
    reembed_progress,
    reset_shared_embedder,
    resolve_custom_model,
    validate_custom_model_path,
)
from kiro_crew.executors import embed_executor, run_in_embed_pool
from kiro_crew.history import is_incognito_transcript
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.platform_compat import kill_and_reap
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from ._shared import _get_memory, _is_restricted_session, _redact_memory_field, read_bounded_json
from .cron import _recognize_session

logger = logging.getLogger(__name__)

# Per-endpoint write serialization for the offloaded markdown saves below.
# asyncio.to_thread hands each PUT to an executor worker, and workers can
# acquire the store's file lock OUT OF REQUEST ORDER — a rapid pair of saves
# could commit the older content last. The event loop used to serialize these
# accidentally (inline writes); these locks restore that ordering explicitly
# while keeping the blocking I/O off the loop.
_prefs_write_lock = LoopBoundLock()
_projects_write_lock = LoopBoundLock()
_history_write_lock = LoopBoundLock()

# Bounded because a wedged native load has no cancellation: without a deadline
# the progress tracker would sit at `applying` forever and every later apply
# would 409. Safe to bound ONLY because the candidate is gated — an abandoned
# loader publishes into an embedder we close, and close() is terminal.
_MODEL_LOAD_TIMEOUT_SECS = 600.0


def _sel():
    """Late-binding sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811
    return _pkg.sel()


async def api_memory_preferences(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/preferences."""
    state: DashboardState = request.app["state"]
    mem = _get_memory(state)
    if request.method == "PUT":
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        content = body.get("content", "")
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
        async with _prefs_write_lock:
            await asyncio.to_thread(mem.write_preferences, content)
        return web.json_response({"ok": True})
    return web.json_response({"content": mem.read_preferences()})


async def api_memory_projects(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/projects."""
    state: DashboardState = request.app["state"]
    mem = _get_memory(state)
    if request.method == "PUT":
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        content = body.get("content", "")
        # Offloaded for the same reason as api_memory_preferences above.
        async with _projects_write_lock:
            await asyncio.to_thread(mem.write_projects, content)
        return web.json_response({"ok": True})
    return web.json_response({"content": mem.read_projects()})


async def api_memory_history(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/history — recent daily summaries."""
    state: DashboardState = request.app["state"]
    mem = _get_memory(state)
    if request.method == "PUT":
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        content = body.get("content", "")
        # Write to today's history file. Offloaded like the two handlers
        # above (synchronous file I/O on the event loop stalls every other
        # gateway task), and routed through the store's atomic writer:
        # write_text would follow a planted symlink at the dated name and
        # tear under concurrent PUTs; the atomic replace commits whole
        # versions and never traverses a link at the temp path.
        today_path = mem._today_history_file()
        today_path.parent.mkdir(parents=True, exist_ok=True)
        async with _history_write_lock:
            await asyncio.to_thread(mem._atomic_write_text, today_path, content)
        return web.json_response({"ok": True})
    return web.json_response({"content": mem.read_recent_history()})


async def api_memory_settings(request: web.Request) -> web.Response:
    """GET/PUT /api/memory/settings — memory consolidation config."""
    cfg = KiroCrewConfig.load()
    if request.method == "PUT":
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
                return web.json_response({"error": "history_idle_hours must be numeric"}, status=400)
        if "history_max_days" in body:
            try:
                updates["history_max_days"] = max(7, int(body["history_max_days"]))
            except (ValueError, TypeError):
                return web.json_response({"error": "history_max_days must be an integer"}, status=400)
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
        # Apply to running consolidator
        state: DashboardState = request.app["state"]
        if state.consolidator:
            new_cfg = KiroCrewConfig.load()
            state.consolidator._history_idle_secs = new_cfg.memory.history_idle_hours * 3600
            state.consolidator._migrated = new_cfg.memory.migrated
        return web.json_response({"ok": True})
    return web.json_response(
        {
            "history_idle_hours": cfg.memory.history_idle_hours,
            "history_max_days": cfg.memory.history_max_days,
            "migrated": cfg.memory.migrated,
        }
    )


def _get_vector_store(state: DashboardState):
    """Get VectorMemoryStore from context_builder's memory, or create standalone."""
    mem = _get_memory(state)
    if mem.vector_store:
        return mem.vector_store
    # Fallback: create standalone
    # COUPLING: ``_get_vector_store_async``'s fast-path predicate mirrors the
    # resolution above. A new ``init()``-bearing branch added here must be
    # reflected there, or async handlers may run it on the event loop again.
    if not hasattr(state, "_standalone_vector"):
        # Both imports resolve their target at CALL time, which is what lets a test
        # substitute the attribute on the source module and have this function
        # observe it. ``KiroCrewConfig`` deliberately shadows this module's own
        # top-level binding: that binding captured the original object at import
        # time and would not see such a substitution.
        from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811
        from kiro_crew.vector_memory import VectorMemoryStore  # noqa: F811

        cfg = KiroCrewConfig.load()
        store = VectorMemoryStore(
            embedding_dim=cfg.memory.embedding_dim,
            decay_rates=cfg.memory.decay_rates or None,
        )
        store.init()
        state._standalone_vector = store  # type: ignore[attr-defined]
        mem.vector_store = store
    return state._standalone_vector  # type: ignore[attr-defined]


async def _get_vector_store_async(state: DashboardState):
    """Async facade over ``_get_vector_store`` honouring init's caller contract.

    ``VectorMemoryStore.init()`` documents that async callers must offload it
    (it is blocking file IO end to end — sqlite connect, migrations, the
    owner-only lockdown pass), so the standalone fallback inside
    ``_get_vector_store`` must not run inline in a handler (#5221). Fast path:
    when a store is already resolvable without
    running ``init()`` — the context_builder supplied one, or a prior call
    cached the standalone fallback on ``state`` — delegate synchronously, so
    the common request path pays no thread hop. In both fast-path cases
    ``_get_vector_store`` returns before reaching its fallback, so ``init()``
    stays unreachable on the loop.
    """
    # Resolve the memory store ON the loop: ``_get_memory``'s
    # check-create-publish of ``state._standalone_memory`` is atomic here (no
    # await), exactly as it is for every synchronous caller. Resolving it only
    # inside the worker would race a concurrent loop-side ``_get_memory`` into
    # publishing a second MemoryStore, detaching ``vector_store`` from the
    # object every other handler reads. MemoryStore's own ``init()`` is a
    # cheap mkdir+seed (not the lockdown-bearing one this wrapper offloads) and
    # ran on the loop for every request before #5221.
    mem = _get_memory(state)
    if mem.vector_store or hasattr(state, "_standalone_vector"):
        return _get_vector_store(state)
    # Slow path: at most the first standalone request per process constructs
    # and ``init()``s the store — offload it. All concurrent misses await ONE
    # shared task, restoring the serialization the synchronous call sites used
    # to get for free from the event loop: without it, two concurrent first
    # requests would both miss the cache and both run ``init()``, leaking one
    # of the two sqlite connections. ``asyncio.shield`` keeps the task (and
    # its worker thread) alive when a caller is cancelled — e.g. an aiohttp
    # client disconnect — so a request landing in that window awaits the same
    # init instead of arming a second one. The slot is armed with no await
    # between the read and the write (cannot race on one loop) and cleared on
    # completion: after success the fast path serves from the cache
    # (``_get_vector_store`` publishes it before the task resolves), and after
    # failure the next request retries with a fresh task — matching the
    # pre-#5221 per-request retry semantics.
    task = getattr(state, "_standalone_vector_init_task", None)
    if task is None:
        task = asyncio.get_running_loop().create_task(
            asyncio.to_thread(_get_vector_store, state)
        )
        state._standalone_vector_init_task = task  # type: ignore[attr-defined]
        task.add_done_callback(
            lambda _t: setattr(state, "_standalone_vector_init_task", None)
        )
    return await asyncio.shield(task)


async def api_memory_semantic(request: web.Request) -> web.Response:
    """GET /api/memory/semantic — list semantic memory entries (paginated).

    Server-capped via ``min(limit, 1000)`` + ``offset`` so a single GET can't
    serialize the whole (continuously-written) semantic table (CWE-770). The
    bound is generous (≈4 MB worst case at the 4 KB per-value limit) so the
    dashboard memory card's client-side filter keeps full coverage for typical
    single-user stores; a store larger than this needs server-side search.
    """
    store = await _get_vector_store_async(request.app["state"])
    try:
        limit = min(int(request.query.get("limit", "1000")), 1000)
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        return web.json_response({"error": "limit/offset must be integers"}, status=400)
    entries = []
    # Offload: the fetch serializes on the store's _db_lock (#1947), and a
    # worker holding it (e.g. backfill's locked FAISS rebuild) would otherwise
    # block the gateway event loop here.
    rows = await asyncio.to_thread(store.get_all_semantic, limit=limit, offset=offset)
    for e in rows:
        d = {k: v for k, v in dict(e).items() if not isinstance(v, (bytes, memoryview))}
        entries.append(_redact_memory_field(d))
    return web.json_response({"entries": entries})


async def api_memory_semantic_write(request: web.Request) -> web.Response:
    """PUT /api/memory/semantic — create/update a semantic entry."""
    state: DashboardState = request.app["state"]
    # Session-recognition gate (shared with the lessons routes, #3226): the
    # restricted-mode check below returns False for an unknown key, so before
    # this gate a forged or never-established X-Session-Key could write
    # semantic memory that create-style routes would refuse. Writes block
    # every private persisted mode, mirroring ``api_lessons_create``.
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, sk, "semantic.write",
        blocks_persisted_mode=is_incognito_transcript,
    )
    if refusal is not None:
        return refusal
    if _is_restricted_session(state, request):
        _sel().log_api_access(
            caller=sk, operation="semantic.write", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response({"error": "Memory writes are not allowed in this session mode."}, status=403)
    store = await _get_vector_store_async(request.app["state"])
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    key = body.get("key", "")
    value = body.get("value")
    confidence = float(body.get("confidence", 1.0)) if isinstance(body.get("confidence"), (int, float)) else 1.0
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
        # error branch. The enum itself belongs in ``vector_memory_constants``
        # (the dependency-free split-out this module's other constants already
        # live in), but relocating it would edit ``vector_memory.py``; deferring
        # the import keeps the cost off the import path without that change.
        from kiro_crew.vector_memory import SemanticRejectCode

        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="semantic.write", outcome="rejected",
            source="dashboard", resources=f"{code.value}:{key}",
        )
        status = 409 if code == SemanticRejectCode.CONFLICT else 422
        msg, _ = redact_exfiltration_urls(message)
        msg, _ = redact_credentials(msg)
        return web.json_response({"error": msg}, status=status)
    sk = request.headers.get("X-Session-Key", "")
    _sel().log_api_access(
        caller=sk, operation="semantic.write", outcome="success",
        source="dashboard", resources=key,
    )
    return web.json_response({"ok": True})


async def api_memory_semantic_delete(request: web.Request) -> web.Response:
    """DELETE /api/memory/semantic/{key} — tombstone a semantic entry."""
    state: DashboardState = request.app["state"]
    # Same recognition gate as the write route: without it, this DELETE was
    # LESS protected than the lessons delete #3226 fixed — an unknown key
    # passed the restricted-mode check (False for unrecognised sessions) and
    # could tombstone any semantic entry. Policy for known sessions is
    # unchanged: this route keeps blocking incognito AND temporary (the
    # ``_is_restricted_session`` check below), so the recovery-path probe
    # blocks every private mode to match.
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, sk, "semantic.delete",
        blocks_persisted_mode=is_incognito_transcript,
    )
    if refusal is not None:
        return refusal
    if _is_restricted_session(state, request):
        _sel().log_api_access(
            caller=sk, operation="semantic.delete", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response({"error": "Memory writes are not allowed in this session mode."}, status=403)
    store = await _get_vector_store_async(request.app["state"])
    key = request.match_info["key"]
    # Offload: acquires _db_lock internally (#1947) — see api_memory_semantic.
    ok = await asyncio.to_thread(store.delete_semantic, key, source="user_explicit")
    if not ok:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})


async def api_memory_events(request: web.Request) -> web.Response:
    """GET /api/memory/events — paginated audit trail."""
    store = await _get_vector_store_async(request.app["state"])
    try:
        limit = min(int(request.query.get("limit", "50")), 200)
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        return web.json_response({"error": "limit/offset must be integers"}, status=400)
    # Offload: serializes on _db_lock (#1947) — see api_memory_semantic.
    events = await asyncio.to_thread(store.get_events, limit=limit, offset=offset)
    return web.json_response({"events": events})


_embedding_setup_status: dict[str, object] = {"step": "idle", "error": ""}
_faiss_install_lock = LoopBoundLock()
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


# ModelDownloadManager.status steps → the setup_step vocabulary the shipped
# frontend polling loop terminates on ("done" / "error" / "idle"). New-style
# steps are additionally exposed raw as download_step for newer frontends.
_SETUP_STEP_LEGACY = {
    "ready": "done",
    "failed": "error",
    "idle": "idle",
    "downloading": "downloading",
    "verifying": "downloading",
    "waiting_retry": "downloading",
}


async def _write_embed_model_config(path: str, dim: int) -> None:
    """Persist ``memory.embed_model_path`` + ``embedding_dim``.

    Same fail-closed contract as :func:`_set_migrated`: an unparseable
    config.json is left alone rather than clobbered with only these two keys,
    which would destroy every other recoverable setting.
    """
    def _apply(data: dict) -> dict:
        memory = data.setdefault("memory", {})
        if path:
            memory["embed_model_path"] = path
        else:
            memory.pop("embed_model_path", None)
        # An explicit memory.embed_model_id OVERRIDES the derived (name+size)
        # identity — _custom_model_id documents that "an explicit id always
        # wins" — so it is pinned to whichever model the operator set it for.
        # Carrying it across a model change keeps the OLD vector-space
        # signature, so a swap to a different model of the SAME dimension
        # reconciles as "space unchanged": vectors from the previous model are
        # retained and then compared against new-model vectors, corrupting
        # semantic results with nothing on screen to explain it. Drop it and let
        # the id be re-derived from the file actually in use.
        memory.pop("embed_model_id", None)
        if dim > 0:
            memory["embedding_dim"] = dim
        return data

    try:
        await run_config_write(update_config_locked, config_path(), mutate=_apply)
    except ConfigReadError as exc:
        logger.warning(
            "config.json is unparseable; refusing to write the embedding "
            "model path to avoid clobbering other settings"
        )
        raise ValueError(
            "config.json could not be parsed — fix it before changing the model"
        ) from exc


def _apply_embedding_model(store: object, raw: str, loop: "asyncio.AbstractEventLoop") -> None:
    """Blocking apply of a model change. Runs on a worker thread, never the loop.

    The candidate is loaded EXACTLY ONCE and is GATED for its whole lifetime as a
    candidate. That combination is what makes a live swap safe:

    1. Install the gated candidate, closing the outgoing model in the same step.
       Peak residency stays one model, and because the slot is never empty a
       concurrent status poll cannot rebuild the outgoing one behind us.
    2. Wait for the load, bounded. A timeout is safe here only BECAUSE of the
       gate: the abandoned loader publishes into an embedder we then close, and
       ``close()`` is terminal, so it can never start serving.
    3. Any failure before activation rolls back — drop the candidate and let the
       next ``get_shared_embedder()`` rebuild the previous model from config,
       which is still untouched at that point.
    4. Persist path + the width the model reported. Only now is the new space the
       configured one.
    5. Retarget the store's width, then reconcile (NULLs foreign vectors, drops
       the stale index).
    6. ACTIVATE. Everything before this point could still hand a caller a vector
       from a space the store had not reconciled to.
    7. Backfill with progress, which is what the dashboard indicator renders.
    """
    prog = reembed_progress()
    candidate_installed = False
    # Hoisted above the try: the catch-all handler below calls _restore_dim(), so
    # it must be defined even when the failure lands before the retarget.
    previous_dim = store._embedding_dim  # type: ignore[attr-defined]
    dim_retargeted = False

    def _restore_dim() -> None:
        """Undo the width retarget so the store matches the model being restored.

        Retargeting without restoring on failure is worse than the failure itself:
        the store would expect the NEW width while the rebuilt backend is the OLD
        model, so backfill's per-row shape check and build_faiss_index' width
        check reject every vector — and reconcile has already NULLed the corpus —
        leaving memory keyword-only for the rest of the process lifetime.
        """
        if dim_retargeted:
            store.set_embedding_dim(previous_dim)  # type: ignore[attr-defined]
    try:
        if raw:
            # Re-validate HERE rather than trusting the request-boundary check:
            # the worker is a thread hop away, so re-deriving the path without
            # the sensitive-path gate would leave the gate and the actual
            # native-library file access in different scopes.
            candidate, verr, _vcode = validate_custom_model_path(raw, "The model path")
            if verr:
                prog.fail(verr)
                return
            install_shared_embedder(build_gated_candidate(candidate))
        else:
            # Reverting to the bundled model takes the SAME gated path. Its width
            # is known, but the file is a download and can be absent — persisting
            # the revert before proving it loads would discard a working custom
            # configuration with nothing to fall back to.
            install_shared_embedder(build_gated_bundled())
        candidate_installed = True
        # From here the outgoing model is no longer authoritative. Anything already
        # inside _try_embed produced its vector in the old space; the store's
        # generation guard drops those instead of committing them behind the
        # reconcile. Bumped for EVERY swap, including same-width ones, which a dim
        # comparison alone would miss.
        store.begin_space_change()  # type: ignore[attr-defined]

        embedder = get_shared_embedder()
        wait_ready = getattr(embedder, "wait_ready", None)
        ready = (
            wait_ready(timeout=_MODEL_LOAD_TIMEOUT_SECS)
            if callable(wait_ready)
            else embedder.is_ready()
        )
        if not ready:
            if candidate_installed:
                # Config still names the PREVIOUS model — nothing has been
                # persisted yet on either branch — so dropping the candidate
                # restores it on the next get_shared_embedder(). The candidate is
                # retired terminally, so if this was a timeout its loader cannot
                # publish a serving model later.
                reset_shared_embedder()
            prog.fail(
                "the model did not load — run 'kirocrew doctor' for the reason "
                "(memory falls back to keyword search meanwhile)"
            )
            return

        # _restore_dim() reads this from the enclosing scope at call time.
        dim_retargeted = store.set_embedding_dim(embedder.dim)  # type: ignore[attr-defined]

        store.embed_fn = make_sync_embed_fn()  # type: ignore[attr-defined]
        reconcile_store_embedding_space(store)  # type: ignore[arg-type]

        # Reconcile DELIBERATELY does not stamp the signature when it could not
        # unlink the stale FAISS pair (read-only memory dir; Windows while the
        # index is mapped — both named in its own comment). Ignoring that would
        # let this report "Re-embedding complete" for a store that was never
        # reconciled, and the next start's load_faiss_index() prefers the
        # surviving OLD-space pair. The recorded space is the observable.
        recorded = store.recorded_embedding_space()  # type: ignore[attr-defined]
        if recorded != active_embedding_space_signature():
            # Config still names the PREVIOUS model (the write is below), so
            # dropping the candidate restores it. The NULLed vectors are refilled
            # by the next boot's backfill under that model.
            reset_shared_embedder()
            _restore_dim()
            prog.fail(
                "the old vector index could not be removed, so the model change was "
                "rolled back — check permissions on the memory directory and retry"
            )
            return

        # Persisted LAST, after the model proved it loads AND the store agreed to
        # its space. Reconcile reads the live backend, not config, so it does not
        # need the new path on disk first — and deferring the write is what makes
        # a reconcile failure recoverable: config still names the PREVIOUS model,
        # so the rollback below rebuilds that model instead of resurrecting the
        # new one, ungated, against a store that was never reconciled.
        try:
            fut = asyncio.run_coroutine_threadsafe(
                _write_embed_model_config(raw, embedder.dim), loop
            )
            fut.result()
        except ValueError as exc:
            # Unparseable config.json. The store is already reconciled to the new
            # space; restoring the previous model re-stamps and re-embeds it on
            # the next reconcile, which is recoverable. Serving a model config
            # does not name would not be.
            reset_shared_embedder()
            _restore_dim()
            prog.fail(str(exc))
            return

        # The store now agrees with the candidate's space AND config names it, so
        # it is finally safe for ordinary consumers to get vectors from it.
        activate_shared_embedder()
        logger.info(
            "Applied embedding model %s (%dd, space %s) — re-embedding in background",
            embedder.model_id,
            embedder.dim,
            active_embedding_space_signature(),
        )
        # pace=False: the user just applied a model change and is watching this
        # progress bar, and semantic search stays degraded until the sweep ends.
        # Bulk pacing exists to keep an UNATTENDED sweep quiet — spreading a wait
        # someone explicitly asked for only doubles it.
        embedded = store.backfill_missing_embeddings(  # type: ignore[attr-defined]
            progress=prog.advance, pace=False
        )
        prog.finish(embedded)
    except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard, never crashes the app
        logger.warning("Applying the embedding model failed", exc_info=True)
        if candidate_installed and not embedding_backend_serving():
            # Failed before activation: a gated candidate left installed would
            # serve nobody for the process lifetime. Drop it so the previous
            # model (still the configured one, unless the write already landed)
            # is rebuilt on demand.
            reset_shared_embedder()
            _restore_dim()
        prog.fail(str(exc) or exc.__class__.__name__)


async def api_memory_embedding_model(request: web.Request) -> web.Response:
    """POST /api/memory/embedding-model — validate and apply a custom model.

    Body: ``{"path": "<absolute path to .gguf>", "validate_only": bool}``.
    An empty ``path`` reverts to the bundled model.

    The dimension is NOT taken from the caller: it is read off the loaded model
    (``n_embd``), so the user cannot get it wrong and the UI needs no dim field.
    """
    state: DashboardState = request.app["state"]
    if _is_restricted_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="memory.embedding_model", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response(
            {"error": "not available in this session", "code": "restricted_session"},
            status=403,
        )
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    raw = str(body.get("path", "") or "").strip()
    validate_only = bool(body.get("validate_only"))

    # Validate BEFORE writing, so a typo never lands in config.
    size_bytes = 0
    if raw:
        path, error, code = validate_custom_model_path(raw, "The model path")
        if error:
            return web.json_response(
                {"ok": False, "error": error, "code": code}, status=400
            )
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
    if validate_only:
        return web.json_response({"ok": True, "size_bytes": size_bytes})

    # KIROCREW_EMBED_MODEL_PATH wins over memory.embed_model_path for the PATH,
    # but resolve_custom_model() always reads memory.embedding_dim from CONFIG.
    # So applying a model here while the env override is set would persist THIS
    # model's width against the ENV's path — a pair _load_model refuses on the
    # width check, leaving the previously-working env-pinned model unloadable on
    # every restart until config.json is hand-edited. Refuse instead: with the
    # env override in force a config write cannot take effect anyway.
    if os.environ.get("KIROCREW_EMBED_MODEL_PATH", "").strip():
        return web.json_response(
            {"ok": False,
             "error": "KIROCREW_EMBED_MODEL_PATH is set, so it overrides the configured "
                      "path — unset it to change the model from here",
             "code": "env_override_active"},
            status=409,
        )

    prog = reembed_progress()

    try:
        store = await _get_vector_store_async(state)
    except Exception:  # noqa: BLE001 - surfaced to the caller, not swallowed
        # Acquire the store BEFORE begin_apply(). If this raised after the
        # progress tracker was armed, is_active() would stay true for the rest of
        # the process lifetime and every later apply would 409 while the card
        # polled an indeterminate bar forever.
        logger.warning("Embedding model apply: vector store unavailable", exc_info=True)
        # Detail is in the server log above; the client body (rendered verbatim
        # into a localized UI) gets a generic message.
        return web.json_response(
            {"ok": False, "error": "vector memory is unavailable",
             "code": "vector_store_unavailable"},
            status=503,
        )

    if prog.is_active():
        # Single-flight: a second apply mid-re-embed would race the first over
        # the same rows and the same FAISS file. Checked once, AFTER the
        # awaited store acquisition — the acquisition can yield to the loop
        # (#5221), so a pre-await check could go stale before begin_apply();
        # and whenever an apply is active, a prior apply already resolved the
        # store, so the acquisition above was the free sync fast path. Checked
        # BEFORE the SEL audit so a refused apply is not logged as allowed.
        return web.json_response(
            {"error": "a model change is already being applied",
             "code": "model_change_in_progress"},
            status=409
        )

    # Audit the ALLOWED decision too, not just the restricted-session denial
    # above. This mutates config AND reshapes the whole vector store (reconcile
    # NULLs every embedding, then the backfill re-embeds it), so an operator
    # reading the SEL log must see the change that actually happened — auditing
    # only blocked attempts would show the denials and hide the applies.
    # Logged BEFORE the worker starts so the intent is recorded even if the
    # process dies mid-apply; the outcome is observable via the reembed status.
    _sel().log_api_access(
        caller=request.headers.get("X-Session-Key", ""),
        operation="memory.embedding_model",
        outcome="allowed",
        source="dashboard",
        resources=f"apply:{raw or 'bundled'}",
    )

    # Config is written by the worker AFTER the candidate's width is probed, so a
    # bad file never displaces a working configuration.
    prog.begin_apply()
    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(
        embed_executor(), _apply_embedding_model, store, raw, loop
    )
    # Retain the future so it is not garbage-collected mid-apply.
    state._embed_model_apply_task = task  # type: ignore[attr-defined]
    return web.json_response({"ok": True, "size_bytes": size_bytes, "status": "applying"})


async def api_memory_embedding_status(request: web.Request) -> web.Response:
    """GET /api/memory/embedding-status — embedding system status + setup progress."""
    embedder = get_shared_embedder()
    mgr = model_download_manager()
    step = str(mgr.status["step"])
    model_present = model_file_present()
    custom = resolve_custom_model()

    setup_step = _SETUP_STEP_LEGACY.get(step, step)
    setup_error = str(mgr.status["error"])
    can_retry = step == "failed" and bool(setup_error)
    if custom is not None:
        # No download is pending or possible in custom mode, so the download
        # manager's step ("idle" — it never ran) would leave the frontend
        # polling forever. Report a TERMINAL state derived from whether the
        # configured file is actually usable, and never offer Retry: retrying
        # would download the bundled model, which is not the one in use.
        can_retry = False
        if custom.error or not model_present:
            setup_step = "error"
            setup_error = custom.error or f"custom embedding model not readable: {custom.path}"
        else:
            setup_step = "done"
            setup_error = ""

    return web.json_response(
        {
            # Embeddings are always-on since the in-process runtime landed.
            "enabled": True,
            # Legacy value kept: the shipped frontend hard-checks
            # provider === "ollama" to render the healthy state; report the
            # legacy token until the frontend ships its companion change.
            "provider": "ollama",
            # Legacy field names kept for frontend compatibility.
            "ollama_installed": True,  # n/a — runtime is vendored/always present
            "model_available": model_present,
            # Model disclosure: the stable identifier of the embedding model
            # producing vectors + its output dimensionality. Surfaced so the
            # Memory tab can show users exactly which model runs locally.
            "model_id": embedder.model_id,
            "model_dim": embedder.dim,
            # Provenance: "custom" means a user-supplied GGUF from
            # memory.embed_model_path is in use and the bundled model is never
            # downloaded. The path is shown so a misconfiguration is diagnosable
            # from the UI rather than only from the logs.
            "model_source": "custom" if custom is not None else "default",
            "model_path": str(custom.path) if custom is not None else "",
            # "healthy" = embeddings usable now or ready to lazily activate:
            # the model file being present is what matters — the in-memory
            # load happens automatically on first embed.
            "server_healthy": model_present or embedder.is_ready(),
            "needs_docker": False,
            "docker_available": True,
            "setup_step": setup_step,
            "download_step": step,
            "download_attempt": mgr.status["attempt"],
            "bytes_downloaded": mgr.status.get("bytes_downloaded", 0),
            "bytes_total": mgr.status.get("bytes_total", 0),
            "setup_error": setup_error,
            "can_retry": can_retry,
            # Live re-embed progress for the Memory tab indicator. Same
            # in-memory pattern as the download status above, so the card's
            # existing 2s poll picks it up with no new endpoint.
            "reembed": reembed_progress().snapshot(),
        }
    )


async def _ensure_pip_available() -> tuple[bool, str]:
    """Ensure pip is importable in the runtime interpreter.

    Some packaged or minimal Python runtimes ship without pip, so a bare
    ``sys.executable -m pip install`` fails with "No module named pip" and the
    faiss-cpu install below never runs. Bootstrap pip via ``ensurepip`` (shipped
    with CPython) first. No-op when pip already imports. Returns
    ``(ok, error_message)`` — ``error_message`` is empty on success.
    """
    try:
        import pip  # noqa: F401
        return True, ""
    except ImportError:
        pass
    try:
        sandboxed_argv, cleanup = await wrap_argv_async(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            mode="standard",
            _prepare=wrap_argv,
        )
    except SandboxUnavailableError as exc:
        # Fail-closed sandbox (any host with no OS backend). Report it as a
        # normal not-ok result: the caller resets the setup status and returns a
        # 500, so an escaping exception can never leave the non-terminal
        # "installing_faiss" latched and 409 every later Enable click.
        return False, f"pip bootstrap could not run in a sandbox: {exc}"
    sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
    try:
        proc = await create_subprocess_limited(
            *sandboxed_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            await kill_and_reap(proc)
            logger.warning("ensurepip bootstrap timed out")
            return False, "pip bootstrap (ensurepip) timed out"
        if proc.returncode != 0:
            logger.warning("ensurepip bootstrap failed: %s", stderr.decode()[:500])
            return False, "pip bootstrap (ensurepip) failed"
        importlib.invalidate_caches()
        logger.info("Bootstrapped pip via ensurepip")
        return True, ""
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


async def api_memory_enable_embeddings(request: web.Request) -> web.Response:
    """POST /api/memory/enable-embeddings — trigger/retry model download and wire embeddings."""
    global _embedding_setup_status

    # Allow retry — reset any previous error state
    if _embedding_setup_status["step"] == "error":
        _embedding_setup_status = {"step": "idle", "error": ""}

    # Prevent concurrent setup attempts
    if _embedding_setup_status["step"] not in ("idle", "done", "failed"):
        return web.json_response(
            {"error": f"Setup already in progress: {_embedding_setup_status['step']}"},
            status=409,
        )

    _embedding_setup_status = {"step": "downloading", "error": ""}
    mgr = model_download_manager()

    try:
        # If the model isn't present, kick/adopt the background download and
        # return immediately — the frontend polls embedding-status for
        # progress. Never await ensure_model here: the manager's asyncio lock
        # may be held by the startup background task mid-backoff, which would
        # pin this HTTP request open for up to hours.
        if not model_file_present():
            if mgr.status["step"] in ("downloading", "verifying", "waiting_retry"):
                # Background download already in flight — surface its progress.
                return web.json_response(
                    {"ok": True, "status": "downloading", "setup_step": mgr.status["step"]}
                )
            state: DashboardState = request.app["state"]
            task = asyncio.create_task(mgr.ensure_model(attempts=DOWNLOAD_ATTEMPTS_INTERACTIVE))
            retained = state.__dict__.setdefault("_bg_embed_tasks", set())
            retained.add(task)

            def _on_download_done(t: "asyncio.Task[bool]") -> None:
                global _embedding_setup_status
                if t.cancelled():
                    _embedding_setup_status = {
                        "step": "failed",
                        "error": "cancelled",
                    }
                elif t.exception():
                    _embedding_setup_status = {
                        "step": "failed",
                        "error": str(t.exception()),
                    }
                elif not t.result():
                    # ensure_model() returning False = download failed after all
                    # retries without raising — surface it so the frontend shows
                    # the error + Retry button instead of a silent idle state.
                    _embedding_setup_status = {
                        "step": "failed",
                        "error": str(mgr.status.get("error", "download failed")),
                    }
                else:
                    _embedding_setup_status = {"step": "idle", "error": ""}

            task.add_done_callback(_on_download_done)
            task.add_done_callback(retained.discard)
            _embedding_setup_status = {"step": "downloading", "error": ""}
            return web.json_response({"ok": True, "status": "downloading"})
    except Exception:
        logger.exception("Embedding setup failed")
        _embedding_setup_status = {
            "step": "idle",
            "error": "Unexpected error — click Enable to retry",
        }
        return web.json_response(
            {"error": "Setup failed unexpectedly. Click Enable to retry."}, status=500
        )

    # Ensure faiss-cpu is installed (required for FAISS vector index).
    async with _faiss_install_lock:
        try:
            import faiss  # noqa: F401
        except ImportError:
            _embedding_setup_status = {"step": "installing_faiss", "error": ""}
            pip_ok, pip_err = await _ensure_pip_available()
            if not pip_ok:
                _embedding_setup_status = {
                    "step": "idle",
                    "error": f"{pip_err} — click Enable to retry",
                }
                return web.json_response(
                    {"error": f"{pip_err}. Click Enable to retry."}, status=500
                )
            try:
                sandboxed_argv, cleanup = await wrap_argv_async(
                    [sys.executable, "-m", "pip", "install", "-q",
                     "faiss-cpu", "--only-binary=:all:"],
                    mode="standard",
                    _prepare=wrap_argv,
                )
            except SandboxUnavailableError:
                # faiss is a pure accelerator; episodic recall still works via
                # the stdlib cosine fallback (_sqlite_vector_search). On a host
                # with no sandbox backend the install cannot run, but that must
                # not wedge setup — so fall through to the embed_fn wiring below
                # with faiss absent, and let the tail set the terminal "done".
                # `kirocrew doctor` points the user at a manual
                # `pip install faiss-cpu` if they want the accelerator.
                #
                # Deliberately NOT resetting the status to "idle" here: the 409
                # guard is checked BEFORE _faiss_install_lock, so publishing a
                # terminal status mid-flight would admit a second concurrent
                # Enable and duplicate the ~85 lines of unserialized setup that
                # follow (embed_fn wiring, load_faiss_index, the config
                # read/write cycle). The tail's "done" is the only terminal write.
                logger.info(
                    "Skipping on-demand faiss-cpu install: no sandbox backend on "
                    "this host. Episodic recall uses the stdlib cosine fallback."
                )
                cleanup = None
                sandboxed_argv = None
            if sandboxed_argv is not None:
                sandboxed_argv = cgroup_scope_argv(
                    sandboxed_argv
                )  # cgroup DoS ceiling
                try:
                    proc = await create_subprocess_limited(
                        *sandboxed_argv,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                    except asyncio.TimeoutError:
                        await kill_and_reap(proc)
                        logger.warning("faiss-cpu install timed out")
                        _embedding_setup_status = {
                            "step": "idle",
                            "error": "faiss-cpu install timed out — click Enable to retry",
                        }
                        return web.json_response(
                            {"error": "faiss-cpu install timed out."}, status=500,
                        )
                    if proc.returncode != 0:
                        logger.warning("faiss-cpu install failed: %s", stderr.decode()[:500])
                        _embedding_setup_status = {
                            "step": "idle",
                            "error": "faiss-cpu installation failed — click Enable to retry",
                        }
                        return web.json_response(
                            {"error": "faiss-cpu installation failed. Click Enable to retry."},
                            status=500,
                        )
                    else:
                        importlib.invalidate_caches()
                        logger.info("Installed faiss-cpu for vector indexing")
                finally:
                    if cleanup:
                        try:
                            os.unlink(cleanup)
                        except OSError:
                            pass

    # Wire embed_fn now that the model file is confirmed present.
    store = await _get_vector_store_async(request.app["state"])
    store.embed_fn = make_sync_embed_fn()

    # Build FAISS index for any existing episodic memories with embeddings.
    # Blocking disk read (can be large) — offload off the event loop.
    try:
        await asyncio.to_thread(store.load_faiss_index)
    except Exception:
        logger.exception("Failed to load FAISS index")
        _embedding_setup_status = {
            "step": "idle",
            "error": "FAISS index load failed — click Enable to retry",
        }
        return web.json_response(
            {"error": "FAISS index load failed. Click Enable to retry."},
            status=500,
        )

    # Persist config

    def _apply(data: dict) -> dict:
        memory = data.setdefault("memory", {})
        memory["embedding_provider"] = "llama_cpp"
        memory["embedding_dim"] = 1024
        memory["migrated"] = True
        return data

    try:
        await run_config_write(update_config_locked, config_path(), mutate=_apply)
    except ConfigReadError:
        # Fail closed: writing back a {} baseline would drop every other setting.
        logger.exception("Refusing to persist embedding config: config unreadable")
        _embedding_setup_status = {"step": "error", "error": "config unreadable"}
        return web.json_response(
            {"error": "failed to read config file", "code": "config_unreadable"}, status=500
        )

    # Apply migrated to running consolidator
    state = request.app["state"]
    if state.consolidator:
        state.consolidator._migrated = True
    _embedding_setup_status = {"step": "done", "error": ""}
    return web.json_response({"ok": True})


async def api_memory_disable_embeddings(request: web.Request) -> web.Response:
    """POST /api/memory/disable-embeddings — gone: embeddings are always-on.

    Kept as a graceful 410 (not a 404) because the shipped frontend still
    renders a Disable button until its companion change lands. Remove
    together with the frontend button.
    """
    return web.json_response(
        {
            "error": "Embeddings are always-on and can no longer be disabled. "
            "Memory falls back to keyword search automatically whenever the "
            "model is unavailable."
        },
        status=410,
    )


async def api_memory_episodic_search(request: web.Request) -> web.Response:
    """GET /api/memory/episodic/search?q=...&tags=t1,t2 — search episodic memories."""
    store = await _get_vector_store_async(request.app["state"])
    query = request.query.get("q", "")[:500]
    try:
        limit = min(int(request.query.get("limit", "20")), 50)
    except (ValueError, TypeError):
        limit = 20
    tag_filter = [t.strip() for t in request.query.get("tags", "").split(",") if t.strip()] or None
    # _try_embed runs blocking in-process model inference (and a ~1s model
    # load on first call); offload to keep the dashboard event loop responsive.
    emb = (
        await asyncio.to_thread(store._try_embed, query)
        if store.embed_fn and query
        else None
    )
    results = []
    # Offload: search_episodic serializes on _db_lock (#1947) — see
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
    """GET /api/memory/episodic?tags=t1,t2 — paginated list of episodic memories."""
    store = await _get_vector_store_async(request.app["state"])
    try:
        limit = min(int(request.query.get("limit", "50")), 100)
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        return web.json_response({"error": "limit/offset must be integers"}, status=400)
    tag_filter = [t.strip() for t in request.query.get("tags", "").split(",") if t.strip()] or None
    # Offload: serializes on _db_lock (#1947) — see api_memory_semantic.
    rows = await asyncio.to_thread(
        store.get_episodic_list, limit=limit, offset=offset, tag_filter=tag_filter
    )
    entries = [_redact_memory_field(dict(e)) for e in rows]
    return web.json_response({"entries": entries})


async def api_memory_episodic_delete(request: web.Request) -> web.Response:
    """DELETE /api/memory/episodic/{id} — tombstone an episodic memory."""
    state: DashboardState = request.app["state"]
    # This route had NO session check at all — not even the restricted-mode
    # one its semantic siblings carry — so any caller, restricted or forged,
    # could tombstone episodic memories. Apply the shared recognition gate
    # (#3226) plus the same live-slot restricted-mode policy as
    # ``api_memory_semantic_delete``.
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, sk, "episodic.delete",
        blocks_persisted_mode=is_incognito_transcript,
    )
    if refusal is not None:
        return refusal
    if _is_restricted_session(state, request):
        _sel().log_api_access(
            caller=sk, operation="episodic.delete", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response(
            {
                "error": "Memory writes are not allowed in this session mode.",
                "code": "restricted_session",
            },
            status=403,
        )
    store = await _get_vector_store_async(state)
    mem_id = request.match_info["id"]
    # Offload: acquires _db_lock internally (#1947) — see api_memory_semantic.
    ok = await asyncio.to_thread(store.delete_episodic, mem_id)
    if not ok:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})


async def api_memory_stats(request: web.Request) -> web.Response:
    """GET /api/memory/stats — memory system statistics."""
    store = await _get_vector_store_async(request.app["state"])
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
    from kiro_crew.memory import legacy_memory_present  # noqa: F811

    stats["has_legacy_memory"] = legacy_memory_present()
    return web.json_response(stats)


async def api_memory_migrate(request: web.Request) -> web.Response:
    """POST /api/memory/migrate — migrate legacy markdown memory to vector store."""
    store = await _get_vector_store_async(request.app["state"])

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
        state: DashboardState = request.app["state"]
        if state.consolidator:
            state.consolidator._migrated = True
    return web.json_response(counts)


async def api_memory_import(request: web.Request) -> web.Response:
    """POST /api/memory/import — import memory from JSON (export format)."""
    if _is_restricted_session(request.app["state"], request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="memory.import", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response({"error": "Memory writes are not allowed in this session mode."}, status=403)
    store = await _get_vector_store_async(request.app["state"])
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
    store = await _get_vector_store_async(request.app["state"])
    query = request.query.get("q", "")[:500]
    # Offload: the fetch serializes on _db_lock (#1947) — see api_memory_semantic.
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
    if _is_restricted_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk, operation="memory.consolidate", outcome="denied",
            source="dashboard", resources="restricted_session_block",
        )
        return web.json_response({"error": "Memory writes are not allowed in this session mode."}, status=403)
    if not state.consolidator:
        return web.json_response({"error": "consolidator not available"}, status=503)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    key = body.get("key", "").strip()
    if not key:
        return web.json_response({"error": "session key required"}, status=400)
    include_history = body.get("include_history", True)
    # Claim the key before the eligibility probe below, which awaits. Testing
    # membership and adding must happen with no yield between them: the probe
    # offloads a transcript read, and a check-then-act spanning that await lets
    # two concurrent POSTs both pass the guard and both dispatch, double-billing
    # an LLM turn on the same span. The claim is released again on every path
    # that does not hand the key to _consolidate, which discards it in its own
    # finally once the task ends.
    if key in state.consolidator._running:
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
                    await asyncio.to_thread(
                        state.consolidator._log.consolidation_counts, key
                    )
                )[0]
            except Exception:
                # No count means the extent test is skipped and the cap stands, which
                # only ever refuses a turn — never spends one on an unverified premise.
                logger.warning("Could not read message count for %s", key, exc_info=True)
        if include_history and not state.consolidator.retry_eligible(
            key, message_count=_total
        ):
            return web.json_response(
                {
                    "error": "consolidation is in retry backoff for this session",
                    "code": "consolidation_retry_backoff",
                },
                status=429,
            )
        task = asyncio.create_task(
            state.consolidator._consolidate(key, include_history)
        )
        dispatched = True
        state.consolidator._tasks.add(task)
        task.add_done_callback(state.consolidator._tasks.discard)
        return web.json_response({"ok": True, "key": key})
    finally:
        if not dispatched:
            state.consolidator._running.discard(key)


async def api_memory_observability(request: web.Request) -> web.Response:
    """GET /api/memory/observability — memory health metrics and context preview."""
    store = await _get_vector_store_async(request.app["state"])
    query = request.query.get("q", "")[:500]
    # Offload: both serialize on _db_lock (#1947) — see api_memory_semantic.
    stats = await asyncio.to_thread(store.memory_stats)
    rejections = await asyncio.to_thread(store.get_rejection_stats)
    # get_context_preview with a query embeds the query AND every non-lesson
    # semantic row (blocking urllib per row) — the worst on-loop amplification
    # in the store; offload so it can't stall the gateway event loop.
    preview = await run_in_embed_pool(store.get_context_preview, query_text=query)
    return web.json_response(
        {
            "stats": stats,
            "rejections": rejections,
            "context_preview": preview,
        }
    )


async def api_memory_promote(request: web.Request) -> web.Response:
    """POST /api/memory/promote — promote repeated episodic patterns to semantic facts."""
    store = await _get_vector_store_async(request.app["state"])
    # allow_absent: every field below has a default, so a bodyless POST is
    # legitimate. A body that is present but malformed is still a 400 -- the
    # previous `except Exception: body = {}` answered 200-with-defaults to a
    # client typo, which silently ran a different promotion than the caller
    # asked for.
    body, body_err = await read_bounded_json(request, max_bytes=None, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    try:
        min_count = int(body.get("min_count", 5))
        min_sim = float(body.get("min_sim", 0.75))
    except (ValueError, TypeError):
        return web.json_response({"error": "min_count/min_sim must be numeric"}, status=400)
    # Run in executor (can take 10+ seconds)
    loop = asyncio.get_running_loop()
    promoted = await loop.run_in_executor(None, store.promote_episodic_patterns, min_count, min_sim)
    return web.json_response({"ok": True, "promoted": promoted})


def _build_memory_graph(mem: Any, lessons: list) -> tuple[list[dict], list[dict]]:
    """Synchronous helper — safe to run in a thread."""
    import hashlib
    import re

    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: dict[str, str] = {}
    seen_ids: set[str] = set()

    def _id(prefix: str, label: str) -> str:
        return hashlib.md5(f"{prefix}:{label}".encode(), usedforsecurity=False).hexdigest()[:12]

    def _add(prefix: str, label: str, group: str, title: str = "") -> str:
        nid = _id(prefix, label)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append(
                {"id": nid, "label": label[:60], "group": group, "title": title or label}
            )
            node_ids[f"{prefix}:{label}"] = nid
        return nid

    # --- Preferences ---
    try:
        pref_text = mem.read_preferences() or ""
        for line in pref_text.splitlines():
            line = line.strip().removeprefix("- ").strip()
            if (
                line
                and not line.startswith("#")
                and not line.startswith("<!--")
                and len(line) > 5
            ):
                _add("pref", line[:80], "preference", line)
    except Exception:
        pass

    # --- Projects ---
    try:
        proj_text = mem.read_projects() or ""
        current_project = ""
        for line in proj_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                current_project = stripped[3:].strip()
                _add("proj", current_project, "project", current_project)
            elif stripped.startswith("- ") and current_project:
                detail = stripped[2:].strip()
                if len(detail) > 3:
                    detail_id = _add(
                        "proj_d", f"{current_project}: {detail[:60]}", "project", detail
                    )
                    proj_id = node_ids.get(f"proj:{current_project}")
                    if proj_id:
                        edges.append({"from": proj_id, "to": detail_id})
    except Exception:
        pass

    # --- Semantic Memory (vector store) ---
    vs = mem.vector_store
    if vs:
        try:
            for entry in vs.get_all_semantic():
                key = entry.get("key", "")
                # Lesson rows are rendered as prose by the lessons loop below;
                # adding them here too would show each lesson twice, once as a
                # dict repr of its stored mapping.
                if str(key).startswith("lesson."):
                    continue
                val = entry.get("value_json", "")
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                val_str = str(val) if not isinstance(val, str) else val
                _add("sem", key, "semantic", f"{key} = {val_str[:120]}")
        except Exception:
            pass

    # --- Lessons ---
    try:
        lessons_data = None
        try:
            lessons_data = vs.get_lessons() if vs else None
        except Exception:
            pass
        if lessons_data:
            # Deferred import: ``vector_memory`` pulls snowballstemmer plus the
            # optional numpy/faiss imports, and this helper is the handler's
            # only use of it, on one search path.
            from kiro_crew.vector_memory import _lesson_display_text

            for entry in lessons_data:
                rule = entry.get("value_json", "")
                if isinstance(rule, str):
                    try:
                        rule = json.loads(rule)
                    except Exception:
                        pass
                # Rendered text for either storage shape; fall back to str() so a
                # malformed row still surfaces in search rather than vanishing.
                text = _lesson_display_text(rule) or str(rule)
                _add("lesson", text[:80], "lesson", text)
        else:
            for le in lessons:
                _add("lesson", le.rule[:80], "lesson", le.rule)
    except Exception:
        pass

    # --- History (recent days only) ---
    try:
        hist = mem.read_recent_history(days=14) or ""
        for line in hist.splitlines():
            stripped = line.strip()
            m = re.match(r"^#{1,4}\s+(.+)", stripped)
            if m:
                raw = str(_redact_memory_field(m.group(1).strip()))
                _add("hist", raw[:80], "history", raw)
            elif stripped.startswith("[") and "]" in stripped and len(stripped) > 20:
                raw = str(_redact_memory_field(stripped))
                _add("hist", raw[:80], "history", raw[:200])
    except Exception:
        pass

    # --- Auto-detect edges by project-name mention ---
    # Match the project's SHORT name, not the FULL project header (e.g.
    # "KiroCrew (Public)"): the full header almost never occurs verbatim inside
    # node titles (~0 edges across thousands of nodes, leaving the graph with no
    # structure to lay out). The short name (leading token with any
    # parenthetical qualifier stripped: "KiroCrew (Public)" -> "kirocrew",
    # "kiro-cli (Rust)" -> "kiro-cli") is what actually shows up in semantic
    # keys, lessons, and history lines.
    def _project_short_name(full: str) -> str:
        base = re.sub(r"\(.*?\)", "", full).strip()
        parts = base.split()
        return (parts[0] if parts else base).lower()

    # Generic words that would link to a large fraction of nodes if a project
    # were literally named after one ("Web", "App", "The …"); excluded so a
    # common short name can't turn the graph back into a hairball.
    edge_stopwords = {
        "the", "and", "for", "new", "web", "app", "api", "dev", "doc", "docs",
        "test", "tests", "main", "core", "misc", "todo", "wip", "old", "tmp",
    }
    project_matchers: list[tuple[str, str]] = []
    for k in node_ids:
        if k.startswith("proj:") and ":" not in k.split(":", 1)[1]:
            short = _project_short_name(k.split(":", 1)[1])
            # Require >=3 chars and not a generic stopword to avoid noisy links.
            if len(short) >= 3 and short not in edge_stopwords:
                project_matchers.append((node_ids[k], short))

    for n in nodes:
        if n["group"] in ("preference", "semantic", "lesson", "history"):
            title_lower = n["title"].lower()
            for proj_id, short in project_matchers:
                if n["id"] == proj_id:
                    continue
                if re.search(r"\b" + re.escape(short) + r"\b", title_lower):
                    edges.append({"from": n["id"], "to": proj_id})

    return nodes, edges


async def api_memory_graph(request: web.Request) -> web.Response:
    """GET /api/memory/graph — return all memory as nodes + edges for graph visualization."""
    state: DashboardState = request.app["state"]
    mem = _get_memory(state)

    try:
        loop = asyncio.get_running_loop()
        nodes, edges = await loop.run_in_executor(
            None, _build_memory_graph, mem, state.lessons.load_all()
        )

        for n in nodes:
            n["label"] = _redact_memory_field(n["label"])
            n["title"] = _redact_memory_field(n["title"])

        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="memory_graph", outcome="success"
        )
        return web.json_response({"nodes": nodes, "edges": edges})
    except Exception:
        logging.getLogger(__name__).exception("memory_graph failed")
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="memory_graph", outcome="failure"
        )
        return web.json_response({"error": "failed to build memory graph"}, status=500)
