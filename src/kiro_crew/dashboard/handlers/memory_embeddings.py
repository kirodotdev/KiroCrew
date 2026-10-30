"""The embedding model behind semantic memory: choosing it, reporting it, enabling it.

``POST /api/memory/embedding-model`` validates and applies a custom model and
re-embeds the vector store, ``GET /api/memory/embedding-status`` reports the download,
the active model and any re-embed in progress, ``POST /api/memory/enable-embeddings``
downloads the model and installs ``faiss-cpu`` into the gateway's interpreter, and
``POST /api/memory/disable-embeddings`` answers 410: embeddings are always on.

Served through :mod:`kiro_crew.dashboard.handlers.memory`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.registry import minimal_env
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.context import validated_cached_vector_stores
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.dashboard.handlers._shared import (
    _is_restricted_session,
    memory_startup_refusal,
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.files import (
    _PathProbeBusy,
    _probe_busy_response,
    _run_path_probe,
)
from kiro_crew.dashboard.handlers.memory_services import _get_vector_store_async, _sel
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.embeddings import (
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
    make_sync_embed_fn,
    model_download_manager,
    model_file_present,
    reconcile_store_embedding_space,
    reembed_progress,
    reset_shared_embedder,
    resolve_custom_model,
    validate_custom_model_path,
)
from kiro_crew.executors import embed_executor
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.platform_compat import isolated_python_argv, kill_and_reap
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.memory")


# Bounded because a wedged native load has no cancellation: without a deadline
# the progress tracker would sit at `applying` forever and every later apply
# would 409. Safe to bound ONLY because the candidate is gated — an abandoned
# loader publishes into an embedder we close, and close() is terminal.
_MODEL_LOAD_TIMEOUT_SECS = 600.0


# Log-line budget for pip/ensurepip stderr in the warnings below.
_PIP_STDERR_LOG_CHARS = 500


def _redact_pip_stderr(raw: bytes) -> str:
    """Redact pip/ensurepip stderr for a log line, then bound its length.

    Through the CONTEXT rather than `security.redact_and_truncate`: a pip failure
    is prime territory for a host-specific credential shape (an internal registry
    cookie, a token in an index URL), and those live in a loaded companion's
    regexes rather than in the OSS baseline. Reading the baseline here would scan a
    companion host's stderr with the weaker pass and log what it missed. The
    `_log_` spelling is the one that cannot raise, which this path needs: the
    caller is reporting a failure, and losing the report is worse than losing the
    line.

    Redact BEFORE bounding. Slicing first can cut a credential in half, and half a
    token does not match the redactors' patterns (an AWS key ID needs its full
    20 characters), so the surviving fragment would reach gateway.log verbatim. The
    character cap is for log volume, so it belongs last.
    """
    return redact_log_via_context(raw.decode(errors="replace"))[:_PIP_STDERR_LOG_CHARS]


_embedding_setup_status: dict[str, object] = {"step": "idle", "error": ""}
_faiss_install_lock = LoopBoundLock()


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


async def _write_embed_model_config(
    path: str, dim: int
) -> tuple[Callable[[], Awaitable[None]], bool]:
    """Return a conditional rollback and inheritance from the locked prior settings."""
    import uuid

    path = str(Path(path).expanduser()) if path else ""
    rebuild_generation = uuid.uuid4().hex

    def _record_model() -> tuple[str, list[int]]:
        model = Path(path)
        stamp = _model_file_stamp(model)
        model_id = _custom_model_id(model, "")
        if _model_file_stamp(model) != stamp:
            raise OSError("embedding model changed while recording its identity")
        return model_id, list(stamp)

    model_id, stamp = await asyncio.to_thread(_record_model) if path else ("", [])
    keys = (
        "embed_model_path",
        "embed_model_id",
        "embed_model_stamp",
        "embed_model_legacy_ids",
        "embedding_dim",
    )
    previous: dict[str, Any] = {}
    written: dict[str, Any] = {}

    def _apply(data: dict) -> dict:
        memory = data.setdefault("memory", {})
        previous.update({key: memory[key] for key in keys if key in memory})
        memory.pop("embed_model_legacy_ids", None)
        memory["embed_rebuild_generation"] = rebuild_generation
        if path:
            memory["embed_model_path"] = path
            memory["embed_model_id"] = model_id
            memory["embed_model_stamp"] = stamp
        else:
            memory.pop("embed_model_path", None)
            memory.pop("embed_model_id", None)
            memory.pop("embed_model_stamp", None)
        if dim > 0:
            memory["embedding_dim"] = dim
        written.update({key: memory[key] for key in keys if key in memory})
        return data

    try:
        await run_config_write(update_config_locked, config_path(), mutate=_apply, fsync=True)
    except ConfigReadError as exc:
        raise ValueError(
            "config.json could not be parsed; fix it before changing the model"
        ) from exc

    async def rollback() -> None:
        def restore(data: dict) -> dict:
            memory = data.get("memory", {})
            if {key: memory[key] for key in keys if key in memory} != written or memory.get(
                "embed_rebuild_generation"
            ) != rebuild_generation:
                raise ValueError("Model settings changed during apply; rollback refused")
            # Keep the repair request: some stores may already be invalidated,
            # while closed stores still need it even when the model rolls back.
            for key in keys:
                memory.pop(key, None)
            memory.update(previous)
            return data

        await run_config_write(update_config_locked, config_path(), mutate=restore)

    return rollback, bool(legacy_embedding_ids(previous.get("embed_model_legacy_ids")))


def _store_progress_adapter(
    prog: ReembedProgress, base: int, total: int
) -> Callable[[int, int], None]:
    """Map one store's ``(done, total)`` backfill stream onto the multi-store bar.

    ``backfill_missing_embeddings`` reports per-batch progress for ONE store, and
    resets ``done`` to 0 between its lesson and episodic phases (each phase has
    its own denominator). The dashboard bar counts every store's work against
    one total, so this adapter offsets the store's stream by ``base`` (rows
    completed by earlier stores), folds a phase reset into a running offset
    instead of letting the bar jump backward, and never lets the reported total
    fall below what has already been counted. The caller reconciles the exact
    count after the store finishes; this only keeps the bar moving meanwhile.
    """
    phase_base = 0
    last = 0
    high_water = base

    def report(done: int, _phase_total: int) -> None:
        nonlocal phase_base, last, high_water
        done = max(0, done)
        if done < last:
            # A new phase started (lessons -> episodes): keep what the previous
            # phase already counted rather than restarting from zero.
            phase_base += last
        last = done
        current = max(high_water, base + phase_base + done)
        high_water = current
        prog.advance(current, max(total, current))

    return report


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
    3. Persist the verified path, digest and measured width before clearing any
       vectors. A failed write leaves the stores intact.
    4. Retarget and reconcile every store. On failure, conditionally restore the
       prior model settings before resetting the candidate; refuse rollback if
       an owner changed those settings meanwhile.
    5. ACTIVATE only after every store accepts the configured space.
    6. Backfill with progress, which is what the dashboard indicator renders.
    """
    prog = reembed_progress()
    candidate_installed = False
    restore_config: Callable[[], Awaitable[None]] | None = None
    # Hoisted above the try: the catch-all handler below calls _restore_dim(), so
    # it must be defined even when the failure lands before the retarget.
    stores = tuple(dict.fromkeys((store, *validated_cached_vector_stores())))
    previous_dims = {target: target._embedding_dim for target in stores}  # type: ignore[attr-defined]
    retargeted: set[object] = set()

    def _restore_dim() -> None:
        """Undo the width retarget so the store matches the model being restored.

        Retargeting without restoring on failure is worse than the failure itself:
        the store would expect the NEW width while the rebuilt backend is the OLD
        model, so backfill's per-row shape check and build_faiss_index' width
        check reject every vector — and reconcile has already NULLed the corpus —
        leaving memory keyword-only for the rest of the process lifetime.
        """
        for target in retargeted:
            target.set_embedding_dim(previous_dims[target])  # type: ignore[attr-defined]

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
            raw = str(candidate)
            install_shared_embedder(build_gated_candidate(candidate))
        else:
            # Reverting to the bundled model takes the SAME gated path. Its width
            # is known, but the file is a download and can be absent — persisting
            # the revert before proving it loads would discard a working custom
            # configuration with nothing to fall back to.
            install_shared_embedder(build_gated_bundled())
        candidate_installed = True
        # From here the outgoing model is not authoritative. Anything already
        # inside _try_embed produced its vector in the old space; the store's
        # generation guard drops those instead of committing them behind the
        # reconcile. Bumped for EVERY swap, including same-width ones, which a dim
        # comparison alone would miss.
        for target in stores:
            target.begin_space_change()  # type: ignore[attr-defined]

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

        # Digest and persistence failures must leave stored vectors intact.
        # The candidate stays gated until every store accepts the new space.
        fut = asyncio.run_coroutine_threadsafe(_write_embed_model_config(raw, embedder.dim), loop)
        restore_config, inherited_vectors = fut.result()
        if raw and _read_memory_config().get("embed_model_id") != embedder.model_id:
            raise OSError("model weights changed during apply; retry the model change")

        # Include stores opened while loading or persisting the candidate.
        stores = tuple(dict.fromkeys((*stores, *validated_cached_vector_stores())))
        for target in stores:
            previous_dims.setdefault(target, target._embedding_dim)  # type: ignore[attr-defined]

        # _restore_dim() reads this from the enclosing scope at call time.
        for target in stores:
            target.embed_fn = make_sync_embed_fn()  # type: ignore[attr-defined]
            retargeted.add(target)
            reconcile_store_embedding_space(target)  # type: ignore[arg-type]

        # Reconcile DELIBERATELY does not stamp the signature when it could not
        # unlink the stale FAISS pair (read-only memory dir; Windows while the
        # index is mapped — both named in its own comment). Ignoring that would
        # let this report "Re-embedding complete" for a store that was never
        # reconciled, and the next start's load_faiss_index() prefers the
        # surviving OLD-space pair. The recorded space is the observable.
        unreconciled = [
            target
            for target in stores
            if target.recorded_embedding_space() != active_embedding_space_signature()  # type: ignore[attr-defined]
            or target.recorded_rebuild_generation() != embedding_rebuild_generation()  # type: ignore[attr-defined]
        ]
        if unreconciled:
            raise RuntimeError(
                "the old vector index could not be removed; check permissions "
                "on the memory directory and retry"
            )

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
        # Count resolved NULL work across all three kinds. Concurrent edits or
        # forgetting can resolve work too, so this is not an inference counter.
        generation = embedding_rebuild_generation()
        total = sum(target.embedding_repair_state(generation)[1] for target in stores)  # type: ignore[attr-defined]
        prog.begin_run(total)
        completed = 0
        for target in stores:
            before = target.embedding_repair_state(generation)[1]  # type: ignore[attr-defined]
            # Per-batch progress within this store, offset by the stores already
            # done. Without it the bar sits at 0/total for the whole sweep on the
            # common single-store install and only moves once the store finishes.
            target.backfill_missing_embeddings(  # type: ignore[attr-defined]
                progress=_store_progress_adapter(prog, completed, total), pace=False
            )
            after = target.embedding_repair_state(generation)[1]  # type: ignore[attr-defined]
            # Reconcile against the store's own NULL count, exactly as before: the
            # adapter cannot see semantic-KV rows (that phase reports no progress)
            # and must not count rows a concurrent writer resolved.
            completed += max(0, before - after)
            prog.advance(completed, max(total, completed))
        prog.finish(completed)
    except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard, never crashes the app
        logger.warning("Applying the embedding model failed", exc_info=True)
        if candidate_installed and not embedding_backend_serving():
            try:
                if restore_config is not None:
                    asyncio.run_coroutine_threadsafe(restore_config(), loop).result()
            except Exception:
                logger.exception("Model configuration rollback failed; candidate remains gated")
                prog.fail("Model configuration rollback failed; fix model settings and reapply")
                return
            reset_shared_embedder()
            _restore_dim()
        prog.fail(str(exc) or exc.__class__.__name__)


