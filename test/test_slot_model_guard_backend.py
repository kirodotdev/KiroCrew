"""POST /api/chat/slots/{slot}/model and /api/chat/slots/model: the display-only
guard is judged on the harness each slot runs on."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.state import DashboardState


class TestSlotModelGuardFollowsTheBackend:
    """Whether a canonical key is display-only is the BACKEND's model-id namespace
    question: on ``acp_backend=claude`` the key is the picker's own row and
    ``_wire_model_id`` translates it, so the guard must let it through -- while a
    member DM on an ``acp``-namespace harness still refuses it."""

    @staticmethod
    def _app(state: DashboardState) -> web.Application:
        from kiro_crew.dashboard.chat import api_chat_slot_model, api_chat_slots_model

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
        app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
        return app

    @staticmethod
    def _claude_key() -> str:
        """A canonical key the claude_code namespace translates, read off the registry."""
        from kiro_crew import model_registry

        return next(
            e["model_name"]
            for e in model_registry.display_list("claude_code")
            if model_registry.is_canonical_key(e["model_name"])
            and model_registry.to_provider_id(e["model_name"], "claude_code")
        )

    @staticmethod
    def _configure(monkeypatch, *, default: str, member: str) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        original = KiroCrewConfig.load

        def load(*args, **kwargs):
            cfg = original(*args, **kwargs)
            cfg.agent.acp_backend = default
            cfg.agent.member_acp_backend = member
            return cfg

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(load))

    @pytest.mark.asyncio
    async def test_claude_backend_accepts_a_canonical_key(self, tmp_path, monkeypatch):
        from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS

        self._configure(monkeypatch, default=ACP_BACKEND_CLAUDE, member=ACP_BACKEND_KAS)
        key = self._claude_key()
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": key})

        assert resp.status == 200, await resp.text()
        assert state._slots["a"].model == key

    @pytest.mark.asyncio
    async def test_kiro_backend_still_refuses_it(self, tmp_path, monkeypatch):
        from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO

        self._configure(monkeypatch, default=ACP_BACKEND_KIRO, member=ACP_BACKEND_CLAUDE)
        key = self._claude_key()
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": key})
            data = await resp.json()

        assert resp.status == 400
        assert key in data["error"]
        assert "provider" not in data["error"]
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_member_dm_is_judged_on_the_member_backend(self, tmp_path, monkeypatch):
        from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS

        self._configure(monkeypatch, default=ACP_BACKEND_CLAUDE, member=ACP_BACKEND_KAS)
        key = self._claude_key()
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("member-ada", model="", mode="member")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/member-ada/model", json={"model": key})

        assert resp.status == 400
        assert state._slots["member-ada"].model == ""

    @pytest.mark.asyncio
    async def test_bulk_pick_must_pass_on_every_slots_backend(self, tmp_path, monkeypatch):
        from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS

        self._configure(monkeypatch, default=ACP_BACKEND_CLAUDE, member=ACP_BACKEND_KAS)
        key = self._claude_key()
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("a", model="")
        state.get_or_create_slot("member-ada", model="", mode="member")
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": key})

        assert resp.status == 400
        assert state._slots["a"].model == ""
