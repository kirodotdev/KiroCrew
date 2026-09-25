"""``DELETE /api/chat/folders/{id}?delete_contents=<bool>``.

The default is the safe path the sidebar always had: unfile the folder's live
sessions, re-parent its direct children to the top level, remove the one row.
``delete_contents=true`` removes the whole subtree in one step: descendant folders
deleted, live sessions ARCHIVED through the same ``close_slot`` the tab ✕ uses
(never a hard delete), archived sessions unfiled from the folders that are gone.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state

from kiro_crew.dashboard import chat_folders, chat_handlers
from kiro_crew.dashboard.chat_handlers import SlotCloseError


async def _create(client: TestClient, name: str, parent_id: str = "") -> str:
    body = {"name": name}
    if parent_id:
        body["parent_id"] = parent_id
    resp = await client.post("/api/chat/folders", json=body)
    assert resp.status in (200, 201), await resp.text()
    return (await resp.json())["id"]


def _folder_ids(state) -> set[str]:
    return {f["id"] for f in state._folders}


def _live_slot(state, key: str, folder_id: str):
    slot = state.get_or_create_slot(key)
    slot.folder_id = folder_id
    slot.append("user", f"hello from {key}")
    slot.drain()
    return slot


def _archived_session(state, key: str, folder_id: str) -> None:
    log = state.conversation_log
    log.append(key, "user", f"archived {key}")
    log.update_metadata(key, {"folder_id": folder_id, "closed": True})


@pytest.mark.parametrize("flag", ["", "?delete_contents=false", "?delete_contents=0"])
@pytest.mark.asyncio
async def test_default_reparents_children_and_unfiles_slots_as_before(tmp_path, monkeypatch, flag):
    """Without the flag (or with it off) nothing changes: one row goes, the
    child survives at the top level, the live session stays live, unfiled."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        parent = await _create(client, "Shift")
        child = await _create(client, "TICKET-1", parent)
        slot = _live_slot(state, "in-shift", parent)
        resp = await client.delete(f"/api/chat/folders/{parent}{flag}")
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    assert body == {"ok": True}, "the safe path's body carries no counts"
    assert _folder_ids(state) == {child}
    assert next(f for f in state._folders if f["id"] == child)["parent_id"] == ""
    assert state._slots.get("in-shift") is slot, "the safe path must not archive"
    assert slot.folder_id == ""


@pytest.mark.asyncio
async def test_delete_contents_removes_nested_folders_and_archives_sessions(tmp_path, monkeypatch):
    """The shift-folder case: a subtree three deep with live and archived
    sessions at every level goes in ONE request, and nothing outside it moves."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Sep 25 - Oct 2 shift")
        ticket_a = await _create(client, "TICKET-A", shift)
        ticket_b = await _create(client, "TICKET-B", shift)
        nested = await _create(client, "TICKET-A/notes", ticket_a)
        other = await _create(client, "Unrelated")

        in_shift = _live_slot(state, "live-shift", shift)
        in_a = _live_slot(state, "live-a", ticket_a)
        in_nested = _live_slot(state, "live-nested", nested)
        outside = _live_slot(state, "live-outside", other)
        _archived_session(state, "dashboard:old-b", ticket_b)
        _archived_session(state, "dashboard:old-outside", other)

        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    # Folders: the whole subtree is gone, the unrelated folder untouched.
    assert _folder_ids(state) == {other}
    assert body["ok"] is True
    assert body["delete_contents"] is True
    assert body["deleted_folder_ids"] == sorted({shift, ticket_a, ticket_b, nested})
    # Live sessions in the subtree were ARCHIVED -- gone from the live table,
    # present in history as closed -- and unfiled there.
    for key in ("live-shift", "live-a", "live-nested"):
        assert key not in state._slots
        meta = log.get_metadata(f"dashboard:{key}")
        assert meta.get("closed") is True, f"{key} was not archived: {meta}"
        assert meta.get("folder_id", "") == ""
        assert log.read_messages(f"dashboard:{key}"), f"{key}'s transcript must survive"
    assert body["archived_sessions"] == 3
    assert body["unfiled_sessions"] == 0
    # The session archived earlier under TICKET-B is unfiled, still closed.
    old_b = log.get_metadata("dashboard:old-b")
    assert old_b.get("folder_id", "") == ""
    assert old_b.get("closed") is True
    # Three closing saves carried their folder plus the one archived earlier.
    assert body["unfiled_history_sessions"] == 4
    # Outside the subtree nothing changed, live or archived.
    assert state._slots.get("live-outside") is outside
    assert outside.folder_id == other
    assert log.get_metadata("dashboard:old-outside").get("folder_id") == other
    del in_shift, in_a, in_nested


@pytest.mark.asyncio
async def test_delete_contents_refuses_an_unknown_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        keep = await _create(client, "Keep")
        slot = _live_slot(state, "kept", keep)
        resp = await client.delete("/api/chat/folders/fldr00000000?delete_contents=true")
        assert resp.status == 404
    assert _folder_ids(state) == {keep}
    assert state._slots.get("kept") is slot and slot.folder_id == keep


@pytest.mark.asyncio
async def test_a_refused_close_stops_the_cascade_and_leaves_the_tree(tmp_path, monkeypatch):
    """A session that cannot be archived is the tab-✕'s own failure, surfaced
    with that close's code; the folder tree is not touched and the session stays
    live and filed, so the person retries from a state they can see."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)

    async def refuse(state_, slot, name, *, pre_pop_check=None):
        raise SlotCloseError("a history write is still running", code="history_write_running")

    monkeypatch.setattr(chat_handlers, "close_slot", refuse)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        slot = _live_slot(state, "stuck", ticket)
        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 500
        body = await resp.json()

    assert body["code"] == "history_write_running"
    assert body["archived_sessions"] == 0
    assert body["session"] == "stuck"
    assert _folder_ids(state) == {shift, ticket}
    assert state._slots.get("stuck") is slot
    assert slot.folder_id == ticket


