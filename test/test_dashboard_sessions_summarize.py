"""Tests for ``api_sessions_summarize`` — one-line LLM summaries for sessions.

The background LLM session is faked (SimpleNamespace) so the handler's
event-loop + best-effort fallback logic is exercised without a real provider.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import move_transcript_past

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.dashboard.handlers import api_sessions_summarize
from kiro_crew.history import ConversationLog


class _FakeBgSession:
    """Minimal stand-in for a background LLM session handle."""

    def __init__(self, reply: str):
        self._reply = reply
        self.destroyed = False

    async def set_model(self, model):  # noqa: D401 — best-effort no-op
        return None

    async def prompt(self, _prompt):
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=self._reply)
        yield SimpleNamespace(kind=EVENT_COMPLETE, text="")

    async def reject_tool(self, _request_id):
        return None

    async def destroy(self):
        self.destroyed = True


def _make_app(log: ConversationLog, reply: str, created: list) -> web.Application:
    async def _get_bg_session():
        s = _FakeBgSession(reply)
        created.append(s)
        return s

    sessions = SimpleNamespace(get_bg_session=_get_bg_session)
    state = SimpleNamespace(conversation_log=log, sessions=sessions)
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/sessions/summarize", api_sessions_summarize)
    return app


class TestSessionsSummarizeHandler:
    @pytest.mark.asyncio
    async def test_summarizes_requested_keys(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "help me tune the redis timeout")
        created: list = []
        async with TestClient(TestServer(_make_app(log, "Tuning redis timeout", created))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            assert resp.status == 200
            body = await resp.json()
            assert body["summaries"]["alpha"] == "Tuning redis timeout"
        # The ephemeral bg session was destroyed.
        assert created and created[0].destroyed

    @pytest.mark.asyncio
    async def test_skip_reply_is_dropped(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "hi")
        async with TestClient(TestServer(_make_app(log, "SKIP", []))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            assert (await resp.json())["summaries"] == {}

    @pytest.mark.asyncio
    async def test_unknown_key_skipped(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "hello")
        async with TestClient(TestServer(_make_app(log, "X", []))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": ["ghost"]})
            assert (await resp.json())["summaries"] == {}

    @pytest.mark.asyncio
    async def test_bad_body_is_rejected(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        async with TestClient(TestServer(_make_app(log, "X", []))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": "notalist"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_count_is_bounded(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(20):
            log.append(f"s{i}", "user", f"topic {i}")
        created: list = []
        async with TestClient(TestServer(_make_app(log, "sum", created))) as c:
            resp = await c.post(
                "/api/sessions/summarize",
                json={"keys": [f"s{i}" for i in range(20)]},
            )
            assert resp.status == 200
        # Only the bounded top-N sessions triggered an LLM pass.
        from kiro_crew.dashboard.handlers.sessions import _SUMMARIZE_MAX_SESSIONS

        assert len(created) == _SUMMARIZE_MAX_SESSIONS

    @pytest.mark.asyncio
    async def test_cache_hit_skips_llm_on_unchanged_session(self, tmp_path):
        """A repeat summarize for an unchanged session reuses the cached summary
        and does NOT spin up another background session."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "help me tune the redis timeout")
        created: list = []
        async with TestClient(TestServer(_make_app(log, "Tuning redis timeout", created))) as c:
            r1 = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            r2 = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            assert (await r1.json())["summaries"]["alpha"] == "Tuning redis timeout"
            assert (await r2.json())["summaries"]["alpha"] == "Tuning redis timeout"
        # First call generated (1 bg session); second was a pure cache hit (0 more).
        assert len(created) == 1

    @pytest.mark.asyncio
    async def test_cache_invalidated_when_session_changes(self, tmp_path):
        """A new message bumps the session mtime, invalidating the cached summary
        so the next call regenerates."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "first topic")
        created: list = []
        async with TestClient(TestServer(_make_app(log, "sum", created))) as c:
            await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            # New activity in the session — mtime advances, cache is stale.
            sig = log.session_mtime("alpha")  # what the first call cached against
            log.append("alpha", "user", "a new turn changes the transcript")
            move_transcript_past(log, "alpha", sig)  # don't rely on the OS tick (#2981)
            await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
        assert len(created) == 2

    @pytest.mark.asyncio
    async def test_summarize_never_rewrites_session_file(self, tmp_path):
        """The summary cache lives in a sidecar, never the session JSONL.

        Regression for the data-loss race: summarizing must not read-modify-write
        the session log (an append landing mid-rewrite would be clobbered) and
        must not bump its mtime (which would reorder list_sessions)."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "help me tune the redis timeout")
        session_path = tmp_path / "alpha.jsonl"
        before_bytes = session_path.read_bytes()
        before_mtime = session_path.stat().st_mtime
        async with TestClient(TestServer(_make_app(log, "Tuning redis timeout", []))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            assert resp.status == 200
        # Session log is byte-for-byte unchanged and its mtime did not advance.
        assert session_path.read_bytes() == before_bytes
        assert session_path.stat().st_mtime == before_mtime
        # The summary was cached in a sidecar and is reusable.
        assert log.get_cached_summary("alpha") == "Tuning redis timeout"
