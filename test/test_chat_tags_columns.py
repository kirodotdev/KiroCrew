"""Tests for chat_tags — the sidebar board's columns and drag-drop.

Column CRUD and ordering, and what a drop onto a column does to a slot's tags.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import chat_tags_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard.state import _ChatSlot

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_hermetic_signing_secret = chat_tags_helpers._hermetic_signing_secret


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
    async def test_list_columns_survives_a_hand_edited_order(self, tmp_path, monkeypatch):
        """``tag_boards.json`` is loaded verbatim like ``tags.json``; same key, same rule."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._tag_boards = [
            {"id": "b", "name": "B", "tag_ids": [], "mode": "any", "order": 1},
            {"id": "x", "name": "X", "tag_ids": [], "mode": "any", "order": "invalid"},
            {"id": "m", "name": "M", "tag_ids": [], "mode": "any"},
        ]
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/tag-columns")
            assert resp.status == 200
            names = [c["name"] for c in await resp.json()]
            assert names[-1] == "B" and set(names) == {"B", "X", "M"}

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
    async def test_a_repeated_tag_id_is_stored_once_and_ensure_finds_the_column(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "T"})).json()
            first = await client.post(
                "/api/chat/tag-columns",
                json={"name": "Lane", "tag_ids": [tag["id"], tag["id"]], "ensure": True},
            )
            assert first.status == 201
            col = await first.json()
            assert col["tag_ids"] == [tag["id"]]
            again = await client.post(
                "/api/chat/tag-columns",
                json={"name": "Lane", "tag_ids": [tag["id"]], "ensure": True},
            )
            assert (await again.json())["id"] == col["id"]
        assert len(state._tag_boards) == 1

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
    @pytest.mark.parametrize("spelled", ["false", "0", "no", "off", "False"])
    async def test_a_spelled_false_include_untagged_is_saved_as_false(
        self, tmp_path, monkeypatch, spelled
    ):
        # ``bool("false")`` is True; the folder route already reads these
        # spellings as False and the column must agree.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            col = await (
                await client.post(
                    "/api/chat/tag-columns", json={"name": "C", "include_untagged": True}
                )
            ).json()
            resp = await client.patch(
                f"/api/chat/tag-columns/{col['id']}", json={"include_untagged": spelled}
            )
            assert resp.status == 200
            assert (await resp.json())["include_untagged"] is False
            created = await (
                await client.post(
                    "/api/chat/tag-columns", json={"name": "D", "include_untagged": spelled}
                )
            ).json()
            assert created["include_untagged"] is False

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
            assert "tags_revision" not in data
            assert done["id"] in data["tags"]
            assert todo["id"] not in data["tags"]
            assert spike["id"] in data["tags"]

    @pytest.mark.asyncio
    async def test_drop_refusal_restores_tags_with_fresh_revision(self, tmp_path, monkeypatch):
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
            original_revision = slot.tags_revision
            state._slots["s1"] = slot
            col = await (
                await client.post(
                    "/api/chat/tag-columns", json={"tag_ids": [done["id"]], "mode": "any"}
                )
            ).json()

            async def _refuse(*_args, **_kwargs):
                return False

            state.push_slots_update = MagicMock()
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", _refuse):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
            data = await resp.json()
            assert data["ok"] is False
            # Tags rolled back, but under a FRESH revision (not the prior one a
            # client that saw the leaked provisional frame already treats as
            # stale), and the rollback is broadcast so clients reconverge.
            assert slot.tags == [todo["id"]]
            assert slot.tags_revision != original_revision
            state.push_slots_update.assert_called()

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


class TestCreateAfterDeleteAppends:
    @pytest.mark.asyncio
    async def test_new_column_after_deletes_lands_last(self, tmp_path, monkeypatch):
        """A column created after two deletes sorts after every survivor.

        Four columns hold orders 0-3; deleting the first two leaves 2 and 3, so a
        position counted from the list length (2) would tie the first survivor.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            made = {}
            for name in ("A", "B", "C", "D"):
                resp = await client.post("/api/chat/tag-columns", json={"name": name})
                assert resp.status == 201
                made[name] = await resp.json()
            for name in ("A", "B"):
                resp = await client.delete(f"/api/chat/tag-columns/{made[name]['id']}")
                assert resp.status == 200
            resp = await client.post("/api/chat/tag-columns", json={"name": "E"})
            assert resp.status == 201
            listed = await (await client.get("/api/chat/tag-columns")).json()
            assert [c["name"] for c in listed] == ["C", "D", "E"]
            assert len({c["order"] for c in listed}) == 3

    @pytest.mark.asyncio
    async def test_first_column_gets_order_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/tag-columns", json={"name": "Only"})
            assert resp.status == 201
            assert (await resp.json())["order"] == 0


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