class _Interleave:
    """Run one HTTP request from inside the cascade's first ``close_slot`` await
    window -- after the subtree was frozen, before any folder is removed -- and
    record its outcome, then let the real close proceed."""

    def __init__(self, monkeypatch, client: TestClient, send):
        self.results: list[tuple[int, dict]] = []
        real_close = chat_handlers.close_slot

        async def close_after_interleaving(state_, slot, name, *, pre_pop_check=None):
            if not self.results:
                resp = await send(client)
                body = await resp.json() if resp.content_type == "application/json" else {}
                self.results.append((resp.status, body))
            await real_close(state_, slot, name, pre_pop_check=pre_pop_check)

        monkeypatch.setattr(chat_handlers, "close_slot", close_after_interleaving)


def _deleting(state) -> set[str]:
    return set(chat_folders._deleting_folder_ids(state))


@pytest.mark.asyncio
async def test_a_child_created_under_the_frozen_subtree_is_refused(tmp_path, monkeypatch):
    """The subtree is frozen for the cascade's duration, so a folder cannot be
    born under it with sessions no pass of this delete has seen: the create is
    refused with a typed error and the cascade removes exactly its snapshot."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        _live_slot(state, "early", ticket)
        race = _Interleave(
            monkeypatch,
            client,
            lambda c: c.post("/api/chat/folders", json={"name": "TICKET-2", "parent_id": ticket}),
        )
        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    assert race.results == [
        (400, {"error": "parent folder is being deleted", "code": "folder_parent_deleting"})
    ]
    assert body["deleted_folder_ids"] == sorted({shift, ticket})
    assert body["archived_sessions"] == 1
    assert _folder_ids(state) == set()
    assert _deleting(state) == set(), "the freeze must be released"


@pytest.mark.asyncio
async def test_reparenting_a_child_out_of_the_frozen_subtree_is_refused(tmp_path, monkeypatch):
    """A child cannot leave the subtree while the cascade runs: the set the
    archive pass walks is the set ``_remove`` deletes, so a session archived from
    that child never ends up filed under a folder that survived."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        _live_slot(state, "in-ticket", ticket)
        race = _Interleave(
            monkeypatch,
            client,
            lambda c: c.patch(f"/api/chat/folders/{ticket}", json={"parent_id": ""}),
        )
        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    assert race.results == [(409, {"error": "folder is being deleted", "code": "folder_deleting"})]
    assert body["deleted_folder_ids"] == sorted({shift, ticket})
    assert _folder_ids(state) == set()
    assert "in-ticket" not in state._slots
    meta = log.get_metadata("dashboard:in-ticket")
    assert meta.get("closed") is True and meta.get("folder_id", "") == ""
    assert _deleting(state) == set()


