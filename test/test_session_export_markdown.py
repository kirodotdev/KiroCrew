"""Tests for ``api_session_export`` handler (Markdown export)."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_session_export
from kiro_crew.history import ConversationLog


def _make_app(log: ConversationLog) -> web.Application:
    from types import SimpleNamespace

    state = SimpleNamespace(conversation_log=log)
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/sessions/{key}/export", api_session_export)
    return app


class TestSessionExportMarkdown:
    @pytest.mark.asyncio
    async def test_basic_export(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("sess1", "user", "Hello there")
        log.append("sess1", "assistant", "Hi! How can I help?")

        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/sess1/export")
            assert resp.status == 200
            assert resp.content_type == "text/markdown"
            assert "attachment" in resp.headers.get("Content-Disposition", "")
            body = await resp.text()
            assert "# " in body
            assert "**You:**" in body
            assert "**Assistant:**" in body
            assert "Hello there" in body
            assert "Hi! How can I help?" in body

    @pytest.mark.asyncio
    async def test_export_with_format_param(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("sess2", "user", "test message")

        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/sess2/export?format=markdown")
            assert resp.status == 200
            assert resp.content_type == "text/markdown"

    @pytest.mark.asyncio
    async def test_unsupported_format_returns_400(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("sess3", "user", "test")

        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/sess3/export?format=pdf")
            assert resp.status == 400
            data = await resp.json()
            assert "unsupported format" in data["error"]

    @pytest.mark.asyncio
    async def test_missing_session_returns_404(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)

        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/nonexistent/export")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_no_conversation_log_returns_400(self, tmp_path):
        from types import SimpleNamespace

        state = SimpleNamespace(conversation_log=None)
        app = web.Application()
        app["state"] = state
        app.router.add_get("/api/sessions/{key}/export", api_session_export)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/sessions/any/export")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_export_contains_separator(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("sess4", "user", "msg1")
        log.append("sess4", "assistant", "msg2")

        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/sess4/export")
            body = await resp.text()
            assert body.count("---") >= 2

    @pytest.mark.asyncio
    async def test_export_filename_contains_key(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("my-session", "user", "hello")

        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/my-session/export")
            disposition = resp.headers.get("Content-Disposition", "")
            assert "my-session" in disposition

    @pytest.mark.asyncio
    async def test_system_and_tool_roles(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("sess5", "system", "system prompt")
        log.append("sess5", "tool", "tool output")

        async with TestClient(TestServer(_make_app(log))) as client:
            resp = await client.get("/api/sessions/sess5/export")
            body = await resp.text()
            assert "**System:**" in body
            assert "**Tool:**" in body
