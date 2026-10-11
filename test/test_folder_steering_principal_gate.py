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
from chat_test_helpers import stamp_the_person

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


def _make_app(
    state: DashboardState, principal: str, *, person: bool = False, attested_peer: bool = False
) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        # An agent principal arrives as the token middleware publishes an app
        # claim; the PERSON's own sidebar call carries no claim and the stamps
        # the middleware writes only when the person's own credential validated
        # (:func:`stamp_the_person`: the positive ``is_dashboard_user`` bit and
        # the owner's subject) -- what the steering fence reads the person by
        # (``chat_folders._is_the_person``). A caller with neither is any other
        # session: a Channels agent, a messaging-transport turn. ``attested_peer``
        # is the AF_UNIX peer check's stamp (``request["peer_verified"]``): the
        # declared ``X-Session-Key`` was kernel-verified, so the audit may name it.
        request["app"] = principal
        if person:
            stamp_the_person(request)
        if attested_peer:
            request["peer_verified"] = True
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
    """Clearing to ``[]`` only removes reads and stays allowed for every principal."""
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


class TestAChannelAgentIsRefusedLikeAnAppOrMember:
    """A Channels agent's key names no slot and no app."""

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
        """The person's own folder, which a channel key would otherwise reach as the person."""
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
    async def test_the_refusal_is_audited_against_the_channel_key_only_when_attested(
        self, tmp_path, no_disk, monkeypatch
    ) -> None:
        """The SEL row names the declared key only when the AF_UNIX peer check kernel-attested."""
        sel_fn = MagicMock()
        monkeypatch.setattr(cf, "sel", sel_fn)
        for attested, expected in ((False, "unattributable"), (True, self.CHANNEL)):
            sel_fn.reset_mock()
            state = _state([])
            app = _make_app(state, "", attested_peer=attested)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/folders",
                    json={"name": "Radar", "steering_dirs": [str(tmp_path)]},
                    headers={"X-Session-Key": self.CHANNEL},
                )
                assert resp.status == 403, await resp.text()
            kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
            assert kwargs["caller"] == expected, (attested, kwargs)
            assert kwargs["operation"] == "chat.folder_create"
            assert kwargs["outcome"] == "denied"
            assert kwargs["source"] == "app_isolation"
            assert "steering_dirs" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_a_channel_agent_may_clear_and_writes_without_steering(
        self,
    ) -> None:
        """Only a declaration is the person's."""
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
            assert cleared.status == 200, await cleared.text()
            assert not state._folders[0].get("steering_dirs")
            created = await client.post(
                "/api/chat/folders",
                json={"name": "Radar"},
                headers={"X-Session-Key": self.CHANNEL},
            )
            assert created.status == 201, await created.text()
        assert not state._folders[0].get("steering_dirs")
        assert "owner_app" not in state._folders[1]

    @pytest.mark.asyncio
    async def test_the_person_still_declares_steering_dirs(self, tmp_path, monkeypatch) -> None:
        """The person's own dashboard call carries the empty principal and a dashboard session."""
        monkeypatch.setattr(
            cf, "_validate_steering_dirs", lambda value: ([str(tmp_path)] if value else [], None)
        )
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
    """A turn driven from a messaging-transport thread (``slack:``, ``discord:``) presents that."""

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
    async def test_the_refusal_is_audited_against_the_transport_key_only_when_attested(
        self, tmp_path, no_disk, monkeypatch
    ) -> None:
        sel_fn = MagicMock()
        monkeypatch.setattr(cf, "sel", sel_fn)
        for attested, expected in ((False, "unattributable"), (True, self.SLACK)):
            sel_fn.reset_mock()
            state = _state([])
            app = _make_app(state, "", attested_peer=attested)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/folders",
                    json={"name": "Radar", "steering_dirs": [str(tmp_path)]},
                    headers={"X-Session-Key": self.SLACK},
                )
                assert resp.status == 403, await resp.text()
            kwargs = sel_fn.return_value.log_api_access.call_args.kwargs
            assert kwargs["caller"] == expected, (attested, kwargs)
            assert kwargs["source"] == "app_isolation"
            assert "steering_dirs" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_it_may_clear_like_every_principal(self) -> None:
        """Only a declaration is the person's; a clear (``[]``) only removes reads and lands."""
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
            assert cleared.status == 200, await cleared.text()
        assert not state._folders[0].get("steering_dirs")


