"""Route registration for chat slots, resume, optimizer, follow-up cards, context injection.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import chat, handlers, session_transfer
from kiro_crew.dashboard.handlers.source_providers import (
    api_issue_source,
    api_pull_request_auto_merge,
    api_pull_request_checks,
    api_pull_request_comment,
    api_pull_request_pending_review,
    api_pull_request_ready,
    api_pull_request_reply,
    api_pull_request_resolve,
    api_pull_request_source,
    api_pull_request_status,
    api_pull_request_submit_review,
    api_pull_request_unresolve,
)
from kiro_crew.dashboard.handlers.worktree import api_worktree_create


def register(app: web.Application) -> None:
    """Register the chat routes on *app*."""
    # Chat
    app.router.add_post("/api/chat", chat.api_chat)
    app.router.add_post("/api/source/pull-request", api_pull_request_source)
    app.router.add_post("/api/source/pull-request/checks", api_pull_request_checks)
    app.router.add_post("/api/source/pull-request/status", api_pull_request_status)
    app.router.add_post("/api/source/pull-request/resolve", api_pull_request_resolve)
    app.router.add_post("/api/source/pull-request/unresolve", api_pull_request_unresolve)
    app.router.add_post("/api/source/pull-request/reply", api_pull_request_reply)
    app.router.add_post("/api/source/pull-request/comment", api_pull_request_comment)
    app.router.add_post("/api/source/pull-request/auto-merge", api_pull_request_auto_merge)
    app.router.add_post("/api/source/pull-request/ready", api_pull_request_ready)
    app.router.add_post("/api/source/pull-request/pending-review", api_pull_request_pending_review)
    app.router.add_post("/api/source/pull-request/submit-review", api_pull_request_submit_review)
    app.router.add_post("/api/source/issue", api_issue_source)
    app.router.add_get("/api/chat/slots", chat.api_chat_slots)
    app.router.add_post("/api/chat/slots", chat.api_chat_slot_create)
    app.router.add_post("/api/chat/slots/cleanup", chat.api_chat_slots_cleanup)
    app.router.add_post("/api/chat/slots/model", chat.api_chat_slots_model)
    # Static segment BEFORE the {slot} routes below, matching the cleanup/model
    # precedent: aiohttp resolves in registration order, so a later
    # ``/api/chat/slots/{slot}`` POST would otherwise shadow this path.
    app.router.add_post("/api/chat/slots/import", session_transfer.api_chat_slot_import)
    app.router.add_get("/api/chat/slots/{slot}", chat.api_chat_slot_detail)
    app.router.add_get("/api/chat/slots/{slot}/summary", chat.api_chat_slot_summary)
    # Same path, POST: reading a summary must stay free of side effects, so
    # generating one is a separate verb rather than a query flag on the GET.
    app.router.add_post("/api/chat/slots/{slot}/summary", chat.api_chat_slot_summary_generate)
    app.router.add_get("/api/chat/slots/{slot}/source-links", chat.api_chat_slot_source_links)
    app.router.add_post("/api/chat/slots/{slot}/stop", chat.api_chat_slot_stop)
    app.router.add_post("/api/chat/slots/{slot}/interrupt", chat.api_chat_slot_interrupt)
    app.router.add_post("/api/chat/slots/{slot}/end-wait", chat.api_chat_slot_end_wait)
    # Deliberately NOT /resume — that path is already taken by "open a history
    # session into a tab" (api_chat_slot_resume) and means something else.
    app.router.add_post("/api/chat/slots/{slot}/continue", chat.api_chat_slot_continue)
    app.router.add_delete(
        "/api/chat/slots/{slot}/queue/{queue_id}", chat.api_chat_slot_queue_cancel
    )
    app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", chat.api_chat_slot_queue_edit)
    app.router.add_put("/api/chat/slots/{slot}/queue/order", chat.api_chat_slot_queue_reorder)
    app.router.add_delete("/api/chat/slots/{slot}", chat.api_chat_slot_delete)
    app.router.add_post(
        "/api/chat/slots/{slot}/reset-conversation", chat.api_chat_slot_reset_conversation
    )
    app.router.add_post("/api/chat/slots/{slot}/agent", chat.api_chat_slot_agent)

    # Optimizer
    app.router.add_post("/api/optimizer/optimize", handlers.handle_optimize)
    app.router.add_post("/api/chat/slots/{slot}/model", chat.api_chat_slot_model)
    app.router.add_get("/api/chat/slots/{slot}/autocompact", chat.api_chat_slot_autocompact)
    app.router.add_post("/api/chat/slots/{slot}/autocompact", chat.api_chat_slot_autocompact)
    app.router.add_post(
        "/api/chat/slots/{slot}/reasoning-effort", chat.api_chat_slot_reasoning_effort
    )
    app.router.add_post("/api/chat/slots/{slot}/workspace", chat.api_chat_slot_workspace)
    app.router.add_post("/api/chat/slots/{slot}/reload", chat.api_chat_slot_reload)
    app.router.add_post("/api/chat/slots/{slot}/project", chat.api_chat_slot_project)
    # Follow-up suggestion card (suggest_followup MCP tool -> card below composer)
    app.router.add_post("/api/chat/slots/{slot}/followup", chat.api_chat_slot_followup)
    app.router.add_post("/api/worktree/create", api_worktree_create)
    app.router.add_get("/api/recent-projects", chat.api_recent_projects)
    app.router.add_patch("/api/chat/slots/{slot}/color", chat.api_chat_slot_color)
    # Context injection (App Kit — silent background context)
    app.router.add_post("/api/chat/slots/{slot}/context", chat.api_chat_slot_context)
    # Note — visible transcript line + silent next-turn context, no LLM turn
    app.router.add_post("/api/chat/slots/{slot}/note", chat.api_chat_slot_note)
    app.router.add_post("/api/chat/slots/{slot}/fork", chat.api_chat_slot_fork)
    app.router.add_post("/api/chat/slots/{slot}/side/open", handlers.api_side_open)
    app.router.add_post("/api/chat/slots/{slot}/side/turn", handlers.api_side_turn)
    app.router.add_post("/api/chat/slots/{slot}/side/close", handlers.api_side_close)
    app.router.add_delete(
        "/api/chat/slots/{slot}/side/queue/{queue_id}",
        handlers.api_side_queue_cancel,
    )
    app.router.add_patch(
        "/api/chat/slots/{slot}/side/queue/{queue_id}",
        handlers.api_side_queue_edit,
    )
