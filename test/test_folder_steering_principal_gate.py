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


def _make_app(state: DashboardState, principal: str, *, person: bool = False) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        # An agent principal arrives as the token middleware publishes an app
        # claim; the PERSON's own sidebar call carries no claim and the positive
        # ``is_dashboard_user`` stamp the middleware sets only when the person's
        # own credential validated -- the one bit the steering fence reads the
        # person by (``chat_folders._is_the_person``). A caller with neither is
        # any other session: a Channels agent, a messaging-transport turn.
        request["app"] = principal
        if person:
            request["is_dashboard_user"] = True
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
async def test_an_app_cannot_clear_steering_dirs_even_on_its_own_folder():
    """A clear is the same mutation of the person's context in the other
    direction: the person declared steering on the app's folder (allowed), and
    an app's ``[]`` would remove it from every chat in the subtree. Refused with
    the gate's own code; the declaration stands."""
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
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "steering_dirs_forbidden"
    assert state._folders[0]["steering_dirs"] == ["/srv/standards"]


@pytest.mark.asyncio
async def test_app_create_without_steering_dirs_is_unaffected():
    state = _state([])
    async with TestClient(TestServer(_make_app(state, "acme"))) as client:
        resp = await client.post("/api/chat/folders", json={"name": "Radar"})
        assert resp.status == 201, await resp.text()
    assert state._folders[0]["owner_app"] == "acme"


class TestAChannelAgentIsRefusedLikeAnAppOrMember:
    """A Channels agent's key names no slot and no app, so ``folder_principal``
    reads it as the PERSON -- but WHO the steering fence confines is not read
    from the key or the principal: it is the one bit every folder fence keys
    on (``_is_the_person``), the token middleware's POSITIVE ``is_dashboard_user``
    stamp, which only the person's own credential earns. So a
    Channels agent is refused a declaration exactly as an app or member is
    (same 403 shape; the audit names its key under the one source), while the
    person's own browser call -- the stamped one -- is unchanged. Clearing
    to ``[]`` and an unbound write are not this rule's concern.
    """

    CHANNEL = "channel:chan-000001:helper"

    @pytest.mark.asyncio
    async def test_a_channel_agent_cannot_declare_steering_dirs_on_create(
        self, tmp_path, no_disk
    ) -> None:
        state = _state([])
        async with TestClient(TestServer(_make_app(state, ""))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Radar", "steering_dirs": [str(tmp_path)]},
                headers={"X-Session-Key": self.CHANNEL},
            )
            assert resp.status == 403, await resp.text()
            body = await resp.json()
        assert body["code"] == "steering_dirs_forbidden"
        assert state._folders == [], "nothing was created"
        assert no_disk == []

    @pytest.mark.asyncio
    async def test_a_channel_agent_cannot_declare_steering_dirs_on_update(
        self, tmp_path, no_disk
    ) -> None:
        """The person's own folder, which a channel key would otherwise reach
        as the person."""
        persons = {"id": "f1", "name": "Work", "parent_id": None, "order": 0}
        state = _state([persons])
        async with TestClient(TestServer(_make_app(state, ""))) as client:
            resp = await client.patch(
                "/api/chat/folders/f1",
                json={"steering_dirs": [str(tmp_path)]},
                headers={"X-Session-Key": self.CHANNEL},
            )
            assert resp.status == 403, await resp.text()
            assert (await resp.json())["code"] == "steering_dirs_forbidden"
        assert "steering_dirs" not in state._folders[0]
        assert no_disk == []

    @pytest.mark.asyncio
    async def test_the_refusal_is_audited_against_the_channel_key(
        self, tmp_path, no_disk, monkeypatch
    ) -> None:
        sel_fn = MagicMock()
        monkeypatch.setattr(cf, "sel", sel_fn)
        state = _state([])
        async with TestClient(TestServer(_make_app(state, ""))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Radar", "steering_dirs": [str(tmp_path)]},
                headers={"X-Session-Key": self.CHANNEL},
            )
            assert resp.status == 403, await resp.text()
        kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
        assert kwargs["caller"] == self.CHANNEL
        assert kwargs["operation"] == "chat.folder_create"
        assert kwargs["outcome"] == "denied"
        assert kwargs["source"] == "app_isolation"
        assert "steering_dirs" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_a_channel_agent_cannot_clear_but_writes_without_steering(
        self,
    ) -> None:
        """A clear REMOVES the person's declaration from every chat in the
        subtree -- the same mutation in the other direction -- so it is refused
        like a declaration; a create that names no steering at all is not this
        rule's concern and lands."""
        persons = {
            "id": "f1",
            "name": "Work",
            "parent_id": None,
            "order": 0,
            "steering_dirs": ["/srv/standards"],
        }
        state = _state([persons])
        async with TestClient(TestServer(_make_app(state, ""))) as client:
            cleared = await client.patch(
                "/api/chat/folders/f1",
                json={"steering_dirs": []},
                headers={"X-Session-Key": self.CHANNEL},
            )
            assert cleared.status == 403, await cleared.text()
            assert (await cleared.json())["code"] == "steering_dirs_forbidden"
            assert state._folders[0]["steering_dirs"] == ["/srv/standards"]
            created = await client.post(
                "/api/chat/folders",
                json={"name": "Radar"},
                headers={"X-Session-Key": self.CHANNEL},
            )
            assert created.status == 201, await created.text()
        assert state._folders[0]["steering_dirs"] == ["/srv/standards"]
        assert "owner_app" not in state._folders[1]

    @pytest.mark.asyncio
    async def test_the_person_still_declares_steering_dirs(self, tmp_path, monkeypatch) -> None:
        """The person's own dashboard call carries the empty principal and a
        dashboard session key; the fence never fires for it."""
        monkeypatch.setattr(cf, "_validate_steering_dirs", lambda value: ([str(tmp_path)], None))
        persons = {"id": "f1", "name": "Work", "parent_id": None, "order": 0}
        state = _state([persons])
        async with TestClient(TestServer(_make_app(state, "", person=True))) as client:
            resp = await client.patch(
                "/api/chat/folders/f1",
                json={"steering_dirs": [str(tmp_path)]},
                headers={"X-Session-Key": "chat-1-100"},
            )
            assert resp.status == 200, await resp.text()
        assert state._folders[0]["steering_dirs"] == [str(tmp_path)]


