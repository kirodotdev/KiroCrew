"""The dashboard state, app and client builders the chat reaction route tests share.

Mirrors ``dashboard_chat_pins_helpers.py``: a ``DashboardState`` over mocked
services, an app carrying the three reaction routes, a ``TestClient`` that can
pose as an app caller, and a reaction record builder.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_reactions import (
    api_chat_reactions_create,
    api_chat_reactions_delete_by_query,
    api_chat_reactions_list,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _raise_os_error(*args, **kwargs):
    """Stub for save_chat_reactions that always raises.

    Accepts the optional staged-records argument the persist-before-publish
    save path now passes.
    """
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
    app.router.add_get("/api/chat/reactions", api_chat_reactions_list)
    app.router.add_post("/api/chat/reactions", api_chat_reactions_create)
    app.router.add_delete("/api/chat/reactions", api_chat_reactions_delete_by_query)
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


def _seed_reactions(state, reactions: list[dict]) -> None:
    """Inject reactions as if already loaded from disk.

    Sets the loaded flag so the handlers' lazy ``ensure_chat_reactions_loaded``
    does not re-read an empty on-disk file and wipe the injected records.
    """
    state._chat_reactions = reactions
    state._chat_reactions_loaded = True


def _reaction(
    reaction_id: str, slot_key: str, mid: str, emoji: str, *, origin_app: str = ""
) -> dict:
    return {
        "id": reaction_id,
        "slot_key": slot_key,
        "mid": mid,
        "emoji": emoji,
        "reacted_at": "2026-01-01T00:00:00+00:00",
        "origin_app": origin_app,
    }
