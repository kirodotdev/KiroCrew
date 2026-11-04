"""Only the person may pin a folder's ``default_crew``.

The field chooses the machine a folder's new chats start on, with that
machine's agents, files and credentials. Folder permission is not that
permission: an app, an admitted crew member or an MCP tool call on the internal
credential that may edit a folder must not be able to send the person's next
New chat in it to another machine. Neither may an allow-listed non-owner person,
whom the peer control plane refuses a crew session directly. Both write sites
refuse a non-empty value from anyone but the dashboard owner with 403 and write
nothing; clearing to ``""`` stays allowed; the owner's own calls are unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

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


def _make_app(
    state: DashboardState,
    principal: str = "",
    *,
    internal: bool = False,
    user: str = "local-app",
) -> web.Application:
    """Publish claims the way ``token_auth_middleware`` does.

    A dashboard (person) caller carries ``app=""`` and its token subject in
    ``user``; ``local-app`` is the local owner's subject, any other subject is an
    allow-listed non-owner.
    """
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_principal(request: web.Request, handler: Any) -> Any:
        request["app"] = principal
        request["user"] = user
        if internal:
            request["internal_auth"] = True
        return await handler(request)

    app.middlewares.append(_publish_principal)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    return app


@pytest.mark.asyncio
async def test_app_cannot_pin_a_crew_on_create():
    state = _state([])
    async with TestClient(TestServer(_make_app(state, "acme"))) as client:
        resp = await client.post(
            "/api/chat/folders", json={"name": "Radar", "default_crew": "box-1"}
        )
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "default_crew_forbidden"
    assert state._folders == [], "nothing was created"


@pytest.mark.asyncio
async def test_app_cannot_pin_a_crew_on_its_own_folder():
    own = {"id": "f1", "name": "Radar", "parent_id": None, "order": 0, "owner_app": "acme"}
    state = _state([own])
    async with TestClient(TestServer(_make_app(state, "acme"))) as client:
        resp = await client.patch(
            "/api/chat/folders/f1", json={"name": "Radar 2", "default_crew": "box-1"}
        )
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "default_crew_forbidden"
    # The refused request writes nothing, not even the fields beside it.
    assert state._folders[0]["name"] == "Radar"
    assert "default_crew" not in state._folders[0]


@pytest.mark.asyncio
async def test_member_and_internal_callers_are_refused_the_same_way():
    for app in (
        _make_app(_state([]), "member:reviewer-store"),
        _make_app(_state([]), internal=True),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Reviews", "default_crew": "box-1"}
            )
            assert resp.status == 403, await resp.text()
            assert (await resp.json())["code"] == "default_crew_forbidden"


@pytest.mark.asyncio
async def test_app_may_still_clear_the_crew_on_its_own_folder():
    own = {
        "id": "f1",
        "name": "Radar",
        "parent_id": None,
        "order": 0,
        "owner_app": "acme",
        "default_crew": "box-1",
    }
    state = _state([own])
    async with TestClient(TestServer(_make_app(state, "acme"))) as client:
        resp = await client.patch("/api/chat/folders/f1", json={"default_crew": ""})
        assert resp.status == 200, await resp.text()
    assert state._folders[0]["default_crew"] == ""


@pytest.mark.asyncio
async def test_the_person_pins_a_crew_as_before():
    state = _state([{"id": "f1", "name": "Payments", "parent_id": None, "order": 0}])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch("/api/chat/folders/f1", json={"default_crew": "box-1"})
        assert resp.status == 200, await resp.text()
        resp = await client.post(
            "/api/chat/folders", json={"name": "Ledger", "default_crew": "box-2"}
        )
        assert resp.status in (200, 201), await resp.text()
    by_name = {f["name"]: f for f in state._folders}
    assert by_name["Payments"]["default_crew"] == "box-1"
    assert by_name["Ledger"]["default_crew"] == "box-2"


@pytest.mark.asyncio
async def test_a_non_owner_dashboard_user_cannot_pin_a_crew():
    """An allow-listed non-owner signs in with ``app=""`` like the owner does.

    ``folder_principal`` reads that as the person, so only the owner check
    tells them apart: without it this caller could point the owner's next New
    chat at a crew the peer control plane would refuse them directly.
    """
    folder = {"id": "f1", "name": "Payments", "parent_id": None, "order": 0}
    state = _state([dict(folder)])
    app = _make_app(state, user="guest@example.com")
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch(
            "/api/chat/folders/f1", json={"name": "Payments 2", "default_crew": "box-1"}
        )
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "default_crew_forbidden"
        resp = await client.post(
            "/api/chat/folders", json={"name": "Ledger", "default_crew": "box-1"}
        )
        assert resp.status == 403, await resp.text()
    assert state._folders == [folder], "neither request wrote anything"


@pytest.mark.asyncio
async def test_a_non_owner_may_still_clear_the_crew():
    pinned = {
        "id": "f1",
        "name": "Payments",
        "parent_id": None,
        "order": 0,
        "default_crew": "box-1",
    }
    state = _state([pinned])
    app = _make_app(state, user="guest@example.com")
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch("/api/chat/folders/f1", json={"default_crew": ""})
        assert resp.status == 200, await resp.text()
    assert state._folders[0]["default_crew"] == ""