@pytest.mark.asyncio
async def test_reparenting_a_folder_into_the_frozen_subtree_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        outside = await _create(client, "Outside")
        _live_slot(state, "in-ticket", ticket)
        race = _Interleave(
            monkeypatch,
            client,
            lambda c: c.patch(f"/api/chat/folders/{outside}", json={"parent_id": ticket}),
        )
        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 200, await resp.text()

    assert race.results == [(409, {"error": "folder is being deleted", "code": "folder_deleting"})]
    assert _folder_ids(state) == {outside}
    assert next(f for f in state._folders if f["id"] == outside)["parent_id"] == ""


@pytest.mark.asyncio
async def test_filing_a_session_into_the_frozen_subtree_is_refused_and_nothing_dangles(
    tmp_path, monkeypatch
):
    """A filing whose existence check would pass but whose save would land after
    the removal is refused up front: the store reports a frozen folder absent, so
    no live or persisted session names a folder that is gone."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        _live_slot(state, "in-ticket", ticket)
        outside = _live_slot(state, "outside", "")
        race = _Interleave(
            monkeypatch,
            client,
            lambda c: c.patch("/api/chat/slots/outside/folder", json={"folder_id": ticket}),
        )
        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 200, await resp.text()
        body = await resp.json()

        # Filing into the deleted folder AFTER the cascade is the plain 400.
        late = await client.patch("/api/chat/slots/outside/folder", json={"folder_id": ticket})
        assert late.status == 400, await late.text()

    assert race.results == [(400, {"error": "folder not found", "code": "folder_not_found"})]
    assert state._slots.get("outside") is outside and outside.folder_id == ""
    assert log.get_metadata("dashboard:outside").get("folder_id", "") == ""
    assert body["archived_sessions"] == 1 and body["unfiled_sessions"] == 0
    assert _folder_ids(state) == set()


@pytest.mark.asyncio
async def test_a_second_delete_of_a_frozen_folder_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        _live_slot(state, "in-ticket", ticket)
        race = _Interleave(monkeypatch, client, lambda c: c.delete(f"/api/chat/folders/{ticket}"))
        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 200, await resp.text()

    assert race.results == [(409, {"error": "folder is being deleted", "code": "folder_deleting"})]
    assert _folder_ids(state) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["", "?delete_contents=true"])
async def test_the_commit_itself_unfiles_a_slot_filed_after_the_unfile_snapshot(
    tmp_path, monkeypatch, flag
):
    """A filing path that consults neither the store nor the freeze (a cron
    placement, an inherited fork folder) can set a slot's ``folder_id`` after the
    unfile loop's snapshot. The removal callback sweeps live slots in the same
    synchronous step as the commit, so no live or persisted slot names a folder
    the commit removed."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    real_mutate = state.mutate_folders
    late: dict[str, object] = {}

    async def file_late_then_commit(mutate, on_committed=None):
        if getattr(mutate, "__name__", "") == "_remove" and "slot" not in late:
            # Lands after every pre-commit pass, as a detached placement would.
            late["slot"] = _live_slot(state, "late", str(late["folder"]))
        return await real_mutate(mutate)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        late["folder"] = ticket if flag else shift
        monkeypatch.setattr(state, "mutate_folders", file_late_then_commit)
        resp = await client.delete(f"/api/chat/folders/{shift}{flag}")
        assert resp.status == 200, await resp.text()

    slot = late["slot"]
    assert state._slots.get("late") is slot, "swept, not archived: it stays live"
    assert slot.folder_id == ""
    assert log.get_metadata("dashboard:late").get("folder_id", "") == ""
    assert not any(s.folder_id in {shift, ticket} for s in state._slots.values())
    assert shift not in _folder_ids(state)


@pytest.mark.asyncio
async def test_a_frozen_folder_reads_absent_to_a_new_filing_and_present_to_a_stored_one(
    tmp_path, monkeypatch
):
    """The two readings of a frozen folder, and that neither writes it."""
    from kiro_crew.dashboard.arrival_folders import arrival_folder_exists

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        folder = await _create(client, "Going")
        hidden = await client.patch(f"/api/chat/folders/{folder}", json={"hidden": True})
        assert hidden.status == 200, await hidden.text()
        chat_folders._deleting_folder_ids(state).add(folder)
        try:
            assert await chat_folders._unhide_folder(state, folder) is False
            assert await chat_folders._unhide_folder(state, folder, frozen_is_present=True) is True
            assert await arrival_folder_exists(state, folder) is False
            assert await arrival_folder_exists(state, folder, frozen_is_present=True) is True
        finally:
            chat_folders._deleting_folder_ids(state).discard(folder)
        row = next(f for f in state._folders if f["id"] == folder)
        assert row.get("hidden") is True, "neither reading writes a frozen folder"
        # Thawed, the ordinary reading resumes: present, and the un-hide lands.
        assert await chat_folders._unhide_folder(state, folder) is True
        assert not next(f for f in state._folders if f["id"] == folder).get("hidden")


