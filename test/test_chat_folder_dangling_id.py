"""A dangling ``folder_id`` -- one no folder record carries -- is inert.

The sidebar files a session by its folder id, so a live slot naming a folder that
is gone would otherwise render in no lane at all: not in the folder (no block
exists for the id) and not in the unfiled lane (the id is non-empty). Every slot
payload passes through ``DashboardState.serialize_slot`` -- the full list, the
``slot_patch`` frame, the single-slot answers -- so that is the ONE place the
reading is made harmless: the payload says unfiled. It is a projection only: the
read path writes nothing, ``slot.folder_id`` keeps the stored value, and the
slot's next folder write replaces it.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state


def _folder(state, fid: str, name: str) -> None:
    state._folders.append({"id": fid, "name": name, "parent_id": "", "order": len(state._folders)})


def test_a_dangling_folder_id_projects_as_unfiled_and_is_not_written(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _folder(state, "fld-live", "Live")
    filed = state.get_or_create_slot("filed")
    filed.folder_id = "fld-live"
    dangling = state.get_or_create_slot("dangling")
    dangling.folder_id = "fld-gone"

    rows = {row["key"]: row for row in state.serialize_slots()}
    assert rows["filed"]["folder_id"] == "fld-live", "a filing into a real folder projects as is"
    assert rows["dangling"]["folder_id"] == "", "a dangling id projects as unfiled"
    # Every audience view is built through the same chokepoint.
    bare, dashboard_user, owner = state.serialize_slot_views(owner=True)
    for view in (bare, dashboard_user, owner or []):
        assert {r["key"]: r["folder_id"] for r in view}["dangling"] == ""
    # The single-slot projection agrees, with and without a precomputed id set.
    assert state.serialize_slot(dangling)["folder_id"] == ""
    assert state.serialize_slot(dangling, known_folder_ids={"fld-gone"})["folder_id"] == "fld-gone"
    # A projection only: the stored value stands and nothing was persisted.
    assert dangling.folder_id == "fld-gone"
    assert state.conversation_log.get_metadata("dashboard:dangling").get("folder_id", "") == ""


def test_a_stored_folder_id_of_the_wrong_shape_projects_as_unfiled(tmp_path, monkeypatch):
    """The stored value comes back from a file an agent's tools can edit, so a
    list or a dict where the id should be is a dangling id too: the projection
    reads it as unfiled instead of the membership test raising and taking the
    whole slots feed with it."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _folder(state, "fld-live", "Live")
    odd = state.get_or_create_slot("odd")
    odd.folder_id = ["fld-live"]  # type: ignore[assignment]
    other = state.get_or_create_slot("other")
    other.folder_id = {"id": "fld-live"}  # type: ignore[assignment]

    rows = {row["key"]: row for row in state.serialize_slots()}
    assert rows["odd"]["folder_id"] == "" and rows["other"]["folder_id"] == ""
    assert state.serialize_slot(odd, known_folder_ids={"fld-live"})["folder_id"] == ""
    assert odd.folder_id == ["fld-live"], "a projection only"


