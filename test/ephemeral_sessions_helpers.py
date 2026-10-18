"""Shared builders for the ephemeral-session tests.

``_make_state`` builds a ``DashboardState`` over a real ``ConversationLog``,
and ``_make_app`` serves the chat-slot and lessons routes for that state.
``test_ephemeral_sessions.py`` and ``test_ephemeral_sessions_recognition.py``
both import them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _make_state(tmp_path, **kwargs):
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        # ``save_or_enrich`` returns one of inserted / enriched / unchanged, and the
        # lessons route echoes that word in its response body -- so the mock has to
        # answer with a real one rather than a MagicMock, which is not serializable.
        # Same fixture shape as ``test_api_input_validation.py``.
        lessons=MagicMock(
            load_all=MagicMock(return_value=[]),
            save_or_enrich=MagicMock(return_value="inserted"),
        ),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        **kwargs,
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _make_app(state):
    from kiro_crew.dashboard.chat import (
        api_chat_slot_create,
        api_chat_slot_delete,
        api_chat_slot_resume,
        api_chat_slots,
    )
    from kiro_crew.dashboard.handlers import api_lessons_create, api_lessons_delete

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_delete("/api/chat/slots/{slot}", api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/resume", api_chat_slot_resume)
    app.router.add_post("/api/lessons", api_lessons_create)
    app.router.add_delete("/api/lessons", api_lessons_delete)
    return app
