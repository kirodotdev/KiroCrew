"""The folder routes keep a mate's home folder locked.

* A person may collapse and reorder a mate's home and the folders inside it,
  and nothing else: rename, delete and moving a folder in are 403
  ``mate_home_locked``.
* A person may not move a chat out of a mate's home.
* A mate may not reparent its folders out of its home: 403 ``mate_folder_scope``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.dashboard.chat_folders import (
    _refuse_mate_move_out,
    api_chat_folder_delete,
    api_chat_folder_update,
)
from kiro_crew.dashboard.mate_folders import MATE_HOME_KEY
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

MATE = "member:member-radar-0123"
HOME = "fldrhome0001"
INSIDE = "fldrinside01"
PERSON = "fldrperson01"
OUTSIDE = "fldroutside1"
HEADERS = {"X-Session-Key": "dashboard:chat-1-100"}


def _folders() -> list[dict[str, Any]]:
    return [
        {"id": HOME, "name": "Radar", "parent_id": "", "owner_app": MATE, MATE_HOME_KEY: True},
        {"id": INSIDE, "name": "PR review", "parent_id": HOME, "owner_app": MATE},
        {"id": PERSON, "name": "Work", "parent_id": ""},
        {"id": OUTSIDE, "name": "Old notes", "parent_id": "", "owner_app": MATE},
    ]


def _state() -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = _folders()
    state._slots = {"chat-1-100": _ChatSlot("chat-1-100")}
    state.push_slots_update = MagicMock()
    state.conversation_log = None

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        changed, value = fn(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    state.mutate_folders = AsyncMock(side_effect=_mutate)
    return state


def _app(state: DashboardState, member: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _caller(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        if member:
            request[MEMBER_CHAT_PRINCIPAL_KEY] = member
        return await handler(request)

    app.middlewares.append(_caller)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", api_chat_folder_delete)
    return app


def _row(state: DashboardState, fid: str) -> dict[str, Any]:
    return next(f for f in state._folders if f["id"] == fid)


@pytest.mark.asyncio
class TestAPersonIsKeptOutOfAMateHome:
    @pytest.mark.parametrize("fid", [HOME, INSIDE])
    async def test_rename_and_delete_are_refused(self, fid: str) -> None:
        state = _state()
        async with TestClient(TestServer(_app(state))) as client:
            rename = await client.patch(
                f"/api/chat/folders/{fid}", json={"name": "Mine"}, headers=HEADERS
            )
            delete = await client.delete(f"/api/chat/folders/{fid}", headers=HEADERS)
            for resp in (rename, delete):
                assert resp.status == 403
                assert (await resp.json())["code"] == "mate_home_locked"
        assert state._folders == _folders()

    @pytest.mark.parametrize("fid", [HOME, INSIDE])
    async def test_moving_it_or_into_it_is_refused(self, fid: str) -> None:
        state = _state()
        async with TestClient(TestServer(_app(state))) as client:
            out = await client.patch(
                f"/api/chat/folders/{fid}", json={"parent_id": PERSON}, headers=HEADERS
            )
            into = await client.patch(
                f"/api/chat/folders/{PERSON}", json={"parent_id": fid}, headers=HEADERS
            )
            for resp in (out, into):
                assert resp.status == 403
                assert (await resp.json())["code"] == "mate_home_locked"
        assert state._folders == _folders()

    @pytest.mark.parametrize("fid", [HOME, INSIDE])
    async def test_collapse_and_order_still_apply(self, fid: str) -> None:
        state = _state()
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{fid}", json={"collapsed": True, "order": 3}, headers=HEADERS
            )
            assert resp.status == 200
        assert _row(state, fid)["collapsed"] is True
        assert _row(state, fid)["order"] == 3


@pytest.mark.asyncio
async def test_a_mate_may_not_reparent_out_of_its_home() -> None:
    state = _state()
    async with TestClient(TestServer(_app(state, member=MATE))) as client:
        for parent in (PERSON, ""):
            resp = await client.patch(
                f"/api/chat/folders/{INSIDE}", json={"parent_id": parent}, headers=HEADERS
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "mate_folder_scope"
        # A folder the mate owns outside its home is not moved in.
        resp = await client.patch(
            f"/api/chat/folders/{OUTSIDE}", json={"parent_id": HOME}, headers=HEADERS
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "mate_folder_scope"
        # The home itself takes only layout changes, from the mate too.
        resp = await client.patch(
            f"/api/chat/folders/{HOME}", json={"parent_id": PERSON}, headers=HEADERS
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "mate_home_locked"
    assert state._folders == _folders()


def _request(member: str = "") -> web.Request:
    request = make_mocked_request("PATCH", "/api/chat/slots/chat-1-100/folder")
    request["app"] = ""
    if member:
        request[MEMBER_CHAT_PRINCIPAL_KEY] = member
    return request


@pytest.mark.parametrize("current", [HOME, INSIDE])
@pytest.mark.parametrize("target", [PERSON, ""])
def test_a_person_may_not_move_a_chat_out_of_a_mate_home(current: str, target: str) -> None:
    resp = _refuse_mate_move_out(_state(), _request(), target, current)
    assert resp is not None and resp.status == 403


def test_a_person_moves_chats_freely_outside_any_home() -> None:
    assert _refuse_mate_move_out(_state(), _request(), PERSON, "") is None
    assert _refuse_mate_move_out(_state(), _request(), "", PERSON) is None


def _create_state(tmp_path: Any) -> DashboardState:
    """A real state for POST /api/chat/slots, holding the folders above."""
    from chat_test_helpers import _make_ready_kiro_prerequisite

    from kiro_crew.history import ConversationLog

    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state._folders.extend(_folders())
    return state


@pytest.mark.asyncio
async def test_a_create_naming_a_chat_in_a_mate_home_may_not_move_it_out(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.chat import api_chat_slot_create

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _create_state(tmp_path)
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/chat/slots", json={"name": "s1"})).status == 200
        for current in (HOME, INSIDE):
            state._slots["s1"].folder_id = current
            for target in (PERSON, OUTSIDE):
                resp = await client.post(
                    "/api/chat/slots", json={"name": "s1", "folder_id": target}
                )
                assert resp.status == 403
                assert (await resp.json())["code"] == "mate_home_locked"
                assert state._slots["s1"].folder_id == current
        # A chat outside any home is refiled as before.
        state._slots["s1"].folder_id = OUTSIDE
        resp = await client.post("/api/chat/slots", json={"name": "s1", "folder_id": PERSON})
        assert resp.status == 200
    assert state._slots["s1"].folder_id == PERSON
