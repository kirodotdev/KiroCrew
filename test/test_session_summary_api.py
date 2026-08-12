"""Tests for GET /api/chat/slots/{slot}/summary.

The endpoint is deliberately read-only: opening the panel must never spend
tokens, so these assert it serves the cache and nothing more.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, move_transcript_past

from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import api_chat_slot_summary
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot

pytestmark = pytest.mark.asyncio


def _payload(title="set up auth"):
    return {
        "intents": [
            {
                "title": title,
                "ranges": [[1, 2]],
                "status": "completed",
                "verified": False,
                "state": "needs-you",
                "last_touched_turn": 2,
            }
        ],
        "constraints": ["restart the worker after a config change"],
        "generated_at": 1_760_000_000.0,
        "user_turns": 2,
        "last_activity": "2026-08-10T10:00:00+00:00",
    }


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots/{slot}/summary", api_chat_slot_summary)
    return app


def _pin_flag(monkeypatch, enabled: bool) -> None:
    def _load():
        cfg = KiroCrewConfig()
        cfg.session_summary = SessionSummaryConfig(enabled=enabled)
        return cfg

    monkeypatch.setattr(chat_handlers.KiroCrewConfig, "load", staticmethod(_load))


class TestSummaryEndpoint:
    async def test_unknown_slot_is_404_with_a_machine_readable_code(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/nope/summary")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    async def test_a_slot_with_no_summary_returns_empty_not_an_error(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s1/summary")
            assert resp.status == 200
            body = await resp.json()
            assert body["intents"] == []
            assert body["generated_at"] is None
            assert body["stale"] is False

    async def test_serves_a_fresh_cached_summary(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["stale"] is False
        assert body["intents"][0]["title"] == "set up auth"
        assert body["intents"][0]["state"] == "needs-you"
        assert body["constraints"] == ["restart the worker after a config change"]
        assert body["generated_at"] == 1_760_000_000.0
        assert body["user_turns"] == 2

    async def test_a_stale_summary_is_served_and_flagged(self, tmp_path, monkeypatch):
        """Better a summary marked out of date than an empty panel."""
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        sig = log.session_mtime(hkey)
        log.set_cached_intent_summary(hkey, _payload(), sig)
        log.append(hkey, "user", "a newer turn")
        move_transcript_past(log, hkey, sig)  # don't rely on the OS tick (#2981)

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["stale"] is True
        assert body["intents"][0]["title"] == "set up auth"

    async def test_reports_the_feature_flag_so_the_panel_can_explain_itself(
        self, tmp_path, monkeypatch
    ):
        _pin_flag(monkeypatch, False)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s1/summary")
            assert resp.status == 200
            assert (await resp.json())["enabled"] is False

    async def test_a_corrupt_sidecar_degrades_to_empty(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        path = log._intent_summary_cache_path(hkey)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s1/summary")
            assert resp.status == 200
            assert (await resp.json())["intents"] == []

    async def test_the_endpoint_never_generates(self, tmp_path, monkeypatch):
        """Opening the panel must not spend tokens."""
        from kiro_crew.dashboard import chat_summary

        called: list[int] = []

        async def fake(*a, **k):
            called.append(1)
            return ""

        monkeypatch.setattr(chat_summary, "run_bg_oneliner", fake)
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.get("/api/chat/slots/s1/summary")
        assert called == []

    async def test_disabling_the_flag_stops_serving_an_earlier_summary(
        self, tmp_path, monkeypatch
    ):
        """Opting out has to stop serving, not just stop producing."""
        _pin_flag(monkeypatch, False)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["enabled"] is False
        assert body["intents"] == []
        assert body["constraints"] == []
        assert body["generated_at"] is None


class TestSummaryAppIsolation:
    """App Kit §5.2: a summary is conversation content, not public metadata."""

    @staticmethod
    def _app_client_app(state, caller: str) -> web.Application:
        app = _make_app(state)

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = caller
            return await handler(request)

        app.middlewares.insert(0, inject_app)
        return app

    async def test_a_foreign_app_cannot_read_another_apps_summary(
        self, tmp_path, monkeypatch
    ):
        mock_sel = MagicMock()
        monkeypatch.setattr(chat_handlers, "sel", lambda: mock_sel)
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        slot._app = "app-B"
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(self._app_client_app(state, "app-A"))) as client:
            resp = await client.get("/api/chat/slots/s1/summary")
            # 404, not 403: a foreign slot must be indistinguishable from a
            # missing one (anti-enumeration). True reason lands in SEL.
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

        denied = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied) == 1
        assert denied[0][1]["source"] == "app_isolation"

    async def test_an_app_cannot_read_an_unscoped_slots_summary(self, tmp_path, monkeypatch):
        mock_sel = MagicMock()
        monkeypatch.setattr(chat_handlers, "sel", lambda: mock_sel)
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")  # _app stays empty

        async with TestClient(TestServer(self._app_client_app(state, "app-A"))) as client:
            assert (await client.get("/api/chat/slots/s1/summary")).status == 404

    async def test_the_owning_app_still_reads_its_own_summary(self, tmp_path, monkeypatch):
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        slot._app = "app-A"
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(self._app_client_app(state, "app-A"))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["intents"][0]["title"] == "set up auth"

    async def test_a_dashboard_user_reads_an_app_owned_summary(self, tmp_path, monkeypatch):
        """An explicit empty request_app is the dashboard user and bypasses the check."""
        _pin_flag(monkeypatch, True)
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        slot._app = "app-B"
        state._slots[slot.key] = slot
        hkey = slot_history_key(slot)
        log = state.conversation_log
        log.append(hkey, "user", "hello")
        log.set_cached_intent_summary(hkey, _payload(), log.session_mtime(hkey))

        async with TestClient(TestServer(self._app_client_app(state, ""))) as client:
            body = await (await client.get("/api/chat/slots/s1/summary")).json()
        assert body["intents"][0]["title"] == "set up auth"
