"""Only the person may declare ``steering_dirs``.

A steering directory is a host-file read the unsandboxed gateway performs for
the folder and hands to every chat in it. Folder permission is not host-file
permission: an app or crew member that owns a folder must not be able to point
it at an arbitrary readable Markdown tree and have that read laundered into
its own model session. Both write sites refuse a non-empty list from a
non-person principal with 403 before any path is touched; clearing to ``[]``
stays allowed; the person's own calls are unchanged.

"The person" means the owner or a teammate. A dashboard session that is not
the owner's (a channel allowlist link) holds the person's empty principal, so
both sites also run the owner gate, and on an update that covers clearing too.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import pinned_fs
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


# ── dashboard sessions that are not the owner's ──────────────────────────────
#
# A Telegram, Teams or Webex allowlist link (and any other dashboard session
# whose subject is not the owner) carries the same empty app claim as the
# owner's own browser, so ``folder_principal`` names it the person and the
# principal refusal above admits it. The owner gate is what tells them apart.

OWNER = "U0OWNER"
LINK_USER = "U0LINKUSER"


def _make_dashboard_app(state: DashboardState, user: str) -> web.Application:
    state.owner_id = OWNER
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_user(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = user
        return await handler(request)

    app.middlewares.append(_publish_user)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    return app


@pytest.mark.asyncio
async def test_link_session_cannot_declare_steering_dirs_on_create(tmp_path, no_disk):
    state = _state([])
    async with TestClient(TestServer(_make_dashboard_app(state, LINK_USER))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Notes", "steering_dirs": [str(tmp_path)]},
        )
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "owner_only"
    assert state._folders == [], "nothing was created"
    assert no_disk == []


@pytest.mark.asyncio
@pytest.mark.parametrize("new_dirs", [["/srv/elsewhere"], []], ids=["set", "clear"])
async def test_link_session_cannot_change_steering_dirs_on_an_owner_folder(new_dirs, no_disk):
    stored = ["/srv/standards"]
    folder = {"id": "f1", "name": "Org", "parent_id": None, "order": 0, "steering_dirs": stored}
    state = _state([folder])
    async with TestClient(TestServer(_make_dashboard_app(state, LINK_USER))) as client:
        resp = await client.patch("/api/chat/folders/f1", json={"steering_dirs": new_dirs})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "owner_only"
    assert state._folders[0]["steering_dirs"] == stored
    assert no_disk == []


@pytest.mark.asyncio
async def test_link_session_folder_without_steering_dirs_is_unaffected():
    folder = {"id": "f1", "name": "Org", "parent_id": None, "order": 0}
    state = _state([folder])
    async with TestClient(TestServer(_make_dashboard_app(state, LINK_USER))) as client:
        created = await client.post("/api/chat/folders", json={"name": "Notes"})
        assert created.status == 201, await created.text()
        renamed = await client.patch("/api/chat/folders/f1", json={"name": "Org 2"})
        assert renamed.status == 200, await renamed.text()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="steering_dirs are refused where a directory cannot be opened relative to a descriptor",
)
async def test_owner_still_declares_and_changes_steering_dirs(tmp_path):
    first = tmp_path / "standards"
    first.mkdir()
    second = tmp_path / "more"
    second.mkdir()
    state = _state([])
    async with TestClient(TestServer(_make_dashboard_app(state, OWNER))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Org", "steering_dirs": [str(first)]},
        )
        assert resp.status == 201, await resp.text()
        fid = (await resp.json())["id"]
        assert state._folders[0]["steering_dirs"] == [str(first.resolve())]
        resp = await client.patch(f"/api/chat/folders/{fid}", json={"steering_dirs": [str(second)]})
        assert resp.status == 200, await resp.text()
    assert state._folders[0]["steering_dirs"] == [str(second.resolve())]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="steering_dirs are refused where a directory cannot be opened relative to a descriptor",
)
async def test_teammate_passes_the_same_gate_as_the_owner(tmp_path, monkeypatch):
    from kiro_crew.shared_crew import teammate_access

    monkeypatch.setattr(teammate_access, "teammate_passes_owner_gate", lambda request: True)
    state = _state([])
    async with TestClient(TestServer(_make_dashboard_app(state, "U0TEAMMATE"))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Org", "steering_dirs": [str(tmp_path)]},
        )
        assert resp.status == 201, await resp.text()
    assert state._folders[0]["steering_dirs"] == [str(tmp_path.resolve())]


@pytest.mark.asyncio
async def test_app_create_without_steering_dirs_is_unaffected():
    state = _state([])
    async with TestClient(TestServer(_make_app(state, "acme"))) as client:
        resp = await client.post("/api/chat/folders", json={"name": "Radar"})
        assert resp.status == 201, await resp.text()
    assert state._folders[0]["owner_app"] == "acme"
