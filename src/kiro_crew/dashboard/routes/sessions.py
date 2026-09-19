"""Route registration for session workspace, folders, message pins, tags.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import chat, chat_voice, handlers, openai_compat


def register(app: web.Application) -> None:
    """Register the sessions routes on *app*."""
    # Session workspace (Orchestrated Chat)
    app.router.add_get("/api/sessions/{id}/agents", handlers.api_session_agents_list)
    app.router.add_get("/api/sessions/{id}/agents/{agent_id}", handlers.api_session_agent_result)
    app.router.add_get(
        "/api/sessions/{id}/agents/{agent_id}/stream", handlers.api_session_agent_stream
    )
    # Crew log: the projection paths are registered first, ahead of the range read
    # they share a prefix with, per this module's ordering rule. The batch read is
    # ahead of the per-name one so its literal path is matched before the pattern
    # that would otherwise capture "projections" as a name.
    app.router.add_get(
        "/api/sessions/{id}/crew-log/projections",
        handlers.api_session_crew_log_projections,
    )
    app.router.add_get(
        "/api/sessions/{id}/crew-log/projection/{name}",
        handlers.api_session_crew_log_projection,
    )
    app.router.add_get("/api/sessions/{id}/crew-log", handlers.api_session_crew_log)
    # The unit-keyed door the ``kirocrew-crew-log`` MCP server proxies. A separate
    # prefix from the two routes above because its authorization model is
    # different (an internal caller scoped on the session key it forwards, rather
    # than the owner's cookie), not because the data differs -- both doors call the
    # same page reader and the same fold. Literal paths before the patterned ones,
    # per this module's ordering rule.
    app.router.add_get("/api/crew-log/sessions", handlers.api_crew_log_sessions)
    app.router.add_get("/api/crew-log/resolve", handlers.api_crew_log_resolve)
    app.router.add_get(
        "/api/crew-log/units/{unit}/projection/{name}", handlers.api_crew_log_unit_projection
    )
    app.router.add_get("/api/crew-log/units/{unit}/page", handlers.api_crew_log_unit_page)
    app.router.add_get("/api/capability/mcp/registry", handlers.api_capability_mcp_registry)
    app.router.add_post("/api/chat/slots/{slot}/resume", chat.api_chat_slot_resume)
    app.router.add_post("/api/chat/slots/{slot}/approve", chat.api_chat_slot_approve)
    app.router.add_post("/api/chat/slots/{slot}/plan-action", chat.api_chat_plan_action)
    app.router.add_post("/api/chat/mode", chat.api_chat_mode)
    app.router.add_post("/api/chat/nav/resolve-links", chat.api_chat_nav_resolve_links)
    app.router.add_post("/api/chat/slots/{slot}/generate-title", chat.api_chat_slot_generate_title)
    app.router.add_patch("/api/chat/slots/{slot}/title", chat.api_chat_slot_rename)
    app.router.add_post("/api/chat/slots/{slot}/regenerate", chat.api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/switch-variant", chat.api_chat_slot_switch_variant)
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", chat.api_chat_slot_edit_resend)
    app.router.add_post("/api/chat/slots/{slot}/rewind", chat.api_chat_slot_rewind)
    # Folders
    app.router.add_get("/api/chat/folders", chat.api_chat_folders)
    app.router.add_post("/api/chat/folders", chat.api_chat_folder_create)
    # Deliberately OUTSIDE /api/chat: app API grants are prefix-matched, so a
    # pre-existing app permission for /api/chat/folders (folder CRUD) must not
    # silently inherit host-filesystem enumeration. The scaffolder app is
    # granted /api/project-scaffold and nothing else.
    app.router.add_post("/api/project-scaffold/scan", chat.api_chat_folders_scan)
    app.router.add_post("/api/project-scaffold/create", chat.api_chat_folders_scaffold)
    app.router.add_patch("/api/chat/folders/{id}", chat.api_chat_folder_update)
    # Atomic multi-folder reorder -- one transaction for a whole sidebar drag or
    # tool renumber, so a partial failure cannot leave a mix of old and new
    # order numbers. Registered BEFORE the "{id}" delete so its literal path is
    # not shadowed by the id parameter.
    app.router.add_post("/api/chat/folders/reorder", chat.api_chat_folder_reorder)
    app.router.add_delete("/api/chat/folders/{id}", chat.api_chat_folder_delete)
    app.router.add_patch("/api/chat/slots/{slot}/folder", chat.api_chat_slot_folder)
    app.router.add_patch("/api/chat/slots/{slot}/pin", chat.api_chat_slot_pin)
    app.router.add_patch("/api/chat/slots/{slot}/mode", chat.api_chat_slot_mode)
    # Message pins
    app.router.add_get("/api/chat/pins", chat.api_chat_pins_list)
    app.router.add_post("/api/chat/pins", chat.api_chat_pins_create)
    app.router.add_delete("/api/chat/pins/by-query", chat.api_chat_pins_delete_by_query)
    app.router.add_delete("/api/chat/pins/{id}", chat.api_chat_pins_delete)
    # Tags
    app.router.add_get("/api/chat/tags", chat.api_chat_tags)
    app.router.add_post("/api/chat/tags", chat.api_chat_tag_create)
    app.router.add_patch("/api/chat/tags/{id}", chat.api_chat_tag_update)
    app.router.add_delete("/api/chat/tags/{id}", chat.api_chat_tag_delete)
    app.router.add_put("/api/chat/slots/{slot}/tags", chat.api_chat_slot_tags)
    app.router.add_post("/api/chat/slots/{slot}/drop", chat.api_chat_slot_drop)
    app.router.add_get("/api/chat/tag-columns", chat.api_chat_tag_columns)
    app.router.add_post("/api/chat/tag-columns", chat.api_chat_tag_column_create)
    app.router.add_put("/api/chat/tag-columns/order", chat.api_chat_tag_columns_reorder)
    app.router.add_patch("/api/chat/tag-columns/{id}", chat.api_chat_tag_column_update)
    app.router.add_delete("/api/chat/tag-columns/{id}", chat.api_chat_tag_column_delete)
    app.router.add_post("/api/voice/synthesize", chat.api_voice_synthesize)
    app.router.add_post("/api/voice/cancel", chat_voice.api_voice_cancel)
    chat_voice.register_voice_lifecycle(app)
    app.router.add_get("/api/voice/config", chat.api_voice_config)
    app.router.add_put("/api/voice/config", chat.api_voice_config)
    app.router.add_get("/api/voice/voices", chat.api_voice_voices)
    app.router.add_get("/api/voice/system-voices", chat.api_voice_system_voices)
    app.router.add_post("/api/chat/slots/{slot}/slack-link", chat.api_chat_slot_slack_link)
    app.router.add_post("/api/chat/slots/{slot}/slack-unlink", chat.api_chat_slot_slack_unlink)
    app.router.add_post("/api/chat/slots/{slot}/slack-pause", chat.api_chat_slot_slack_pause)
    app.router.add_post("/api/chat/slots/{slot}/mirror-link", chat.api_chat_slot_mirror_link)
    app.router.add_post("/api/chat/slots/{slot}/mirror-unlink", chat.api_chat_slot_mirror_unlink)
    app.router.add_post("/api/chat/slots/{slot}/mirror-pause", chat.api_chat_slot_mirror_pause)
    app.router.add_get("/api/chat/channel-targets", chat.api_channel_targets)
    app.router.add_get("/api/slack/channels", chat.api_slack_channels)

    # OpenAI-compatible API
    app.router.add_post("/v1/chat/completions", openai_compat.api_completions)
