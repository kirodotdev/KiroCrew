"""Tests for chat_tags — tag vocabulary CRUD, slot tag assignment, sidebar columns, drag-drop.

This file holds the pure helpers and the tag vocabulary endpoints. Slot tag assignment is
in ``test_chat_tags_slot_tags.py``, sidebar columns and drag-drop in
``test_chat_tags_columns.py``, and persistence failures, races and non-object bodies in
``test_chat_tags_failure_modes.py``. ``chat_tags_helpers.py`` holds the autouse fixture
the four share.
"""

from __future__ import annotations

from unittest.mock import patch

import chat_tags_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard.chat_tags import _normalize_column, _valid_color
from kiro_crew.dashboard.state import _ChatSlot

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_hermetic_signing_secret = chat_tags_helpers._hermetic_signing_secret


# ── Pure helpers ──


class TestValidColor:
    @pytest.mark.parametrize(
        "value,expected",
        [
            pytest.param("#abcdef", "#abcdef", id="test_accepts_lowercase_hex"),
            pytest.param("#ABCDEF", "#ABCDEF", id="test_accepts_uppercase_hex"),
            pytest.param("#abc", "#6b7280", id="test_rejects_short_hex"),
            pytest.param("ff0000", "#6b7280", id="test_rejects_no_hash"),
            pytest.param("not-a-color", "#6b7280", id="test_rejects_garbage"),
        ],
    )
    def test_valid_color(self, value, expected):
        assert _valid_color(value) == expected