class TestASessionDrivenFromAMessagingChannelIsRefusedTheSameWay:
    """A turn driven from a messaging-transport thread (``slack:``, ``discord:``)
    presents that key: no slot, no app. Nothing about the key's shape is read
    -- it carries no ``is_dashboard_user`` stamp, so it is not the person by the
    same one bit as a Channels agent, an app or a member, and is refused a
    declaration at both write sites with the same 403; the audit names the
    transport key under the one source; clearing to ``[]`` is unchanged.
    """

    SLACK = "slack:1785370133.085469"

    @pytest.mark.asyncio
    async def test_it_cannot_declare_steering_dirs_on_create(self, tmp_path, no_disk) -> None:
        state = _state([])
        async with TestClient(TestServer(_make_app(state, ""))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Radar", "steering_dirs": [str(tmp_path)]},
                headers={"X-Session-Key": self.SLACK},
            )
            assert resp.status == 403, await resp.text()
            body = await resp.json()
        assert body["code"] == "steering_dirs_forbidden"
        assert state._folders == [], "nothing was created"
        assert no_disk == []

    @pytest.mark.asyncio
    async def test_it_cannot_declare_steering_dirs_on_update(self, tmp_path, no_disk) -> None:
        persons = {"id": "f1", "name": "Work", "parent_id": None, "order": 0}
        state = _state([persons])
        async with TestClient(TestServer(_make_app(state, ""))) as client:
            resp = await client.patch(
                "/api/chat/folders/f1",
                json={"steering_dirs": [str(tmp_path)]},
                headers={"X-Session-Key": self.SLACK},
            )
            assert resp.status == 403, await resp.text()
            assert (await resp.json())["code"] == "steering_dirs_forbidden"
        assert "steering_dirs" not in state._folders[0]
        assert no_disk == []

    @pytest.mark.asyncio
    async def test_the_refusal_is_audited_against_the_transport_key(
        self, tmp_path, no_disk, monkeypatch
    ) -> None:
        sel_fn = MagicMock()
        monkeypatch.setattr(cf, "sel", sel_fn)
        state = _state([])
        async with TestClient(TestServer(_make_app(state, ""))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Radar", "steering_dirs": [str(tmp_path)]},
                headers={"X-Session-Key": self.SLACK},
            )
            assert resp.status == 403, await resp.text()
        kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
        assert kwargs["caller"] == self.SLACK
        assert kwargs["source"] == "app_isolation"
        assert "steering_dirs" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_it_cannot_clear_either(self) -> None:
        persons = {
            "id": "f1",
            "name": "Work",
            "parent_id": None,
            "order": 0,
            "steering_dirs": ["/srv/standards"],
        }
        state = _state([persons])
        async with TestClient(TestServer(_make_app(state, ""))) as client:
            cleared = await client.patch(
                "/api/chat/folders/f1",
                json={"steering_dirs": []},
                headers={"X-Session-Key": self.SLACK},
            )
            assert cleared.status == 403, await cleared.text()
            assert (await cleared.json())["code"] == "steering_dirs_forbidden"
        assert state._folders[0]["steering_dirs"] == ["/srv/standards"]
