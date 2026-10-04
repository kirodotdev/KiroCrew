"""Memory preparation, repair, embeddings and migration around boot.

The one gateway-lifetime preparation worker behind ``MemoryStartup`` and its fence,
the paced member-store repair loop, in-process embeddings with the model download,
and the one-shot legacy markdown-to-vector migration with its re-embed sweep.

The service construction itself (``_init_services``) stays in the facade: the
memory-store seam and hot-reload audits read it there.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import (
        Any,
        GatewayOrchestrator,
        asyncio,
        embed_executor,
        embedding_model_is_custom,
        get_shared_embedder,
        logger,
        maintenance_executor,
        make_sync_embed_fn,
        model_file_present,
        peek_ready_shared_embedder,
        reconcile_store_embedding_space,
        reembed_progress,
        shutdown_event,
        start_background_model_download,
        store_embedding_space_is_stale,
    )


def _initialize_memory_worker(self: GatewayOrchestrator) -> bool:
    """Restore and open the already-wired memory objects after readiness."""
    from kiro_crew.config.loader import data_home
    from kiro_crew.context import reset_memory_caches
    from kiro_crew.cron_service.identity import migrate_legacy_member_schedules
    from kiro_crew.memory_backup import apply_pending_member_restores
    from kiro_crew.memory_stores import repair_legacy_member_stores

    startup = self._memory_startup
    if startup is None:
        return False
    try:
        with startup.worker():
            try:
                assert self.ctx_builder is not None
                memory = self.ctx_builder.memory
                reset_memory_caches(memory)
                # The gateway's one run of the pre-identity member store
                # upgrade. The CLI prologue skips `gateway` because the boot
                # path admits no new work before the dashboard socket accepts
                # requests; this worker runs after readiness, before pending
                # restores and before any consumer resolves a member. A no-op
                # when nothing needs repair; never raises.
                upgraded = repair_legacy_member_stores()
                if upgraded:
                    logger.info("Upgraded member memory stores: %s", ", ".join(upgraded))
                # Then the schedules those stores' members own, once, before
                # the scheduler arms (it waits on this barrier). Same store
                # directory the gateway's CronService uses; never raises.
                captured = migrate_legacy_member_schedules(data_home())
                if captured:
                    logger.info(
                        "Captured the member execution of %d pre-identity schedule(s)",
                        len(captured),
                    )
                if startup.stopped:
                    return False
                restored = apply_pending_member_restores(
                    should_stop=lambda: startup.stopped, on_error=startup.fail_store
                )
                if startup.stopped:
                    return False
                if "default" not in startup.store_errors:
                    try:
                        memory.init()
                        self.vector_memory.init()
                        indexed = memory.rebuild_index()
                        logger.info("Global memory ready: FTS indexed %d files", indexed)
                    except Exception as exc:
                        startup.fail_store("default", exc)
                        self.vector_memory.close()
                if restored:
                    logger.info("Activated memory restores for %s", ", ".join(restored))
                if startup.store_errors:
                    logger.error(
                        "Memory preparation completed with unavailable stores: %s",
                        ", ".join(startup.store_errors),
                    )
                return startup.complete()
            finally:
                if startup.stopped:
                    # Stop cannot cancel a worker thread. Close its late
                    # handle before worker() releases the startup barrier.
                    self.vector_memory.close()
    except Exception as exc:
        startup.fail(exc)
        logger.error("Memory startup failed; memory operations remain unavailable", exc_info=True)
        return False


def _stop_memory_startup(self: GatewayOrchestrator) -> None:
    """Called off-loop; an in-flight initializer owns its own final close."""
    self._memory_repair_stop.set()
    startup = self._memory_startup
    if startup is not None and startup.stop():
        vector = getattr(self, "vector_memory", None)
        if vector is not None:
            vector.close()
        startup.release()


def _start_memory_after_ready(self: GatewayOrchestrator) -> None:
    """Start migrations and repair only after preparation and readiness."""
    startup = self._memory_startup
    if startup is None or startup.stopped or not startup.ready:
        return
    if "default" not in startup.store_errors and self._auto_migrate_task is None:
        self._auto_migrate_task = asyncio.create_task(self._auto_migrate_memory())
        self._background_tasks.add(self._auto_migrate_task)
        self._auto_migrate_task.add_done_callback(self._background_tasks.discard)
    if self._memory_repair_task is None:
        self._memory_repair_task = asyncio.create_task(self._repair_member_memory())
        self._background_tasks.add(self._memory_repair_task)
        self._memory_repair_task.add_done_callback(self._background_tasks.discard)


def _schedule_memory_preparation(self: GatewayOrchestrator) -> "asyncio.Task[None] | None":
    """Publish the one restore/open task without yielding to its worker."""
    if self._memory_startup is None:
        return None
    if self._memory_startup_task is None:

        async def initialize() -> None:
            import contextvars

            from kiro_crew.executors import memory_preparation_executor

            # Its own thread, not asyncio.to_thread: the loop's default executor
            # is shared by every boot task, and admission stays closed while
            # this job waits for a free slot there. The context copy keeps
            # to_thread's contextvars behaviour.
            pool = memory_preparation_executor()
            run = contextvars.copy_context().run
            try:
                await asyncio.get_running_loop().run_in_executor(
                    pool, run, self._initialize_memory_worker
                )
            except asyncio.CancelledError:
                await asyncio.to_thread(self._stop_memory_startup)
                raise
            finally:
                # A worker still running after cancellation keeps its thread
                # until it returns; stop() above already told it to close.
                pool.shutdown(wait=False)

        task = asyncio.create_task(initialize())
        self._memory_startup_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
    dashboard_state = self.dashboard_state
    if dashboard_state is not None:
        dashboard_state.memory_startup_task = self._memory_startup_task
    return self._memory_startup_task


async def _wait_for_memory_preparation(self: GatewayOrchestrator) -> bool:
    """Keep consumers dormant during recovery while observing owner shutdown."""
    from kiro_crew.memory_startup import MemoryStartupUnavailable, require_memory_prepared

    task = self._schedule_memory_preparation()
    if task is None:
        return True
    startup = self._memory_startup
    if startup is None:
        raise MemoryStartupUnavailable(
            "Memory preparation has no lifecycle owner. Restart the gateway."
        )
    stopping = asyncio.create_task(shutdown_event.wait())
    try:
        done, _ = await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if stopping in done or shutdown_event.is_set():
            startup.stop()
            return False
        await task
        if startup.stopped:
            return False
        try:
            require_memory_prepared()
        except MemoryStartupUnavailable:
            # A failed pass has no consumers to start. Keep its owner
            # recovery shell reachable until the owner requests shutdown.
            logger.error("Memory preparation failed; dashboard recovery remains available")
            await stopping
            startup.stop()
            return False
    except asyncio.CancelledError:
        startup.stop()
        task.cancel()
        raise
    finally:
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
    # Cancelling the event waiter yielded once more. Observe a stop that
    # arrived during its cleanup before run() starts any consumers.
    if shutdown_event.is_set():
        startup.stop()
        return False
    return not startup.stopped


def _repair_member_memory_once(self: GatewayOrchestrator) -> None:
    """Visit one already-open memory store using the ready shared model."""
    from kiro_crew.context import cached_vector_store_entries
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, require_memory_store

    if (
        self._memory_repair_stop.is_set()
        or reembed_progress().is_active()
        or peek_ready_shared_embedder() is None
    ):
        return
    entries_by_name = dict(cached_vector_store_entries())
    global_store = getattr(self, "vector_memory", None)
    if global_store is not None:
        # Global is wired directly onto the gateway rather than into the
        # named-store cache. Prefer that active handle if a malformed cache
        # ever repeats the reserved identity; repair must never open a store.
        entries_by_name[DEFAULT_MEMORY_STORE] = global_store
    entries = sorted(entries_by_name.items())
    if not entries:
        return
    name, store = next(
        (entry for entry in entries if entry[0] > self._memory_repair_cursor), entries[0]
    )
    # Advance before validation/inference so a broken member cannot starve
    # healthy members. Cache access never creates or opens another store.
    self._memory_repair_cursor = name
    require_memory_store(name)
    if name == DEFAULT_MEMORY_STORE:
        migration = self._auto_migrate_task
        if migration is not None and not migration.done():
            # The boot migration owns Global until both its migration and
            # full repair phases finish. It runs on another executor, so a
            # standing repair here would otherwise mutate the same store.
            return
    if self._memory_repair_stop.is_set():
        return
    reconcile_store_embedding_space(store)
    if not store.has_pending_embeddings() and not store_embedding_space_is_stale(store):
        return
    if store.embed_fn is None:
        store.embed_fn = make_sync_embed_fn()
    store.backfill_missing_embeddings(
        max_rows_per_kind=16, should_stop=self._memory_repair_stop.is_set
    )


async def _repair_member_memory(self: GatewayOrchestrator) -> None:
    """Run one paced repair loop across already-open V1 and V2 stores."""
    loop = asyncio.get_running_loop()
    while not self._memory_repair_stop.is_set():
        try:
            await loop.run_in_executor(embed_executor(), self._repair_member_memory_once)
        except asyncio.CancelledError:
            self._memory_repair_stop.set()
            raise
        except Exception:
            if not self._memory_repair_stop.is_set():
                logger.warning("Memory embedding repair deferred", exc_info=True)
        await asyncio.sleep(30)


async def _start_embeddings(self: GatewayOrchestrator) -> None:
    """Wire in-process embeddings and kick background model download.

    The embed_fn_factory is wired unconditionally so that _try_embed()
    lazily rebinds embed_fn once the model file lands — no gateway
    restart required. If the model is already present (common case after
    first boot), embed_fn is bound immediately.
    """
    self.vector_memory.embed_fn_factory = make_sync_embed_fn
    if model_file_present():
        self.vector_memory.embed_fn = make_sync_embed_fn()
        logger.info("In-process embeddings ready (model already present)")
    elif embedding_model_is_custom():
        # No download will fix this — the operator has to correct the path.
        # resolve_custom_model() already logged the specific reason.
        logger.warning(
            "Custom embedding model is not usable — memory falls back to keyword "
            "search. Run 'kirocrew doctor' for the reason."
        )
    else:
        logger.info(
            "Embedding model not yet present — downloading in background; "
            "memory falls back to keyword search until ready"
        )
    self._model_download_task = start_background_model_download()


async def _auto_migrate_memory(self: GatewayOrchestrator) -> None:
    """Migrate legacy markdown memory into the vector store, then backfill.

    Runs once at boot as a fire-and-forget background task. Two idempotent
    phases, all blocking work offloaded to the maintenance executor so the
    event loop is never stalled:

      1. Migrate (gated on ``memory.migrated`` being False): parse legacy
         markdown/lessons via ``migrate_from_markdown``, flip
         ``memory.migrated`` to True (even for a fresh install with zero
         legacy entries, so everyone lands in vector-only mode), sync the
         live consolidator, and acknowledge via an audit event + log line.
      2. Re-embed sweep (independent of phase 1): embed any episodic rows
         written without a vector (migrated before the model landed) and
         rebuild the FAISS index. Self-healing across boots. Gated on a cheap
         non-loading probe FIRST — nothing pending and a current vector space
         means the sweep returns without loading the embedding model at all,
         so a steady-state boot never pays its ~1GB RSS. Only once there is
         work does it wait on model readiness.

    Never raises: any failure is logged and leaves ``migrated`` unchanged so
    the next boot retries. Boot survives regardless.
    """
    from kiro_crew.memory import legacy_memory_present
    from kiro_crew.memory_startup import memory_store_startup_error

    if memory_store_startup_error():
        logger.warning("Global memory migration deferred until owner recovery and restart")
        return

    # Every dereference lives inside the try so the "never raises" contract
    # above holds even on a boot where ``_init_services`` never ran (or was
    # stubbed): this is a fire-and-forget task, so an escaping exception is
    # only surfaced later as an unretrieved-task error, far from its cause.
    loop = asyncio.get_running_loop()
    try:
        # ``Any``: mypy checks the nested sweep below without this function's
        # ``is None`` narrowing.
        store: Any = getattr(self, "vector_memory", None)
        if store is None:
            logger.debug("auto-migrate skipped: vector memory not initialised")
            return
        # Reconcile BEFORE phase 1 when the backend is ALREADY usable. A ready
        # backend makes migration write real vectors, and write_episodic's
        # FAISS dedup search would then query an index built at the previous
        # model's dimensionality — faiss raises on the mismatch, which aborts
        # migration AND phase 2, so the store would never reconcile, on every
        # boot. No waiting here on purpose: when the backend is NOT ready,
        # migration writes NULL vectors and skips the FAISS search entirely,
        # so there is nothing to reconcile ahead of, and waiting would delay
        # first-boot migration behind the model download.
        if get_shared_embedder().is_ready():
            await loop.run_in_executor(
                maintenance_executor(), reconcile_store_embedding_space, store
            )
        # ── Phase 1: migrate ──
        if not self._cfg.memory.migrated:
            # Bind embed_fn so migration writes real vectors when the model
            # is already present; otherwise rows are written NULL and the
            # sweep below (or a later boot) backfills them.
            if store.embed_fn is None and model_file_present():
                store.embed_fn = make_sync_embed_fn()

            had_legacy = await loop.run_in_executor(maintenance_executor(), legacy_memory_present)
            counts = {"semantic": 0, "episodic": 0, "skipped": 0}
            if had_legacy:
                counts = await loop.run_in_executor(
                    maintenance_executor(), store.migrate_from_markdown
                )
            # Flip the flag for everyone (fresh installs included) so the
            # app enters vector-only mode and stops writing markdown.
            await self._set_memory_migrated(True)
            self._cfg.memory.migrated = True
            if self.consolidator is not None:
                self.consolidator._migrated = True
            summary = (
                f"semantic={counts['semantic']} episodic={counts['episodic']} "
                f"skipped={counts['skipped']}"
            )
            try:
                store._log_event("migration", "system", "auto_migrate", None, summary, "auto")
            except Exception:
                logger.debug("auto-migrate audit log failed", exc_info=True)
            logger.info("Auto-migrated legacy memory: %s", summary)

        # ── Phase 2: re-embed sweep ──
        # Wait (non-blocking to boot — we are our own task) for the model, so
        # rows written NULL during phase 1 get vectors.
        if not model_file_present() and self._model_download_task is not None:
            try:
                await self._model_download_task
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("model download task errored", exc_info=True)
        # Gate on EMBEDDER READINESS, not on the bundled GGUF being on disk.
        # model_file_present() is a proxy that only means anything for the
        # file-backed llama.cpp backend: a backend installed via
        # register_embedding_backend() (remote endpoint, ONNX, ...) can be
        # ready with no local file at all, and gating on the file would leave
        # it outside this block entirely — so its foreign vectors would never
        # be reconciled. Readiness is the property actually required here.

        def _wait_then_backfill() -> int:
            # Probe for work BEFORE touching the embedder. wait_ready() below
            # calls _kick_background_load(), which mmaps the ~700MB GGUF and
            # allocates its KV/compute buffers — the single largest chunk of
            # gateway RSS. A boot with nothing to embed would otherwise pay
            # all of it for a sweep that embeds zero rows. Both probes here are
            # non-loading: has_pending_embeddings() is three LIMIT-1 SELECTs,
            # and store_embedding_space_is_stale() compares signatures built
            # from model_id/dim, which are set when the backend is CONSTRUCTED.
            # Deliberately NOT reconcile_store_embedding_space(): that one is
            # destructive and refuses to clear against an unready backend, so
            # it is the wrong tool for a question asked before the load.
            has_pending = getattr(store, "has_pending_embeddings", None)
            # A store without the probe (a stub, a foreign implementation) is
            # treated as having work rather than silently losing its sweep.
            pending = has_pending() if callable(has_pending) else True
            if not pending and not store_embedding_space_is_stale(store):
                logger.debug(
                    "Re-embed sweep: no rows pending and the stored vector space "
                    "is current — leaving the embedding model unloaded"
                )
                return 0
            embedder = get_shared_embedder()
            # wait_ready() is on the llama.cpp backend but not the
            # EmbeddingBackend ABC (a swapped-in backend may not support
            # blocking-wait); fall back to is_ready() when absent.
            wait_ready = getattr(embedder, "wait_ready", None)
            ready = wait_ready(timeout=120) if callable(wait_ready) else embedder.is_ready()
            if not ready:
                logger.info(
                    "Embedding model not ready within timeout; deferring "
                    "re-embed sweep to a later boot"
                )
                return 0
            if store.embed_fn is None:
                store.embed_fn = make_sync_embed_fn()
            # Reconcile BEFORE the sweep: a model change clears stale vectors
            # to NULL and the same sweep re-embeds them in one pass. Routed
            # through the shared chokepoint so every process that opens a
            # store reconciles identically (see reconcile_store_embedding_space).
            reconcile_store_embedding_space(store)
            return store.backfill_missing_embeddings()

        # embed_executor(), NOT maintenance_executor(): pacing turns this
        # from a ~72-minute worst case into a multi-hour one, and mc-maint is
        # a 4-worker pool documented as "reserved for the FAST periodic
        # sweeps + overlay rewrites" — parking one of its four slots
        # (mostly asleep) for a working day contradicts that reservation.
        # mc-embed is the bulkhead built for exactly this: its
        # rationale is that embed work "queues behind ITSELF instead of
        # starving" everything else, it has 8 workers, and the same
        # atexit shutdown hook already covers it. Interactive embeds are
        # unaffected either way — LlamaCppEmbedder serializes every call
        # onto one owned inference thread, so the model lock is the
        # bottleneck there, not a pool slot.
        await loop.run_in_executor(embed_executor(), _wait_then_backfill)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Auto-migration failed; will retry next boot", exc_info=True)


async def _set_memory_migrated(self: GatewayOrchestrator, value: bool) -> None:
    """Persist ``memory.migrated`` to config.json (config-lock guarded)."""
    from kiro_crew.dashboard.handlers.memory import _set_migrated

    await _set_migrated(value)
