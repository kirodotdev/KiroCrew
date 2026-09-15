"""Route registration for task runner, diagnostics bundle, portability export/import.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import chat, handlers
from kiro_crew.dashboard.handlers import review_fix


def register(app: web.Application) -> None:
    """Register the taskrunner routes on *app*."""
    # Task runner (MCP routes via _register_mcp_routes; dashboard-only routes below)
    app.router.add_post("/api/taskrunner/plan", handlers.api_taskrunner_plan)
    app.router.add_post("/api/taskrunner/plan/cancel", handlers.api_taskrunner_plan_cancel)
    app.router.add_post("/api/taskrunner/from-chat", handlers.api_taskrunner_from_chat)
    app.router.add_delete("/api/taskrunner/{task_id}", handlers.api_taskrunner_delete)
    app.router.add_patch("/api/taskrunner/{task_id}/name", handlers.api_taskrunner_rename)
    app.router.add_patch(
        "/api/taskrunner/{task_id}/tasks/{index}", handlers.api_taskrunner_update_task
    )
    app.router.add_post("/api/taskrunner/{task_id}/retry", handlers.api_taskrunner_retry)
    app.router.add_post("/api/taskrunner/{task_id}/pause", handlers.api_taskrunner_pause)
    app.router.add_post("/api/taskrunner/{task_id}/to-chat", handlers.api_taskrunner_to_chat)
    app.router.add_get(
        "/api/taskrunner/{task_id}/plan-context", handlers.api_taskrunner_plan_context
    )
    app.router.add_get("/api/taskrunner/{task_id}/plan.yaml", handlers.api_taskrunner_export_yaml)
    app.router.add_put("/api/taskrunner/{task_id}/plan", handlers.api_taskrunner_update_plan)
    app.router.add_post("/api/taskrunner/{task_id}/execute", handlers.api_taskrunner_execute_plan)
    app.router.add_get("/api/taskrunner/{task_id}/review-fix", review_fix.api_taskrunner_review_fix)
    app.router.add_post(
        "/api/taskrunner/{task_id}/review-fix/actions",
        review_fix.api_taskrunner_review_fix_actions,
    )
    app.router.add_post("/api/reveal", handlers.api_reveal_path)
    app.router.add_get("/api/file-read", handlers.api_file_read)
    app.router.add_get("/api/file-download", handlers.api_file_download)
    app.router.add_get("/api/file-office-preview", handlers.api_file_office_preview)
    app.router.add_get("/api/file-raw", handlers.api_file_raw)
    app.router.add_get("/api/file-stream", handlers.api_file_stream)
    app.router.add_get("/api/file-watch", handlers.api_file_watch)
    app.router.add_post("/api/file-write", handlers.api_file_write)
    app.router.add_get("/api/file-diff", handlers.api_file_diff)
    app.router.add_get("/api/file-search", handlers.api_file_search)
    app.router.add_get("/api/file-sheet", handlers.api_file_sheet)
    app.router.add_get("/api/browse-dirs", handlers.api_browse_dirs)
    app.router.add_get("/api/browse-files", handlers.api_browse_files)
    app.router.add_get("/api/project/git", handlers.api_project_git)
    app.router.add_get("/api/project/git/status", handlers.api_project_git_status)
    app.router.add_get("/api/project/git/log", handlers.api_project_git_log)
    app.router.add_get("/api/project/tree", handlers.api_project_tree)
    app.router.add_post("/api/upload", handlers.api_upload)
    app.router.add_post("/api/upload/file", handlers.api_upload_file)
    app.router.add_post("/api/slack/upload-file", handlers.api_slack_upload_file)
    app.router.add_post("/api/channel/upload-file", handlers.api_channel_upload_file)
    app.router.add_post("/api/slack/pins", handlers.api_slack_pins)
    app.router.add_post("/api/slack/reactions", handlers.api_slack_reactions)
    app.router.add_post("/api/chat/slots/{name}/slack-link", chat.api_chat_slot_slack_link)
    app.router.add_post("/api/chat/slots/{name}/slack-unlink", chat.api_chat_slot_slack_unlink)
    app.router.add_post("/api/chat/slots/{name}/slack-pause", chat.api_chat_slot_slack_pause)
    app.router.add_post("/api/chat/slots/{name}/mirror-link", chat.api_chat_slot_mirror_link)
    app.router.add_post("/api/chat/slots/{name}/mirror-unlink", chat.api_chat_slot_mirror_unlink)
    app.router.add_post("/api/chat/slots/{name}/mirror-pause", chat.api_chat_slot_mirror_pause)
    app.router.add_get("/api/slack/channels", chat.api_slack_channels)
    app.router.add_post("/api/outbox/notify", handlers.api_outbox_notify)
    app.router.add_get("/api/outbox", handlers.api_outbox_list)
    app.router.add_get("/api/outbox/{filename}", handlers.api_outbox_download)
    app.router.add_post("/api/screenshot", handlers.api_screenshot)

    # Diagnostics / "Report a Problem" (redacted support bundle)
    app.router.add_post("/api/diagnostics/collect", handlers.api_diagnostics_collect)
    app.router.add_get("/api/diagnostics/download/{filename}", handlers.api_diagnostics_download)

    # Portability (export/import config+memory as zip)
    app.router.add_get("/api/portability/export", handlers.api_portability_export)
    app.router.add_post("/api/portability/import", handlers.api_portability_import)
    app.router.add_post("/api/portability/preview", handlers.api_portability_preview)
