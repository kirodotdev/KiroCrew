"""Tests for chat_tags — tag vocabulary CRUD, slot tag assignment, sidebar columns, drag-drop."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard import chat_tags as chat_tags_module
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

    def test_rejects_unknown_tag_ids(self, tmp_path):
        # An unknown id must reject the payload, not be silently dropped —
        # a drop lands the column on tag_ids [] (match-all), which presents
        # to the user as "the filter does nothing".
        state = self._state_with_tags(tmp_path)
        assert _normalize_column(state, {"tag_ids": ["t1", "ghost", "t2"]}) is None

    def test_accepts_known_tag_ids(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {"tag_ids": ["t1", "t2"]})
        assert col is not None
        assert col["tag_ids"] == ["t1", "t2"]

    def test_accepts_empty_tag_ids_as_match_all(self, tmp_path):
        # [] is the documented clear-filter/match-all state; it must stay valid.
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {"tag_ids": []})
        assert col is not None
        assert col["tag_ids"] == []

    def test_rejects_non_string_tag_id_entries(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        assert _normalize_column(state, {"tag_ids": ["t1", 42]}) is None

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
            "source": "tags",
            "state_key": "",
        }

    def test_existing_values_preserved_when_keys_absent(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        existing = {
            "id": "c1",
            "name": "Keep",
            "tag_ids": ["t1"],
            "mode": "all",
            "order": 7,
            "include_untagged": True,
        }
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
            resp = await client.post(
                "/api/chat/tags", json={"name": "Spike", "color": "#22c55e", "status": False}
            )
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
            resp = await client.post(
                "/api/chat/tags", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_tag_rename_recolor_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Old"})).json()
            resp = await client.patch(
                f"/api/chat/tags/{tag['id']}",
                json={"name": "New", "color": "#00ff00", "order": 9, "status": True},
            )
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
            resp = await client.patch(
                f"/api/chat/tags/{tag['id']}",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_tag_redacts_credentials_in_name(self, tmp_path, monkeypatch):
        """PATCH a tag name containing an AWS key — the credential is redacted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Safe"})).json()
            resp = await client.patch(
                f"/api/chat/tags/{tag['id']}",
                json={"name": "key-AKIAIOSFODNN7EXAMPLE"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "AKIAIOSFODNN7EXAMPLE" not in data["name"]

    @pytest.mark.asyncio
    async def test_update_tag_redacts_credential_straddling_truncation(self, tmp_path, monkeypatch):
        """Redaction must run BEFORE truncation: a credential crossing the
        60-char cut would otherwise be sliced into a fragment the scanners
        no longer recognize, persisting a raw key prefix."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Redact"})).json()
            resp = await client.patch(
                f"/api/chat/tags/{tag['id']}",
                json={"name": "x" * 50 + "AKIAIOSFODNN7EXAMPLE"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "AKIA" not in data["name"]

    @pytest.mark.asyncio
    async def test_create_tag_redacts_credential_straddling_truncation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/tags",
                json={"name": "x" * 50 + "AKIAIOSFODNN7EXAMPLE"},
            )
            assert resp.status == 201
            data = await resp.json()
            assert "AKIA" not in data["name"]

    @pytest.mark.asyncio
    async def test_delete_tag_persists_vocab_before_slots(self, tmp_path, monkeypatch):
        """Crash-atomic ordering: tags.json is the single durable commit and
        must be written BEFORE any slot strip — a crash after it leaves only
        harmless dangling slot ids (pruned on load), never lost assignments
        with a still-live tag."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        call_order: list[str] = []

        async def _tracking_save(st, slot, **kwargs):
            call_order.append("save_slot")
            return True  # a committed save — None would read as the refusal

        original_write = __import__(
            "kiro_crew.dashboard.chat_tags", fromlist=["_write_tags_snapshot"]
        )._write_tags_snapshot

        def _tracking_write(st, snapshot):
            call_order.append("write_tags_snapshot")
            return original_write(st, snapshot)

        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Del"})).json()
            slot = _ChatSlot("s1")
            slot.tags = [tag["id"]]
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", _tracking_save):
                with patch("kiro_crew.dashboard.chat_tags._write_tags_snapshot", _tracking_write):
                    resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 200
            # Vocabulary removal must be committed BEFORE slot persistence.
            assert call_order.index("write_tags_snapshot") < call_order.index("save_slot")

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
            col = await (
                await client.post(
                    "/api/chat/tag-columns",
                    json={"tag_ids": [tag["id"], other["id"]], "mode": "any"},
                )
            ).json()
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

    @pytest.mark.asyncio
    async def test_delete_strips_all_slots_despite_partial_save_failure(
        self, tmp_path, monkeypatch
    ):
        """A failing slot save during the post-commit strip must not abort
        stripping the remaining slots; the failed one is marked dirty."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Partial"})).json()
            slot_a = _ChatSlot("s1")
            slot_a.tags = [tag["id"]]
            slot_b = _ChatSlot("s2")
            slot_b.tags = [tag["id"]]
            state._slots["s1"] = slot_a
            state._slots["s2"] = slot_b

            saves: list[str] = []

            async def _partial_failing_save(state_arg, slot_arg, **kwargs):
                saves.append(slot_arg.key)
                if slot_arg.key == "s1":
                    raise IOError("disk full")
                return True  # a committed save — None would read as the refusal

            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", _partial_failing_save):
                resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 200
            assert all(t["id"] != tag["id"] for t in state._tags)
            # BOTH slots stripped in memory despite s1's save failing:
            assert tag["id"] not in slot_a.tags
            assert tag["id"] not in slot_b.tags
            assert "s1" in saves and "s2" in saves  # s2 not aborted by s1
            assert getattr(slot_a, "_dirty", False) is True

    @pytest.mark.asyncio
    async def test_delete_succeeds_despite_board_persist_failure(self, tmp_path, monkeypatch):
        """Board strip failure after the vocab commit is tolerated: deletion
        succeeds; the dangling board reference is pruned on next load."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "BoardFail"})).json()
            col = await (
                await client.post(
                    "/api/chat/tag-columns",
                    json={"tag_ids": [tag["id"]], "mode": "any"},
                )
            ).json()
            slot = _ChatSlot("s1")
            slot.tags = [tag["id"]]
            state._slots["s1"] = slot

            def _failing_boards(snapshot):
                raise IOError("disk full")

            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                with patch.object(state, "save_tag_boards_snapshot", _failing_boards):
                    resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 200
            assert all(t["id"] != tag["id"] for t in state._tags)
            assert tag["id"] not in slot.tags
            updated_col = next(c for c in state._tag_boards if c["id"] == col["id"])
            assert tag["id"] not in updated_col["tag_ids"]  # stripped in memory


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
                "/api/chat/slots/s1/tags",
                data="not json",
                headers={"Content-Type": "application/json"},
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
                "/api/chat/tag-columns",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_column(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            col = await (await client.post("/api/chat/tag-columns", json={"name": "Old"})).json()
            resp = await client.patch(
                f"/api/chat/tag-columns/{col['id']}", json={"name": "New", "include_untagged": True}
            )
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
    async def test_update_column_valid_tag_id_persists(self, tmp_path, monkeypatch):
        """The board filter round-trip: a tag id taken from the same list the
        popover renders (GET /api/chat/tags) must survive the PATCH."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Jira"})).json()
            listed = await (await client.get("/api/chat/tags")).json()
            assert tag["id"] in {t["id"] for t in listed}
            col = await (await client.post("/api/chat/tag-columns", json={"name": "L"})).json()
            resp = await client.patch(
                f"/api/chat/tag-columns/{col['id']}", json={"tag_ids": [tag["id"]]}
            )
            assert resp.status == 200
            assert (await resp.json())["tag_ids"] == [tag["id"]]
            persisted = next(c for c in state._tag_boards if c["id"] == col["id"])
            assert persisted["tag_ids"] == [tag["id"]]

    @pytest.mark.asyncio
    async def test_update_column_unknown_tag_id_rejected_400(self, tmp_path, monkeypatch):
        """An unknown tag id must fail LOUDLY (400 + code), never return 200
        with the id silently dropped — the drop is what made the board filter
        appear to do nothing (column falls back to match-all)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Real"})).json()
            col = await (
                await client.post(
                    "/api/chat/tag-columns", json={"tag_ids": [tag["id"]], "mode": "any"}
                )
            ).json()
            resp = await client.patch(
                f"/api/chat/tag-columns/{col['id']}", json={"tag_ids": ["ghost123"]}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_column_payload"
            # The column's previous filter is untouched by the rejected write.
            persisted = next(c for c in state._tag_boards if c["id"] == col["id"])
            assert persisted["tag_ids"] == [tag["id"]]

    @pytest.mark.asyncio
    async def test_update_column_empty_tag_ids_clears_filter(self, tmp_path, monkeypatch):
        """[] is the clear-filter (match-all) state the board UI sends; it
        must stay accepted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "T"})).json()
            col = await (
                await client.post(
                    "/api/chat/tag-columns", json={"tag_ids": [tag["id"]], "mode": "any"}
                )
            ).json()
            resp = await client.patch(f"/api/chat/tag-columns/{col['id']}", json={"tag_ids": []})
            assert resp.status == 200
            assert (await resp.json())["tag_ids"] == []

    @pytest.mark.asyncio
    async def test_create_column_unknown_tag_id_rejected_400(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/tag-columns", json={"tag_ids": ["ghost123"]})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_column_payload"

    @pytest.mark.asyncio
    async def test_create_column_empty_tag_ids_allowed(self, tmp_path, monkeypatch):
        """The board creates columns with tag_ids [] (add-column flows);
        validation must keep that working."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/tag-columns", json={"name": "", "tag_ids": [], "mode": "any"}
            )
            assert resp.status == 201
            assert (await resp.json())["tag_ids"] == []

    @pytest.mark.asyncio
    async def test_update_column_invalid_json_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            col = await (await client.post("/api/chat/tag-columns", json={})).json()
            resp = await client.patch(
                f"/api/chat/tag-columns/{col['id']}",
                data="not json",
                headers={"Content-Type": "application/json"},
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
                "/api/chat/tag-columns/order",
                data="not json",
                headers={"Content-Type": "application/json"},
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
            todo = await (
                await client.post("/api/chat/tags", json={"name": "ToDo", "status": True})
            ).json()
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            spike = await (
                await client.post("/api/chat/tags", json={"name": "spike", "status": False})
            ).json()
            slot = _ChatSlot("s1")
            slot.tags = [todo["id"], spike["id"]]
            state._slots["s1"] = slot
            col = await (
                await client.post(
                    "/api/chat/tag-columns", json={"tag_ids": [done["id"]], "mode": "any"}
                )
            ).json()
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
            todo = await (
                await client.post("/api/chat/tags", json={"name": "ToDo", "status": True})
            ).json()
            slot = _ChatSlot("s1")
            slot.tags = [todo["id"]]
            state._slots["s1"] = slot
            col = await (
                await client.post("/api/chat/tag-columns", json={"tag_ids": [], "mode": "any"})
            ).json()
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
                "/api/chat/slots/s1/drop",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_drop_on_multi_tag_column_is_noop(self, tmp_path, monkeypatch):
        """Drop on a column with > 1 status tag is a no-op (not a single-status lane)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            todo = await (
                await client.post("/api/chat/tags", json={"name": "ToDo", "status": True})
            ).json()
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            slot = _ChatSlot("s1")
            slot.tags = [todo["id"]]
            state._slots["s1"] = slot
            col = await (
                await client.post(
                    "/api/chat/tag-columns",
                    json={"tag_ids": [todo["id"], done["id"]], "mode": "any"},
                )
            ).json()
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

    def test_load_prunes_dangling_column_tag_ids(self, tmp_path, monkeypatch):
        """A crash mid-tag-delete can leave a deleted tag id on a column.
        Load must prune it: the column API rejects unknown ids, so a dangling
        id left in place would make that column's filter un-editable."""
        import json as _json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "tags.json").write_text(
            _json.dumps([{"id": "live1", "name": "Live", "color": "#111111", "order": 0}]),
            encoding="utf-8",
        )
        (tmp_path / "tag_boards.json").write_text(
            _json.dumps(
                [{"id": "c1", "name": "L", "tag_ids": ["live1", "ghost"], "mode": "any", "order": 0}]
            ),
            encoding="utf-8",
        )
        state = _make_state(tmp_path)
        state.load_tags()
        assert state._tag_boards[0]["tag_ids"] == ["live1"]

    def test_load_keeps_column_tag_ids_when_vocabulary_unknown(self, tmp_path, monkeypatch):
        """A corrupt tags.json means the vocabulary is UNKNOWN — pruning
        against it would wipe every column filter. Fail open, same rule as
        the slot-restore prune."""
        import json as _json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "tags.json").write_text("not-json-at-all", encoding="utf-8")
        (tmp_path / "tag_boards.json").write_text(
            _json.dumps([{"id": "c1", "name": "L", "tag_ids": ["t1"], "mode": "any", "order": 0}]),
            encoding="utf-8",
        )
        state = _make_state(tmp_path)
        state.load_tags()
        assert state._tag_boards[0]["tag_ids"] == ["t1"]


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
            todo = await (
                await client.post("/api/chat/tags", json={"name": "ToDo", "status": True})
            ).json()
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            spike = await (
                await client.post("/api/chat/tags", json={"name": "spike", "status": False})
            ).json()
            slot = _ChatSlot("s1")
            slot.tags = [todo["id"]]
            state._slots["s1"] = slot
            # Column is "Done AND spike" — exactly one status tag in the filter.
            col = await (
                await client.post(
                    "/api/chat/tag-columns",
                    json={"tag_ids": [done["id"], spike["id"]], "mode": "all"},
                )
            ).json()
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
            data = await resp.json()
            assert data["ok"] is True
            assert done["id"] in data["tags"]
            assert todo["id"] not in data["tags"]


# ── F1: DELETE fail-closed propagation from save_slot_off_loop ──


class TestDeleteSlotPersistPropagatesFromUnderlying:
    """Crash-atomic contract: an underlying slot write failure during the
    post-commit strip does NOT fail the deletion — the vocab commit already
    made it durable; the slot is marked dirty for periodic-flush retry."""

    @pytest.mark.asyncio
    async def test_delete_succeeds_despite_underlying_write_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Under"})).json()
            slot = _ChatSlot("s1")
            slot.tags = [tag["id"]]
            state._slots["s1"] = slot

            async def _failing_save(state_arg, slot_arg, **kwargs):
                raise IOError("disk full")

            with patch.object(chat_tags_module, "save_slot_off_loop", _failing_save):
                resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 200
            # Tag removed from vocabulary; slot stripped in memory; the
            # failed write left the slot dirty so the flush retries it.
            assert all(t["id"] != tag["id"] for t in state._tags)
            assert tag["id"] not in slot.tags
            assert getattr(slot, "_dirty", False) is True


# ── F2: Tag create surfaces persist failure and rolls back ──


class TestTagCreatePersistFailure:
    """F2: save_tags_snapshot now uses strict writes; a create that fails to
    persist must return 5xx and NOT leave the tag in state._tags."""

    @pytest.mark.asyncio
    async def test_create_returns_500_and_rolls_back_on_write_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            with patch.object(
                state.__class__,
                "_atomic_write_json_strict",
                side_effect=IOError("disk full"),
            ):
                resp = await client.post("/api/chat/tags", json={"name": "Ghost"})
            assert resp.status == 500
            body = await resp.json()
            assert body["code"] == "persist_failed"
            # Tag must NOT be in state
            assert not any(t.get("name") == "Ghost" for t in state._tags)


# ── F3: Board column create concurrency test ──


class TestBoardColumnConcurrency:
    """F3: Two concurrent column creates must both appear in state and the
    final snapshot (lock serializes them)."""

    @pytest.mark.asyncio
    async def test_concurrent_column_creates_both_persisted(self, tmp_path, monkeypatch):
        import asyncio

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        snapshots_written: list[list[dict]] = []
        _real_save = state.save_tag_boards_snapshot

        def _recording_save(snapshot):
            snapshots_written.append(snapshot)
            _real_save(snapshot)

        async with TestClient(TestServer(app)) as client:
            with patch.object(state, "save_tag_boards_snapshot", _recording_save):
                results = await asyncio.gather(
                    client.post("/api/chat/tag-columns", json={"mode": "any"}),
                    client.post("/api/chat/tag-columns", json={"mode": "any"}),
                )
            assert all(r.status == 201 for r in results)
            col_ids = [await r.json() for r in results]
            ids_created = {c["id"] for c in col_ids}
            # Both columns in state
            state_ids = {c["id"] for c in state._tag_boards}
            assert ids_created.issubset(state_ids)
            # Final snapshot (last written) contains both
            assert len(snapshots_written) >= 2
            final_snap_ids = {c["id"] for c in snapshots_written[-1]}
            assert ids_created.issubset(final_snap_ids)


# ── F1-ext: Delete vocab-write failure triggers compensating rollback ──


class TestDeleteVocabWriteCompensation:
    """Crash-atomic contract: the vocabulary write is the single durable
    commit. If it fails, NOTHING else has been touched — slots are never
    stripped and no compensation writes are needed."""

    @pytest.mark.asyncio
    async def test_vocab_write_failure_leaves_slots_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "VocabFail"})).json()
            slot = _ChatSlot("s1")
            slot.tags = [tag["id"]]
            state._slots["s1"] = slot

            slot_saves: list[str] = []

            async def _recording_save(state_arg, slot_arg, **kwargs):
                slot_saves.append(slot_arg.key)

            with patch.object(chat_tags_module, "save_slot_off_loop", _recording_save):
                with patch.object(
                    chat_tags_module, "_write_tags_snapshot", side_effect=IOError("disk full")
                ):
                    resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 500
            body = await resp.json()
            assert body["code"] == "persist_failed"
            # Vocabulary restored in memory; slot NEVER touched (no strip, no
            # save) — the failed vocab write is the only attempted mutation.
            assert any(t["id"] == tag["id"] for t in state._tags)
            assert tag["id"] in slot.tags
            assert slot_saves == []


