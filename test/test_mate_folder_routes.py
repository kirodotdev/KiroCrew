"""The folder routes keep a mate's home folder locked.

* A person may collapse and reorder a mate's home and the folders inside it,
  and nothing else: rename, delete of the home, and moving a folder in are 403
  ``mate_home_locked``.
* A person may delete an EMPTY folder inside a mate's home; one that
  holds a child folder, a live, archived or cron-filed session is 409
  ``folder_not_empty``, and every other caller is refused.
* A person may not move a chat out of a mate's home.
* A mate may not reparent its folders out of its home: 403 ``mate_folder_scope``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_folders import (
    _refuse_mate_move_out,
    api_chat_folder_delete,
    api_chat_folder_update,
)
from kiro_crew.dashboard.handlers.cron import _ChatFolderGone, _persist_holding_folder
from kiro_crew.dashboard.mate_folders import MATE_HOME_KEY
from kiro_crew.dashboard.snapshot_commit import close_in_flight
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
    state.crons = MagicMock()
    state.crons.chat_folder_ids_async = AsyncMock(return_value=set())

    async def _mutate(fn: Any, on_committed: Any = None, prepare: Any = None) -> Any:
        if prepare is not None:
            await prepare()
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
    async def test_rename_is_refused(self, fid: str) -> None:
        state = _state()
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.patch(
                f"/api/chat/folders/{fid}", json={"name": "Mine"}, headers=HEADERS
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "mate_home_locked"
        assert state._folders == _folders()

    async def test_deleting_the_home_is_refused_even_when_empty(self) -> None:
        state = _state()
        state._folders = [f for f in state._folders if f["id"] != INSIDE]
        before = list(state._folders)
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.delete(f"/api/chat/folders/{HOME}", headers=HEADERS)
            assert resp.status == 403
            assert (await resp.json())["code"] == "mate_home_locked"
        assert state._folders == before

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


@pytest.mark.asyncio
class TestAPersonMayDeleteAnEmptyFolderInsideAMateHome:
    async def test_an_empty_one_is_deleted(self) -> None:
        state = _state()
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.delete(f"/api/chat/folders/{INSIDE}", headers=HEADERS)
            assert resp.status == 200
        assert [f["id"] for f in state._folders] == [HOME, PERSON, OUTSIDE]
        state.push_slots_update.assert_called_once()

    async def test_one_with_a_project_dir_is_still_deleted_from_the_menu(self) -> None:
        state = _state()
        next(f for f in state._folders if f["id"] == INSIDE)["project_dir"] = "/work/x"
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.delete(f"/api/chat/folders/{INSIDE}", headers=HEADERS)
            assert resp.status == 200
        assert INSIDE not in {f["id"] for f in state._folders}

    async def test_the_cleanup_offers_it_and_keeps_the_home(self) -> None:
        from kiro_crew.dashboard.chat_folder_cleanup import removable_folder_ids

        folders = _folders()
        # An icon on it is the mate's choice, so it does not keep the folder.
        folders[1]["icon"] = "rocket"
        ids = removable_folder_ids(folders, set(), include_top_level=True)
        assert INSIDE in ids
        assert HOME not in ids
        # A project directory keeps it: the next worker filed there runs in it.
        folders[1]["project_dir"] = "/work/repo"
        assert INSIDE not in removable_folder_ids(folders, set(), include_top_level=True)
        del folders[1]["project_dir"]
        # A live session inside still keeps it.
        assert INSIDE not in removable_folder_ids(folders, {INSIDE}, include_top_level=True)

    @pytest.mark.parametrize("filled", ["child", "live", "closing", "cron", "archived"])
    async def test_a_folder_that_is_not_empty_stays(self, filled: str) -> None:
        state = _state()
        closing = _ChatSlot("chat-2-200")
        if filled == "child":
            state._folders.append(
                {"id": "fldrgrand001", "name": "deep", "parent_id": INSIDE, "owner_app": MATE}
            )
        elif filled == "live":
            state._slots["chat-1-100"].folder_id = INSIDE
        elif filled == "closing":
            closing.folder_id = INSIDE
        elif filled == "cron":
            state.crons.chat_folder_ids_async = AsyncMock(return_value={INSIDE})
        else:
            state.conversation_log = MagicMock()
            state.conversation_log.list_sessions_indexed = MagicMock(
                return_value=[{"folder_id": INSIDE}]
            )
        before = [dict(f) for f in state._folders]
        with close_in_flight(state, "chat-2-200", closing):
            async with TestClient(TestServer(_app(state))) as client:
                resp = await client.delete(f"/api/chat/folders/{INSIDE}", headers=HEADERS)
                assert resp.status == 409
                assert (await resp.json())["code"] == "folder_not_empty"
        assert state._folders == before

    async def test_an_unreadable_cron_store_deletes_nothing(self) -> None:
        state = _state()
        state.crons.chat_folder_ids_async = AsyncMock(side_effect=OSError("busy"))
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.delete(f"/api/chat/folders/{INSIDE}", headers=HEADERS)
            assert resp.status == 503
        assert state._folders == _folders()

    async def test_an_agent_tool_call_in_the_persons_session_may_not_delete_it(self) -> None:
        state = _state()
        internal = {**HEADERS, "X-Internal-Secret": "s", "X-Internal-Caller": "kirocrew-dashboard"}
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.delete(
                f"/api/chat/folders/{INSIDE}?if_empty=true", headers=internal
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "mate_home_locked"
        assert state._folders == _folders()

    async def test_the_cleanup_keeps_one_holding_an_archived_session(self) -> None:
        from kiro_crew.dashboard.chat_folder_cleanup import removable_folder_ids

        folders = _folders()
        ids = removable_folder_ids(folders, set(), include_top_level=True, archived={INSIDE})
        assert INSIDE not in ids
        # Archived sessions still do not keep a folder outside a Bot's home.
        assert OUTSIDE in removable_folder_ids(
            folders, set(), include_top_level=True, archived={OUTSIDE}
        )

    async def test_another_mate_may_not_delete_it(self) -> None:
        state = _state()
        other = "member:member-other-4567"
        async with TestClient(TestServer(_app(state, member=other))) as client:
            resp = await client.delete(f"/api/chat/folders/{INSIDE}", headers=HEADERS)
            assert resp.status == 403
        assert state._folders == _folders()


class _GatedCrons:
    """A cron store whose folder read can be held open, so a test fixes the
    order of a job save and a delete on the real folder-store lock."""

    def __init__(self, gate: bool) -> None:
        self.saved: set[str] = set()
        self.gate = gate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat_folder_ids_async(self) -> set[str]:
        if self.gate:
            self.entered.set()
            await self.release.wait()
        return set(self.saved)


def _real_state(tmp_path: Any, crons: _GatedCrons) -> DashboardState:
    state = _make_state(tmp_path)
    state._folders = _folders()
    state.crons = crons
    return state


def _person_request() -> web.Request:
    return make_mocked_request("DELETE", f"/api/chat/folders/{INSIDE}", headers=HEADERS)


async def _delete(state: DashboardState, request: web.Request, fid: str) -> web.Response:
    # Imported here so the rest of this file still runs without the module.
    from kiro_crew.dashboard.mate_subfolder_delete import delete_empty_bot_subfolder

    return await delete_empty_bot_subfolder(state, request, fid)


@pytest.mark.asyncio
class TestACronSaveDuringTheDeleteIsNeverLost:
    """A job save and the person's delete take the same folder-store lock, so
    one finishes first: a job saved first keeps the folder, and a job saved
    after the delete is refused. Neither ends with a job filed into a folder
    that is gone."""

    async def test_a_job_saved_first_keeps_the_folder(self, tmp_path: Any) -> None:
        crons = _GatedCrons(gate=False)
        state = _real_state(tmp_path, crons)
        save_entered = asyncio.Event()
        release_save = asyncio.Event()

        async def _persist() -> str:
            save_entered.set()
            await release_save.wait()
            crons.saved.add(INSIDE)
            return "saved"

        save = asyncio.create_task(_persist_holding_folder(state, INSIDE, _persist))
        await asyncio.wait_for(save_entered.wait(), 60)
        delete = asyncio.create_task(_delete(state, _person_request(), INSIDE))
        release_save.set()
        assert await asyncio.wait_for(save, 60) == "saved"
        resp = await asyncio.wait_for(delete, 60)
        assert resp.status == 409
        assert INSIDE in {f["id"] for f in state._folders}

    async def test_a_job_saved_after_the_delete_is_refused(self, tmp_path: Any) -> None:
        crons = _GatedCrons(gate=True)
        state = _real_state(tmp_path, crons)

        async def _persist() -> str:
            crons.saved.add(INSIDE)
            return "saved"

        delete = asyncio.create_task(_delete(state, _person_request(), INSIDE))
        await asyncio.wait_for(crons.entered.wait(), 60)
        save = asyncio.create_task(_persist_holding_folder(state, INSIDE, _persist))
        crons.release.set()
        resp = await asyncio.wait_for(delete, 60)
        assert resp.status == 200
        with pytest.raises(_ChatFolderGone):
            await asyncio.wait_for(save, 60)
        assert crons.saved == set()
        assert INSIDE not in {f["id"] for f in state._folders}


def _cancelling_state(*, lands: bool) -> DashboardState:
    """A state whose folder write is cancelled; *lands* says whether it was
    already confirmed, as ``FolderRepository.mutate`` reports on a cancel."""
    state = _state()

    async def _mutate(fn: Any, on_committed: Any = None, prepare: Any = None) -> Any:
        if prepare is not None:
            await prepare()
        if lands:
            fn(state._folders)
            if on_committed is not None:
                on_committed()
        raise asyncio.CancelledError()

    state.mutate_folders = AsyncMock(side_effect=_mutate)
    return state


@pytest.mark.asyncio
class TestACancelledDeleteStillAuditsWhatLanded:
    async def test_a_landed_delete_publishes_and_audits_before_the_cancel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.mate_subfolder_delete.sel", lambda: audit)
        state = _cancelling_state(lands=True)
        with pytest.raises(asyncio.CancelledError):
            await _delete(state, _person_request(), INSIDE)
        assert INSIDE not in {f["id"] for f in state._folders}
        state.push_slots_update.assert_called_once()
        audit.log_api_access.assert_called_once()
        assert audit.log_api_access.call_args.kwargs["outcome"] == "allowed"

    async def test_a_cancel_before_the_write_lands_publishes_and_audits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.mate_subfolder_delete.sel", lambda: audit)
        state = _cancelling_state(lands=False)
        with pytest.raises(asyncio.CancelledError):
            await _delete(state, _person_request(), INSIDE)
        assert state._folders == _folders()
        state.push_slots_update.assert_not_called()
        audit.log_api_access.assert_not_called()