def test_the_freeze_predicates_never_hash_a_value_that_is_not_an_id(tmp_path, monkeypatch):
    """A list or a dict where the id should be is not frozen, is gone, and reads
    absent to the un-hide -- none of them raises."""
    import asyncio

    from kiro_crew.dashboard import chat_folders

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _folder(state, "fld-live", "Live")
    chat_folders._deleting_folder_ids(state).add("fld-live")
    try:
        for odd in (["fld-live"], {"id": "fld-live"}, 7):
            assert chat_folders.is_folder_id(odd) is False
            assert chat_folders.folder_is_deleting(state, odd) is False  # type: ignore[arg-type]
            assert chat_folders.folder_is_gone(state, odd) is True  # type: ignore[arg-type]
            assert asyncio.run(chat_folders._unhide_folder(state, odd)) is False  # type: ignore[arg-type]
            assert (
                asyncio.run(chat_folders._unhide_folder(state, odd, frozen_is_present=True))  # type: ignore[arg-type]
                is False
            )
        assert chat_folders.is_folder_id("fld-live") and not chat_folders.is_folder_id("")
        assert chat_folders.folder_is_deleting(state, "fld-live") is True
        assert chat_folders.folder_is_gone(state, "") is False
    finally:
        chat_folders._deleting_folder_ids(state).discard("fld-live")


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["", "?delete_contents=true"], ids=["default", "cascade"])
async def test_a_folder_delete_survives_a_live_slot_whose_folder_id_is_not_an_id(
    tmp_path, monkeypatch, flag
):
    """A live slot restored with ``folder_id: ["x"]`` (a hand-edited metadata
    line) must not fail every folder delete on the set lookup: the route reads
    such a slot as filed nowhere and deletes the asked folder, both modes. A
    folder row whose ``parent_id`` is not a string is left as it is."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _folder(state, "fld-doomed", "Doomed")
    state._folders.append({"id": "fld-odd-parent", "name": "Odd", "parent_id": ["x"], "order": 7})
    odd = state.get_or_create_slot("odd")
    odd.folder_id = ["x"]  # type: ignore[assignment]
    filed = state.get_or_create_slot("filed")
    filed.folder_id = "fld-doomed"
    monkeypatch.setattr("kiro_crew.dashboard.chat_folders.save_slot_off_loop", _saved_ok)

    async def _close(state_, slot, name, **_kw):
        state_._slots.pop(name, None)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _close)
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.delete(f"/api/chat/folders/fld-doomed{flag}")
    assert resp.status == 200, await resp.text()
    assert not any(f["id"] == "fld-doomed" for f in state._folders)
    assert odd.folder_id == ["x"], "a value that is not an id is not this delete's to touch"
    assert "odd" in state._slots
    if flag:
        assert "filed" not in state._slots, "the cascade archived the filed session"
    else:
        assert filed.folder_id == ""
    assert next(f for f in state._folders if f["id"] == "fld-odd-parent")["parent_id"] == ["x"]


async def _saved_ok(*_a, **_k):
    return True


def test_the_slot_patch_frame_carries_the_projected_reading(tmp_path, monkeypatch):
    """The patch frame is built from ``serialize_slot`` too, so a patch naming
    ``folder_id`` on a dangling slot says unfiled rather than the stored id."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("dangling")
    slot.folder_id = "fld-gone"
    frames: list[dict] = []
    monkeypatch.setattr(state, "_has_legacy_slots_audience", lambda: False)
    monkeypatch.setattr(state, "_has_slot_patch_clients", lambda: True)
    monkeypatch.setattr(state, "_send_slot_patch", lambda data: frames.append(data))

    state.push_slot_patch("dangling", ("folder_id",))

    assert frames == [{"slots": [{"key": "dangling", "folder_id": ""}]}]


@pytest.mark.asyncio
async def test_a_dangling_folder_id_is_replaced_by_the_slots_next_folder_write(
    tmp_path, monkeypatch
):
    """Cleared on the slot's next folder write, never by a read: the GET renders
    the session unfiled while the stored id stands; filing the session (or
    unfiling it) through the slot-folder route writes the new placement over it,
    in memory and on disk."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    _folder(state, "fld-live", "Live")
    slot = state.get_or_create_slot("dangling")
    slot.folder_id = "fld-gone"
    slot.append("user", "hello")
    slot.drain()
    log.update_metadata("dashboard:dangling", {"folder_id": "fld-gone"})

    async with TestClient(TestServer(_make_folder_app(state))) as client:
        listed = await client.get("/api/chat/slots")
        assert listed.status == 200
        assert next(r for r in await listed.json() if r["key"] == "dangling")["folder_id"] == ""
        # The read wrote nothing.
        assert slot.folder_id == "fld-gone"
        assert log.get_metadata("dashboard:dangling").get("folder_id") == "fld-gone"

        moved = await client.patch(
            "/api/chat/slots/dangling/folder", json={"folder_id": "fld-live"}
        )
        assert moved.status == 200, await moved.text()

    assert slot.folder_id == "fld-live"
    assert log.get_metadata("dashboard:dangling").get("folder_id") == "fld-live"
    assert state.serialize_slot(slot)["folder_id"] == "fld-live"


def test_a_filing_into_a_frozen_folder_projects_unfiled_only_while_frozen(tmp_path, monkeypatch):
    """Frozen is not gone. While a delete holds the folder frozen the payload says
    unfiled -- the session surfaces unfiled while the delete runs -- and the
    moment the freeze lifts (an abort) the same stored filing renders again.
    Nothing is written either way."""
    from kiro_crew.dashboard import chat_folders

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _folder(state, "fld-going", "Going")
    slot = state.get_or_create_slot("filed")
    slot.folder_id = "fld-going"
    assert state.serialize_slot(slot)["folder_id"] == "fld-going"
    chat_folders._deleting_folder_ids(state).add("fld-going")
    try:
        assert state.serialize_slot(slot)["folder_id"] == ""
        assert {r["key"]: r["folder_id"] for r in state.serialize_slots()}["filed"] == ""
        assert slot.folder_id == "fld-going", "a projection only"
    finally:
        chat_folders._deleting_folder_ids(state).discard("fld-going")
    assert state.serialize_slot(slot)["folder_id"] == "fld-going"
    assert state.conversation_log.get_metadata("dashboard:filed").get("folder_id", "") == ""
