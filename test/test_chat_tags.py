"""Tests for chat_tags — tag vocabulary CRUD, slot tag assignment, sidebar columns, drag-drop."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard.chat_tags import _normalize_column, _valid_color
from kiro_crew.dashboard.state import _ChatSlot

# ── Pure helpers ──


class TestValidColor:
    def test_accepts_lowercase_hex(self):
        assert _valid_color("#abcdef") == "#abcdef"

    def test_accepts_uppercase_hex(self):
        assert _valid_color("#ABCDEF") == "#ABCDEF"

    def test_rejects_short_hex(self):
        assert _valid_color("#abc") == "#6b7280"

    def test_rejects_no_hash(self):
        assert _valid_color("ff0000") == "#6b7280"

    def test_rejects_garbage(self):
        assert _valid_color("not-a-color") == "#6b7280"


class TestNormalizeColumn:
    def _state_with_tags(self, tmp_path):
        state = _make_state(tmp_path)
        state._tags = [
            {"id": "t1", "name": "T1", "color": "#111111", "order": 0, "status": True},
            {"id": "t2", "name": "T2", "color": "#222222", "order": 1, "status": False},
        ]
        return state

    def test_returns_none_for_non_dict(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        assert _normalize_column(state, "not-a-dict") is None

    def test_filters_unknown_tag_ids(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {"tag_ids": ["t1", "ghost", "t2"]})
        assert col is not None
        assert col["tag_ids"] == ["t1", "t2"]

    def test_rejects_non_list_tag_ids(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        assert _normalize_column(state, {"tag_ids": "not-a-list"}) is None

    def test_rejects_invalid_mode(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        assert _normalize_column(state, {"mode": "fancy"}) is None

    def test_truncates_name_to_max(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {"name": "x" * 200})
        assert col is not None
        assert len(col["name"]) == 60

    def test_coerces_order_to_int(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {"order": "5"})
        assert col is not None
        assert col["order"] == 5

    def test_ignores_unparseable_order(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {"order": "abc"})
        assert col is not None
        # Default applied via setdefault
        assert col["order"] == 0

    def test_include_untagged_coerced_to_bool(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {"include_untagged": 1})
        assert col is not None
        assert col["include_untagged"] is True

    def test_defaults_when_empty_payload(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {})
        assert col == {
            "mode": "any",
            "tag_ids": [],
            "name": "",
            "order": 0,
            "include_untagged": False,
        }

    def test_existing_values_preserved_when_keys_absent(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        existing = {"id": "c1", "name": "Keep", "tag_ids": ["t1"], "mode": "all", "order": 7, "include_untagged": True}
        col = _normalize_column(state, {"name": "Updated"}, existing=existing)
        assert col is not None
        assert col["id"] == "c1"
        assert col["name"] == "Updated"
        assert col["tag_ids"] == ["t1"]
        assert col["mode"] == "all"
        assert col["order"] == 7
        assert col["include_untagged"] is True


# ── Tag vocabulary endpoints ──


class TestTagVocabulary:
    @pytest.mark.asyncio
    async def test_list_seeds_default_vocabulary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.load_tags()
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/tags")
            assert resp.status == 200
            tags = await resp.json()
            names = {t["name"] for t in tags}
            assert names == {"Planned", "ToDo", "Implementation", "Review", "Done"}
            assert all(t["status"] is True for t in tags)

    @pytest.mark.asyncio
    async def test_list_returns_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._tags = [
            {"id": "b", "name": "B", "color": "#000000", "order": 1, "status": False},
            {"id": "a", "name": "A", "color": "#000000", "order": 0, "status": False},
        ]
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/tags")
            tags = await resp.json()
            assert [t["name"] for t in tags] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_create_tag(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/tags", json={"name": "Spike", "color": "#22c55e", "status": False})
            assert resp.status == 201
            tag = await resp.json()
            assert tag["name"] == "Spike"
            assert tag["color"] == "#22c55e"
            assert tag["status"] is False
            assert "id" in tag
            assert (tmp_path / "tags.json").exists()

    @pytest.mark.asyncio
    async def test_create_tag_invalid_color_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/tags", json={"name": "Bug", "color": "not-a-color"})
            tag = await resp.json()
            assert tag["color"] == "#6b7280"

    @pytest.mark.asyncio
    async def test_create_tag_empty_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/tags", json={"name": "   "})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_tag_invalid_json_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/tags", data="not json", headers={"Content-Type": "application/json"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_tag_rename_recolor_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Old"})).json()
            resp = await client.patch(f"/api/chat/tags/{tag['id']}",
                                      json={"name": "New", "color": "#00ff00", "order": 9, "status": True})
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "New"
            assert data["color"] == "#00ff00"
            assert data["order"] == 9
            assert data["status"] is True

    @pytest.mark.asyncio
    async def test_update_tag_empty_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Keep"})).json()
            resp = await client.patch(f"/api/chat/tags/{tag['id']}", json={"name": "   "})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_tag_unparseable_order_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "X"})).json()
            resp = await client.patch(f"/api/chat/tags/{tag['id']}", json={"order": "abc"})
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_update_tag_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/tags/ghost", json={"name": "Anything"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_update_tag_invalid_json_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "X"})).json()
            resp = await client.patch(f"/api/chat/tags/{tag['id']}",
                                      data="not json", headers={"Content-Type": "application/json"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_delete_tag_strips_from_slots_and_columns(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Spike"})).json()
            other = await (await client.post("/api/chat/tags", json={"name": "Other"})).json()
            slot = _ChatSlot("s1")
            slot.tags = [tag["id"], other["id"]]
            state._slots["s1"] = slot
            col = await (await client.post(
                "/api/chat/tag-columns", json={"tag_ids": [tag["id"], other["id"]], "mode": "any"}
            )).json()
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 200
            assert tag["id"] not in {t["id"] for t in state._tags}
            assert slot.tags == [other["id"]]
            updated_col = next(c for c in state._tag_boards if c["id"] == col["id"])
            assert tag["id"] not in updated_col["tag_ids"]
            assert other["id"] in updated_col["tag_ids"]

    @pytest.mark.asyncio
    async def test_delete_tag_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete("/api/chat/tags/ghost")
            assert resp.status == 404


# ── Slot tag assignment ──


class TestSlotTags:
    @pytest.mark.asyncio
    async def test_assign_filters_unknown_and_dedupes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            t1 = await (await client.post("/api/chat/tags", json={"name": "T1"})).json()
            t2 = await (await client.post("/api/chat/tags", json={"name": "T2"})).json()
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [t1["id"], "ghost", t1["id"], t2["id"], 7]},
                )
            assert resp.status == 200
            data = await resp.json()
            assert data["tags"] == [t1["id"], t2["id"]]
            assert slot.tags == [t1["id"], t2["id"]]

    @pytest.mark.asyncio
    async def test_assign_slot_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/chat/slots/ghost/tags", json={"tags": []})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_assign_invalid_json_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/s1/tags", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_assign_non_array_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/chat/slots/s1/tags", json={"tags": "not-a-list"})
            assert resp.status == 400


# ── Sidebar columns ──


class TestColumns:
    @pytest.mark.asyncio
    async def test_list_columns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/tag-columns")
            assert resp.status == 200
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_create_column(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "T"})).json()
            resp = await client.post(
                "/api/chat/tag-columns",
                json={"name": "Lane", "tag_ids": [tag["id"]], "mode": "all"},
            )
            assert resp.status == 201
            col = await resp.json()
            assert col["name"] == "Lane"
            assert col["tag_ids"] == [tag["id"]]
            assert col["mode"] == "all"
            assert "id" in col
            assert (tmp_path / "tag_boards.json").exists()

    @pytest.mark.asyncio
    async def test_create_column_invalid_mode_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/tag-columns", json={"mode": "fancy"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_column_invalid_json_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/tag-columns", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_column(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            col = await (await client.post("/api/chat/tag-columns", json={"name": "Old"})).json()
            resp = await client.patch(f"/api/chat/tag-columns/{col['id']}",
                                      json={"name": "New", "include_untagged": True})
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "New"
            assert data["include_untagged"] is True

    @pytest.mark.asyncio
    async def test_update_column_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/tag-columns/ghost", json={"name": "X"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_update_column_invalid_payload_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            col = await (await client.post("/api/chat/tag-columns", json={})).json()
            resp = await client.patch(f"/api/chat/tag-columns/{col['id']}", json={"mode": "fancy"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_column_invalid_json_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            col = await (await client.post("/api/chat/tag-columns", json={})).json()
            resp = await client.patch(
                f"/api/chat/tag-columns/{col['id']}", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_delete_column(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            col = await (await client.post("/api/chat/tag-columns", json={})).json()
            resp = await client.delete(f"/api/chat/tag-columns/{col['id']}")
            assert resp.status == 200
            assert state._tag_boards == []

    @pytest.mark.asyncio
    async def test_delete_column_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete("/api/chat/tag-columns/ghost")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reorder_columns(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/tag-columns", json={"name": "A"})).json()
            b = await (await client.post("/api/chat/tag-columns", json={"name": "B"})).json()
            resp = await client.put("/api/chat/tag-columns/order", json={"ids": [b["id"], a["id"]]})
            assert resp.status == 200
            listed = await (await client.get("/api/chat/tag-columns")).json()
            assert [c["id"] for c in listed] == [b["id"], a["id"]]

    @pytest.mark.asyncio
    async def test_reorder_columns_with_int_id_does_not_crash_audit(self, tmp_path, monkeypatch):
        """Regression: review-bot flagged ',
        '.join(ids[:10]) raising TypeError on non-string elements,
        which would skip the SEL audit event after the state mutation."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/tag-columns", json={"name": "A"})).json()
            # Send a malformed ids list mixing string + int (the audit join must coerce).
            resp = await client.put("/api/chat/tag-columns/order", json={"ids": [a["id"], 42]})
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_reorder_invalid_json_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/tag-columns/order", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_reorder_non_list_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/chat/tag-columns/order", json={"ids": "not-a-list"})
            assert resp.status == 400


