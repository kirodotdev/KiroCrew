"""Route registration for memory, speech-to-text, and semantic vector memory.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import handlers, stt_stream


def register(app: web.Application) -> None:
    """Register the memory routes on *app*."""
    # Memory
    app.router.add_get("/api/memory/preferences", handlers.api_memory_preferences)
    app.router.add_put("/api/memory/preferences", handlers.api_memory_preferences)
    app.router.add_get("/api/memory/projects", handlers.api_memory_projects)
    app.router.add_put("/api/memory/projects", handlers.api_memory_projects)
    app.router.add_get("/api/memory/history", handlers.api_memory_history)
    app.router.add_put("/api/memory/history", handlers.api_memory_history)
    app.router.add_get("/api/memory/settings", handlers.api_memory_settings)
    app.router.add_put("/api/memory/settings", handlers.api_memory_settings)

    # STT (Speech-to-Text)
    app.router.add_get("/api/config/stt", handlers.api_stt_config)
    app.router.add_put("/api/config/stt", handlers.api_stt_config)
    app.router.add_post("/api/stt/install", handlers.api_stt_install)
    app.router.add_post("/api/stt/transcribe", handlers.api_stt_transcribe)
    app.router.add_get("/api/ws/stt", stt_stream.api_ws_stt)

    # Vector Memory (Semantic)
    app.router.add_get("/api/memory/semantic", handlers.api_memory_semantic)
    app.router.add_put("/api/memory/semantic", handlers.api_memory_semantic_write)
    app.router.add_delete("/api/memory/semantic/{key:.+}", handlers.api_memory_semantic_delete)
    app.router.add_get("/api/memory/events", handlers.api_memory_events)
    app.router.add_get("/api/memory/embedding-status", handlers.api_memory_embedding_status)
    app.router.add_post("/api/memory/enable-embeddings", handlers.api_memory_enable_embeddings)
    app.router.add_post("/api/memory/embedding-model", handlers.api_memory_embedding_model)
    app.router.add_post("/api/memory/disable-embeddings", handlers.api_memory_disable_embeddings)
    app.router.add_get("/api/memory/episodic/search", handlers.api_memory_episodic_search)
    app.router.add_get("/api/memory/episodic", handlers.api_memory_episodic_list)
    app.router.add_delete("/api/memory/episodic/{id}", handlers.api_memory_episodic_delete)
    app.router.add_get("/api/memory/stats", handlers.api_memory_stats)
    app.router.add_post("/api/memory/migrate", handlers.api_memory_migrate)
    app.router.add_post("/api/memory/import", handlers.api_memory_import)
    app.router.add_get("/api/memory/context-preview", handlers.api_memory_context_preview)
    app.router.add_post("/api/memory/consolidate", handlers.api_memory_consolidate)
    app.router.add_get("/api/session/archive", handlers.api_session_archive_list)
    app.router.add_get("/api/session/archive/{name}", handlers.api_session_archive_read)
    app.router.add_get("/api/memory/observability", handlers.api_memory_observability)
    app.router.add_get("/api/memory/graph", handlers.api_memory_graph)
    app.router.add_post("/api/memory/promote", handlers.api_memory_promote)

    # Crons, lessons, spawn, taskrunner, send-message, notifications
    # are registered via _register_mcp_routes() above.
