"""A full folder or terminal count cap answers 409 Conflict, not 429.

Each cap clears only when the user deletes a folder or closes a terminal, so
waiting never helps. 429 tells clients and proxies to retry; 409 does not. The
``error`` and ``code`` bodies stay exactly what they were.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_request,
    _make_session,
)

from kiro_crew.dashboard import chat_folders_routes
from kiro_crew.dashboard.chat_folders import MAX_CHAT_FOLDERS, api_chat_folder_create
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.dashboard.mate_folders import MateFolderError
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _folder_state(count: int) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = [
        {"id": f"fldr{i:04d}", "name": f"F{i}", "parent_id": None, "order": i} for i in range(count)
    ]
    slot = _ChatSlot("chat-1-100")
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.conversation_log = None

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        changed, value = fn(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    state.mutate_folders = _mutate
    return state


async def _create_folder(state: DashboardState) -> tuple[int, dict[str, Any]]:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "New"},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_folder_cap_full_answers_409_with_its_code() -> None:
    state = _folder_state(MAX_CHAT_FOLDERS)

    status, body = await _create_folder(state)

    assert status == 409
    assert body == {
        "error": f"folder cap reached ({MAX_CHAT_FOLDERS})",
        "code": "folder_cap_reached",
    }
    assert len(state._folders) == MAX_CHAT_FOLDERS


@pytest.mark.asyncio
async def test_mate_home_folder_cap_answers_409_with_its_code(monkeypatch) -> None:
    state = _folder_state(MAX_CHAT_FOLDERS)
    monkeypatch.setattr(chat_folders_routes, "is_mate_principal", lambda _app: True)
    monkeypatch.setattr(
        chat_folders_routes, "home_create_target", AsyncMock(side_effect=MateFolderError())
    )

    status, body = await _create_folder(state)

    assert status == 409
    assert body == {
        "error": f"folder cap reached ({MAX_CHAT_FOLDERS})",
        "code": "folder_cap_reached",
    }
    assert len(state._folders) == MAX_CHAT_FOLDERS


@pytest.mark.asyncio
async def test_terminal_create_cap_full_answers_409_with_its_code() -> None:
    registry = {"s1": _make_session(), "s2": _make_session()}
    req = _make_request(registry=registry)
    with (
        patch.object(terminal, "_get_config", return_value={"enabled": True, "max_sessions": 2}),
        patch.object(terminal, "_sel") as mock_sel,
    ):
        mock_sel.return_value.log_api_access = MagicMock()
        resp = await terminal.api_terminal_create(req)

    assert resp.status == 409
    assert json.loads(resp.body) == {"error": "Max 2 sessions", "code": "terminal_max_sessions"}
    assert set(registry) == {"s1", "s2"}


@pytest.mark.asyncio
async def test_terminal_ws_open_cap_full_answers_409_with_its_text() -> None:
    registry = {"s1": _make_session(), "s2": _make_session()}
    req = _make_request(registry=registry, session_id="new")
    with (
        patch.object(terminal, "_get_config", return_value={"enabled": True, "max_sessions": 2}),
        patch.object(terminal, "_sel") as mock_sel,
    ):
        mock_sel.return_value.log_api_access = MagicMock()
        resp = await terminal.api_terminal_ws(req)

    assert isinstance(resp, web.Response)
    assert resp.status == 409
    assert resp.text == "Max 2 terminal sessions"
    assert set(registry) == {"s1", "s2"}
