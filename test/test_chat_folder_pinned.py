"""``PATCH /api/chat/folders/{id}`` ``pinned``: the folder pin the sidebar reads.

A pinned folder's sessions stay listed while the sidebar's status chips, tag
chips or folder checkboxes narrow the list. The flag lives on the folder, server
side like the session pin, so the same browser-independent state reaches every
client. Absent means not pinned: a folder written before the field existed
reads unpinned, and unpinning drops the key rather than storing ``False``.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state


@pytest.fixture
def state(tmp_path, monkeypatch) -> Any:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


async def _create(client: TestClient, name: str) -> dict[str, Any]:
    resp = await client.post("/api/chat/folders", json={"name": name})
    assert resp.status == 201, await resp.text()
    return await resp.json()


async def _listed(client: TestClient, fid: str) -> dict[str, Any]:
    resp = await client.get("/api/chat/folders")
    assert resp.status == 200
    return next(f for f in await resp.json() if f["id"] == fid)


@pytest.mark.asyncio
async def test_a_new_folder_is_not_pinned(state) -> None:
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        folder = await _create(client, "work")
        assert "pinned" not in folder
        assert "pinned" not in await _listed(client, folder["id"])


@pytest.mark.asyncio
async def test_pinning_stores_the_flag_and_the_list_returns_it(state) -> None:
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        folder = await _create(client, "work")
        resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"pinned": True})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["pinned"] is True
        assert (await _listed(client, folder["id"]))["pinned"] is True
        stored = next(f for f in state._folders if f["id"] == folder["id"])
        assert stored["pinned"] is True


@pytest.mark.asyncio
async def test_unpinning_drops_the_key(state) -> None:
    """``False`` is never stored: absent is the one representation of unpinned."""
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        folder = await _create(client, "work")
        await client.patch(f"/api/chat/folders/{folder['id']}", json={"pinned": True})
        resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"pinned": False})
        assert resp.status == 200, await resp.text()
        assert "pinned" not in await resp.json()
        stored = next(f for f in state._folders if f["id"] == folder["id"])
        assert "pinned" not in stored


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["false", "true", 1, 0, None, [True]])
async def test_a_non_boolean_is_a_400_not_a_coercion(state, raw) -> None:
    """``"false"`` is truthy, so coercing it would pin on a request that asked to unpin."""
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        folder = await _create(client, "work")
        resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"pinned": raw})
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "pinned_invalid"
        stored = next(f for f in state._folders if f["id"] == folder["id"])
        assert "pinned" not in stored


@pytest.mark.asyncio
async def test_an_app_cannot_pin_its_own_folder(tmp_path) -> None:
    """The pin overrides the person's filters, so only the person sets it."""
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.chat_folders import api_chat_folder_update
    from kiro_crew.dashboard.state import DashboardState

    own = {"id": "f1", "name": "Radar", "parent_id": None, "order": 0, "owner_app": "acme"}
    st = DashboardState.__new__(DashboardState)
    st._folders = [own]
    st._tags = []
    st._slots = {}
    st.conversation_log = None
    st.push_slots_update = MagicMock()

    async def _mutate(fn, on_committed=None):
        changed, value = fn(st._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    st.mutate_folders = _mutate

    @web.middleware
    async def _publish_app(request, handler):
        request["app"] = "acme"
        return await handler(request)

    app = web.Application(middlewares=[_publish_app])
    app["state"] = st
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch("/api/chat/folders/f1", json={"pinned": True})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "pinned_forbidden"
    assert "pinned" not in st._folders[0]


@pytest.mark.asyncio
async def test_other_fields_leave_the_pin_alone(state) -> None:
    """A rename or a collapse toggle must not clear a pin it did not mention."""
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        folder = await _create(client, "work")
        await client.patch(f"/api/chat/folders/{folder['id']}", json={"pinned": True})
        resp = await client.patch(
            f"/api/chat/folders/{folder['id']}", json={"name": "renamed", "collapsed": True}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["name"] == "renamed"
        assert body["pinned"] is True