# ── Drag-drop semantics ──


class TestDrop:
    @pytest.mark.asyncio
    async def test_drop_on_status_lane_swaps_status_tag(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            todo = await (await client.post("/api/chat/tags", json={"name": "ToDo", "status": True})).json()
            done = await (await client.post("/api/chat/tags", json={"name": "Done", "status": True})).json()
            spike = await (await client.post("/api/chat/tags", json={"name": "spike", "status": False})).json()
            slot = _ChatSlot("s1")
            slot.tags = [todo["id"], spike["id"]]
            state._slots["s1"] = slot
            col = await (await client.post(
                "/api/chat/tag-columns", json={"tag_ids": [done["id"]], "mode": "any"}
            )).json()
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
            data = await resp.json()
            assert data["ok"] is True
            assert done["id"] in data["tags"]
            assert todo["id"] not in data["tags"]
            assert spike["id"] in data["tags"]

    @pytest.mark.asyncio
    async def test_drop_on_filter_only_column_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            todo = await (await client.post("/api/chat/tags", json={"name": "ToDo", "status": True})).json()
            slot = _ChatSlot("s1")
            slot.tags = [todo["id"]]
            state._slots["s1"] = slot
            col = await (await client.post(
                "/api/chat/tag-columns", json={"tag_ids": [], "mode": "any"}
            )).json()
            resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
            data = await resp.json()
            assert data["ok"] is False
            assert data["tags"] == [todo["id"]]

    @pytest.mark.asyncio
    async def test_drop_slot_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/ghost/drop", json={"column_id": "x"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_drop_column_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": "ghost"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_drop_invalid_json_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/s1/drop", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_drop_on_multi_tag_column_is_noop(self, tmp_path, monkeypatch):
        """Drop on a column with > 1 status tag is a no-op (not a single-status lane)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            todo = await (await client.post("/api/chat/tags", json={"name": "ToDo", "status": True})).json()
            done = await (await client.post("/api/chat/tags", json={"name": "Done", "status": True})).json()
            slot = _ChatSlot("s1")
            slot.tags = [todo["id"]]
            state._slots["s1"] = slot
            col = await (await client.post(
                "/api/chat/tag-columns", json={"tag_ids": [todo["id"], done["id"]], "mode": "any"}
            )).json()
            resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
            data = await resp.json()
            assert data["ok"] is False
            assert data["tags"] == [todo["id"]]


# ── load_tags safety: do not overwrite a present-but-corrupt tags.json ──


class TestLoadTagsSafety:
    def test_load_failure_does_not_overwrite_with_defaults(self, tmp_path, monkeypatch):
        """If tags.json exists but cannot be parsed, never silently overwrite it
        with the seed vocabulary — that would destroy the user's data."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        # Write an unreadable / corrupt tags file
        corrupt = tmp_path / "tags.json"
        corrupt.write_text("not-json-at-all", encoding="utf-8")
        original = corrupt.read_text(encoding="utf-8")
        state = _make_state(tmp_path)
        state.load_tags()
        # On parse failure: vocabulary stays empty AND the file is untouched.
        assert state._tags == []
        assert corrupt.read_text(encoding="utf-8") == original

    def test_missing_file_seeds_defaults(self, tmp_path, monkeypatch):
        """If tags.json doesn't exist, seed the default 5 status tags."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.load_tags()
        names = {t["name"] for t in state._tags}
        assert names == {"Planned", "ToDo", "Implementation", "Review", "Done"}
        assert (tmp_path / "tags.json").exists()

    def test_explicitly_empty_file_is_not_reseeded(self, tmp_path, monkeypatch):
        """If tags.json contains [], the user explicitly cleared every tag —
        do not re-seed defaults across restart."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "tags.json").write_text("[]", encoding="utf-8")
        state = _make_state(tmp_path)
        state.load_tags()
        assert state._tags == []
        # And the file content is preserved (no re-seed write).
        assert (tmp_path / "tags.json").read_text(encoding="utf-8") == "[]"


class TestReorderUniqueOrders:
    @pytest.mark.asyncio
    async def test_partial_reorder_does_not_collide(self, tmp_path, monkeypatch):
        """Reordering only a subset of columns must not leave older columns
        sharing an `order` value with the newly-renumbered ones."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            a = await (await client.post("/api/chat/tag-columns", json={"name": "A"})).json()
            b = await (await client.post("/api/chat/tag-columns", json={"name": "B"})).json()
            c = await (await client.post("/api/chat/tag-columns", json={"name": "C"})).json()
            # Only reorder the C-then-B subset; A is left implicit.
            resp = await client.put("/api/chat/tag-columns/order", json={"ids": [c["id"], b["id"]]})
            assert resp.status == 200
            listed = await (await client.get("/api/chat/tag-columns")).json()
            orders = {col["id"]: col["order"] for col in listed}
            # All three orders must be unique
            assert len(set(orders.values())) == 3
            # The explicitly-ordered ids land at 0 and 1, in submitted order.
            assert orders[c["id"]] == 0
            assert orders[b["id"]] == 1
            # The unmentioned A is pushed past the explicit ordering.
            assert orders[a["id"]] >= 2


class TestDropOnMixedColumn:
    @pytest.mark.asyncio
    async def test_drop_on_status_plus_filter_still_swaps_status(self, tmp_path, monkeypatch):
        """The docstring promises that a drop on a column with exactly one
        status tag swaps onto that status — additional non-status tags in
        the column's filter must not block the swap."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            todo = await (await client.post("/api/chat/tags", json={"name": "ToDo", "status": True})).json()
            done = await (await client.post("/api/chat/tags", json={"name": "Done", "status": True})).json()
            spike = await (await client.post("/api/chat/tags", json={"name": "spike", "status": False})).json()
            slot = _ChatSlot("s1")
            slot.tags = [todo["id"]]
            state._slots["s1"] = slot
            # Column is "Done AND spike" — exactly one status tag in the filter.
            col = await (await client.post(
                "/api/chat/tag-columns",
                json={"tag_ids": [done["id"], spike["id"]], "mode": "all"},
            )).json()
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
            data = await resp.json()
            assert data["ok"] is True
            assert done["id"] in data["tags"]
            assert todo["id"] not in data["tags"]
