"""Tests for board state lanes — columns whose membership is derived runtime state.

A state lane is the second column kind on the sidebar board: instead of filtering
by tag it names one live session lane (needs_approval / waiting / working / idle).
The backend's whole job is to keep the discriminator coherent — a lane must name a
known key, a tag column must not claim one — and to refuse a drop onto a lane,
since no tag write can move a card into a state the agent is not in.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard.chat_tags import _VALID_STATE_KEYS, _normalize_column
from kiro_crew.dashboard.state import _ChatSlot


class TestNormalizeStateColumn:
    def _state(self, tmp_path):
        state = _make_state(tmp_path)
        state._tags = [
            {"id": "t1", "name": "T1", "color": "#111111", "order": 0, "status": True},
        ]
        return state

    def test_defaults_to_tag_source(self, tmp_path):
        # Every column persisted before lanes existed omits the field, and the
        # board must keep reading those as tag columns.
        col = _normalize_column(self._state(tmp_path), {"tag_ids": []})
        assert col is not None
        assert col["source"] == "tags"
        assert col["state_key"] == ""

    def test_accepts_each_known_lane(self, tmp_path):
        state = self._state(tmp_path)
        for key in _VALID_STATE_KEYS:
            col = _normalize_column(state, {"source": "state", "state_key": key})
            assert col is not None, key
            assert col["source"] == "state"
            assert col["state_key"] == key

    def test_rejects_unknown_lane_key(self, tmp_path):
        # An unknown key would render a column that can never match a session.
        assert (
            _normalize_column(self._state(tmp_path), {"source": "state", "state_key": "wat"})
            is None
        )

    def test_rejects_unknown_source(self, tmp_path):
        assert _normalize_column(self._state(tmp_path), {"source": "vibes"}) is None

    def test_rejects_state_source_without_key(self, tmp_path):
        assert _normalize_column(self._state(tmp_path), {"source": "state"}) is None

    def test_rejects_state_source_with_blank_key(self, tmp_path):
        assert (
            _normalize_column(self._state(tmp_path), {"source": "state", "state_key": ""}) is None
        )

    def test_rejects_tag_source_carrying_a_lane_key(self, tmp_path):
        # A tag column claiming a lane would be labelled by a state it does not
        # filter by. Refuse rather than silently clearing one of the two fields.
        assert (
            _normalize_column(self._state(tmp_path), {"source": "tags", "state_key": "working"})
            is None
        )

    def test_update_cannot_smuggle_a_bad_key_onto_an_existing_lane(self, tmp_path):
        existing = _normalize_column(
            self._state(tmp_path), {"source": "state", "state_key": "idle"}
        )
        assert existing is not None
        assert (
            _normalize_column(self._state(tmp_path), {"state_key": "nope"}, existing=existing)
            is None
        )


class TestStateColumnRoutes:
    @pytest.mark.asyncio
    async def test_create_state_lane_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/tag-columns", json={"source": "state", "state_key": "needs_approval"}
            )
            assert resp.status == 201
            created = await resp.json()
            assert created["source"] == "state"
            assert created["state_key"] == "needs_approval"
            listed = await (await client.get("/api/chat/tag-columns")).json()
            assert [c["state_key"] for c in listed] == ["needs_approval"]

    @pytest.mark.asyncio
    async def test_create_unknown_lane_rejected_400(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/tag-columns", json={"source": "state", "state_key": "shipped"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_drop_onto_state_lane_is_refused_and_preserves_tags(self, tmp_path, monkeypatch):
        # The card's lane follows the session's own runtime state, so there is
        # nothing to write. Critically, the refusal must not touch slot.tags:
        # falling through to the status-tag swap would reassign a workflow tag
        # the lane never filtered by.
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
            lane = await (
                await client.post(
                    "/api/chat/tag-columns", json={"source": "state", "state_key": "working"}
                )
            ).json()
            resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": lane["id"]})
            data = await resp.json()
            assert data["ok"] is False
            assert data["reason"] == "column is a derived state lane"
            assert data["tags"] == [todo["id"]]
            assert slot.tags == [todo["id"]]

    @pytest.mark.asyncio
    async def test_tag_columns_still_accept_drops_alongside_lanes(self, tmp_path, monkeypatch):
        # Lanes and tag columns coexist on one board; adding lanes must not
        # disturb the tag columns' drag behaviour.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            await client.post(
                "/api/chat/tag-columns", json={"source": "state", "state_key": "idle"}
            )
            col = await (
                await client.post(
                    "/api/chat/tag-columns", json={"tag_ids": [done["id"]], "mode": "any"}
                )
            ).json()
            from unittest.mock import patch

            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
            data = await resp.json()
            assert data["ok"] is True
            assert data["tags"] == [done["id"]]


class TestStateLaneUniqueness:
    """A lane is a singleton: two columns naming one runtime state would render
    every matching session twice. The client cannot enforce that -- it decides
    from a cached column list -- so the rule lives under the write lock."""

    @pytest.mark.asyncio
    async def test_creating_an_existing_lane_returns_it_instead_of_duplicating(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            first = await client.post(
                "/api/chat/tag-columns", json={"source": "state", "state_key": "working"}
            )
            assert first.status == 201
            created = await first.json()

            second = await client.post(
                "/api/chat/tag-columns", json={"source": "state", "state_key": "working"}
            )
            # 200, not 201: the caller's intent is "ensure this lane exists".
            assert second.status == 200
            assert (await second.json())["id"] == created["id"]

            listed = await (await client.get("/api/chat/tag-columns")).json()
            assert [c["state_key"] for c in listed] == ["working"]

    @pytest.mark.asyncio
    async def test_concurrent_creates_of_one_lane_persist_a_single_column(
        self, tmp_path, monkeypatch
    ):
        # The race a client-side "is it missing?" check cannot close: both
        # requests are in flight before either has persisted.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            results = await asyncio.gather(
                *[
                    client.post(
                        "/api/chat/tag-columns",
                        json={"source": "state", "state_key": "needs_approval"},
                    )
                    for _ in range(4)
                ]
            )
            assert sorted(r.status for r in results) == [200, 200, 200, 201]
            listed = await (await client.get("/api/chat/tag-columns")).json()
            assert [c["state_key"] for c in listed] == ["needs_approval"]

    @pytest.mark.asyncio
    async def test_distinct_lanes_are_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            for key in ("needs_approval", "waiting", "working", "idle"):
                resp = await client.post(
                    "/api/chat/tag-columns", json={"source": "state", "state_key": key}
                )
                assert resp.status == 201, key
            listed = await (await client.get("/api/chat/tag-columns")).json()
            assert [c["state_key"] for c in listed] == [
                "needs_approval",
                "waiting",
                "working",
                "idle",
            ]

    @pytest.mark.asyncio
    async def test_update_onto_an_occupied_lane_is_refused(self, tmp_path, monkeypatch):
        # Sibling of the create hole: retargeting a lane column onto a state that
        # is already taken would duplicate it just as effectively.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/chat/tag-columns", json={"source": "state", "state_key": "working"}
            )
            idle = await (
                await client.post(
                    "/api/chat/tag-columns", json={"source": "state", "state_key": "idle"}
                )
            ).json()

            resp = await client.patch(
                f"/api/chat/tag-columns/{idle['id']}", json={"state_key": "working"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "duplicate_state_lane"

            listed = await (await client.get("/api/chat/tag-columns")).json()
            assert sorted(c["state_key"] for c in listed) == ["idle", "working"]

    @pytest.mark.asyncio
    async def test_updating_a_lane_to_its_own_key_is_allowed(self, tmp_path, monkeypatch):
        # The exclude_id carve-out: a column must not collide with itself, or a
        # no-op rename of a lane would be refused.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            lane = await (
                await client.post(
                    "/api/chat/tag-columns", json={"source": "state", "state_key": "waiting"}
                )
            ).json()
            resp = await client.patch(
                f"/api/chat/tag-columns/{lane['id']}", json={"state_key": "waiting"}
            )
            assert resp.status == 200
