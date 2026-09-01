"""Tests for ``api_sessions_search`` handler."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_sessions_search
from kiro_crew.history import ConversationLog


def _make_app(log: ConversationLog) -> web.Application:
    # SimpleNamespace documents exactly which DashboardState attributes the
    # handler depends on; adding a new one will raise a clear AttributeError
    # instead of inheriting real DashboardState behavior via __new__.
    from types import SimpleNamespace
    state = SimpleNamespace(conversation_log=log)
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/sessions/search", api_sessions_search)
    return app


class TestSessionsSearchHandler:
    @pytest.mark.asyncio
    async def test_short_query_returns_empty(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "anything")
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/search?q=a")
            assert resp.status == 200
            assert (await resp.json())["sessions"] == []

    @pytest.mark.asyncio
    async def test_matches_content(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "discussed TICKET-1234567 today")
        log.append("beta", "user", "unrelated")
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/search?q=TICKET-1234567")
            keys = [s["key"] for s in (await resp.json())["sessions"]]
            assert keys == ["alpha"]

    @pytest.mark.asyncio
    async def test_invalid_limit_falls_back_to_default(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "hello world")
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/search?q=hello&limit=abc")
            assert resp.status == 200
            assert "sessions" in await resp.json()

    @pytest.mark.asyncio
    async def test_negative_limit_is_clamped(self, tmp_path):
        """Negative limit must be clamped to a positive value."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "hello world")
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/search?q=hello&limit=-1")
            assert resp.status == 200
            assert [s["key"] for s in (await resp.json())["sessions"]] == ["alpha"]

    @pytest.mark.asyncio
    async def test_query_with_control_chars_is_sanitized(self, tmp_path):
        """Null bytes / control chars in ``q`` must not crash the handler."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "hello world")
        async with TestClient(TestServer(_make_app(log))) as client:
            # %00 is a null byte — sanitize_string strips control chars.
            resp = await client.get("/api/sessions/search?q=hello%00")
            assert resp.status == 200
            assert [s["key"] for s in (await resp.json())["sessions"]] == ["alpha"]

    @pytest.mark.asyncio
    async def test_title_is_redacted(self, tmp_path, monkeypatch):
        """LLM-generated titles must be sanitized through redact helpers."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "matches query")
        # Inject a title with a credential-like pattern via monkeypatch
        calls = []

        def _fake_redact_creds(text):
            calls.append(("creds", text))
            return ("[REDACTED]", [])

        def _fake_redact_urls(text):
            calls.append(("urls", text))
            return (text, [])

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.redact_credentials", _fake_redact_creds
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.redact_exfiltration_urls", _fake_redact_urls
        )
        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/search?q=matches")
            sessions = (await resp.json())["sessions"]
            assert sessions and sessions[0]["title"] == "[REDACTED]"
            assert any(c[0] == "creds" for c in calls)
            assert any(c[0] == "urls" for c in calls)