@pytest.mark.asyncio
async def test_the_freeze_is_released_when_a_refused_close_aborts_the_cascade(
    tmp_path, monkeypatch
):
    """An aborted cascade leaves the tree intact AND writable: the frozen set is
    emptied in ``finally``, so the folders it could not delete accept mutations."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)

    async def refuse(state_, slot, name, *, pre_pop_check=None):
        raise SlotCloseError("a history write is still running", code="history_write_running")

    monkeypatch.setattr(chat_handlers, "close_slot", refuse)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        _live_slot(state, "stuck", ticket)
        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 500
        assert _deleting(state) == set()
        moved = await client.patch(f"/api/chat/folders/{ticket}", json={"parent_id": ""})
        assert moved.status == 200, await moved.text()
        child = await _create(client, "TICKET-2", ticket)

    assert _folder_ids(state) == {shift, ticket, child}
    assert next(f for f in state._folders if f["id"] == ticket)["parent_id"] == ""


@pytest.mark.asyncio
async def test_a_session_moved_out_of_the_subtree_during_its_close_is_not_archived(
    tmp_path, monkeypatch
):
    """The archive pass read the session's folder before ``close_slot`` awaited; a
    move committed inside that window makes it a session the person just placed
    elsewhere. The synchronous pre-pop re-check unwinds that close, the session
    stays live in its new folder, and the cascade goes on without it."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    real_close = chat_handlers.close_slot
    moved: list[str] = []

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        keep = await _create(client, "Keep")
        moving = _live_slot(state, "moving", ticket)
        _live_slot(state, "staying", ticket)

        async def move_out_then_close(state_, slot, name, *, pre_pop_check=None):
            if name == "moving" and not moved:
                # The drag-out lands while this close is suspended in its awaits,
                # after the loop's membership read and before the pop.
                slot.folder_id = keep
                moved.append(name)
            await real_close(state_, slot, name, pre_pop_check=pre_pop_check)

        monkeypatch.setattr(chat_handlers, "close_slot", move_out_then_close)
        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    assert moved == ["moving"]
    assert state._slots.get("moving") is moving, "the moved session must stay live"
    assert moving.folder_id == keep
    assert not moving.is_closing, "the aborted close must release its fence"
    assert log.get_metadata("dashboard:moving").get("closed") is not True
    assert "staying" not in state._slots
    assert log.get_metadata("dashboard:staying").get("closed") is True
    assert body["archived_sessions"] == 1
    assert body["unfiled_sessions"] == 0
    assert body["deleted_folder_ids"] == sorted({shift, ticket})
    assert _folder_ids(state) == {keep}


