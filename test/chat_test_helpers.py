"""Shared helpers for chat test modules."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService


class _ReadyKiroPrerequisiteService(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True


_READY_KIRO_PREREQUISITE = object.__new__(_ReadyKiroPrerequisiteService)


def _make_ready_kiro_prerequisite() -> KiroPrerequisiteService:
    """Return a filesystem-free ready prerequisite for embedded test apps."""

    return _READY_KIRO_PREREQUISITE


def _make_state(tmp_path, **kwargs):
    """Create a DashboardState with mocked services and real ConversationLog."""
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        **kwargs,
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _make_app(state: DashboardState) -> web.Application:
    """Minimal aiohttp app with chat endpoints."""
    from kiro_crew.dashboard.chat import (
        api_chat,
        api_chat_mode,
        api_chat_plan_action,
        api_chat_slot_approve,
        api_chat_slot_color,
        api_chat_slot_delete,
        api_chat_slot_detail,
        api_chat_slot_fork,
        api_chat_slot_regenerate,
        api_chat_slot_rename,
        api_chat_slot_resume,
        api_chat_slot_rewind,
        api_chat_slot_stop,
        api_chat_slot_switch_variant,
        api_chat_slots,
        api_chat_slots_cleanup,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_post("/api/chat/slots/cleanup", api_chat_slots_cleanup)
    app.router.add_get("/api/chat/slots/{slot}", api_chat_slot_detail)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post("/api/chat/slots/{slot}/stop", api_chat_slot_stop)
    app.router.add_delete("/api/chat/slots/{slot}", api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/resume", api_chat_slot_resume)
    app.router.add_patch("/api/chat/slots/{slot}/title", api_chat_slot_rename)
    app.router.add_patch("/api/chat/slots/{slot}/color", api_chat_slot_color)
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/fork", api_chat_slot_fork)
    app.router.add_post("/api/chat/slots/{slot}/rewind", api_chat_slot_rewind)
    app.router.add_post("/api/chat/slots/{slot}/switch-variant", api_chat_slot_switch_variant)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/plan-action", api_chat_plan_action)
    return app


def _make_app_with_agent_routes(state: DashboardState) -> web.Application:
    """Minimal aiohttp app with chat endpoints including agent and create routes."""
    from kiro_crew.dashboard.chat import (
        api_chat_slot_agent,
        api_chat_slot_approve,
        api_chat_slot_create,
        api_chat_slot_delete,
        api_chat_slot_detail,
        api_chat_slot_rename,
        api_chat_slot_resume,
        api_chat_slot_workspace,
        api_chat_slots,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_get("/api/chat/slots/{slot}", api_chat_slot_detail)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    app.router.add_delete("/api/chat/slots/{slot}", api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/resume", api_chat_slot_resume)
    app.router.add_patch("/api/chat/slots/{slot}/title", api_chat_slot_rename)
    return app


def _make_folder_app(state: DashboardState) -> web.Application:
    """Minimal aiohttp app with folder endpoints."""
    from kiro_crew.dashboard.chat import api_chat_slots
    from kiro_crew.dashboard.chat_folders import (
        api_chat_folder_create,
        api_chat_folder_delete,
        api_chat_folder_update,
        api_chat_folders,
        api_chat_slot_folder,
        api_chat_slot_pin,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/folders", api_chat_folders)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", api_chat_folder_delete)
    app.router.add_patch("/api/chat/slots/{slot}/folder", api_chat_slot_folder)
    app.router.add_patch("/api/chat/slots/{slot}/pin", api_chat_slot_pin)
    app.router.add_get("/api/chat/slots", api_chat_slots)
    return app


def _make_tags_app(state: DashboardState) -> web.Application:
    """Minimal aiohttp app with chat_tags endpoints (vocabulary, columns, drop, slot tags)."""
    from kiro_crew.dashboard.chat_tags import (
        api_chat_slot_drop,
        api_chat_slot_tags,
        api_chat_tag_column_create,
        api_chat_tag_column_delete,
        api_chat_tag_column_update,
        api_chat_tag_columns,
        api_chat_tag_columns_reorder,
        api_chat_tag_create,
        api_chat_tag_delete,
        api_chat_tag_update,
        api_chat_tags,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/tags", api_chat_tags)
    app.router.add_post("/api/chat/tags", api_chat_tag_create)
    app.router.add_patch("/api/chat/tags/{id}", api_chat_tag_update)
    app.router.add_delete("/api/chat/tags/{id}", api_chat_tag_delete)
    app.router.add_put("/api/chat/slots/{slot}/tags", api_chat_slot_tags)
    app.router.add_post("/api/chat/slots/{slot}/drop", api_chat_slot_drop)
    app.router.add_get("/api/chat/tag-columns", api_chat_tag_columns)
    app.router.add_post("/api/chat/tag-columns", api_chat_tag_column_create)
    app.router.add_put("/api/chat/tag-columns/order", api_chat_tag_columns_reorder)
    app.router.add_patch("/api/chat/tag-columns/{id}", api_chat_tag_column_update)
    app.router.add_delete("/api/chat/tag-columns/{id}", api_chat_tag_column_delete)
    return app


class AsyncIterator:
    """Helper to create an async iterator from a list."""

    def __init__(self, items):
        self._items = items
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item
