"""Ownership on the chat-folder tree-shaping endpoints.

A folder created by an app carries it in ``owner_app``; an absent key reads as
the person's, which is what makes this a field addition rather than a migration.
An app may create at the top level or inside a folder it owns, and may rename,
reparent or delete only what it owns. The person is never confined.

The scope is derived from the authenticated calling session, never the body: the
managed MCP set authenticates with the internal secret, which carries no app
claim, so an app agent's tool call arrives with ``request["app"]`` empty.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_folders import (
    api_chat_folder_create,
    api_chat_folder_delete,
    api_chat_folder_update,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# fldr…01 belongs to the person, …02 to issue-radar, …03 to another app, and
# …04 predates the field entirely (no key at all) — the legacy row.
PERSON = "fldr00000001"
RADAR = "fldr00000002"
OTHER = "fldr00000003"
LEGACY = "fldr00000004"


def _folders() -> list[dict[str, Any]]:
    return [
        {"id": PERSON, "name": "Work", "parent_id": "", "owner_app": ""},
        {"id": RADAR, "name": "Radar output", "parent_id": "", "owner_app": "issue-radar"},
        {"id": OTHER, "name": "Specs", "parent_id": "", "owner_app": "spec-builder"},
        {"id": LEGACY, "name": "Old", "parent_id": ""},
    ]


def _app_slot(key: str, app: str) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._app = app
    return slot


def _state(*slots: _ChatSlot, folders: list[dict[str, Any]] | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = _folders() if folders is None else folders
    state._slots = {s.key: s for s in slots}
    state.push_slots_update = MagicMock()
    # No archive by default: _folder_history_counts returns {} early on a falsy
    # conversation_log, which is what an app's delete consults for emptiness. A
    # bare MagicMock here would be iterated instead and raise.
    state.conversation_log = None

    async def _mutate(fn: Any) -> Any:
        # The real store runs the callback under a lock and hands back its
        # second element; the ownership decisions live inside that callback, so a
        # mock that never calls it would prove nothing.
        _changed, value = fn(state._folders)
        return value

    state.mutate_folders = AsyncMock(side_effect=_mutate)
    return state


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        # Stands in for the token middleware. Empty for the internal-secret
        # (MCP) transport, which is the path an app agent's tool call takes.
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", api_chat_folder_delete)
    return app


def _by_id(state: DashboardState, fid: str) -> dict[str, Any] | None:
    return next((f for f in state._folders if f["id"] == fid), None)


class TestCreateStampsTheOwner:
    @pytest.mark.asyncio
    async def test_an_apps_folder_is_stamped_with_that_app(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 201
        assert body["owner_app"] == "issue-radar"

    @pytest.mark.asyncio
    async def test_the_persons_folder_carries_no_owner_key(self) -> None:
        """Absent, not empty-string: "absent means the person" stays the one
        representation, and the person's rows keep the shape they have on disk."""
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Q3"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 201
        assert "owner_app" not in body

    @pytest.mark.asyncio
    async def test_the_owner_is_never_taken_from_the_body(self) -> None:
        """A caller that could name its own owner could name someone else's."""
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "owner_app": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 201
        assert body["owner_app"] == "issue-radar"

    @pytest.mark.asyncio
    async def test_an_app_may_nest_under_its_own_folder(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "parent_id": RADAR},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 201

    @pytest.mark.asyncio
    async def test_an_app_may_not_nest_under_the_persons_folder(self) -> None:
        """Nesting writes to THAT folder's child list."""
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        before = len(state._folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "parent_id": PERSON},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        assert len(state._folders) == before

    @pytest.mark.asyncio
    async def test_a_legacy_row_without_the_key_is_the_persons(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Runs", "parent_id": LEGACY},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 403


class TestRenameAndReparentAreBounded:
    @pytest.mark.asyncio
    async def test_an_app_can_rename_its_own_folder(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"name": "Renamed"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_an_app_cannot_rename_the_persons_folder(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"name": "Hijacked"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        assert _by_id(state, PERSON)["name"] == "Work"

    @pytest.mark.asyncio
    async def test_an_app_cannot_rename_another_apps_folder(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{OTHER}",
                json={"name": "Hijacked"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 403
        assert _by_id(state, OTHER)["name"] == "Specs"

    @pytest.mark.asyncio
    async def test_the_person_is_not_confined_by_an_apps_ownership(self) -> None:
        state = _state(_ChatSlot("chat-1-100"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"name": "Tidied up"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["name"] == "Tidied up"

    @pytest.mark.asyncio
    async def test_an_app_cannot_reparent_its_folder_into_the_persons(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"parent_id": PERSON},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        assert _by_id(state, RADAR)["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_an_app_can_reparent_to_the_top_level(self) -> None:
        """The top level is not a folder row, so it has no owner to violate —
        that is where an app's own tree starts."""
        folders = _folders()
        nested = {
            "id": "fldr00000005",
            "name": "Runs",
            "parent_id": RADAR,
            "owner_app": "issue-radar",
        }
        folders.append(nested)
        state = _state(_app_slot("chat-1-100", "issue-radar"), folders=folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                "/api/chat/folders/fldr00000005",
                json={"parent_id": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, "fldr00000005")["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_ownership_cannot_be_reassigned_by_a_patch(self) -> None:
        """Stamped once at create; not a field a request can hand over or clear."""
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"owner_app": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["owner_app"] == "issue-radar"

    @pytest.mark.asyncio
    async def test_moving_own_folder_that_holds_a_foreign_one_is_refused(self) -> None:
        """A move takes the subtree with it, so the person's nested folder would
        be relocated by an app's write."""
        folders = _folders()
        folders.append({"id": "fldr00000007", "name": "Theirs", "parent_id": RADAR})
        state = _state(_app_slot("chat-1-100", "issue-radar"), folders=folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"parent_id": ""},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_not_owned"
        assert _by_id(state, RADAR)["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_renaming_a_folder_that_holds_a_foreign_one_is_still_allowed(self) -> None:
        """Only the MOVE is gated on the subtree -- a rename relocates nothing."""
        folders = _folders()
        folders.append({"id": "fldr00000007", "name": "Theirs", "parent_id": RADAR})
        state = _state(_app_slot("chat-1-100", "issue-radar"), folders=folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"name": "Renamed"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_the_person_can_still_move_a_folder_holding_an_apps(self) -> None:
        """Containment cuts both ways, but the person is never confined."""
        folders = _folders()
        folders.append(
            {
                "id": "fldr00000007",
                "name": "Radar sub",
                "parent_id": PERSON,
                "owner_app": "issue-radar",
            }
        )
        state = _state(_ChatSlot("chat-1-100"), folders=folders)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"parent_id": OTHER},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 200
        assert _by_id(state, PERSON)["parent_id"] == OTHER


class TestAnAppCannotDeleteFolders:
    """A delete relocates everything the folder contains, and those contents live
    in a DIFFERENT store from the folder -- the slot table and the session
    archive, neither sharing a lock with it. So emptiness cannot be established
    atomically with the removal, and every narrower rule leaked through another
    seam. The person keeps the delete they always had.

    Nothing shipped loses a capability: no MCP tool exposes folder deletion, and
    the only client of the route is the dashboard UI.
    """

    @pytest.mark.asyncio
    async def test_an_app_cannot_delete_even_an_empty_folder_it_owns(self) -> None:
        state = _state(_app_slot("chat-1-100", "issue-radar"))
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.delete(
                    f"/api/chat/folders/{RADAR}",
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
                body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "folder_delete_forbidden"
        assert _by_id(state, RADAR) is not None

    @pytest.mark.asyncio
    async def test_no_session_is_touched_by_the_refusal(self) -> None:
        """Refused before the unfile loop, so nothing is written and there is
        nothing to roll back."""
        mine = _app_slot("chat-9-900", "issue-radar")
        mine.folder_id = RADAR
        state = _state(_app_slot("chat-1-100", "issue-radar"), mine)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.delete(
                    f"/api/chat/folders/{RADAR}",
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 403
        assert mine.folder_id == RADAR

    @pytest.mark.asyncio
    async def test_the_person_can_still_delete_a_full_folder(self) -> None:
        """The person is not confined: clearing out a folder full of
        conversations and subfolders is the delete they already had."""
        theirs = _app_slot("chat-9-900", "issue-radar")
        theirs.folder_id = RADAR
        folders = _folders()
        folders.append({"id": "fldr00000006", "name": "Sub", "parent_id": RADAR})
        state = _state(_ChatSlot("chat-1-100"), theirs, folders=folders)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.delete(
                    f"/api/chat/folders/{RADAR}",
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 200
        assert _by_id(state, RADAR) is None
        assert theirs.folder_id == ""
        assert _by_id(state, "fldr00000006")["parent_id"] == ""


class TestACallerWhoseSlotIsGoneIsRefused:
    """An empty scope reads as the person, which is right for a caller that never
    had a slot (Slack, a channel session, the person's cron) and wrong for a
    `dashboard:` key, which NAMES one. A tab closing while its tool call is in
    flight pops the slot without draining, so an app-owned session would arrive
    unattributable and be handed the person's authority over the person's folders.
    """

    @pytest.mark.asyncio
    async def test_create_is_refused(self) -> None:
        state = _state()  # the named slot is absent from the registry
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Sneaky"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "caller_unattributable"

    @pytest.mark.asyncio
    async def test_rename_of_the_persons_folder_is_refused(self) -> None:
        state = _state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{PERSON}",
                json={"name": "Hijacked"},
                headers={"X-Session-Key": "dashboard:chat-1-100"},
            )
        assert resp.status == 403
        assert _by_id(state, PERSON)["name"] == "Work"

    @pytest.mark.asyncio
    async def test_delete_is_refused_before_any_slot_is_unfiled(self) -> None:
        filed = _ChatSlot("chat-9-900")
        filed.folder_id = PERSON
        state = _state(filed)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.delete(
                    f"/api/chat/folders/{PERSON}",
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 403
        assert _by_id(state, PERSON) is not None
        assert filed.folder_id == PERSON

    @pytest.mark.asyncio
    async def test_a_caller_that_never_had_a_slot_is_still_the_person(self) -> None:
        """The refusal must not swallow Slack, channel or cron callers -- they
        never had a slot to be confined to, which is a different fact."""
        state = _state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{RADAR}",
                json={"name": "Tidied"},
                headers={"X-Session-Key": "slack:T1/C1"},
            )
        assert resp.status == 200
        assert _by_id(state, RADAR)["name"] == "Tidied"
