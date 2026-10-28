"""Verify api_chat_slot_stop broadcasts parent_stopped after stop_slot_turn."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web


@pytest.fixture
def _make_request():
    """Build a fake aiohttp request whose app["state"] is a mock."""

    def _build(*, slot_name="slot-1", app_token="", force=False):
        state = MagicMock()
        state._slots = {slot_name: MagicMock()}
        state.broadcast_ws = MagicMock()

        app = {"state": state}
        request = MagicMock(spec=web.Request)
        request.app = app
        request.__getitem__ = lambda self, key: app[key]
        request.get = lambda key, default="": app_token if key == "app" else default
        request.match_info = {"slot": slot_name}
        request.query = {"force": "true"} if force else {}
        return request, state

    return _build


@pytest.mark.asyncio
async def test_stop_broadcasts_parent_stopped(_make_request):
    """The Stop button route should broadcast parent_stopped after stop."""
    request, state = _make_request()

    with (
        patch(
            "kiro_crew.dashboard.chat_handlers.deny_app_slot_access",
            return_value=None,
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers._app_cancel_denied",
            return_value=None,
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers._cancel_target",
            return_value="sess-1",
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers.stop_slot_turn",
            new_callable=AsyncMock,
            return_value={"ok": True, "info": "stopped"},
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers.slot_not_found",
            return_value=web.json_response({"error": "not found"}, status=404),
        ),
    ):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        resp = await api_chat_slot_stop(request)
        assert resp.status == 200
        state.broadcast_ws.assert_called_once_with("parent_stopped", {"slot": "slot-1"})


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_break_stop(_make_request):
    """If broadcast_ws raises, the stop should still succeed."""
    request, state = _make_request()
    state.broadcast_ws.side_effect = RuntimeError("ws down")

    with (
        patch(
            "kiro_crew.dashboard.chat_handlers.deny_app_slot_access",
            return_value=None,
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers._app_cancel_denied",
            return_value=None,
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers._cancel_target",
            return_value="sess-1",
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers.stop_slot_turn",
            new_callable=AsyncMock,
            return_value={"ok": True, "info": "stopped"},
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers.slot_not_found",
            return_value=web.json_response({"error": "not found"}, status=404),
        ),
    ):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        resp = await api_chat_slot_stop(request)
        assert resp.status == 200  # broadcast failed but stop succeeded


@pytest.mark.asyncio
async def test_no_broadcast_on_slot_not_found(_make_request):
    """Missing slot returns 404 and should not broadcast."""
    request, state = _make_request(slot_name="nonexistent")
    state._slots = {}  # no slots

    with patch(
        "kiro_crew.dashboard.chat_handlers.slot_not_found",
        return_value=web.json_response({"error": "not found"}, status=404),
    ):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        resp = await api_chat_slot_stop(request)
        assert resp.status == 404
        state.broadcast_ws.assert_not_called()
