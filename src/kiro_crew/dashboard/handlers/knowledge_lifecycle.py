"""Wiring the Knowledge Library into the dashboard app: the route table, the source
watcher, artifact auto-ingest, live config reloads and the pools' shutdown.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from kiro_crew.artifacts import get_default_store
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig, config_dir, read_config_text
from kiro_crew.dashboard.handlers.knowledge_agent_context import (
    add_agent_document_route,
    search_for_context,
)
from kiro_crew.dashboard.handlers.knowledge_bundles import export_all, export_item, import_bundle
from kiro_crew.dashboard.handlers.knowledge_embeddings import (
    batch_embed_items,
    get_embedding_status,
)
from kiro_crew.dashboard.handlers.knowledge_ingest import get_job, ingest_file, ingest_text
from kiro_crew.dashboard.handlers.knowledge_library import (
    delete_item,
    get_entity_graph,
    get_entity_items,
    get_full_graph,
    get_item,
    get_item_content,
    get_related_items,
    get_stats,
    list_entities,
    list_items,
    list_namespaces,
    source_counts,
    update_item,
)
from kiro_crew.dashboard.handlers.knowledge_sources import (
    add_source,
    confirm_source,
    delete_source,
    get_config,
    list_source_files,
    list_sources,
    pause_source,
    pick_folder,
    rename_source,
    resume_source,
    retry_file,
    skip_file,
)
from kiro_crew.dashboard.handlers.knowledge_sync import sync_source
from kiro_crew.knowledge.artifact_ingest import ArtifactKnowledgeSync
from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.connectors.base import BaseConnector
from kiro_crew.knowledge.connectors.local_folder import LocalFolderConnector
from kiro_crew.knowledge.embedder import create_embedder_from_config
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.llm_pool import DEFAULT_EXTRACTION_EFFORT, LLMPool
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.sync import SyncScheduler
from kiro_crew.knowledge.watcher import KnowledgeWatcher

logger = logging.getLogger("kiro_crew.dashboard.handlers.knowledge")


def _create_embedder(app):
    """Create embedder from Kiro Crew config. Returns None if disabled/unavailable."""
    cfg_path = config_dir() / "config.json"
    try:
        cfg = json.loads(read_config_text(cfg_path)) if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    return create_embedder_from_config(cfg)


# ---------- Source Watcher ----------


async def _start_watcher_async(app: web.Application) -> None:
    """Start the source watcher (auto-watches local_file sources)."""
    old_watcher = app.get("knowledge_watcher")
    if old_watcher:
        await old_watcher.stop()
    pipeline = app["knowledge_pipeline"]
    store = app["state"].knowledge_store

    watcher = KnowledgeWatcher(store=store, pipeline=pipeline)
    app["knowledge_watcher"] = watcher
    task = asyncio.create_task(watcher.start())
    app["_knowledge_watcher_task"] = task


async def _start_artifact_ingest_async(
    app: web.Application, cfg: KiroCrewConfig | None = None
) -> None:
    """Wire artifact -> Knowledge Library sync when auto-ingest is enabled.

    Registers an in-process change-listener on the artifact store: every
    create / content-update / delete (from the agent's MCP tools, the CLI, the
    dashboard, bookmarks, and provider pull/clone -- all of which funnel
    through the store in the gateway process) ingests or removes that
    artifact's item group in the aggregate "Artifacts" Knowledge source. Every
    start also runs a reconcile pass that ingests what the store has and the
    Library lacks and drops state for artifacts that are gone, so drift from the
    window in which this was switched off is repaired rather than left permanent.
    Gated on ``knowledge.auto_ingest_artifacts`` (off by default). See
    ``kiro_crew.knowledge.artifact_ingest`` for the full design.

    ``cfg`` is the already-loaded config when the live applier calls this with
    the document the watcher just adopted; boot passes nothing and the load runs
    off the loop, because ``KiroCrewConfig.load()`` is filesystem work.
    """
    if cfg is None:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if not cfg.knowledge.auto_ingest_artifacts:
        return
    pipeline = app["knowledge_pipeline"]
    kinds = set(cfg.knowledge.auto_ingest_artifact_kinds)
    art_store = get_default_store()
    sync = ArtifactKnowledgeSync(
        art_store=art_store,
        pipeline=pipeline,
        kinds=kinds,
        loop=asyncio.get_running_loop(),
    )
    art_store.set_change_listener(sync.on_change)
    # Hold a reference so the listener binding isn't garbage-collected.
    app["artifact_knowledge_sync"] = sync
    await sync.start()


async def _stop_artifact_ingest(app: web.Application) -> None:
    """Unregister the artifact->Library listener and drop the sync object.

    Detaching the listener is what actually stops the ingest: with it gone, an
    artifact write does not reach the pipeline. Existing Library items are
    LEFT in place -- turning auto-ingest off asks for no new ingests, not for a
    retroactive delete of documents already searchable -- and a later re-enable
    repairs the gap through the reconcile pass in
    :func:`_start_artifact_ingest_async`.
    """
    sync = app.pop("artifact_knowledge_sync", None)
    if sync is None:
        return
    try:
        get_default_store().set_change_listener(None)
    except Exception:
        logger.warning("artifact auto-ingest: listener detach failed", exc_info=True)


async def _apply_knowledge_config(app: web.Application, change: object) -> None:
    """Apply a live ``knowledge.*`` change to the running ingest stack.

    Three independent effects, each guarded so one failure cannot block the
    others:

    * ``embed_timeout_secs`` / ``embed_content_budget`` -- rebuild the embedder
      and swap it into both the app key and the pipeline. The embedder holds no
      connection, so a swap is a plain object replacement; an embed already in
      flight finishes against the old one.
    * ``auto_ingest_artifacts`` -- register or unregister the artifact listener,
      so the toggle takes effect on the next artifact write.
    * ``auto_ingest_artifact_kinds`` -- replace the kinds set on the live sync
      object, which is the only thing the change gates.
    """
    new = getattr(change, "new")
    touched = getattr(change, "touched")
    if touched("knowledge.embed_timeout_secs", "knowledge.embed_content_budget"):
        try:
            embedder = await asyncio.to_thread(_create_embedder, app)
            app["knowledge_embedder"] = embedder
            pipeline = app.get("knowledge_pipeline")
            if pipeline is not None:
                pipeline.embedder = embedder
        except Exception:
            logger.warning("knowledge embedder rebuild failed; keeping the old one", exc_info=True)
    if touched("knowledge.auto_ingest_artifacts"):
        try:
            if new.knowledge.auto_ingest_artifacts:
                if app.get("artifact_knowledge_sync") is None:
                    await _start_artifact_ingest_async(app, new)
            else:
                await _stop_artifact_ingest(app)
        except Exception:
            logger.warning("knowledge artifact auto-ingest rewire failed", exc_info=True)
    if touched("knowledge.auto_ingest_artifact_kinds"):
        sync = app.get("artifact_knowledge_sync")
        if sync is not None:
            sync.kinds = set(new.knowledge.auto_ingest_artifact_kinds)


async def _watch_knowledge_config(app: web.Application) -> None:
    """Register the knowledge appliers once the loop is running.

    An ``on_startup`` hook rather than a call from ``setup_knowledge_routes``,
    because the applier needs a running loop to re-register the artifact listener
    and the subscription must outlive the setup call. The Subscription is held on
    ``app`` so the watcher's weak reference to the bound closure stays alive for
    the app's lifetime.
    """

    async def _applier(change: object) -> None:
        await _apply_knowledge_config(app, change)

    app["_knowledge_config_sub"] = live.subscribe(
        "knowledge", callback=_applier, name="knowledge-routes"
    )


async def _shutdown_knowledge_pools(app: web.Application) -> None:
    """Shut down each workload pool once, including the legacy alias."""
    seen: set[int] = set()
    for key in (
        "knowledge_extraction_pool",
        "knowledge_fetch_pool",
    ):
        pool = app.get(key)
        if pool is None or id(pool) in seen:
            continue
        seen.add(id(pool))
        try:
            await pool.shutdown()
        except Exception:
            logger.exception("Knowledge pool shutdown failed: %s", key)


def setup_knowledge_routes(app: web.Application) -> None:
    # The detached-task registries are created here, while the app is still
    # mutable: aiohttp freezes the application when its runner starts, and a
    # first-use ``setdefault`` from a request handler is then a write to a
    # frozen app (deprecated today, an error in a later aiohttp). Other route
    # sets on the same app share ``_bg_tasks``, hence setdefault rather than
    # assignment.
    app.setdefault("_bg_tasks", set())
    app.setdefault("_scan_tasks", set())
    # Initialize pipeline and sync scheduler if not already set
    if "knowledge_pipeline" not in app:
        store = app["state"].knowledge_store
        cfg = KiroCrewConfig.load()
        extraction_pool = LLMPool(
            pool_size=cfg.knowledge.extraction_pool_size,
            effort_key="extraction_effort",
            fallback_effort=DEFAULT_EXTRACTION_EFFORT,
            config_pool_size_key="extraction_pool_size",
        )
        fetch_pool = LLMPool(pool_size=1)
        embedder = _create_embedder(app)
        pipeline = IngestionPipeline(
            store=store,
            extractor=EntityExtractor(pool=extraction_pool),
            chunker=HeadingAwareChunker(),
            reader=FileReader(),
            embedder=embedder,
        )
        app["knowledge_extraction_pool"] = extraction_pool
        app["knowledge_fetch_pool"] = fetch_pool
        app["knowledge_embedder"] = embedder
        connectors: dict[str, "BaseConnector"] = {}
        # Local folder connector (always available)
        connectors["local_folder"] = LocalFolderConnector()
        connectors["obsidian_vault"] = LocalFolderConnector()
        # Edition-contributed connectors (CPP KnowledgeProvider seam). Built-ins
        # are set FIRST so an edition can both ADD a new source_type and, if it
        # ever needs to, override a built-in. The Default returns {} → standalone
        # keeps exactly {local_folder, obsidian_vault}. Fail-closed: a
        # non-standalone host that cannot compose raises (via safe_context_call);
        # a transient adapter error degrades to built-ins only.
        from kiro_crew.platform.context import current_context, safe_context_call

        _no_extra: dict[str, "BaseConnector"] = {}

        def _extra_connectors() -> "dict[str, BaseConnector]":
            # Bind the context ONCE so the KnowledgeProvider adapter and the cfg it
            # receives come from the SAME PlatformContext (a context swap between
            # two lookups could otherwise pair an adapter with a foreign cfg).
            ctx = current_context()
            return ctx.knowledge.extra_connectors(ctx.cfg)

        connectors.update(
            safe_context_call(
                _extra_connectors,
                fallback=_no_extra,
                log_message="knowledge.extra_connectors failed; built-in connectors only",
            )
        )
        app["knowledge_pipeline"] = pipeline
        app["knowledge_sync"] = SyncScheduler(store=store, pipeline=pipeline, connectors=connectors)
        # Start source watcher (auto-watches local_file sources)
        app.on_startup.append(_start_watcher_async)
        # Start artifact ingest watcher (no-op unless auto-ingest is enabled)
        app.on_startup.append(_start_artifact_ingest_async)
        # Follow knowledge.* live: embedder tuning, artifact auto-ingest on/off,
        # and the eligible artifact kinds.
        app.on_startup.append(_watch_knowledge_config)

    app.router.add_get("/api/knowledge/config", get_config)
    app.router.add_get("/api/knowledge/items", list_items)
    app.router.add_get("/api/knowledge/namespaces", list_namespaces)
    app.router.add_get("/api/knowledge/stats", get_stats)
    app.router.add_get("/api/knowledge/sources", list_sources)
    app.router.add_get("/api/knowledge/source-counts", source_counts)
    app.router.add_post("/api/knowledge/sources", add_source)
    app.router.add_post("/api/knowledge/pick-folder", pick_folder)
    app.router.add_post("/api/knowledge/sources/{id}/sync", sync_source)
    app.router.add_post("/api/knowledge/sources/{id}/confirm", confirm_source)
    app.router.add_post("/api/knowledge/sources/{id}/pause", pause_source)
    app.router.add_post("/api/knowledge/sources/{id}/resume", resume_source)
    app.router.add_get("/api/knowledge/sources/{id}/files", list_source_files)
    app.router.add_post("/api/knowledge/sources/{id}/files/retry", retry_file)
    app.router.add_post("/api/knowledge/sources/{id}/files/skip", skip_file)
    app.router.add_post("/api/knowledge/sources/{id}/ingest-text", ingest_text)
    app.router.add_delete("/api/knowledge/sources/{id}", delete_source)
    app.router.add_patch("/api/knowledge/sources/{id}", rename_source)
    app.router.add_get("/api/knowledge/entities", list_entities)
    app.router.add_get("/api/knowledge/graph", get_full_graph)
    app.router.add_get("/api/knowledge/export", export_all)
    app.router.add_post("/api/knowledge/ingest", ingest_file)
    app.router.add_post("/api/knowledge/agent-document", add_agent_document_route)
    app.router.add_post("/api/knowledge/import", import_bundle)
    app.router.add_get("/api/knowledge/items/{id}", get_item)
    app.router.add_patch("/api/knowledge/items/{id}", update_item)
    app.router.add_delete("/api/knowledge/items/{id}", delete_item)
    app.router.add_get("/api/knowledge/items/{id}/content", get_item_content)
    app.router.add_get("/api/knowledge/items/{id}/related", get_related_items)
    app.router.add_get("/api/knowledge/items/{id}/export", export_item)
    app.router.add_get("/api/knowledge/entities/by-name/{name}/items", get_entity_items)
    app.router.add_get("/api/knowledge/entities/{id}/graph", get_entity_graph)
    app.router.add_get("/api/knowledge/jobs/{id}", get_job)
    app.router.add_get("/api/knowledge/embedding/status", get_embedding_status)
    app.router.add_post("/api/knowledge/embedding/generate", batch_embed_items)
    app.router.add_get("/api/knowledge/search-for-context", search_for_context)

    # Pool lifecycle: lazy start on first request, shutdown on app exit
    app.on_cleanup.append(_shutdown_knowledge_pools)

    async def _stop_watcher(app: web.Application) -> None:
        watcher = app.get("knowledge_watcher")
        if watcher:
            await watcher.stop()
        task = app.get("_knowledge_watcher_task")
        if task and not task.done():
            task.cancel()

    app.on_cleanup.append(_stop_watcher)
