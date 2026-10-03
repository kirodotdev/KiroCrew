"""Tests for ``api_sessions_summarize`` — one-line LLM summaries for sessions.

The background LLM session is faked (SimpleNamespace) so the handler's
event-loop + best-effort fallback logic is exercised without a real provider.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import move_transcript_past

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.dashboard.handlers import api_sessions_summarize
from kiro_crew.dashboard.handlers import sessions as sessions_handlers
from kiro_crew.history import ConversationLog, HistoryLockTimeout, TranscriptBusy


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
    async def _get_bg_session(start_priority=None):
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
            move_transcript_past(log, "alpha", sig)  # don't rely on the OS tick
            await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
        assert len(created) == 2

    @pytest.mark.asyncio
    async def test_summarize_never_rewrites_session_file(self, tmp_path):
        """The summary cache lives in a sidecar, never the session JSONL.

        Summarizing must not read-modify-write
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

    @pytest.mark.asyncio
    async def test_restricted_line_never_serves_a_cached_summary(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "private topic")
        sig = log.session_mtime("alpha")
        assert sig is not None
        log.set_cached_summary("alpha", "Private cached summary", sig)
        log.update_metadata("alpha", {"memory_mode": "incognito"})
        created: list = []

        async with TestClient(TestServer(_make_app(log, "unused", created))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            assert resp.status == 200
            assert (await resp.json())["summaries"] == {}

        assert created == []

    @pytest.mark.asyncio
    async def test_lock_timeout_while_reading_cache_returns_empty(self, tmp_path, monkeypatch):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "private topic")
        created: list = []
        state = _make_app(log, "unused", created)["state"]

        @contextlib.contextmanager
        def _timeout(self, stems):
            raise HistoryLockTimeout("summary cache read lock held")
            yield  # pragma: no cover

        monkeypatch.setattr(type(log), "locked_stems", _timeout)
        assert await sessions_handlers._summarize_one(state, "alpha") == ""
        assert created == []

    @pytest.mark.asyncio
    async def test_lock_timeout_while_publishing_withholds_summary_and_cache(
        self, tmp_path, monkeypatch, caplog
    ):
        """Contention at publication withholds the unverifiable summary and cache."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "private topic")
        created: list = []
        state = _make_app(log, "Private derived summary", created)["state"]
        real_locked_stems = type(log).locked_stems
        calls = {"n": 0}

        @contextlib.contextmanager
        def _timeout_on_publish(self, stems):
            calls["n"] += 1
            if calls["n"] == 3:
                raise HistoryLockTimeout("summary publication lock held")
            with real_locked_stems(self, stems):
                yield

        monkeypatch.setattr(type(log), "locked_stems", _timeout_on_publish)
        with caplog.at_level(logging.DEBUG, logger=sessions_handlers.__name__):
            assert await sessions_handlers._summarize_one(state, "alpha") == ""
        assert calls["n"] == 3
        assert created and created[0].destroyed
        assert not log._summary_cache_path("alpha").exists()
        assert (
            "Summary for alpha withheld: the transcript lock was busy at publication" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_a_busy_publish_uses_the_busy_arm_and_fails_closed(self, tmp_path, monkeypatch):
        """Busy and Withheld use distinct arms but both fail closed."""

        @contextlib.contextmanager
        def _busy(self, key):
            raise TranscriptBusy("held elsewhere")
            yield  # pragma: no cover

        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "private topic")
        state = _make_app(log, "Private derived summary", [])["state"]
        monkeypatch.setattr(type(log), "publication_hold", _busy)
        assert await sessions_handlers._summarize_one(state, "alpha") == ""
        assert not log._summary_cache_path("alpha").exists()

    @pytest.mark.asyncio
    async def test_line_tightening_during_model_call_discards_summary(
        self, tmp_path, monkeypatch, caplog
    ):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "private topic")
        cache_path = log._summary_cache_path("alpha")
        model_called: list[str] = []

        async def tighten_then_reply(*_args, **_kwargs):
            model_called.append("yes")
            await asyncio.to_thread(log.update_metadata, "alpha", {"memory_mode": "incognito"})
            return "Private derived summary"

        monkeypatch.setattr(sessions_handlers, "run_bg_oneliner", tighten_then_reply)
        with caplog.at_level(logging.DEBUG, logger=sessions_handlers.__name__):
            async with TestClient(TestServer(_make_app(log, "unused", []))) as c:
                resp = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
                assert resp.status == 200
                assert (await resp.json())["summaries"] == {}

        assert model_called == ["yes"]
        assert not cache_path.exists()
        assert (
            "Discarding summary for alpha: the transcript became restricted during "
            "summarisation" in caplog.text
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply",
        [
            "I cannot summarize this conversation without more context",
            "Sorry, the transcript does not contain enough information",
            "As an AI, I do not have access to the linked document",
            "SKIP, the topic is unclear",
            "SKIP - only a greeting so far",
            "SKIP\n\nThe topic is unclear.",
            "The conversation is too vague to summarize",
            "This conversation does not contain enough information",
            "Based on the transcript, I cannot determine a topic",
        ],
    )
    async def test_a_refusal_or_verdict_reason_is_not_served_or_cached(self, tmp_path, reply):
        """An exact ``summary.upper() == "SKIP"`` test lets a refusal sentence
        (or ``SKIP, <reason>``) through to the sidecar cache, where it is served
        on every later list until the transcript changes. Neither may be
        returned NOR cached. Mutation:
        drop the ``looks_like_prose`` branch / restore the exact-equality test
        -- red on the response AND on the cache read."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "help me tune the redis timeout")
        created: list = []
        async with TestClient(TestServer(_make_app(log, reply, created))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            assert resp.status == 200
            assert (await resp.json())["summaries"] == {}
        assert created, "the model was consulted"
        assert not log.get_cached_summary("alpha")

    @pytest.mark.asyncio
    async def test_only_the_first_line_of_a_multi_line_reply_is_the_summary(self, tmp_path):
        """The prompt asks for ONE line; an unasked-for explanation after it is
        not part of the summary. Mutation: keep the whole reply -- red."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "help me tune the redis timeout")
        reply = "Tuning the redis client timeout\n\nThe user also mentioned retries."
        async with TestClient(TestServer(_make_app(log, reply, []))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            assert (await resp.json())["summaries"]["alpha"] == "Tuning the redis client timeout"

    @pytest.mark.asyncio
    async def test_a_legitimate_long_summary_is_still_stored(self, tmp_path):
        """The summary contract is 18 words, three times the title's, so the
        title's 12-word ceiling must NOT apply here. Mutation: call the guard
        with its title defaults -- red."""
        reply = (
            "User and assistant tune the redis client timeout, add retry with "
            "backoff and verify against staging"
        )
        assert 12 < len(reply.split()) <= 18
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "help me tune the redis timeout")
        async with TestClient(TestServer(_make_app(log, reply, []))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            assert (await resp.json())["summaries"]["alpha"] == reply
        assert log.get_cached_summary("alpha") == reply

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply",
        [
            "The conversation covers tuning the redis client timeout",
            "This conversation is about a Tailwind v4 migration",
            "Based on the transcript, user and assistant debug a flaky login test",
            "User tunes the redis timeout. Assistant adds retry with backoff",
            "\uc0ac\uc6a9\uc790\uac00 redis \ud0c0\uc784\uc544\uc6c3\uc744 \uc870\uc815\ud569\ub2c8\ub2e4",
        ],
    )
    async def test_a_summary_shaped_reply_is_stored_and_cached(self, tmp_path, reply):
        """A summary DESCRIBES the conversation, so the title guard's narration
        openers, mid-line terminator and Korean polite ending are its legitimate
        shape here. Rejecting one loses the summary AND re-asks the model on
        every later list, because "" is never cached. The exemption is for the
        affirmative shape only: a refusal opening the same way is still
        rejected (see the refusal rows above). Mutation: pass the title
        defaults for ``openers`` / ``sentence_shape`` -- red."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "help me tune the redis timeout")
        async with TestClient(TestServer(_make_app(log, reply, []))) as c:
            resp = await c.post("/api/sessions/summarize", json={"keys": ["alpha"]})
            assert (await resp.json())["summaries"]["alpha"] == reply
        assert log.get_cached_summary("alpha") == reply