def _probe_model_path(raw: str) -> tuple[str, str, int]:
    """``(error, code, size_bytes)`` for a model path the request named.

    Blocking: ``validate_custom_model_path`` stats the path and the size is one
    more stat, on a mount the caller chose. So the handler runs this on the
    bounded request-path probe pool, in one hop, never on the event loop.
    """
    path, error, code = validate_custom_model_path(raw, "The model path")
    if error:
        return error, code, 0
    try:
        return "", "", path.stat().st_size
    except OSError:
        return "", "", 0


async def api_memory_embedding_model(request: web.Request) -> web.Response:
    """POST /api/memory/embedding-model — validate and apply a custom model.

    Body: ``{"path": "<absolute path to .gguf>", "validate_only": bool}``.
    An empty ``path`` reverts to the bundled model.

    The dimension is NOT taken from the caller: it is read off the loaded model
    (``n_embd``), so the user cannot get it wrong and the UI needs no dim field.

    Owner-gated first: an apply rewrites the owner's config and re-embeds the
    owner's whole vector store.
    """
    owner_denied = await require_owner_dashboard_request(request, "memory.embedding_model")
    if owner_denied is not None:
        return owner_denied
    state: DashboardState = request.app["state"]
    if _is_restricted_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk,
            operation="memory.embedding_model",
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
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
        try:
            error, code, size_bytes = await _run_path_probe(_probe_model_path, raw)
        except _PathProbeBusy:
            return _probe_busy_response(resource=raw, operation="memory.embedding_model")
        if error:
            return web.json_response({"ok": False, "error": error, "code": code}, status=400)
    if validate_only:
        return web.json_response({"ok": True, "size_bytes": size_bytes})

    startup_refusal = memory_startup_refusal()
    if startup_refusal is not None:
        return startup_refusal

    # KIROCREW_EMBED_MODEL_PATH wins over memory.embed_model_path for the PATH,
    # but resolve_custom_model() always reads memory.embedding_dim from CONFIG.
    # So applying a model here while the env override is set would persist THIS
    # model's width against the ENV's path — a pair _load_model refuses on the
    # width check, leaving the working env-pinned model unloadable on
    # every restart until config.json is hand-edited. Refuse instead: with the
    # env override in force a config write cannot take effect anyway.
    if os.environ.get("KIROCREW_EMBED_MODEL_PATH", "").strip():
        return web.json_response(
            {
                "ok": False,
                "error": "KIROCREW_EMBED_MODEL_PATH is set, so it overrides the configured "
                "path — unset it to change the model from here",
                "code": "env_override_active",
            },
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
            {
                "ok": False,
                "error": "vector memory is unavailable",
                "code": "vector_store_unavailable",
            },
            status=503,
        )

    if prog.is_active():
        # Single-flight: a second apply mid-re-embed would race the first over
        # the same rows and the same FAISS file. Checked once, AFTER the
        # awaited store acquisition — the acquisition can yield to the loop, so
        # a pre-await check could go stale before begin_apply();
        # and whenever an apply is active, a prior apply already resolved the
        # store, so the acquisition above was the free sync fast path. Checked
        # BEFORE the SEL audit so a refused apply is not logged as allowed.
        return web.json_response(
            {
                "error": "a model change is already being applied",
                "code": "model_change_in_progress",
            },
            status=409,
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
    task = loop.run_in_executor(embed_executor(), _apply_embedding_model, store, raw, loop)
    # Retain the future so it is not garbage-collected mid-apply.
    state._embed_model_apply_task = task  # type: ignore[attr-defined]
    return web.json_response({"ok": True, "size_bytes": size_bytes, "status": "applying"})


def _embedding_repair_status(state: object, generation: str) -> dict[str, object]:
    """Inspect open handles only; closed or unreadable stores remain deferred."""
    from kiro_crew.context import cached_vector_store_entries
    from kiro_crew.memory_stores import active_store_names, require_memory_store

    result: dict[str, object] = {
        "generation": generation,
        "scope": "open_stores",
        "pending_invalidation": 0,
        "pending_vectors": 0,
        "deferred_stores": 0,
        "unknown_scope": False,
    }
    if not generation:
        return result
    try:
        cfg = KiroCrewConfig.load()
        if cfg.degraded_sections:
            result["unknown_scope"] = True
        names = set(active_store_names())
        handles = dict(cached_vector_store_entries())
        global_store = getattr(
            getattr(getattr(state, "context_builder", None), "memory", None), "vector_store", None
        )
        if global_store is not None:
            handles["default"] = global_store
        pending = remaining = deferred = 0
        for name in names:
            store = handles.get(name)
            if store is None:
                deferred += 1
                continue
            try:
                require_memory_store(name, config=cfg)
                needs_invalidation, nulls = store.embedding_repair_state(generation)
                pending += int(needs_invalidation)
                remaining += nulls
            except Exception:
                deferred += 1
                result["unknown_scope"] = True
        result.update(
            pending_invalidation=pending, pending_vectors=remaining, deferred_stores=deferred
        )
    except Exception:
        result["unknown_scope"] = True
    return result


async def api_memory_embedding_status(request: web.Request) -> web.Response:
    """GET /api/memory/embedding-status — embedding system status + setup progress."""
    embedder = get_shared_embedder()
    mgr = model_download_manager()
    step = str(mgr.status["step"])
    model_present = model_file_present()
    custom = resolve_custom_model()

    # The vendored llama.cpp runtime load result, read the way ``kirocrew
    # doctor`` reads it: the import failed AND vendored libs ship for this
    # platform. A failed import does NOT recover in-process
    # (``_load_llama_class`` is lru_cached on None), so once it has failed the
    # model cannot produce a vector for the life of the gateway, no matter that
    # the model file is present. Without this the payload below could not tell
    # that fault apart from a healthy gateway whose model is merely idle. The
    # ``_platform_libs_dirname() is not None`` conjunct excludes the designed
    # unsupported-platform degradation, where no libs ship and keyword search is
    # expected rather than a fault. Offloaded because the first call per process
    # runs the one bounded import attempt (every later call is the cached read);
    # the status handler must not block the event loop on it.
    def _runtime_load_failed() -> bool:
        return _load_llama_class() is None and _platform_libs_dirname() is not None

    runtime_failed = await asyncio.to_thread(_runtime_load_failed)

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

    memory_config = await asyncio.to_thread(_read_memory_config)
    generation = embedding_rebuild_generation(memory_config)
    repair = await asyncio.to_thread(
        _embedding_repair_status, getattr(request, "app", {}).get("state"), generation
    )
    inherited = bool(
        custom is not None and legacy_embedding_ids(memory_config.get("embed_model_legacy_ids"))
    )
    if custom is not None and setup_error:
        error_code = custom.error_code or "model_path_unreadable"
    elif setup_error:
        error_code = "model_download_failed"
    elif runtime_failed:
        # The model file is present (or none is needed) and no download or
        # custom-path fault owns the error slot, yet the runtime cannot load, so
        # memory is keyword-only for the life of the process. Report it the way a
        # model-file fault is reported — a code plus plain text naming the cost —
        # following the ``model_error_*`` pattern. ``kirocrew doctor`` has the
        # per-platform remedy (missing native libs, the Windows MSVC runtime, an
        # unusable LLAMA_CPP_LIB_PATH override); the Memory tab's job is to stop
        # the fault reading as a healthy idle model.
        #
        # ``setup_step`` is deliberately LEFT as the download manager reports it
        # (``idle`` here) rather than forced to ``error``: ``error`` is a
        # download-flow state the enabling card renders with a "check network"
        # hint, which is the wrong remedy for a runtime load failure. The fault
        # surfaces through ``server_healthy``/``setup_error_code``/``setup_error``
        # instead, which the card renders without the download copy.
        error_code = "model_error_runtime_load"
        setup_error = (
            "The in-process embedding runtime failed to load, so memory uses "
            "keyword search meanwhile. Run 'kirocrew doctor' for the cause."
        )
    else:
        error_code = ""
    progress = reembed_progress().snapshot()
    if (
        generation
        and progress["step"] in ("idle", "done")
        and any(
            repair[key]
            for key in (
                "pending_invalidation",
                "pending_vectors",
                "deferred_stores",
                "unknown_scope",
            )
        )
    ):
        progress["step"] = "deferred"
    return web.json_response(
        {
            # Embeddings are always-on; this field is not a toggle.
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
            # load happens automatically on first embed. EXCEPT when the runtime
            # import has failed: that load will never happen this process (the
            # loader's result is cached on None), so a present model file no
            # longer makes embeddings "ready to lazily activate" — report
            # unhealthy, matching the doctor row.
            "server_healthy": bool(
                (model_present or embedder.is_ready())
                and not (custom is not None and custom.error)
                and not runtime_failed
            ),
            "needs_docker": False,
            "docker_available": True,
            "setup_step": setup_step,
            "download_step": step,
            "download_attempt": mgr.status["attempt"],
            "bytes_downloaded": mgr.status.get("bytes_downloaded", 0),
            "bytes_total": mgr.status.get("bytes_total", 0),
            "setup_error": setup_error,
            "setup_error_code": error_code,
            "setup_error_params": {
                "path": str(custom.path) if custom is not None else "",
                "error": setup_error,
            },
            "setup_warning": (
                LEGACY_EMBEDDING_WARNING
                if inherited
                else str(_embedding_setup_status.get("warning", ""))
            ),
            "setup_warning_code": "legacy_embedding_vectors" if inherited else "",
            "setup_warning_params": {},
            "repair": repair,
            "model_active": embedding_backend_serving() and embedder.is_ready(),
            "can_retry": can_retry,
            # Live re-embed progress for the Memory tab indicator. Same
            # in-memory pattern as the download status above, so the card's
            # existing 2s poll picks it up with no new endpoint.
            "reembed": progress,
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
            isolated_python_argv("-m", "ensurepip", "--upgrade"),
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
            # don't leak secrets to pip subprocesses (same reason and same
            # helper as the sibling pip spawn in apps/backend_runtime/provisioning.py): `standard`
            # mode scrubs only _SENSITIVE_ENV_PREFIXES, and on a host where no
            # launcher runs at all nothing else strips the gateway's channel
            # tokens or owner id from a child that executes packaging code.
            env=minimal_env(),
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            await kill_and_reap(proc)
            logger.warning("ensurepip bootstrap timed out")
            return False, "pip bootstrap (ensurepip) timed out"
        if proc.returncode != 0:
            logger.warning("ensurepip bootstrap failed: %s", _redact_pip_stderr(stderr))
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
    """POST /api/memory/enable-embeddings — trigger/retry model download and wire embeddings.

    Owner-gated first: setup downloads a model onto the owner's host, installs
    ``faiss-cpu`` into the gateway interpreter and rewrites the owner's config.
    """
    global _embedding_setup_status

    owner_denied = await require_owner_dashboard_request(request, "memory.enable_embeddings")
    if owner_denied is not None:
        return owner_denied

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
    faiss_warning = ""
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
                    isolated_python_argv(
                        "-m",
                        "pip",
                        "install",
                        "-q",
                        "faiss-cpu",
                        "--only-binary=:all:",
                    ),
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
                sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
                try:
                    proc = await create_subprocess_limited(
                        *sandboxed_argv,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        # don't leak secrets to pip subprocesses. The allowlist
                        # keeps what an install needs (PATH, HOME, TMPDIR,
                        # PYTHONPATH, VIRTUAL_ENV, XDG_CACHE_HOME) and drops
                        # proxy/CA/PIP_* hints, so a host that reaches PyPI only
                        # through an env-configured proxy installs faiss from
                        # pip.conf (HOME is kept) or by hand -- faiss is an
                        # accelerator and recall falls back to the stdlib cosine
                        # path, which is the cheaper side of this trade.
                        env=minimal_env(),
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
                            {"error": "faiss-cpu install timed out."},
                            status=500,
                        )
                    if proc.returncode != 0:
                        # Same fall-through as the no-sandbox branch; the reason
                        # reaches the card via embedding-status's setup_warning.
                        reason = _redact_pip_stderr(stderr)
                        logger.warning("faiss-cpu install failed: %s", reason)
                        tail = reason.strip().splitlines()[-1:] or ["no compatible wheel"]
                        faiss_warning = f"faiss install failed: {tail[0][:200]}"
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

    # Persist config.
    #
    # The width comes off the LIVE backend, never a literal. `_load_model` refuses a
    # model whose own `n_embd` disagrees with `memory.embedding_dim`, so persisting a
    # fixed 1024 while a 768- or 1536-wide model is active makes that model
    # unloadable on every later restart — a breakage nobody sees until the next boot
    # and which needs a hand-edit of config.json to undo. `dim` is set when the
    # backend is CONSTRUCTED, so reading it costs no model load.
    try:
        active_dim = int(get_shared_embedder().dim)
    except Exception:
        logger.exception("Refusing to persist embedding config: active vector width unreadable")
        _embedding_setup_status = {"step": "error", "error": "embedding width unreadable"}
        return web.json_response(
            {
                "error": "could not read the active embedding width",
                "code": "embedding_dim_unreadable",
            },
            status=500,
        )
    if active_dim <= 0:
        # Fail loudly rather than substituting a default: a non-positive width means
        # the backend has not settled on one, and guessing here is exactly the
        # persistent mismatch this read exists to prevent.
        logger.error("Refusing to persist embedding config: active vector width is %d", active_dim)
        _embedding_setup_status = {"step": "error", "error": "embedding width unreadable"}
        return web.json_response(
            {
                "error": "the active embedding width is not usable",
                "code": "embedding_dim_unreadable",
            },
            status=500,
        )

    def _apply(data: dict) -> dict:
        memory = data.setdefault("memory", {})
        memory["embedding_provider"] = "llama_cpp"
        memory["embedding_dim"] = active_dim
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
    _embedding_setup_status = {"step": "done", "error": "", "warning": faiss_warning}
    return web.json_response({"ok": True})


async def api_memory_disable_embeddings(request: web.Request) -> web.Response:
    """POST /api/memory/disable-embeddings — gone: embeddings are always-on.

    A 410 (not a 404) compatibility stub for older clients that still post
    here. The current frontend renders no Disable button and never calls it.
    """
    return web.json_response(
        {
            "error": "Embeddings are always-on and can no longer be disabled. "
            "Memory falls back to keyword search automatically whenever the "
            "model is unavailable."
        },
        status=410,
    )