# ── F2-ext: Update race with concurrent DELETE ──


class TestUpdateDeleteRace:
    """F2: The PATCH handler must resolve the tag INSIDE the write lock.
    A concurrent DELETE that wins the lock first must cause PATCH to 404
    (not silently mutate a detached dict)."""

    @pytest.mark.asyncio
    async def test_concurrent_delete_causes_update_to_404(self, tmp_path, monkeypatch):
        import asyncio as _asyncio

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Race"})).json()
            tid = tag["id"]

            # Make _write_tags_snapshot slow so the DELETE holds the lock long
            # enough for PATCH to queue behind it.
            original_write = __import__(
                "kiro_crew.dashboard.chat_tags", fromlist=["_write_tags_snapshot"]
            )._write_tags_snapshot

            def _slow_write(st, snap):
                __import__("time").sleep(0.15)
                return original_write(st, snap)

            # Run DELETE and PATCH concurrently.
            # DELETE wins the lock; PATCH waits, then re-resolves -> 404.
            with patch(
                "kiro_crew.dashboard.chat_tags._write_tags_snapshot",
                side_effect=_slow_write,
            ):
                delete_task = _asyncio.ensure_future(client.delete(f"/api/chat/tags/{tid}"))
                # Small delay so DELETE grabs the lock first.
                await _asyncio.sleep(0.01)
                patch_task = _asyncio.ensure_future(
                    client.patch(f"/api/chat/tags/{tid}", json={"name": "Updated"})
                )

                delete_resp, patch_resp = await _asyncio.gather(delete_task, patch_task)

            # DELETE should succeed.
            assert delete_resp.status == 200

            # PATCH must 404 (tag deleted under the lock before PATCH could resolve it).
            assert patch_resp.status == 404
            patch_body = await patch_resp.json()
            assert patch_body["code"] == "not_found"

            # The tag must NOT be in state (no silent ghost mutation).
            assert not any(t["id"] == tid for t in state._tags)


