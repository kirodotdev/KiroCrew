"""Dashboard chat: session folder CRUD and folder persistence."""

from __future__ import annotations

import asyncio
import json
import os
import re
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import (
    _make_app,
    _make_app_with_agent_routes,
    _make_folder_app,
    _make_state,
    close_before_resume,
)

from kiro_crew.testing.wait import until_parked

# ── Folder CRUD tests ──


class TestFolderCRUD:
    @pytest.mark.asyncio
    async def test_list_folders_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/folders")
            assert resp.status == 200
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_create_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Oncall"})
            assert resp.status == 201
            data = await resp.json()
            assert data["name"] == "Oncall"
            assert "id" in data
            assert data["collapsed"] is False
            # Persisted to disk
            assert (tmp_path / "folders.json").exists()

    @pytest.mark.asyncio
    async def test_create_folder_with_parent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Parent"})
            parent = await resp.json()
            resp = await client.post(
                "/api/chat/folders", json={"name": "Child", "parent_id": parent["id"]}
            )
            child = await resp.json()
            assert child["parent_id"] == parent["id"]

    @pytest.mark.asyncio
    async def test_create_folder_accepts_default_agent(self, tmp_path, monkeypatch):
        """The create modal collects the full folder config, so POST must take it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Payments", "default_agent": "kirocrew-dev"},
            )
            assert resp.status == 201
            data = await resp.json()
            assert data["default_agent"] == "kirocrew-dev"

    @pytest.mark.asyncio
    async def test_create_folder_accepts_palette_color(self, tmp_path, monkeypatch):
        """Create persists an allowlisted palette color and rejects others."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Redteam", "color": "#ef4444"}
            )
            assert resp.status == 201
            assert (await resp.json())["color"] == "#ef4444"
            bad = await client.post("/api/chat/folders", json={"name": "Bad", "color": "#123456"})
            assert bad.status == 400
            assert (await bad.json())["code"] == "color_invalid"

    def test_folder_color_palette_matches_frontend_catalog(self):
        """The backend allowlist and the frontend catalog must offer the same
        hues: the modal renders FOLDER_COLOR_PALETTE (folderColorCatalog.tsx)
        while create/PATCH validate against _FOLDER_COLOR_PALETTE, so drift
        means the UI offers a swatch the backend rejects with a 400."""
        from pathlib import Path

        from kiro_crew.dashboard.chat_folders import _FOLDER_COLOR_PALETTE

        catalog = (
            Path(__file__).resolve().parent.parent
            / "website"
            / "src"
            / "components"
            / "folderColorCatalog.tsx"
        )
        frontend = set(re.findall(r"#[0-9a-f]{6}", catalog.read_text(encoding="utf-8")))
        assert frontend == set(_FOLDER_COLOR_PALETTE)

    @pytest.mark.asyncio
    async def test_patch_color_set_and_clear(self, tmp_path, monkeypatch):
        """PATCH color: allowlisted value sets, empty string clears the key."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Blue"})
            fid = (await resp.json())["id"]
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"color": "#3b82f6"})
            assert resp.status == 200
            assert state._folders[0]["color"] == "#3b82f6"
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"color": ""})
            assert resp.status == 200
            assert "color" not in state._folders[0]

    @pytest.mark.asyncio
    async def test_create_folder_invalid_parent_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Orphan", "parent_id": "nonexistent"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_empty_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": ""})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_folder_rename(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Old"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"name": "New"})
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "New"

    @pytest.mark.asyncio
    async def test_a_failed_folder_write_restores_the_slots_it_unfiled(self, tmp_path, monkeypatch):
        """Delete must not half-land: unfiled slots go back if the store write fails.

        The handler unfiles the folder's slots first, then commits the folder
        removal. If that commit fails, leaving the slots unfiled strands
        conversations outside a folder that still exists. Restoring them is
        deliberately order-neutral — the reverse order strands a dangling
        folder_id instead, so undoing whichever half landed is what closes both.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "Work"})).json()
            slot = state.get_or_create_slot("in-folder")
            slot.folder_id = folder["id"]

            def boom(path, data):
                raise OSError("disk full")

            monkeypatch.setattr(state, "_atomic_write_json", boom)
            resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            assert resp.status >= 500

        assert slot.folder_id == folder["id"], (
            "the slot stayed unfiled after the folder removal failed; the delete "
            "half-landed and the conversation left a folder that still exists"
        )

    @pytest.mark.asyncio
    async def test_a_parent_deleted_under_the_lock_rejects_the_create(self, tmp_path, monkeypatch):
        """A child must not land with a dangling parent_id.

        Parent existence is validated before the store lock is taken, so a
        concurrent delete of that parent would otherwise persist an orphan. Both
        the sidebar and the folder picker surface orphans at the top level rather
        than hiding them, so this is a tree-shape defect rather than a
        disappearance — but still a folder nobody asked to put at the root.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            parent = await (await client.post("/api/chat/folders", json={"name": "P"})).json()

            real = state.mutate_folders

            async def delete_parent_then_apply(mutate):
                # Stand in for a concurrent DELETE landing while this create
                # waits for the store lock.
                state._folders[:] = [f for f in state._folders if f["id"] != parent["id"]]
                return await real(mutate)

            monkeypatch.setattr(state, "mutate_folders", delete_parent_then_apply)
            resp = await client.post(
                "/api/chat/folders", json={"name": "Child", "parent_id": parent["id"]}
            )
            assert resp.status == 400, (
                f"create was accepted with a deleted parent (status {resp.status}); "
                "the folder persists as an orphan"
            )
        assert not any(f.get("name") == "Child" for f in state._folders)

    @pytest.mark.asyncio
    async def test_update_folder_collapse(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "F"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"collapsed": True})
            data = await resp.json()
            assert data["collapsed"] is True

    @pytest.mark.asyncio
    async def test_update_folder_reparent_into_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            b = await (await client.post("/api/chat/folders", json={"name": "B"})).json()
            resp = await client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": a["id"]})
            assert resp.status == 200
            assert (await resp.json())["parent_id"] == a["id"]
            assert state._folders[1]["parent_id"] == a["id"]

    @pytest.mark.asyncio
    async def test_update_folder_reparent_to_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            b = await (
                await client.post("/api/chat/folders", json={"name": "B", "parent_id": a["id"]})
            ).json()
            # Empty string and null both mean "move to top level".
            resp = await client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": ""})
            assert resp.status == 200
            assert (await resp.json())["parent_id"] == ""
            resp = await client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": a["id"]})
            assert (await resp.json())["parent_id"] == a["id"]
            resp = await client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": None})
            assert resp.status == 200
            assert (await resp.json())["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_concurrent_opposite_reparents_cannot_persist_a_cycle(
        self, tmp_path, monkeypatch
    ):
        """Two opposite reparents must not both apply and orphan the pair.

        The tree-shape rules (parent exists, target is not a descendant) are
        checked once before the store lock as a fast reject, but a folder write
        is serialized and off-loop, so while one is in flight BOTH requests can
        validate against the same pre-state. If the winner's mutation were not
        re-tested under the lock, `A -> B` and `B -> A` would both persist and
        neither folder would be reachable from the root any more.

        The lock is held deliberately here so both handlers are provably queued
        on it before either applies -- the window is opened, not raced for.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            b = await (await client.post("/api/chat/folders", json={"name": "B"})).json()

            gate = asyncio.Event()

            async def _hold_the_store() -> None:
                async with state._folders_lock:
                    await gate.wait()

            holder = asyncio.create_task(_hold_the_store())
            while not state._folders_lock.locked():
                await asyncio.sleep(0)

            t1 = asyncio.create_task(
                client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": b["id"]})
            )
            t2 = asyncio.create_task(
                client.patch(f"/api/chat/folders/{b['id']}", json={"parent_id": a["id"]})
            )

            # Both requests have passed their pre-lock validation against the
            # SAME pre-state and are now waiting on the store. _folders_lock is a
            # LoopBoundLock; until_parked reads this loop's inner asyncio.Lock.
            await until_parked(state._folders_lock, count=2)
            gate.set()
            r1, r2 = await asyncio.gather(t1, t2)
            await holder

            assert sorted([r1.status, r2.status]) == [200, 409], (
                "both opposite reparents were accepted; the tree-shape rules are "
                "not being re-tested under the store lock"
            )

            parents = {f["id"]: f.get("parent_id", "") for f in state._folders}

            def _reaches_root(fid: str) -> bool:
                seen: set[str] = set()
                cur = fid
                while cur:
                    if cur in seen:
                        return False
                    seen.add(cur)
                    cur = parents.get(cur, "")
                return True

            assert all(
                _reaches_root(fid) for fid in parents
            ), f"a parent cycle was persisted: {parents}"
            stored = json.loads((tmp_path / state._FOLDERS_FILE).read_text(encoding="utf-8"))
            assert {f["id"]: f.get("parent_id", "") for f in stored} == parents

    @pytest.mark.asyncio
    async def test_update_folder_reparent_self_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            resp = await client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": a["id"]})
            assert resp.status == 400
            assert state._folders[0]["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_update_folder_reparent_unknown_parent_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            resp = await client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": "nope"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_folder_reparent_cycle_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/folders", json={"name": "A"})).json()
            b = await (
                await client.post("/api/chat/folders", json={"name": "B", "parent_id": a["id"]})
            ).json()
            c = await (
                await client.post("/api/chat/folders", json={"name": "C", "parent_id": b["id"]})
            ).json()
            # A -> C would create A > B > C > A
            resp = await client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": c["id"]})
            assert resp.status == 400
            assert "descendant" in (await resp.json())["error"]
            # Direct child is also a descendant
            resp = await client.patch(f"/api/chat/folders/{a['id']}", json={"parent_id": b["id"]})
            assert resp.status == 400
            assert state._folders[0]["parent_id"] == ""

    @pytest.mark.asyncio
    async def test_update_folder_rejected_field_leaves_no_partial_mutation(
        self, tmp_path, monkeypatch
    ):
        """A PATCH mixing a VALID field (name) with an INVALID one (bad parent_id)
        must be all-or-nothing: the 400 rejection must NOT persist the name change
        (validate-all-before-mutate)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            f = await (await client.post("/api/chat/folders", json={"name": "Original"})).json()
            resp = await client.patch(
                f"/api/chat/folders/{f['id']}",
                json={"name": "Renamed", "parent_id": "does-not-exist"},
            )
            assert resp.status == 400
            # The name change from the SAME rejected request must not have landed.
            assert state._folders[0]["name"] == "Original"
            # And a later valid update must not resurrect the rejected name.
            resp = await client.patch(f"/api/chat/folders/{f['id']}", json={"collapsed": True})
            assert resp.status == 200
            assert state._folders[0]["name"] == "Original"

    @pytest.mark.asyncio
    async def test_create_folder_hidden_defaults_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "F"})
            assert (await resp.json())["hidden"] is False
            resp = await client.get("/api/chat/folders")
            assert (await resp.json())[0]["hidden"] is False

    @pytest.mark.asyncio
    async def test_update_folder_hidden(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "F"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"hidden": True})
            assert (await resp.json())["hidden"] is True
            assert state._folders[0]["hidden"] is True
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"hidden": False})
            assert (await resp.json())["hidden"] is False

    @pytest.mark.asyncio
    async def test_folders_get_includes_history_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "f1", "name": "A", "order": 0, "collapsed": False},
            {"id": "f2", "name": "B", "order": 1, "collapsed": False},
        ]
        # History count is computed from the full session list (authoritative),
        # not the paginated client history window.
        monkeypatch.setattr(
            state.conversation_log,
            "list_sessions",
            lambda: [
                {"key": "a", "folder_id": "f1"},
                {"key": "b", "folder_id": "f1"},
                {"key": "c", "folder_id": "f2"},
                {"key": "d"},  # unfiled — ignored
            ],
        )
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/folders")
            folders = {f["id"]: f for f in await resp.json()}
            assert folders["f1"]["history_count"] == 2
            assert folders["f2"]["history_count"] == 1

    @pytest.mark.asyncio
    async def test_assign_slot_to_folder_unhides(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        state._folders = [
            {"id": "f1", "name": "Test", "order": 0, "collapsed": False, "hidden": True}
        ]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            # Moving a session into a hidden folder re-engages it (Model B) → un-hides.
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            assert resp.status == 200
            assert state._folders[0]["hidden"] is False

    @pytest.mark.asyncio
    async def test_resume_session_unhides_folder(self, tmp_path, monkeypatch):
        """Reviving a history session filed in a hidden folder un-hides it (Model B).

        Complements test_assign_slot_to_folder_unhides (the move path): here the
        re-engage happens via api_chat_slot_resume loading an archived session
        from history, which is the revive path that lets a hidden folder reappear.
        """
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "f1", "name": "Test", "order": 0, "collapsed": False, "hidden": True}
        ]
        # Create a session filed in f1, persist it to history, then drop the active
        # slot so resume loads it fresh from history (the revive path).
        slot = state.get_or_create_slot("revive1")
        slot.folder_id = "f1"
        slot.append("user", "old msg")
        slot.drain()
        _save_slot_to_history(state, slot, closed=True)
        # Closed BEFORE the resume begins, whatever the clock's resolution.
        close_before_resume(state.conversation_log, "dashboard:revive1")
        state._slots.pop("revive1", None)
        assert state._folders[0]["hidden"] is True  # still hidden before revive

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/revive1/resume", json={"key": "dashboard:revive1"}
            )
            assert resp.status == 200
        assert state._folders[0]["hidden"] is False  # revive re-engaged → un-hid

    @pytest.mark.asyncio
    async def test_resume_answers_with_the_session_folder(self, tmp_path, monkeypatch):
        """The resume answer names the folder the session is filed in.

        The client opens a collapsed folder path from this field when the resume
        takes the view; the slots push that also carries it can land after the
        switch, leaving the active row hidden. Both the revive path and the
        already-open path answer with it.
        """
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": True}]
        slot = state.get_or_create_slot("revive2")
        slot.folder_id = "f1"
        slot.append("user", "old msg")
        slot.drain()
        _save_slot_to_history(state, slot, closed=True)
        close_before_resume(state.conversation_log, "dashboard:revive2")
        state._slots.pop("revive2", None)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/revive2/resume", json={"key": "dashboard:revive2"}
            )
            assert resp.status == 200
            assert (await resp.json())["folder_id"] == "f1"
            # The session is open now, so a second resume takes the dedup path.
            again = await client.post(
                "/api/chat/slots/revive2/resume", json={"key": "dashboard:revive2"}
            )
            assert again.status == 200
            assert (await again.json())["folder_id"] == "f1"

    @pytest.mark.asyncio
    async def test_update_folder_default_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "MS&AD"})
            folder = await resp.json()
            resp = await client.patch(
                f"/api/chat/folders/{folder['id']}", json={"default_agent": "msad"}
            )
            data = await resp.json()
            assert data["default_agent"] == "msad"
            assert state._folders[0]["default_agent"] == "msad"

    @pytest.mark.asyncio
    async def test_update_folder_clear_default_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "f1", "name": "Test", "order": 0, "collapsed": False, "default_agent": "nissay"}
        ]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/folders/f1", json={"default_agent": ""})
            data = await resp.json()
            assert data["default_agent"] == ""

    @pytest.mark.asyncio
    async def test_create_folder_with_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        proj = tmp_path / "proj"
        proj.mkdir()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": str(proj)}
            )
            assert resp.status == 201
            data = await resp.json()
            assert data["project_dir"] == os.path.realpath(str(proj))

    @pytest.mark.asyncio
    async def test_create_folder_relative_project_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": "relative/path"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_nonexistent_project_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        missing = tmp_path / "does-not-exist"
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": str(missing)}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_folder_sensitive_project_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "P", "project_dir": "~/.ssh"}
            )
            assert resp.status == 400
            data = await resp.json()
            assert "sensitive" in data["error"]

    @pytest.mark.asyncio
    async def test_update_folder_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        proj = tmp_path / "proj2"
        proj.mkdir()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "P"})
            folder = await resp.json()
            resp = await client.patch(
                f"/api/chat/folders/{folder['id']}", json={"project_dir": str(proj)}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["project_dir"] == os.path.realpath(str(proj))

    @pytest.mark.asyncio
    async def test_slot_create_inherits_nearest_folder_project(self, tmp_path, monkeypatch):
        """The server owns folder inheritance when the client cache omits project."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        root_project = tmp_path / "root-project"
        parent_project = tmp_path / "parent-project"
        root_project.mkdir()
        parent_project.mkdir()
        state._folders = [
            {
                "id": "root",
                "name": "Root",
                "order": 0,
                "parent_id": "",
                "project_dir": str(root_project),
            },
            {
                "id": "parent",
                "name": "Parent",
                "order": 1,
                "parent_id": "root",
                "project_dir": str(parent_project),
            },
            {
                "id": "child",
                "name": "Child",
                "order": 2,
                "parent_id": "parent",
                "project_dir": "",
            },
        ]
        mock_cfg = MagicMock()
        mock_cfg.dashboard.default_project = ""
        # A bare MagicMock leaks into the slot: the handler stamps
        # cfg.default_agent (a truthy MagicMock) as the slot's agent when the
        # request names none, and the coalesced slots broadcast then dies in
        # json.dumps ("Object of type MagicMock is not JSON serializable"),
        # 500ing the create. Pin it to a string like the sibling cfg mocks.
        mock_cfg.default_agent = ""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda _workspace: str(root_project),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            lambda *_args, **_kwargs: None,
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "folder-project", "folder_id": "child"},
            )
            data = await resp.json()

        assert resp.status == 200
        assert data["folder_id"] == "child"
        assert data["project"] == os.path.realpath(str(parent_project))
        assert state._slots["folder-project"].project == data["project"]

    @pytest.mark.asyncio
    async def test_slot_create_rejects_invalid_inherited_project(self, tmp_path, monkeypatch):
        """A stale folder path fails before a partially configured slot is created."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {
                "id": "folder",
                "name": "Folder",
                "order": 0,
                "parent_id": "",
                "project_dir": str(tmp_path / "missing"),
            }
        ]

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "invalid-folder-project", "folder_id": "folder"},
            )
            data = await resp.json()

        assert resp.status == 400
        assert data["code"] == "folder_project_invalid"
        assert state._slots == {}

    @pytest.mark.asyncio
    async def test_slot_create_does_not_rescope_existing_named_slot(self, tmp_path, monkeypatch):
        """Folder inheritance initializes new slots; explicit project changes stay explicit."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        old_project = tmp_path / "old-project"
        folder_project = tmp_path / "folder-project"
        old_project.mkdir()
        folder_project.mkdir()
        slot = state.get_or_create_slot("existing")
        slot.project = str(old_project)
        slot.append("user", "existing conversation")
        slot.drain()
        state._folders = [
            {
                "id": "folder",
                "name": "Folder",
                "order": 0,
                "parent_id": "",
                "project_dir": str(folder_project),
            }
        ]
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            lambda *_args, **_kwargs: None,
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "existing", "folder_id": "folder"},
            )

        assert resp.status == 200
        assert slot.project == str(old_project)

    def test_resolve_folder_project_dir_terminates_on_cycle(self):
        from kiro_crew.dashboard.chat_folders import _resolve_folder_project_dir

        folders = [
            {"id": "a", "parent_id": "b", "project_dir": ""},
            {"id": "b", "parent_id": "a", "project_dir": ""},
        ]
        assert _resolve_folder_project_dir(folders, "a") == ("", None)

    @pytest.mark.asyncio
    async def test_update_folder_empty_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Keep"})
            folder = await resp.json()
            resp = await client.patch(f"/api/chat/folders/{folder['id']}", json={"name": "  "})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_nonexistent_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/folders/nonexistent", json={"name": "X"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Delete Me"})
            folder = await resp.json()
            resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            assert resp.status == 200
            resp = await client.get("/api/chat/folders")
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_delete_folder_reparents_children(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "parent", "name": "Parent", "order": 0, "collapsed": False},
            {"id": "child", "name": "Child", "order": 1, "collapsed": False, "parent_id": "parent"},
        ]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.delete("/api/chat/folders/parent")
            assert len(state._folders) == 1
            assert state._folders[0]["id"] == "child"
            assert state._folders[0].get("parent_id") == ""

    @pytest.mark.asyncio
    async def test_delete_folder_ungroups_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.folder_id = "f-del"
        state._folders.append({"id": "f-del", "name": "X", "order": 0, "collapsed": False})
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.delete("/api/chat/folders/f-del")
            assert slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_assign_slot_to_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            assert resp.status == 200
            data = await resp.json()
            assert data["folder_id"] == "f1"
            assert state._slots["myslot"].folder_id == "f1"

    @pytest.mark.asyncio
    async def test_slot_folder_change_sets_reinject_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            # Moving into a folder flags the slot for a one-shot [FOLDER] re-inject.
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            assert resp.status == 200
            assert slot._folder_changed is True
            # A no-op PATCH (same folder) must not re-flag.
            slot._folder_changed = False
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            assert resp.status == 200
            assert slot._folder_changed is False

    @pytest.mark.asyncio
    async def test_unassign_slot_from_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.folder_id = "f1"
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": ""})
            assert resp.status == 200
            assert state._slots["myslot"].folder_id == ""

    @pytest.mark.asyncio
    async def test_assign_folder_nonexistent_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/nope/folder", json={"folder_id": "f1"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_expected_created_matching_the_live_slot_files_it(self, tmp_path, monkeypatch):
        """The generation token is optional; when it matches the slot's own
        ``created_at`` the write lands exactly as an untokened one does."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/myslot/folder",
                json={"folder_id": "f1", "expected_created": slot.created_at},
            )
            assert resp.status == 200
            assert slot.folder_id == "f1"

    @pytest.mark.asyncio
    async def test_expected_created_from_a_replaced_slot_is_refused_409(
        self, tmp_path, monkeypatch
    ):
        """A caller that resolved a slot in an earlier request, then saw its
        tab close and the same key recreated for another conversation, carries
        the OLD slot's ``created_at``. The recreated slot has the same key and
        the same ``dashboard:<key>`` transcript key, so only the token can tell
        them apart — and it must, or the stale write files someone else's
        session."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        first = state.get_or_create_slot("myslot")
        stale_token = first.created_at
        # Close the tab and recreate the key: a different slot object, a
        # different birth stamp, the same key.
        del state._slots["myslot"]
        replacement = state.get_or_create_slot("myslot")
        replacement.created_at = stale_token + "-later"
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/myslot/folder",
                json={"folder_id": "f1", "expected_created": stale_token},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "session_gone"
            # The replacement conversation was not filed.
            assert replacement.folder_id == ""
            assert replacement._folder_changed is False

    @pytest.mark.asyncio
    async def test_assign_nonexistent_folder_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/myslot/folder", json={"folder_id": "nonexistent"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pin_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/pin", json={"pinned": True})
            assert resp.status == 200
            data = await resp.json()
            assert data["pinned"] is True
            assert state._slots["myslot"].pinned is True

    @pytest.mark.asyncio
    async def test_unpin_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.pinned = True
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/pin", json={"pinned": False})
            assert resp.status == 200
            assert state._slots["myslot"].pinned is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None, []])
    async def test_pin_slot_rejects_non_boolean_values(self, tmp_path, monkeypatch, value):
        """The API must not apply Python truthiness to JSON metadata."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/myslot/pin", json={"pinned": value})
            assert resp.status == 400
            payload = await resp.json()
            assert payload["error"] == "pinned must be a boolean"
            assert payload["code"] == "pinned_not_bool"
        assert slot.pinned is False

    @pytest.mark.asyncio
    async def test_slots_include_pinned(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.pinned = True
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            slots = await resp.json()
            assert any(s.get("pinned") is True for s in slots)

    @pytest.mark.asyncio
    async def test_slots_include_folder_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.folder_id = "f-abc"
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            slots = await resp.json()
            assert any(s["folder_id"] == "f-abc" for s in slots)


class TestFolderPersistence:
    def test_load_folders_from_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        import json

        (tmp_path / "folders.json").write_text(
            json.dumps([{"id": "f1", "name": "Test", "order": 0}])
        )
        state = _make_state(tmp_path)
        state.load_folders()
        assert len(state._folders) == 1
        assert state._folders[0]["name"] == "Test"

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [{"id": "f1", "name": "Roundtrip", "order": 0, "collapsed": True}]
        state.save_folders()
        state._folders = []
        state.load_folders()
        assert state._folders[0]["name"] == "Roundtrip"
        assert state._folders[0]["collapsed"] is True

    def test_load_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.load_folders()
        assert state._folders == []

    def test_load_corrupted_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "folders.json").write_text("not json")
        state = _make_state(tmp_path)
        state.load_folders()
        assert state._folders == []


class TestChatFolderOrderIsNotA500:
    """A caller-supplied order never reaches int() unguarded: no middleware maps
    handler exceptions, so it would surface as HTTP 500. A bad order is skipped."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_order", ["abc", None, [], {}, "1.5", 1e309, -1e309, "nan"])
    async def test_bad_order_does_not_500(self, tmp_path, monkeypatch, bad_order):
        """1e309 parses to float('inf'), which body_int turns into a ValueError
        where int(inf) would raise OverflowError."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            created = await (await client.post("/api/chat/folders", json={"name": "Oncall"})).json()
            resp = await client.patch(
                f"/api/chat/folders/{created['id']}", json={"order": bad_order}
            )
            assert resp.status == 200, f"order={bad_order!r} produced {resp.status}"
            # The bad field is skipped, not coerced into some other order.
            assert (await resp.json())["order"] == created["order"]

    @pytest.mark.asyncio
    async def test_valid_order_is_still_applied(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            created = await client.post("/api/chat/folders", json={"name": "Oncall"})
            folder_id = (await created.json())["id"]
            resp = await client.patch(f"/api/chat/folders/{folder_id}", json={"order": 7})
            assert resp.status == 200
            assert (await resp.json())["order"] == 7