class TestAnAllowListedUsersDashboardLinkIsNotThePerson:
    """A Slack user on the bot's allow-list can ask for a dashboard link (``!dashboard``) and."""

    LINK_HOLDER = {"X-Test-User": "U0ALLOWED"}

    @pytest.mark.asyncio
    async def test_it_cannot_declare_but_may_clear_and_the_owner_can_declare(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            cf, "_validate_steering_dirs", lambda value: ([str(tmp_path)] if value else [], None)
        )
        steered = {
            "id": "f1",
            "name": "Work",
            "parent_id": None,
            "order": 0,
            "steering_dirs": ["/srv/standards"],
        }
        plain = {"id": "f2", "name": "Plain", "parent_id": None, "order": 1}
        state = _state([steered, plain])
        async with TestClient(TestServer(_make_app(state, "", person=True))) as client:
            declared = await client.patch(
                "/api/chat/folders/f2",
                json={"steering_dirs": [str(tmp_path)]},
                headers=self.LINK_HOLDER,
            )
            assert declared.status == 403, await declared.text()
            assert (await declared.json())["code"] == "steering_dirs_forbidden"
            cleared = await client.patch(
                "/api/chat/folders/f1", json={"steering_dirs": []}, headers=self.LINK_HOLDER
            )
            assert cleared.status == 200, await cleared.text()  # a clear only removes reads
            owners = await client.patch(
                "/api/chat/folders/f2", json={"steering_dirs": [str(tmp_path)]}
            )
            assert owners.status == 200, await owners.text()
        assert not state._folders[0].get("steering_dirs")
        assert state._folders[1]["steering_dirs"] == [str(tmp_path)]

    @pytest.mark.asyncio
    async def test_a_session_signed_in_before_the_owner_was_configured_is_told_to_sign_in_again(
        self, tmp_path, monkeypatch
    ) -> None:
        """The operator's own session from before ``KIROCREW_OWNER_ID`` was configured keeps its."""
        from kiro_crew.dashboard.handlers.source_providers import STALE_OWNER_SESSION_CODE

        monkeypatch.setattr(
            cf, "_validate_steering_dirs", lambda value: ([str(tmp_path)] if value else [], None)
        )
        steered = {
            "id": "f1",
            "name": "Work",
            "parent_id": None,
            "order": 0,
            "steering_dirs": ["/srv/standards"],
        }
        plain = {"id": "f2", "name": "Plain", "parent_id": None, "order": 1}
        state = _state([steered, plain])
        state.owner_id = "U0OWNER"
        stale = {"X-Test-User": "local-startup"}
        async with TestClient(TestServer(_make_app(state, "", person=True))) as client:
            declared = await client.patch(
                "/api/chat/folders/f2", json={"steering_dirs": [str(tmp_path)]}, headers=stale
            )
            assert declared.status == 401, await declared.text()
            assert (await declared.json())["code"] == STALE_OWNER_SESSION_CODE
            link = await client.patch(
                "/api/chat/folders/f2",
                json={"steering_dirs": [str(tmp_path)]},
                headers=self.LINK_HOLDER,
            )
            assert link.status == 403, await link.text()
            assert (await link.json())["code"] == "steering_dirs_forbidden"
            cleared = await client.patch(
                "/api/chat/folders/f1", json={"steering_dirs": []}, headers=stale
            )
            assert cleared.status == 200, await cleared.text()
        assert not state._folders[0].get("steering_dirs")
        assert "steering_dirs" not in state._folders[1]