class TestNormalizeColumn:
    def _state_with_tags(self, tmp_path):
        state = _make_state(tmp_path)
        state._tags = [
            {"id": "t1", "name": "T1", "color": "#111111", "order": 0, "status": True},
            {"id": "t2", "name": "T2", "color": "#222222", "order": 1, "status": False},
        ]
        return state

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not-a-dict", id="test_returns_none_for_non_dict"),
            # An unknown id must reject the payload, not be silently dropped —
            # a drop lands the column on tag_ids [] (match-all), which presents
            # to the user as "the filter does nothing".
            pytest.param({"tag_ids": ["t1", "ghost", "t2"]}, id="test_rejects_unknown_tag_ids"),
            pytest.param({"tag_ids": ["t1", 42]}, id="test_rejects_non_string_tag_id_entries"),
            pytest.param({"tag_ids": "not-a-list"}, id="test_rejects_non_list_tag_ids"),
            pytest.param({"mode": "fancy"}, id="test_rejects_invalid_mode"),
        ],
    )
    def test_invalid_column_is_rejected(self, tmp_path, payload):
        state = self._state_with_tags(tmp_path)
        assert _normalize_column(state, payload) is None

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

    def test_truncates_name_to_max(self, tmp_path):
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {"name": "x" * 200})
        assert col is not None
        assert len(col["name"]) == 60

    @pytest.mark.parametrize(
        "order,expected",
        [
            pytest.param("5", 5, id="test_coerces_order_to_int"),
            # An unparseable order falls back to the default.
            pytest.param("abc", 0, id="test_ignores_unparseable_order"),
        ],
    )
    def test_order_is_normalized(self, tmp_path, order, expected):
        state = self._state_with_tags(tmp_path)
        col = _normalize_column(state, {"order": order})
        assert col is not None
        assert col["order"] == expected

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
    async def test_list_survives_a_hand_edited_order(self, tmp_path, monkeypatch):
        """``tags.json`` is loaded verbatim, so a row whose ``order`` is not a number
        must sort as 0 rather than turn every reader of the vocabulary into a 500."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._tags = [
            {"id": "b", "name": "B", "color": "#000000", "order": 1, "status": False},
            {"id": "x", "name": "X", "color": "#000000", "order": "invalid", "status": False},
            {"id": "f", "name": "F", "color": "#000000", "order": 0.5, "status": False},
            {"id": "m", "name": "M", "color": "#000000", "status": False},
        ]
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/tags")
            assert resp.status == 200
            names = [t["name"] for t in await resp.json()]
            assert names[-1] == "B" and set(names) == {"B", "X", "F", "M"}

    def test_tag_order_never_raises(self):
        from kiro_crew.dashboard.chat_tags import _order_key

        assert _order_key({"order": 3}) == 3
        assert _order_key({"order": 2.9}) == 2
        for bad in ("invalid", None, True, [1], float("nan"), float("inf"), -float("inf")):
            assert _order_key({"order": bad}) == 0
        assert _order_key({}) == 0

    @pytest.mark.asyncio
    async def test_create_refuses_an_app_caller(self, tmp_path, monkeypatch):
        """Tags have no owner, so an app may not add to the person's shared list.

        Decided at the route on the middleware's validated claim, which is what
        makes the ``chat_tag_create`` MCP tool's refusal the endpoint's rule and
        not a tool-layer copy of it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        from aiohttp import web as _web

        @_web.middleware
        async def _as_app(request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/tags", json={"name": "Coined"})
            assert resp.status == 403
            assert (await resp.json())["code"] == "app_forbidden"
            listed = await (await client.get("/api/chat/tags")).json()
        assert all(t["name"] != "Coined" for t in listed)
        assert all(t.get("name") != "Coined" for t in state._tags)

    @pytest.mark.asyncio
    async def test_create_rate_limits_an_internal_caller(self, tmp_path, monkeypatch):
        """Same budget as folder create, so an agent loop cannot grow tags.json unbounded.

        Mutation guard: drop the ``allow_create`` call and the whole burst is 201.
        """
        from kiro_crew.dashboard import create_rate_limit

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        create_rate_limit.reset_for_tests()
        try:
            state = _make_state(tmp_path)
            app = _make_tags_app(state)
            headers = {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-dashboard"}
            async with TestClient(TestServer(app)) as client:
                allowed = 0
                for i in range(create_rate_limit.MAX_TAG_CREATES_PER_WINDOW + 3):
                    resp = await client.post(
                        "/api/chat/tags", json={"name": f"T{i}"}, headers=headers
                    )
                    if resp.status == 201:
                        allowed += 1
                    else:
                        assert resp.status == 429
                        assert (await resp.json())["code"] == "create_rate_limited"
            assert allowed == create_rate_limit.MAX_TAG_CREATES_PER_WINDOW
        finally:
            create_rate_limit.reset_for_tests()

    @pytest.mark.asyncio
    async def test_create_does_not_rate_limit_the_browser(self, tmp_path, monkeypatch):
        """A person seeding a board can legitimately outpace the agent budget."""
        from kiro_crew.dashboard import create_rate_limit

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        create_rate_limit.reset_for_tests()
        try:
            state = _make_state(tmp_path)
            app = _make_tags_app(state)
            async with TestClient(TestServer(app)) as client:
                for i in range(create_rate_limit.MAX_TAG_CREATES_PER_WINDOW + 3):
                    resp = await client.post("/api/chat/tags", json={"name": f"B{i}"})
                    assert resp.status == 201
        finally:
            create_rate_limit.reset_for_tests()

    @pytest.mark.asyncio
    async def test_update_refuses_an_app_caller_and_a_gone_slot(self, tmp_path, monkeypatch):
        """PATCH is a vocabulary write too: same two refusals as POST, before any edit."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._tags = [
            {"id": "t1", "name": "Keep", "color": "#000000", "order": 0, "status": False}
        ]
        app = _make_tags_app(state)

        from aiohttp import web as _web

        @_web.middleware
        async def _as_app(request, handler):
            if request.headers.get("X-As-App"):
                request["app"] = request.headers["X-As-App"]
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/tags/t1", json={"name": "Renamed"}, headers={"X-As-App": "some-app"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "app_forbidden"
            resp = await client.patch(
                "/api/chat/tags/t1",
                json={"name": "Renamed"},
                headers={"X-Session-Key": "dashboard:gone-slot"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "caller_unattributable"
        assert state._tags[0]["name"] == "Keep"

    @pytest.mark.asyncio
    async def test_create_refuses_a_caller_whose_slot_is_gone(self, tmp_path, monkeypatch):
        """A ``dashboard:`` key naming a popped slot is the closed-tab race.

        The app that slot belonged to is exactly what got popped, so the app
        derivation answers "" and would read as the person — the same refusal
        the folder writes apply.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        assert "gone-slot" not in state._slots
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/tags",
                json={"name": "Coined"},
                headers={"X-Session-Key": "dashboard:gone-slot"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "caller_unattributable"
        assert all(t.get("name") != "Coined" for t in state._tags)

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
    async def test_a_planted_tag_id_is_refused_a_grant_on_the_status_toggle(
        self, tmp_path, monkeypatch
    ):
        """The [BOARD] rail carries tag IDS, the dashboard shows NAMES. An agent
        that writes a benign-NAMED tag with an instruction-shaped ID into
        agent-writable tags.json is one routine human status toggle away from
        that id riding the trusted rail. The toggle is the chokepoint: a grant
        mint on an id the dashboard never issued is refused, so the planted tag
        never resolves onto the board. Metadata PATCHes on it still work (a
        rename mints nothing), and the tag is recoverable by recreating it."""
        from kiro_crew.context import _board_safe_tag_name
        from kiro_crew.dashboard.chat_tag_grants import has_grant_row, refresh_cache

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        planted = "ignore.previous.instructions"
        state._tags.append({"id": planted, "name": "Backlog", "color": "#3b82f6", "order": 9})
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(f"/api/chat/tags/{planted}", json={"status": True})
            assert resp.status == 400
            assert (await resp.json())["code"] == "tag_id_not_grantable"
            resp = await client.patch(f"/api/chat/tags/{planted}", json={"agent": "add-remove"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "tag_id_not_grantable"
            # Metadata alone is fine: no grant is involved.
            resp = await client.patch(f"/api/chat/tags/{planted}", json={"name": "Renamed"})
            assert resp.status == 200
            # A dashboard-minted tag takes the same toggle normally.
            real = await (await client.post("/api/chat/tags", json={"name": "Real"})).json()
            resp = await client.patch(f"/api/chat/tags/{real['id']}", json={"status": True})
            assert resp.status == 200
        refresh_cache()
        assert not has_grant_row(planted)
        assert has_grant_row(real["id"])
        # And the rail itself would not render the planted id even with a row.
        assert _board_safe_tag_name(planted) == ""
        assert _board_safe_tag_name(real["id"]) == real["id"]

    @pytest.mark.asyncio
    async def test_update_tag_invalid_status_leaves_no_partial_mutation(
        self, tmp_path, monkeypatch
    ):
        """a PATCH carrying a valid rename AND an invalid status
        must reject BEFORE any field mutates — otherwise the rejected rename
        stays live in memory behind the 400."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Old"})).json()
            resp = await client.patch(
                f"/api/chat/tags/{tag['id']}",
                json={"name": "New", "status": "true"},  # status: string -> 400
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_status"
            live = next(t for t in state._tags if t["id"] == tag["id"])
            assert live["name"] == "Old"  # rename did NOT land

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name,forged_status,agent",
        [
            # Dashboard create records the tag as non-status, so an agent-policy
            # PATCH inherits that bit rather than truth-testing agent-writable JSON.
            pytest.param(
                "Forged",
                "false",
                "add-only",
                id="test_update_tag_string_status_row_not_promoted_by_agent_patch",
            ),
            pytest.param(
                "Forged2",
                True,
                "add-remove",
                id="test_agent_patch_does_not_promote_forged_bool_status",
            ),
        ],
    )
    async def test_forged_status_is_not_promoted_by_agent_patch(
        self, tmp_path, monkeypatch, name, forged_status, agent
    ):
        """A forged status in agent-writable tags.json never overrides protected identity."""
        from kiro_crew.dashboard.chat_tags import agent_tag_grant

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": name})).json()
            # Simulate the hand-edited file: a forged status on the live row.
            live = next(t for t in state._tags if t["id"] == tag["id"])
            live["status"] = forged_status
            resp = await client.patch(f"/api/chat/tags/{tag['id']}", json={"agent": agent})
            assert resp.status == 200
            # Policy updated, but NO workflow-state authority minted — the
            # forged tags.json bit never reaches the protected store.
            assert agent_tag_grant({"id": tag["id"]}) == (agent, False)

    @pytest.mark.asyncio
    async def test_failed_mint_restores_prior_grant(self, tmp_path, monkeypatch):
        """revoke-up-front must not become a PERMANENT revocation
        when the replacement mint fails — the prior grant is restored and the
        PATCH still surfaces 500."""
        from kiro_crew.dashboard import chat_tags as ct

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        real_mint = ct.mint_grant

        def _failing_mint(tag_id, *, policy, status):
            if policy == "add-only":
                raise OSError("simulated store write failure")
            real_mint(tag_id, policy=policy, status=status)

        async with TestClient(TestServer(app)) as client:
            tag = await (
                await client.post("/api/chat/tags", json={"name": "Keep2", "status": True})
            ).json()
            assert ct.agent_tag_grant({"id": tag["id"]}) == ("add-remove", True)
            monkeypatch.setattr(ct, "mint_grant", _failing_mint)
            resp = await client.patch(f"/api/chat/tags/{tag['id']}", json={"agent": "add-only"})
            assert resp.status == 500
            # The prior grant survives the failed narrowing PATCH.
            assert ct.agent_tag_grant({"id": tag["id"]}) == ("add-remove", True)

    @pytest.mark.asyncio
    async def test_status_patch_without_agent_keeps_the_recorded_policy(
        self, tmp_path, monkeypatch
    ):
        """A status-inclusive PATCH that omits ``agent`` — the ordinary form-edit
        re-send — keeps the policy already in the protected store. A human-only
        ``none`` row stays ``none`` and a narrowed ``add-only`` stays ``add-only``;
        only a tag with no protected record at all takes the out-of-the-box
        ``add-remove`` default, the same one the create path mints."""
        from kiro_crew.dashboard import chat_tags as ct

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        async with TestClient(TestServer(app)) as client:
            # Human-only workflow state: created as a status tag, then reserved.
            reserved = await (
                await client.post("/api/chat/tags", json={"name": "Human-only", "status": True})
            ).json()
            resp = await client.patch(f"/api/chat/tags/{reserved['id']}", json={"agent": "none"})
            assert resp.status == 200
            assert ct.agent_tag_grant({"id": reserved["id"]}) == ("none", True)
            # The form re-sends name + status without agent.
            resp = await client.patch(
                f"/api/chat/tags/{reserved['id']}", json={"name": "Human only", "status": True}
            )
            assert resp.status == 200
            assert ct.agent_tag_grant({"id": reserved["id"]}) == ("none", True)

            # Narrowed policy survives the same re-send.
            narrowed = await (
                await client.post("/api/chat/tags", json={"name": "Narrow", "status": True})
            ).json()
            await client.patch(f"/api/chat/tags/{narrowed['id']}", json={"agent": "add-only"})
            resp = await client.patch(f"/api/chat/tags/{narrowed['id']}", json={"status": True})
            assert resp.status == 200
            assert ct.agent_tag_grant({"id": narrowed["id"]}) == ("add-only", True)

            # A plain tag already has protected identity. Promoting it to a
            # workflow state without an explicit policy stays human-only.
            plain = await (await client.post("/api/chat/tags", json={"name": "Plain"})).json()
            assert ct.has_grant_row(plain["id"])
            resp = await client.patch(f"/api/chat/tags/{plain['id']}", json={"status": True})
            assert resp.status == 200
            assert ct.agent_tag_grant({"id": plain["id"]}) == ("none", True)

    @pytest.mark.asyncio
    async def test_failed_mint_rolls_back_vocabulary(self, tmp_path, monkeypatch):
        """A PATCH whose grant transition fails leaves both stores unchanged."""
        from kiro_crew.dashboard import chat_tags as ct

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        def _failing_mint(tag_id, *, policy, status):
            raise OSError("simulated store write failure")

        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Stable"})).json()
            monkeypatch.setattr(ct, "mint_grant", _failing_mint)
            resp = await client.patch(
                f"/api/chat/tags/{tag['id']}",
                # The final policy is the intended failing write; the identity
                # downgrade occurs before the vocabulary write.
                json={"name": "Renamed", "agent": "add-only", "status": False},
            )
            assert resp.status == 500
            live = next(t for t in state._tags if t["id"] == tag["id"])
            assert live["name"] == "Stable"  # memory rolled back
            import json as _json

            on_disk = _json.loads((tmp_path / "tags.json").read_text(encoding="utf-8"))
            disk_tag = next(t for t in on_disk if t["id"] == tag["id"])
            assert disk_tag["name"] == "Stable"  # disk rolled back too

    @pytest.mark.asyncio
    async def test_delete_aborts_when_revoke_fails(self, tmp_path, monkeypatch):
        """a deletion must never outlive the authority it removes
        — a failed revoke aborts the DELETE with the tag (and grant) intact."""
        from kiro_crew.dashboard import chat_tags as ct

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        def _failing_revoke(tag_id):
            raise OSError("simulated store write failure")

        async with TestClient(TestServer(app)) as client:
            tag = await (
                await client.post("/api/chat/tags", json={"name": "Guarded", "status": True})
            ).json()
            assert ct.agent_tag_grant({"id": tag["id"]}) == ("add-remove", True)
            monkeypatch.setattr(ct, "revoke_grant", _failing_revoke)
            resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 500
            assert any(t["id"] == tag["id"] for t in state._tags)  # still present
            assert ct.agent_tag_grant({"id": tag["id"]}) == ("add-remove", True)

    @pytest.mark.asyncio
    async def test_update_keeps_status_identity_across_the_write_window(
        self, tmp_path, monkeypatch
    ):
        """A PATCH that re-mints a grant is two store writes with the vocabulary
        commit in between. Observed AT the vocabulary write (the crash window),
        the tag must still carry its status identity as a ``("none", True)``
        row -- less authority, never a missing row that the applier would read
        as a plain label and let two workflow states coexist."""
        from kiro_crew.dashboard import chat_tag_grants as grants
        from kiro_crew.dashboard import chat_tags as ct

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        real_write = ct._write_tags_snapshot
        seen: dict[str, object] = {}

        async with TestClient(TestServer(app)) as client:
            tag = await (
                await client.post("/api/chat/tags", json={"name": "Stage", "status": True})
            ).json()

            def _observing_write(_state, _snapshot):
                grants.refresh_cache()
                seen["row_exists"] = grants.has_grant_row(tag["id"])
                seen["grant"] = grants.resolve_grant(tag["id"])
                return real_write(_state, _snapshot)

            monkeypatch.setattr(ct, "_write_tags_snapshot", _observing_write)
            resp = await client.patch(
                f"/api/chat/tags/{tag['id']}", json={"name": "Stage 2", "agent": "add-only"}
            )
            assert resp.status == 200
        # Inside the window: a row exists, grants nothing, keeps the status bit.
        assert seen == {"row_exists": True, "grant": ("none", True)}
        grants.refresh_cache()
        assert ct.agent_tag_grant({"id": tag["id"]}) == ("add-only", True)

    @pytest.mark.asyncio
    async def test_metadata_only_update_failure_never_touches_the_grant_store(
        self, tmp_path, monkeypatch
    ):
        """A rename/recolour PATCH does not capture the prior grant (it never
        asked for a grant change), so its failure compensation must not act on
        the grant store at all -- acting on 'no prior row' there would revoke
        the tag's real grant behind the 500."""
        from kiro_crew.dashboard import chat_tag_grants as grants
        from kiro_crew.dashboard import chat_tags as ct

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        def _failing_write(_state, _snapshot):
            raise OSError("simulated tags.json write failure")

        async with TestClient(TestServer(app)) as client:
            tag = await (
                await client.post("/api/chat/tags", json={"name": "Stage", "status": True})
            ).json()
            grants.refresh_cache()
            assert ct.agent_tag_grant({"id": tag["id"]}) == ("add-remove", True)
            monkeypatch.setattr(ct, "_write_tags_snapshot", _failing_write)
            resp = await client.patch(f"/api/chat/tags/{tag['id']}", json={"name": "Stage 2"})
            assert resp.status == 500
        grants.refresh_cache()
        assert ct.agent_tag_grant({"id": tag["id"]}) == ("add-remove", True)

    @pytest.mark.asyncio
    async def test_rowless_tag_is_refused_before_vocab_write(self, tmp_path, monkeypatch):
        """A legacy or planted row cannot recreate provenance through PATCH."""
        from kiro_crew.dashboard import chat_tag_grants as grants
        from kiro_crew.dashboard import chat_tags as ct

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        def _failing_write(_state, _snapshot):
            pytest.fail("rowless refusal must happen before the vocabulary write")

        async with TestClient(TestServer(app)) as client:
            tag = await (
                await client.post("/api/chat/tags", json={"name": "Legacy", "status": True})
            ).json()
            grants.revoke_grant(tag["id"])  # simulate the upgraded, row-less state
            grants.refresh_cache()
            assert not grants.has_grant_row(tag["id"])
            monkeypatch.setattr(ct, "_write_tags_snapshot", _failing_write)
            resp = await client.patch(
                f"/api/chat/tags/{tag['id']}", json={"status": True, "agent": "add-remove"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "tag_id_not_grantable"
        grants.refresh_cache()
        assert not grants.has_grant_row(tag["id"])

    @pytest.mark.asyncio
    async def test_delete_vocab_failure_restores_grant(self, tmp_path, monkeypatch):
        """revoke succeeded but the vocabulary persist
        failed — the captured grant is re-minted so a failed DELETE is a
        no-op on authority."""
        from kiro_crew.dashboard import chat_tags as ct

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        def _failing_write(_state, _snapshot):
            raise OSError("simulated tags.json write failure")

        async with TestClient(TestServer(app)) as client:
            tag = await (
                await client.post("/api/chat/tags", json={"name": "Kept", "status": True})
            ).json()
            monkeypatch.setattr(ct, "_write_tags_snapshot", _failing_write)
            resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 500
            assert any(t["id"] == tag["id"] for t in state._tags)
            assert ct.agent_tag_grant({"id": tag["id"]}) == ("add-remove", True)

    @pytest.mark.asyncio
    async def test_create_mint_failure_rolls_back_the_tag(self, tmp_path, monkeypatch):
        """a POST with status:true whose grant mint fails must not
        return 201 for a tag chat_tag refuses — the grant is minted BEFORE
        the vocabulary commit, so a mint failure aborts with NOTHING durable
        in either store and surfaces 500."""
        from kiro_crew.dashboard import chat_tags as ct

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        def _failing_mint(tag_id, *, policy, status):
            raise OSError("simulated store write failure")

        async with TestClient(TestServer(app)) as client:
            monkeypatch.setattr(ct, "mint_grant", _failing_mint)
            resp = await client.post("/api/chat/tags", json={"name": "Doomed", "status": True})
            assert resp.status == 500
            assert not any(t.get("name") == "Doomed" for t in state._tags)
            # Grant-first ordering: the mint failed before the vocabulary
            # write, so tags.json was never touched by this request — it is
            # either absent (fresh home) or free of the doomed tag.
            tags_path = tmp_path / "tags.json"
            if tags_path.exists():
                import json as _json

                on_disk = _json.loads(tags_path.read_text(encoding="utf-8"))
                assert not any(t.get("name") == "Doomed" for t in on_disk)

    @pytest.mark.asyncio
    async def test_update_tag_empty_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Keep"})).json()
            resp = await client.patch(f"/api/chat/tags/{tag['id']}", json={"name": "   "})
            assert resp.status == 400
            live = next(t for t in state._tags if t["id"] == tag["id"])
            assert live["name"] == "Keep"

    @pytest.mark.asyncio
    async def test_update_tag_unparseable_order_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "X"})).json()
            await client.patch(f"/api/chat/tags/{tag['id']}", json={"order": 7})
            resp = await client.patch(f"/api/chat/tags/{tag['id']}", json={"order": "abc"})
            assert resp.status == 200
            live = next(t for t in state._tags if t["id"] == tag["id"])
            assert live["order"] == 7

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
        do not recognize, persisting a raw key prefix."""
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
