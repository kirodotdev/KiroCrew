"""The dashboard state, app and client builders the chat pin route tests share.

``test_dashboard_chat_pins.py``, ``test_dashboard_chat_pins_ownership.py`` and
``test_dashboard_chat_pins_load_and_broadcast.py`` import these by name: a
``DashboardState`` over mocked services, an app carrying the four pin routes, a
``TestClient`` that can pose as an app caller, a pin record builder and the
failing ``save_chat_pins`` stub.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_pins import (
    api_chat_pins_create,
    api_chat_pins_delete,
    api_chat_pins_delete_by_query,
    api_chat_pins_list,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _raise_os_error():
    """Stub for save_chat_pins that always raises."""
    raise OSError("disk full")


def _make_state(tmp_path):
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
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/pins", api_chat_pins_list)
    app.router.add_post("/api/chat/pins", api_chat_pins_create)
    app.router.add_delete("/api/chat/pins/by-query", api_chat_pins_delete_by_query)
    app.router.add_delete("/api/chat/pins/{id}", api_chat_pins_delete)
    return app


def _client(tmp_path, *, state=None, app_name: str = "") -> TestClient:
    state = state or _make_state(tmp_path)
    app = _make_app(state)
    if app_name:

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = app_name
            return await handler(request)

        app.middlewares.append(_as_app)
    return TestClient(TestServer(app))


def _pin(pin_id: str, slot_key: str, message_ts: str, *, origin_app: str = "") -> dict:
    return {
        "id": pin_id,
        "slot_key": slot_key,
        "mid": f"m-{pin_id}",
        "message_ts": message_ts,
        "role": "assistant",
        "preview": f"preview for {slot_key}",
        "pinned_at": "2026-01-01T00:00:00+00:00",
        "origin_app": origin_app,
    }
