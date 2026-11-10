"""``POST /api/chat/folders/collapse-all``: the sidebar's one-click folder reset.

One ``mutate_folders`` pass closes every folder and changes nothing else; a
caller that is not the person is refused with the store untouched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_folders import api_chat_folders_collapse_all
from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.loop_lock import LoopBoundLock


def _folders() -> list[dict[str, Any]]:
    return [
        {"id": "fldr00000001", "name": "Work", "parent_id": "", "order": 0, "collapsed": False},
        {"id": "fldr00000002", "name": "Done", "parent_id": "", "order": 1, "collapsed": True},
        {
            "id": "fldr00000003",
            "name": "Radar",
            "parent_id": "fldr00000001",
            "order": 0,
            "owner_app": "issue-radar",
            "created_by_session": "dashboard:chat-1-100",
        },
    ]


def _state(writes: list[list[dict[str, Any]]]) -> DashboardState:
    """A state whose ``mutate_folders`` runs the real repository transaction."""
    state = MagicMock(spec=DashboardState)
    state._folders = _folders()
    state._slots = {}
    state.push_slots_update = MagicMock()
    repo = FolderRepository(lambda: MagicMock())
    lock = LoopBoundLock()

    async def _mutate(fn: Any) -> Any:
        return await repo.mutate(
            lambda: state._folders,
            lock,
            fn,
            lambda: MagicMock(name="folders.json"),
            lambda _path, snapshot: writes.append(snapshot),
        )

    state.mutate_folders = _mutate
    return state


def _app(state: DashboardState, app_scope: str) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = app_scope
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders/collapse-all", api_chat_folders_collapse_all)
    return app


@pytest.mark.asyncio
async def test_one_request_collapses_every_folder_in_one_write() -> None:
    writes: list[list[dict[str, Any]]] = []
    state = _state(writes)
    async with TestClient(TestServer(_app(state, ""))) as client:
        resp = await client.post("/api/chat/folders/collapse-all")
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "count": 2}
    assert len(writes) == 1
    expected = [dict(f, collapsed=True) for f in _folders()]
    assert writes[0] == expected
    assert state._folders == expected


@pytest.mark.asyncio
async def test_an_app_is_refused_and_nothing_is_written() -> None:
    writes: list[list[dict[str, Any]]] = []
    state = _state(writes)
    async with TestClient(TestServer(_app(state, "issue-radar"))) as client:
        resp = await client.post("/api/chat/folders/collapse-all")
        assert resp.status == 403
        assert (await resp.json())["code"] == "folder_collapse_all_forbidden"
    assert writes == []
    assert state._folders == _folders()
