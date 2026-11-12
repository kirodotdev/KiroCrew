"""``DELETE /api/chat/folders/{id}?if_empty=true`` deletes only an empty folder.

This is the mode the ``chat_folder_delete`` MCP tool sends. It must never unfile
a session or lift a subfolder: occupancy is decided in the same locked step that
removes the folder, so a session filed after the caller's own read is still seen.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state

from kiro_crew.dashboard import chat_folders

#: The agent session the folders below are created and deleted by.
AGENT = "conductor"


def _agent(state: Any, slot: str = AGENT) -> dict[str, str]:
    """Headers of an MCP call made from session *slot* (created if absent).

    The internal secret is what tells an agent's call from the browser's; the
    session key names the live session the call is attributed to.
    """
    state.get_or_create_slot(slot)
    return {
        "X-Internal-Secret": "s3cret",
        "X-Internal-Caller": "kirocrew-dashboard",
        "X-Session-Key": f"dashboard:{slot}",
    }


async def _create(
    client: TestClient, state: Any, name: str, parent_id: str = "", *, by: str | None = AGENT
) -> dict[str, Any]:
    """Create a folder as agent session *by*, or as the person when ``by`` is None."""
    body: dict[str, Any] = {"name": name}
    if parent_id:
        body["parent_id"] = parent_id
    headers = _agent(state, by) if by else {}
    resp = await client.post("/api/chat/folders", json=body, headers=headers)
    assert resp.status in (200, 201), await resp.text()
    return await resp.json()


async def _agent_delete(client: TestClient, state: Any, fid: str, slot: str = AGENT) -> Any:
    return await client.delete(
        f"/api/chat/folders/{fid}?if_empty=true", headers=_agent(state, slot)
    )


def _ids(state: Any) -> set[str]:
    return {f["id"] for f in state._folders}


class TestIfEmptyDelete:
    @pytest.mark.asyncio
    async def test_an_empty_folder_is_deleted(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "Empty")
            resp = await _agent_delete(client, state, folder["id"])
            assert resp.status == 200
        assert folder["id"] not in _ids(state)

    @pytest.mark.asyncio
    async def test_a_live_session_keeps_the_folder(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "Work")
            slot = state.get_or_create_slot("filed")
            slot.folder_id = folder["id"]
            resp = await _agent_delete(client, state, folder["id"])
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "folder_not_empty"
        assert folder["id"] in _ids(state)
        assert slot.folder_id == folder["id"], "the empty-only delete unfiled a session"

    @pytest.mark.asyncio
    async def test_a_subfolder_keeps_the_folder(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            parent = await _create(client, state, "Parent")
            child = await _create(client, state, "Child", parent["id"])
            resp = await _agent_delete(client, state, parent["id"])
            assert resp.status == 409
        assert parent["id"] in _ids(state)
        kept = next(f for f in state._folders if f["id"] == child["id"])
        assert kept["parent_id"] == parent["id"], "the empty-only delete lifted a subfolder"

    @pytest.mark.asyncio
    async def test_an_archived_session_keeps_the_folder(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "Old")
            monkeypatch.setattr(
                chat_folders, "_folder_history_counts", lambda _state: {folder["id"]: 1}
            )
            resp = await _agent_delete(client, state, folder["id"])
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "folder_not_empty"
        assert "1" not in body["error"], "the refusal must not carry a count"
        assert folder["id"] in _ids(state)

    @pytest.mark.asyncio
    async def test_a_session_filed_during_the_archive_scan_is_still_seen(
        self, tmp_path, monkeypatch
    ) -> None:
        """The live-slot check runs after the scan's await, under the store lock.

        A pre-check before that await would pass here, and the delete would
        then unfile the session that landed.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "Racy")
            slot = state.get_or_create_slot("late")

            def _scan_while_a_session_is_filed(_state: Any) -> dict[str, int]:
                slot.folder_id = folder["id"]
                return {}

            monkeypatch.setattr(
                chat_folders, "_folder_history_counts", _scan_while_a_session_is_filed
            )
            resp = await _agent_delete(client, state, folder["id"])
            assert resp.status == 409
        assert folder["id"] in _ids(state)
        assert slot.folder_id == folder["id"]

    @pytest.mark.asyncio
    async def test_a_folder_gone_before_the_lock_is_not_found(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "Vanishing")

            def _scan_while_it_is_deleted(_state: Any) -> dict[str, int]:
                state._folders[:] = [f for f in state._folders if f["id"] != folder["id"]]
                return {}

            monkeypatch.setattr(chat_folders, "_folder_history_counts", _scan_while_it_is_deleted)
            resp = await _agent_delete(client, state, folder["id"])
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_without_the_flag_a_full_folder_is_still_deleted(
        self, tmp_path, monkeypatch
    ) -> None:
        """The person's own sidebar delete keeps unfiling, as it always did."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "Full")
            slot = state.get_or_create_slot("filed")
            slot.folder_id = folder["id"]
            resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            assert resp.status == 200
        assert folder["id"] not in _ids(state)
        assert slot.folder_id == ""


def _mark(state: Any, fid: str) -> str:
    row = next(f for f in state._folders if f["id"] == fid)
    return str(row.get(chat_folders.CREATED_BY_SESSION) or "")


class TestOnlyTheCreatingAgentsUntouchedFolder:
    """The empty-only delete removes a folder only while it still reads as the
    caller's own: created by the caller's session, never edited or used by the
    person since. The first test is the case that matters most: a folder the person made
    must survive an agent's cleanup even when it is empty."""

    @pytest.fixture
    def state(self, tmp_path, monkeypatch) -> Any:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        return _make_state(tmp_path)

    async def _refused(self, client: TestClient, state: Any, fid: str, slot: str = AGENT) -> None:
        resp = await _agent_delete(client, state, fid, slot)
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "folder_not_agent_owned"
        assert fid in _ids(state)

    @pytest.mark.asyncio
    async def test_a_folder_the_person_created_is_kept(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "ops", by=None)
            assert _mark(state, folder["id"]) == ""
            await self._refused(client, state, folder["id"])

    @pytest.mark.asyncio
    async def test_another_agent_sessions_folder_is_kept(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "theirs", by="other-lead")
            assert _mark(state, folder["id"]) == "dashboard:other-lead"
            await self._refused(client, state, folder["id"])

    @pytest.mark.asyncio
    async def test_reusing_the_persons_same_name_folder_does_not_mark_it(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            mine = await _create(client, state, "ops", by=None)
            reused = await _create(client, state, "ops")
            assert reused["id"] == mine["id"]
            assert _mark(state, mine["id"]) == ""
            await self._refused(client, state, mine["id"])

    @pytest.mark.asyncio
    async def test_a_browser_call_naming_a_session_is_not_an_agent(self, state) -> None:
        """Only the internal secret marks an agent; a session key alone is not enough."""
        state.get_or_create_slot(AGENT)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "typed"},
                headers={"X-Session-Key": f"dashboard:{AGENT}"},
            )
            assert resp.status == 201
            folder = await resp.json()
            assert _mark(state, folder["id"]) == ""
            await self._refused(client, state, folder["id"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "edit",
        [
            {"name": "renamed"},
            {"color": "#ef4444"},
            {"hidden": True},
            {"pinned": True},
            {"parent_id": ""},
        ],
    )
    async def test_a_person_editing_the_folder_claims_it(self, state, edit) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "work")
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json=edit)
            assert resp.status == 200, await resp.text()
            assert _mark(state, folder["id"]) == ""
            await self._refused(client, state, folder["id"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("edit", [{"collapsed": True}, {"order": 7}])
    async def test_a_layout_change_by_the_person_does_not_claim_it(self, state, edit) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "work")
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json=edit)
            assert resp.status == 200
            resp = await _agent_delete(client, state, folder["id"])
            assert resp.status == 200, await resp.text()
        assert folder["id"] not in _ids(state)

    @pytest.mark.asyncio
    async def test_a_person_regenerating_the_icon_claims_it(self, state, monkeypatch) -> None:
        """Auto-generate sends only ``regenerate_icon``, which never enters ``changes``."""
        monkeypatch.setattr(chat_folders, "_spawn_chat_folder_icon_task", lambda *a, **k: None)
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "work")
            resp = await client.patch(
                f"/api/chat/folders/{folder['id']}", json={"regenerate_icon": True}
            )
            assert resp.status == 200, await resp.text()
            assert _mark(state, folder["id"]) == ""
            await self._refused(client, state, folder["id"])

    @pytest.mark.asyncio
    async def test_a_person_moving_a_folder_in_and_out_claims_the_parent(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            parent = await _create(client, state, "work")
            mine = await _create(client, state, "notes", by=None)
            for dest in (parent["id"], ""):
                resp = await client.patch(
                    f"/api/chat/folders/{mine['id']}", json={"parent_id": dest}
                )
                assert resp.status == 200, await resp.text()
            assert _mark(state, parent["id"]) == ""
            await self._refused(client, state, parent["id"])

    @pytest.mark.asyncio
    async def test_an_agent_moving_its_folder_in_keeps_the_parent_mark(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            parent = await _create(client, state, "work")
            child = await _create(client, state, "sub")
            resp = await client.patch(
                f"/api/chat/folders/{child['id']}",
                json={"parent_id": parent["id"]},
                headers=_agent(state),
            )
            assert resp.status == 200, await resp.text()
            assert _mark(state, parent["id"]) == f"dashboard:{AGENT}"

    @pytest.mark.asyncio
    async def test_an_agent_editing_its_own_folder_keeps_the_mark(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "work")
            resp = await client.patch(
                f"/api/chat/folders/{folder['id']}", json={"name": "w2"}, headers=_agent(state)
            )
            assert resp.status == 200
            resp = await _agent_delete(client, state, folder["id"])
            assert resp.status == 200
        assert folder["id"] not in _ids(state)

    @pytest.mark.asyncio
    async def test_a_session_the_person_filed_then_removed_leaves_it_claimed(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "work")
            state.get_or_create_slot("mine")
            resp = await client.patch(
                "/api/chat/slots/mine/folder", json={"folder_id": folder["id"]}
            )
            assert resp.status == 200, await resp.text()
            resp = await client.patch("/api/chat/slots/mine/folder", json={"folder_id": ""})
            assert resp.status == 200
            assert _mark(state, folder["id"]) == ""
            await self._refused(client, state, folder["id"])

    @pytest.mark.asyncio
    async def test_an_agent_filing_a_session_keeps_the_mark(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "work")
            state.get_or_create_slot("worker")
            resp = await client.patch(
                "/api/chat/slots/worker/folder",
                json={"folder_id": folder["id"]},
                headers=_agent(state),
            )
            assert resp.status == 200, await resp.text()
            assert _mark(state, folder["id"]) == f"dashboard:{AGENT}"

    @pytest.mark.asyncio
    async def test_a_subfolder_the_person_made_and_removed_leaves_it_claimed(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            parent = await _create(client, state, "work")
            child = await _create(client, state, "notes", parent["id"], by=None)
            resp = await client.delete(f"/api/chat/folders/{child['id']}")
            assert resp.status == 200
            assert _mark(state, parent["id"]) == ""
            await self._refused(client, state, parent["id"])

    @pytest.mark.asyncio
    async def test_unhide_claims_only_for_the_person(self, state) -> None:
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            folder = await _create(client, state, "work")
        assert await chat_folders._unhide_folder(state, folder["id"])
        assert _mark(state, folder["id"]) == f"dashboard:{AGENT}"
        assert await chat_folders._unhide_folder(state, folder["id"], claim_for_person=True)
        assert _mark(state, folder["id"]) == ""

    @pytest.mark.asyncio
    async def test_the_claim_survives_a_reload(self, state, tmp_path) -> None:
        """The mark lives on the folder row in folders.json, so a restart keeps it."""
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            kept = await _create(client, state, "kept")
            claimed = await _create(client, state, "claimed")
            await client.patch(f"/api/chat/folders/{claimed['id']}", json={"name": "c2"})
        reloaded = _make_state(tmp_path)
        reloaded.load_folders()
        rows = {f["id"]: f for f in reloaded._folders}
        assert rows[kept["id"]].get(chat_folders.CREATED_BY_SESSION) == f"dashboard:{AGENT}"
        assert chat_folders.CREATED_BY_SESSION not in rows[claimed["id"]]
