"""App isolation on PATCH /api/chat/slots/{slot}/folder.

Filing a session is a WRITE to that session's own state: it moves the row in
the sidebar and re-injects the folder breadcrumb on that session's next turn.
So the route has to answer "may this caller touch this slot?", and the answer
cannot come from the transport: the managed MCP set authenticates with the
internal secret, which carries no app claim, so an app agent's tool call
arrives with ``request["app"]`` empty. The scope is derived from the
authenticated calling session instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_folders import api_chat_slot_folder
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState, *, declared_app: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler):
        # Stands in for the token middleware, which publishes the validated app
        # token's name. Empty for the internal-secret (MCP) transport.
        request["app"] = declared_app
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_patch("/api/chat/slots/{slot}/folder", api_chat_slot_folder)
    return app


def _state(*slots: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {s.key: s for s in slots}
    state._folders = [{"id": "fldr00000001", "name": "Work", "parent_id": ""}]
    state.push_slots_update = MagicMock()
    state.mutate_folders = AsyncMock(return_value="")
    return state


def _app_slot(key: str, app: str) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._app = app
    return slot


class TestFolderFilingIsAppScoped:
    @pytest.mark.asyncio
    async def test_an_app_cannot_refile_another_apps_session(self) -> None:
        caller = _app_slot("chat-1-100", "issue-radar")
        target = _app_slot("chat-2-200", "spec-builder")
        state = _state(caller, target)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"), patch(
            "kiro_crew.dashboard.chat_folders._unhide_folder", AsyncMock(return_value=True)
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-2-200/folder",
                    json={"folder_id": "fldr00000001"},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
                body = await resp.json()
        assert resp.status == 404
        assert body["code"] == "slot_not_found"
        assert target.folder_id == ""

    @pytest.mark.asyncio
    async def test_an_app_cannot_refile_the_users_own_session(self) -> None:
        """An unscoped slot is the user's; an app holding the route is not."""
        caller = _app_slot("chat-1-100", "issue-radar")
        target = _ChatSlot("chat-2-200")  # no _app — created by the human
        state = _state(caller, target)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"), patch(
            "kiro_crew.dashboard.chat_folders._unhide_folder", AsyncMock(return_value=True)
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-2-200/folder",
                    json={"folder_id": "fldr00000001"},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 404
        assert target.folder_id == ""

    @pytest.mark.asyncio
    async def test_an_app_can_refile_its_own_session(self) -> None:
        caller = _app_slot("chat-1-100", "issue-radar")
        target = _app_slot("chat-2-200", "issue-radar")
        state = _state(caller, target)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"), patch(
            "kiro_crew.dashboard.chat_folders._unhide_folder", AsyncMock(return_value=True)
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-2-200/folder",
                    json={"folder_id": "fldr00000001"},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 200
        assert target.folder_id == "fldr00000001"

    @pytest.mark.asyncio
    async def test_the_user_can_still_organize_every_session(self) -> None:
        """The point of the folder tools — an unscoped caller is not confined."""
        caller = _ChatSlot("chat-1-100")
        target = _app_slot("chat-2-200", "issue-radar")
        state = _state(caller, target)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"), patch(
            "kiro_crew.dashboard.chat_folders._unhide_folder", AsyncMock(return_value=True)
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-2-200/folder",
                    json={"folder_id": "fldr00000001"},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 200
        assert target.folder_id == "fldr00000001"

    @pytest.mark.asyncio
    async def test_scope_is_never_taken_from_the_request_body(self) -> None:
        """A caller that could name its own scope could name someone else's."""
        caller = _app_slot("chat-1-100", "issue-radar")
        target = _app_slot("chat-2-200", "spec-builder")
        state = _state(caller, target)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"), patch(
            "kiro_crew.dashboard.chat_folders._unhide_folder", AsyncMock(return_value=True)
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-2-200/folder",
                    json={"folder_id": "fldr00000001", "app": "spec-builder"},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
        assert resp.status == 404
        assert target.folder_id == ""

    @pytest.mark.asyncio
    async def test_an_app_token_still_confines_the_caller(self) -> None:
        """The token path keeps working — it is not replaced, only backstopped."""
        target = _app_slot("chat-2-200", "spec-builder")
        state = _state(target)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"), patch(
            "kiro_crew.dashboard.chat_folders._unhide_folder", AsyncMock(return_value=True)
        ):
            app = _make_app(state, declared_app="issue-radar")
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-2-200/folder",
                    json={"folder_id": "fldr00000001"},
                )
        assert resp.status == 404
        assert target.folder_id == ""
