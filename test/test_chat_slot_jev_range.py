"""Tests for POST /api/chat/slots/{slot}/jev-range endpoint."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import _identity

from kiro_crew.dashboard import chat_jev_range
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history, _save_slot_to_history
from kiro_crew.dashboard.routes import chat as routes_chat

NO_RANGE = {"models": [], "effort_min": "", "effort_max": ""}
RANGE = {"models": ["model-a", "model-b"], "effort_min": "low", "effort_max": "high"}
NARROWER = {"models": ["model-a"], "effort_min": "", "effort_max": "low"}
ADVERTISED = ["model-a", "model-b", "model-c"]
URL = "/api/chat/slots/s1/jev-range"


def _make_app(state) -> web.Application:
    app = web.Application(middlewares=[_identity])
    app["state"] = state
    routes_chat.register(app)
    return app


def _warm_state(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    provider = SimpleNamespace(available_models=lambda: [{"modelId": m} for m in ADVERTISED])
    state.sessions.get_provider.return_value = provider
    return state, slot


class TestSettingTheRange:
    @pytest.mark.asyncio
    async def test_the_owner_sets_a_range_and_the_slot_projection_returns_it(self, tmp_path):
        state, slot = _warm_state(tmp_path)
        assert slot.to_dict()["jev_range"] == NO_RANGE

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(URL, json=RANGE)
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True, "jev_range": RANGE}
        assert slot.to_dict()["jev_range"] == RANGE

    @pytest.mark.asyncio
    async def test_a_non_owner_request_is_refused_and_the_range_is_unchanged(self, tmp_path):
        state, slot = _warm_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            assert (await client.post(URL, json=RANGE)).status == 200
            resp = await client.post(URL, json=NARROWER, headers={"X-Test-User": "someone-else"})

        assert resp.status == 403
        assert slot.to_dict()["jev_range"] == RANGE

    @pytest.mark.asyncio
    async def test_a_repeated_id_is_stored_once(self, tmp_path):
        state, slot = _warm_state(tmp_path)
        body = {"models": ["model-a", "model-b", "model-a"], "effort_min": "", "effort_max": ""}

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(URL, json=body)
            data = await resp.json()

        assert resp.status == 200
        assert slot.jev_range["models"] == ["model-a", "model-b"]
        assert data["jev_range"]["models"] == ["model-a", "model-b"]


class TestRefusals:
    @pytest.mark.asyncio
    async def test_an_app_scoped_request_is_refused_and_the_range_is_unchanged(self, tmp_path):
        state, slot = _warm_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(URL, json=RANGE, headers={"X-Test-App": "some-app"})

        assert resp.status == 404
        assert slot.to_dict()["jev_range"] == NO_RANGE

    @pytest.mark.asyncio
    async def test_a_range_posted_while_its_slot_is_replaced_is_refused_and_the_new_slot_kept(
        self, tmp_path, monkeypatch
    ):
        state, slot = _warm_state(tmp_path)
        replacement = SimpleNamespace(jev_range=dict(NO_RANGE), key="s1", _app="")

        async def _replace_during_the_owner_check(request, operation):
            state._slots["s1"] = replacement

        monkeypatch.setattr(
            chat_jev_range, "require_owner_dashboard_request", _replace_during_the_owner_check
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(URL, json=RANGE)

        assert resp.status == 404
        assert replacement.jev_range == NO_RANGE

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "fields"),
        [
            ({"models": [], "effort_min": "extreme", "effort_max": ""}, ("effort_min",)),
            ({"models": [], "effort_min": "", "effort_max": "extreme"}, ("effort_max",)),
            (
                {"models": [], "effort_min": "high", "effort_max": "low"},
                ("effort_min", "effort_max"),
            ),
        ],
    )
    async def test_a_bad_effort_is_refused_naming_the_field(self, tmp_path, body, fields):
        state, slot = _warm_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            assert (await client.post(URL, json=RANGE)).status == 200
            resp = await client.post(URL, json=body)
            data = await resp.json()

        assert resp.status == 400
        assert data["code"] == "jev_range_invalid"
        assert any(field in data["error"] for field in fields)
        assert slot.to_dict()["jev_range"] == RANGE

    @pytest.mark.asyncio
    async def test_an_id_the_warm_advertised_list_lacks_is_refused_naming_the_id(self, tmp_path):
        state, slot = _warm_state(tmp_path)
        body = {"models": ["model-a", "ghost-model"], "effort_min": "", "effort_max": ""}

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(URL, json=body)
            data = await resp.json()

        assert resp.status == 400
        assert data["code"] == "jev_range_invalid"
        assert "ghost-model" in data["error"]
        assert slot.to_dict()["jev_range"] == NO_RANGE


class TestNeverInTranscriptMetadata:
    """A range bounds what an owner's credential may spend; the transcript line is agent-writable."""

    @pytest.mark.asyncio
    async def test_the_range_is_neither_written_to_nor_read_from_the_metadata_line(self, tmp_path):
        state, slot = _warm_state(tmp_path)
        slot.append("user", "hello")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            assert (await client.post(URL, json=RANGE)).status == 200
        _save_slot_to_history(state, slot, closed=False)

        assert "jev_range" not in state.conversation_log._read_metadata("dashboard:s1")

        del state._slots["s1"]
        path = state.conversation_log._path("dashboard:s1")
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = json.dumps({**json.loads(lines[0]), "jev_range": RANGE})
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        restored = _rehydrate_slot_from_history(state, "s1")

        assert restored is not None
        assert restored.to_dict()["jev_range"] == NO_RANGE


class TestCodexEffortPairs:
    """A codex session advertises ``m[low]``/``m[high]``; the picker posts the base name."""

    @staticmethod
    def _paired_state(tmp_path):
        state, slot = _warm_state(tmp_path)
        pairs = ["m[low]", "m[high]"]
        state.sessions.get_provider.return_value = SimpleNamespace(
            available_models=lambda: [{"modelId": m} for m in pairs]
        )
        return state, slot

    @pytest.mark.asyncio
    @pytest.mark.parametrize("posted", ["m", "m[high]"])
    async def test_a_base_name_or_a_pair_id_is_accepted_and_stored_as_posted(
        self, tmp_path, posted
    ):
        state, slot = self._paired_state(tmp_path)
        body = {"models": [posted], "effort_min": "", "effort_max": ""}

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(URL, json=body)

        assert resp.status == 200
        assert slot.jev_range["models"] == [posted]

    @pytest.mark.asyncio
    async def test_a_model_the_session_does_not_offer_is_still_refused(self, tmp_path):
        state, slot = self._paired_state(tmp_path)
        body = {"models": ["other"], "effort_min": "", "effort_max": ""}

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(URL, json=body)
            data = await resp.json()

        assert resp.status == 400
        assert "models not offered by this session: other" in json.dumps(data)
        assert slot.jev_range == NO_RANGE