class TestNonObjectBodiesAcrossConvertedHandlers:
    """Every converted handler answers 400, never 5xx, on a non-object body.

    ``[]`` / ``"s"`` / ``5`` / ``true`` / ``null`` are all VALID JSON, so
    ``request.json()`` returned them and the ``.get()`` (or ``in``) that each
    handler performs next raised from OUTSIDE the parse ``try`` -- a 500 for
    what is really malformed client input (issue #5587). Driven through a real
    client so the shared guard's 64 KB pre-decode cap is exercised on the wire,
    which is how these endpoints now read their body; the cap decision for each
    site is recorded in ``_CAP_REGISTER`` in ``test_json_object_body_guard.py``.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [[], "a string", 5, 1.5, True, None], ids=repr)
    async def test_non_object_body_is_400_not_500(self, payload, tmp_path) -> None:
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            # Seed a real tag and column: the {id} routes check existence BEFORE
            # parsing, so a made-up id would 404 and never reach the guard --
            # the test would pass for the wrong reason.
            created = await client.post("/api/chat/tags", json={"name": "T"})
            assert created.status in (200, 201), await created.text()
            tag_id = (await created.json())["id"]
            col = await client.post("/api/chat/tag-columns", json={"name": "C"})
            assert col.status in (200, 201), await col.text()
            col_id = (await col.json())["id"]

            routes = [
                ("post", "/api/chat/tags"),
                ("patch", f"/api/chat/tags/{tag_id}"),
                ("post", "/api/chat/tag-columns"),
                ("put", "/api/chat/tag-columns/order"),
                ("patch", f"/api/chat/tag-columns/{col_id}"),
            ]
            # api_chat_slot_tags and api_chat_slot_drop are converted too, but
            # they resolve the slot BEFORE parsing, so reaching their guard
            # needs a live slot rather than a seeded tag. Their call sites are
            # pinned in _CAP_REGISTER, and their guard is the same shared call.
            for method, path in routes:
                # ``json=None`` makes the client send NO body, which is a
                # different fact (invalid_json) from a body containing the JSON
                # literal ``null`` -- send that one as raw bytes.
                if payload is None:
                    resp = await getattr(client, method)(
                        path,
                        data=b"null",
                        headers={"Content-Type": "application/json"},
                    )
                else:
                    resp = await getattr(client, method)(path, json=payload)
                assert resp.status == 400, (path, payload, resp.status)
                assert (await resp.json())["code"] == "body_not_object", path


# ── load_tags normalises the persisted status flag (the cause-level seam) ──


class TestPersistedStatusFlagIsNormalised:
    """``status`` becomes a real bool at load, so every reader can use truthiness.

    Readers of this field used to have to test ``is True``, because a legacy or
    hand-edited ``tags.json`` can carry the STRING ``"false"`` and a string is
    truthy. That put the burden on five separate readers to remember the same
    trick, and they disagreed: the drop handler classified an inactive tag as a
    board lane while the lane filter did not, so the very drop that should have
    fired ``SessionLaneChanged`` never did. Normalising once at the boundary is
    what makes those readers agree, so this pins the boundary, not the readers.
    """

    def _load(self, tmp_path, monkeypatch, raw_status):
        import json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "tags.json").write_text(
            json.dumps(
                [{"id": "lane1", "name": "Lane", "color": "#111111", "order": 0, "status": raw_status}]
            ),
            encoding="utf-8",
        )
        state = _make_state(tmp_path)
        state.load_tags()
        return state._tags[0]["status"]

    def test_the_stringy_false_that_broke_truthiness_becomes_False(self, tmp_path, monkeypatch):
        got = self._load(tmp_path, monkeypatch, "false")
        assert got is False, f'"false" must normalise to the bool False, got {got!r}'

    def test_a_stringy_true_becomes_True(self, tmp_path, monkeypatch):
        """The repair must not simply zero every string -- that loses real lanes."""
        got = self._load(tmp_path, monkeypatch, "true")
        assert got is True, f'"true" must normalise to the bool True, got {got!r}'

    def test_a_real_bool_is_left_exactly_as_it_was(self, tmp_path, monkeypatch):
        assert self._load(tmp_path, monkeypatch, True) is True
        assert self._load(tmp_path, monkeypatch, False) is False

    def test_the_repair_is_persisted_so_it_happens_once(self, tmp_path, monkeypatch):
        """Normalising in memory only would re-run the repair on every boot."""
        import json

        self._load(tmp_path, monkeypatch, "false")
        on_disk = json.loads((tmp_path / "tags.json").read_text(encoding="utf-8"))
        assert on_disk[0]["status"] is False, f"the repair was not persisted: {on_disk!r}"
