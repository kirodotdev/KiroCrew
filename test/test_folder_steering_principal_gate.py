"""Only the person may declare ``steering_dirs``.

A steering directory is a host-file read the unsandboxed gateway performs for
the folder and hands to every chat in it. Folder permission is not host-file
permission: an app or crew member that owns a folder must not be able to point
it at an arbitrary readable Markdown tree and have that read laundered into
its own model session. Both write sites refuse a non-empty list from a
non-person principal with 403 before any path is touched; clearing to ``[]``
stays allowed; the person's own calls are unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_folders as cf
from kiro_crew.dashboard.chat_folders import api_chat_folder_create, api_chat_folder_update
from kiro_crew.dashboard.state import DashboardState


def _state(folders: list[dict[str, Any]]) -> DashboardState:
    state = DashboardState.__new__(DashboardState)
    state._folders = folders
    state._tags = []
    state._slots = {}
    state.conversation_log = None
    state.push_slots_update = MagicMock()

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        changed, value = fn(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    async def _read(fn: Any) -> Any:
        return fn(state._folders)

    state.mutate_folders = _mutate
    state.read_folders = _read
    return state


def _make_app(state: DashboardState, principal: str) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = principal
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    return app


@pytest.fixture
def no_disk(monkeypatch):
    """The refusal must land BEFORE any path is validated or touched."""
    calls: list[Any] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("path validation ran for a refused principal")

    monkeypatch.setattr(cf, "_validate_steering_dirs", _boom)
    return calls


@pytest.mark.asyncio
async def test_app_cannot_declare_steering_dirs_on_create(tmp_path, no_disk):
    state = _state([])
    async with TestClient(TestServer(_make_app(state, "acme"))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Radar", "steering_dirs": [str(tmp_path)]},
        )
        assert resp.status == 403, await resp.text()
        body = await resp.json()
    assert body["code"] == "steering_dirs_forbidden"
    assert state._folders == [], "nothing was created"
    assert no_disk == []


@pytest.mark.asyncio
async def test_app_cannot_declare_steering_dirs_on_its_own_folder(tmp_path, no_disk):
    own = {"id": "f1", "name": "Radar", "parent_id": None, "order": 0, "owner_app": "acme"}
    state = _state([own])
    async with TestClient(TestServer(_make_app(state, "acme"))) as client:
        resp = await client.patch(
            "/api/chat/folders/f1",
            json={"steering_dirs": [str(tmp_path)]},
        )
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "steering_dirs_forbidden"
    assert "steering_dirs" not in state._folders[0]
    assert no_disk == []


@pytest.mark.asyncio
async def test_member_principal_is_refused_the_same_way(tmp_path, no_disk):
    state = _state([])
    async with TestClient(TestServer(_make_app(state, "member:reviewer-store"))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Reviews", "steering_dirs": [str(tmp_path)]},
        )
        assert resp.status == 403, await resp.text()
    assert no_disk == []


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", ["acme", "member:reviewer-store"])
async def test_a_tool_bound_clear_from_a_non_person_principal_is_refused(principal, no_disk):
    """``chat_folder_steering_set`` sends a caller binding. Through it a
    non-person principal may not clear steering either: only the person could
    have declared it. That holds for a member however its slot is shaped (a
    member DM, or an ordinary chat bound to the member's own store), because the
    gate keys on the resolved principal, not on the slot's mode."""
    own = {
        "id": "f1",
        "name": "Radar",
        "parent_id": None,
        "order": 0,
        "owner_app": principal,
        "steering_dirs": ["/srv/standards"],
    }
    state = _state([own])
    async with TestClient(TestServer(_make_app(state, principal))) as client:
        resp = await client.patch(
            "/api/chat/folders/f1",
            json={"steering_dirs": [], "steering_caller_generation": 1},
        )
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "steering_dirs_forbidden"
    assert state._folders[0]["steering_dirs"] == ["/srv/standards"]
    assert no_disk == []


@pytest.mark.asyncio
async def test_app_may_still_clear_steering_dirs_on_its_own_folder():
    own = {
        "id": "f1",
        "name": "Radar",
        "parent_id": None,
        "order": 0,
        "owner_app": "acme",
        "steering_dirs": ["/srv/standards"],
    }
    state = _state([own])
    async with TestClient(TestServer(_make_app(state, "acme"))) as client:
        resp = await client.patch("/api/chat/folders/f1", json={"steering_dirs": []})
        assert resp.status == 200, await resp.text()
    # The writer stores "declares none" as an absent key or an empty list.
    assert not state._folders[0].get("steering_dirs")


@pytest.mark.asyncio
async def test_app_create_without_steering_dirs_is_unaffected():
    state = _state([])
    async with TestClient(TestServer(_make_app(state, "acme"))) as client:
        resp = await client.post("/api/chat/folders", json={"name": "Radar"})
        assert resp.status == 201, await resp.text()
    assert state._folders[0]["owner_app"] == "acme"