@pytest.mark.asyncio
async def test_a_slot_popped_while_the_unfile_loop_awaits_does_not_crash_the_cascade(
    tmp_path, monkeypatch
):
    """A slot another retraction already owns is skipped by the archive pass and
    reached by the unfile loop, whose save awaits; that retraction's pop lands
    meanwhile. The loop walks a snapshot, so the cascade completes and the folders
    go instead of a ``RuntimeError`` escaping with the tree intact."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    real_save = chat_folders.save_slot_off_loop
    popped: list[str] = []

    async def save_after_a_concurrent_pop(state_, slot, **kwargs):
        if slot.key == "closing" and not popped:
            # The other close reaches its point of no return while this save
            # awaits.
            state._slots.pop("closing")
            popped.append("closing")
        return await real_save(state_, slot, **kwargs)

    monkeypatch.setattr(chat_folders, "save_slot_off_loop", save_after_a_concurrent_pop)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        closing = _live_slot(state, "closing", ticket)
        closing.begin_close()
        resp = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert resp.status == 200, await resp.text()
        body = await resp.json()

    assert popped == ["closing"]
    assert _folder_ids(state) == set()
    assert body["deleted_folder_ids"] == sorted({shift, ticket})
    assert body["archived_sessions"] == 0
    assert body["unfiled_sessions"] == 1
    assert "closing" not in state._slots
    assert closing.folder_id == ""


def test_the_archived_unfile_is_a_compare_and_set_that_recreates_nothing():
    """The pass lists sessions, then writes each one later. Between the two a
    session can be refiled elsewhere (the guard must refuse) or deleted (the
    write must not recreate it as a metadata-only stub)."""
    listed = [
        {"key": "still", "folder_id": "gone-1"},
        {"key": "refiled", "folder_id": "gone-1"},
        {"key": "deleted", "folder_id": "gone-2"},
        {"key": "", "folder_id": "gone-1"},
    ]
    on_disk: dict[str, dict] = {
        "still": {"folder_id": "gone-1", "closed": True},
        "refiled": {"folder_id": "kept", "closed": True},
    }
    calls: list[tuple[str, dict, bool]] = []

    class _Log:
        def list_sessions(self):
            return list(listed)

        def update_metadata_if(self, key, fields, guard, *, require_existing=False):
            calls.append((key, fields, require_existing))
            meta = on_disk.get(key)
            if meta is None:
                if require_existing:
                    return False
                on_disk[key] = dict(fields)  # the stub the flag exists to prevent
                return True
            if not guard(meta):
                return False
            meta.update(fields)
            return True

    cleared = chat_folders._clear_archived_folder_assignments(_Log(), {"gone-1", "gone-2"})

    assert cleared == 1
    assert [c[0] for c in calls] == ["still", "refiled", "deleted"], "an empty key is skipped"
    assert all(c[1] == {"folder_id": ""} and c[2] is True for c in calls)
    assert on_disk["still"] == {"folder_id": "", "closed": True}
    assert on_disk["refiled"] == {"folder_id": "kept", "closed": True}
    assert "deleted" not in on_disk


@pytest.mark.asyncio
async def test_a_failed_folder_commit_after_the_archive_pass_is_audited_and_retryable(
    tmp_path, monkeypatch
):
    """The archive pass cannot be undone, so a folders.json write that fails
    after it leaves the state a refused close leaves: the sessions are in
    History, filed under folders that still exist. The escape is audited with the
    archive count, and a retry finds nothing live left and commits."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(chat_folders, "sel", lambda: _Sel())
    real_mutate = state.mutate_folders
    failures: list[str] = []

    async def fail_once(mutate):
        # Only the removal commit fails; the freeze and thaw callbacks that share
        # the store lock go through.
        if getattr(mutate, "__name__", "") == "_remove" and not failures:
            failures.append("torn write")
            raise OSError("folders.json: short write")
        return await real_mutate(mutate)

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        shift = await _create(client, "Shift")
        ticket = await _create(client, "TICKET-1", shift)
        _live_slot(state, "live", ticket)
        monkeypatch.setattr(state, "mutate_folders", fail_once)
        first = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert first.status == 500
        # Archived by the pass, still filed under the folder that still exists.
        assert "live" not in state._slots
        assert log.get_metadata("dashboard:live").get("closed") is True
        assert log.get_metadata("dashboard:live").get("folder_id") == ticket
        assert _folder_ids(state) == {shift, ticket}
        error_rows = [e for e in events if e.get("outcome") == "error"]
        assert len(error_rows) == 1
        assert error_rows[0]["operation"] == "chat.folder_delete"
        assert error_rows[0]["resources"] == f"{shift} delete_contents archived=1"
        assert error_rows[0]["error"] == "OSError"

        retry = await client.delete(f"/api/chat/folders/{shift}?delete_contents=true")
        assert retry.status == 200, await retry.text()
        body = await retry.json()

    assert body["deleted_folder_ids"] == sorted({shift, ticket})
    assert body["archived_sessions"] == 0
    assert body["unfiled_history_sessions"] == 1
    assert _folder_ids(state) == set()
    assert log.get_metadata("dashboard:live").get("folder_id", "") == ""


@pytest.mark.asyncio
async def test_delete_contents_on_a_leaf_folder_reports_zero_counts(tmp_path, monkeypatch):
    """The counts are the contract the sidebar's second confirm step will show,
    so an empty folder answers zeros rather than omitting them."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        leaf = await _create(client, "Empty")
        resp = await client.delete(f"/api/chat/folders/{leaf}?delete_contents=true")
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body == {
        "ok": True,
        "delete_contents": True,
        "deleted_folder_ids": [leaf],
        "unfiled_sessions": 0,
        "archived_sessions": 0,
        "unfiled_history_sessions": 0,
    }
    assert _folder_ids(state) == set()
